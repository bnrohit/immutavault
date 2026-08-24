# Production acceptance checklist

A green CI/release suite proves the software tree is internally consistent. It does **not** prove that a particular vCenter, Proxmox cluster, XCP-ng pool, S3 account, firewall, NAS or DR network is correctly configured. Run this checklist before production enablement and after material infrastructure changes.

## Phase 1 - appliance

1. `sudo ./scripts/preflight.sh` reports no failures.
2. `./scripts/release_check.sh` passes on the release checkout used for installation.
3. `systemctl --failed` is empty after installation.
4. `immutavault hardware` reports expected CPU/RAM/storage architecture.
5. Vault DNS, NTP, TLS trust and backup NIC paths are verified.
6. Repository storage has adequate capacity and survives a reboot/mount cycle.
7. `/etc/immutavault/immutavault.env` is mode 0640 or stricter and not in Git/backups accessible to ordinary users.
8. An off-site/second-failure-domain copy exists before important production workloads are declared protected.

## Phase 2 - repository/data plane

Before production schedules are enabled, run the automated live data-plane test:

```bash
sudo ./scripts/data_plane_acceptance.sh
```

It creates a tiny encrypted test snapshot, restores and hashes it, proves the network writer cannot forget it, then removes only the disposable snapshot metadata via the privileged local maintenance path.


1. `immutavault doctor` passes.
2. Primary REST repository authentication succeeds over TLS.
3. A test backup can be written through the network append-only identity.
4. The same identity can read/restore that snapshot.
5. A delete/forget attempt using the network writer is rejected by append-only rest-server.
6. The root-only local maintenance identity can run retention when policy makes a point eligible.
7. `immutavault verify` passes.
8. `immutavault audit-verify` returns `valid: true`.

## Phase 3 - each hypervisor family actually used

Do these with a disposable/non-critical VM first.

### VMware

- vCenter CA trust is valid; `insecure: false`.
- Dedicated account has only the privileges necessary for inventory, snapshot, clone, export/import and power operations required by the deployment.
- `mode: hot-clone-export` is used for running production VMs.
- VMware Tools quiesce succeeds when strict application consistency is required.
- A backup completes without leaving an Immutavault snapshot or temporary clone behind.
- Recovery-point verification passes.
- Restore to a new VM name succeeds.
- Restored VM boots isolated from production and passes application checks.

### Proxmox

- Dedicated SSH identity works non-interactively.
- `vzdump --mode snapshot` succeeds for the test VM/CT.
- Temporary archives are cleaned even after a deliberately interrupted test.
- New-VMID restore succeeds and existing VMIDs are refused.
- Restored workload boots isolated and application checks pass.

### XCP-ng

- Dedicated SSH identity works non-interactively.
- `snapshot-export` creates/removes the temporary snapshot correctly and exports it through `snapshot-export-to-template`.
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
7. For R2, native Bucket Lock **Date** rules are verified on persistent restic namespaces, the horizon is refreshed after another successful copy, and transient `locks/` is not retention-locked. Confirm operators understand R2 locks are admin-mutable and are not S3 Compliance Object Lock.
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

Record release version/commit, hypervisor versions, server model/firmware, storage target versions, network diagram, test VM names, RPO/RTO observed, tester/approver, date and evidence/log locations. Re-run the affected portions after major hypervisor, storage, firmware, network or Immutavault upgrades.
