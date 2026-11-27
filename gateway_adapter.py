"""Hermes gateway integration for the Iroh channel plugin.

All Hermes imports happen inside ``register()``, so this module imports cleanly
in a plain Python environment. The transport contract it consumes lives in
``transport.py``.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from . import transport
except ImportError:
    import transport

    _transport_path = getattr(transport, "__file__", None)
    if _transport_path is None or (
        Path(_transport_path).resolve().parent
        != Path(__file__).resolve().parent
    ):
        raise ImportError(
            "refusing to load transport from outside the plugin directory"
        ) from None

logger = logging.getLogger(__name__)

PLATFORM_NAME = "iroh"
PLATFORM_LABEL = "Iroh"
PLATFORM_HINT = (
    "You are chatting over an Iroh peer-to-peer channel. Replies are streamed "
    "incrementally as plain text. Markdown is rendered literally and attachments "
    "are not supported: never claim to send files or images."
)
SEGMENT_SEPARATOR = "\n\n"

METADATA_SCHEMA = 1
METADATA_STRING_MAX = 256
MICROS_PER_USD = 1_000_000

# Structured tool frames: display data only, bounded before the transport applies
# its own 16 KiB frame guard.
TOOL_ARG_MAX_BYTES = 15 * 1024
TOOL_ARG_STRING_MAX = 2048
TOOL_ARG_KEYS_MAX = 24
TOOL_ARG_ITEMS_MAX = 24
TOOL_ARG_DEPTH_MAX = 4
TOOL_MATCH_PREFIX = 24
TOOL_MATCH_MIN = 8
TOOL_ANNOUNCED_MAX = 256
TOOL_HISTORY_MESSAGES = 12
TOOL_PENDING_MAX = 32
TOOL_PENDING_TTL = 300.0
RESPONSE_TAIL_MAX_CHARS = 4096

_TOOL_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "access_key",
    "client_secret",
    "session_key",
)

# Payload-shaped keys are not needed for a tooltip and may contain whole files
# or binary data.
_TOOL_OMIT_KEYS = frozenset(
    {
        "content",
        "file_content",
        "new_content",
        "old_content",
        "new_str",
        "old_str",
        "new_string",
        "old_string",
        "new_text",
        "old_text",
        "file_text",
        "text_content",
        "patch",
        "diff",
        "data",
        "body",
        "stdin",
        "base64",
        "blob",
        "bytes",
        "image",
        "image_data",
        "audio",
        "video",
        "file",
        "files",
        "attachments",
    }
)

_TOOL_SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization|bearer)"
        r"\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{8,}"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)

_USAGE_FIELDS = (
    ("input_tokens", ("input_tokens", "prompt_tokens")),
    ("output_tokens", ("output_tokens", "completion_tokens")),
    ("total_tokens", ("total_tokens",)),
    ("reasoning_tokens", ("reasoning_tokens",)),
    ("cache_read_tokens", ("cache_read_tokens", "cache_read_input_tokens")),
    (
        "cache_write_tokens",
        ("cache_write_tokens", "cache_creation_input_tokens"),
    ),
)


TOOL_FRAME_DEBUG_ENV = "HERMES_IROH_DEBUG_TOOL_FRAMES"
TOOL_FRAME_DEBUG_VALUES = frozenset({"1", "true", "yes", "on"})
TOOL_FRAME_DEBUG_CANDIDATES_MAX = 8
TOOL_FRAME_DEBUG_TOKEN_MAX = 128
_DIAGNOSTIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def _tool_frame_debug_enabled() -> bool:
    return (
        os.environ.get(TOOL_FRAME_DEBUG_ENV, "").strip().lower()
        in TOOL_FRAME_DEBUG_VALUES
    )


def _diagnostic_token(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    safe = "".join(
        character
        if character.isprintable() and not character.isspace()
        else "_"
        for character in value
    )
    return safe[:TOOL_FRAME_DEBUG_TOKEN_MAX] or "-"


def _diagnostic_stream(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    digest = hashlib.sha256(value.encode("utf-8", errors="replace"))
    return digest.hexdigest()[:12]


def _diagnostic_identifier(
    value: Any, private: tuple[Any, ...] = ()
) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    if any(isinstance(item, str) and item and value == item for item in private):
        return _diagnostic_stream(value)
    if any(pattern.search(value) for pattern in _TOOL_SECRET_VALUE_PATTERNS):
        return _diagnostic_stream(value)
    if len(value) > TOOL_FRAME_DEBUG_TOKEN_MAX:
        return _diagnostic_stream(value)
    if _DIAGNOSTIC_IDENTIFIER_RE.fullmatch(value) is None:
        return _diagnostic_stream(value)
    return value


def _debug_tool_frames(event: str, **fields: Any) -> None:
    if not _tool_frame_debug_enabled():
        return
    parts = [f"event={_diagnostic_token(event)}"]
    for key, value in fields.items():
        if isinstance(value, bool):
            rendered = "1" if value else "0"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif isinstance(value, str):
            rendered = _diagnostic_token(value)
        else:
            continue
        parts.append(f"{key}={rendered}")
    logger.debug("iroh tool-frames debug: %s", " ".join(parts))


class ResponseRouter:
    """Converts cumulative Hermes content into append-only deltas."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}
        self._tails: dict[str, str] = {}
        self._terminal: set[str] = set()

    def reset(self, chat_id: str) -> None:
        self.release(chat_id)

    def release(self, chat_id: str) -> None:
        self._last.pop(chat_id, None)
        self._tails.pop(chat_id, None)
        self._terminal.discard(chat_id)

    def is_terminal(self, chat_id: str) -> bool:
        return chat_id in self._terminal

    def _drain_tail(self, chat_id: str, text: str, *, final: bool) -> str:
        if final or len(text) > RESPONSE_TAIL_MAX_CHARS:
            self._tails.pop(chat_id, None)
            delta = text
            reason = "final" if final else "tail_bound"
        else:
            newline = text.rfind("\n")
            if newline < 0:
                self._tails[chat_id] = text
                delta = ""
                reason = "incomplete_line"
            else:
                self._tails[chat_id] = text[newline + 1 :]
                delta = text[: newline + 1]
                reason = "newline"
        _debug_tool_frames(
            "router_buffer",
            stream=_diagnostic_stream(chat_id),
            status="flushed" if delta else "buffered",
            reason=reason,
            final=final,
            input_length=len(text),
            delta_length=len(delta),
            tail_length=len(self._tails.get(chat_id, "")),
        )
        return delta

    def consume(self, chat_id: str, content: str, *, final: bool = False) -> str:
        if chat_id in self._terminal:
            _debug_tool_frames(
                "router_buffer",
                stream=_diagnostic_stream(chat_id),
                status="skipped",
                reason="terminal",
                final=final,
                input_length=len(content),
                delta_length=0,
                tail_length=0,
            )
            return ""
        previous = self._last.get(chat_id, "")
        tail = self._tails.get(chat_id, "")
        if content == previous:
            combined = tail
        elif content.startswith(previous):
            combined = tail + content[len(previous) :]
        elif previous.startswith(content):
            if not final:
                _debug_tool_frames(
                    "router_buffer",
                    stream=_diagnostic_stream(chat_id),
                    status="buffered",
                    reason="shorter_snapshot",
                    final=final,
                    input_length=len(content),
                    delta_length=0,
                    tail_length=len(tail),
                )
                return ""
            self._tails.pop(chat_id, None)
            _debug_tool_frames(
                "router_buffer",
                stream=_diagnostic_stream(chat_id),
                status="flushed",
                reason="final_shorter_snapshot",
                final=final,
                input_length=len(content),
                delta_length=len(tail),
                tail_length=0,
            )
            return tail
        else:
            prefix = tail + SEGMENT_SEPARATOR if previous else tail
            self._last[chat_id] = content
            return self._drain_tail(chat_id, prefix + content, final=final)
        self._last[chat_id] = content
        return self._drain_tail(chat_id, combined, final=final)

    def mark_terminal(self, chat_id: str) -> bool:
        if chat_id in self._terminal:
            return False
        self._terminal.add(chat_id)
        return True


class ChannelService:
    """Process-wide transport owner shared by the adapter and commands."""

    _instance: Optional["ChannelService"] = None

    def __init__(
        self,
        key_store: Optional[transport.NodeKeyStore] = None,
        allowlist: Optional[transport.Allowlist] = None,
    ) -> None:
        self.key_store = key_store or transport.NodeKeyStore()
        self.allowlist = allowlist or transport.Allowlist()
        self.server: Optional[transport.ChannelServer] = None
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        self.router = ResponseRouter()
        self._announced_tools: dict[str, set[str]] = {}
        self._pending_tools: dict[str, deque] = {}
        self._pending_lock = threading.Lock()

    @classmethod
    def get(cls) -> "ChannelService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def reset_tool_tracking(self, chat_id: str) -> None:
        self._announced_tools.pop(chat_id, None)

    def release_tool_tracking(self, chat_id: str) -> None:
        self._announced_tools.pop(chat_id, None)

    def mark_tool_announced(self, chat_id: str, key: str) -> bool:
        """Return True the first time ``key`` is seen for this stream."""
        if not key:
            return False
        announced = self._announced_tools.setdefault(chat_id, set())
        if key in announced:
            return False
        if len(announced) >= TOOL_ANNOUNCED_MAX:
            announced.clear()
        announced.add(key)
        return True

    def tool_announced(self, chat_id: str, key: str) -> bool:
        return key in self._announced_tools.get(chat_id, ())

    def remember_tool_call(
        self,
        session_id: str,
        tool_call_id: str,
        name: str,
        arguments: Any,
    ) -> None:
        """Remember one pre-execution tool call for a later progress line.

        Values are kept in memory only and never logged. Duplicate ids are
        ignored, and each session keeps a bounded, expiring queue.
        """
        argument_type = (
            "mapping" if isinstance(arguments, Mapping) else type(arguments).__name__
        )
        call_id = tool_call_id if isinstance(tool_call_id, str) else ""
        private = (session_id,)
        diag_call_id = _diagnostic_identifier(call_id, private)
        diag_tool_name = _diagnostic_identifier(name, private)
        if not isinstance(session_id, str) or not session_id:
            _debug_tool_frames(
                "hook_capture",
                session=_diagnostic_stream(session_id),
                call_id=diag_call_id,
                tool_name=diag_tool_name,
                argument_type=argument_type,
                status="rejected",
                reason="invalid_session",
            )
            return
        if not isinstance(name, str) or not name:
            _debug_tool_frames(
                "hook_capture",
                session=_diagnostic_stream(session_id),
                call_id=diag_call_id,
                tool_name=diag_tool_name,
                argument_type=argument_type,
                status="rejected",
                reason="invalid_tool_name",
            )
            return
        if isinstance(arguments, str):
            try:
                arguments = json.loads(
                    arguments, parse_constant=_reject_json_constant
                )
            except (TypeError, ValueError, RecursionError):
                _debug_tool_frames(
                    "hook_capture",
                    session=_diagnostic_stream(session_id),
                    call_id=diag_call_id,
                    tool_name=diag_tool_name,
                    argument_type=argument_type,
                    status="rejected",
                    reason="invalid_arguments",
                )
                return
        if not isinstance(arguments, Mapping):
            _debug_tool_frames(
                "hook_capture",
                session=_diagnostic_stream(session_id),
                call_id=diag_call_id,
                tool_name=diag_tool_name,
                argument_type=argument_type,
                status="rejected",
                reason="invalid_arguments",
            )
            return
        record = {
            "id": call_id,
            "name": name,
            "arguments": dict(arguments),
        }
        key = _tool_call_key(record)
        if not key:
            _debug_tool_frames(
                "hook_capture",
                session=_diagnostic_stream(session_id),
                call_id=diag_call_id,
                tool_name=diag_tool_name,
                argument_type=argument_type,
                status="rejected",
                reason="invalid_call_key",
            )
            return
        with self._pending_lock:
            queue = self._pending_tools.setdefault(
                session_id, deque(maxlen=TOOL_PENDING_MAX)
            )
            if any(item.get("key") == key for item in queue):
                _debug_tool_frames(
                    "hook_capture",
                    session=_diagnostic_stream(session_id),
                    call_id=diag_call_id,
                    tool_name=diag_tool_name,
                    argument_type=argument_type,
                    status="rejected",
                    reason="duplicate",
                )
                return
            record["key"] = key
            record["ts"] = time.monotonic()
            queue.append(record)
        _debug_tool_frames(
            "hook_capture",
            session=_diagnostic_stream(session_id),
            call_id=diag_call_id,
            tool_name=diag_tool_name,
            argument_type=argument_type,
            status="accepted",
            reason="stored",
        )

    def pending_tool_calls(self, session_id: str) -> list[dict]:
        if not isinstance(session_id, str) or not session_id:
            return []
        cutoff = time.monotonic() - TOOL_PENDING_TTL
        with self._pending_lock:
            queue = self._pending_tools.get(session_id)
            if not queue:
                return []
            return [item for item in queue if item.get("ts", 0.0) >= cutoff]

    def forget_tool_call(self, session_id: str, key: str) -> None:
        with self._pending_lock:
            queue = self._pending_tools.get(session_id)
            if not queue:
                return
            remaining = deque(
                (item for item in queue if item.get("key") != key),
                maxlen=TOOL_PENDING_MAX,
            )
            if remaining:
                self._pending_tools[session_id] = remaining
            else:
                self._pending_tools.pop(session_id, None)

    async def start(self) -> transport.ChannelServer:
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        async with self._lifecycle_lock:
            if self.server is not None and self.server.running:
                return self.server
            limits = transport.server_limits_from_env()
            server = transport.ChannelServer(
                key_store=self.key_store,
                allowlist=self.allowlist,
                turn_timeout=transport.turn_timeout_from_env(),
                on_session_end=self._release_session_state,
                max_pending_handshakes=limits.max_pending_handshakes,
                max_streams_per_node=limits.max_streams_per_node,
                max_prompts_per_minute=limits.max_prompts_per_minute,
                max_response_bytes=limits.max_response_bytes,
            )
            await server.start()
            self.server = server
            return server

    async def stop(self) -> None:
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        async with self._lifecycle_lock:
            server, self.server = self.server, None
            if server is not None:
                await server.stop()

    def _release_session_state(self, session: transport.StreamSession) -> None:
        current = (
            self.server.session_for(session.chat_id)
            if self.server is not None
            else None
        )
        if current is None or current is session:
            self.router.release(session.chat_id)
            self.release_tool_tracking(session.chat_id)

    def node_id(self) -> str:
        return self.key_store.node_id()

    def ticket(self) -> Optional[str]:
        if self.server is None:
            return None
        return self.server.ticket


def _endpoint_hint(*, node_id_missing: bool) -> str:
    if not transport.iroh_available():
        return (
            "the iroh Python package is not importable; run "
            "hermes plugins enable iroh-channel to install iroh>=1.2,<2 and "
            "accept the dependency prompt"
        )
    if node_id_missing:
        return (
            "the node id could not be derived; check the gateway log and the "
            f"state directory {transport.default_state_dir()} for key errors"
        )
    return (
        "the endpoint is not connected; set "
        "gateway.platforms.iroh.enabled=true and restart the gateway from a "
        "normal shell outside it, then check /iroh_info again"
    )


def format_info(service: ChannelService) -> str:
    running = service.server is not None and service.server.running
    try:
        node_id: Optional[str] = service.node_id()
    except Exception as exc:
        node_id = None
        logger.warning(
            "iroh node id unavailable: %s (check the gateway log and the "
            "state directory for key errors)",
            type(exc).__name__,
        )
    lines = [f"node id: {node_id if node_id else 'unavailable'}"]
    ticket = service.ticket() if running else None
    lines.append(f"ticket: {ticket if ticket else 'unavailable'}")
    lines.append(f"endpoint: {'running' if running else 'not running'}")
    lines.append(f"alpn: {transport.ALPN.decode()}")
    if service.server is not None and service.server.relay is not None:
        relay_text = service.server.relay.describe()
    else:
        try:
            relay_text = transport.parse_relay_setting(
                os.environ.get("HERMES_IROH_RELAY")
            ).describe()
        except transport.RelayConfigError:
            relay_text = "invalid configuration"
    lines.append(f"relay: {relay_text}")
    lines.append(f"allowlist entries: {len(service.allowlist.entries())}")
    lines.append(f"state dir: {transport.default_state_dir()}")
    if not running:
        hint = _endpoint_hint(node_id_missing=node_id is None)
        lines.append(f"hint: {hint}")
    return "\n".join(lines)


async def handle_info(service: ChannelService, args: str = "") -> str:
    del args
    return format_info(service)


async def handle_allow(service: ChannelService, args: str = "") -> str:
    parts = (args or "").split()
    if len(parts) != 1:
        return "usage: /iroh_allow <client_node_id>"
    try:
        normalized = await service.allowlist.add(parts[0])
    except ValueError as exc:
        return f"invalid node id: {exc}"
    except OSError as exc:
        return f"could not update allowlist: {type(exc).__name__}"
    return f"allowlisted: {normalized}"


async def handle_revoke(service: ChannelService, args: str = "") -> str:
    parts = (args or "").split()
    if len(parts) != 1:
        return "usage: /iroh_revoke <client_node_id>"
    try:
        normalized = await service.allowlist.remove(parts[0])
    except ValueError as exc:
        return f"invalid node id: {exc}"
    except OSError as exc:
        return f"could not update allowlist: {type(exc).__name__}"
    if service.allowlist.check(normalized):
        return (
            f"still authorized: {normalized} "
            f"({transport.Allowlist.ENV_VAR})"
        )
    return f"revoked: {normalized}"


def _read_field(source: Any, *names: str) -> Any:
    if source is None:
        return None
    for name in names:
        try:
            if isinstance(source, Mapping):
                value = source.get(name)
            else:
                value = getattr(source, name, None)
        except Exception:
            value = None
        if value is not None:
            return value
    return None


def _metadata_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > METADATA_STRING_MAX or "://" in text:
        return None
    return text


def _metadata_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _metadata_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _metadata_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _usage_counters(
    source: Any, *, prefix: str = "", require_io: bool = False
) -> dict:
    counters: dict[str, int] = {}
    for name, aliases in _USAGE_FIELDS:
        value = _metadata_int(
            _read_field(source, *(f"{prefix}{alias}" for alias in aliases))
        )
        if value is not None:
            counters[name] = value
    if "total_tokens" not in counters:
        input_tokens = counters.get("input_tokens")
        output_tokens = counters.get("output_tokens")
        if input_tokens is not None and output_tokens is not None:
            counters["total_tokens"] = input_tokens + output_tokens
    if require_io and not {
        "input_tokens",
        "output_tokens",
        "total_tokens",
    } <= counters.keys():
        return {}
    return counters


def _model_identity(settings: Any) -> dict:
    identity: dict[str, Any] = {}
    model = _metadata_string(_read_field(settings, "model", "model_name"))
    raw_provider = _read_field(settings, "provider", "provider_name")
    provider = _metadata_string(raw_provider)
    if provider is None:
        provider = _metadata_string(
            _read_field(raw_provider, "name", "value")
        )
    if model:
        identity["model"] = model
    if provider:
        identity["provider"] = provider
    return identity


def _context_fields(source: Any) -> dict:
    compressor = _read_field(source, "context_compressor")
    tokens = _metadata_int(
        _read_field(
            compressor,
            "last_prompt_tokens",
            "context_tokens",
            "current_tokens",
            "last_real_tokens",
        )
    )
    if tokens is None:
        tokens = _metadata_int(
            _read_field(source, "last_prompt_tokens", "context_tokens")
        )
    length = _metadata_int(
        _read_field(
            compressor, "context_length", "max_tokens", "context_window"
        )
    )
    if length is None:
        length = _metadata_int(
            _read_field(source, "context_length", "context_window")
        )
    if tokens is not None and length:
        tokens = min(tokens, length)
    fields: dict[str, Any] = {}
    if tokens is not None:
        fields["context_tokens"] = tokens
    if length is not None:
        fields["context_length"] = length
    if tokens is not None and length:
        fields["context_percent"] = max(
            0, min(100, round(tokens * 100 / length))
        )
    return fields


def _session_usage(agent: Any) -> dict:
    usage = _usage_counters(agent, prefix="session_")
    api_calls = _metadata_int(
        _read_field(agent, "session_api_calls", "api_calls")
    )
    if api_calls is not None:
        usage["api_calls"] = api_calls
    estimated = _metadata_float(
        _read_field(agent, "session_estimated_cost_usd", "estimated_cost_usd")
    )
    if estimated is not None:
        usage["estimated_cost_usd"] = estimated
    for field, names in (
        ("cost_status", ("session_cost_status", "cost_status")),
        ("cost_source", ("session_cost_source", "cost_source")),
    ):
        value = _metadata_string(_read_field(agent, *names))
        if value is not None:
            usage[field] = value
    usage.update(_context_fields(agent))
    return usage


def _credits_state(agent: Any) -> Any:
    if agent is None:
        return None
    getter = getattr(agent, "get_credits_state", None)
    if callable(getter):
        try:
            state = getter()
        except Exception:
            state = None
        if inspect.isawaitable(state):
            close = getattr(state, "close", None)
            if callable(close):
                close()
            state = None
        if state is not None:
            return state
    try:
        return getattr(agent, "_credits_state", None)
    except Exception:
        return None


def _credits_available(credits: Any) -> bool:
    if credits is None:
        return False
    if isinstance(credits, Mapping):
        return True
    has_data = getattr(credits, "has_data", None)
    if has_data is True:
        return True
    from_header = getattr(credits, "from_header", None)
    if from_header is True:
        return True
    if has_data is not None or from_header is not None:
        return False
    return True


def _add_amount_pair(
    target: dict,
    source: Any,
    usd_name: str,
    micros_name: str,
    *,
    include_micros: bool = True,
) -> None:
    micros = _metadata_int(_read_field(source, micros_name))
    usd = _metadata_float(_read_field(source, usd_name))
    if usd is None and micros is not None:
        usd = round(micros / MICROS_PER_USD, 6)
    if micros is not None and include_micros:
        target[micros_name] = micros
    if usd is not None:
        target[usd_name] = usd


def _binding_flag(gauge: Any) -> bool:
    return (
        _metadata_bool(
            _read_field(gauge, "binding", "is_binding", "primary")
        )
        is True
    )


def _normalize_gauge(gauge: Any) -> Optional[dict]:
    result: dict[str, Any] = {}
    identifier = _metadata_string(_read_field(gauge, "id"))
    label = _metadata_string(_read_field(gauge, "label"))
    used_bp = _metadata_int(_read_field(gauge, "used_bp"))
    resets_at_ms = _metadata_int(_read_field(gauge, "resets_at_ms"))
    if identifier is not None:
        result["id"] = identifier
    if label is not None:
        result["label"] = label
    if used_bp is not None:
        result["used_bp"] = used_bp
    if resets_at_ms is not None:
        result["resets_at_ms"] = resets_at_ms
    return result or None


def _plan_gauge(credits: Any) -> Optional[dict]:
    direct = _read_field(credits, "plan_gauge")
    if direct is not None:
        return _normalize_gauge(direct)
    gauges = _read_field(credits, "gauges")
    if not isinstance(gauges, (list, tuple)):
        gauges = _read_field(credits, "plan_gauges")
    if not isinstance(gauges, (list, tuple)) or not gauges:
        return None
    binding = _read_field(credits, "binding_gauge")
    if binding is None:
        binding = next(
            (gauge for gauge in gauges if _binding_flag(gauge)), None
        )
    full = _read_field(binding, "full") if binding is not None else None
    if full is None and binding is not None:
        used_bp = _metadata_int(_read_field(binding, "used_bp"))
        full = used_bp is not None and used_bp >= 10000
    day = next(
        (
            gauge
            for gauge in gauges
            if _read_field(gauge, "id") == "day"
            and _read_field(gauge, "scope") == "all"
        ),
        None,
    )
    chosen = binding if binding is not None and full else (day or binding)
    if chosen is None:
        chosen = gauges[0]
    return _normalize_gauge(chosen)


def _account_balance(
    credits: Any, fallback_provider: Optional[str]
) -> dict:
    if not _credits_available(credits):
        return {}
    fields: dict[str, Any] = {}
    _add_amount_pair(fields, credits, "remaining_usd", "remaining_micros")
    _add_amount_pair(
        fields, credits, "subscription_usd", "subscription_micros"
    )
    _add_amount_pair(
        fields,
        credits,
        "subscription_limit_usd",
        "subscription_limit_micros",
        include_micros=False,
    )
    _add_amount_pair(fields, credits, "purchased_usd", "purchased_micros")
    paid_access = _metadata_bool(_read_field(credits, "paid_access"))
    if paid_access is not None:
        fields["paid_access"] = paid_access
    as_of_ms = _metadata_int(_read_field(credits, "as_of_ms"))
    if as_of_ms:
        fields["as_of_ms"] = as_of_ms
    gauge = _plan_gauge(credits)
    if gauge is not None:
        fields["plan_gauge"] = gauge
    if not fields:
        return {}
    balance: dict[str, Any] = {}
    provider = _metadata_string(_read_field(credits, "provider"))
    provider = provider or fallback_provider
    if provider:
        balance["provider"] = provider
    balance.update(fields)
    return balance


def _captured_session_usage(stats: Any) -> dict:
    if not isinstance(stats, Mapping):
        return {}
    usage: dict[str, Any] = {}
    for name, aliases in _USAGE_FIELDS:
        value = _metadata_int(_read_field(stats, *aliases))
        if value is not None:
            usage[name] = value
    if "total_tokens" not in usage:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = input_tokens + output_tokens
    api_calls = _metadata_int(_read_field(stats, "api_calls"))
    if api_calls is not None:
        usage["api_calls"] = api_calls
    usage.update(_context_fields(stats))
    return usage


def build_metadata(
    *,
    generated_at: str,
    latency_ms: int,
    model_settings: Any = None,
    turn_usage: Any = None,
    session_stats: Any = None,
    agent: Any = None,
) -> dict:
    metadata: dict[str, Any] = {
        "schema": METADATA_SCHEMA,
        "generated_at": generated_at,
        "latency_ms": max(0, _metadata_int(latency_ms) or 0),
    }
    identity = _model_identity(model_settings)
    metadata.update(identity)
    usage = _usage_counters(turn_usage, require_io=True)
    if usage:
        metadata["usage"] = usage
    session_usage = _session_usage(agent)
    session_usage.update(_captured_session_usage(session_stats))
    if session_usage:
        metadata["session_usage"] = session_usage
    balance = _account_balance(
        _credits_state(agent), identity.get("provider")
    )
    if balance:
        metadata["balance"] = balance
    return metadata


def _utc_timestamp(epoch_seconds: Optional[float] = None) -> str:
    moment = (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        if epoch_seconds is not None
        else datetime.now(timezone.utc)
    )
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _session_latency_ms(session: Any) -> int:
    started = getattr(session, "started_monotonic", None)
    if started is None:
        return 0
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _stream_source(
    adapter: Any, session: transport.StreamSession
) -> Any:
    return adapter.build_source(
        chat_id=session.chat_id,
        chat_name=session.node_id,
        chat_type="dm",
        user_id=session.node_id,
        user_name=session.node_id[:12],
    )


def _session_source(adapter: Any, session: transport.StreamSession) -> Any:
    build_source = getattr(adapter, "build_source", None)
    if not callable(build_source):
        return None
    try:
        return _stream_source(adapter, session)
    except Exception:
        return None


async def _session_model_settings(adapter: Any, source: Any) -> Any:
    accessor = getattr(adapter, "get_session_model_settings", None)
    if not callable(accessor):
        return None
    try:
        settings = accessor(source)
        if inspect.isawaitable(settings):
            settings = await settings
    except Exception:
        return None
    return settings


def _turn_agent(state: Any) -> Any:
    turn = _read_field(state, "turn")
    agent = _read_field(turn, "agent")
    if agent is not None:
        return agent
    for name in ("turn_agent", "agent", "cached_agent", "active_agent"):
        agent = _read_field(state, name)
        if agent is not None:
            return agent
    return None


def _usable_agent(agent: Any) -> bool:
    if agent is None:
        return False
    try:
        from gateway.run import _AGENT_PENDING_SENTINEL
    except Exception:
        return True
    return agent is not _AGENT_PENDING_SENTINEL


def _lookup(lookup: Any, key: Any) -> Any:
    if not callable(lookup):
        return None
    try:
        return lookup(key)
    except Exception:
        return None


def _live_agent(adapter: Any, source: Any) -> Any:
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None or source is None:
        return None
    key_for_source = getattr(runner, "_session_key_for_source", None)
    if not callable(key_for_source):
        return None
    try:
        key = key_for_source(source)
    except Exception:
        return None
    resident = _lookup(getattr(runner, "_resident_agent_for", None), key)
    if _usable_agent(resident):
        return resident
    state = _lookup(getattr(runner, "_peek_session_state", None), key)
    agent = _turn_agent(state)
    if _usable_agent(agent):
        return agent
    cached = _lookup(getattr(runner, "_cached_agent_for", None), key)
    if _usable_agent(cached):
        return cached
    return None


def _execution_holder_value(holder: Any) -> Any:
    if holder is None:
        return None
    if isinstance(holder, contextvars.ContextVar):
        try:
            return holder.get()
        except Exception:
            return None
    getter = getattr(holder, "get", None)
    if callable(getter) and not isinstance(holder, Mapping):
        try:
            return getter()
        except Exception:
            return None
    if callable(holder) and not isinstance(holder, type):
        try:
            return holder()
        except Exception:
            return None
    return holder


def _execution_payload() -> Any:
    try:
        from gateway import session_results
    except Exception:
        return None
    holder = getattr(session_results, "execution_result", None)
    return _execution_holder_value(holder)


def _execution_usage(value: Any) -> Any:
    usage = _read_field(value, "usage")
    if usage is not None:
        return usage
    return value


def _oneline_text(text: str) -> str:
    return " ".join(str(text).split())


def _is_secret_key(name: str) -> bool:
    normalized = str(name).lower().replace("-", "_")
    return any(part in normalized for part in _TOOL_SECRET_KEY_PARTS)


def _clip_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore") + "\u2026"


def _redact_secret_values(text: str) -> str:
    for pattern in _TOOL_SECRET_VALUE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _bound_tool_value(
    value: Any,
    depth: int,
    truncated: list[bool],
    preserved_omit_keys: frozenset[str] = frozenset(),
) -> Any:
    if depth > TOOL_ARG_DEPTH_MAX:
        truncated[0] = True
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        truncated[0] = True
        return "[unsupported]"
    if isinstance(value, str):
        clipped = _clip_utf8(value, TOOL_ARG_STRING_MAX)
        redacted = _redact_secret_values(clipped)
        if redacted != value:
            truncated[0] = True
        return redacted
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= TOOL_ARG_KEYS_MAX:
                truncated[0] = True
                break
            name = str(key)[:128]
            normalized = name.lower().replace("-", "_")
            preserve = depth == 0 and normalized in preserved_omit_keys
            if normalized in _TOOL_OMIT_KEYS and not preserve:
                result[name] = "[omitted]"
                truncated[0] = True
            elif _is_secret_key(name):
                result[name] = "[redacted]"
            else:
                result[name] = _bound_tool_value(
                    item, depth + 1, truncated, preserved_omit_keys
                )
        return result
    if isinstance(value, (list, tuple)):
        result_list = []
        for index, item in enumerate(value):
            if index >= TOOL_ARG_ITEMS_MAX:
                truncated[0] = True
                break
            result_list.append(
                _bound_tool_value(item, depth + 1, truncated, preserved_omit_keys)
            )
        return result_list
    truncated[0] = True
    return "[unsupported]"


def _reject_json_constant(value: str) -> None:
    """Reject non-finite JSON constants that Python accepts by default."""
    raise ValueError(f"non-finite JSON constant: {value}")


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )


def _bound_tool_arguments(
    arguments: Any, tool_name: str = ""
) -> Optional[tuple[dict, bool]]:
    """Return ``(object, truncated)`` or None when no safe object can be built."""
    if not isinstance(arguments, Mapping):
        return None
    preserved = frozenset({"file"}) if tool_name == "read_file" else frozenset()
    truncated = [False]
    bounded = _bound_tool_value(arguments, 0, truncated, preserved)
    if not isinstance(bounded, dict):
        return None
    for key in list(bounded):
        try:
            size = _json_size(bounded[key])
        except (TypeError, ValueError):
            return None
        if size > TOOL_ARG_MAX_BYTES:
            bounded[key] = "[truncated]"
            truncated[0] = True
    while bounded:
        try:
            total = _json_size(bounded)
        except (TypeError, ValueError):
            return None
        if total <= TOOL_ARG_MAX_BYTES:
            break
        bounded.popitem()
        truncated[0] = True
    if not bounded and arguments:
        return None
    return bounded, truncated[0]


def _agent_history(agent: Any) -> list:
    best: list = []
    for name in (
        "_session_messages",
        "messages",
        "_messages",
        "conversation_history",
        "_conversation_history",
        "history",
    ):
        value = _read_field(agent, name)
        if isinstance(value, list) and len(value) > len(best):
            best = value
    return best


def _tool_call_record(call: Any) -> Optional[dict]:
    function = _read_field(call, "function")
    name = _read_field(function, "name")
    if name is None:
        name = _read_field(call, "name", "tool_name")
    raw_arguments = _read_field(function, "arguments")
    if raw_arguments is None:
        raw_arguments = _read_field(call, "arguments", "args")
    if isinstance(raw_arguments, str):
        try:
            raw_arguments = json.loads(
                raw_arguments, parse_constant=_reject_json_constant
            )
        except (TypeError, ValueError, RecursionError):
            return None
    if not isinstance(raw_arguments, Mapping):
        return None
    if not isinstance(name, str) or not name:
        return None
    identifier = _read_field(call, "id", "tool_call_id")
    return {
        "id": identifier if isinstance(identifier, str) else "",
        "name": name,
        "arguments": dict(raw_arguments),
    }


def _recent_tool_calls(messages: list) -> list[dict]:
    records: list[dict] = []
    start = max(0, len(messages) - TOOL_HISTORY_MESSAGES)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, Mapping) and message.get("role") == "user":
            start = index + 1
            break
    for message in messages[start:]:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        calls = _read_field(message, "tool_calls")
        if not isinstance(calls, (list, tuple)):
            continue
        for call in calls:
            record = _tool_call_record(call)
            if record is not None:
                records.append(record)
    return records


def _argument_strings(value: Any, depth: int = 0) -> list[str]:
    if depth > TOOL_ARG_DEPTH_MAX:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_argument_strings(item, depth + 1))
            if len(strings) >= 16:
                break
        return strings[:16]
    if isinstance(value, (list, tuple)):
        strings = []
        for item in value:
            strings.extend(_argument_strings(item, depth + 1))
            if len(strings) >= 16:
                break
        return strings[:16]
    return []


def _line_matches_arguments(line: str, arguments: Mapping) -> bool:
    normalized_line = _oneline_text(line)
    if not normalized_line:
        return False
    for value in _argument_strings(arguments):
        normalized = _oneline_text(value)
        if len(normalized) < TOOL_MATCH_MIN:
            continue
        if normalized[:TOOL_MATCH_PREFIX] in normalized_line:
            return True
    return False


_FILE_TOOL_NAMES = frozenset({"read_file", "write_file"})
_READ_PATH_KEYS = ("path", "file", "filepath", "file_path")
_FILE_MARKER_RE = re.compile(
    r"^\s*(?:[^\w\s]+(?:\s*[^\w\s]+)*\s*)?"
    r"(read_file|write_file|Reading|Writing)(?![A-Za-z0-9_])"
)
_READ_LINE_SUFFIX_RE = re.compile(r"\s+L\d+(?:-\d+)?$")


def _normalized_path(value: str) -> str:
    return value.strip().replace("\\", "/")


def _leading_file_marker(text: str) -> Optional[str]:
    match = _FILE_MARKER_RE.match(text)
    return match.group(1) if match is not None else None


def _raw_file_selector(tail: str) -> Optional[str]:
    tail = tail.strip()
    if tail.startswith("("):
        body = tail[1:].strip()
        close = re.search(r"\)\s*(?:\.\.\.|…)?\s*$", body)
        if close is not None:
            body = body[: close.start()].strip()
        if body.startswith("{"):
            try:
                decoded = json.loads(body, parse_constant=_reject_json_constant)
            except (TypeError, ValueError, RecursionError):
                decoded = None
            if isinstance(decoded, Mapping):
                path = decoded.get("path")
                if isinstance(path, str):
                    return path
        path_match = re.search(
            r"\bpath\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^,}\s]+))",
            body,
        )
        if path_match is not None:
            return next(
                (group for group in path_match.groups() if group is not None),
                None,
            )
    tail = tail.lstrip(":= ")
    if tail[:1] in ("'", '"'):
        end = tail.find(tail[0], 1)
        if end > 0:
            return tail[1:end]
    return tail or None


def _file_selector(line: str, operation: str) -> Optional[str]:
    text = _oneline_text(line)
    marker_match = _FILE_MARKER_RE.match(text)
    if marker_match is None:
        return None
    marker = marker_match.group(1)
    tail = text[marker_match.end() :].strip()
    if marker in _FILE_TOOL_NAMES:
        return _raw_file_selector(tail)
    label = "Reading" if operation == "read_file" else "Writing"
    if marker != label:
        return None
    return tail.lstrip(": ").strip() or None


def _clean_file_selector(selector: str, operation: str) -> tuple[str, bool]:
    visible = selector.strip()
    if operation == "read_file":
        visible = _READ_LINE_SUFFIX_RE.sub("", visible).rstrip()
    truncated = visible.endswith(("...", "…"))
    if truncated:
        marker_length = 1 if visible.endswith("…") else 3
        visible = visible[:-marker_length].rstrip()
    if operation == "read_file":
        visible = _READ_LINE_SUFFIX_RE.sub("", visible).rstrip()
    if len(visible) >= 2 and visible[0] == visible[-1] and visible[0] in (
        "'",
        '"',
    ):
        visible = visible[1:-1]
    return visible, truncated


def _file_selector_matches(
    operation: str, arguments: Mapping, selector: str
) -> bool:
    visible, truncated = _clean_file_selector(selector, operation)
    if len(visible) < TOOL_MATCH_MIN:
        return False
    visible = _normalized_path(visible)
    keys = _READ_PATH_KEYS if operation == "read_file" else ("path",)
    for key in keys:
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        path = _normalized_path(value)
        if not path:
            continue
        if operation == "write_file":
            if path.startswith(visible):
                return True
            continue
        if "/" not in visible:
            basename = path.rsplit("/", 1)[-1]
            matches = (
                basename.startswith(visible)
                if truncated
                else basename == visible
            )
            if matches:
                return True
        else:
            matches = path.startswith(visible) if truncated else path == visible
            if matches:
                return True
    return False


def _line_matches_record(line: str, record: Mapping) -> bool:
    name = record.get("name")
    arguments = record.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        return False
    text = _oneline_text(line)
    marker = _leading_file_marker(text)
    if marker in _FILE_TOOL_NAMES:
        if name != marker:
            return False
        selector = _file_selector(text, name)
        return selector is not None and _file_selector_matches(
            name, arguments, selector
        )
    if marker == "Writing":
        if name != "write_file":
            return False
        selector = _file_selector(text, name)
        return selector is not None and _file_selector_matches(
            name, arguments, selector
        )
    if marker == "Reading":
        if name == "read_file":
            selector = _file_selector(text, name)
            return selector is not None and _file_selector_matches(
                name, arguments, selector
            )
        if name == "web_extract":
            return _line_matches_arguments(line, arguments)
        return False
    if name in _FILE_TOOL_NAMES:
        return False
    return _line_matches_arguments(line, arguments)


def _tool_call_key(record: Mapping) -> str:
    content_key = _tool_call_content_key(record)
    if not content_key:
        return ""
    identifier = record.get("id")
    if isinstance(identifier, str) and identifier:
        return f"id:{identifier}"
    return content_key


def _tool_call_content_key(record: Mapping) -> str:
    try:
        encoding = json.dumps(
            record.get("arguments"),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        return ""
    digest = hashlib.sha256(
        f"{record.get('name')}\0{encoding}".encode("utf-8")
    ).hexdigest()
    return f"args:{digest}"


def _progress_lines(delta: str) -> list[str]:
    return [line.strip() for line in delta.splitlines() if line.strip()]


def collect_tool_frames(
    service: ChannelService,
    adapter: Any,
    session: transport.StreamSession,
    delta: str,
) -> list[dict]:
    """Best-effort structured tool calls for the progress lines in ``delta``.

    The rendered line is only a selector: the payload always comes from the
    recorded arguments. No history, no match, or an ambiguous match yields an
    empty list and leaves the text stream untouched.
    """
    lines = _progress_lines(delta)
    stream = _diagnostic_stream(session.chat_id)
    if not lines or adapter is None:
        _debug_tool_frames(
            "candidate_select",
            stream=stream,
            pending=0,
            history=0,
            records=0,
            candidates=0,
            matches=0,
            reason="no_lines" if not lines else "no_adapter",
        )
        return []
    source = _session_source(adapter, session)
    agent = _live_agent(adapter, source)
    if agent is None:
        for _line in lines:
            _debug_tool_frames(
                "candidate_select",
                stream=stream,
                pending=0,
                history=0,
                records=0,
                candidates=0,
                matches=0,
                reason="no_agent",
            )
        return []
    agent_session_id = _read_field(agent, "session_id")
    if not isinstance(agent_session_id, str):
        agent_session_id = ""
    diagnostic_private = (
        agent_session_id,
        session.chat_id,
        getattr(session, "node_id", ""),
    )
    pending_records = service.pending_tool_calls(agent_session_id)
    history_records = _recent_tool_calls(_agent_history(agent))
    records: list[dict] = []
    seen_keys: set[str] = set()
    seen_content: set[str] = set()
    for record in (*pending_records, *history_records):
        key = _tool_call_key(record)
        content_key = _tool_call_content_key(record)
        if not key or key in seen_keys or content_key in seen_content:
            continue
        seen_keys.add(key)
        if content_key:
            seen_content.add(content_key)
        records.append(record)
    if not records:
        for _line in lines:
            _debug_tool_frames(
                "candidate_select",
                stream=stream,
                pending=len(pending_records),
                history=len(history_records),
                records=0,
                candidates=0,
                matches=0,
                reason="no_records",
            )
        return []
    frames: list[dict] = []
    for line in lines:
        candidates: list[dict] = []
        for record in records:
            key = _tool_call_key(record)
            if not key or service.tool_announced(session.chat_id, key):
                continue
            candidates.append(record)
        matches = [
            record
            for record in candidates
            if _line_matches_record(line, record)
        ]
        announced_matches = [
            record
            for record in records
            if service.tool_announced(session.chat_id, _tool_call_key(record))
            and _line_matches_record(line, record)
        ]
        if not matches:
            reason = "already_announced" if announced_matches else "no_match"
        elif len(matches) > 1:
            reason = "ambiguous"
        else:
            reason = "matched"
        _debug_tool_frames(
            "candidate_select",
            stream=stream,
            pending=len(pending_records),
            history=len(history_records),
            records=len(records),
            candidates=len(candidates),
            matches=len(matches),
            reason=reason,
        )
        matched_keys = {_tool_call_key(record) for record in matches}
        for record in candidates[:TOOL_FRAME_DEBUG_CANDIDATES_MAX]:
            _debug_tool_frames(
                "candidate",
                stream=stream,
                call_id=_diagnostic_identifier(
                    record.get("id", ""), diagnostic_private
                ),
                tool_name=_diagnostic_identifier(
                    record.get("name", ""), diagnostic_private
                ),
                matched=_tool_call_key(record) in matched_keys,
            )
        if len(matches) != 1:
            continue
        record = matches[0]
        key = _tool_call_key(record)
        service.mark_tool_announced(session.chat_id, key)
        service.forget_tool_call(agent_session_id, key)
        prepared = _bound_tool_arguments(record["arguments"], record["name"])
        if prepared is None:
            _debug_tool_frames(
                "frame_prepare",
                stream=stream,
                call_id=_diagnostic_identifier(
                    record["id"], diagnostic_private
                ),
                tool_name=_diagnostic_identifier(
                    record["name"], diagnostic_private
                ),
                status="skipped",
                reason="invalid_arguments",
            )
            continue
        arguments, truncated = prepared
        _debug_tool_frames(
            "frame_prepare",
            stream=stream,
            call_id=_diagnostic_identifier(
                record["id"], diagnostic_private
            ),
            tool_name=_diagnostic_identifier(
                record["name"], diagnostic_private
            ),
            status="prepared",
            reason="bounded",
            truncated=truncated,
        )
        frames.append(
            {
                "id": str(record["id"])[:128],
                "name": record["name"],
                "arguments": arguments,
                "truncated": truncated,
            }
        )
    return frames


async def collect_response_metadata(
    adapter: Any,
    session: transport.StreamSession,
    *,
    now: Optional[float] = None,
) -> dict:
    source = _session_source(adapter, session)
    model_settings = await _session_model_settings(adapter, source)
    execution = _execution_payload()
    return build_metadata(
        generated_at=_utc_timestamp(now),
        latency_ms=_session_latency_ms(session),
        model_settings=model_settings,
        turn_usage=_execution_usage(execution),
        session_stats=_read_field(execution, "session_usage"),
        agent=_live_agent(adapter, source),
    )


async def deliver_message(
    service: ChannelService,
    session: transport.StreamSession,
    content: str,
    *,
    finalize: bool,
    metadata_factory: Optional[
        Callable[[transport.StreamSession], Any]
    ] = None,
    tool_frames_factory: Optional[
        Callable[[transport.StreamSession, str], Any]
    ] = None,
) -> None:
    if session.terminal:
        _debug_tool_frames(
            "final_delivery",
            stream=_diagnostic_stream(session.chat_id),
            status="skipped",
            reason="terminal",
            delta_length=0,
            frames=0,
        )
        return
    delta = service.router.consume(session.chat_id, content, final=finalize)
    frames: Any = None
    emitted_frames = 0
    collection_failed = False
    if delta and tool_frames_factory is not None:
        try:
            frames = tool_frames_factory(session, delta)
            if inspect.isawaitable(frames):
                frames = await frames
        except Exception as exc:
            logger.debug(
                "iroh tool frame collection failed: %s", type(exc).__name__
            )
            frames = None
            collection_failed = True
        for frame in frames or ():
            if not isinstance(frame, Mapping):
                continue
            name = frame.get("name")
            arguments = frame.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, Mapping):
                continue
            await session.emit_tool(
                frame.get("id") if isinstance(frame.get("id"), str) else "",
                name,
                dict(arguments),
                truncated=bool(frame.get("truncated")),
            )
            emitted_frames += 1
    if delta:
        await session.append(delta)
    if finalize:
        _debug_tool_frames(
            "final_delivery",
            stream=_diagnostic_stream(session.chat_id),
            status="failed" if collection_failed else "collected",
            reason=(
                "collection_failed"
                if collection_failed
                else "new_text"
                if delta
                else "no_new_text"
            ),
            delta_length=len(delta),
            frames=emitted_frames,
        )
    if finalize and service.router.mark_terminal(session.chat_id):
        metadata: Optional[dict] = None
        if metadata_factory is not None:
            try:
                metadata = await metadata_factory(session)
            except Exception as exc:
                logger.debug(
                    "iroh response metadata collection failed: %s",
                    type(exc).__name__,
                )
                metadata = None
        await session.finish(metadata=metadata)
        service.router.release(session.chat_id)
        service.release_tool_tracking(session.chat_id)


def build_prompt_handler(
    service: ChannelService, adapter: Any, event_factory: Callable[..., Any]
) -> Callable[[transport.StreamSession, str], Any]:
    async def _on_prompt(
        session: transport.StreamSession, text: str
    ) -> None:
        source = _stream_source(adapter, session)
        event = event_factory(text=text, source=source)
        service.router.reset(session.chat_id)
        service.reset_tool_tracking(session.chat_id)
        try:
            await adapter.handle_message(event)
        except Exception:
            logger.exception("iroh prompt submission failed")
            await session.fail(transport.ERR_INTERNAL)

    return _on_prompt


def _platform_entry_fields() -> set[str]:
    try:
        from gateway.platform_registry import PlatformEntry
    except Exception:
        return set()
    try:
        return {field.name for field in dataclasses.fields(PlatformEntry)}
    except (TypeError, AttributeError):
        return set()


def _check_requirements() -> bool:
    return transport.iroh_available()


def _validate_config(config: Any) -> bool:
    del config
    return transport.iroh_available()


def _no_active_stream_result(send_result: Any) -> Any:
    return send_result(success=False, error="no active iroh stream")


def build_adapter_class(
    service: ChannelService,
    base: Any,
    platform: Any,
    send_result: Any,
    event_factory: Callable[..., Any],
) -> Any:
    class IrohChannelAdapter(base):
        supports_async_delivery = False
        max_message_length = 0

        def __init__(self, config: Any) -> None:
            super().__init__(config, platform)
            self._server: Optional[transport.ChannelServer] = None

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            del is_reconnect
            try:
                server = await service.start()
            except Exception as exc:
                detail = str(exc).strip()
                reason = type(exc).__name__
                if detail:
                    reason = f"{reason}: {detail}"
                logger.error(
                    "iroh channel failed to start (%s): see /iroh_info and "
                    "the gateway log; if the platform is disabled, set "
                    "gateway.platforms.iroh.enabled=true and restart the "
                    "gateway from a normal shell outside it",
                    reason,
                )
                return False
            self._server = server
            server.set_prompt_handler(
                build_prompt_handler(service, self, event_factory)
            )
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            await service.stop()
            self._server = None
            self._mark_disconnected()

        async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: Any = None,
            metadata: Any = None,
        ) -> Any:
            del reply_to
            session = self._session(chat_id)
            if session is None:
                return _no_active_stream_result(send_result)
            meta = metadata if isinstance(metadata, Mapping) else {}
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "iroh send metadata keys: %s content_length=%d",
                    sorted(str(key) for key in meta),
                    len(content),
                )
            finalize = bool(meta.get("notify") or meta.get("final"))
            if meta.get("_interim_send"):
                finalize = False
            elif not finalize and not meta.get("expect_edits") and session.slash_prompt:
                finalize = True
            return await self._deliver(chat_id, content, finalize=finalize)

        async def edit_message(
            self,
            chat_id: str,
            message_id: str,
            content: str,
            *,
            finalize: bool = False,
        ) -> Any:
            del message_id
            return await self._deliver(chat_id, content, finalize=finalize)

        async def _deliver(
            self, chat_id: str, content: str, *, finalize: bool
        ) -> Any:
            session = self._session(chat_id)
            if session is None:
                return _no_active_stream_result(send_result)
            await deliver_message(
                service,
                session,
                content,
                finalize=finalize,
                metadata_factory=self._response_metadata,
                tool_frames_factory=self._tool_frames,
            )
            return send_result(success=True, message_id=session.chat_id)

        def _tool_frames(
            self, session: transport.StreamSession, delta: str
        ) -> list[dict]:
            return collect_tool_frames(service, self, session, delta)

        async def _response_metadata(
            self, session: transport.StreamSession
        ) -> dict:
            return await collect_response_metadata(self, session)

        async def send_typing(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            return None

        async def delete_message(self, *args: Any, **kwargs: Any) -> bool:
            del args, kwargs
            return False

        async def get_chat_info(self, chat_id: str) -> dict:
            session = self._session(chat_id)
            name = session.node_id if session is not None else chat_id
            return {"name": name, "type": "dm"}

        def _session(
            self, chat_id: str
        ) -> Optional[transport.StreamSession]:
            if self._server is None:
                return None
            return self._server.session_for(chat_id)

    return IrohChannelAdapter


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.platforms.event import MessageEvent

    if not transport.iroh_available():
        logger.warning(
            "iroh SDK is not importable; the iroh platform stays unavailable "
            "until `hermes plugins enable iroh-channel` installs "
            "iroh>=1.2,<2 and the gateway is restarted"
        )

    service = ChannelService.get()

    def _remember_tool_call(**kwargs: Any) -> None:
        """Observer-only ``pre_tool_call`` hook: keep full args for a later line."""
        try:
            service.remember_tool_call(
                kwargs.get("session_id", ""),
                kwargs.get("tool_call_id", ""),
                kwargs.get("tool_name", ""),
                kwargs.get("args"),
            )
        except Exception as exc:
            logger.debug(
                "iroh tool-call capture skipped: %s", type(exc).__name__
            )
        return None

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        try:
            register_hook("pre_tool_call", _remember_tool_call)
        except Exception as exc:
            logger.debug(
                "iroh pre_tool_call hook registration skipped: %s",
                type(exc).__name__,
            )

    def _adapter_factory(config: Any) -> Any:
        # Hermes only accepts the platform name after ctx.register_platform has
        # added it to the platform registry, so construct it when the adapter is
        # created rather than during plugin registration.
        adapter_class = build_adapter_class(
            service,
            BasePlatformAdapter,
            Platform(PLATFORM_NAME),
            SendResult,
            MessageEvent,
        )
        return adapter_class(config)

    kwargs: dict[str, Any] = {
        "name": PLATFORM_NAME,
        "label": PLATFORM_LABEL,
        "adapter_factory": _adapter_factory,
        "check_fn": _check_requirements,
        "validate_config": _validate_config,
        "platform_hint": PLATFORM_HINT,
        "max_message_length": 0,
        "emoji": "\U0001f517",
        "install_hint": (
            "The iroh Python package is not importable in the Hermes "
            "environment. Declare it in pyproject.toml (iroh>=1.2,<2) and "
            "re-enable the plugin so Hermes prepares its dependencies."
        ),
    }
    known = _platform_entry_fields()
    if "install_hint" not in known:
        kwargs.pop("install_hint", None)
    if "trusted_inbound" in known:
        kwargs["trusted_inbound"] = True
    if "supports_async_delivery" in known:
        kwargs["supports_async_delivery"] = False
    ctx.register_platform(**kwargs)

    def _command_handler(
        handler: Callable[[ChannelService, str], Any],
    ) -> Callable[..., Any]:
        async def _handler(
            raw_args: str = "", *args: Any, **handler_kwargs: Any
        ) -> str:
            del args, handler_kwargs
            return await handler(service, raw_args)

        return _handler

    ctx.register_command(
        name="iroh_info",
        handler=_command_handler(handle_info),
        description="Show the Iroh node id, ticket, relay mode, and allowlist size",
    )
    ctx.register_command(
        name="iroh_allow",
        handler=_command_handler(handle_allow),
        description="Authorize a client node id: /iroh_allow <client_node_id>",
    )
    ctx.register_command(
        name="iroh_revoke",
        handler=_command_handler(handle_revoke),
        description="Revoke a client node id: /iroh_revoke <client_node_id>",
    )
