#!/usr/bin/env bash
set -euo pipefail

# Disposable end-to-end repository test used by CI. It proves that the exact
# upstream restic client can initialize, back up to, restore from, and verify an
# authenticated TLS append-only rest-server repository, and that the network
# writer cannot forget/delete its snapshot.

for c in restic rest-server openssl htpasswd curl sha256sum python3; do
  command -v "$c" >/dev/null 2>&1 || { echo "missing required command: $c" >&2; exit 1; }
done

TMP=$(mktemp -d)
PORT="${IMMUTAVAULT_CI_REST_PORT:-18443}"
USER=ci-writer
PASS='ci-transport-password'
REPO_PASS='ci-repository-encryption-key'
PID=''
cleanup() {
  if [[ -n "$PID" ]]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

mkdir -p "$TMP/repository" "$TMP/src" "$TMP/restore"
printf 'immutavault-live-data-plane\n' > "$TMP/src/payload.txt"
SOURCE_SHA=$(sha256sum "$TMP/src/payload.txt" | awk '{print $1}')

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -keyout "$TMP/server.key" -out "$TMP/server.crt" \
  -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost' >/dev/null 2>&1
htpasswd -Bbc "$TMP/htpasswd" "$USER" "$PASS" >/dev/null

rest-server \
  --path "$TMP/repository" \
  --append-only \
  --htpasswd-file "$TMP/htpasswd" \
  --listen ":$PORT" \
  --tls --tls-cert "$TMP/server.crt" --tls-key "$TMP/server.key" --tls-min-ver 1.3 \
  >"$TMP/rest-server.log" 2>&1 &
PID=$!

ready=0
for _ in $(seq 1 40); do
  if ! kill -0 "$PID" 2>/dev/null; then
    cat "$TMP/rest-server.log" >&2
    echo 'rest-server exited during startup' >&2
    exit 1
  fi
  # curl succeeds on transport/TLS even if the root path returns a non-2xx HTTP response.
  if curl --silent --show-error --cacert "$TMP/server.crt" --user "$USER:$PASS" \
      "https://localhost:$PORT/" -o /dev/null; then
    ready=1
    break
  fi
  sleep 0.25
done
[[ $ready -eq 1 ]] || { cat "$TMP/rest-server.log" >&2; echo 'rest-server did not become reachable' >&2; exit 1; }

export RESTIC_REPOSITORY="rest:https://localhost:$PORT/ci-repo/"
export RESTIC_PASSWORD="$REPO_PASS"
export RESTIC_REST_USERNAME="$USER"
export RESTIC_REST_PASSWORD="$PASS"
export RESTIC_CACERT="$TMP/server.crt"

restic init >/dev/null
restic backup "$TMP/src" --tag immutavault-ci >/dev/null
SNAP=$(restic snapshots --tag immutavault-ci --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d; print(d[-1]["id"])')
restic check >/dev/null
restic restore "$SNAP" --target "$TMP/restore" >/dev/null
RESTORED=$(find "$TMP/restore" -type f -name payload.txt -print -quit)
[[ -n "$RESTORED" ]] || { echo 'restored payload not found' >&2; exit 1; }
RESTORED_SHA=$(sha256sum "$RESTORED" | awk '{print $1}')
[[ "$SOURCE_SHA" == "$RESTORED_SHA" ]] || { echo 'restore digest mismatch' >&2; exit 1; }

set +e
restic forget "$SNAP" >"$TMP/forget.out" 2>"$TMP/forget.err"
FORGET_RC=$?
set -e
if [[ $FORGET_RC -eq 0 ]]; then
  cat "$TMP/forget.out" "$TMP/forget.err" >&2
  echo 'SECURITY FAILURE: append-only network writer successfully forgot/deleted a snapshot' >&2
  exit 1
fi

# The protected snapshot must still be readable after the rejected delete attempt.
restic snapshots "$SNAP" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d' >/dev/null
printf 'PASS: authenticated TLS append-only backup/restore/digest/delete-resistance data plane\n'
