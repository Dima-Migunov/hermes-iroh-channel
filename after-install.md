## Iroh Channel — Next Steps

✅ Plugin installed. Access is **denied by default**: no client can connect until
its node id is allowed.

### 1. Enable the plugin and the platform

The plugin name is `iroh-channel`; the platform it registers is `iroh`.

```bash
hermes plugins enable iroh-channel
hermes config set gateway.platforms.iroh.enabled true
hermes gateway restart
```

### 2. Check the `iroh` dependency

The plugin loads without the Python SDK, but the platform stays unavailable and
starts no endpoint. `hermes plugins enable` is what prepares `iroh>=1.2,<2` from
`pyproject.toml`; it asks for consent before installing into the managed
environment. Verify with:

```bash
python3 -c "import iroh; print(iroh.__version__)"
```

If that fails, re-run `hermes plugins enable iroh-channel` and accept the
dependency prompt. Do not `pip install iroh` into the environment by hand: a
manual install is not part of the managed dependency set and is not retained
when Hermes rebuilds that environment.

### 3. Get the node id and ticket

```text
/iroh_info
```

```text
node id: 3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29
ticket: endpointabc123...
alpn: iroh-channel/1
relay: default
allowlist entries: 0
state dir: /home/you/.hermes/plugins/iroh-channel
```

Send the **ticket** to the client application. The **node id** is the value you
authorize in the next step.

### 4. Authorize the client

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

### 5. Keep the node identity across reinstalls

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
| Platform never starts | Enable it: `hermes config set gateway.platforms.iroh.enabled true`, then `hermes gateway restart` |
| `check_fn` reports unavailable | The `iroh` package is not importable: re-run `hermes plugins enable iroh-channel` and accept the dependency prompt |
| Client connects, then is immediately closed | Its node id is not allowlisted: run `/iroh_allow <node_id>` and reconnect |
| `ticket: unavailable` | The endpoint is not bound — the platform is disabled or the SDK is missing |
| Clients on another network cannot connect | Relaying is disabled (`HERMES_IROH_RELAY=disabled`) and no direct address is reachable |
| `relay: invalid configuration` | `HERMES_IROH_RELAY` needs `disabled`/`off`/`none` or absolute http(s) URLs |
