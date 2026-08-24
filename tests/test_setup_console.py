from pathlib import Path

import pytest
import yaml

from immutavault import setup_wizard as sc
from immutavault.config import load_config


def base_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "repository": {
                    "url": "rest:https://vault.example:8000/",
                    "local_path": "/srv/immutavault/repository",
                    "staging_path": "/srv/immutavault/staging",
                    "cacert": "/etc/immutavault/tls/ca.crt",
                    "verify_percent": 5,
                    "lock_file": "/tmp/immutavault.lock",
                    "retention": {"keep_within_days": 30, "keep_daily": 30, "keep_weekly": 8, "keep_monthly": 12, "keep_yearly": 3, "min_restore_points": 2},
                },
                "runtime": {
                    "state_db": str(path.parent / "state.db"),
                    "command_timeout_seconds": 60,
                    "keep_staging_on_failure": True,
                    "restore_staging_path": str(path.parent / "restore"),
                    "verify_staging_path": str(path.parent / "verify"),
                    "staging_min_free_percent": 1,
                    "state_backup_path": str(path.parent / "state-backups"),
                    "state_backup_keep": 4,
                },
                "protection": {"anomaly_data_added_ratio": 0.7, "anomaly_size_change_ratio": 0.5, "preserve_suspicious_points_days": 90, "verify_after_backup": False, "require_four_eyes_restore": True},
                "portal": {"enabled": False, "listen": "127.0.0.1", "port": 8787, "users": []},
                "platforms": [], "replicas": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_vmware_credentials_are_environment_only(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    result = m.save_platform({"name": "vc-main", "type": "vmware", "endpoint": "https://vc/sdk", "username": "svc-user", "password": "super-secret"})
    text = cfg.read_text(); data = yaml.safe_load(text)
    assert result["saved"] == "vc-main"
    assert "svc-user" not in text and "super-secret" not in text
    assert data["platforms"][0]["options"]["username_env"] == "IMMUTAVAULT_VC_MAIN_USERNAME"
    env_text = env.read_text()
    assert "IMMUTAVAULT_VC_MAIN_USERNAME=svc-user" in env_text
    assert "IMMUTAVAULT_VC_MAIN_PASSWORD=super-secret" in env_text


def test_selected_vms_replace_wildcard_and_survive_platform_resave(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    body = {"name": "pve", "type": "proxmox", "endpoint": "pve.example", "ssh_user": "backupsvc", "ssh_key_path": "/keys/pve"}
    m.save_platform(body)
    result = m.save_selection("pve", ["dns01", "app01", "dns01"])
    m.save_platform(body)
    data = yaml.safe_load(cfg.read_text())
    assert result["selected"] == 2
    assert data["platforms"][0]["include"] == ["dns01", "app01"]
    assert data["platforms"][0]["exclude"] == []


def test_per_platform_ssh_key_is_stored_as_reference(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    m.save_platform({"name": "xcp-dr", "type": "xcpng", "endpoint": "xcp.example", "ssh_user": "backupsvc", "ssh_key_path": "/keys/xcp-dr"})
    data = yaml.safe_load(cfg.read_text())
    assert data["platforms"][0]["options"]["ssh_key_env"] == "IMMUTAVAULT_XCP_DR_SSH_KEY"
    assert "/keys/xcp-dr" not in cfg.read_text()
    assert "IMMUTAVAULT_XCP_DR_SSH_KEY=/keys/xcp-dr" in env.read_text()


def test_s3_secrets_are_environment_only(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    m.save_storage({"name": "wasabi-dr", "backend": "s3", "provider": "wasabi", "endpoint": "https://s3.example", "region": "us-test-1", "bucket": "vault", "access_key": "AK123", "secret_key": "SECRET123", "password": "REPOPASS", "immutable": True, "lock_days": 30})
    text = cfg.read_text(); replica = yaml.safe_load(text)["replicas"][0]
    assert "AK123" not in text and "SECRET123" not in text and "REPOPASS" not in text
    assert replica["object_lock_enabled"] is True
    assert replica["object_lock_mode"] == "COMPLIANCE"
    assert replica["access_key_env"] == "WASABI_DR_ACCESS_KEY_ID"


def test_dr_wizard_stages_network_then_maps_exact_selected_vms(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    m.save_platform({"name": "vc-main", "type": "vmware", "endpoint": "https://vc-main/sdk", "username": "u", "password": "p"})
    m.save_platform({"name": "vc-dr", "type": "vmware", "endpoint": "https://vc-dr/sdk", "username": "u2", "password": "p2"})
    m.save_selection("vc-main", ["dns01", "app01"])
    nas = tmp_path / "nas"; nas.mkdir()
    m.save_storage({"name": "dr-nas", "backend": "filesystem", "path": str(nas), "password": "repo-pass"})
    result = m.save_dr({"primary_site": "main", "dr_site": "dr", "replica": "dr-nas", "primary_gateway_host": "gw-main", "dr_gateway_host": "gw-dr", "primary_vtep": "10.255.0.1", "dr_vtep": "10.255.0.2", "vlan_id": 48, "vni": 10048, "subnet": "10.14.48.0/21", "gateway_cidr": "10.14.48.1/21", "mtu": 1450, "source_platform": "vc-main", "target_platform": "vc-dr"})
    loaded = load_config(cfg)
    assert result["enabled"] is True and result["auto_failover"] is False
    assert [w.name for w in loaded.dr.workloads] == ["dns01", "app01"]
    assert [w.boot_order for w in loaded.dr.workloads] == [10, 20]
    assert loaded.dr.auto_failover is False
    assert loaded.dr.fence.mode == "manual"


def test_dr_wizard_blocks_cross_hypervisor_mapping(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    m.save_platform({"name": "pve-main", "type": "proxmox", "endpoint": "pve", "ssh_key_path": "/keys/pve"})
    m.save_platform({"name": "xcp-dr", "type": "xcpng", "endpoint": "xcp", "ssh_key_path": "/keys/xcp"})
    m.save_selection("pve-main", ["vm01"])
    nas = tmp_path / "nas"; nas.mkdir()
    m.save_storage({"name": "dr-nas", "backend": "filesystem", "path": str(nas), "password": "repo-pass"})
    with pytest.raises(ValueError, match="cross-hypervisor"):
        m.save_dr({"primary_site": "main", "dr_site": "dr", "replica": "dr-nas", "primary_gateway_host": "gw-main", "dr_gateway_host": "gw-dr", "primary_vtep": "10.255.0.1", "dr_vtep": "10.255.0.2", "vlan_id": 48, "vni": 10048, "subnet": "10.14.48.0/21", "gateway_cidr": "10.14.48.1/21", "source_platform": "pve-main", "target_platform": "xcp-dr"})


def test_dr_prepare_requires_typed_confirmation(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    with pytest.raises(ValueError, match="confirmation"):
        m.dr_prepare("dr", "yes")


def test_remote_setup_console_requires_tls(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    with pytest.raises(RuntimeError, match="requires TLS"):
        sc.serve(str(cfg), str(env), "0.0.0.0", 8788, None, None, "token")


def test_ui_exposes_guided_safe_flow():
    for text in ("1. Add a hypervisor", "2. Choose VMs to protect", "3. Add storage or cloud", "4. Configure disaster-recovery site", "5. Test and start protection"):
        assert text in sc.UI
    assert "APPLY DR NETWORK" in sc.UI
    assert "Automatic DR failover stays OFF" in sc.UI


def test_normal_schedule_button_does_not_enable_dr_watch():
    text = Path(sc.base.__file__).read_text(encoding="utf-8") + Path(sc.__file__).read_text(encoding="utf-8")
    assert "immutavault-dr-watch.timer" not in text
    assert 'confirmation != "ENABLE SCHEDULES"' in text


def test_package_exposes_setup_command():
    pyproject = (Path(sc.__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'immutavault-setup = "immutavault.setup_wizard:main"' in pyproject


def test_atomic_setup_preserves_installed_file_permissions(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    cfg.chmod(0o640)
    env.write_text("EXISTING=value\n", encoding="utf-8"); env.chmod(0o640)
    m = sc.SetupManager(str(cfg), str(env))
    m.save_platform({"name": "vc", "type": "vmware", "endpoint": "https://vc/sdk", "username": "u", "password": "p"})
    assert cfg.stat().st_mode & 0o777 == 0o640
    assert env.stat().st_mode & 0o777 == 0o640


def test_optional_ospf_secret_stays_out_of_yaml(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    nas = tmp_path / "nas"; nas.mkdir()
    m.save_storage({"name": "dr-nas", "backend": "filesystem", "path": str(nas), "password": "repo-pass"})
    m.save_platform({"name": "vc-main", "type": "vmware", "endpoint": "https://vc-main/sdk", "username": "u", "password": "p"})
    m.save_platform({"name": "vc-dr", "type": "vmware", "endpoint": "https://vc-dr/sdk", "username": "u2", "password": "p2"})
    m.save_selection("vc-main", ["vm01"])
    m.save_dr({"primary_site": "main", "dr_site": "dr", "replica": "dr-nas", "primary_gateway_host": "gw-main", "dr_gateway_host": "gw-dr", "primary_vtep": "10.255.0.1", "dr_vtep": "10.255.0.2", "vlan_id": 48, "vni": 10048, "subnet": "10.14.48.0/21", "gateway_cidr": "10.14.48.1/21", "ospf_key": "ospfSecret1", "dr_ssh_key_path": "/keys/dr", "source_platform": "vc-main", "target_platform": "vc-dr"})
    text = cfg.read_text(); data = yaml.safe_load(text)
    assert "ospfSecret1" not in text and "/keys/dr" not in text
    assert data["disaster_recovery"]["sites"][0]["gateway"]["ospf_auth_key_env"] == "IMMUTAVAULT_OSPF_KEY"
    env_text = env.read_text()
    assert "IMMUTAVAULT_OSPF_KEY=ospfSecret1" in env_text
    assert "IMMUTAVAULT_SSH_KEY=/keys/dr" in env_text


def test_installers_publish_setup_console_command():
    root = Path(sc.__file__).resolve().parents[2]
    controller = (root / "scripts/install_controller.sh").read_text(encoding="utf-8")
    upgrade = (root / "scripts/upgrade.sh").read_text(encoding="utf-8")
    rollback = (root / "scripts/rollback.sh").read_text(encoding="utf-8")
    launcher = (root / "scripts/launch_setup_console.sh").read_text(encoding="utf-8")
    assert "/usr/local/bin/immutavault-setup" in controller
    assert "/usr/local/bin/immutavault-setup" in upgrade
    assert "/usr/local/bin/immutavault-setup" in rollback
    assert "Remote guided setup requires TLS" in launcher


def test_setup_writes_are_schema_validated_before_replace(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    before = cfg.read_text()
    with pytest.raises(ValueError):
        sc._atomic(cfg, {"platforms": []})
    assert cfg.read_text() == before


def test_rpo_target_is_shared_config_schema(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg); m = sc.SetupManager(str(cfg), str(env))
    m.save_rpo_target(60)
    loaded = load_config(cfg)
    assert loaded.protection.rpo_target_minutes == 60
    assert yaml.safe_load(cfg.read_text())["protection"]["rpo_target_minutes"] == 60


def test_dashboard_prioritizes_rpo_and_immutable_copy_widgets():
    assert "RPO Status" in sc.base.UI or "RPO Status" in sc.UI
    assert "Immutable-Copy Verification" in sc.base.UI or "Immutable-Copy Verification" in sc.UI
    assert "/api/v1/setup/dashboard" in Path(sc.base.__file__).read_text(encoding="utf-8")


def test_copy_verification_uses_catalog_flag():
    state = Path(sc.__file__).resolve().parent / "state.py"
    wizard = Path(sc.__file__).resolve()
    assert "mark_copy_verified" in state.read_text(encoding="utf-8")
    text = wizard.read_text(encoding="utf-8")
    assert "verify_immutable_copies" in text
    assert "mark_copy_verified" in text
