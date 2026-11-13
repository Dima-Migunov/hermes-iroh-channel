# Iroh Channel for Hermes Agent

A Hermes Agent `platform` plugin that gives the agent a direct peer-to-peer
channel over [Iroh](https://iroh.computer). Clients dial the agent's node by
ticket, authenticate with node keys, and exchange newline-delimited JSON frames
over QUIC bidirectional streams. No inbound port, no platform account, and no
relay account are required to run the plugin.

This repository is the plugin directory: install it with
`hermes plugins install owner/iroh-channel`, or from a local checkout with
`hermes plugins install ./Iroh-channel`.

## Layout

| Path | Purpose |
| --- | --- |
| `plugin.yaml` | Hermes manifest (`kind: platform`, name `iroh-channel`) |
| `pyproject.toml` | Dependency declaration (`iroh>=1.2,<2`) and test extra |
| `__init__.py` | Import-light entry point delegating to `gateway_adapter.register` |
| `transport.py` | Iroh endpoint, node identity, allowlist, frame codec, stream sessions |
| `gateway_adapter.py` | Hermes platform adapter, response router, slash commands |
| `after-install.md` | Post-install steps Hermes shows after the plugin is installed |
| `tests/` | Unit and loopback integration tests |

`transport.py` has no Hermes imports, so the protocol layer can be exercised in a
plain Python environment. Hermes imports live inside
`gateway_adapter.register()` and the adapter factory.

## Installation

```bash
# from GitHub
hermes plugins install owner/iroh-channel

# or from the parent directory of a local checkout
hermes plugins install ./Iroh-channel
```

Install leaves the plugin disabled. Enable the plugin (loads `register()`) and the
platform (binds the endpoint) — they are two separate switches:

```bash
hermes plugins enable iroh-channel
hermes config set platforms.iroh.enabled true
hermes gateway restart
```

Enabling the plugin is what loads the adapter; the endpoint binds when the gateway
connects the `iroh` platform. The `iroh` Python package must be importable in the
Hermes environment; Hermes prepares it from this repository's `pyproject.toml`
when the plugin is enabled, and without it the plugin still loads but reports the
platform as unavailable with an install hint and starts no endpoint.

```bash
pip install 'iroh>=1.2,<2'
```

Enabling the plugin is what binds the endpoint. The `iroh` Python package must
be importable in the Hermes environment; without it the plugin still loads, but
reports the platform as unavailable with an install hint and starts no endpoint.

```bash
pip install 'iroh>=1.2,<2'
```

## Configuration

State lives in the plugin directory itself, so nothing has to be configured: the
plugin works wherever Hermes installed it, in whichever profile and `HERMES_HOME`.
The environment variables below are optional overrides.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_IROH_STATE_DIR` | the plugin directory | Override for the directory holding `node.key` and `allowlist.json` |
| `HERMES_IROH_KEY_PATH` | `$HERMES_IROH_STATE_DIR/node.key` | Node secret key file; overrides only the key path |
| `HERMES_IROH_ALLOWLIST` | unset | Comma- or whitespace-separated node ids, unioned with the allowlist file |
| `HERMES_IROH_RELAY` | unset | Relay mode: unset/blank for the n0 default relays, `disabled`/`off`/`none` to disable relaying, or a comma-separated relay URL list |
| `HERMES_IROH_TURN_TIMEOUT` | `600` | Seconds to wait for a gateway turn before the stream fails with `timeout` |

### State directory and key file

The node secret is a raw 32-byte Ed25519 key stored at `HERMES_IROH_KEY_PATH`.
By default it sits next to `plugin.yaml` in the installed plugin directory.

- The file is created on first start and is never regenerated, so the node id is
  stable across restarts.
- It is written atomically: a temporary sibling is `fsync`ed, `chmod 0600`, then
  renamed into place.
- A newly created parent directory is `chmod 0700`.
- If the file exists but is not a valid 32-byte key, startup fails and reports
  the path and the reason. The plugin never overwrites a malformed key and never
  silently changes its identity.

Hermes `plugins update` preserves untracked files, so the key survives an update.
`plugins remove` deletes the plugin directory, which takes the key with it: the
reinstalled plugin generates a new node id and every client must be authorized
again. Set `HERMES_IROH_STATE_DIR` to a directory outside the plugin when the
identity must outlive a reinstall.

### Relay configuration

| `HERMES_IROH_RELAY` | Result |
| --- | --- |
| unset or blank | `RelayMode.default_mode()` (n0 production relays) |
| `disabled`, `off`, `none` | `RelayMode.disabled()` |
| `https://relay.example.com,https://relay2.example.com` | `RelayMode.custom_from_urls([...])` with exactly those relays |
| anything unparseable | Startup fails with a diagnostic naming the invalid value |

Relay URLs must be absolute `http`/`https` URLs; empty list elements and other
schemes are rejected before binding. With relaying disabled, the ticket still
carries direct addresses, so clients on another network cannot connect unless a
direct address is reachable.

### Client allowlist

The effective allowlist is the **union** of the persisted file and the
environment variable, normalized by trimming whitespace and lowercasing.

File schema (`$HERMES_IROH_STATE_DIR/allowlist.json`):

```json
{"allowed_node_ids": ["3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"]}
```

Behavior:

- **Fail closed.** An absent, empty, or unparseable file plus an unset or empty
  environment variable yields an empty allowlist, which denies every client.
- A malformed file logs a warning and contributes no file entries; environment
  entries still apply.
- The check runs on the authenticated QUIC `remote_id` immediately after the
  connection is established, before `accept_bi()` and before any prompt byte is
  read. A rejected connection is closed immediately and no Hermes event is
  created.
- Entries are deduplicated and persisted in normalized form. The file is
  rewritten atomically with mode `0600`.
- The file is re-read when its mtime or size changes, so manual edits and
  `/iroh_allow` take effect for new connections without a restart.
- Additions are validated as 64-character hexadecimal Iroh node ids (and through
  `EndpointId.from_string` when `iroh` is importable). Invalid values are refused
  and the allowlist is unchanged.

The environment variable is a bootstrap mechanism, for example a container
secret. It does not replace the file, and nothing written by `/iroh_allow` goes
into the environment.

## Security model

- **Transport authentication is the authorization boundary.** A client is
  accepted only when its node id is in the effective allowlist. The node id is
  derived from the client's QUIC handshake, so a client cannot claim another
  node's identity.
- **Hermes user authorization is not bypassed for anything else.** Because every
  inbound event has already been authenticated and allowlisted, the platform
  registers with `trusted_inbound` when the running Hermes core exposes that
  field. Normal tool approvals, dangerous-command guards, and other platform
  authorizations are unaffected.
- **No unsolicited messages.** The platform delivers only replies to an active
  prompt stream; `supports_async_delivery` is off and sending to a chat without
  an active stream fails with an error result.
- **No secrets in logs.** Key material and allowlist contents are never logged;
  failures name the path and the error class, not the data.

## Protocol

This section is the client-facing contract. It is versioned by the ALPN string:
a breaking change requires a new ALPN.

### Transport

| Property | Value |
| --- | --- |
| ALPN | `iroh-channel/1` |
| Transport | QUIC over Iroh, one endpoint per agent |
| Streams | One bidirectional stream per prompt/response exchange |
| Framing | UTF-8 JSON Lines: one JSON object per line, one trailing `\n` per frame |
| Serialization | Compact JSON: `separators=(",", ":")`, non-ASCII preserved as UTF-8 |

A connection negotiated with another ALPN is refused. The server verifies the
negotiated ALPN again after the handshake and closes with an application error
code if it does not match.

A connection is closed before any stream is read when the remote node id is not
in the effective allowlist.

### Frame types

Every frame is a JSON object with a `type` field. Receivers ignore unknown fields
for forward compatibility.

| Type | Direction | Fields |
| --- | --- | --- |
| `prompt` | client → server | `type`: `"prompt"`, `text`: non-empty string, `session_id`: optional string |
| `start` | server → client | `type`: `"start"`, `session_id`: the session id used |
| `delta` | server → client | `type`: `"delta"`, `content`: string chunk of response text |
| `done` | server → client | `type`: `"done"` |
| `error` | server → client | `type`: `"error"`, `message`: stable error value |

Documented examples (each is one exact line ending with `\n`):

```json
{"type":"prompt","session_id":"default","text":"hello"}
{"type":"start","session_id":"default"}
{"type":"delta","content":"Hello! How can I help?"}
{"type":"done"}
{"type":"error","message":"session_busy"}
```

### Prompt

The client sends exactly one prompt per stream, as the first line:

```json
{"type":"prompt","session_id":"default","text":"Summarize the changelog"}
```

- `text` must be a non-empty string. It is sent verbatim as the user turn.
- `session_id` is optional. Absent, `null`, blank, or whitespace-only values
  become `default`. The value identifies a conversation within the client's node
  id; use a stable per-conversation value to resume a conversation across
  reconnects.
- The prompt line is capped at 64 KiB (64 * 1024 bytes) before its trailing
  newline. A longer line yields `{"type":"error","message":"prompt_too_large"}`.
- The client must send the newline (or close its send side) to finish the
  prompt. Bytes after the first newline are ignored; the stream is thereafter a
  response channel.

The server waits 30 seconds for the prompt line, then fails the stream with
`timeout`.

### Response sequence

For every accepted prompt the server sends:

1. exactly one `start` frame carrying the session id, before any text;
2. zero or more `delta` frames, in order;
3. exactly one terminal frame: `done` on success or `error` on failure.

No frame follows the terminal frame. The server finishes its send side after the
terminal frame; the client observes end-of-stream.

An empty agent response is `start` immediately followed by `done`.

### Append-only delta semantics

Delta content is concatenated in arrival order to reconstruct the reply:

```text
delta.content[0] + delta.content[1] + ... == assistant text
```

The server never repeats text already sent in the same response. When Hermes
starts a new text segment (for example after a tool call), the server separates
it from the previous segment with a blank line (`\n\n`) inside the next delta.
A segment boundary never ends the response.

Each `delta` frame carries at most 16 KiB (16 * 1024 bytes) of UTF-8 content;
longer text is split on character boundaries, so a client can concatenate
without repair.

### Limits

| Limit | Value |
| --- | --- |
| Prompt line | 64 KiB |
| Delta frame content | 16 KiB |
| Prompt read timeout | 30 s |
| Turn timeout | 600 s (`HERMES_IROH_TURN_TIMEOUT`) |
| Concurrent connections | 64 |
| Streams per connection | 8 |
| Total concurrent streams | 64 |
| In-flight prompts per node id + session id | 1 |

When a stream limit is reached the server answers the new stream with
`{"type":"error","message":"too_many_streams"}` and keeps existing streams
running.

### One in-flight prompt per session

A node id plus session id identifies one conversation. A second prompt for a
conversation that already has an in-flight prompt receives:

```json
{"type":"error","message":"session_busy"}
```

The first stream is untouched. Retry after the first stream's terminal frame.
Different session ids, or different node ids, run concurrently.

### Error values

| Value | Meaning |
| --- | --- |
| `invalid_prompt` | The first line is not a JSON object, is not type `prompt`, has empty or non-string `text`, or has a non-string `session_id` |
| `prompt_too_large` | The prompt line exceeded 64 KiB |
| `session_busy` | Another prompt is already in flight for this node id and session id |
| `timeout` | The prompt did not arrive within 30 s, or the turn produced no terminal frame within the turn timeout |
| `unauthorized` | Reserved for authorization failures; unauthorized connections are closed before a stream exists |
| `internal_error` | Unclassified failure; no internal detail is sent |
| `too_many_streams` | A connection or total stream limit was reached |

### Close behavior

- The server finishes its send side after `done` or `error`.
- If the client closes the stream before the terminal frame, the server stops
  forwarding, releases the session slot, and keeps serving other clients. The
  Hermes turn may continue, but its output is discarded.
- Closing one stream never affects another stream or connection.

### Client walkthrough

```python
import asyncio
import json

import iroh


async def main(ticket: str) -> None:
    iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
    client = await iroh.Endpoint.bind(
        iroh.EndpointOptions(
            preset=iroh.preset_n0(),
            alpns=[b"iroh-channel/1"],
        )
    )
    addr = iroh.EndpointTicket.from_string(ticket).endpoint_addr()

    conn = await client.connect(addr, b"iroh-channel/1")
    bi = await conn.open_bi()
    send, recv = bi.send(), bi.recv()

    await send.write_all(
        json.dumps({"type": "prompt", "session_id": "default", "text": "hello"})
        .encode() + b"\n"
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
            if frame["type"] == "delta":
                print(frame["content"], end="", flush=True)
            elif frame["type"] == "done":
                print()
            elif frame["type"] == "error":
                print(f"\n[error] {frame['message']}")

    await client.close()


asyncio.run(main("<ticket from /iroh_info>"))
```

Sequence: dial by ticket → `open_bi` → send one `prompt` line → read `start`,
`delta`…, terminal frame → close the stream. The client may close as soon as it
has the terminal frame.

### Session keying

The server keys a Hermes conversation as:

```text
chat_id = f"{remote_node_id}:{session_id}"
```

Hermes scopes a session by platform, chat id, and user id, and the platform sets
the user id to the authenticated node id. Two prompts from the same node id with
the same session id continue one conversation; different node ids or different
session ids are isolated.

## Slash commands

Both commands run in the gateway process that owns the session and return text
to the dispatcher; they never print and never raise into it.

### `/iroh_info`

Returns one labeled line per fact:

```text
node id: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
ticket: endpointabc123...
alpn: iroh-channel/1
relay: default
allowlist entries: 2
state dir: /home/you/.hermes/plugins/iroh-channel
```

- `node id` is always present; it is derived from the stored key, so it works
  before the endpoint binds.
- `ticket` is `unavailable` when the endpoint is not running, and the dial
  string otherwise.
- `relay` shows `default`, `disabled`, or the configured URL list.
- `allowlist entries` counts the effective union of the file and the environment.

### `/iroh_allow <client_node_id>`

Validates the value, persists it to `allowlist.json`, and confirms the
normalized id:

```text
/iroh_allow 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
→ allowlisted: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
```

With no argument or with extra arguments it returns
`usage: /iroh_allow <client_node_id>` and changes nothing. An invalid value
returns `invalid node id: <reason>` and changes nothing. Additions take effect
for the next connection without a restart.

## Delivery behavior and limitations

- **Streaming.** Assistant text is forwarded as the gateway produces it, as
  append-only deltas. A non-streamed reply arrives as a single delta followed by
  `done`.
- **No attachments.** Images, audio, and files are not transferred in either
  direction. The agent's platform hint tells it not to claim otherwise.
- **No typing indicators.** `send_typing` is a no-op.
- **No editing or deletion.** `delete_message` returns `False`; edits are
  interpreted as stream progress, not as message mutations.
- **No unsolicited messages.** Delivery requires an active prompt stream. A send
  to a chat id with no active stream returns an unsuccessful result with
  `error="no active iroh stream"`.
- **No message chunking.** `max_message_length` is 0, so Hermes does not split
  replies; deltas are bounded by the 16 KiB frame limit instead.

## Tests

```bash
python3 -m pytest -q          # unit and loopback integration tests
python3 -m compileall -q .    # byte-compilation check
```

Tests that need the Iroh SDK are skipped when `iroh` is not importable. The
integration tests bind loopback endpoints with relaying disabled, so they need
neither network access nor a relay account.
