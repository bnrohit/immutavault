"""Batch staging never implies VM boot or production cutover."""
from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from immutavault import migration_stage_batch
from immutavault.migration_stage_batch import StagingBatchBlocked, stage_approved_batch


def plan():
    rows = [
        {"source_vm_id": f"vm{i}", "source_name": f"VM{i}",
         "target_name": f"new-VM{i}", "snapshot_id": f"snap{i}",
         "ready_for_operator_review": True,
         "checks": [{"code": "protection_evidence", "passed": True}]}
        for i in range(3)
    ]
    result = {
        "schema_version": 1,
        "source_platform": "old", "destination_platform": "new",
        "source_family": "xcpng", "destination_family": "xcpng",
        "ready_for_operator_review": True, "approved_to_execute": False,
        "global_checks": [{"code": "iscsi_paths_verified", "passed": True}],
        "vms": rows,
    }
    result["plan_sha256"] = sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def reqs():
    return {"requests": [
        {"source_vm_id": f"vm{i}", "request_id": i + 1}
        for i in range(3)
    ]}


class FakeStager:
    calls = []
    fail_preflight = None
    fail_stage = None

    def __init__(self, engine, report):
        self.engine = engine
        self.report = report

    def _row_for_request(self, request_id):
        i = request_id - 1
        return {"status": "approved"}, self.report["vms"][i]

    def _validate_live_destination(self, row):
        FakeStager.calls.append(("preflight", row["source_vm_id"]))
        if FakeStager.fail_preflight == row["source_vm_id"]:
            raise ValueError("unsafe target")

    def stage(self, request_id, *, actor, execute):
        FakeStager.calls.append(("stage", request_id))
        assert execute
        if FakeStager.fail_stage == request_id:
            raise RuntimeError("simulated import failure")
        return {
            "status": "STAGED_POWERED_OFF",
            "destination_vm_uuid": f"new-uuid-{request_id}",
        }


@pytest.fixture
def mock_stager(monkeypatch, tmp_path):
    FakeStager.calls = []
    FakeStager.fail_stage = None
    FakeStager.fail_preflight = None
    monkeypatch.setattr(migration_stage_batch, "XCPNGMigrationStager", FakeStager)
    return SimpleNamespace(cfg=SimpleNamespace(
        repository=SimpleNamespace(lock_file=str(tmp_path / "lock"))))


def test_preview_does_not_touch_infrastructure():
    r = stage_approved_batch(plan(), reqs(), actor="admin")
    assert r["dry_run"] is True
    assert r["hypervisor_changes"] is False
    assert r["approved_to_execute"] is False


def test_sequential_stages_are_powered_off_with_precheck(mock_stager):
    r = stage_approved_batch(plan(), reqs(), actor="operator",
                             engine=mock_stager, execute=True)
    assert r["status"] == "STAGED_BATCH"
    assert len(r["staged"]) == 3
    assert r["production_cutover_performed"] is False
    assert r["source_vms_unchanged"] is True
    assert [x[0] for x in FakeStager.calls] == [
        "preflight", "preflight", "preflight", "stage", "stage", "stage"]


def test_unsafe_third_preflight_prevents_first_write(mock_stager):
    FakeStager.fail_preflight = "vm2"
    with pytest.raises(ValueError, match="unsafe target"):
        stage_approved_batch(plan(), reqs(), actor="operator",
                             engine=mock_stager, execute=True)
    assert all(event[0] == "preflight" for event in FakeStager.calls)


def test_partial_import_failure_stops_remaining(mock_stager):
    FakeStager.fail_stage = 2
    report = stage_approved_batch(plan(), reqs(), actor="operator",
                                  engine=mock_stager, execute=True)
    assert report["status"] == "PARTIAL_FAILURE"
    assert len(report["staged"]) == 1
    assert report["failed"]["request_id"] == 2
    assert ("stage", 3) not in FakeStager.calls


def test_batch_limit_does_not_trigger_extra_jobs(mock_stager):
    result = stage_approved_batch(plan(), reqs(), actor="operator",
                                  engine=mock_stager, execute=True, max_jobs=1)
    assert result["status"] == "MORE_TO_STAGE"
    assert len(result["staged"]) == 1
    assert result["remaining_requests"] == 2
    assert ("stage", 2) not in FakeStager.calls


@pytest.mark.parametrize("requests", [
    {"requests": []},
    {"requests": [{"source_vm_id": "vm0", "request_id": 1},
                  {"source_vm_id": "vm0", "request_id": 2}]},
    {"requests": [{"source_vm_id": "vm0", "request_id": 1},
                  {"source_vm_id": "vm1", "request_id": 1}]},
    {"requests": [{"source_vm_id": "out-of-plan", "request_id": 2}]},
    {"requests": [{"source_vm_id": "vm0", "request_id": True}]},
    {"no_requests": []},
])
def test_invalid_request_manifest_never_stages(requests, mock_stager):
    with pytest.raises(StagingBatchBlocked):
        stage_approved_batch(plan(), requests, actor="operator",
                             engine=mock_stager, execute=True)
    assert FakeStager.calls == []


def test_wrong_request_to_vm_prevents_changes(monkeypatch, mock_stager):
    class Wrong(FakeStager):
        def _row_for_request(self, request_id):
            return {"status": "approved"}, self.report["vms"][1]
    monkeypatch.setattr(migration_stage_batch, "XCPNGMigrationStager", Wrong)
    with pytest.raises(StagingBatchBlocked, match="different VM"):
        stage_approved_batch(plan(), reqs(), actor="operator",
                             engine=mock_stager, execute=True)
    assert FakeStager.calls == []
