## Iroh Channel — Next Steps

✅ Plugin installed. Access is **denied by default**: no client can connect until
its node id is allowed.

### 1. Enable the plugin and the platform

The plugin name is `iroh-channel`; the platform it registers is `iroh`. The
supported terminal path is the helper:

```bash
scripts/enable.sh
```

It enables the plugin, sets `gateway.platforms.iroh.enabled=true`, verifies the
result with `hermes plugins list`, and prints the restart command for the
detected host. Re-running it is safe, and it never runs `pip install`.

Manual equivalent:

```bash
hermes plugins enable iroh-channel
hermes config set gateway.platforms.iroh.enabled true
```

Do **not** run `hermes gateway restart` from this session. The agent runs inside
the gateway process, so Hermes refuses with `gateway_lifecycle_block`; that
refusal is expected, not an installation failure.

#### Terminal helper

`scripts/enable.sh` is the supported terminal path. It does not restart the
gateway by default. `--restart` performs the restart and is only for a human
running the helper from a normal shell outside the gateway — restarting from
inside kills the agent session that invoked it. `scripts/enable.sh --help`
shows the options.

### 2. Stop here: a human or host restarts the gateway

Platform plugins load only when the gateway starts, and the agent cannot restart
the process that hosts it. Hand this one action to a human or to the host:

| Host | Restart path |
| --- | --- |
| Local Hermes Desktop | Quit and reopen the app, or use its restart control |
| Web host with controls | Use the host panel's restart or stop/start control |
| CLI or systemd | From a shell outside the gateway: `systemctl --user restart hermes-gateway.service` (fallback `hermes gateway restart`) |
| Remote Desktop client | The client cannot restart a gateway on another host; the host operator or host panel must do it |

There is no terminal-free command the plugin can supply for a remote gateway.
Ask the host operator.

### 3. Check `/iroh_info`

```text
/iroh_info
```

```text
node id: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
ticket: endpointabc123...
endpoint: running
alpn: iroh-channel/1
relay: default
allowlist entries: 0
state dir: /home/you/.hermes/plugins/iroh-channel
```

`endpoint: running` confirms the restart took effect. While it says
`endpoint: not running`, `ticket: unavailable` and one `hint:` line name the
next step: install the SDK, fix an unreadable key, or enable the platform and
restart from outside the gateway.

### 4. Send the ticket to the client

Send the **ticket** to the client application. The **node id** is the value you
authorize in the next step, not the dial string.

### 5. Authorize the client

```text
/iroh_allow <client_node_id>
```

```text
allowlisted: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
```

The entry is persisted to `allowlist.json` and applies to the next connection
without a restart. For scripted setups, set the id up front instead:

```bash
hermes config set HERMES_IROH_ALLOWLIST "<64-hex-node-id>"
```

### Dependency check

The plugin loads without the Python SDK, but the platform stays unavailable and
starts no endpoint. `hermes plugins enable` is what prepares `iroh>=1.2,<2` from
`pyproject.toml`; it asks for consent before installing into the managed
environment. Verify with:

```bash
python3 -c "import iroh; print(iroh.__version__)"
```

If that fails, re-run `hermes plugins enable iroh-channel`, accept the dependency
prompt, and restart the gateway outside it. Do not `pip install iroh` into the
environment by hand: a manual install is not part of the managed dependency set
and is not retained when Hermes rebuilds that environment.

### Keep the node identity across reinstalls

Nothing needs configuring: `node.key` and `allowlist.json` live in the installed
plugin directory, so the plugin uses whichever `HERMES_HOME` and profile it was
installed into. `hermes plugins update` preserves the key.

`hermes plugins remove` deletes the plugin directory, which takes the key with it
— a reinstall then creates a new node id and every client must be authorized
again. Only if the identity must survive a reinstall, move the state out:

```bash
hermes config set HERMES_IROH_STATE_DIR "$HOME/.hermes/iroh-channel"
```

Move the existing `node.key` there first, or the plugin will create a new one.
The key is created once and never regenerated; a malformed key makes startup fail
instead of silently changing the identity.

---

### Troubleshooting

| Issue | Solution |
|-------|----------|
| `gateway_lifecycle_block` when restarting | Expected: the agent is inside the gateway. Do not retry from the session; use the human or host restart step above |
| `endpoint: not running`, `ticket: unavailable` | The platform flag is off or the gateway has not restarted. Run `scripts/enable.sh`, then restart outside the gateway |
| `hint:` says the SDK is not importable | The `iroh` package is missing: `hermes plugins enable iroh-channel`, accept the dependency prompt, and restart |
| Client connects, then is immediately closed | Its node id is not allowlisted: run `/iroh_allow <node_id>` and reconnect |
| Clients on another network cannot connect | Relaying is disabled (`HERMES_IROH_RELAY=disabled`) and no direct address is reachable |
| `relay: invalid configuration` | `HERMES_IROH_RELAY` needs `disabled`/`off`/`none` or absolute http(s) URLs |
