from pathlib import Path
from types import SimpleNamespace

from immutavault import policy
from immutavault.state import StateDB


def test_named_policy_marks_replication_degraded(monkeypatch):
    events = []
    engine = SimpleNamespace(
        backup_all=lambda dry_run: [
            {"vm": "app01", "status": "success"},
            {"vm": "db01", "status": "degraded",
             "replicas": {"offsite": {"status": "failed"}}},
        ],
        state=SimpleNamespace(audit=lambda *a: events.append(a)),
    )
    monkeypatch.setattr(policy, "_PolicyConfigView", lambda cfg, p: None)
    monkeypatch.setattr(policy, "CertifiedBackupEngine", lambda cfg: engine)
    runner = policy.ProtectionPolicyRunner(SimpleNamespace())
    runner.policy = lambda pid: SimpleNamespace(
        id=pid, name="Daily", enabled=True, immutable_days=30,
        replica_targets=("offsite",))
    result = runner.run("daily", actor="operator")
    assert result["failed"] == 1
    assert result["degraded"] == 1
    assert events[0][1] == "policy.backup.partial_failure"


def test_job_summary_surfaces_degraded(tmp_path: Path):
    state = StateDB(str(tmp_path / "state.db"))
    job = state.start_job("vmware", "vm-uuid", "dc01")
    state.finish_job(job, status="degraded", snapshot_id="snapshot",
                     error="replica/verification incomplete")
    summary = state.status_summary()
    assert summary["jobs"]["degraded"] == 1
    assert summary["last_failed_backup"]["vm_name"] == "dc01"
    assert summary["last_failed_backup"]["status"] == "degraded"
