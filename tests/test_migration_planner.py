from immutavault.migration_planner import Destination, VM, plan_vm


def test_ready_is_still_not_approved():
    vm = VM("test", 1024, 4096, ("vlan2",), source_id="vm-123")
    dst = Destination("new", 2048, 8192, ("vlan2-new",))
    plan = plan_vm(vm, dst, {"vlan2": "vlan2-new"}, backup_verified=True,
                   recovery_test_passed=True, same_hypervisor_family=True)
    assert plan.ready
    assert plan.as_dict()["approved_to_execute"] is False


def test_unmapped_network_blocks():
    vm = VM("test", 1024, 4096, ("vlan401",), source_id="vm-123")
    dst = Destination("new", 2048, 8192, ("vlan2-new",))
    plan = plan_vm(vm, dst, {}, backup_verified=True,
                   recovery_test_passed=True, same_hypervisor_family=True)
    assert not plan.ready
    assert not next(c for c in plan.checks if c["name"] == "network_mapping")["passed"]


def test_uncertified_cross_hypervisor_blocks():
    vm = VM("test", 1024, 4096, ("vlan2",), source_id="vm-123")
    dst = Destination("new", 2048, 8192, ("vlan2",))
    plan = plan_vm(vm, dst, {"vlan2": "vlan2"})
    assert not plan.ready
    assert not next(c for c in plan.checks if c["name"] == "certified_transport")["passed"]
