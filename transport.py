"""Iroh transport for the Hermes Agent Iroh channel plugin.

The module is independent of Hermes: it owns the node identity, relay
configuration, client allowlist, frame codec, and the QUIC endpoint. The
Hermes gateway adapter lives in ``gateway_adapter.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

ALPN = b"iroh-channel/1"
DEFAULT_SESSION_ID = "default"

MAX_PROMPT_BYTES = 64 * 1024
MAX_DELTA_BYTES = 16 * 1024
MAX_TOOL_FRAME_BYTES = 16 * 1024
MAX_METADATA_BYTES = 4096
PROMPT_READ_CHUNK = 4096

PROMPT_TIMEOUT = 30.0
DEFAULT_TURN_TIMEOUT = 600.0
MIN_TURN_TIMEOUT = 1.0
MAX_TURN_TIMEOUT = 86400.0
DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_STREAMS_PER_CONNECTION = 8
DEFAULT_MAX_TOTAL_STREAMS = 64
DEFAULT_MAX_PENDING_HANDSHAKES = 32
DEFAULT_MAX_STREAMS_PER_NODE = 2
DEFAULT_MAX_PROMPTS_PER_MINUTE = 30
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RECORDED_REJECTIONS = 32
PROMPT_RATE_WINDOW = 60.0
REJECTION_LOG_INTERVAL = 5.0
ACCEPT_BACKOFF_MIN = 0.05
ACCEPT_BACKOFF_MAX = 5.0

ERR_INVALID_PROMPT = "invalid_prompt"
ERR_PROMPT_TOO_LARGE = "prompt_too_large"
ERR_SESSION_BUSY = "session_busy"
ERR_TIMEOUT = "timeout"
ERR_UNAUTHORIZED = "unauthorized"
ERR_INTERNAL = "internal_error"
ERR_TOO_MANY_STREAMS = "too_many_streams"
ERR_RATE_LIMITED = "rate_limited"
ERR_RESPONSE_TOO_LARGE = "response_too_large"

ERROR_VALUES = frozenset(
    {
        ERR_INVALID_PROMPT,
        ERR_PROMPT_TOO_LARGE,
        ERR_SESSION_BUSY,
        ERR_TIMEOUT,
        ERR_UNAUTHORIZED,
        ERR_INTERNAL,
        ERR_TOO_MANY_STREAMS,
        ERR_RATE_LIMITED,
        ERR_RESPONSE_TOO_LARGE,
    }
)

SECRET_KEY_BYTES = 32
ALLOWLIST_MAX_BYTES = 1024 * 1024
MAX_KEY_READ_BYTES = SECRET_KEY_BYTES + 1

_NODE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_.-]{1,128}$")
_DISABLED_RELAY_VALUES = frozenset({"disabled", "off", "none"})


class TransportError(Exception):
    """Base class for transport configuration and runtime failures."""


class KeyStoreError(TransportError):
    """The node key file exists but cannot be used."""


class RelayConfigError(TransportError):
    """The relay override cannot be parsed."""


class FileSafetyError(TransportError):
    """A state file failed the ownership, mode, or size checks."""


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
    if not math.isfinite(value):
        raise TransportError(
            f"HERMES_IROH_TURN_TIMEOUT must be finite: {raw!r}"
        )
    if value < MIN_TURN_TIMEOUT or value > MAX_TURN_TIMEOUT:
        raise TransportError(
            "HERMES_IROH_TURN_TIMEOUT must be between "
            f"{MIN_TURN_TIMEOUT:g} and {MAX_TURN_TIMEOUT:g} seconds: {raw!r}"
        )
    return value


@dataclass(frozen=True)
class ServerLimits:
    """Admission and quota limits applied by ``ChannelServer``."""

    max_pending_handshakes: int = DEFAULT_MAX_PENDING_HANDSHAKES
    max_streams_per_node: int = DEFAULT_MAX_STREAMS_PER_NODE
    max_prompts_per_minute: int = DEFAULT_MAX_PROMPTS_PER_MINUTE
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise TransportError(f"{name} is not an integer: {raw!r}") from exc
    if value <= 0:
        raise TransportError(f"{name} must be positive: {raw!r}")
    return value


def server_limits_from_env() -> ServerLimits:
    return ServerLimits(
        max_pending_handshakes=_positive_int_from_env(
            "HERMES_IROH_MAX_PENDING_HANDSHAKES",
            DEFAULT_MAX_PENDING_HANDSHAKES,
        ),
        max_streams_per_node=_positive_int_from_env(
            "HERMES_IROH_MAX_STREAMS_PER_NODE",
            DEFAULT_MAX_STREAMS_PER_NODE,
        ),
        max_prompts_per_minute=_positive_int_from_env(
            "HERMES_IROH_MAX_PROMPTS_PER_MINUTE",
            DEFAULT_MAX_PROMPTS_PER_MINUTE,
        ),
        max_response_bytes=_positive_int_from_env(
            "HERMES_IROH_MAX_RESPONSE_BYTES",
            DEFAULT_MAX_RESPONSE_BYTES,
        ),
    )


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


def _file_safety_problem(
    info: os.stat_result, *, require_owner_only: bool
) -> Optional[str]:
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if not hasattr(os, "getuid"):
        return None
    if info.st_uid != os.getuid():
        return "is not owned by the current user"
    if require_owner_only:
        if info.st_mode & 0o077:
            return "must not grant group or other permissions (require 0600)"
    elif info.st_mode & 0o022:
        return "must not be group- or world-writable"
    return None


def _read_safe_bytes(
    path: Path, *, max_bytes: int, require_owner_only: bool
) -> bytes:
    if path.is_symlink():
        raise FileSafetyError("is a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise FileSafetyError(
            f"{type(exc).__name__} while opening"
        ) from exc
    try:
        info = os.fstat(fd)
        problem = _file_safety_problem(
            info, require_owner_only=require_owner_only
        )
        if problem is not None:
            raise FileSafetyError(problem)
        if info.st_size > max_bytes:
            raise FileSafetyError(f"exceeds the {max_bytes}-byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > max_bytes:
                raise FileSafetyError(f"exceeds the {max_bytes}-byte limit")
            chunks.append(chunk)
    finally:
        os.close(fd)


def _require_safe_state_directory(path: Path) -> None:
    resolved = path.resolve()
    try:
        info = resolved.stat()
    except OSError as exc:
        raise PermissionError(
            f"{path}: state directory cannot be checked "
            f"({type(exc).__name__}); set HERMES_IROH_STATE_DIR to a private "
            "directory"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(
            f"{path}: state directory is not a directory; set "
            "HERMES_IROH_STATE_DIR to a private directory"
        )
    if not hasattr(os, "getuid"):
        return
    if info.st_uid != os.getuid():
        raise PermissionError(
            f"{path}: state directory is not owned by the current user; "
            f"set HERMES_IROH_STATE_DIR to a private directory"
        )
    if info.st_mode & 0o022:
        raise PermissionError(
            f"{path}: state directory must not be group- or world-writable; "
            f"run chmod 700 {path} or set HERMES_IROH_STATE_DIR to a private "
            "directory"
        )


def _write_file_atomic(path: Path, payload: bytes, *, prefix: str) -> None:
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            logger.debug("could not set 0700 on %s", parent)
    _require_safe_state_directory(parent)
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=prefix, suffix=".tmp")
    try:
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "wb")
            fd = -1
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _sync_directory(parent)


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
        if self.path.exists() or self.path.is_symlink():
            try:
                raw = _read_safe_bytes(
                    self.path,
                    max_bytes=MAX_KEY_READ_BYTES,
                    require_owner_only=True,
                )
            except FileSafetyError as exc:
                raise KeyStoreError(
                    f"{self.path}: cannot read node key ({exc})"
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
            return ",".join(_redacted_relay_url(url) for url in self.urls)
        return "default"


def _redacted_relay_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _validate_relay_url(url: str, position: int) -> None:
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in url
    ):
        raise RelayConfigError(
            f"HERMES_IROH_RELAY entry {position} contains whitespace or "
            "control characters"
        )
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise RelayConfigError(
            f"HERMES_IROH_RELAY entry {position} is not a valid URL "
            f"({type(exc).__name__})"
        ) from exc
    if parsed.scheme not in ("http", "https"):
        raise RelayConfigError(
            f"HERMES_IROH_RELAY entry {position} must use the http or https "
            "scheme"
        )
    if parsed.username is not None or parsed.password is not None:
        raise RelayConfigError(
            f"HERMES_IROH_RELAY entry {position} must not include userinfo"
        )
    if not parsed.hostname:
        raise RelayConfigError(
            f"HERMES_IROH_RELAY entry {position} has no hostname"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise RelayConfigError(
            f"HERMES_IROH_RELAY entry {position} has an invalid port"
        ) from exc


def parse_relay_setting(value: Optional[str]) -> RelayConfig:
    raw = (value or "").strip()
    if not raw:
        return RelayConfig(None, "default")
    if raw.lower() in _DISABLED_RELAY_VALUES:
        return RelayConfig(raw, "disabled")
    urls = tuple(part.strip() for part in raw.split(","))
    for position, url in enumerate(urls, start=1):
        if not url:
            raise RelayConfigError(
                f"HERMES_IROH_RELAY entry {position} is empty"
            )
        _validate_relay_url(url, position)
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
                "HERMES_IROH_RELAY could not be applied "
                f"({type(exc).__name__})"
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
        self._file_unsafe: Optional[str] = None
        self._loaded = False

    def _read_env(self) -> str:
        return os.environ.get(self.ENV_VAR, "")

    def _file_stamp_now(self) -> Optional[tuple[int, int]]:
        try:
            info = os.lstat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    @staticmethod
    def _valid_entry(entry: Any) -> Optional[str]:
        if not isinstance(entry, str):
            return None
        value = normalize_node_id(entry)
        return value if _NODE_ID_RE.match(value) else None

    def _audit_reload(
        self, previous: frozenset[str], current: frozenset[str]
    ) -> None:
        added = sorted(current - previous)
        removed = sorted(previous - current)
        if not added and not removed:
            return
        logger.info(
            "iroh audit: allowlist reload added=%s removed=%s entries=%d",
            ",".join(added) or "-",
            ",".join(removed) or "-",
            len(current),
        )

    def _reload_if_changed(self) -> None:
        stamp = self._file_stamp_now()
        if self._loaded and stamp == self._file_stamp:
            return
        previous = self._file_entries
        was_loaded = self._loaded
        self._loaded = True
        self._file_stamp = stamp
        self._file_unsafe = None
        if stamp is None:
            self._file_entries = frozenset()
            if was_loaded:
                self._audit_reload(previous, self._file_entries)
            return
        try:
            raw = _read_safe_bytes(
                self.path,
                max_bytes=ALLOWLIST_MAX_BYTES,
                require_owner_only=False,
            )
        except FileSafetyError as exc:
            self._file_unsafe = str(exc)
            logger.warning(
                "iroh allowlist file %s is unusable (%s); no file entries active",
                self.path,
                exc,
            )
            self._file_entries = frozenset()
            if was_loaded:
                self._audit_reload(previous, self._file_entries)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            logger.warning(
                "iroh allowlist file %s is unusable (%s); no file entries active",
                self.path,
                type(exc).__name__,
            )
            self._file_entries = frozenset()
            if was_loaded:
                self._audit_reload(previous, self._file_entries)
            return
        entries = payload.get("allowed_node_ids") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            logger.warning(
                "iroh allowlist file %s is unusable (invalid schema); "
                "no file entries active",
                self.path,
            )
            self._file_entries = frozenset()
            if was_loaded:
                self._audit_reload(previous, self._file_entries)
            return
        normalized: set[str] = set()
        invalid = 0
        for entry in entries:
            value = self._valid_entry(entry)
            if value is None:
                invalid += 1
            else:
                normalized.add(value)
        if invalid:
            logger.warning(
                "iroh allowlist file %s has %d invalid entries; "
                "they authorize no client",
                self.path,
                invalid,
            )
        self._file_entries = frozenset(normalized)
        if was_loaded:
            self._audit_reload(previous, self._file_entries)

    def _env_entries(self) -> frozenset[str]:
        raw = self._env_reader() or ""
        parts = re.split(r"[,\s]+", raw.strip())
        return frozenset(
            value
            for value in (self._valid_entry(part) for part in parts if part.strip())
            if value is not None
        )

    def entries(self) -> frozenset[str]:
        self._reload_if_changed()
        return self._file_entries | self._env_entries()

    def check(self, node_id: str) -> bool:
        return normalize_node_id(node_id) in self.entries()

    def _refuse_unsafe_write(self) -> None:
        if self._file_unsafe is None:
            return
        raise PermissionError(
            f"allowlist file {self.path} {self._file_unsafe}; fix the file "
            "permissions or set HERMES_IROH_STATE_DIR to a private directory"
        )

    async def add(self, node_id: str) -> str:
        normalized = self._validate(node_id)
        async with self._lock:
            self._reload_if_changed()
            self._refuse_unsafe_write()
            current = set(self._file_entries)
            current.add(normalized)
            self._write_atomic(sorted(current))
            self._file_entries = frozenset(current)
            self._file_stamp = self._file_stamp_now()
            self._file_unsafe = None
            self._loaded = True
            logger.info(
                "iroh audit: allowlist added node_id=%s entries=%d",
                normalized,
                len(current),
            )
        return normalized

    async def remove(self, node_id: str) -> str:
        normalized = self._validate(node_id)
        async with self._lock:
            self._reload_if_changed()
            self._refuse_unsafe_write()
            current = set(self._file_entries)
            if normalized not in current:
                return normalized
            current.discard(normalized)
            self._write_atomic(sorted(current))
            self._file_entries = frozenset(current)
            self._file_stamp = self._file_stamp_now()
            self._file_unsafe = None
            self._loaded = True
            logger.info(
                "iroh audit: allowlist removed node_id=%s entries=%d",
                normalized,
                len(current),
            )
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


def tool_frame(
    tool_id: str,
    name: str,
    arguments: dict,
    *,
    truncated: bool = False,
) -> Optional[dict]:
    """Build one bounded ``tool`` frame, or None when it cannot be sent safely.

    Arguments are display data: an oversized, non-object, or unencodable payload
    is dropped rather than split, because a client must never see a partial JSON
    object. Values are not logged.
    """
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(arguments, dict):
        return None
    frame: dict = {"type": "tool"}
    if isinstance(tool_id, str) and tool_id:
        frame["id"] = tool_id
    frame["name"] = name
    frame["arguments"] = arguments
    if truncated:
        frame["truncated"] = True
    try:
        encoded = encode_frame(frame)
    except (TypeError, ValueError):
        return None
    if len(encoded) > MAX_TOOL_FRAME_BYTES:
        return None
    return frame


def done_frame(metadata: Optional[dict] = None) -> dict:
    if not metadata:
        return {"type": "done"}
    try:
        encoded = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        logger.debug("iroh done metadata is not JSON-encodable; omitting it")
        return {"type": "done"}
    if len(encoded) > MAX_METADATA_BYTES:
        logger.debug(
            "iroh done metadata is %d bytes, over the %d-byte limit; "
            "omitting it",
            len(encoded),
            MAX_METADATA_BYTES,
        )
        return {"type": "done"}
    return {"type": "done", "metadata": metadata}


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
    if not session_id.strip():
        session_id = DEFAULT_SESSION_ID
    elif not _SESSION_ID_RE.fullmatch(session_id):
        raise PromptError(
            ERR_INVALID_PROMPT,
            "session_id must match ^(?!.*\\.\\.)[A-Za-z0-9_.-]{1,128}$",
        )
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
    size = len(encoded)
    start = 0
    while start < size:
        end = min(start + limit, size)
        if end < size:
            while end > start and (encoded[end] & 0xC0) == 0x80:
                end -= 1
            if end == start:
                end = start + limit
                while end < size and (encoded[end] & 0xC0) == 0x80:
                    end += 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
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
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._send = send
        self.node_id = node_id
        self.session_id = session_id
        self.chat_id = chat_id
        self.max_delta_bytes = max_delta_bytes
        self.max_response_bytes = max_response_bytes
        self.finished = asyncio.Event()
        self.started = False
        self.terminal = False
        self.slash_prompt = False
        self.cancelled = False
        self.deltas_sent = 0
        self.tool_frames_sent = 0
        self.bytes_sent = 0
        self.tool_bytes_sent = 0
        self.started_monotonic = time.monotonic()

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
            size = len(chunk.encode("utf-8"))
            if self.bytes_sent + size > self.max_response_bytes:
                logger.info(
                    "iroh audit: prompt rejected reason=%s node_id=%s "
                    "session_id=%s bytes=%d limit=%d",
                    ERR_RESPONSE_TOO_LARGE,
                    self.node_id,
                    self.session_id,
                    self.bytes_sent + size,
                    self.max_response_bytes,
                )
                await self.fail(ERR_RESPONSE_TOO_LARGE)
                return
            await self._write(delta_frame(chunk))
            self.deltas_sent += 1
            self.bytes_sent += size

    async def emit_tool(
        self,
        tool_id: str,
        name: str,
        arguments: dict,
        *,
        truncated: bool = False,
    ) -> bool:
        """Write one optional ``tool`` frame before the terminal frame.

        Returns False when the session is closed or the payload cannot be encoded
        within the 16 KiB frame limit. A skipped frame is presentation data only:
        the text stream keeps going, and frames do not consume its byte budget.
        """
        if not self._can_write():
            return False
        frame = tool_frame(tool_id, name, arguments, truncated=truncated)
        if frame is None:
            return False
        size = len(encode_frame(frame))
        await self._write(frame)
        if self.cancelled:
            return False
        self.tool_frames_sent += 1
        self.tool_bytes_sent += size
        return True

    async def finish(self, metadata: Optional[dict] = None) -> None:
        if not self._can_write():
            return
        self.terminal = True
        await self._write(done_frame(metadata), terminal=True)
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
        on_session_end: Optional[Callable[[StreamSession], None]] = None,
        bind_addr: Optional[str] = None,
        preset: Any = None,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        prompt_timeout: float = PROMPT_TIMEOUT,
        max_prompt_bytes: int = MAX_PROMPT_BYTES,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_streams_per_connection: int = DEFAULT_MAX_STREAMS_PER_CONNECTION,
        max_total_streams: int = DEFAULT_MAX_TOTAL_STREAMS,
        max_pending_handshakes: int = DEFAULT_MAX_PENDING_HANDSHAKES,
        max_streams_per_node: int = DEFAULT_MAX_STREAMS_PER_NODE,
        max_prompts_per_minute: int = DEFAULT_MAX_PROMPTS_PER_MINUTE,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        for name, value in (
            ("max_pending_handshakes", max_pending_handshakes),
            ("max_streams_per_node", max_streams_per_node),
            ("max_prompts_per_minute", max_prompts_per_minute),
            ("max_response_bytes", max_response_bytes),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.key_store = key_store if key_store is not None else NodeKeyStore()
        self.allowlist = allowlist if allowlist is not None else Allowlist()
        self.relay = relay
        self.prompt_handler = prompt_handler
        self.on_session_end = on_session_end
        self.bind_addr = bind_addr
        self.preset = preset
        self.turn_timeout = turn_timeout
        self.prompt_timeout = prompt_timeout
        self.max_prompt_bytes = max_prompt_bytes
        self.max_connections = max_connections
        self.max_streams_per_connection = max_streams_per_connection
        self.max_total_streams = max_total_streams
        self.max_pending_handshakes = max_pending_handshakes
        self.max_streams_per_node = max_streams_per_node
        self.max_prompts_per_minute = max_prompts_per_minute
        self.max_response_bytes = max_response_bytes

        self.node_id: Optional[str] = None
        self._ticket: Optional[str] = None
        self._endpoint: Any = None
        self._accept_task: Optional[asyncio.Task] = None
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        self._connections: set[Any] = set()
        self._connection_tasks: set[asyncio.Task] = set()
        self._stream_tasks: set[asyncio.Task] = set()
        self._sessions: dict[str, StreamSession] = {}
        self._active_connections = 0
        self._active_streams = 0
        self._node_streams: dict[str, int] = {}
        self._node_prompt_times: dict[str, deque[float]] = {}
        self._handshake_semaphore: Optional[asyncio.Semaphore] = None
        self.rejection_count = 0
        self._last_rejection_log = 0.0
        self._suppressed_rejections = 0
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

    def _log_rejection(self, reason: str, *, node_id: Optional[str] = None) -> None:
        self.rejection_count += 1
        now = time.monotonic()
        if (
            self._last_rejection_log
            and now - self._last_rejection_log < REJECTION_LOG_INTERVAL
        ):
            self._suppressed_rejections += 1
            return
        suppressed, self._suppressed_rejections = (
            self._suppressed_rejections,
            0,
        )
        self._last_rejection_log = now
        logger.info(
            "iroh audit: rejected reason=%s node_id=%s suppressed=%d",
            reason,
            node_id or "-",
            suppressed,
        )

    @staticmethod
    async def _refuse_incoming(incoming: Any) -> None:
        for name in ("close", "refuse", "abort"):
            method = getattr(incoming, name, None)
            if method is None:
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    await result
                return
            except Exception as exc:
                logger.debug(
                    "iroh incoming %s failed: %s", name, type(exc).__name__
                )

    @staticmethod
    async def _close_endpoint_quietly(endpoint: Any) -> None:
        try:
            await endpoint.close()
        except Exception as exc:
            logger.debug("iroh endpoint close failed: %s", type(exc).__name__)

    async def start(self) -> None:
        import iroh

        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        async with self._lifecycle_lock:
            if self._endpoint is not None:
                return
            self._handshake_semaphore = asyncio.Semaphore(
                self.max_pending_handshakes
            )
            self._node_streams.clear()
            self._node_prompt_times.clear()
            self.rejection_count = 0
            self._suppressed_rejections = 0
            self._last_rejection_log = 0.0
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
                endpoint = await iroh.Endpoint.bind(
                    iroh.EndpointOptions(**options)
                )
            except Exception as exc:
                raise TransportError(
                    f"could not bind the Iroh endpoint ({type(exc).__name__})"
                ) from exc
            try:
                node_id = str(endpoint.id())
                ticket = str(iroh.EndpointTicket.from_addr(endpoint.addr()))
                accept_task = asyncio.create_task(self._accept_loop())
            except asyncio.CancelledError:
                await self._close_endpoint_quietly(endpoint)
                raise
            except BaseException as exc:
                await self._close_endpoint_quietly(endpoint)
                raise TransportError(
                    f"could not start the Iroh endpoint ({type(exc).__name__})"
                ) from exc
            self._endpoint = endpoint
            self.node_id = node_id
            self._ticket = ticket
            self._accept_task = accept_task
            logger.info(
                "iroh channel listening: node_id=%s relay=%s",
                self.node_id,
                relay_config.describe(),
            )

    async def stop(self) -> None:
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        async with self._lifecycle_lock:
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
                await self._close_endpoint_quietly(endpoint)
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
            self._node_streams.clear()
            self._node_prompt_times.clear()
            self._handshake_semaphore = None

    async def _accept_loop(self) -> None:
        backoff = ACCEPT_BACKOFF_MIN
        while self._endpoint is not None:
            try:
                incoming = await self._endpoint.accept_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._endpoint is None:
                    return
                logger.debug(
                    "iroh accept_next failed: %s", type(exc).__name__
                )
                self._log_rejection("accept_failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, ACCEPT_BACKOFF_MAX)
                continue
            backoff = ACCEPT_BACKOFF_MIN
            if incoming is None:
                await asyncio.sleep(ACCEPT_BACKOFF_MIN)
                continue
            if not await self._acquire_handshake_slot(incoming):
                continue
            task = asyncio.create_task(self._handle_incoming(incoming))
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)

    async def _acquire_handshake_slot(self, incoming: Any) -> bool:
        semaphore = self._handshake_semaphore
        if semaphore is not None and semaphore.locked():
            self._log_rejection("admission_limit")
            await self._refuse_incoming(incoming)
            return False
        if semaphore is not None:
            await semaphore.acquire()
        return True

    async def _handle_incoming(self, incoming: Any) -> None:
        try:
            connection, remote_id = await self._handshake(incoming)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh handshake failed: %s", type(exc).__name__)
            return
        finally:
            semaphore = self._handshake_semaphore
            if semaphore is not None:
                semaphore.release()
        if connection is None:
            return
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

    async def _handshake(self, incoming: Any) -> tuple[Any, Optional[str]]:
        try:
            accepting = await incoming.accept()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh incoming accept failed: %s", type(exc).__name__)
            return None, None
        try:
            connection = await accepting.connect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh connect failed: %s", type(exc).__name__)
            return None, None
        remote_id = str(connection.remote_id())
        alpn = connection.alpn()
        if alpn != ALPN:
            self._log_rejection("invalid_alpn", node_id=remote_id)
            self._close_connection(connection, 1, b"invalid alpn")
            return None, None
        if not self.allowlist.check(remote_id):
            self._record_rejection(remote_id)
            self._log_rejection("unauthorized", node_id=remote_id)
            self._close_connection(connection, 2, b"unauthorized")
            return None, None
        if self._active_connections >= self.max_connections:
            self._log_rejection("connection_limit", node_id=remote_id)
            self._close_connection(connection, 3, b"connection limit")
            return None, None
        self._active_connections += 1
        self._connections.add(connection)
        return connection, remote_id

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
            node_streams = self._node_streams.get(remote_id, 0)
            if (
                self._active_streams >= self.max_total_streams
                or stream_semaphore.locked()
                or node_streams >= self.max_streams_per_node
            ):
                if node_streams >= self.max_streams_per_node:
                    self._log_rejection(
                        ERR_TOO_MANY_STREAMS, node_id=remote_id
                    )
                await self._reject_stream(bi_stream)
                continue
            await stream_semaphore.acquire()
            self._active_streams += 1
            self._node_streams[remote_id] = node_streams + 1
            task = asyncio.create_task(
                self._serve_stream_task(
                    bi_stream, remote_id, stream_semaphore
                )
            )
            self._stream_tasks.add(task)
            task.add_done_callback(self._stream_tasks.discard)

    async def _reject_stream(self, bi_stream: Any) -> None:
        await self._send_error(bi_stream.send(), ERR_TOO_MANY_STREAMS)

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
            remaining = self._node_streams.get(remote_id, 0) - 1
            if remaining > 0:
                self._node_streams[remote_id] = remaining
            else:
                self._node_streams.pop(remote_id, None)
            semaphore.release()

    def _authorize_stream(self, remote_id: str) -> bool:
        if self.allowlist.check(remote_id):
            return True
        self._record_rejection(remote_id)
        self._log_rejection(ERR_UNAUTHORIZED, node_id=remote_id)
        return False

    def _record_prompt_attempt(self, remote_id: str) -> bool:
        now = time.monotonic()
        window = self._node_prompt_times.setdefault(remote_id, deque())
        while window and now - window[0] >= PROMPT_RATE_WINDOW:
            window.popleft()
        window.append(now)
        if len(window) > self.max_prompts_per_minute:
            self._log_rejection(ERR_RATE_LIMITED, node_id=remote_id)
            return False
        return True

    async def _serve_stream(self, bi_stream: Any, remote_id: str) -> None:
        send = bi_stream.send()
        if not self._authorize_stream(remote_id):
            await self._send_error(send, ERR_UNAUTHORIZED)
            return
        recv = bi_stream.recv()
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
        if not self._authorize_stream(remote_id):
            await self._send_error(send, ERR_UNAUTHORIZED)
            return
        if not self._record_prompt_attempt(remote_id):
            await self._send_error(send, ERR_RATE_LIMITED)
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
            max_response_bytes=self.max_response_bytes,
        )
        session.slash_prompt = prompt.text.lstrip().startswith("/")
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
            stopped_task = asyncio.create_task(self._watch_send_stopped(send))
            finished_task = asyncio.create_task(session.finished.wait())
            try:
                await self._wait_for_session_end(
                    session, finished_task, stopped_task
                )
            finally:
                for task in (finished_task, stopped_task):
                    task.cancel()
                if not handler_task.done():
                    handler_task.cancel()
                await asyncio.gather(
                    handler_task, finished_task, stopped_task, return_exceptions=True
                )
        finally:
            if self._sessions.get(chat_id) is session:
                self._sessions.pop(chat_id, None)
            if not session.terminal:
                session.cancel()
            if self.on_session_end is not None:
                try:
                    self.on_session_end(session)
                except Exception as exc:
                    logger.debug(
                        "iroh session-end cleanup failed: %s",
                        type(exc).__name__,
                    )

    @staticmethod
    async def _watch_send_stopped(send: Any) -> None:
        """Resolve as soon as the peer stops reading or drops the stream.

        This also resolves after our own ``finish()``, so callers must check
        ``session.terminal`` to tell a normal ending from a client stop.
        """
        stopped = getattr(send, "stopped", None)
        if stopped is None:
            await asyncio.Event().wait()
            return
        try:
            await stopped()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _wait_for_session_end(
        self,
        session: "StreamSession",
        finished_task: asyncio.Task,
        stopped_task: asyncio.Task,
    ) -> None:
        """End the turn on a terminal frame, a client stop, or the turn timeout.

        A client that closes the receive side sends no protocol frame, so the
        send stream is the only signal it left. Watching it releases the
        session slot immediately instead of holding it until the turn timeout.
        """
        done, _pending = await asyncio.wait(
            {finished_task, stopped_task},
            timeout=self.turn_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if finished_task in done or session.terminal:
            return
        if stopped_task in done:
            logger.debug("iroh client stopped the stream; releasing session")
            return
        await session.fail(ERR_TIMEOUT)

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

    @staticmethod
    async def _send_error(send: Any, code: str) -> None:
        try:
            await send.write_all(encode_frame(error_frame(code)))
            await send.finish()
        except Exception:
            pass
