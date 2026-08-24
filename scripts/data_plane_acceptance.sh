#!/usr/bin/env bash
set -euo pipefail

CONFIG="${IMMUTAVAULT_CONFIG:-/etc/immutavault/immutavault.yml}"
ENVFILE="${IMMUTAVAULT_ENV:-/etc/immutavault/immutavault.env}"
KEEP_TEST_POINT=0

usage(){ cat <<'USAGE'
Run a live acceptance test against the installed primary append-only repository.
Run this before enabling production schedules or during a controlled maintenance window.

Usage:
  sudo ./scripts/data_plane_acceptance.sh [--config PATH] [--env PATH] [--keep-test-point]

Tests:
  * TLS/authenticated restic connectivity
  * real encrypted backup through REST writer
  * real restore and SHA-256 comparison
  * append-only writer is unable to forget/delete the recovery point
  * root local repository path can remove the disposable test snapshot metadata
USAGE
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="${2:?missing config}"; shift 2 ;;
    --env) ENVFILE="${2:?missing env}"; shift 2 ;;
    --keep-test-point) KEEP_TEST_POINT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
for c in restic python3 sha256sum; do command -v "$c" >/dev/null || { echo "$c is required" >&2; exit 1; }; done
[[ -f "$CONFIG" && -f "$ENVFILE" ]] || { echo "Config/env file missing" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$ENVFILE"
set +a
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD is required}"
: "${REST_SERVER_USER:?REST_SERVER_USER is required}"
: "${REST_SERVER_PASSWORD:?REST_SERVER_PASSWORD is required}"

readarray -t VALUES < <(CONFIG_PATH="$CONFIG" python3 - <<'PY'
import os, yaml
with open(os.environ['CONFIG_PATH'], encoding='utf-8') as f:
    d=yaml.safe_load(f) or {}
r=d.get('repository') or {}
print(r.get('url',''))
print(r.get('local_path',''))
print(r.get('cacert',''))
PY
)
REST_URL="${VALUES[0]:-}"
LOCAL_REPO="${VALUES[1]:-}"
CACERT="${VALUES[2]:-}"
[[ "$REST_URL" == rest:* ]] || { echo "repository.url must be a rest: URL" >&2; exit 1; }
[[ -d "$LOCAL_REPO" ]] || { echo "repository.local_path is not accessible: $LOCAL_REPO" >&2; exit 1; }
[[ -z "$CACERT" || -f "$CACERT" ]] || { echo "repository CA file missing: $CACERT" >&2; exit 1; }

export RESTIC_REPOSITORY="$REST_URL"
export RESTIC_REST_USERNAME="$REST_SERVER_USER"
export RESTIC_REST_PASSWORD="$REST_SERVER_PASSWORD"
[[ -n "$CACERT" ]] && export RESTIC_CACERT="$CACERT"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
STAMP=$(date -u +%Y%m%dT%H%M%SZ)-$$
SRC="$TMP/source"
DST="$TMP/restore"
mkdir -p "$SRC" "$DST"
FILE="immutavault-acceptance-${STAMP}.txt"
printf 'Immutavault live data-plane acceptance %s\n%s\n' "$STAMP" "$(openssl rand -hex 32)" > "$SRC/$FILE"
EXPECTED=$(sha256sum "$SRC/$FILE" | awk '{print $1}')

echo '[1/5] Checking authenticated repository connectivity...'
restic snapshots --json >/dev/null

echo '[2/5] Writing a real encrypted test snapshot through append-only REST...'
OUT=$(restic backup "$SRC" --json --tag immutavault-acceptance --tag "$STAMP")
SNAP=$(printf '%s\n' "$OUT" | python3 -c 'import json,sys; ids=[]
for line in sys.stdin:
    try: e=json.loads(line)
    except Exception: continue
    if e.get("message_type")=="summary" and e.get("snapshot_id"): ids.append(e["snapshot_id"])
print(ids[-1] if ids else "")')
[[ -n "$SNAP" ]] || { echo 'restic did not return a snapshot ID' >&2; exit 1; }
echo "Snapshot: $SNAP"

echo '[3/5] Restoring the exact snapshot and comparing SHA-256...'
restic restore "$SNAP" --target "$DST" >/dev/null
RESTORED=$(find "$DST" -type f -name "$FILE" -print -quit)
[[ -f "$RESTORED" ]] || { echo 'restored acceptance file was not found' >&2; exit 1; }
ACTUAL=$(sha256sum "$RESTORED" | awk '{print $1}')
[[ "$ACTUAL" == "$EXPECTED" ]] || { echo "restore digest mismatch: $ACTUAL != $EXPECTED" >&2; exit 1; }

echo '[4/5] Proving network writer cannot delete the snapshot...'
set +e
DELETE_OUTPUT=$(restic forget "$SNAP" 2>&1)
DELETE_RC=$?
set -e
if [[ $DELETE_RC -eq 0 ]]; then
  echo 'SECURITY FAILURE: append-only network writer successfully forgot/deleted a snapshot' >&2
  exit 10
fi
echo "Append-only delete rejected as expected (rc=$DELETE_RC)."

# Confirm it still exists after the rejected delete.
restic snapshots "$SNAP" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d else 1)'

echo '[5/5] Testing privileged local maintenance visibility...'
RESTIC_REPOSITORY="$LOCAL_REPO" RESTIC_PASSWORD="$RESTIC_PASSWORD" restic snapshots "$SNAP" --json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d else 1)'

if [[ $KEEP_TEST_POINT -eq 0 ]]; then
  # Remove only snapshot metadata locally. Do not prune here; normal root-only
  # retention/prune will reclaim unreferenced packs under its regular lock/window.
  RESTIC_REPOSITORY="$LOCAL_REPO" RESTIC_PASSWORD="$RESTIC_PASSWORD" restic forget "$SNAP" >/dev/null
  echo 'Disposable acceptance snapshot metadata removed through the privileged local path.'
else
  echo 'Acceptance snapshot retained by operator request.'
fi

printf '\nLIVE DATA-PLANE ACCEPTANCE PASSED\nSnapshot tested: %s\nDigest: %s\n' "$SNAP" "$EXPECTED"
