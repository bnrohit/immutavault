import json
from immutavault.config import RepositoryConfig, RetentionConfig
from immutavault.restic import ResticRepository


def test_retention_does_not_delete_protected(monkeypatch, tmp_path):
    cfg = RepositoryConfig(
        url="rest:https://example.invalid/",
        local_path=str(tmp_path / "repo"),
        staging_path=str(tmp_path / "staging"),
        retention=RetentionConfig(keep_within_days=30, keep_daily=30, keep_weekly=8, keep_monthly=12, keep_yearly=3, min_restore_points=2),
    )
    repo = ResticRepository(cfg)
    calls = []

    class R:
        def __init__(self, stdout=""):
            self.returncode = 0; self.stdout = stdout; self.stderr = ""; self.command = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--dry-run" in cmd:
            return R(json.dumps([{"remove": [{"id": "old1"}, {"id": "protected1"}], "keep": []}]))
        return R("")

    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setattr("immutavault.restic.run", fake_run)
    removed = repo.retention(protected_snapshot_ids={"protected1"})
    assert removed == ["old1"]
    assert any(c[:2] == ["restic", "forget"] and "old1" in c and "protected1" not in c for c in calls)
    assert any(c[:2] == ["restic", "prune"] for c in calls)
