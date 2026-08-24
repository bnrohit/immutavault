# VMware native incremental transport — v0.7

Immutavault v0.7 adds an optional VMware Changed Block Tracking (CBT) data path using **pyVmomi + VMware VDDK through nbdkit/libnbd**. The existing v0.6 hot-clone OVF transport remains a fully supported safety fallback.

## Operational modes

| Mode | Behavior |
|---|---|
| `auto` | Prefer CBT/VDDK when healthy; automatically take a new hot-clone full baseline when incremental safety cannot be proven. Recommended. |
| `hot-clone-export` | Always use the v0.6 full snapshot/temporary-clone/OVF path. |
| `vddk-cbt-strict` | Require CBT/VDDK. Fail the backup instead of falling back. |

## Why the first backup is full

A CBT chain must start from a full recovery anchor. Immutavault creates the source snapshot, records that snapshot's disk change IDs, clones **that same snapshot** to a temporary powered-off VM, exports the clone as OVF/VMDK, and commits the change IDs only after restic returns a successful immutable snapshot ID.

This same-snapshot rule is important: taking the full backup at one point in time and recording CBT change IDs from a later snapshot could leave a gap that no subsequent incremental contains.

## Incremental backup sequence

```text
running VM
   ↓
quiesced VMware snapshot
   ↓
validate CBT epoch + disk/hardware fingerprint
   ↓
QueryChangedDiskAreas(previous changeId)
   ↓
nbdkit VDDK snapshot disk
   ↓
libnbd reads ONLY changed extents
   ↓
compact delta files + maps + SHA-256
   ↓
restic immutable snapshot
   ↓
ONLY AFTER restic succeeds:
commit new CBT change IDs
   ↓
delete/consolidate VMware snapshot
```

The default VDDK transport preference is:

```text
SAN → HotAdd → NBDSSL → NBD
```

Actual transport selection is performed by VDDK/nbdkit from the transports allowed by the operator.

## Automatic full fallback

With `mode: auto`, Immutavault takes a fresh hot-clone full baseline when any of the following occurs:

- no previous committed CBT state exists;
- VDDK or the nbdkit VDDK plugin is unavailable;
- the configured VDDK directory is missing or unusable;
- the VDDK TLS thumbprint is not configured;
- CBT is disabled and automatic enablement is not allowed;
- the VM does not support CBT;
- a disk has no usable change ID;
- the CBT epoch changes/reset is detected;
- VM disk layout, CPU/memory/firmware or network hardware fingerprint changes;
- `QueryChangedDiskAreas` fails;
- a VDDK changed-block read fails;
- the configured maximum chain length is reached.

Fallback is deliberately a **new full baseline**, never a guessed or partially trusted incremental.

## Transaction safety

The VMware adapter writes a transport marker into the staging backup. The repository layer does not advance CBT state until `restic backup` has returned the immutable recovery-point ID.

If export succeeds but restic fails, the prior change ID remains authoritative. The next incremental therefore queries from the last successful backup and cannot silently skip the failed interval.

## Immutable chain metadata

Every delta snapshot contains:

- parent recovery-point ID;
- baseline recovery-point ID;
- new per-disk change IDs;
- VM/disk fingerprint;
- changed extent map;
- SHA-256 for delta data;
- disk ordinal/key/capacity metadata.

The original restic snapshot also receives an `immutavault-cbt-parent:<snapshot-id>` tag. This makes dependency information available from the immutable repository itself, not only from the controller cache.

## Retention and replicas

A retained delta is not useful if its baseline or an intermediate delta has been deleted. v0.7 therefore makes retention dependency-aware:

```text
retained point
  ↓ parent
incremental
  ↓ parent
incremental
  ↓ parent
full baseline
```

All ancestors remain protected while a retained child depends on them.

Replication is also chain-aware. When the newest point is copied to Wasabi, B2, IDrive e2, R2, a filesystem replica, or another Immutavault vault, every required ancestor is copied/verified as well. A DR copy is not considered healthy merely because the newest delta exists.

## Restore

The selected recovery point is restored first. If it is a CBT delta, Immutavault reads its immutable parent marker and recursively restores parents until the full baseline is reached.

Then:

```text
full OVF baseline
   ↓
import as NEW powered-off VM
   ↓
verify target disk layout
   ↓
apply delta 1 with VDDK/libnbd
   ↓
apply delta 2
   ↓
...
   ↓
selected recovery point
```

Delta files are SHA-256 checked before writes. The target must remain powered off during delta application. Existing production VMs are never implicitly overwritten.

## Requirements

- x86-64 Linux backup proxy/appliance;
- supported vCenter/ESXi environment;
- pyVmomi 9.1.x (installed by the Python package);
- `nbdkit`;
- `nbdsh` / libnbd;
- nbdkit VDDK plugin;
- licensed VMware VDDK distribution installed separately;
- vCenter account with the privileges required for snapshots, CBT queries, disk access, temporary clone/export and recovery operations;
- vCenter TLS/VDDK thumbprint.

VMware VDDK is proprietary. **Immutavault does not download, bundle, redistribute, or bypass VMware/Broadcom licensing for VDDK.**

Install open-source prerequisites and validate a separately installed VDDK directory with:

```bash
sudo ./scripts/install_vmware_vddk_transport.sh \
  --vddk-dir /opt/vmware-vix-disklib-distrib
```

## Example

```yaml
platforms:
  - name: vc-primary
    type: vmware
    enabled: true
    endpoint: https://vcenter.example.local/sdk
    mode: auto
    include: ["DC01", "SQL01", "FILE01"]
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      insecure: false
      quiesce: true
      quiesce_fallback_crash_consistent: false
      cbt_auto_enable: true
      incremental_fallback: true
      cbt_max_chain_length: 32
      vddk_libdir: /opt/vmware-vix-disklib-distrib
      vddk_thumbprint: "AA:BB:CC:DD:..."
      vddk_transports: "san:hotadd:nbdssl:nbd"
```

The Guided Setup Console exposes these VMware options without requiring YAML editing.

## Production acceptance

Normal GitHub CI can validate fallback logic, dependency/retention behavior, package compatibility and transport simulation, but it cannot legally contain a licensed VDDK distribution or a real vCenter.

Before enabling CBT/VDDK for production, run a controlled acceptance using the actual VDDK/vCenter environment:

1. create a full baseline of a disposable running VM;
2. change known files/data in the guest;
3. run incremental #1 and confirm transferred bytes are materially below full VM size;
4. make different guest changes and run incremental #2;
5. restore incremental #2 as a new VM and validate all changes;
6. deliberately reset/disable CBT and confirm the next backup automatically becomes a new full baseline in `auto` mode;
7. test the selected SAN/HotAdd/NBDSSL transport;
8. copy the chain to the real immutable DR target and restore from that target;
9. boot and application-health-check the restored VM.

Until that environment-specific drill passes, treat VDDK/CBT as **implemented and software-tested, but not certified for that particular vSphere estate**.
