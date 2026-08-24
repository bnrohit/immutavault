# VMware backup transport

## v0.5 recommended mode: `hot-clone-export`

Cold OVF export is not appropriate for always-on production backup. Immutavault v0.5 therefore uses a snapshot-assisted temporary-clone workflow:

1. Create a short-lived source snapshot (`memory=false`).
2. Request VMware Tools quiescing when configured.
3. Create a powered-off temporary full clone from that snapshot.
4. Export the temporary clone as OVF/VMDK.
5. Destroy the temporary clone.
6. Remove/consolidate the source snapshot.
7. Encrypt/deduplicate the exported payload into the append-only repository.

The protected VM is not deliberately powered off. Snapshot/quiesce operations can still cause a brief stun, and application-consistent quiescing depends on VMware Tools and the guest/application.

Recommended strict policy:

```yaml
mode: hot-clone-export
options:
  quiesce: true
  quiesce_fallback_crash_consistent: false
```

With fallback disabled, a failed quiesce fails the backup instead of silently reducing consistency. An administrator may explicitly allow crash-consistent fallback for workloads where that policy is acceptable.

## Capacity requirement

The temporary clone needs enough datastore capacity during export. Monitor free datastore capacity and snapshot consolidation health. Do not leave long-running source snapshots.

## Credentials

Each vCenter has independent environment-backed credentials. A primary and DR vCenter should use different service accounts/secrets and trusted CA files. Keep `insecure: false` for production.

## Current limitation

This mode still transfers a full exported image into the staging/data mover path. It is not VMware VDDK/CBT incremental transport and is not CDP. For large VMware estates or short RPOs, a future tested VDDK/CBT plugin is the correct high-efficiency path.

## Acceptance

Before protecting important VMs, verify on a disposable VM that:

- quiesced snapshot succeeds,
- temporary clone is created powered off,
- export completes,
- clone is removed,
- snapshot is consolidated/removed,
- recovery point verifies,
- restored VM imports under a new name and boots in an isolated network.
