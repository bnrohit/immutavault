#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }
BIN=$(command -v immutavault-setup || true)
[[ -n "$BIN" && -x "$BIN" ]] || BIN=/opt/immutavault/current/bin/immutavault-setup
[[ -x "$BIN" ]] || { echo "immutavault-setup is not installed. Install Immutavault v0.6.0+ first." >&2; exit 2; }
CONFIG=${IMMUTAVAULT_CONFIG:-/etc/immutavault/immutavault.yml}
ENVFILE=${IMMUTAVAULT_ENV:-/etc/immutavault/immutavault.env}
LISTEN=${IMMUTAVAULT_SETUP_LISTEN:-0.0.0.0}
PORT=${IMMUTAVAULT_SETUP_PORT:-8788}
CERT=${IMMUTAVAULT_SETUP_TLS_CERT:-/etc/immutavault/tls/portal.crt}
KEY=${IMMUTAVAULT_SETUP_TLS_KEY:-/etc/immutavault/tls/portal.key}
[[ -f "$CONFIG" && -f "$ENVFILE" ]] || { echo "Missing $CONFIG or $ENVFILE; run the installer first." >&2; exit 3; }
if [[ "$LISTEN" != "127.0.0.1" && "$LISTEN" != "::1" && "$LISTEN" != "localhost" ]]; then
  [[ -f "$CERT" && -f "$KEY" ]] || { echo "Remote guided setup requires TLS files: $CERT and $KEY" >&2; exit 4; }
  exec "$BIN" --config "$CONFIG" --env "$ENVFILE" --listen "$LISTEN" --port "$PORT" --tls-cert "$CERT" --tls-key "$KEY"
fi
exec "$BIN" --config "$CONFIG" --env "$ENVFILE" --listen "$LISTEN" --port "$PORT"
