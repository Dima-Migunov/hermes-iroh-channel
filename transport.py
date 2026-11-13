"""Iroh transport for the Hermes Agent Iroh channel plugin.

The module is independent of Hermes: it owns the node identity, relay
configuration, client allowlist, frame codec, and the QUIC endpoint. The
Hermes gateway adapter lives in ``gateway_adapter.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALPN = b"iroh-channel/1"
DEFAULT_SESSION_ID = "default"

MAX_PROMPT_BYTES = 64 * 1024
MAX_DELTA_BYTES = 16 * 1024
PROMPT_READ_CHUNK = 4096

PROMPT_TIMEOUT = 30.0
DEFAULT_TURN_TIMEOUT = 600.0
DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_STREAMS_PER_CONNECTION = 8
DEFAULT_MAX_TOTAL_STREAMS = 64
MAX_RECORDED_REJECTIONS = 32

ERR_INVALID_PROMPT = "invalid_prompt"
ERR_PROMPT_TOO_LARGE = "prompt_too_large"
ERR_SESSION_BUSY = "session_busy"
ERR_TIMEOUT = "timeout"
ERR_UNAUTHORIZED = "unauthorized"
ERR_INTERNAL = "internal_error"
ERR_TOO_MANY_STREAMS = "too_many_streams"

ERROR_VALUES = frozenset(
    {
        ERR_INVALID_PROMPT,
        ERR_PROMPT_TOO_LARGE,
        ERR_SESSION_BUSY,
        ERR_TIMEOUT,
        ERR_UNAUTHORIZED,
        ERR_INTERNAL,
        ERR_TOO_MANY_STREAMS,
    }
)

SECRET_KEY_BYTES = 32

_NODE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_DISABLED_RELAY_VALUES = frozenset({"disabled", "off", "none"})


class TransportError(Exception):
    """Base class for transport configuration and runtime failures."""


class KeyStoreError(TransportError):
    """The node key file exists but cannot be used."""


class RelayConfigError(TransportError):
    """The relay override cannot be parsed."""


class PromptError(TransportError):
    """A prompt was rejected; carries a stable protocol error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def iroh_available() -> bool:
    try:
        import iroh  # noqa: F401
    except Exception:
        return False
    return True


def default_state_dir() -> Path:
    override = os.environ.get("HERMES_IROH_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent


def default_key_path() -> Path:
    override = os.environ.get("HERMES_IROH_KEY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return default_state_dir() / "node.key"


def default_allowlist_path() -> Path:
    return default_state_dir() / "allowlist.json"


def turn_timeout_from_env(default: float = DEFAULT_TURN_TIMEOUT) -> float:
    raw = os.environ.get("HERMES_IROH_TURN_TIMEOUT", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise TransportError(
            f"HERMES_IROH_TURN_TIMEOUT is not a number: {raw!r}"
        ) from exc
    if value <= 0:
        raise TransportError(
            f"HERMES_IROH_TURN_TIMEOUT must be positive: {raw!r}"
        )
    return value


def _write_file_atomic(path: Path, payload: bytes, *, prefix: str) -> None:
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            logger.debug("could not set 0700 on %s", parent)
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=prefix, suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    NodeKeyStore._sync_directory(parent)


class IrohSecretBackend:
    """Thin adapter over the iroh secret-key API."""

    def generate(self) -> Any:
        import iroh

        return iroh.SecretKey.generate()

    def load(self, raw: bytes) -> Any:
        import iroh

        return iroh.SecretKey.from_bytes(raw)

    def to_bytes(self, secret: Any) -> bytes:
        return secret.to_bytes()

    def node_id(self, secret: Any) -> str:
        return str(secret.public())


class NodeKeyStore:
    """Persistent Ed25519 node identity."""

    def __init__(
        self,
        path: Optional[os.PathLike[str] | str] = None,
        backend: Any = None,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else default_key_path()
        self.backend = backend if backend is not None else IrohSecretBackend()
        self._secret: Any = None

    def load_or_create(self) -> Any:
        if self._secret is not None:
            return self._secret
        if self.path.exists():
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise KeyStoreError(
                    f"{self.path}: cannot read node key ({type(exc).__name__})"
                ) from exc
            if len(raw) != SECRET_KEY_BYTES:
                raise KeyStoreError(
                    f"{self.path}: expected a {SECRET_KEY_BYTES}-byte node key, "
                    f"found {len(raw)} bytes"
                )
            try:
                secret = self.backend.load(raw)
            except Exception as exc:
                raise KeyStoreError(
                    f"{self.path}: not a valid node key ({type(exc).__name__})"
                ) from exc
        else:
            secret = self.backend.generate()
            raw = self.backend.to_bytes(secret)
            if len(raw) != SECRET_KEY_BYTES:
                raise KeyStoreError(
                    f"generated node key has {len(raw)} bytes, expected "
                    f"{SECRET_KEY_BYTES}"
                )
            self._write_atomic(raw)
        self._secret = secret
        return secret

    def node_id(self) -> str:
        return self.backend.node_id(self.load_or_create())

    def _write_atomic(self, raw: bytes) -> None:
        _write_file_atomic(self.path, raw, prefix=".node.key.")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


@dataclass(frozen=True)
class RelayConfig:
    """Parsed ``HERMES_IROH_RELAY`` value."""

    value: Optional[str]
    kind: str
    urls: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.kind == "disabled":
            return "disabled"
        if self.kind == "custom":
            return ",".join(self.urls)
        return "default"


def parse_relay_setting(value: Optional[str]) -> RelayConfig:
    raw = (value or "").strip()
    if not raw:
        return RelayConfig(None, "default")
    if raw.lower() in _DISABLED_RELAY_VALUES:
        return RelayConfig(raw, "disabled")
    urls = tuple(part.strip() for part in raw.split(","))
    if not urls or any(not url for url in urls):
        raise RelayConfigError(
            f"HERMES_IROH_RELAY contains an empty relay URL: {raw!r}"
        )
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RelayConfigError(
                f"HERMES_IROH_RELAY contains an invalid relay URL: {url!r}"
            )
    return RelayConfig(raw, "custom", urls)


def build_relay_mode(config: RelayConfig) -> Any:
    import iroh

    if config.kind == "disabled":
        return iroh.RelayMode.disabled()
    if config.kind == "custom":
        try:
            return iroh.RelayMode.custom_from_urls(list(config.urls))
        except Exception as exc:
            raise RelayConfigError(
                f"HERMES_IROH_RELAY could not be applied: {config.value!r}"
            ) from exc
    return iroh.RelayMode.default_mode()


def normalize_node_id(value: str) -> str:
    return value.strip().lower()


def validate_node_id(value: str) -> str:
    normalized = normalize_node_id(value)
    if not _NODE_ID_RE.match(normalized):
        raise ValueError(
            "expected a 64-character hexadecimal node id"
        )
    if iroh_available():
        import iroh

        try:
            iroh.EndpointId.from_string(normalized)
        except Exception as exc:
            raise ValueError("not a valid Iroh node id") from exc
    return normalized


class Allowlist:
    """Fail-closed client allowlist backed by a JSON file and an env var."""

    ENV_VAR = "HERMES_IROH_ALLOWLIST"

    def __init__(
        self,
        path: Optional[os.PathLike[str] | str] = None,
        env_reader: Optional[Callable[[], str]] = None,
        validate: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.path = (
            Path(path).expanduser()
            if path is not None
            else default_allowlist_path()
        )
        self._env_reader = env_reader or self._read_env
        self._validate = validate or validate_node_id
        self._lock = asyncio.Lock()
        self._file_entries: frozenset[str] = frozenset()
        self._file_stamp: Optional[tuple[int, int]] = None
        self._loaded = False

    def _read_env(self) -> str:
        return os.environ.get(self.ENV_VAR, "")

    def _file_stamp_now(self) -> Optional[tuple[int, int]]:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload_if_changed(self) -> None:
        stamp = self._file_stamp_now()
        if self._loaded and stamp == self._file_stamp:
            return
        self._loaded = True
        self._file_stamp = stamp
        if stamp is None:
            self._file_entries = frozenset()
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "iroh allowlist file %s is unusable (%s); no file entries active",
                self.path,
                type(exc).__name__,
            )
            self._file_entries = frozenset()
            return
        entries = payload.get("allowed_node_ids") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            logger.warning(
                "iroh allowlist file %s is unusable (invalid schema); "
                "no file entries active",
                self.path,
            )
            self._file_entries = frozenset()
            return
        normalized = {
            normalize_node_id(entry)
            for entry in entries
            if isinstance(entry, str) and normalize_node_id(entry)
        }
        self._file_entries = frozenset(normalized)

    def _env_entries(self) -> frozenset[str]:
        raw = self._env_reader() or ""
        parts = re.split(r"[,\s]+", raw.strip())
        return frozenset(
            normalize_node_id(part) for part in parts if part.strip()
        )

    def entries(self) -> frozenset[str]:
        self._reload_if_changed()
        return self._file_entries | self._env_entries()

    def check(self, node_id: str) -> bool:
        return normalize_node_id(node_id) in self.entries()

    async def add(self, node_id: str) -> str:
        normalized = self._validate(node_id)
        async with self._lock:
            self._reload_if_changed()
            current = set(self._file_entries)
            current.add(normalized)
            self._write_atomic(sorted(current))
            self._file_entries = frozenset(current)
            self._file_stamp = self._file_stamp_now()
            self._loaded = True
        return normalized

    def _write_atomic(self, entries: Iterable[str]) -> None:
        payload = json.dumps(
            {"allowed_node_ids": list(entries)}, indent=2, ensure_ascii=False
        ) + "\n"
        _write_file_atomic(
            self.path, payload.encode("utf-8"), prefix=".allowlist."
        )


@dataclass(frozen=True)
class Prompt:
    session_id: str
    text: str


def encode_frame(frame: dict) -> bytes:
    return (
        json.dumps(
            frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def start_frame(session_id: str) -> dict:
    return {"type": "start", "session_id": session_id}


def delta_frame(content: str) -> dict:
    return {"type": "delta", "content": content}


def done_frame() -> dict:
    return {"type": "done"}


def error_frame(message: str) -> dict:
    return {"type": "error", "message": message}


def parse_prompt(raw: bytes) -> Prompt:
    if len(raw) > MAX_PROMPT_BYTES:
        raise PromptError(ERR_PROMPT_TOO_LARGE, "prompt exceeds the size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptError(ERR_INVALID_PROMPT, "prompt is not valid UTF-8") from exc
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PromptError(ERR_INVALID_PROMPT, "prompt is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PromptError(ERR_INVALID_PROMPT, "prompt must be a JSON object")
    if payload.get("type") != "prompt":
        raise PromptError(ERR_INVALID_PROMPT, 'prompt type must be "prompt"')
    body = payload.get("text")
    if not isinstance(body, str) or body == "":
        raise PromptError(ERR_INVALID_PROMPT, "prompt text must be a non-empty string")
    session_id = payload.get("session_id", DEFAULT_SESSION_ID)
    if session_id is None:
        session_id = DEFAULT_SESSION_ID
    if not isinstance(session_id, str):
        raise PromptError(ERR_INVALID_PROMPT, "session_id must be a string")
    session_id = session_id.strip() or DEFAULT_SESSION_ID
    return Prompt(session_id=session_id, text=body)


async def read_prompt_line(
    recv: Any,
    *,
    limit: int = MAX_PROMPT_BYTES,
    timeout: float = PROMPT_TIMEOUT,
) -> bytes:
    async def _read() -> bytes:
        buffer = bytearray()
        while True:
            chunk = await recv.read(PROMPT_READ_CHUNK)
            if not chunk:
                if not buffer:
                    raise PromptError(ERR_INVALID_PROMPT, "prompt stream is empty")
                return bytes(buffer)
            newline = chunk.find(b"\n")
            if newline >= 0:
                buffer.extend(chunk[:newline])
                if len(buffer) > limit:
                    raise PromptError(
                        ERR_PROMPT_TOO_LARGE, "prompt line exceeds the size limit"
                    )
                return bytes(buffer)
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise PromptError(
                    ERR_PROMPT_TOO_LARGE, "prompt line exceeds the size limit"
                )

    try:
        return await asyncio.wait_for(_read(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise PromptError(ERR_TIMEOUT, "prompt was not received in time") from exc


def chunk_text(text: str, limit: int = MAX_DELTA_BYTES) -> list[str]:
    limit = max(limit, 1)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = encoded
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining.decode("utf-8"))
            break
        cut = limit
        while cut > 0 and (remaining[cut] & 0xC0) == 0x80:
            cut -= 1
        if cut == 0:
            cut = max(limit, 1)
            while cut < len(remaining) and (remaining[cut] & 0xC0) == 0x80:
                cut += 1
        chunks.append(remaining[:cut].decode("utf-8"))
        remaining = remaining[cut:]
    return chunks


def make_chat_id(node_id: str, session_id: str) -> str:
    return f"{node_id}:{session_id}"


class StreamSession:
    """Server side of one prompt/response stream."""

    def __init__(
        self,
        send: Any,
        *,
        node_id: str,
        session_id: str,
        chat_id: str,
        max_delta_bytes: int = MAX_DELTA_BYTES,
    ) -> None:
        self._send = send
        self.node_id = node_id
        self.session_id = session_id
        self.chat_id = chat_id
        self.max_delta_bytes = max_delta_bytes
        self.finished = asyncio.Event()
        self.started = False
        self.terminal = False
        self.cancelled = False
        self.deltas_sent = 0
        self.bytes_sent = 0

    def _can_write(self) -> bool:
        return not self.terminal and not self.cancelled

    async def start(self) -> None:
        if self.started or not self._can_write():
            return
        self.started = True
        await self._write(start_frame(self.session_id))

    async def append(self, text: str) -> None:
        if not self._can_write() or not text:
            return
        for chunk in chunk_text(text, self.max_delta_bytes):
            if not self._can_write():
                return
            await self._write(delta_frame(chunk))
            self.deltas_sent += 1
            self.bytes_sent += len(chunk.encode("utf-8"))

    async def finish(self) -> None:
        if not self._can_write():
            return
        self.terminal = True
        await self._write(done_frame(), terminal=True)
        await self._close_send()
        self.finished.set()

    async def fail(self, message: str) -> None:
        if not self._can_write():
            return
        self.terminal = True
        await self._write(error_frame(message), terminal=True)
        await self._close_send()
        self.finished.set()

    def cancel(self) -> None:
        if self.terminal:
            return
        self.cancelled = True
        self.finished.set()

    async def _write(self, frame: dict, *, terminal: bool = False) -> None:
        if self.cancelled:
            return
        if self.terminal and not terminal:
            return
        try:
            await self._send.write_all(encode_frame(frame))
        except Exception as exc:
            logger.debug("iroh stream write failed: %s", type(exc).__name__)
            self.cancelled = True
            self.finished.set()

    async def _close_send(self) -> None:
        try:
            await self._send.finish()
        except Exception as exc:
            logger.debug("iroh stream close failed: %s", type(exc).__name__)

    async def wait_terminal(self, timeout: Optional[float] = None) -> bool:
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.terminal


class ChannelServer:
    """Iroh endpoint, accept loop, and per-stream session registry."""

    def __init__(
        self,
        *,
        key_store: Optional[NodeKeyStore] = None,
        allowlist: Optional[Allowlist] = None,
        relay: Optional[RelayConfig] = None,
        prompt_handler: Optional[
            Callable[[StreamSession, str], Awaitable[None]]
        ] = None,
        bind_addr: Optional[str] = None,
        preset: Any = None,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        prompt_timeout: float = PROMPT_TIMEOUT,
        max_prompt_bytes: int = MAX_PROMPT_BYTES,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_streams_per_connection: int = DEFAULT_MAX_STREAMS_PER_CONNECTION,
        max_total_streams: int = DEFAULT_MAX_TOTAL_STREAMS,
    ) -> None:
        self.key_store = key_store if key_store is not None else NodeKeyStore()
        self.allowlist = allowlist if allowlist is not None else Allowlist()
        self.relay = relay
        self.prompt_handler = prompt_handler
        self.bind_addr = bind_addr
        self.preset = preset
        self.turn_timeout = turn_timeout
        self.prompt_timeout = prompt_timeout
        self.max_prompt_bytes = max_prompt_bytes
        self.max_connections = max_connections
        self.max_streams_per_connection = max_streams_per_connection
        self.max_total_streams = max_total_streams

        self.node_id: Optional[str] = None
        self._ticket: Optional[str] = None
        self._endpoint: Any = None
        self._accept_task: Optional[asyncio.Task] = None
        self._stop_lock: Optional[asyncio.Lock] = None
        self._connections: set[Any] = set()
        self._connection_tasks: set[asyncio.Task] = set()
        self._stream_tasks: set[asyncio.Task] = set()
        self._sessions: dict[str, StreamSession] = {}
        self._active_connections = 0
        self._active_streams = 0
        self.rejections: deque[str] = deque(maxlen=MAX_RECORDED_REJECTIONS)
        self._rejection_event = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._endpoint is not None

    @property
    def ticket(self) -> Optional[str]:
        return self._ticket if self._endpoint is not None else None

    def set_prompt_handler(
        self, handler: Callable[[StreamSession, str], Awaitable[None]]
    ) -> None:
        self.prompt_handler = handler

    def session_for(self, chat_id: str) -> Optional[StreamSession]:
        return self._sessions.get(chat_id)

    async def wait_for_rejection(self, timeout: float = 5.0) -> Optional[str]:
        try:
            await asyncio.wait_for(self._rejection_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        self._rejection_event.clear()
        return self.rejections[-1] if self.rejections else None

    def _record_rejection(self, remote_id: Optional[str]) -> None:
        if remote_id:
            self.rejections.append(remote_id)
        self._rejection_event.set()

    async def start(self) -> None:
        import iroh

        if self._endpoint is not None:
            return
        iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
        secret = self.key_store.load_or_create()
        relay_config = self.relay
        if relay_config is None:
            relay_config = parse_relay_setting(
                os.environ.get("HERMES_IROH_RELAY")
            )
        relay_mode = build_relay_mode(relay_config)
        self.relay = relay_config
        options: dict[str, Any] = {
            "preset": self.preset if self.preset is not None else iroh.preset_n0(),
            "secret_key": secret.to_bytes(),
            "alpns": [ALPN],
            "relay_mode": relay_mode,
        }
        if self.bind_addr:
            options["bind_addr"] = self.bind_addr
        try:
            endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(**options))
        except Exception as exc:
            raise TransportError(
                f"could not bind the Iroh endpoint ({type(exc).__name__})"
            ) from exc
        self._endpoint = endpoint
        self.node_id = str(endpoint.id())
        self._ticket = str(iroh.EndpointTicket.from_addr(endpoint.addr()))
        self._accept_task = asyncio.create_task(self._accept_loop())
        logger.info(
            "iroh channel listening: node_id=%s relay=%s",
            self.node_id,
            relay_config.describe(),
        )

    async def stop(self) -> None:
        if self._stop_lock is None:
            self._stop_lock = asyncio.Lock()
        async with self._stop_lock:
            task, self._accept_task = self._accept_task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for session in list(self._sessions.values()):
                session.cancel()
            self._sessions.clear()
            for connection in list(self._connections):
                try:
                    connection.close(0, b"server shutting down")
                except Exception:
                    pass
            self._connections.clear()
            endpoint, self._endpoint = self._endpoint, None
            if endpoint is not None:
                try:
                    await endpoint.close()
                except Exception as exc:
                    logger.debug(
                        "iroh endpoint close failed: %s", type(exc).__name__
                    )
            self._ticket = None
            pending = [t for t in self._connection_tasks if not t.done()]
            pending.extend(t for t in self._stream_tasks if not t.done())
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._connection_tasks.clear()
            self._stream_tasks.clear()
            self._active_connections = 0
            self._active_streams = 0

    async def _accept_loop(self) -> None:
        while self._endpoint is not None:
            try:
                incoming = await self._endpoint.accept_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("iroh accept_next failed: %s", type(exc).__name__)
                return
            if incoming is None:
                await asyncio.sleep(0.05)
                continue
            task = asyncio.create_task(self._handle_incoming(incoming))
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)

    async def _handle_incoming(self, incoming: Any) -> None:
        try:
            accepting = await incoming.accept()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh incoming accept failed: %s", type(exc).__name__)
            return
        try:
            connection = await accepting.connect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh connect failed: %s", type(exc).__name__)
            return
        alpn = connection.alpn()
        if alpn != ALPN:
            self._close_connection(connection, 1, b"invalid alpn")
            return
        remote_id = str(connection.remote_id())
        if not self.allowlist.check(remote_id):
            self._record_rejection(remote_id)
            logger.warning("iroh connection rejected: node is not allowlisted")
            self._close_connection(connection, 2, b"unauthorized")
            return
        if self._active_connections >= self.max_connections:
            logger.warning("iroh connection rejected: connection limit reached")
            self._close_connection(connection, 3, b"connection limit")
            return
        self._active_connections += 1
        self._connections.add(connection)
        try:
            await self._serve_connection(connection, remote_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "iroh connection ended with error: %s", type(exc).__name__
            )
        finally:
            self._active_connections -= 1
            self._connections.discard(connection)

    @staticmethod
    def _close_connection(connection: Any, code: int, reason: bytes) -> None:
        try:
            connection.close(code, reason)
        except Exception:
            pass

    async def _serve_connection(self, connection: Any, remote_id: str) -> None:
        stream_semaphore = asyncio.Semaphore(self.max_streams_per_connection)
        while True:
            try:
                bi_stream = await connection.accept_bi()
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            if self._endpoint is None:
                return
            if (
                self._active_streams >= self.max_total_streams
                or stream_semaphore.locked()
            ):
                await self._reject_stream(bi_stream)
                continue
            await stream_semaphore.acquire()
            self._active_streams += 1
            task = asyncio.create_task(
                self._serve_stream_task(
                    bi_stream, remote_id, stream_semaphore
                )
            )
            self._stream_tasks.add(task)
            task.add_done_callback(self._stream_tasks.discard)

    async def _reject_stream(self, bi_stream: Any) -> None:
        send = bi_stream.send()
        try:
            await send.write_all(encode_frame(error_frame(ERR_TOO_MANY_STREAMS)))
            await send.finish()
        except Exception:
            pass

    async def _serve_stream_task(
        self,
        bi_stream: Any,
        remote_id: str,
        semaphore: asyncio.Semaphore,
    ) -> None:
        try:
            await self._serve_stream(bi_stream, remote_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh stream failed: %s", type(exc).__name__)
        finally:
            self._active_streams -= 1
            semaphore.release()

    async def _serve_stream(self, bi_stream: Any, remote_id: str) -> None:
        recv = bi_stream.recv()
        send = bi_stream.send()
        try:
            raw = await read_prompt_line(
                recv,
                limit=self.max_prompt_bytes,
                timeout=self.prompt_timeout,
            )
        except PromptError as exc:
            await self._send_error(send, exc.code)
            return
        try:
            prompt = parse_prompt(raw)
        except PromptError as exc:
            await self._send_error(send, exc.code)
            return
        chat_id = make_chat_id(remote_id, prompt.session_id)
        existing = self._sessions.get(chat_id)
        if existing is not None and not existing.terminal:
            await self._send_error(send, ERR_SESSION_BUSY)
            return
        session = StreamSession(
            send,
            node_id=remote_id,
            session_id=prompt.session_id,
            chat_id=chat_id,
        )
        self._sessions[chat_id] = session
        try:
            await session.start()
            handler = self.prompt_handler
            if handler is None:
                await session.fail(ERR_INTERNAL)
                return
            handler_task = asyncio.create_task(
                self._run_handler(handler, session, prompt.text)
            )
            try:
                await asyncio.wait_for(
                    session.finished.wait(), timeout=self.turn_timeout
                )
            except asyncio.TimeoutError:
                await session.fail(ERR_TIMEOUT)
            finally:
                if not handler_task.done():
                    handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
        finally:
            if self._sessions.get(chat_id) is session:
                self._sessions.pop(chat_id, None)
            if not session.terminal:
                session.cancel()

    async def _run_handler(
        self,
        handler: Callable[[StreamSession, str], Awaitable[None]],
        session: StreamSession,
        text: str,
    ) -> None:
        try:
            await handler(session, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("iroh prompt handler failed")
            await session.fail(ERR_INTERNAL)
        finally:
            if not session.terminal:
                session.cancel()

    @staticmethod
    async def _send_error(send: Any, code: str) -> None:
        try:
            await send.write_all(encode_frame(error_frame(code)))
            await send.finish()
        except Exception:
            pass
