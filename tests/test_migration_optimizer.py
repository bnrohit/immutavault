"""Performance scheduling is planning only; these tests touch no hypervisor."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import pytest

from immutavault.migration_optimizer import (
    MigrationOptimizationError,
    optimize_migration_plan,
)


def signed(rows):
    report = {
        "schema_version": 1,
        "source_platform": "old-xcp",
        "destination_platform": "new-xcp",
        "source_family": "xcpng",
        "destination_family": "xcpng",
        "ready_for_operator_review": True,
        "approved_to_execute": False,
        "global_checks": [{"code": "storage_multipath_verified", "passed": True}],
        "vms": rows,
    }
    report["plan_sha256"] = sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def vm(i, *, host="host1", storage="sr1", disk=10 * 2**30):
    return {
        "source_vm_id": f"vm-{i}", "source_name": f"pilot-{i}",
        "target_name": f"migrated-pilot-{i}", "destination_host": host,
        "snapshot_id": f"snap-{i}", "ready_for_operator_review": True,
        "checks": [{"code": "exact_network_mapping", "passed": True}],
        "disks": [{"source_sr": "old", "target_sr": storage,
                   "size_bytes": disk, "valid": True}],
    }


def test_two_independent_storage_hosts_parallelize_without_bandwidth_magic():
    plan = signed([vm(1), vm(2, host="host2", storage="sr2")])
    out = optimize_migration_plan(plan, effective_mib_per_second=100)
    assert out["total_waves"] == 1
    assert len(out["waves"][0]["vms"]) == 2
    assert out["approved_to_execute"] is False
    assert out["parallel_stage_execution_implemented"] is False
    # Data-transfer component stays exactly the same; only overlap of setup
    # overhead is modeled, never doubling network bandwidth.
    assert 15 * 60 <= out["hypothetical_overhead_savings_seconds"] <= 15 * 60 + 1
    assert out["hypothetical_parallel_wave_seconds"] > 0


def test_same_storage_serializes_even_on_different_hosts():
    plan = signed([vm(1), vm(2, host="host2")])
    out = optimize_migration_plan(plan, max_parallel=8)
    assert out["total_waves"] == 2
    assert out["illustrative_saved_setup_seconds"] == 0


def test_same_host_serializes_even_on_distinct_storage():
    plan = signed([vm(1), vm(2, storage="sr2")])
    out = optimize_migration_plan(plan)
    assert out["total_waves"] == 2


def test_dag_dependencies_enforce_strict_different_wave():
    plan = signed([vm(1), vm(2, host="host2", storage="sr2")])
    out = optimize_migration_plan(plan, dependencies={"vm-2": ["vm-1"]})
    assert out["total_waves"] == 2
    assert out["waves"][0]["source_vm_ids"] == ["vm-1"]


def test_priority_order_is_deterministic():
    plan = signed([vm(1), vm(2, host="host2", storage="sr2"),
                   vm(3, host="host3", storage="sr3")])
    out = optimize_migration_plan(
        plan, max_parallel=1, priorities={"vm-3": 3, "vm-1": 0})
    assert [w["source_vm_ids"][0] for w in out["waves"]] == [
        "vm-3", "vm-2", "vm-1"]
    assert out == optimize_migration_plan(
        plan, max_parallel=1, priorities={"vm-3": 3, "vm-1": 0})


def test_multi_sr_disk_reserves_all_storage_resources():
    both = vm(1)
    both["disks"].append({"target_sr": "sr2", "size_bytes": 2**30, "valid": True})
    plan = signed([both, vm(2, host="host2", storage="sr2")])
    assert optimize_migration_plan(plan)["total_waves"] == 2


@pytest.mark.parametrize("kwargs", [
    {"max_parallel": 0},
    {"max_parallel": True},
    {"max_per_storage": 0},
    {"effective_mib_per_second": 0},
    {"stage_overhead_minutes": -1},
    {"priorities": {"no-such-vm": 3}},
    {"priorities": {"vm-1": True}},
    {"dependencies": {"vm-1": ["vm-1"]}},
    {"dependencies": {"vm-1": ["vm-2"], "vm-2": ["vm-1"]}},
])
def test_unsafe_configuration_blocks(kwargs):
    plan = signed([vm(1), vm(2, host="host2", storage="sr2")])
    with pytest.raises(MigrationOptimizationError):
        optimize_migration_plan(plan, **kwargs)


def test_plan_fingerprint_is_verified_before_scheduling():
    plan = signed([vm(1)])
    plan["vms"][0]["disks"][0]["size_bytes"] = 1
    with pytest.raises(MigrationOptimizationError, match="integrity"):
        optimize_migration_plan(plan)


def test_blocked_vm_cannot_be_scheduled():
    plan = signed([vm(1)])
    plan["global_checks"][0]["passed"] = False
    plan["ready_for_operator_review"] = False
    plan.pop("plan_sha256")
    plan["plan_sha256"] = sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(MigrationOptimizationError, match="blocked"):
        optimize_migration_plan(plan)


def test_no_assumed_cross_family_support():
    plan = signed([vm(1)])
    plan["destination_family"] = "vmware"
    plan.pop("plan_sha256")
    plan["plan_sha256"] = sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(MigrationOptimizationError, match="cross-family"):
        optimize_migration_plan(plan)
