from pathlib import Path

import pytest
import yaml

from immutavault import setup_console as sc


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
                    "lock_file": "/run/lock/immutavault.lock",
                    "retention": {"keep_within_days": 30, "keep_daily": 30, "keep_weekly": 8, "keep_monthly": 12, "keep_yearly": 3, "min_restore_points": 2},
                },
                "runtime": {
                    "state_db": "/tmp/state.db",
                    "command_timeout_seconds": 60,
                    "keep_staging_on_failure": True,
                    "restore_staging_path": "/tmp/restore",
                    "verify_staging_path": "/tmp/verify",
                    "staging_min_free_percent": 1,
                    "state_backup_path": "/tmp/state-backups",
                    "state_backup_keep": 4,
                },
                "protection": {"anomaly_data_added_ratio": 0.7, "anomaly_size_change_ratio": 0.5, "preserve_suspicious_points_days": 90, "verify_after_backup": False, "require_four_eyes_restore": True},
                "portal": {"enabled": False, "listen": "127.0.0.1", "port": 8787, "users": []},
                "platforms": [],
                "replicas": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_vmware_credentials_are_environment_only(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    m = sc.SetupManager(str(cfg), str(env))
    result = m.save_platform({"name": "vc-main", "type": "vmware", "endpoint": "https://vc/sdk", "username": "svc", "password": "secret"})
    data = yaml.safe_load(cfg.read_text())
    text = cfg.read_text()
    assert result["saved"] == "vc-main"
    assert "secret" not in text and "svc" not in text
    assert data["platforms"][0]["options"]["username_env"] == "IMMUTAVAULT_VC_MAIN_USERNAME"
    env_text = env.read_text()
    assert "IMMUTAVAULT_VC_MAIN_USERNAME=svc" in env_text
    assert "IMMUTAVAULT_VC_MAIN_PASSWORD=secret" in env_text


def test_selected_vms_replace_wildcard(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    m = sc.SetupManager(str(cfg), str(env))
    m.save_platform({"name": "pve", "type": "proxmox", "endpoint": "pve.example", "ssh_user": "backupsvc", "ssh_key_path": "/keys/pve"})
    result = m.save_selection("pve", ["dns01", "app01"])
    data = yaml.safe_load(cfg.read_text())
    assert result["selected"] == 2
    assert data["platforms"][0]["include"] == ["dns01", "app01"]
    assert data["platforms"][0]["exclude"] == []


def test_s3_secrets_are_environment_only(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    m = sc.SetupManager(str(cfg), str(env))
    m.save_storage({"name": "wasabi-dr", "backend": "s3", "provider": "wasabi", "endpoint": "https://s3.example", "region": "us-test-1", "bucket": "vault", "access_key": "AK123", "secret_key": "SECRET123", "password": "REPOPASS", "immutable": True, "lock_days": 30})
    text = cfg.read_text()
    data = yaml.safe_load(text)
    assert "AK123" not in text and "SECRET123" not in text and "REPOPASS" not in text
    replica = data["replicas"][0]
    assert replica["object_lock_enabled"] is True
    assert replica["object_lock_mode"] == "COMPLIANCE"
    assert replica["access_key_env"] == "WASABI_DR_ACCESS_KEY_ID"


def test_dr_setup_keeps_automatic_failover_off(tmp_path, monkeypatch):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    monkeypatch.setattr(sc, "load_config", lambda _: object())
    m = sc.SetupManager(str(cfg), str(env))
    result = m.save_dr({"primary_site": "main", "dr_site": "dr", "replica": "wasabi-dr", "primary_gateway_host": "gw-main", "dr_gateway_host": "gw-dr", "primary_vtep": "10.255.0.1", "dr_vtep": "10.255.0.2", "vlan_id": 48, "vni": 10048, "subnet": "10.14.48.0/21", "gateway_cidr": "10.14.48.1/21", "mtu": 1450})
    data = yaml.safe_load(cfg.read_text())
    assert result["auto_failover"] is False
    assert data["disaster_recovery"]["auto_failover"] is False
    assert data["disaster_recovery"]["fence"]["mode"] == "manual"


def test_dr_prepare_requires_typed_confirmation(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    m = sc.SetupManager(str(cfg), str(env))
    with pytest.raises(ValueError, match="confirmation"):
        m.dr_prepare("dr", "yes")


def test_remote_setup_console_requires_tls(tmp_path):
    cfg, env = tmp_path / "config.yml", tmp_path / "env"
    base_config(cfg)
    with pytest.raises(RuntimeError, match="requires TLS"):
        sc.serve(str(cfg), str(env), "0.0.0.0", 8788, None, None, "token")


def test_ui_exposes_five_guided_steps():
    for text in ("1. Hypervisor", "2. Select VMs", "3. Storage / Cloud", "4. DR site and network", "5. Test and start"):
        assert text in sc.UI
    assert "APPLY DR NETWORK" in sc.UI
    assert "Automatic DR failover stays OFF" in sc.UI
