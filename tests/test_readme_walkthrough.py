"""Verifies the README client walkthrough against a live loopback endpoint."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transport
from conftest import requires_iroh

pytestmark = requires_iroh


async def run_documented_walkthrough(
    ticket: str, *, secret_key: bytes | None = None
) -> list[dict]:
    """The exact client sequence from README "Client walkthrough"."""
    import iroh

    iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
    options: dict = {
        "preset": iroh.preset_minimal(),
        "bind_addr": "127.0.0.1:0",
        "alpns": [b"iroh-channel/1"],
        "relay_mode": iroh.RelayMode.disabled(),
    }
    if secret_key is not None:
        options["secret_key"] = secret_key
    client = await iroh.Endpoint.bind(iroh.EndpointOptions(**options))
    seen: list[dict] = []
    try:
        addr = iroh.EndpointTicket.from_string(ticket).endpoint_addr()

        conn = await client.connect(addr, b"iroh-channel/1")
        bi = await conn.open_bi()
        send, recv = bi.send(), bi.recv()

        await send.write_all(
            json.dumps(
                {"type": "prompt", "session_id": "default", "text": "hello"}
            ).encode()
            + b"\n"
        )
        await send.finish()

        buffer = b""
        while True:
            chunk = await recv.read(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                frame = json.loads(line)
                seen.append(frame)
                if frame["type"] in ("done", "error"):
                    return seen
        return seen
    finally:
        await client.close()


async def test_documented_walkthrough_matches_server_sequence(tmp_path):
    import iroh

    server_key = transport.NodeKeyStore(path=tmp_path / "server.key")
    client_key = transport.NodeKeyStore(path=tmp_path / "client.key")
    client_secret = client_key.load_or_create()
    client_id = str(client_secret.public())

    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(
        json.dumps({"allowed_node_ids": [client_id]}), encoding="utf-8"
    )

    async def handler(session, text):
        await session.append("Hello! ")
        await session.append("How can I help?")

    server = transport.ChannelServer(
        key_store=server_key,
        allowlist=transport.Allowlist(
            path=allowlist_path, env_reader=lambda: ""
        ),
        relay=transport.parse_relay_setting("disabled"),
        bind_addr="127.0.0.1:0",
        preset=iroh.preset_minimal(),
        prompt_handler=handler,
    )

    async def finish_after_reply(session, text):
        await handler(session, text)
        await session.finish()

    server.set_prompt_handler(finish_after_reply)
    try:
        await server.start()
        frames = await asyncio.wait_for(
            run_documented_walkthrough(
                server.ticket, secret_key=client_secret.to_bytes()
            ),
            timeout=30,
        )

        types = [frame["type"] for frame in frames]
        assert types[0] == "start"
        assert types[-1] == "done"
        assert types.count("done") == 1
        assert frames[0]["session_id"] == "default"
        assert "".join(
            frame["content"] for frame in frames if frame["type"] == "delta"
        ) == "Hello! How can I help?"
    finally:
        await server.stop()


def test_readme_walkthrough_uses_the_same_calls():
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )

    for fragment in (
        "iroh.iroh_ffi.uniffi_set_event_loop",
        "iroh.Endpoint.bind(",
        "iroh.EndpointTicket.from_string(ticket).endpoint_addr()",
        'await client.connect(addr, b"iroh-channel/1")',
        "await conn.open_bi()",
        '{"type": "prompt", "session_id": "default", "text": "hello"}',
        "await send.finish()",
        'if frame["type"] == "delta"',
        'elif frame["type"] == "done"',
        'elif frame["type"] == "error"',
    ):
        assert fragment in text, f"README walkthrough is missing {fragment}"
