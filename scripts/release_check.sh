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
v2v_doc = Path('docs/CERTIFIED_V2V.md').read_text()
v2v_config = Path('config/enterprise-v1.0.example.yml').read_text()
v2v_code = Path('src/immutavault/v2v.py').read_text()
v2v_cert = Path('src/immutavault/v2v_cert.py').read_text()
v2v_engine = Path('src/immutavault/v2v_engine.py').read_text()
v11_cfg = Path('config/enterprise-v1.1.example.yml').read_text()
mgmt_cfg = Path('src/immutavault/management_config.py').read_text()
mgmt_broker = Path('src/immutavault/management_broker.py').read_text()
mgmt_service = Path('src/immutavault/management_service.py').read_text()
mgmt_final = Path('src/immutavault/management_service_final.py').read_text()
policy = Path('src/immutavault/policy.py').read_text()
portal = Path('src/immutavault/portal_v11_final.py').read_text()
recovery_test = Path('src/immutavault/recovery_test.py').read_text()

assert f'__version__ = "{version}"' in init
assert re.search(r'^version = "' + re.escape(version) + r'"$', pyproject, re.M)
assert 'immutavault = "immutavault.cli_v11:main"' in pyproject
assert 'immutavault-flr-broker = "immutavault.flr_broker:main"' in pyproject
assert 'immutavault-management-broker = "immutavault.management_service_final:main"' in pyproject
assert readme.startswith(f'# Immutavault v{version}\n')
assert f'git checkout v{version}' in readme
for token in (
    'Unified Management', 'SO_PEERCRED', 'primary repository only', 'Run DR Test',
    'NFS', 'SMB 3.1.1', 'bootstrap.sh', 'build_appliance.sh', 'SHA256SUMS',
    'immutavault-vmware-proxmox-v1', 'VirtIO', 'Secure Boot', 'vTPM',
    'powered off', 'certified provider', 'FLR broker', 'target readiness',
    'incremental_strict: true', 'incremental_fallback: false',
    'application_consistency_strict: true', 'Broadcom VDDK', 'not bundled',
):
    assert token in readme, f'README missing release token: {token}'

# Preserve strict native VMware behavior.
for token in ('incremental_strict: true', 'incremental_fallback: false', 'fail closed', 'fallback_safe'):
    assert token in vmware_doc, f'VMware runbook missing {token}'
for token in ('mode: "vddk"', 'incremental_strict: true', 'incremental_fallback: false', 'application_consistency_strict: true'):
    assert token in incremental_example

# Preserve FLR privilege separation and read-only safety.
for token in ('restic mount', 'guestmount --ro', 'path traversal', 'symlink'):
    assert token in flr_doc
for token in ('--no-lock', 'guestmount', 'does not follow guest symlinks', 'max_download_bytes'):
    assert token in flr_code
for token in ('SO_PEERCRED', 'admin=False', 'DEFAULT_SOCKET', 'RemoteFLRFile', 'owner-only'):
    assert token in flr_broker

# Preserve certified V2V fail-closed contract.
for token in ('v2v:\n  enabled: false', 'require_verified_point: true', 'allow_suspicious_points: false', 'allow_secure_boot: false', 'virt_v2v_min_version: "2.12.0"', 'v2v_network_map:'):
    assert token in v2v_config
for token in ('VMware/vCenter -> Proxmox VE', 'Native VMware VDDK/CBT layout', 'powered off', 'vTPM', 'Secure Boot', 'SHA-256-pinned', 'VMware/Proxmox -> XCP-ng', 'isolated recovery network'):
    assert token in v2v_doc
for token in ('input:ova', 'output:local', 'convert:linux', 'convert:windows', 'source_read_only', 'target_new_vm', 'network_mapped', 'rollback_available', 'qm importdisk', 'automatic_power_on'):
    assert token in v2v_code
assert 'OVF_EXPORT_TRANSPORTS' in v2v_cert and 'NATIVE_INCREMENTAL_TRANSPORTS' in v2v_cert
assert 'pvesm status --storage' in v2v_cert and 'ip -o link show dev' in v2v_cert
assert 'cross-hypervisor recovery blocked at execution' in v2v_engine

# v1.1 management must be validated, exact-scope and privilege-separated.
for token in ('management:', 'broker_socket:', 'daily-production', 'replica_targets:', 'dr_test_networks:', 'dr_test_auto_cleanup: true'):
    assert token in v11_cfg
for token in ('POLICY_ID_RE', 'exact VM names', 'replica_targets', 'dr_test_networks', 'broker_socket'):
    assert token in mgmt_cfg
for token in ('SO_PEERCRED', 'management broker rejected unauthorized local peer', 'policy_save', 'dr_test_network_save'):
    assert token in mgmt_broker
for token in ('/srv/immutavault/storage', 'mount_type', 'cifs', 'nfs', 'systemd-escape', 'legacy_backup_timer_disabled'):
    assert token in mgmt_service
for token in ('FinalValidatedManagementManager', 'credentials', 'mountpoint', 'writable'):
    assert token in mgmt_final
assert 'An empty replica list means primary repository only' in policy
for token in ('ManagedRecoveryEngine', 'Run DR Test', 'replica_targets', 'mount_source', 'mount_username', 'mount_password', 'isolated recovery test cleanup failed'):
    assert token in portal
for token in ('vif-move', 'vm.network.change', 'qm set', 'dr_test_auto_cleanup'):
    assert token in recovery_test
print(version)
PY
pass "version/release documentation consistent: $VERSION"

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
pass 'unit/security/DR/V2V/FLR/management suite passed'

PYTHONPATH=src python3 - <<'PY'
from immutavault.config import load_config
from immutavault.v2v_config import load_v10_config
from immutavault.management_config import load_v11_config
core = load_config('config/immutavault.example.yml')
assert core.runtime.command_timeout_seconds > 0
v10 = load_v10_config('config/enterprise-v1.0.example.yml')
assert v10.v2v.enabled is False and v10.v2v.require_verified_point is True
v11 = load_v11_config('config/enterprise-v1.1.example.yml')
assert v11.management.enabled is True
assert v11.management.policies[0].id == 'daily-production'
assert v11.management.policies[0].replica_targets == ()
print('config valid')
PY
pass 'v1.1 and retained example configurations validate'

PYTHONPATH=src python3 -m immutavault.cli_v11 --help >/dev/null
PYTHONPATH=src python3 -m immutavault.cli_v11 policy-run --help >/dev/null
PYTHONPATH=src python3 -m immutavault.cli_v11 management-status --help >/dev/null
PYTHONPATH=src python3 -m immutavault.flr_broker --help >/dev/null
PYTHONPATH=src python3 -m immutavault.management_service_final --help >/dev/null
./scripts/bootstrap.sh --help >/dev/null
./scripts/build_appliance.sh --help >/dev/null
pass 'v1.1 CLI/broker/bootstrap/appliance smoke tests'

if command -v systemd-analyze >/dev/null 2>&1; then
  mkdir -p "$TMP/systemd"
  cp systemd/* "$TMP/systemd/"
  sed -i 's#/usr/local/bin/immutavault-flr-broker#/bin/true#g; s#/usr/local/bin/immutavault-management-broker#/bin/true#g; s#/usr/local/bin/immutavault#/bin/true#g; s#/usr/bin/env rest-server#/bin/true#g' "$TMP"/systemd/*.service
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
from immutavault.management_config import load_v11_config
from immutavault.flr_broker import FLRBrokerClient
from immutavault.management_broker import ManagementBrokerClient
from immutavault.portal_v11_final import ManagedRecoveryEngine
assert immutavault.__version__ == '$VERSION'
cfg = load_v11_config('config/enterprise-v1.1.example.yml')
assert cfg.management.enabled
assert FLRBrokerClient is not None and ManagementBrokerClient is not None and ManagedRecoveryEngine is not None
print(immutavault.__version__)
PY
pass 'built wheel imports and loads v1.1 production configuration'

# Static install/data-plane contracts retained from previous releases.
grep -q './scripts/install_restic.sh' scripts/install.sh || fail 'all-in-one installer lacks verified restic path'
grep -q 'sha256sum --check --status' scripts/install_restic.sh || fail 'restic installer lacks checksum verification'
grep -q 'check_restic.sh' scripts/preflight.sh || fail 'preflight lacks restic compatibility gate'
grep -q './scripts/install_rest_server.sh' scripts/install.sh || fail 'all-in-one installer lacks verified rest-server path'
grep -q 'sha256sum --check --status' scripts/install_rest_server.sh || fail 'rest-server installer lacks checksum verification'
grep -q 'check_rest_server.sh' scripts/install_repository.sh || fail 'repository installer lacks rest-server compatibility gate'
grep -q -- '--append-only' scripts/check_rest_server.sh || fail 'rest-server compatibility gate lacks append-only check'
grep -q -- '--tls-min-ver' scripts/check_rest_server.sh || fail 'rest-server compatibility gate lacks hardened TLS check'
grep -q 'EnvironmentFile=/etc/immutavault/repository.env' systemd/immutavault-rest-server.service || fail 'rest-server still receives controller environment'
grep -q 'NoNewPrivileges=true' systemd/immutavault-portal.service || fail 'portal must enforce NoNewPrivileges=true'
grep -q 'PrivateDevices=true' systemd/immutavault-portal.service || fail 'portal must isolate host devices'
grep -q '^CapabilityBoundingSet=$' systemd/immutavault-portal.service || fail 'portal capability set must be empty'
grep -q 'PrivateMounts=true' systemd/immutavault-flr.service || fail 'FLR broker must use a private mount namespace'
grep -q 'User=root' systemd/immutavault-flr.service || fail 'FLR broker service identity contract changed unexpectedly'
grep -q 'immutavault-management.service' scripts/install.sh || fail 'installer does not enable management broker'
grep -q 'User=root' systemd/immutavault-management.service || fail 'management broker must be a local privileged broker'
grep -q 'CAP_SYS_ADMIN' systemd/immutavault-management.service || fail 'management broker lacks required NFS/SMB mount capability'
grep -q '^CapabilityBoundingSet=$' systemd/immutavault-portal.service || fail 'management privilege leaked into portal'
grep -q 'User=immutavault' systemd/immutavault-policy@.service || fail 'named policy worker must remain unprivileged'
grep -q 'immutavault.cli_v11:main' pyproject.toml || fail 'package CLI is not v1.1'
grep -q 'management_service_final:main' pyproject.toml || fail 'package management broker is not final v1.1 service'
grep -q 'IMMUTAVAULT_ARCHIVE_SHA256' scripts/bootstrap.sh || fail 'bootstrap lacks optional archive digest pin'
grep -q 'base-image-sha256' scripts/build_appliance.sh || fail 'appliance builder lacks mandatory base-image digest'
grep -q 'subformat=streamOptimized' scripts/build_appliance.sh || fail 'appliance builder lacks VMware streamOptimized VMDK'
grep -q 'format=vhd\|VHD\|vpc' scripts/build_appliance.sh || fail 'appliance builder lacks explicit XCP-ng VHD artifact path'
! grep -q 'immutavault-.*\.xva' scripts/build_appliance.sh || fail 'appliance builder fabricates an XVA name'
pass 'pinned data plane, privilege separation and appliance contracts are present'

printf '\nALL RELEASE CHECKS PASSED for Immutavault %s\n' "$VERSION"
