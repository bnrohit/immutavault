from immutavault.config import RepositoryConfig
from immutavault.restic import ResticRepository


def test_primary_rest_backend_uses_environment_basic_auth(monkeypatch):
    monkeypatch.setenv("RESTIC_PASSWORD", "repo-secret")
    monkeypatch.setenv("REST_SERVER_USER", "writer")
    monkeypatch.setenv("REST_SERVER_PASSWORD", "transport-secret")
    cfg = RepositoryConfig(
        url="rest:https://vault.example:8000/",
        local_path="/srv/immutavault/repository",
        staging_path="/srv/immutavault/staging",
    )
    env = ResticRepository(cfg)._env(local=False)
    assert env["RESTIC_REST_USERNAME"] == "writer"
    assert env["RESTIC_REST_PASSWORD"] == "transport-secret"
    assert "transport-secret" not in env["RESTIC_REPOSITORY"]


def test_replica_copy_source_credentials_are_not_command_line(monkeypatch):
    monkeypatch.setenv("REST_SERVER_USER", "writer@example")
    monkeypatch.setenv("REST_SERVER_PASSWORD", "p:a/ss")
    cfg = RepositoryConfig(
        url="rest:https://vault.example:8000/repo",
        local_path="/srv/immutavault/repository",
        staging_path="/srv/immutavault/staging",
    )
    value = ResticRepository(cfg)._source_repository_with_auth()
    assert value.startswith("rest:https://writer%40example:p%3Aa%2Fss@vault.example:8000/repo")


def test_r2_replica_refreshes_rolling_bucket_lock_after_copy(monkeypatch):
    import json
    from immutavault.config import ReplicaConfig
    from immutavault.runner import CommandResult

    monkeypatch.setenv("RESTIC_PASSWORD", "source-secret")
    monkeypatch.setenv("REST_SERVER_USER", "writer")
    monkeypatch.setenv("REST_SERVER_PASSWORD", "transport")
    monkeypatch.setenv("RESTIC_R2_PASSWORD", "r2-repo-secret")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    cfg = RepositoryConfig(
        url="rest:https://vault.example:8000/repo",
        local_path="/srv/immutavault/repository",
        staging_path="/srv/immutavault/staging",
    )
    replica = ReplicaConfig(
        name="r2", backend="s3", provider="cloudflare_r2",
        endpoint="https://account.r2.cloudflarestorage.com", bucket="vault", prefix="prod",
        password_env="RESTIC_R2_PASSWORD", access_key_env="R2_ACCESS_KEY_ID", secret_key_env="R2_SECRET_ACCESS_KEY",
        r2_bucket_lock_enabled=True, r2_bucket_lock_days=30,
    )
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get("env", {})))
        if "snapshots" in command:
            return CommandResult(command, 0, json.dumps([{"id": "snap1"}]), "")
        return CommandResult(command, 0, "", "")
    lock_calls = []
    def fake_lock(target, *, minimum_days=None):
        lock_calls.append((target.name, minimum_days))
        return {"enabled": True, "kind": "cloudflare_r2_bucket_lock", "retain_until": "future"}
    monkeypatch.setattr("immutavault.restic.run", fake_run)
    monkeypatch.setattr("immutavault.restic.ensure_r2_bucket_lock", fake_lock)

    result = ResticRepository(cfg).copy_snapshot("snap1", replica, immutable_days=45)
    assert result["status"] == "success"
    assert lock_calls == [("r2", 45)]
    assert any("copy" in command for command, _ in calls)
    assert any("snapshots" in command for command, _ in calls)
