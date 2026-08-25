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
flr_broker = Path('src/immutavault/flr_broker.py').read_text()
config_example = Path('config/immutavault.example.yml').read_text()
v2v_doc = Path('docs/CERTIFIED_V2V.md').read_text()
v2v_config = Path('config/enterprise-v1.0.example.yml').read_text()
v2v_code = Path('src/immutavault/v2v.py').read_text()
v2v_cert = Path('src/immutavault/v2v_cert.py').read_text()
v2v_engine = Path('src/immutavault/v2v_engine.py').read_text()
v2v_cli = Path('src/immutavault/cli_v10.py').read_text()

assert f'__version__ = "{version}"' in init
assert re.search(r'^version = "' + re.escape(version) + r'"$', pyproject, re.M)
assert 'immutavault = "immutavault.cli_v10:main"' in pyproject
assert 'immutavault-flr-broker = "immutavault.flr_broker:main"' in pyproject

# Release-facing operator documentation must never lag VERSION.
assert readme.startswith(f'# Immutavault v{version}\n'), 'README release heading is stale'
assert f'git checkout v{version}' in readme, 'README install command is not pinned to current VERSION'
assert 'incremental_strict: true' in readme
assert 'incremental_fallback: false' in readme
assert 'Broadcom VDDK' in readme and 'not bundled' in readme.lower()
assert 'file-level recovery' in readme.lower()
assert 'application_consistency_strict: true' in readme
assert 'immutavault-vmware-proxmox-v1' in readme
assert 'powered off' in readme.lower()
assert 'XCP-ng' in readme and 'certified provider' in readme
assert 'FLR broker' in readme, 'README must describe v1.0.1 FLR privilege separation'

# Preserve the v0.7.1 strict VMware contract.
for token in ('incremental_strict: true', 'incremental_fallback: false', 'fail closed', 'fallback_safe'):
    assert token in vmware_doc, f'VMware runbook missing required policy token: {token}'
assert 'mode: "vddk"' in incremental_example
assert 'incremental_strict: true' in incremental_example
assert 'incremental_fallback: false' in incremental_example
assert 'application_consistency_strict: true' in incremental_example

# Preserve v0.8 FLR data-safety semantics while moving mount privilege out of the portal.
assert 'flr:' in config_example and 'mount_root: "/srv/immutavault/flr"' in config_example
for token in ('restic mount', 'guestmount --ro', 'path traversal', 'symlink'):
    assert token in flr_doc, f'FLR runbook missing safety token: {token}'
for token in ('--no-lock', 'guestmount', 'does not follow guest symlinks', 'max_download_bytes'):
    assert token in flr_code, f'FLR implementation missing required safety token: {token}'
for token in ('SO_PEERCRED', 'admin=False', 'DEFAULT_SOCKET', 'RemoteFLRFile', 'owner-only'):
    assert token in flr_broker, f'FLR broker missing hardening token: {token}'

# v1.0 certified V2V remains opt-in/fail-closed; v1.0.1 adds target readiness checks.
for token in (
    'v2v:\n  enabled: false',
    'require_verified_point: true',
    'allow_suspicious_points: false',
    'allow_secure_boot: false',
    'virt_v2v_min_version: "2.12.0"',
    'v2v_network_map:',
):
    assert token in v2v_config, f'v1.0 example missing V2V safety token: {token}'
for token in (
    'VMware/vCenter -> Proxmox VE',
    'Native VMware VDDK/CBT layout',
    'powered off',
    'vTPM',
    'Secure Boot',
    'SHA-256-pinned',
    'VMware/Proxmox -> XCP-ng',
    'isolated recovery network',
):
    assert token in v2v_doc, f'certified V2V runbook missing safety token: {token}'
for token in (
    'input:ova', 'output:local', 'convert:linux', 'convert:windows',
    'source_read_only', 'target_new_vm', 'network_mapped', 'rollback_available',
    'qm importdisk', 'automatic_power_on',
):
    assert token in v2v_code, f'V2V implementation missing required contract token: {token}'
assert 'OVF_EXPORT_TRANSPORTS' in v2v_cert and 'NATIVE_INCREMENTAL_TRANSPORTS' in v2v_cert
assert 'pvesm status --storage' in v2v_cert, 'V2V target storage preflight is missing'
assert 'ip -o link show dev' in v2v_cert, 'V2V target bridge preflight is missing'
assert 'cross-hypervisor recovery blocked at execution' in v2v_engine
assert 'v2v-doctor' in v2v_cli and 'v2v-plan' in v2v_cli
print(version)
PY
pass "version/release documentation consistent: $VERSION"

# Never ship runtime secrets, TLS private keys, catalogs, or backup payloads.
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
pass 'unit/security/DR/V2V/FLR-broker suite passed'

PYTHONPATH=src python3 - <<'PY'
from immutavault.config import load_config
from immutavault.v2v_config import load_v10_config
cfg = load_config('config/immutavault.example.yml')
assert cfg.runtime.command_timeout_seconds > 0
assert cfg.repository.retention.keep_within_days >= 1
v10 = load_v10_config('config/enterprise-v1.0.example.yml')
assert v10.v2v.enabled is False
assert v10.v2v.require_verified_point is True
print('config valid')
PY
pass 'example configurations validate'

PYTHONPATH=src python3 -m immutavault.cli_v10 --help >/dev/null
PYTHONPATH=src python3 -m immutavault.cli_v10 v2v-plan --help >/dev/null
PYTHONPATH=src python3 -m immutavault.flr_broker --help >/dev/null
pass 'v1.0.1 source-tree CLI/broker smoke tests'

if command -v systemd-analyze >/dev/null 2>&1; then
  mkdir -p "$TMP/systemd"
  cp systemd/* "$TMP/systemd/"
  sed -i 's#/usr/local/bin/immutavault-flr-broker#/bin/true#g; s#/usr/local/bin/immutavault#/bin/true#g; s#/usr/bin/env rest-server#/bin/true#g' "$TMP"/systemd/*.service
  if ! systemd-analyze verify "$TMP"/systemd/*.service "$TMP"/systemd/*.timer >"$TMP/systemd.out" 2>&1; then
    cat "$TMP/systemd.out" >&2
    fail 'systemd unit verification failed'
  fi
  pass 'systemd units verify'
fi

rm -rf build dist src/*.egg-info
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
from immutavault.flr_broker import FLRBrokerClient
from immutavault.v2v_config import load_v10_config
assert immutavault.__version__ == '$VERSION'
load_config('config/immutavault.example.yml')
v10 = load_v10_config('config/enterprise-v1.0.example.yml')
assert v10.v2v.enabled is False
assert FLRBrokerClient is not None
print(immutavault.__version__)
PY
pass 'built wheel imports and loads production/v1.0.1 configuration'

# Static install/data-plane contracts.
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
grep -q 'libguestfs-tools' scripts/install_appliance.sh || fail 'appliance installer lacks FLR/libguestfs dependency'
grep -q 'fuse3' scripts/install_appliance.sh || fail 'appliance installer lacks FUSE3 dependency'
grep -q 'guestmount' scripts/check_flr.sh || fail 'FLR prerequisite checker is missing guestmount gate'
grep -q 'NoNewPrivileges=true' systemd/immutavault-portal.service || fail 'portal must enforce NoNewPrivileges=true'
grep -q 'PrivateDevices=true' systemd/immutavault-portal.service || fail 'portal must isolate host devices'
grep -q '^CapabilityBoundingSet=$' systemd/immutavault-portal.service || fail 'portal capability set must be empty'
grep -q 'PrivateMounts=true' systemd/immutavault-flr.service || fail 'FLR broker must use a private mount namespace'
grep -q 'User=root' systemd/immutavault-flr.service || fail 'FLR broker service identity contract changed unexpectedly'
grep -q 'immutavault-flr.service' scripts/install.sh || fail 'installer does not enable FLR broker'
grep -q 'immutavault-flr-broker' scripts/install_controller.sh || fail 'controller installer lacks broker entrypoint'
! grep -q 'usermod -a -G fuse immutavault' scripts/install_controller.sh || fail 'portal identity is still granted fuse group access'
grep -q 'virt-v2v --machine-readable' scripts/check_v2v.sh || fail 'V2V capability checker lacks machine-readable probe'
grep -q 'input:ova' scripts/check_v2v.sh || fail 'V2V capability checker lacks OVA input gate'
grep -q 'output:local' scripts/check_v2v.sh || fail 'V2V capability checker lacks local output gate'
pass 'restic/rest-server installs are pinned, checksummed and capability-gated'
pass 'FLR privilege separation and installation contract are present'
pass 'V2V conversion and target-readiness gates are present'

printf '\nALL RELEASE CHECKS PASSED for Immutavault %s\n' "$VERSION"
