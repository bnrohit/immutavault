#!/usr/bin/env bash
set -euo pipefail

BIN="${1:-${RESTIC_BIN:-restic}}"
MIN_VERSION="${IMMUTAVAULT_MIN_RESTIC_VERSION:-0.19.1}"

fail(){ echo "restic compatibility check failed: $*" >&2; exit 1; }

if [[ "$BIN" == */* ]]; then
  [[ -x "$BIN" ]] || fail "binary is not executable: $BIN"
else
  BIN_PATH=$(command -v "$BIN" 2>/dev/null || true)
  [[ -n "$BIN_PATH" ]] || fail "restic is not installed or not in PATH"
  BIN="$BIN_PATH"
fi

VERSION_OUT=$($BIN version 2>&1) || fail "unable to execute $BIN version"
VERSION=$(printf '%s\n' "$VERSION_OUT" | grep -Eo 'restic [0-9]+\.[0-9]+\.[0-9]+' | head -n1 | awk '{print $2}' || true)
[[ -n "$VERSION" ]] || fail "unable to determine version from: $VERSION_OUT"

if [[ "$(printf '%s\n%s\n' "$MIN_VERSION" "$VERSION" | sort -V | head -n1)" != "$MIN_VERSION" ]]; then
  fail "version $VERSION is older than required $MIN_VERSION"
fi

# Verify command capabilities Immutavault relies on, not just the version string.
for probe in 'backup --help' 'copy --help' 'forget --help' 'prune --help' 'restore --help' 'check --help'; do
  # shellcheck disable=SC2086
  $BIN $probe >/dev/null 2>&1 || fail "required command is unavailable: restic $probe"
done

printf 'Compatible restic detected: %s (required >= %s)\n' "$VERSION" "$MIN_VERSION"
