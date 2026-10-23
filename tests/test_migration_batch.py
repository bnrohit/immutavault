"""A bulk request is not an unattended migration or an approval bypass."""
from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from immutavault import migration_batch
from immutavault.migration_batch import MigrationBatchError, create_migration_requests


def make_plan(count=3):
    rows = []
    for i in range(count):
        rows.append({
            "source_vm_id": f"vm-{i}", "source_name": f"testvm{i}",
            "target_name": f"migrated-testvm{i}", "snapshot_id": f"point-{i}",
            "destination_host": "host", "ready_for_operator_review": True,
            "checks": [{"code": "protection_evidence", "passed": True}],
        })
    report = {
        "schema_version": 1, "source_platform": "old", "destination_platform": "new",
        "source_family": "xcpng", "destination_family": "xcpng",
        "ready_for_operator_review": True, "approved_to_execute": False,
        "global_checks": [{"code": "storage_multipath_verified", "passed": True}],
        "vms": rows,
    }
    report["plan_sha256"] = sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


class FakeState:
    def __init__(self):
        self.requests = []
        self.audits = []
    def list_restore_requests(self, limit=2000):
        return self.requests[:limit]
    def get_point(self, snapshot):
        idx = snapshot.rsplit("-", 1)[1]
        return {"snapshot_id": snapshot, "vm_id": f"vm-{idx}",
                "platform": "old", "verified": 1, "suspicious": 0}
    def audit(self, *args):
        self.audits.append(args)


class FakeEngine:
    def __init__(self, tmp_path):
        self.state = FakeState()
        self.cfg = SimpleNamespace(
            protection=SimpleNamespace(require_four_eyes_restore=True),
            repository=SimpleNamespace(lock_file=str(tmp_path / "mutex")),
        )


def fake_create(plan, source_id, *, engine, requester):
    row = next(v for v in plan["vms"] if v["source_vm_id"] == source_id)
    request_id = len(engine.state.requests) + 1
    engine.state.requests.append({
        "id": request_id,
        "target_platform": plan["destination_platform"],
        "target_name": row["target_name"],
        "snapshot_id": row["snapshot_id"],
        "options_json": json.dumps({"migration_plan_sha256": plan["plan_sha256"]}),
        "status": "pending_approval",
    })
    return {"request_id": request_id}


def test_batch_default_dry_run_requires_no_connection():
    response = create_migration_requests(make_plan(), requester="operator")
    assert response["dry_run"] is True
    assert not response["changes_made"]
    assert len(response["planned_requests"]) == 3


def test_batch_creates_separate_idempotent_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(migration_batch, "create_restore_request", fake_create)
    engine = FakeEngine(tmp_path)
    result = create_migration_requests(
        make_plan(), engine=engine, execute=True, requester="operator")
    assert result["status"] == "REQUESTED"
    assert len(result["created"]) == 3
    assert len(result["requests"]) == 3
    assert result["requests"][0] == {"source_vm_id": "vm-0", "request_id": 1}
    assert result["approval_to_execute"] is False
    assert result["hypervisor_changes"] is False
    again = create_migration_requests(
        make_plan(), engine=engine, execute=True, requester="operator")
    assert again["created"] == []
    assert len(again["already_exists"]) == 3
    assert len(engine.state.requests) == 3


def test_request_limit_can_resume_without_duplicates(monkeypatch, tmp_path):
    monkeypatch.setattr(migration_batch, "create_restore_request", fake_create)
    engine = FakeEngine(tmp_path)
    plan = make_plan(4)
    first = create_migration_requests(
        plan, engine=engine, execute=True, requester="operator", max_new_requests=2)
    assert first["status"] == "MORE_TO_REQUEST"
    assert first["remaining_new_requests"] == 2
    second = create_migration_requests(
        plan, engine=engine, execute=True, requester="operator", max_new_requests=2)
    assert second["status"] == "REQUESTED"
    assert len(second["created"]) == 2
    assert len(engine.state.requests) == 4


def test_missing_point_blocks_entire_batch_before_any_mutations(monkeypatch, tmp_path):
    monkeypatch.setattr(migration_batch, "create_restore_request", fake_create)
    engine = FakeEngine(tmp_path)
    original = engine.state.get_point
    engine.state.get_point = lambda snapshot: None if snapshot == "point-2" else original(snapshot)
    with pytest.raises(MigrationBatchError, match="recovery point"):
        create_migration_requests(make_plan(), engine=engine, execute=True, requester="operator")
    assert engine.state.requests == []


def test_conflicting_existing_name_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(migration_batch, "create_restore_request", fake_create)
    engine = FakeEngine(tmp_path)
    engine.state.requests.append({
        "id": 1, "target_platform": "new", "target_name": "migrated-testvm0",
        "snapshot_id": "OTHER", "options_json": "{}"})
    with pytest.raises(MigrationBatchError, match="another plan"):
        create_migration_requests(make_plan(), engine=engine, execute=True, requester="operator")
    assert len(engine.state.requests) == 1


def test_partial_creation_error_is_reported_and_rerunnable(monkeypatch, tmp_path):
    engine = FakeEngine(tmp_path)
    calls = []
    def fail_second(plan, source_id, *, engine, requester):
        calls.append(source_id)
        if source_id == "vm-1":
            raise OSError("private details never exposed")
        return fake_create(plan, source_id, engine=engine, requester=requester)
    monkeypatch.setattr(migration_batch, "create_restore_request", fail_second)
    report = create_migration_requests(make_plan(), requester="operator",
                                       engine=engine, execute=True)
    assert report["status"] == "PARTIAL_FAILURE"
    assert report["failed"]["error_type"] == "OSError"
    assert "private details" not in str(report)
    assert len(report["created"]) == 1
    assert calls == ["vm-0", "vm-1"]


@pytest.mark.parametrize("kwargs", [
    {"max_new_requests": 0}, {"max_new_requests": True},
    {"requester": ""}, {"max_new_requests": 101},
])
def test_invalid_batch_options_rejected(kwargs):
    values = {"requester": "operator", **kwargs}
    with pytest.raises(MigrationBatchError):
        create_migration_requests(make_plan(), **values)


def test_same_family_enforced():
    plan = make_plan()
    plan["destination_family"] = "vmware"
    plan.pop("plan_sha256")
    plan["plan_sha256"] = sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(MigrationBatchError, match="XCP-ng"):
        create_migration_requests(plan, requester="operator")
