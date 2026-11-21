"""Hermes gateway integration for the Iroh channel plugin.

All Hermes imports happen inside ``register()``, so this module imports cleanly
in a plain Python environment. The transport contract it consumes lives in
``transport.py``.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import inspect
import logging
import math
import os
import time
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


class ResponseRouter:
    """Converts cumulative Hermes content into append-only deltas."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}
        self._terminal: set[str] = set()

    def reset(self, chat_id: str) -> None:
        self.release(chat_id)

    def release(self, chat_id: str) -> None:
        self._last.pop(chat_id, None)
        self._terminal.discard(chat_id)

    def is_terminal(self, chat_id: str) -> bool:
        return chat_id in self._terminal

    def consume(self, chat_id: str, content: str) -> str:
        if chat_id in self._terminal:
            return ""
        previous = self._last.get(chat_id, "")
        if content == previous:
            return ""
        if content.startswith(previous):
            delta = content[len(previous) :]
        elif previous.startswith(content):
            return ""
        else:
            delta = SEGMENT_SEPARATOR + content if previous else content
        self._last[chat_id] = content
        return delta

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

    @classmethod
    def get(cls) -> "ChannelService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

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


def _context_fields(agent: Any) -> dict:
    compressor = _read_field(agent, "context_compressor")
    tokens = _metadata_int(
        _read_field(
            compressor,
            "context_tokens",
            "last_prompt_tokens",
            "current_tokens",
            "last_real_tokens",
        )
    )
    if tokens is None:
        tokens = _metadata_int(
            _read_field(agent, "context_tokens", "last_prompt_tokens")
        )
    length = _metadata_int(
        _read_field(
            compressor, "context_length", "max_tokens", "context_window"
        )
    )
    if length is None:
        length = _metadata_int(
            _read_field(agent, "context_length", "context_window")
        )
    percent = _metadata_float(
        _read_field(compressor, "context_percent", "used_percent")
    )
    if percent is None:
        percent = _metadata_float(_read_field(agent, "context_percent"))
    if percent is None and tokens is not None and length:
        percent = round(tokens * 100.0 / length, 1)
    fields: dict[str, Any] = {}
    if tokens is not None:
        fields["context_tokens"] = tokens
    if length is not None:
        fields["context_length"] = length
    if percent is not None:
        fields["context_percent"] = percent
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


def _plan_gauge(credits: Any) -> Optional[dict]:
    direct = _read_field(credits, "plan_gauge")
    candidates: list[Any] = []
    if direct is not None:
        candidates.append(direct)
    else:
        gauges = _read_field(credits, "plan_gauges", "gauges")
        if isinstance(gauges, (list, tuple)):
            binding = [
                gauge
                for gauge in gauges
                if _metadata_bool(
                    _read_field(gauge, "binding", "is_binding", "primary")
                )
            ]
            candidates.extend(binding or gauges)
    for gauge in candidates:
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
        if result:
            return result
    return None


def _account_balance(
    credits: Any, fallback_provider: Optional[str]
) -> dict:
    if credits is None:
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
    if as_of_ms is not None:
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


def build_metadata(
    *,
    generated_at: str,
    latency_ms: int,
    model_settings: Any = None,
    turn_usage: Any = None,
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
    for name in ("turn_agent", "agent", "cached_agent", "active_agent"):
        agent = _read_field(state, name)
        if agent is not None:
            return agent
    return None


def _live_agent(adapter: Any, source: Any) -> Any:
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None or source is None:
        return None
    key_for_source = getattr(runner, "_session_key_for_source", None)
    peek_state = getattr(runner, "_peek_session_state", None)
    if not callable(key_for_source) or not callable(peek_state):
        return None
    try:
        key = key_for_source(source)
        state = peek_state(key)
    except Exception:
        return None
    return _turn_agent(state)


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


def _execution_usage() -> Any:
    try:
        from gateway import session_results
    except Exception:
        return None
    holder = getattr(session_results, "execution_result", None)
    value = _execution_holder_value(holder)
    usage = _read_field(value, "usage")
    if usage is not None:
        return usage
    return value


async def collect_response_metadata(
    adapter: Any,
    session: transport.StreamSession,
    *,
    now: Optional[float] = None,
) -> dict:
    source = _session_source(adapter, session)
    model_settings = await _session_model_settings(adapter, source)
    return build_metadata(
        generated_at=_utc_timestamp(now),
        latency_ms=_session_latency_ms(session),
        model_settings=model_settings,
        turn_usage=_execution_usage(),
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
) -> None:
    if session.terminal:
        return
    delta = service.router.consume(session.chat_id, content)
    if delta:
        await session.append(delta)
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


def build_prompt_handler(
    service: ChannelService, adapter: Any, event_factory: Callable[..., Any]
) -> Callable[[transport.StreamSession, str], Any]:
    async def _on_prompt(
        session: transport.StreamSession, text: str
    ) -> None:
        source = _stream_source(adapter, session)
        event = event_factory(text=text, source=source)
        service.router.reset(session.chat_id)
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
            meta = metadata or {}
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
            )
            return send_result(success=True, message_id=session.chat_id)

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
