#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

pass(){ printf '[PASS] %s\n' "$*"; }
fail(){ printf '[FAIL] %s\n' "$*" >&2; exit 1; }

printf 'Immutavault release validation\n==============================\n'
VERSION=$(tr -d '[:space:]' < VERSION)
[[ -n "$VERSION" ]] || fail 'VERSION is empty'

if grep -RInE '^(<<<<<<<|=======|>>>>>>>)' --exclude-dir=.git --exclude-dir=.pytest_cache --exclude-dir=dist --exclude-dir=build . >/dev/null; then
  fail 'merge-conflict markers found'
fi
pass 'no merge-conflict markers'

python3 - <<'PY'
from pathlib import Path
import re
version = Path('VERSION').read_text().strip()
init = Path('src/immutavault/__init__.py').read_text()
pyproject = Path('pyproject.toml').read_text()
readme = Path('README.md').read_text()
vmware_doc = Path('docs/VMWARE_BACKUP.md').read_text()
incremental_example = Path('config/vmware-incremental.example.yml').read_text()
flr_doc = Path('docs/FILE_LEVEL_RECOVERY.md').read_text()
flr_code = Path('src/immutavault/flr.py').read_text()
config_example = Path('config/immutavault.example.yml').read_text()

assert f'__version__ = "{version}"' in init
assert re.search(r'^version = "' + re.escape(version) + r'"$', pyproject, re.M)

# Release-facing operator documentation must never lag VERSION again. A stale
# README can send an operator to an older tag or to the wrong VMware transport.
assert readme.startswith(f'# Immutavault v{version}\n'), 'README release heading is stale'
assert f'git checkout v{version}' in readme, 'README install command is not pinned to current VERSION'
assert 'incremental_strict: true' in readme
assert 'incremental_fallback: false' in readme
assert 'Broadcom VDDK' in readme and 'not bundled' in readme.lower()

# The canonical VMware runbook and example must preserve the strict production
# contract introduced in v0.7.1.
for token in (
    'incremental_strict: true',
    'incremental_fallback: false',
    'fail closed',
    'fallback_safe',
):
    assert token in vmware_doc, f'VMware runbook missing required policy token: {token}'
assert 'mode: "vddk"' in incremental_example, 'VMware example is not pinned to native vddk mode'
assert 'incremental_strict: true' in incremental_example
assert 'incremental_fallback: false' in incremental_example
assert 'application_consistency_strict: true' in incremental_example
assert 'flr:' in config_example and 'mount_root: "/srv/immutavault/flr"' in config_example
for token in ('restic mount', 'guestmount --ro', 'path traversal', 'symlink'):
    assert token in flr_doc, f'FLR runbook missing safety token: {token}'
for token in ('--no-lock', 'guestmount', 'does not follow guest symlinks', 'max_download_bytes'):
    assert token in flr_code, f'FLR implementation missing required safety token: {token}'
assert 'file-level recovery' in readme.lower(), 'README does not expose v0.8 FLR'
assert 'application_consistency_strict: true' in readme, 'README omits strict application-consistency policy'
print(version)
PY
pass "version/release documentation consistent: $VERSION"

# Never ship runtime secrets, TLS private keys, catalogs, or backup payloads.
# `git archive` source packages intentionally have no .git directory, so support both
# a normal clone and the release tarball used for offline validation.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  FILE_LIST=$(git ls-files)
else
  FILE_LIST=$(find . -type f -not -path './.pytest_cache/*' -not -path './dist/*' -not -path './build/*' -printf '%P\n')
fi
if printf '%s\n' "$FILE_LIST" | grep -E '(^|/)(\.env|.*\.key|state.*\.db)$|(^|/)(staging|output)/' >/dev/null; then
  fail 'tracked/shipped runtime secret/state/staging file detected'
fi
pass 'no runtime secrets/state/staging shipped'

python3 -m compileall -q src tests
pass 'Python source/tests compile'

for f in scripts/*.sh; do bash -n "$f"; done
pass 'shell scripts parse'

pytest -q
pass 'unit/security/DR suite passed'

PYTHONPATH=src python3 - <<'PY'
from immutavault.config import load_config
cfg = load_config('config/immutavault.example.yml')
assert cfg.runtime.command_timeout_seconds > 0
assert cfg.repository.retention.keep_within_days >= 1
print('config valid')
PY
pass 'example configuration validates'

PYTHONPATH=src python3 -m immutavault.cli --help >/dev/null
pass 'source-tree CLI smoke test'

if command -v systemd-analyze >/dev/null 2>&1; then
  mkdir -p "$TMP/systemd"
  cp systemd/* "$TMP/systemd/"
  # Verify unit grammar independently of whether this CI/sandbox has the real binaries installed.
  sed -i 's#/usr/local/bin/immutavault#/bin/true#g; s#/usr/bin/env rest-server#/bin/true#g' "$TMP"/systemd/*.service
  if ! systemd-analyze verify "$TMP"/systemd/*.service "$TMP"/systemd/*.timer >"$TMP/systemd.out" 2>&1; then
    cat "$TMP/systemd.out" >&2
    fail 'systemd unit verification failed'
  fi
  pass 'systemd units verify'
fi

rm -rf build dist src/*.egg-info
# Prefer an offline build when the active interpreter already has the declared
# setuptools backend. Fresh CI runners may not, even after an editable install
# that used an isolated build environment, so fall back to normal PEP 517 build
# isolation instead of reporting a false release failure.
if python3 -c 'import setuptools.build_meta' >/dev/null 2>&1; then
  python3 -m pip wheel . --no-deps --no-build-isolation -w dist >/dev/null
else
  python3 -m pip wheel . --no-deps -w dist >/dev/null
fi
WHEEL=$(find dist -maxdepth 1 -name 'immutavault-*.whl' -print -quit)
[[ -f "$WHEEL" ]] || fail 'wheel was not produced'
pass "wheel built: $(basename "$WHEEL")"

mkdir -p "$TMP/site"
python3 -m pip install --no-deps --target "$TMP/site" "$WHEEL" >/dev/null
PYTHONPATH="$TMP/site" python3 - <<PY
import immutavault
from immutavault.config import load_config
assert immutavault.__version__ == '$VERSION'
load_config('config/immutavault.example.yml')
print(immutavault.__version__)
PY
pass 'built wheel imports and loads production configuration'

# Static install contract: the all-in-one role must install verified data-plane
# binaries or fail closed.
grep -q './scripts/install_restic.sh' scripts/install.sh || fail 'all-in-one installer lacks verified restic path'
grep -q 'sha256sum --check --status' scripts/install_restic.sh || fail 'restic installer lacks checksum verification'
grep -q 'check_restic.sh' scripts/preflight.sh || fail 'preflight lacks restic compatibility gate'
grep -q './scripts/install_rest_server.sh' scripts/install.sh || fail 'all-in-one installer lacks verified rest-server path'
grep -q 'sha256sum --check --status' scripts/install_rest_server.sh || fail 'rest-server installer lacks checksum verification'
grep -q 'check_rest_server.sh' scripts/install_repository.sh || fail 'repository installer lacks rest-server compatibility gate'
grep -q -- '--append-only' scripts/check_rest_server.sh || fail 'rest-server compatibility gate lacks append-only check'
grep -q -- '--tls-min-ver' scripts/check_rest_server.sh || fail 'rest-server compatibility gate lacks hardened TLS check'
! grep -q 'does NOT download rest-server binaries' scripts/install_appliance.sh || fail 'appliance documentation still contains obsolete rest-server download statement'
grep -q 'EnvironmentFile=/etc/immutavault/repository.env' systemd/immutavault-rest-server.service || fail 'rest-server still receives controller environment'
grep -q 'libguestfs-tools' scripts/install_appliance.sh || fail 'appliance installer lacks FLR libguestfs dependency'
grep -q 'fuse3' scripts/install_appliance.sh || fail 'appliance installer lacks FUSE3 dependency'
grep -q 'guestmount' scripts/check_flr.sh || fail 'FLR prerequisite checker is missing guestmount gate'
grep -q 'NoNewPrivileges=false' systemd/immutavault-portal.service || fail 'portal service cannot use packaged FUSE mount helper'
pass 'restic/rest-server installs are pinned, checksummed, capability-gated, and privilege-separated'
pass 'FLR FUSE/libguestfs installation contract is present'

printf '\nALL RELEASE CHECKS PASSED for Immutavault %s\n' "$VERSION"
