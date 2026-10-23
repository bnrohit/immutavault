# Cluster Migration — experimental v1.2 development

**Do not use unattended on production.** This branch adds topology discovery, a fail-closed capacity and VLAN/SR planner, four-eyes recovery requests, and XCP-ng-to-XCP-ng POWERED-OFF recovery staging. It is NOT a live/warm migration engine. The existing immutable backup and restore engine remains authoritative.

## Capability and readiness

| Feature | Current implementation |
|---|---|
| XCP-ng discovery | Read-only xe data gathering; host enablement is NOT health certification |
| VMware discovery | VM identities only; hosts, resource use and distributed networks not certified |
| Preflight | Validated snapshots, per-host RAM, shared SR space and access, NIC VLANs, protection, firmware and special devices |
| Approval | Existing immutable-recovery-point and four-eyes restore request process |
| XCP-ng staging | After separate approval, restore NEW target VM to explicit SR, keep halted, remap NICs by exact device number |
| Cutover/final delta/fencing/auto-failback | NOT IMPLEMENTED: separate operator runbook and acceptance |
| Cross-family migration | Blocked unless independently certified and supported by existing V2V policy |

An offline plan that passes is **ready for operator review**, never authorized to execute by itself. Do not interpret a manually checked evidence field as live proof.

## Quickstart on a test controller

Set up the two separate pools as distinct named XCP-ng platforms in the existing Immutavault configuration. Follow PRODUCTION_ACCEPTANCE.md and test all steps with an expendable VM before ever considering critical production systems.

Capture source and destination:

    immutavault --config /etc/immutavault/immutavault.yml migration-discover --platform old-xcp --output ./source.json
    immutavault --config /etc/immutavault/immutavault.yml migration-discover --platform new-xcp --output ./destination.json

These files are private and created with mode 0600. Discovery captures host/VM/network/SR identities and best-effort topology. Unknown VLAN is -1 and must be explicitly verified. The collector labels firmware unknown and marks every VM special device profile unreviewed. Verify the source VM's firmware, whether passthrough or specialized NICs are present, every target SR PBD, VLAN and SAN multipath. Do not simply delete these warnings without physical verification.

Create a private selection.json listing exact VM IDs, source-network-ID to target-network-ID mapping, source-SR-ID to target-SR-ID mapping, fresh backup snapshot IDs, recovery-test and application consistency evidence. See config/examples/migration-selection.example.json.

    immutavault --config /etc/immutavault/immutavault.yml migration-plan --source ./source.json --destination ./destination.json --selection ./selection.json --output ./plan.json

Exit status 0: ready for review. Exit 1: blocked. Exit 2: invalid input. Output includes blockers and a SHA-256 plan fingerprint. Output files are never overwritten; re-run with a new filename after changes.

Create a request for one disposable pilot VM, initially without changes:

    immutavault --config /etc/immutavault/immutavault.yml migration-request --source ./source.json --destination ./destination.json --selection ./selection.json --plan ./plan.json --vm-id SOURCE-VM-UUID --requester REQUESTER

After manual preflight signoff, the same command with --execute-request creates a restore request in the control-plane database. **It neither imports nor powers on a VM.** The request must then be independently approved using the established restore-approve CLI and a different named approver.

Inspect an approved XCP-ng staging request (read only):

    immutavault --config /etc/immutavault/immutavault.yml migration-stage --source ./source.json --destination ./destination.json --selection ./selection.json --plan ./plan.json --request-id REQUEST-ID --actor OPERATOR

After independent approval, adding --execute-stage is an actual target-side change: it restores a fresh NEW VM from the verified immutable recovery point onto the mapped shared target SR, checks the target is Halted and remaps VIF device numbers onto their explicitly approved destination networks. No production VM power on/off is performed. A failed remap may leave a powered-off partial target requiring operator attention. Preserve it for investigation; do not boot or blindly destroy it.

**This staged VM is a point-in-time BACKUP, not a final synchronized copy.** For active databases/AD/routers, do not start the target on the same production network as a running source. A production cutover requires independent final-copy/delta design, application consistency testing, source fencing, approved boot and application/client validation. This tool does not execute those steps.

## Snapshot schema

The version-1 source/destination document contains timezone-aware captured_at, family, platform_name, cluster_id; hosts with id/name/healthy/free_memory_bytes; networks with id/name/vlan/accessible_hosts; storage with id/name/free_bytes/shared/accessible_hosts; and vms with id/name/memory_bytes/power_state/firmware/nics/disks/special_devices. Destination also includes supported_firmware confirmed by an operator.

The selection file contains vm_ids, network_map, storage_map, reserve_percent (default 20), target_prefix and per-VM recovery_points keyed by exact source VM ID. Each recovery point requires snapshot_id, verified=true, suspicious=false, isolated_restore_test_passed=true and guest_consistency_approved=true. Explicit evidence requires switch_network_verified, storage_multipath_verified, iscsi_paths_verified and cluster_access_verified. For cross-family conversion a certified_conversion_id plus certification_validated are required but real V2V engine policy still independently governs requests.

These JSON values are operator statements and can be stale or incorrect. Revalidate against real XCP-ng/vCenter/Compellent data and the Immutavault state DB before every operation. Plan fingerprinting detects changes in input and outputs but is not a cryptographic authorization signature.

## Non-negotiable safeguards

- Old pool, source VMs, immutable recovery points and source SAN LUNs are never automatically overwritten, deleted, shut down or reformatted.
- Unknown guest firmware, an unknown VLAN, special NICs, missing restore test, insufficient per-host RAM or unverified shared target SR block planning.
- Shared target SR access is required on *all* target hosts; two separate iSCSI fabrics and multipath must be verified live.
- Multi-VIF workloads including monitoring/SPAN or isolated NAC interfaces require individually verified NIC mapping. Preserve every VLAN and network/security role.
- The active orchestration appliance should not migrate itself as part of its own job.
- The new powered-off target must never be started while the source can still serve the same IP/MAC or write to the same data.
- Do not claim zero downtime or instant rollback. Preserve the old pool until the destination has been proven and signed off.
- This branch does not include VMware production staging or VMware-to-XCP-ng V2V; do not bypass certification gates.
- Keep real configuration snapshots, hostnames, private addressing, Compellent serials, credentials, approver IDs and migration plans out of the public Git repository.

## Production release gates

All test suites and package checks, data-plane acceptance, live vendor tests, isolated pilot restore/boot, failures during import and VIF remapping, SAN multipath, final sync and split-brain simulations, documented emergency recovery, and operations sign-off must pass. See PRODUCTION_ACCEPTANCE.md. Do not treat green unit tests alone as proof of safe cross-pool migration.

Xen Orchestra has its own supported cross-pool VM Copy and licensed Warm Migration capabilities. Those may better fit an urgent minimal-downtime cutover; do not imply this Immutavault development branch implements them.
