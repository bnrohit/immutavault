"""Migration engine acceptance tests: no source/target writes are necessary."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import pytest

from immutavault.migration_workflow import (
    MigrationInputError, create_restore_request, plan_migration, validate_snapshot,
)


NOW = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)


def snapshots():
    captured = NOW.isoformat()
    source = {
        "schema_version": 1, "captured_at": captured, "platform_name": "old-xcp",
        "cluster_id": "old-pool", "family": "xcpng",
        "hosts": [{"id": "oldhost", "name": "old", "free_memory_bytes": 10**11, "healthy": True}],
        "networks": [{"id": "old-vlan2", "name": "Old VM LAN", "vlan": 2,
                      "accessible_hosts": ["oldhost"]}],
        "storage": [{"id": "old-sr", "name": "Old Compellent", "free_bytes": 10**12,
                     "shared": True, "accessible_hosts": ["oldhost"]}],
        "vms": [{"id": "uuid1", "name": "testvm", "memory_bytes": 4 * 2**30,
                 "power_state": "running", "firmware": "bios", "special_devices": [],
                 "nics": [{"network_id": "old-vlan2", "device": "0"}],
                 "disks": [{"sr_id": "old-sr", "size_bytes": 40 * 2**30}]}],
    }
    target = {
        "schema_version": 1, "captured_at": captured, "platform_name": "new-xcp",
        "cluster_id": "new-pool", "family": "xcpng",
        "hosts": [
            {"id": "new154", "name": "154", "free_memory_bytes": 16 * 2**30, "healthy": True},
            {"id": "new155", "name": "155", "free_memory_bytes": 16 * 2**30, "healthy": True},
        ],
        "networks": [{"id": "new-vlan2", "name": "XOCE-VLAN2", "vlan": 2,
                      "accessible_hosts": ["new154", "new155"]}],
        "storage": [{"id": "new-sr", "name": "New dedicated shared LUN",
                     "free_bytes": 400 * 2**30, "shared": True,
                     "accessible_hosts": ["new154", "new155"]}],
        "vms": [],
        "supported_firmware": ["bios"],
    }
    mapping = {
        "vm_ids": ["uuid1"], "network_map": {"old-vlan2": "new-vlan2"},
        "storage_map": {"old-sr": "new-sr"}, "target_prefix": "migrated-",
        "evidence": {
            "switch_network_verified": True, "storage_multipath_verified": True,
            "iscsi_paths_verified": True, "cluster_access_verified": True,
        },
        "recovery_points": {
            "uuid1": {"snapshot_id": "restic-001", "verified": True, "suspicious": False,
                      "isolated_restore_test_passed": True,
                      "guest_consistency_approved": True}
        },
    }
    return source, target, mapping


def plan():
    src, dst, mapping = snapshots()
    return plan_migration(src, dst, mapping, now=NOW)


def code(result, name, vm=True):
    checks = result["vms"][0]["checks"] if vm else result["global_checks"]
    return next(x for x in checks if x["code"] == name)["passed"]


def test_happy_path_is_still_never_approved():
    p = plan()
    assert p["ready_for_operator_review"] is True
    assert p["approved_to_execute"] is False
    assert p["vms"][0]["target_name"] == "migrated-testvm"
    assert len(p["plan_sha256"]) == 64
    assert p == plan()


def test_requires_real_destination_shared_storage():
    a, b, c = snapshots()
    b["storage"][0]["shared"] = False
    result = plan_migration(a, b, c, now=NOW)
    assert not code(result, "shared_storage_capacity")
    assert not result["ready_for_operator_review"]


def test_network_tag_translation_blocks():
    a, b, c = snapshots()
    b["networks"][0]["vlan"] = 3
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "exact_network_mapping")


def test_network_must_reach_placed_host():
    a, b, c = snapshots()
    b["networks"][0]["accessible_hosts"] = []
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "exact_network_mapping")


def test_memory_needs_single_host_not_pool_total():
    a, b, c = snapshots()
    a["vms"][0]["memory_bytes"] = 18 * 2**30
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "host_memory_capacity")


def test_block_missing_backup_restore_test_and_certification():
    a, b, c = snapshots()
    c["recovery_points"]["uuid1"]["isolated_restore_test_passed"] = False
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "protection_evidence")
    b["family"] = "vmware"
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "transport_supported", vm=False)


def test_stale_discovery_blocks():
    a, b, c = snapshots()
    a["captured_at"] = (NOW - timedelta(hours=3)).isoformat()
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "source_snapshot_fresh", vm=False)


def test_target_name_collision_blocks():
    a, b, c = snapshots()
    b["vms"] = [{"id": "another", "name": "MIGRATED-testvm",
                 "memory_bytes": 0, "firmware": "bios",
                 "nics": [], "disks": []}]
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "unique_target_name")


def test_special_devices_including_span_block():
    a, b, c = snapshots()
    a["vms"][0]["special_devices"] = ["physical_span_port"]
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "special_devices_reviewed")


def test_insufficient_per_sr_capacity_blocks():
    a, b, c = snapshots()
    b["storage"][0]["free_bytes"] = 40 * 2**30
    r = plan_migration(a, b, c, now=NOW)
    assert not code(r, "shared_storage_capacity")


@pytest.mark.parametrize("bad", [
    {"schema_version": 2},
    {"captured_at": "2026-10-23T18:00:00"},
    {"hosts": []},
    {"networks": [{"id": "bad", "name": "bad", "vlan": -2, "accessible_hosts": []}]},
])
def test_invalid_snapshot_fails_closed(bad):
    s, _, _ = snapshots()
    s.update(bad)
    with pytest.raises(MigrationInputError):
        validate_snapshot(s, now=NOW)


def test_same_cluster_migration_refused():
    a, b, c = snapshots()
    b["cluster_id"] = a["cluster_id"]
    with pytest.raises(MigrationInputError):
        plan_migration(a, b, c, now=NOW)


class FakeState:
    def __init__(self):
        self.audit_events = []
    def get_point(self, snapshot_id):
        return {"snapshot_id": "restic-001", "vm_id": "uuid1", "platform": "old-xcp",
                "platform_type": "xcpng", "verified": 1, "suspicious": 0}
    def audit(self, *args):
        self.audit_events.append(args)


class FakeEngine:
    def __init__(self, four_eyes=True):
        self.state = FakeState()
        self.cfg = type("Cfg", (), {"protection": type("Prot", (),
                    {"require_four_eyes_restore": four_eyes})()})()
        self.calls = []
    def request_restore(self, **kwargs):
        self.calls.append(kwargs)
        return 42


def test_request_only_uses_existing_approval_workflow():
    engine = FakeEngine()
    result = create_restore_request(plan(), "uuid1", engine=engine, requester="requester")
    assert result["execution_performed"] is False
    assert result["request_id"] == 42
    assert engine.calls[0]["target_name"] == "migrated-testvm"
    assert engine.calls[0]["options"]["migration_plan_sha256"] == plan()["plan_sha256"]


def test_request_fails_when_four_eyes_disabled():
    with pytest.raises(PermissionError):
        create_restore_request(plan(), "uuid1", engine=FakeEngine(four_eyes=False),
                               requester="operator")


def test_request_blocked_if_plan_not_ready():
    wrong = deepcopy(plan())
    wrong["ready_for_operator_review"] = False
    with pytest.raises(PermissionError):
        create_restore_request(wrong, "uuid1", engine=FakeEngine(), requester="operator")


def test_unverified_vlan_fails_in_plan_not_parse():
    a, b, c = snapshots()
    b["networks"][0]["vlan"] = -1
    result = plan_migration(a, b, c, now=NOW)
    assert not code(result, "exact_network_mapping")


def test_shared_storage_must_reach_all_hosts():
    a, b, c = snapshots()
    b["storage"][0]["accessible_hosts"] = ["new154"]
    result = plan_migration(a, b, c, now=NOW)
    assert not code(result, "shared_storage_capacity")


def test_management_appliance_blocked():
    a, b, c = snapshots()
    a["vms"][0]["name"] = "XOCE"
    result = plan_migration(a, b, c, now=NOW)
    assert not code(result, "destination_not_management_appliance")


def test_restore_request_pins_target_sr():
    engine = FakeEngine()
    create_restore_request(plan(), "uuid1", engine=engine, requester="operator")
    assert engine.calls[0]["options"]["sr_uuid"] == "new-sr"
