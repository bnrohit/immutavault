#!/usr/bin/env bash
set -euo pipefail

BIN="${1:-${REST_SERVER_BIN:-rest-server}}"
MIN_VERSION="${IMMUTAVAULT_MIN_REST_SERVER_VERSION:-0.14.0}"

fail(){ echo "rest-server compatibility check failed: $*" >&2; exit 1; }

if [[ "$BIN" == */* ]]; then
  [[ -x "$BIN" ]] || fail "binary is not executable: $BIN"
else
  BIN_PATH=$(command -v "$BIN" 2>/dev/null || true)
  [[ -n "$BIN_PATH" ]] || fail "rest-server is not installed or not in PATH"
  BIN="$BIN_PATH"
fi

HELP=$($BIN --help 2>&1) || fail "unable to read --help from $BIN"
VERSION_OUT=$($BIN --version 2>&1 || true)
VERSION=$(printf '%s\n' "$VERSION_OUT" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)
[[ -n "$VERSION" ]] || fail "unable to determine version from: $VERSION_OUT"

# v0.14.0 introduced the hardened TLS defaults used by Immutavault. Require it
# or newer, and also verify the exact security/runtime flags we depend on.
if [[ "$(printf '%s\n%s\n' "$MIN_VERSION" "$VERSION" | sort -V | head -n1)" != "$MIN_VERSION" ]]; then
  fail "version $VERSION is older than required $MIN_VERSION"
fi

for flag in --append-only --tls --tls-cert --tls-key --tls-min-ver --htpasswd-file; do
  grep -Fq -- "$flag" <<<"$HELP" || fail "required capability is missing: $flag"
done

# Hardened TLS requires the minimum-version selector and support for TLS 1.3.
grep -Fq -- '1.2|1.3' <<<"$HELP" || fail "TLS minimum-version choices do not advertise 1.2/1.3 support"

printf 'Compatible rest-server detected: %s (required >= %s)\n' "$VERSION" "$MIN_VERSION"
