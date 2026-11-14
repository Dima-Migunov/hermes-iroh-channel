#!/usr/bin/env bash
# Enable the iroh-channel plugin and platform, then print the restart step.
set -euo pipefail

plugin="iroh-channel"
platform_switch="gateway.platforms.iroh.enabled"

usage() {
  cat <<'EOF'
Usage: scripts/enable.sh [--restart]

Enable the iroh-channel plugin and set gateway.platforms.iroh.enabled=true.
The script is idempotent and prints the gateway restart command for the
detected host. It does not restart the gateway unless --restart is given.

Run it from a normal shell. Restarting from inside the gateway kills the
agent session that invoked the script; --restart is only for a human running
this outside the gateway.

Options:
  --restart    Restart the gateway after enabling (outside-the-gateway use only)
  -h, --help   Show this help and exit
EOF
}

restart=false
for arg in "$@"; do
  case "$arg" in
    --restart) restart=true ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "error: unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v hermes >/dev/null 2>&1; then
  echo "error: the Hermes CLI (hermes) is required but was not found on PATH." >&2
  echo "Install Hermes or add it to PATH, then re-run scripts/enable.sh." >&2
  exit 127
fi

echo "Enabling plugin: $plugin"
hermes plugins enable "$plugin"

echo "Enabling platform: $platform_switch=true"
hermes config set "$platform_switch" true

echo "Verifying with 'hermes plugins list'..."
if verification="$(hermes plugins list 2>&1)" && grep -q "$plugin" <<<"$verification"; then
  echo "Verified: $plugin appears in the plugin list."
else
  echo "warning: could not verify '$plugin' with 'hermes plugins list'." >&2
  echo "warning: run these manually if needed:" >&2
  echo "  hermes plugins enable $plugin" >&2
  echo "  hermes config set $platform_switch true" >&2
fi

if command -v systemctl >/dev/null 2>&1 &&
  systemctl --user cat hermes-gateway.service >/dev/null 2>&1; then
  restart_command=(systemctl --user restart hermes-gateway.service)
else
  restart_command=(hermes gateway restart)
fi

printf '\nEnabled %s. The endpoint starts only after the gateway restarts.\n' "$plugin"
printf 'Restart command for this host:\n\n  %s\n\n' "${restart_command[*]}"

if [ "$restart" = true ]; then
  echo "Restarting the gateway now (--restart)."
  "${restart_command[@]}"
  echo "Gateway restart requested."
else
  echo "No restart was performed. Run the command above from a normal shell outside the gateway."
fi
