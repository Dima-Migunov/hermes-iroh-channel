"""Hermes gateway integration for the Iroh channel plugin.

All Hermes imports happen inside ``register()`` and the adapter factory, so this
module imports cleanly in a plain Python environment. The transport contract it
consumes lives in ``transport.py``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, Callable, Optional

try:
    from . import transport
except ImportError:
    import transport

logger = logging.getLogger(__name__)

PLATFORM_NAME = "iroh"
PLATFORM_LABEL = "Iroh"
PLATFORM_HINT = (
    "You are chatting over an Iroh peer-to-peer channel. Replies are streamed "
    "incrementally as plain text. Markdown is rendered literally and attachments "
    "are not supported: never claim to send files or images."
)
SEGMENT_SEPARATOR = "\n\n"


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
        if self.server is not None and self.server.running:
            return self.server
        self.server = transport.ChannelServer(
            key_store=self.key_store,
            allowlist=self.allowlist,
            turn_timeout=transport.turn_timeout_from_env(),
        )
        await self.server.start()
        return self.server

    async def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            await server.stop()

    def node_id(self) -> str:
        return self.key_store.node_id()

    def ticket(self) -> Optional[str]:
        if self.server is None:
            return None
        return self.server.ticket


def format_info(service: ChannelService) -> str:
    lines = [f"node id: {service.node_id()}"]
    ticket = service.ticket()
    lines.append(f"ticket: {ticket if ticket else 'unavailable'}")
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
    lines.append(f"state dir: {service.key_store.path.parent}")
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
    return f"allowlisted: {normalized}"


async def deliver_message(
    service: ChannelService,
    session: transport.StreamSession,
    content: str,
    *,
    finalize: bool,
) -> None:
    if session.terminal:
        return
    delta = service.router.consume(session.chat_id, content)
    if delta:
        await session.append(delta)
    if finalize and service.router.mark_terminal(session.chat_id):
        await session.finish()
        service.router.release(session.chat_id)


def build_prompt_handler(
    service: ChannelService, adapter: Any, event_factory: Callable[..., Any]
) -> Callable[[transport.StreamSession, str], Any]:
    async def _on_prompt(
        session: transport.StreamSession, text: str
    ) -> None:
        source = adapter.build_source(
            chat_id=session.chat_id,
            chat_name=session.node_id,
            chat_type="dm",
            user_id=session.node_id,
            user_name=session.node_id[:12],
        )
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
                logger.error(
                    "iroh channel failed to start: %s", type(exc).__name__
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
            finalize = bool(meta.get("notify") or meta.get("final")) or (
                not meta.get("expect_edits")
            )
            await deliver_message(service, session, content, finalize=finalize)
            return send_result(success=True, message_id=session.chat_id)

        async def edit_message(
            self,
            chat_id: str,
            message_id: str,
            content: str,
            *,
            finalize: bool = False,
        ) -> Any:
            del message_id
            session = self._session(chat_id)
            if session is None:
                return _no_active_stream_result(send_result)
            await deliver_message(service, session, content, finalize=finalize)
            return send_result(success=True, message_id=session.chat_id)

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

    service = ChannelService.get()
    platform = Platform(PLATFORM_NAME)
    adapter_class = build_adapter_class(
        service, BasePlatformAdapter, platform, SendResult, MessageEvent
    )

    kwargs: dict[str, Any] = {
        "name": PLATFORM_NAME,
        "label": PLATFORM_LABEL,
        "adapter_factory": adapter_class,
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
