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
