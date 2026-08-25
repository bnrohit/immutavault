#!/usr/bin/env bash
set -uo pipefail
MIN_VERSION="${1:-2.12.0}"
FAIL=0
WARN=0
pass(){ printf '[PASS] %s\n' "$*"; }
warn(){ printf '[WARN] %s\n' "$*"; WARN=$((WARN+1)); }
fail(){ printf '[FAIL] %s\n' "$*" >&2; FAIL=$((FAIL+1)); }

for cmd in virt-v2v virt-v2v-inspector qemu-img; do
  command -v "$cmd" >/dev/null 2>&1 && pass "$cmd available" || fail "$cmd missing"
done

if command -v virt-v2v >/dev/null 2>&1; then
  ACTUAL=$(virt-v2v --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 || true)
  if [[ -z "$ACTUAL" ]]; then
    fail "unable to parse virt-v2v version"
  elif printf '%s\n%s\n' "$MIN_VERSION" "$ACTUAL" | sort -V -C; then
    pass "virt-v2v $ACTUAL >= certified minimum $MIN_VERSION"
  else
    fail "virt-v2v $ACTUAL is below certified minimum $MIN_VERSION"
  fi

  FEATURES=$(virt-v2v --machine-readable 2>/dev/null || true)
  for feature in input:ova output:local convert:linux convert:windows; do
    if grep -Fxq "$feature" <<<"$FEATURES"; then
      pass "virt-v2v capability $feature"
    else
      fail "virt-v2v missing capability $feature"
    fi
  done
fi

if command -v virt-v2v-inspector >/dev/null 2>&1; then
  virt-v2v-inspector --help >/dev/null 2>&1 && pass "virt-v2v-inspector capability probe" || fail "virt-v2v-inspector capability probe failed"
fi
if command -v qemu-img >/dev/null 2>&1; then
  qemu-img --version >/dev/null 2>&1 && pass "qemu-img capability probe" || fail "qemu-img capability probe failed"
fi

if [[ -n "${VIRTIO_WIN:-}" && -e "${VIRTIO_WIN}" ]]; then
  pass "Windows VirtIO driver source available via VIRTIO_WIN"
elif [[ -e /usr/share/virtio-win || -e /usr/share/virtio-win/virtio-win.iso ]]; then
  pass "Windows VirtIO driver source available"
else
  warn "Windows V2V will remain blocked until signed VirtIO drivers are installed or VIRTIO_WIN is configured"
fi

printf '\nV2V capability summary: %d failure(s), %d warning(s)\n' "$FAIL" "$WARN"
[[ $FAIL -eq 0 ]]
