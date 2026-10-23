from copy import deepcopy
from hashlib import sha256
import json

import pytest

from immutavault.migration_reporting import InvalidMigrationPlan, summarize_migration_plan


def signed_plan():
    report = {
        "schema_version": 1,
        "source_platform": "old-xcp",
        "destination_platform": "new-xcp",
        "ready_for_operator_review": True,
        "approved_to_execute": False,
        "global_checks": [{"code": "storage_multipath_verified", "passed": True}],
        "vms": [{
            "source_name": "pilot", "source_vm_id": "vm-1", "destination_host": "154",
            "checks": [{"code": "exact_network_mapping", "passed": True}],
        }],
    }
    report["plan_sha256"] = sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def test_readable_ready_plan_never_authorizes_execute():
    data = summarize_migration_plan(signed_plan())
    assert data["status"] == "READY_FOR_OPERATOR_REVIEW"
    assert data["approval_to_execute"] is False
    assert data["total_vms"] == 1


def test_tampered_plan_cannot_hide_blockers():
    report = signed_plan()
    report["vms"][0]["checks"][0]["passed"] = False
    with pytest.raises(InvalidMigrationPlan, match="integrity"):
        summarize_migration_plan(report)


def test_blocked_storage_produces_action():
    report = signed_plan()
    report["global_checks"][0]["passed"] = False
    report["ready_for_operator_review"] = False
    report.pop("plan_sha256")
    report["plan_sha256"] = sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    summary = summarize_migration_plan(report)
    assert summary["status"] == "BLOCKED"
    assert summary["blocked_vms"] == 1
    assert any("multipath" in action for action in summary["next_actions"])


def test_forged_digest_is_not_accepted():
    report = signed_plan()
    report["plan_sha256"] = "0" * 64
    with pytest.raises(InvalidMigrationPlan):
        summarize_migration_plan(report)


@pytest.mark.parametrize("bad", [{"vms": []}, {"global_checks": "bad"}])
def test_broken_plan_structure_is_rejected(bad):
    report = signed_plan()
    report.update(bad)
    report.pop("plan_sha256")
    report["plan_sha256"] = sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(InvalidMigrationPlan):
        summarize_migration_plan(report)
