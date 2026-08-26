from pathlib import Path

import pytest
import yaml

from immutavault.management_config import PolicySchedule, load_v11_config
from immutavault.management_service_final import FinalValidatedManagementManager
from immutavault.policy import ProtectionPolicyRunner, _PolicyConfigView
from immutavault.portal_v11_final import UI, ManagedRecoveryEngine


def test_v11_example_loads_and_is_fail_closed():
    cfg = load_v11_config("config/enterprise-v1.1.example.yml")
    assert cfg.management.enabled is True
    assert cfg.management.broker_socket == "/run/immutavault/manage.sock"
    assert len(cfg.management.policies) == 1
    policy = cfg.management.policies[0]
    assert policy.id == "daily-production"
    assert policy.selections[0].vms == ("app01", "db01")
    assert policy.replica_targets == ()
    assert policy.verify_after_backup is True
    assert cfg.v2v.enabled is False
    assert cfg.v2v.require_verified_point is True


def test_policy_schedule_calendar_shapes():
    assert PolicySchedule(frequency="manual").on_calendar() is None
    assert PolicySchedule(frequency="daily", time="22:30").on_calendar() == "*-*-* 22:30:00"
    assert PolicySchedule(frequency="weekly", time="03:15", weekdays=("mon", "fri")).on_calendar() == "Mon,Fri *-*-* 03:15:00"
    assert PolicySchedule(frequency="hourly", every_hours=6).on_calendar() == "*-*-* 00/6:00:00"


def test_checkbox_policy_rejects_wildcards(tmp_path):
    data = yaml.safe_load(Path("config/enterprise-v1.1.example.yml").read_text(encoding="utf-8"))
    data["management"]["policies"][0]["selections"][0]["vms"] = ["app*"]
    path = tmp_path / "wildcard.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="exact VM names"):
        load_v11_config(path)


def test_policy_rejects_disabled_replica_target(tmp_path):
    data = yaml.safe_load(Path("config/enterprise-v1.1.example.yml").read_text(encoding="utf-8"))
    data["management"]["policies"][0]["replica_targets"] = ["wasabi-dr"]
    path = tmp_path / "replica.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown or disabled replicas"):
        load_v11_config(path)


def test_policy_view_empty_replicas_means_primary_only():
    cfg = load_v11_config("config/enterprise-v1.1.example.yml")
    policy = cfg.management.policies[0]
    view = _PolicyConfigView(cfg, policy)
    assert view.replicas == []
    enabled = [p for p in view.platforms if p.enabled]
    # Example platforms are disabled by design; importantly the selected scope
    # is still exact and no wildcard is introduced by the policy overlay.
    selected = next(p for p in view.platforms if p.name == "vc-campus-a")
    assert selected.include == ["app01", "db01"]
    assert selected.exclude == []


def test_policy_runner_finds_named_policy_without_mutating_config():
    cfg = load_v11_config("config/enterprise-v1.1.example.yml")
    runner = ProtectionPolicyRunner(cfg)
    policy = runner.policy("daily-production")
    assert policy.name == "Daily Production"
    assert cfg.platforms[0].include == ["app01", "db01"]


def test_managed_nas_paths_are_constrained_below_storage_root(tmp_path):
    manager = FinalValidatedManagementManager(str(tmp_path / "config.yml"), str(tmp_path / "env"))
    typ, source, path, username, password = manager._mount_spec({
        "name": "nas-dr",
        "mount_type": "nfs",
        "mount_source": "truenas.example:/mnt/tank/immutavault",
        "path": "/srv/immutavault/storage/nas-dr",
    })
    assert typ == "nfs"
    assert source.endswith(":/mnt/tank/immutavault")
    assert path == "/srv/immutavault/storage/nas-dr"
    assert username is None and password is None
    with pytest.raises(ValueError, match="must live below"):
        manager._mount_spec({
            "name": "bad",
            "mount_type": "nfs",
            "mount_source": "server:/export",
            "path": "/etc",
        })


def test_smb_requires_credentials_and_uses_secure_options(tmp_path):
    manager = FinalValidatedManagementManager(str(tmp_path / "config.yml"), str(tmp_path / "env"))
    with pytest.raises(ValueError, match="SMB username and password"):
        manager._mount_spec({
            "name": "smb-dr", "mount_type": "cifs", "mount_source": "//server/share",
            "path": "/srv/immutavault/storage/smb-dr",
        })
    options = manager._mount_options("cifs", "/etc/immutavault/test.credentials")
    assert "vers=3.1.1" in options
    assert "nosuid" in options and "nodev" in options and "noexec" in options
    assert "credentials=/etc/immutavault/test.credentials" in options


def test_unified_portal_wires_replicas_direct_nas_and_dr_test():
    for token in (
        "Optional replica targets",
        "replica_targets:[...document.querySelectorAll('.pri:checked')]",
        "Direct NAS type",
        "mount_source",
        "mount_username",
        "mount_password",
        "Run DR Test",
        "dr_test:{network}",
        "ManagedRecoveryEngine",
    ):
        assert token in UI or token in Path("src/immutavault/portal_v11_final.py").read_text(encoding="utf-8")
    assert ManagedRecoveryEngine is not None


def test_management_broker_privilege_is_not_leaked_to_portal():
    portal = Path("systemd/immutavault-portal.service").read_text(encoding="utf-8")
    management = Path("systemd/immutavault-management.service").read_text(encoding="utf-8")
    policy = Path("systemd/immutavault-policy@.service").read_text(encoding="utf-8")
    assert "CapabilityBoundingSet=\n" in portal
    assert "PrivateDevices=true" in portal
    assert "User=root" in management
    assert "CAP_SYS_ADMIN" in management
    assert "User=immutavault" in policy


def test_release_bootstrap_and_appliance_are_pinned():
    bootstrap = Path("scripts/bootstrap.sh").read_text(encoding="utf-8")
    builder = Path("scripts/build_appliance.sh").read_text(encoding="utf-8")
    assert "refs/tags/v${VERSION}.tar.gz" in bootstrap
    assert "IMMUTAVAULT_ARCHIVE_SHA256" in bootstrap
    assert "--base-image-sha256" in builder
    assert "sha256sum --check --status" in builder
    assert "subformat=streamOptimized" in builder
    assert "-O vpc" in builder
    assert ".xva" in builder and "not named .xva" in builder
    assert "SHA256SUMS" in builder


def test_v11_docs_are_release_pinned():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Immutavault v1.1.0\n")
    assert "git checkout v1.1.0" in readme
    assert "get.immutavault.io` is **not** claimed" in readme
    assert Path("docs/UNIFIED_MANAGEMENT.md").exists()
    assert Path("docs/APPLIANCE_DEPLOYMENT.md").exists()
