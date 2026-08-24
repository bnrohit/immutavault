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
  pass "/dev/fuse available"
else
  fail "/dev/fuse is unavailable"
fi

if id immutavault >/dev/null 2>&1; then
  if runuser -u immutavault -- test -r /dev/fuse 2>/dev/null && runuser -u immutavault -- test -w /dev/fuse 2>/dev/null; then
    pass "immutavault can access /dev/fuse"
  else
    fail "immutavault cannot read/write /dev/fuse"
  fi
fi

exit "$FAIL"
