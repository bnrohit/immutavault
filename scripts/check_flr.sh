#!/usr/bin/env bash
set -euo pipefail

FAIL=0
pass(){ printf '[PASS] %s\n' "$*"; }
fail(){ printf '[FAIL] %s\n' "$*" >&2; FAIL=$((FAIL+1)); }

for cmd in restic guestmount guestunmount; do
  command -v "$cmd" >/dev/null 2>&1 && pass "$cmd available" || fail "$cmd missing"
done

if command -v fusermount3 >/dev/null 2>&1; then
  pass "fusermount3 available"
elif command -v fusermount >/dev/null 2>&1; then
  pass "fusermount available"
else
  fail "fusermount3/fusermount missing"
fi

if [[ -c /dev/fuse ]]; then
  pass "/dev/fuse available for the FLR broker"
else
  fail "/dev/fuse is unavailable"
fi

# v1.0.1 privilege separation: the network-facing immutavault user should not
# need direct FUSE access. If services are active, require broker/socket health.
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet immutavault-flr.service 2>/dev/null; then
    [[ -S /run/immutavault/flr.sock ]] && pass "FLR broker socket active" || fail "FLR broker is active but socket is missing"
  elif systemctl is-active --quiet immutavault-portal.service 2>/dev/null; then
    fail "portal is active but FLR broker is not active"
  fi
fi

exit "$FAIL"
