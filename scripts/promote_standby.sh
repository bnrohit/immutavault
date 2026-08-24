#!/usr/bin/env bash
set -euo pipefail

CONFIG=/etc/immutavault/immutavault.yml
ENVFILE=/etc/immutavault/immutavault.env
BACKUP=""
ACTIVATE_PORTAL=0
ACTIVATE_HEALTH=0
ACTIVATE_DR_WATCH=0

usage(){
  cat <<'USAGE'
Promote an Immutavault warm-standby controller from an online state DB backup.

Usage:
  sudo ./scripts/promote_standby.sh --state-backup /path/state-YYYYMMDDTHHMMSSZ.db [options]

Options:
  --config PATH          Config path (default /etc/immutavault/immutavault.yml)
  --env PATH             Environment file (default /etc/immutavault/immutavault.env)
  --activate-portal      Start the recovery portal after validation
  --activate-health      Start health timer after validation
  --activate-dr-watch    Enable/start DR watcher only after DR acceptance is complete
  -h, --help

This script never enables backup/retention/DR-sync jobs automatically. Keep only one
controller authoritative. Do not promote while the primary controller is still writing.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-backup) BACKUP="${2:?missing backup path}"; shift 2 ;;
    --config) CONFIG="${2:?missing config path}"; shift 2 ;;
    --env) ENVFILE="${2:?missing env path}"; shift 2 ;;
    --activate-portal) ACTIVATE_PORTAL=1; shift ;;
    --activate-health) ACTIVATE_HEALTH=1; shift ;;
    --activate-dr-watch) ACTIVATE_DR_WATCH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -n "$BACKUP" && -f "$BACKUP" ]] || { echo "--state-backup must reference an existing DB backup" >&2; exit 2; }
[[ -f "$CONFIG" && -f "$ENVFILE" ]] || { echo "Installed config/env not found" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }
[[ -x /usr/local/bin/immutavault ]] || { echo "/usr/local/bin/immutavault is not installed" >&2; exit 2; }

STATE_DB=$(CONFIG_PATH="$CONFIG" python3 - <<'PY'
import os, yaml
with open(os.environ['CONFIG_PATH'], encoding='utf-8') as f:
    data=yaml.safe_load(f) or {}
print((data.get('runtime') or {}).get('state_db', '/var/lib/immutavault/state.db'))
PY
)
[[ -n "$STATE_DB" ]] || { echo "runtime.state_db resolved empty" >&2; exit 2; }

# Verify source DB before touching the installed catalog.
BACKUP_PATH="$BACKUP" python3 - <<'PY'
import os, sqlite3
p=os.environ['BACKUP_PATH']
with sqlite3.connect(f'file:{p}?mode=ro', uri=True) as c:
    result=c.execute('PRAGMA integrity_check').fetchone()[0]
if result != 'ok':
    raise SystemExit(f'state backup integrity_check failed: {result}')
print('SQLite integrity_check: ok')
PY

# Stop all possible controller writers/readers while replacing state. Repository daemon is independent.
systemctl stop immutavault-portal.service immutavault-backup.timer immutavault-state-backup.timer \
  immutavault-health.timer immutavault-dr-watch.timer immutavault-dr-sync.timer 2>/dev/null || true

install -d -o immutavault -g immutavault -m 0750 "$(dirname "$STATE_DB")"
PREVIOUS="${STATE_DB}.pre-standby-$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -f "$STATE_DB" ]]; then
  cp --preserve=mode,timestamps "$STATE_DB" "$PREVIOUS"
fi
TMP="${STATE_DB}.promoting.$$"
install -o immutavault -g immutavault -m 0600 "$BACKUP" "$TMP"
mv -f "$TMP" "$STATE_DB"

set +e
runuser -u immutavault -- bash -c "set -a; source '$ENVFILE'; set +a; /usr/local/bin/immutavault --config '$CONFIG' audit-verify"
RC=$?
set -e
if [[ $RC -ne 0 ]]; then
  echo "Audit-chain verification failed after standby state promotion" >&2
  if [[ -f "$PREVIOUS" ]]; then
    install -o immutavault -g immutavault -m 0600 "$PREVIOUS" "$STATE_DB"
    echo "Previous standby state restored from $PREVIOUS" >&2
  fi
  exit 3
fi

# Verify configuration before starting anything.
runuser -u immutavault -- bash -c "set -a; source '$ENVFILE'; set +a; /usr/local/bin/immutavault --config '$CONFIG' dr-plan >/dev/null" || {
  echo "DR configuration validation failed; promoted state remains installed but no services were activated" >&2
  exit 4
}

[[ $ACTIVATE_PORTAL -eq 1 ]] && systemctl start immutavault-portal.service
[[ $ACTIVATE_HEALTH -eq 1 ]] && systemctl enable --now immutavault-health.timer
if [[ $ACTIVATE_DR_WATCH -eq 1 ]]; then
  echo "WARNING: enabling DR watcher; this is safe only after fencing/failover acceptance tests." >&2
  systemctl enable --now immutavault-dr-watch.timer
fi

cat <<EOF2
Warm-standby state promoted successfully.
State DB: $STATE_DB
Source:   $BACKUP
Previous: ${PREVIOUS:-none}

No backup, retention, verify, or DR-sync jobs were enabled automatically.
Keep the original controller fenced/stopped before making this standby authoritative.
EOF2
