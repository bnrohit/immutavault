"""Fail-closed, read-only cluster migration planning.

No hypervisor operations are performed here. Inputs are discovery snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class MigrationBlocked(ValueError):
    pass


@dataclass(frozen=True)
class VM:
    name: str
    memory_bytes: int
    disk_bytes: int
    networks: tuple[str, ...]
    firmware: str = "bios"
    special_devices: tuple[str, ...] = ()
    source_id: str = ""


@dataclass(frozen=True)
class Destination:
    name: str
    free_memory_bytes: int
    free_storage_bytes: int
    networks: tuple[str, ...]
    supported_firmware: tuple[str, ...] = ("bios", "uefi")
    migration_certified: bool = False


@dataclass
class Plan:
    source: str
    destination: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    approved_to_execute: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(c["passed"] for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "ready_for_operator_review": self.ready,
            "approved_to_execute": False,
            "checks": self.checks,
        }


def plan_vm(vm: VM, destination: Destination, network_map: dict[str, str],
            *, backup_verified: bool = False, recovery_test_passed: bool = False,
            same_hypervisor_family: bool = False) -> Plan:
    """Produce a conservative report; never infer approval or initiate cutover."""
    if not vm.name or not destination.name:
        raise MigrationBlocked("VM and destination names are required")
    plan = Plan(vm.name, destination.name)

    def check(name: str, passed: bool, reason: str) -> None:
        plan.checks.append({"name": name, "passed": bool(passed), "reason": reason})

    check("verified_immutable_backup", backup_verified, "Verified recovery point required")
    check("recovery_test", recovery_test_passed, "Successful isolated restore required")
    check("certified_transport", same_hypervisor_family or destination.migration_certified,
          "Cross-hypervisor migration requires certified conversion")
    check("memory_capacity", vm.memory_bytes > 0 and vm.memory_bytes <= destination.free_memory_bytes,
          "Destination must have sufficient unreserved RAM")
    check("storage_capacity", vm.disk_bytes > 0 and vm.disk_bytes <= destination.free_storage_bytes,
          "Destination must have sufficient unreserved storage")
    missing = sorted(set(vm.networks) - set(network_map))
    unknown = sorted({network_map[n] for n in vm.networks if n in network_map}
                     - set(destination.networks))
    check("network_mapping", not missing and not unknown,
          f"Unmapped source networks: {missing}; absent target networks: {unknown}")
    check("firmware", vm.firmware in destination.supported_firmware,
          "Firmware must be supported on destination")
    check("special_devices", not vm.special_devices,
          f"Manual handling required: {list(vm.special_devices)}")
    check("source_identity", bool(vm.source_id),
          "Immutable source VM identifier required to prevent name collisions")
    return plan
