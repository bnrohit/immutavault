#!/usr/bin/env bash
set -uo pipefail
FAIL=0
WARN=0
pass(){ printf '[PASS] %s\n' "$*"; }
warn(){ printf '[WARN] %s\n' "$*"; WARN=$((WARN+1)); }
fail(){ printf '[FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }

[[ $(uname -s) == Linux ]] && pass "Linux detected" || fail "Linux is required"
case "$(uname -m)" in x86_64|amd64|aarch64) pass "Supported architecture $(uname -m)";; *) fail "Unsupported architecture $(uname -m)";; esac

if command -v python3 >/dev/null; then
  PY=$(python3 - <<'PY'
import sys
print('.'.join(map(str,sys.version_info[:2])))
PY
)
  python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3,10) else 1)
PY
  [[ $? -eq 0 ]] && pass "Python $PY" || fail "Python 3.10+ required; found $PY"
else fail "python3 missing"; fi

for c in restic ssh openssl curl; do command -v "$c" >/dev/null && pass "$c available" || fail "$c missing"; done
if command -v rest-server >/dev/null; then
  if "$(dirname "$0")/check_rest_server.sh" "$(command -v rest-server)" >/dev/null 2>&1; then
    pass "rest-server available and compatible"
  else
    fail "rest-server is installed but incompatible; require v0.14.0+ with append-only and hardened TLS capabilities"
  fi
else
  warn "rest-server missing (the all/repository installer can install the pinned/SHA-verified release)"
fi
command -v smartctl >/dev/null && pass "SMART tooling available" || warn "smartctl missing"

MEM_KIB=$(awk '/MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
if [[ ${MEM_KIB:-0} -ge 8388608 ]]; then pass "Memory >= 8 GiB"; else warn "Memory below 8 GiB"; fi
CPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)
if [[ ${CPU:-0} -ge 4 ]]; then pass "CPU threads >= 4"; else warn "Fewer than 4 CPU threads"; fi

if [[ -f /etc/immutavault/immutavault.yml && -f /etc/immutavault/immutavault.env && -x /usr/local/bin/immutavault ]]; then
  if runuser -u immutavault -- bash -c 'set -a; source /etc/immutavault/immutavault.env; set +a; /usr/local/bin/immutavault --config /etc/immutavault/immutavault.yml doctor' >/tmp/immutavault-doctor.$$ 2>&1; then
    pass "Immutavault doctor"
  else
    warn "Immutavault doctor reports unresolved configuration; see below"
    cat /tmp/immutavault-doctor.$$
  fi
  rm -f /tmp/immutavault-doctor.$$
else
  warn "Immutavault is not fully installed/configured yet; application doctor skipped"
fi

printf '\nSummary: %d failure(s), %d warning(s)\n' "$FAIL" "$WARN"
[[ $FAIL -eq 0 ]]
