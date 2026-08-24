from pathlib import Path
from immutavault.config import load_config


def test_load_config(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        """
repository:
  url: rest:https://backup.example/repo
  local_path: /srv/repo
  staging_path: /srv/staging
platforms:
  - name: pve
    type: proxmox
    endpoint: pve.example
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg)
    assert loaded.repository.retention_days == 30
    assert loaded.platforms[0].type == "proxmox"


def test_reject_bad_retention(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        """
repository:
  url: x
  local_path: /x
  staging_path: /y
  retention_days: 0
""",
        encoding="utf-8",
    )
    try:
        load_config(cfg)
    except ValueError as exc:
        assert "retention_days" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_runtime_state_backup_defaults(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text("""
repository:
  url: rest:https://backup.example/repo
  local_path: /srv/repo
  staging_path: /srv/staging
runtime:
  state_backup_keep: 42
platforms: []
""", encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.runtime.state_backup_keep == 42
    assert loaded.runtime.state_backup_path.endswith("state-backups")
