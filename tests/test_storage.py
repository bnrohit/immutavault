from datetime import datetime, timezone
from pathlib import Path
import os
import pytest

from immutavault.config import ReplicaConfig, load_config
from immutavault.state import StateDB
from immutavault.storage import apply_object_lock, restic_target_url, target_env


def test_s3_target_url_and_env(monkeypatch):
    cfg = ReplicaConfig(
        name="wasabi", backend="s3", provider="wasabi", endpoint="https://s3.us-west-1.wasabisys.com",
        bucket="vault", prefix="customer-a", region="us-west-1", password_env="RESTIC_WASABI_PASSWORD",
        access_key_env="WASABI_ACCESS_KEY_ID", secret_key_env="WASABI_SECRET_ACCESS_KEY",
    )
    monkeypatch.setenv("RESTIC_WASABI_PASSWORD", "repo-secret")
    monkeypatch.setenv("WASABI_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("WASABI_SECRET_ACCESS_KEY", "secret")
    assert restic_target_url(cfg) == "s3:https://s3.us-west-1.wasabisys.com/vault/customer-a"
    env = target_env(cfg)
    assert env["RESTIC_PASSWORD"] == "repo-secret"
    assert env["AWS_ACCESS_KEY_ID"] == "key"
    assert env["AWS_DEFAULT_REGION"] == "us-west-1"


def test_filesystem_target_url():
    cfg = ReplicaConfig(name="nas", backend="filesystem", path="/mnt/truenas/immutavault")
    assert restic_target_url(cfg) == "/mnt/truenas/immutavault"


def test_r2_object_lock_rejected(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        """
repository:
  url: rest:https://vault.example/repo
  local_path: /srv/repo
  staging_path: /srv/staging
replicas:
  - name: r2
    backend: s3
    provider: cloudflare_r2
    endpoint: https://abc.r2.cloudflarestorage.com
    bucket: backups
    object_lock_enabled: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Object Lock"):
        load_config(cfg)


def test_recovery_copy_catalog(tmp_path: Path):
    db = StateDB(str(tmp_path / "state.db"))
    now = datetime.now(timezone.utc).isoformat()
    db.upsert_recovery_point({
        "snapshot_id":"snap1","created_at":now,"platform":"pve","platform_type":"proxmox","vm_id":"100",
        "vm_name":"vm1","power_state":"running","source_path":"/tmp/vm1","backup_bytes":1,"data_added":1,
        "file_count":1,"manifest_sha256":"x","immutable_until":now,"suspicious":0,"suspicious_reason":None,
        "verified":0,"verified_at":None,"metadata_json":"{}",
    })
    db.upsert_recovery_copy(
        snapshot_id="snap1", target_name="wasabi", backend="s3", provider="wasabi",
        repository="s3:https://endpoint/bucket/prefix", status="success", immutable_until=now,
        object_lock={"enabled": True, "mode": "COMPLIANCE"},
    )
    copies = db.list_recovery_copies("snap1")
    assert copies[0]["target_name"] == "wasabi"
    assert copies[0]["object_lock"]["mode"] == "COMPLIANCE"


class _Paginator:
    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": "vault/data/abc"}, {"Key": "vault/locks/temp"}]}]


class _S3:
    def __init__(self):
        self.locked = []
    def head_bucket(self, **kwargs): return {}
    def get_object_lock_configuration(self, **kwargs):
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}
    def get_paginator(self, name): return _Paginator()
    def get_object_retention(self, **kwargs): return {"Retention": {}}
    def put_object_retention(self, **kwargs): self.locked.append(kwargs)


def test_object_lock_skips_restic_lock_objects(monkeypatch):
    cfg = ReplicaConfig(
        name="b2", backend="s3", provider="backblaze_b2", endpoint="https://s3.us-west-000.backblazeb2.com",
        bucket="vault", prefix="vault", object_lock_enabled=True, object_lock_days=30,
        access_key_env="B2_KEY", secret_key_env="B2_SECRET", password_env="RESTIC_B2_PASSWORD",
    )
    monkeypatch.setenv("B2_KEY", "key"); monkeypatch.setenv("B2_SECRET", "secret")
    fake = _S3()
    monkeypatch.setattr("immutavault.storage._s3_client", lambda cfg: fake)
    result = apply_object_lock(cfg)
    assert result["locked"] == 1
    assert result["skipped"] == 1
    assert fake.locked[0]["Key"] == "vault/data/abc"
    assert fake.locked[0]["Retention"]["Mode"] == "COMPLIANCE"

def test_primary_rest_credentials_embedded_for_copy(monkeypatch):
    from immutavault.config import RepositoryConfig
    from immutavault.restic import ResticRepository
    cfg = RepositoryConfig(url="rest:https://vault.example:8000/repo/", local_path="/srv/repo", staging_path="/srv/staging")
    monkeypatch.setenv("REST_SERVER_USER", "backup writer")
    monkeypatch.setenv("REST_SERVER_PASSWORD", "p@ss word")
    repo = ResticRepository(cfg)
    source = repo._source_repository_with_auth()
    assert source.startswith("rest:https://backup%20writer:p%40ss%20word@vault.example:8000/")


def test_r2_bucket_lock_config_is_supported(tmp_path: Path):
    cfg_path = tmp_path / "r2.yml"
    cfg_path.write_text(
        """
repository:
  url: rest:https://vault.example/repo
  local_path: /srv/repo
  staging_path: /srv/staging
replicas:
  - name: r2
    backend: s3
    provider: cloudflare_r2
    endpoint: https://abc.r2.cloudflarestorage.com
    bucket: backups
    r2_bucket_lock_enabled: true
    r2_bucket_lock_days: 45
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    r2 = cfg.replicas[0]
    assert r2.r2_bucket_lock_enabled is True
    assert r2.r2_bucket_lock_days == 45
    assert r2.object_lock_enabled is False


def test_r2_bucket_lock_preserves_stronger_rules(monkeypatch):
    from immutavault.storage import ensure_r2_bucket_lock
    cfg = ReplicaConfig(
        name="r2", backend="s3", provider="cloudflare_r2",
        endpoint="https://abc.r2.cloudflarestorage.com", bucket="vault", prefix="prod",
        r2_bucket_lock_enabled=True, r2_bucket_lock_days=30,
    )
    suffixes = ["data", "index", "snapshots", "keys", "config"]
    namespaces = ["data/", "index/", "snapshots/", "keys/", "config"]
    existing = [
        {"id": f"immutavault-retention-{suffix}", "enabled": True, "prefix": f"prod/{namespace}",
         "condition": {"type": "Age", "maxAgeSeconds": 90 * 86400}}
        for suffix, namespace in zip(suffixes, namespaces)
    ]
    calls = []
    def fake_api(cfg, method, payload=None):
        calls.append((method, payload))
        return {"rules": existing}
    monkeypatch.setattr("immutavault.storage._r2_api", fake_api)
    result = ensure_r2_bucket_lock(cfg, minimum_days=60)
    assert result["changed"] is False
    assert result["transient_prefix_excluded"] == "prod/locks/"
    assert len(result["rules"]) == 5
    assert [c[0] for c in calls] == ["GET"]


def test_r2_bucket_lock_adds_persistent_namespace_rules(monkeypatch):
    from immutavault.storage import ensure_r2_bucket_lock
    cfg = ReplicaConfig(
        name="r2", backend="s3", provider="cloudflare_r2",
        endpoint="https://abc.r2.cloudflarestorage.com", bucket="vault", prefix="customer-a",
        r2_bucket_lock_enabled=True, r2_bucket_lock_days=30,
    )
    calls = []
    def fake_api(cfg, method, payload=None):
        calls.append((method, payload))
        if method == "GET":
            return {"rules": [{"id": "other", "enabled": True, "prefix": "logs/", "condition": {"type": "Age", "maxAgeSeconds": 86400}}]}
        return {"rules": payload["rules"]}
    monkeypatch.setattr("immutavault.storage._r2_api", fake_api)
    result = ensure_r2_bucket_lock(cfg)
    assert result["changed"] is True
    assert result["transient_prefix_excluded"] == "customer-a/locks/"
    assert "customer-a/data/" in result["protected_prefixes"]
    assert "customer-a/locks/" not in result["protected_prefixes"]
    assert calls[1][0] == "PUT"
    ids = {r["id"] for r in calls[1][1]["rules"]}
    assert "other" in ids
    assert "immutavault-retention-data" in ids
    assert len([i for i in ids if i.startswith("immutavault-retention-")]) == 5
