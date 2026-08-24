# Production acceptance checklist

A green CI/release suite proves the software tree is internally consistent. It does **not** prove that a particular vCenter, Proxmox cluster, XCP-ng pool, S3 account, firewall, NAS or DR network is correctly configured. Run this checklist before production enablement and after material infrastructure changes.

## Phase 1 - appliance

1. `sudo ./scripts/preflight.sh` reports no failures.
2. `./scripts/release_check.sh` passes on the exact release checkout used for installation.
3. `systemctl --failed` is empty after installation.
4. `immutavault hardware` reports expected CPU/RAM/storage architecture.
5. Vault DNS, NTP, TLS trust and backup NIC paths are verified.
6. Repository storage has adequate capacity and survives a reboot/mount cycle.
7. `/etc/immutavault/immutavault.env` is mode 0640 or stricter and is not included in Git or ordinary-user-accessible backups.
8. An off-site/second-failure-domain copy exists before important production workloads are declared protected.

## Phase 2 - repository/data plane

Before production schedules are enabled, run:

```bash
sudo ./scripts/data_plane_acceptance.sh
```

It creates a tiny encrypted test snapshot, restores and hashes it, proves the network writer cannot forget it, then removes only the disposable snapshot metadata through the privileged local maintenance path.

Acceptance gates:

1. `immutavault doctor` passes.
2. Primary REST repository authentication succeeds over TLS.
3. A test backup can be written through the network append-only identity.
4. The same identity can read/restore that snapshot.
5. A delete/forget attempt using the network writer is rejected by append-only rest-server.
6. The root-only local maintenance identity can run retention when policy makes a point eligible.
7. `immutavault verify` passes.
8. `immutavault audit-verify` returns `valid: true`.

## Phase 3 - each hypervisor family actually used

Use a disposable/non-critical VM first.

### VMware - strict native VDDK/CBT

For deployments requiring genuine native incrementals, production policy should be explicit:

```yaml
mode: vddk
options:
  vddk_helper: /usr/local/bin/immutavault-vddk
  incremental_strict: true
  incremental_fallback: false
  quiesce: true
  quiesce_fallback_crash_consistent: false
```

Broadcom VDDK is not bundled. The authorized helper/provider must be installed and supported separately.

Before declaring the VMware workload protected, prove:

- vCenter CA trust is valid and `insecure: false`.
- A dedicated account has only the privileges required by the selected transport and restore workflow.
- The configured helper resolves and reports protocol-v1 capabilities including `cbt`, `backup`, and `restore`.
- The first native baseline succeeds and creates a valid layout/checkpoint.
- A second backup completes as native incremental and provider telemetry shows changed-range behavior.
- `incremental_strict: true` plus helper removal causes the backup/dry-run to fail; no hot-clone fallback point is created.
- Disabling or invalidating CBT causes strict mode to fail closed.
- A change-ID/generation reset causes strict mode to fail closed.
- An unsupported disk causes strict mode to fail closed.
- A provider response with an unknown reason fails closed even if it says `fallback_safe: true`.
- A provider response omitting `fallback_safe` fails closed.
- Invalid provider JSON, corrupt/missing layout/checkpoint, and unexpected provider exceptions fail closed.
- An injected in-progress provider failure invalidates the per-VM native cache before the next run.
- A strict native failure does not advance a trusted checkpoint/change-ID chain.
- Recovery-point verification passes.
- Restore from a native layout to a **new** VM name succeeds with the provider/helper installed.
- The restored VM boots in an isolated network and passes application checks.

If non-strict fallback is intentionally enabled, test it separately. An allow-listed provider-stage reason must still include `fallback_safe: true`; unknown or ambiguous states must fail closed. Any allowed full fallback must be visibly marked as a fallback-full transport rather than presented as native incremental.

### VMware - explicit full transport

If the deployment deliberately uses `mode: hot-clone-export`, prove:

- VMware Tools quiesce succeeds when strict application consistency is required.
- A backup completes without leaving an Immutavault snapshot or temporary clone behind.
- Datastore headroom is sufficient for the temporary clone/export workflow.
- Recovery-point verification passes.
- Restore to a new VM name succeeds and boots isolated.

### Proxmox

- Dedicated SSH identity works non-interactively.
- `vzdump --mode snapshot` succeeds for the test VM/CT.
- Temporary archives are cleaned even after a deliberately interrupted test.
- New-VMID restore succeeds and existing VMIDs are refused.
- Restored workload boots isolated and application checks pass.

### XCP-ng

- Dedicated SSH identity works non-interactively.
- `snapshot-export` creates/removes the temporary snapshot correctly and exports it through the supported snapshot/template path.
- XVA imports as a temporary template, `vm-install` creates the bootable recovery VM on the configured SR, and the imported template is removed.
- Restored workload boots isolated and application checks pass.

## Phase 4 - replica/cloud/NAS

For every enabled replica:

1. `replica-init` succeeds.
2. `storage-targets` reports healthy.
3. A real snapshot copies to that target.
4. Destination snapshot listing confirms the exact snapshot ID exists.
5. Restore **from that replica** succeeds.
6. For S3 Object Lock providers, provider retention status is read back and deletion is tested with a disposable object/snapshot according to provider policy.
7. For R2, native Bucket Lock Date rules are verified on persistent restic namespaces, the horizon is refreshed after another successful copy, and transient `locks/` is not retention-locked. Operators understand R2 locks are admin-mutable and are not S3 Compliance Object Lock.
8. For NFS/SMB, unmount/remount and server reboot behavior is tested and mount dependency is monitored.

## Phase 5 - scheduler/reboot

1. Enable backup/state-backup/health/retention/verify timers.
2. Reboot the vault.
3. Confirm persistent timers resume and repository/portal restart.
4. Confirm a scheduled backup occurs after reboot.
5. Confirm five-minute state backups are being created and retained.
6. Restore the catalog from one state backup on a standby/test node and verify the audit chain.

## Phase 6 - DR (only if used)

Keep `auto_failover: false` initially.

1. Controller survives loss of the primary site: controller is at DR/third site or the warm-standby promotion procedure is tested.
2. Primary and DR VTEPs have routed underlay reachability and correct MTU.
3. VXLAN traffic is restricted/encrypted as required by the WAN design.
4. FRR OSPF neighbor/route behavior is validated on a disposable recovery VLAN.
5. Only one site owns the recovery gateway IP at a time.
6. Primary failure probes are independent enough to avoid one shared failure domain.
7. Fencing command actually isolates primary compute/network.
8. Fencing verification independently proves isolation.
9. `dr-preflight` passes before a failover drill.
10. Off-site recovery points meet RPO and are verified/non-suspicious.
11. Controlled `dr-promote` restores workloads, moves route ownership, boots in dependency order and passes health checks.
12. Confirm there are no duplicate same-IP VMs at the two sites.
13. Generate application changes while DR is active.
14. Controlled `dr-failback` captures final DR state, restores primary, moves routing back and preserves those changes.
15. Repeat the drill at least twice before considering `auto_failover: true`.

## Production acceptance record

Record release version/commit, hypervisor versions, VDDK/provider/helper version where applicable, server model/firmware, storage target versions, network diagram, test VM names, RPO/RTO observed, native changed-bytes/source-bytes-read evidence for VMware incremental testing, tester/approver, date and evidence/log locations.

Re-run the affected portions after major hypervisor, VDDK/helper, storage, firmware, network or Immutavault upgrades.
