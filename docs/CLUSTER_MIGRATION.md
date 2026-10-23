# Cluster Migration — safety-first design (prototype)

This branch introduces a **read-only planning library**, not an operational migration engine. No production VMs, hypervisors, SAN mappings, or backup repositories are modified.

## Intended wizard

1. Register old and new vCenter/ESXi or XCP-ng pools with read-only discovery credentials.
2. Inventory exact VM IDs, guest OS, virtual hardware, firmware, snapshots, disks, storage SR/datastores, VIFs and VLANs, dependencies, RAM, CPU and free target capacity.
3. Establish immutable verified backup and isolated restore-test evidence per VM.
4. Map source networks to target networks and validate VLAN reachability; do not guess from network names.
5. Validate storage presentation and multipath, including shared LUN visibility to every target host.
6. Flag special devices (PCI passthrough, TPM, encrypted disks, monitoring/SPAN ports), incompatible guest tools, secure boot and conversion limitations.
7. Generate per-VM staged plans with explicit maintenance windows, source power-off approval, target power-on approval, health checks and rollback.
8. Stage copies while old VMs remain running **only** where the actual transport supports a consistent copy. Freeze writes or stop source at cutover, confirm final synchronization and fence the source before target boot.
9. Record signed operator approvals and append-only audit events. Never execute a migration based solely on a passing preflight.
10. Keep old cluster available for rollback until application owners sign off.

## GMCS migration considerations

- Old XCP-ng production pool must remain operational; the new xcpng154–156 pool is separate.
- Do not present, initialize or format old Compellent LUNs as new volumes.
- New XCP-ng iSCSI sessions alone do not prove that a shared SR or multipath is ready.
- Keep XOCE on xcpng155 untouched.
- Preserve exact VIF mappings including VLAN 2, 21, 32, 200, 252, 401–403, 599, 1000 and special native VLAN 1.
- The old broker SPAN interface is not interchangeable with a new host's iSCSI port.
- Verify VMware distributed port groups, LACP lag1, Po151–153 and VLAN 248/249 before VMware cutover.
- Use certified conversion paths only. Existing built-in VMware-to-Proxmox V2V does **not** certify VMware-to-XCP-ng.
- Never claim zero downtime: outage depends on transport, final synchronization and guest/application behavior.

## Library usage

```python
from immutavault.migration_planner import VM, Destination, plan_vm
plan = plan_vm(
    VM("pilot", 4 * 2**30, 80 * 2**30, ("VLAN2",), source_id="source-uuid"),
    Destination("xcpng154-156", 16 * 2**30, 200 * 2**30, ("XOCE-VLAN2",)),
    {"VLAN2": "XOCE-VLAN2"},
    backup_verified=True,
    recovery_test_passed=True,
    same_hypervisor_family=True,
)
print(plan.as_dict())
```

Even when `ready_for_operator_review` is true, `approved_to_execute` remains false. A later execution engine must enforce independent authenticated approval and revalidate all checks immediately before cutover.
