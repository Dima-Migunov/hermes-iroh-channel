# Iroh Channel for Hermes Agent

A Hermes Agent `platform` plugin that gives the agent a direct peer-to-peer
channel over [Iroh](https://iroh.computer). Clients dial the agent's node by
ticket, authenticate with node keys, and exchange newline-delimited JSON frames
over QUIC bidirectional streams. No inbound port, no platform account, and no
relay account are required to run the plugin.

## What this is for

Iroh Channel connects Hermes Agent to a desktop client over one direct, encrypted
Iroh connection. Use it to chat with the agent from another machine without an
inbound port, a VPN, or a platform account. This plugin is the server half: it
serves allowlisted clients, and a client application is required to talk to it.

## Client application

**Hermes Iroh App** ([`hermes-iroh-app`](https://github.com/Dima-Migunov/hermes-iroh-app))
is the supported desktop client for Windows, macOS (Apple Silicon), and Linux.
Download a build from its
[releases page](https://github.com/Dima-Migunov/hermes-iroh-app/releases).

The app generates a stable client node id, shown under Connection on its Settings
page. Allowlist that node id on the server with
`/iroh_allow <client_node_id>` before the app can connect. The id is derived from
the app's local key and survives app restarts; losing that key creates a new node
id that must be allowlisted again.

This repository is the plugin directory: install it with
`hermes plugins install Dima-Migunov/hermes-iroh-channel`.

## Quick start

1. Install the plugin:
   `hermes plugins install Dima-Migunov/hermes-iroh-channel`
   ([details](#installation)).
2. Enable it with `scripts/enable.sh`, or use the
   [manual commands](#1-enable-the-plugin-and-the-platform).
3. [Restart the gateway from outside it](#2-restart-the-gateway-from-outside-it).
4. Read the ticket with `/iroh_info` ([details](#3-check-iroh_info)).
5. Install Hermes Iroh App ([download](#client-application)).
6. Copy the client node id from the app's Settings page and authorize it with
   `/iroh_allow <client_node_id>` ([details](#5-authorize-the-client)).
7. Paste the ticket into the app's server target field in Settings, then connect
   ([details](#4-send-the-ticket)).

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
plain Python environment. Hermes imports live inside `gateway_adapter.register()`.

## Installation

```bash
# from GitHub
hermes plugins install Dima-Migunov/hermes-iroh-channel
```

The install clones the repository into `~/.hermes/plugins/iroh-channel/` and
leaves the plugin disabled. It asks `Enable 'iroh-channel' now? [y/N]`; answering
no is the default, so enabling is a separate, explicit step.

### 1. Enable the plugin and the platform

The helper is the supported terminal path:

```bash
scripts/enable.sh
```

It enables `iroh-channel`, sets `gateway.platforms.iroh.enabled=true`, verifies
the result with `hermes plugins list`, and prints the restart command for the
detected host. Re-running it is safe, and it never runs `pip install`.

Manual equivalent:

```bash
hermes plugins enable iroh-channel
hermes config set gateway.platforms.iroh.enabled true
```

`hermes plugins enable` is also the step that prepares the plugin's Python
dependencies: Hermes reads `pyproject.toml` (`iroh>=1.2,<2`), asks for consent,
and installs it into the managed environment. Do not `pip install iroh`
yourself — a manually installed package is not part of that managed
environment and is not retained when Hermes rebuilds it.

Enabling the plugin loads `register()`. The endpoint itself binds when the
gateway connects the `iroh` platform, which is the second switch above.
Without the `iroh` package the plugin still loads, but reports the platform as
unavailable with an install hint and starts no endpoint.

#### Terminal helper

`scripts/enable.sh` is the supported terminal path. It does not restart the
gateway by default. `--restart` performs the restart and is only for a human
running the helper from a normal shell outside the gateway: restarting from
inside kills the agent session that invoked it. `scripts/enable.sh --help`
lists the options.

### 2. Restart the gateway from outside it

Platform plugins load only when the gateway starts. The agent runs inside the
gateway process, so it cannot restart that process; Hermes refuses with
`gateway_lifecycle_block`, and that refusal is expected. One human or host
action performs the restart:

| Host | Restart path |
| --- | --- |
| Local Hermes Desktop | Quit and reopen the app, or use its restart control |
| Web host with controls | Use the host panel's restart or stop/start control |
| CLI or systemd | From a shell outside the gateway: `systemctl --user restart hermes-gateway.service` (fallback `hermes gateway restart`) |
| Remote Desktop client | The client cannot restart a gateway on another host; the host operator or host panel must do it |

The helper prints the command for the detected host. It does not run it unless
a human passes `--restart` outside the gateway.

### 3. Check `/iroh_info`

```text
/iroh_info
```

When the endpoint is running, the output contains `endpoint: running` and a
dial ticket. While it says `endpoint: not running` and `ticket: unavailable`, a
`hint:` line names the next step. See [Slash commands](#slash-commands) for the
full output.

### 4. Send the ticket

Send the ticket to Hermes Iroh App: paste it into the app's server target field
in Settings. The node id identifies this agent; the ticket is the dial string.

### 5. Authorize the client

```text
/iroh_allow <client_node_id>
```

The client's node id must be in the allowlist before it can connect. Additions
take effect for the next connection without a restart. See
[`/iroh_allow`](#iroh_allow-client_node_id).

Revoke access later with `/iroh_revoke <client_node_id>`; removal also takes
effect for the node's next stream without a restart. See
[`/iroh_revoke`](#iroh_revoke-client_node_id).

### Troubleshooting installation

| Symptom | Cause | Action |
| --- | --- | --- |
| `gateway_lifecycle_block` | Expected: the agent is inside the gateway | Use the human or host restart step; do not retry the command from the session |
| `endpoint: not running` / `ticket: unavailable` | Platform disabled or the gateway has not restarted | Run `scripts/enable.sh`, then restart outside the gateway |
| `hint:` names the missing SDK | `iroh` is not importable | `hermes plugins enable iroh-channel`, accept the dependency prompt, restart |
| Client connects and is closed immediately | Node id not allowlisted | Run `/iroh_allow <node_id>` and reconnect |
| Clients time out while connecting under load | Pending handshake admission cap reached | Retry; raise `HERMES_IROH_MAX_PENDING_HANDSHAKES` if the host has capacity |
| Client gets `invalid_prompt` | `session_id` failed the grammar | Use only ASCII letters, digits, `_`, `.`, `-`, at most 128 characters, without `..` |

### Troubleshooting tool frames

Tool-frame correlation is silent by default. To enable content-free diagnostics for
one gateway process, prefix the normal gateway launch command with
`HERMES_IROH_DEBUG_TOOL_FRAMES=1`:

```bash
HERMES_IROH_DEBUG_TOOL_FRAMES=1 <your-normal-gateway-launch-command>
```

Only `1`, `true`, `yes`, and `on` (case-insensitive, with surrounding whitespace
ignored) enable it. Reproduce one turn, then inspect the gateway log for lines
prefixed with `iroh tool-frames debug:`. The gateway logger must be at `DEBUG`
level; otherwise these records are filtered out. Remove the variable and restart
normally when finished.

The report contains only correlation metadata: a short stream hash, record and
candidate counts, call ids and tool names, match counts, skip reasons, and text or
buffer lengths. For example, `event=candidate_select ... candidates=2 matches=2
reason=ambiguous` means the visible progress matched more than one unannounced
call. `reason=no_records` means no captured or recent call was available;
`no_match` means candidates existed but none matched; `already_announced` means
the matching call was already emitted; and `invalid_arguments` means a matched
record could not be bounded into a safe frame. `router_buffer` and
`final_delivery` events explain delayed text and final collection using lengths
and statuses only.

Diagnostics never include progress text, paths, argument keys or values, file
contents, queries, credentials, raw Hermes session ids, chat ids, node ids, or
client identity. They are debug for this change's correlation only and do not
change assistant-text delivery.

For a reproducible install, pin the commit:

```bash
hermes plugins install Dima-Migunov/hermes-iroh-channel --ref <40-character-commit-sha>
```

To work from a local checkout instead, copy the directory into
`~/.hermes/plugins/iroh-channel/` and run `hermes plugins enable iroh-channel`.
`hermes plugins install` takes a catalog name, `owner/repo`, or a Git URL — a
local filesystem path is not a supported source.

## Configuration

State lives in the plugin directory itself, so nothing has to be configured: the
plugin works wherever Hermes installed it, in whichever profile and `HERMES_HOME`.
These are the settings the manifest declares, all optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_IROH_ALLOWLIST` | unset | Comma- or whitespace-separated node ids, unioned with the allowlist file |
| `HERMES_IROH_RELAY` | unset | Relay mode: unset/blank for the n0 default relays, `disabled`/`off`/`none` to disable relaying, or a comma-separated relay URL list |
| `HERMES_IROH_TURN_TIMEOUT` | `600` | Seconds to wait for a gateway turn before the stream fails with `timeout`; finite values from `1` to `86400` |
| `HERMES_IROH_MAX_PENDING_HANDSHAKES` | `32` | Concurrent incoming handshakes admitted before excess connections are refused |
| `HERMES_IROH_MAX_STREAMS_PER_NODE` | `2` | Concurrent prompts allowed per authenticated node id |
| `HERMES_IROH_MAX_PROMPTS_PER_MINUTE` | `30` | Prompts allowed per authenticated node id in a sliding 60-second window |
| `HERMES_IROH_MAX_RESPONSE_BYTES` | `4194304` (4 MiB) | Response content bytes forwarded per turn before `response_too_large` |

The four quota limits must be positive integers. `HERMES_IROH_TURN_TIMEOUT`
must be finite and between 1 and 86400 seconds. An invalid value fails startup
instead of silently falling back to the default.

### State directory and key file

The node secret is a raw 32-byte Ed25519 key stored next to `plugin.yaml` in the
installed plugin directory. It is not a manifest setting. For advanced setups two
path overrides exist: `HERMES_IROH_STATE_DIR` moves the directory, and
`HERMES_IROH_KEY_PATH` moves only the key file.

- The file is created on first start and is never regenerated, so the node id is
  stable across restarts.
- It is written atomically: a temporary sibling is `fsync`ed, `chmod 0600`, then
  renamed into place.
- A newly created parent directory is `chmod 0700`.
- The key file must be a regular file owned by the current user with no group or
  other permission bits (`0600` or stricter). Symlinks and `0644`/`0660` keys are
  refused, and the file is never modified or regenerated. Fix the mode with
  `chmod 600 node.key` and restart.
- The state directory must be owned by the current user and must not be group- or
  world-writable. Writes fail with the path and the required ownership/mode. The
  plugin directory in a checkout is often group-writable, so local runs should set
  `HERMES_IROH_STATE_DIR` to a private directory (`chmod 700`).
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
| anything unparseable | Startup fails with a diagnostic naming the entry position and reason |

Relay URLs must be absolute `http`/`https` URLs with a non-empty hostname and a
valid port. Empty list elements, other schemes, whitespace or control characters,
and URLs with userinfo (`https://user:secret@relay.example.com`) are rejected
before binding. Error messages name the entry position and the reason and never
echo the raw value, so embedded credentials cannot leak through an exception.
With relaying disabled, the ticket still carries direct addresses, so clients on
another network cannot connect unless a direct address is reachable.

Diagnostics are redacted: `/iroh_info`, startup logs, errors, and
`RelayConfig.describe()` show only the scheme, host, port, and path. Userinfo,
query strings, and fragments are omitted.

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
- The file must be a regular file owned by the current user, must not be a
  symlink, must not be group- or world-writable, and must be at most 1 MiB. A file
  that fails these checks contributes no entries and logs a content-free warning;
  environment entries still apply.
- A malformed file logs a warning and contributes no file entries; environment
  entries still apply.
- File and environment entries are validated as 64-character hexadecimal node
  ids. Invalid entries are ignored and authorize no client; the warning reports
  only a count.
- The check runs on the authenticated QUIC `remote_id` immediately after the
  connection is established, before `accept_bi()` and before any prompt byte is
  read. It runs again for every new stream before the prompt is read and again
  before the session is created. A rejected connection or stream is closed and no
  Hermes event is created.
- Revocation applies to new streams on established connections: after
  `/iroh_revoke` removes an entry, the next stream from that node fails with
  `unauthorized`. A turn that is already running may finish. Re-adding the entry
  accepts new streams again.
- Entries are deduplicated and persisted in normalized form. The file is
  rewritten atomically with mode `0600`.
- The file is re-read when its mtime or size changes, so manual edits and
  `/iroh_allow` take effect without a restart. Added and removed public node ids
  are logged at `INFO` on reload.
- Additions are validated as 64-character hexadecimal Iroh node ids (and through
  `EndpointId.from_string` when `iroh` is importable). Invalid values are refused
  and the allowlist is unchanged.
- `/iroh_allow` and `/iroh_revoke` refuse to rewrite a symlinked, wrong-owner,
  or group/world-writable file. A malformed but otherwise safe file can still be
  repaired by an explicit add.

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
- **Bounded work before authentication.** At most 32 incoming handshakes are in
  flight; excess connections are refused or dropped without creating a handler
  task. Transient accept failures retry with 0.05 s to 5 s exponential backoff
  instead of stopping acceptance.
- **Per-client quotas.** Each authenticated node id has a concurrent-prompt limit,
  a prompt rate limit over a sliding 60-second window, and a per-turn response
  byte cap. One noisy client cannot starve another.
- **No secrets in logs.** Key material and relay credentials are never logged;
  failures name the path and the error class, not the data. Public node ids may be
  audited at `INFO` for allowlist changes, rejections, and quota decisions.
  High-volume rejection records are throttled with a suppressed-count summary so
  a flood cannot become a log-volume denial of service.

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
| `tool` | server → client | `type`: `"tool"`, `id`: tool-call id when available, `name`: tool name, `arguments`: JSON object, `truncated`: optional `true` |
| `delta` | server → client | `type`: `"delta"`, `content`: string chunk of response text |
| `done` | server → client | `type`: `"done"`, `metadata`: optional object |
| `error` | server → client | `type`: `"error"`, `message`: stable error value |

Documented examples (each is one exact line ending with `\n`):

```json
{"type":"prompt","session_id":"default","text":"hello"}
{"type":"start","session_id":"default"}
{"type":"tool","id":"call_1","name":"web_search","arguments":{"query":"heavy lift voyage analysis"}}
{"type":"delta","content":"🔍 Searching the web for \"heavy lift voyage analy...\""}
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
  become `default`. A non-blank value must match
  `^(?!.*\.\.)[A-Za-z0-9_.-]{1,128}$`: ASCII letters, digits, `_`, `.`, and `-`,
  at most 128 characters, with no two consecutive dots. Surrounding whitespace is
  not trimmed, so ` session` is invalid. The value identifies a conversation
  within the client's node id; use a stable per-conversation value to resume a
  conversation across reconnects. An invalid value yields `invalid_prompt` before
  a session is created.
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
2. zero or more `tool` frames and zero or more `delta` frames, with every
   `tool` frame placed before the `delta` that carries its progress line;
3. exactly one terminal frame: `done` on success or `error` on failure.

No frame follows the terminal frame. The server finishes its send side after the
terminal frame; the client observes end-of-stream.

An empty agent response is `start` immediately followed by `done`. A successful
`done` frame may also carry an optional `metadata` object; see
[Response metadata](#response-metadata). A `tool` frame never replaces or changes
`delta` content: concatenating `delta.content` still reconstructs the same text.

### Response metadata

A successful `done` frame may carry a `metadata` object describing the turn. It is
optional: a client that only understands `type` still completes the response, and
the frame is exactly `{"type":"done"}` when no metadata is available.

```json
{"type":"done","metadata":{"schema":1,"generated_at":"2026-11-21T12:00:00.000Z","latency_ms":812,"model":"gpt-5.1","provider":"openai","usage":{"input_tokens":1200,"output_tokens":300,"total_tokens":1500},"session_usage":{"input_tokens":10200,"output_tokens":2400,"total_tokens":12600,"api_calls":8,"context_tokens":8400,"context_length":128000,"context_percent":7},"balance":{"provider":"nous","remaining_micros":12500000,"remaining_usd":12.5,"paid_access":true,"as_of_ms":1763726400000}}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | integer | Metadata version; currently `1` |
| `generated_at` | string | ISO-8601 UTC timestamp when the terminal frame was built |
| `latency_ms` | integer | Milliseconds from prompt acceptance to the terminal frame |
| `model` | string | Model that produced the turn, when Hermes reports it |
| `provider` | string | Provider for that model, when Hermes reports it |
| `usage` | object | Per-turn token counts: `input_tokens`, `output_tokens`, `total_tokens`, and `reasoning_tokens`, `cache_read_tokens`, `cache_write_tokens` when available |
| `session_usage` | object | Cumulative conversation counters: `input_tokens`, `output_tokens`, `total_tokens`, `api_calls`, `estimated_cost_usd`, `cost_status`, `cost_source`, and the context fields `context_tokens` (tokens in use), `context_length` (window size), `context_percent` (integer 0-100) when available |
| `balance` | object | Account view: `provider`, `remaining_micros`, `remaining_usd`, `subscription_micros`, `subscription_usd`, `subscription_limit_usd`, `purchased_micros`, `purchased_usd`, `paid_access`, `as_of_ms`, and an optional `plan_gauge` with `id`, `label`, `used_bp`, `resets_at_ms` |

- `schema`, `generated_at`, and `latency_ms` are always present on a metadata
  object. Every other field is omitted when Hermes does not expose the value;
  the server never sends `null` or a fabricated placeholder.
- `usage` describes one turn; `session_usage` describes the whole conversation.
  The keys are distinct so a client cannot mistake session totals for the cost
  of one reply.
- The serialized `metadata` object is capped at 4096 bytes. If it would exceed
  that bound or cannot be encoded as JSON, the server sends a bare
  `{"type":"done"}` and the turn still completes normally.
- Only a successful `done` frame carries metadata. `error` terminal frames never
  do.
- Metadata does not consume the response byte budget:
  `HERMES_IROH_MAX_RESPONSE_BYTES` bounds assistant text only.
- The object contains only the numeric, boolean, and short string fields listed
  above. It never contains prompt or response text, node ids, chat ids, session
  keys, API keys, tokens, credentials, endpoint URLs, or provider headers.
- Collection is best-effort and feature-detected: an older or changed Hermes
  core yields fewer fields, and a collection failure still produces a normal
  `done`.

### Tool call frames

A `tool` frame is optional structured data for a tool call whose human-readable
progress line is truncated (for example with `...`). It is sent only when the
server can recover the recorded arguments from the live Hermes agent; otherwise
no frame is sent and the text stream is unchanged.

```json
{"type":"tool","id":"call_abc123","name":"web_search","arguments":{"query":"heavy lift project cargo vessel voyage analysis"}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | Always `"tool"` |
| `id` | string | Hermes tool-call id when available; omitted when Hermes exposes none |
| `name` | string | Tool name exactly as Hermes recorded it |
| `arguments` | object | Bounded, redacted copy of the recorded tool arguments |
| `truncated` | boolean | Present and `true` when any value was clipped, redacted, or dropped |

- A `tool` frame appears after `start` and before the `delta` frame whose content
  contains the corresponding progress line. The frame and the line describe the
  same call; a client SHOULD attach the frame to the next new progress line it
  receives. When one delta carries several new progress lines, their frames are
  sent in line order immediately before that delta.
- A tool call is announced at most once per response. Cumulative progress edits
  do not repeat a frame, and no `tool` frame follows `done` or `error`.
- Frame collection also runs for the turn-final delivery. If that delivery carries
  a new, uniquely correlatable progress line, its `tool` frame is sent before the
  delivery's text delta and before `done`. A final answer with no new progress
  produces no frame.
- The Hermes `pre_tool_call` observer accepts arguments as a mapping or as a JSON
  string that decodes to an object. Malformed JSON, arrays, scalars, and other
  unsupported shapes are ignored without logging argument values or changing the
  assistant-text stream.
- The frame is display data only. `delta.content` must not be edited, reordered,
  or reconstructed from it, and the client MUST NOT execute or replay the
  arguments.
- The encoded frame is capped at 16 KiB (16 * 1024 bytes). Values whose key names
  indicate credentials or secrets (for example `api_key`, `token`, `password`,
  `authorization`, `client_secret`) are replaced with `"[redacted]"`, and
  secret-shaped values embedded in longer strings are redacted too. Payload keys
  such as `content`, `patch`, or `data` become `"[omitted]"` so file and binary
  contents are not forwarded. Overlong strings and collections are trimmed,
  unsupported values become `"[unsupported]"`, and the frame carries
  `"truncated":true`.
- Tool frames do not consume the assistant-text response budget. A client that
  does not understand `tool` ignores it and sees exactly the same `delta` text and
  exactly one terminal frame.
- File-tool progress is correlated by operation as well as visible path text.
  `read_file` may show a basename, a truncated basename or path, and an optional
  line label such as ` L20` or ` L20-35`; Hermes path aliases (`path`, `file`,
  `filepath`, and defensively `file_path`) are recognized. `write_file` uses only
  its `path` argument and may show a truncated path prefix. Path separators are
  normalized for comparison without accessing the filesystem.
- A raw `read_file` or `write_file` name restricts matching to that operation, and
  the friendly `Writing` label means `write_file`. `Reading` is shared by
  `read_file` and `web_extract`, so the visible selector and argument schema must
  also match. An ellipsis never supplies or guesses hidden path characters, and
  `write_file.content` is never searched for a progress selector.
- A frame requires exactly one unannounced matching call in the active session.
  Duplicate basenames, shared truncated prefixes, and untruncated partial
  basenames remain ambiguous and produce text without a `tool` frame. Non-file
  tools retain unambiguous argument-prefix matching.
- Absence of a `tool` frame means "no structured data"; it never means an empty
  tool call. Some tool calls produce no progress line or no recoverable history.

Recommended client history model: keep the assistant message `content` exactly
as received, and store a parallel list such as
`toolCalls: [{ id, name, arguments, truncated, lineIndex }]` keyed by the progress
line's index in that message. Render the stored `arguments` in the tooltip for
that line. If a history entry predates this frame or has no matching entry, show
the line as-is.

### Append-only delta semantics

Delta content is concatenated in arrival order to reconstruct the reply:

```text
delta.content[0] + delta.content[1] + ... == assistant text
```

The server never repeats text already sent in the same response. When Hermes
starts a new text segment (for example after a tool call), the server separates
it from the previous segment with a blank line (`\n\n`) inside the next delta.
A segment boundary never ends the response.

An unterminated trailing line is retained across cumulative deliveries so a split
progress line can be correlated and announced before its completed text. The
buffer is bounded; when a line exceeds that bound it is flushed without loss.
At turn end, any remaining fragment is flushed as text without inventing a
`tool` frame. Consequently, the final unterminated line of ordinary streamed text
may arrive on the next newline, the safety-bound flush, or the turn-final delta.

Each `delta` frame carries at most 16 KiB (16 * 1024 bytes) of UTF-8 content;
longer text is split on character boundaries, so a client can concatenate
without repair.

### Limits

| Limit | Value |
| --- | --- |
| Prompt line | 64 KiB |
| Delta frame content | 16 KiB |
| Tool frame | 16 KiB |
| Response content per turn | 4 MiB (`HERMES_IROH_MAX_RESPONSE_BYTES`) |
| Response metadata per `done` frame | 4096 bytes |
| Prompt read timeout | 30 s |
| Turn timeout | 600 s (`HERMES_IROH_TURN_TIMEOUT`) |
| Pending incoming handshakes | 32 (`HERMES_IROH_MAX_PENDING_HANDSHAKES`) |
| Concurrent connections | 64 |
| Streams per connection | 8 |
| Total concurrent streams | 64 |
| Concurrent prompts per node id | 2 (`HERMES_IROH_MAX_STREAMS_PER_NODE`) |
| Prompts per node id per 60 s | 30 (`HERMES_IROH_MAX_PROMPTS_PER_MINUTE`) |
| In-flight prompts per node id + session id | 1 |

The `tool` frame limit is per frame. Tool frames are presentation data and do
not consume the 4 MiB assistant-text budget.

When a stream limit is reached the server answers the new stream with
`{"type":"error","message":"too_many_streams"}` and keeps existing streams
running. Exceeding the per-node prompt rate yields `rate_limited`. Exceeding the
response cap yields one `response_too_large` terminal error and stops forwarding.
Each node's quota is independent: one client reaching its limit does not block
another.

Acceptance is resilient: a transient endpoint accept error retries with bounded
exponential backoff (0.05 s doubling to 5 s, reset after a success) instead of
permanently stopping the accept loop.

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
| `invalid_prompt` | The first line is not a JSON object, is not type `prompt`, has empty or non-string `text`, has a non-string `session_id`, or has a `session_id` outside the accepted grammar |
| `prompt_too_large` | The prompt line exceeded 64 KiB |
| `session_busy` | Another prompt is already in flight for this node id and session id |
| `timeout` | The prompt did not arrive within 30 s, or the turn produced no terminal frame within the turn timeout |
| `unauthorized` | The node's allowlist entry was removed before the stream started; no prompt is read and no Hermes event is created |
| `internal_error` | Unclassified failure; no internal detail is sent |
| `too_many_streams` | The per-connection, total, or per-node concurrent-stream limit was reached |
| `rate_limited` | The node exceeded `HERMES_IROH_MAX_PROMPTS_PER_MINUTE`; no turn starts |
| `response_too_large` | The assistant response exceeded `HERMES_IROH_MAX_RESPONSE_BYTES`; forwarding stopped after one terminal error |

### Close behavior

- The server finishes its send side after `done` or `error`.
- If the client closes the stream before the terminal frame, the server notices
  that the peer stopped reading, stops forwarding, releases the session slot
  immediately, and keeps serving other clients. The Hermes turn may continue,
  but its output is discarded. A prompt on the same session id sent right after
  a stop is accepted instead of answering `session_busy`.
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
            elif frame["type"] == "tool":
                # Keep for the tooltip; never modify the delta text.
                print(
                    f"\n[tool {frame.get('id', '')}] {frame['name']}: "
                    f"{json.dumps(frame['arguments'], ensure_ascii=False)}",
                    flush=True,
                )
            elif frame["type"] == "done":
                print()
            elif frame["type"] == "error":
                print(f"\n[error] {frame['message']}")

    await client.close()


asyncio.run(main("<ticket from /iroh_info>"))
```

Sequence: dial by ticket → `open_bi` → send one `prompt` line → read `start`,
`tool`/`delta`…, terminal frame → close the stream. The client may close as soon
as it has the terminal frame.

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

All three commands run in the gateway process that owns the session and return
text to the dispatcher; they never print and never raise into it.

### `/iroh_info`

Returns one labeled line per fact. A running endpoint:

```text
node id: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
ticket: endpointabc123...
endpoint: running
alpn: iroh-channel/1
relay: default
allowlist entries: 2
state dir: /home/you/.hermes/plugins/iroh-channel
```

When the endpoint is not running, `ticket` is `unavailable` and one `hint:`
line at the end names the next step:

```text
ticket: unavailable
endpoint: not running
hint: the endpoint is not connected; set gateway.platforms.iroh.enabled=true and restart the gateway from a normal shell outside it, then check /iroh_info again
```

- `node id` is derived from the stored key, so it works before the endpoint
  binds. If the key cannot be read, it prints `node id: unavailable`, logs the
  exception class, and still returns the other facts.
- `ticket` is the dial string only while the endpoint is running.
- `endpoint` is `running` after the platform connects and `not running` before
  the gateway restart or when the `iroh` package is missing.
- `relay` shows `default`, `disabled`, the redacted custom URL list (scheme,
  host, port, and path only), or `invalid configuration` when the setting cannot
  be parsed. Userinfo and query strings never appear.
- `allowlist entries` counts the effective union of the file and the environment.
- `hint` appears only while the endpoint is not running. It names the missing
  SDK and the re-enable step, an unreadable key and the state directory, or the
  platform flag and the outside-the-gateway restart, in that order.

### `/iroh_allow <client_node_id>`

Validates the value, persists it to `allowlist.json`, and confirms the
normalized id:

```text
/iroh_allow 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
→ allowlisted: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
```

With no argument or with extra arguments it returns
`usage: /iroh_allow <client_node_id>` and changes nothing. An invalid value
returns `invalid node id: <reason>` and changes nothing. If the allowlist file
cannot be written — including when it is a symlink, is not owned by the current
user, is group- or world-writable, or the state directory is insecure — it
returns `could not update allowlist: <error class>` and changes nothing.
Additions take effect for the next stream without a restart.

### `/iroh_revoke <client_node_id>`

Validates the value, removes it from `allowlist.json`, and confirms the
normalized id:

```text
/iroh_revoke 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
→ revoked: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
```

With no argument or with extra arguments it returns
`usage: /iroh_revoke <client_node_id>` and changes nothing. An invalid value
returns `invalid node id: <reason>` and changes nothing. If the allowlist file
cannot be written — including when it is a symlink, is not owned by the current
user, is group- or world-writable, or the state directory is insecure — it
returns `could not update allowlist: <error class>` and changes nothing. An id
that is not in the file is accepted without creating the file. Removal takes
effect for the node's next stream without a restart.

`HERMES_IROH_ALLOWLIST` is a separate authorization source that this command
does not edit. When the variable still contains the id, the response is
`still authorized: <normalized-id> (HERMES_IROH_ALLOWLIST)` instead of a
revocation confirmation.

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

## Releasing

Every push to `main` cuts a release automatically. The `release` workflow
derives the version part from the commits since the last tag, bumps
`pyproject.toml` and `plugin.yaml`, runs `python3 -m pytest -q`, commits
`chore: release vX.Y.Z`, creates an annotated tag, pushes `main` and the tag
atomically, and publishes the GitHub release. Clients see a new version on every
push.

| Commits since the last tag | Part |
| --- | --- |
| `feat!:` or a `BREAKING CHANGE:` footer | `major` |
| any `feat:` | `minor` |
| anything else | `patch` |

The same script runs locally and is the escape hatch when a release must be
forced:

```bash
scripts/release.sh --dry-run          # show the derived plan, change nothing
scripts/release.sh patch              # 1.0.0 -> 1.0.1, ignores the commits
scripts/release.sh minor --notes "..."
```

It refuses to run unless the working tree is clean, the branch is `main`, local
`main` matches `origin/main`, and the two metadata files already agree on the
current version. Override notes with `--notes "..."` or `--notes-file NOTES.md`;
use `--repo owner/name` when `origin` is not the GitHub repository that should
receive the release. The GitHub API call needs `GITHUB_TOKEN` in the environment
or in `.env` with permission to create releases; the workflow uses its built-in
token. A dry run does not need it.

The `release` workflow can also be started from the Actions tab with an explicit
part.

If the script stops after pushing, the commit and tag are already on the
remote; create the release for that tag in the GitHub UI.

## Tests

```bash
python3 -m pytest -q          # unit and loopback integration tests
python3 -m compileall -q .    # byte-compilation check
```

Tests that need the Iroh SDK are skipped when `iroh` is not importable. The
integration tests bind loopback endpoints with relaying disabled, so they need
neither network access nor a relay account.
