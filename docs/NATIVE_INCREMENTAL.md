# Native Incremental VMware Transport (v0.7)

Immutavault v0.7 introduces a capability-gated VMware Changed Block Tracking (CBT) transport. The safe v0.6 `hot-clone-export` path remains present and is the automatic fallback whenever the native provider cannot establish a trustworthy incremental backup.

## Important VDDK distribution boundary

Immutavault does **not** bundle Broadcom/VMware VDDK binaries. VDDK availability and redistribution are controlled by Broadcom. An organization or authorized technology partner that has a supported VDDK integration installs a provider helper named `immutavault-vddk` (or configures another path with `options.vddk_helper`).

The open-source Immutavault core never reports a CBT backup unless the provider passes the protocol/capability checks and returns a valid recoverable layout plus a per-disk checkpoint.

## Recommended mode

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: auto
    include: ["*"]
    exclude: []
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      insecure: false
      quiesce: true
      quiesce_fallback_crash_consistent: false

      # v0.7 native transport policy
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_fallback: true
      incremental_strict: false
      incremental_cache_root: /var/cache/immutavault/vddk
      incremental_block_size: 134217728   # 128 MiB
      vddk_transport_order: [san, hotadd, nbdssl]
```

`mode: auto` means:

1. Probe the external VDDK provider.
2. If it supports protocol v1 + CBT + backup + restore, request a native backup.
3. Reuse the previous per-disk CBT checkpoint when it is valid.
4. If CBT is unavailable or its generation is invalid, use the safe full fallback.
5. Never silently call a full OVF export "CBT".

Existing configurations using `mode: hot-clone-export` continue to force the v0.6 transport.

Set `incremental_strict: true` only when policy requires native incremental protection and a missing provider should make the backup fail rather than fall back.

## CBT behavior

The provider is responsible for the VMware API/VDDK operations and must follow VMware CBT semantics:

- create a point-in-time VM snapshot before querying changed areas;
- retain the per-disk `changeId` associated with the completed backup;
- use the previous disk `changeId` for later incremental queries;
- use `*` only to establish a new allocated-block baseline when no trustworthy previous checkpoint exists;
- query until the entire disk range has been covered;
- treat a changed change-ID UUID/generation as an invalid incremental chain and request a new baseline;
- never claim success for disks that do not support trustworthy CBT;
- remove/consolidate temporary VMware snapshots even when backup fails.

## Provider protocol v1

The provider is an executable. Secrets are inherited through environment variables such as `GOVC_USERNAME` and `GOVC_PASSWORD`; Immutavault does not put credentials on the command line or in the JSON request.

### Capability probe

```bash
immutavault-vddk capabilities --json
```

Minimum successful response:

```json
{
  "protocol_version": 1,
  "provider": "partner-vddk",
  "provider_version": "1.0.0",
  "features": ["cbt", "backup", "restore"],
  "transport_modes": ["san", "hotadd", "nbdssl"]
}
```

### Backup

Immutavault sends a JSON request on stdin:

```bash
immutavault-vddk backup --json
```

The request includes platform/VM identity, destination, the previous per-disk checkpoint when available, block size, quiescing policy, and preferred VDDK transport order.

Successful providers return JSON similar to:

```json
{
  "status": "success",
  "mode": "incremental",
  "transport": "hotadd",
  "changed_bytes": 734003200,
  "source_bytes_read": 734003200,
  "checkpoint": {
    "disk-2000": "52f.../17",
    "disk-2001": "18a.../44"
  }
}
```

The destination must also contain `immutavault-vddk-layout.json`, describing the complete recoverable virtual-disk block layout. A success response without this file is rejected.

### Safe fallback response

For conditions where a full backup is safe, the provider can return a non-zero exit code and JSON such as:

```json
{
  "status": "fallback",
  "reason": "change_id_reset",
  "fallback_safe": true,
  "error": "disk change-ID generation changed"
}
```

Known invalidating reasons include:

- `cbt_disabled`
- `cbt_invalid`
- `change_id_reset`
- `invalid_change_id`
- `unsupported_disk`

For these reasons Immutavault discards the stale native cache before running the full fallback.

If `fallback_safe` is false, Immutavault fails closed and does **not** start a fallback backup because the provider reported an ambiguous state.

## Recoverable block layout

The provider maintains a local per-VM cache under `incremental_cache_root`. The recommended layout uses large fixed-size block files (128 MiB by default):

```text
vc-primary/sql01/
├── immutavault-vddk-layout.json
├── .immutavault-cbt-checkpoint.json
├── .immutavault-transport.json
└── disks/
    ├── disk-2000/
    │   └── blocks/
    │       ├── 000000000000.blk
    │       ├── 000000000001.blk
    │       └── ...
    └── disk-2001/
        └── blocks/
            └── ...
```

The provider updates only block files intersecting CBT extents. Immutavault then hard-links that cache into the one-shot backup staging tree. This provides a self-contained logical restic snapshot without copying unchanged blocks locally. Restic still performs its own content-addressed deduplication and repository encryption.

The cache contains recoverable guest data and must be treated like backup staging: use an encrypted filesystem/storage pool, restrict access to the Immutavault service account, and monitor capacity.

## Restore

When a selected recovery point contains `immutavault-vddk-layout.json`, the VMware adapter uses the same authorized provider in restore mode. The provider must create/write a **new** VM. Immutavault checks that the requested target VM name does not already exist before provider restore is allowed.

If the provider is unavailable, an incremental-format recovery point cannot be falsely converted into an OVF restore; the restore fails with a clear provider requirement.

Full fallback recovery points remain normal OVF/VMDK packages and use the existing v0.6 restore path.

## Fallback matrix

| Condition | `mode: auto`, fallback enabled | strict mode |
|---|---|---|
| Provider installed + CBT healthy | Native incremental | Native incremental |
| Provider missing | Hot-clone full backup | Fail |
| CBT disabled | Hot-clone full backup | Fail |
| Invalid/reset change ID | Invalidate cache + hot-clone full | Fail |
| Unsupported disk | Hot-clone full backup | Fail |
| Provider says state is ambiguous/unsafe | Fail closed | Fail closed |
| Explicit `mode: hot-clone-export` | Hot-clone full backup | Hot-clone full backup |

## What v0.7 does not claim

The open-source core does not redistribute VDDK, does not bypass Broadcom licensing/partner requirements, and does not label the fallback as incremental. Native transport must be validated with the exact vCenter/ESXi version, VDDK provider, proxy transport (SAN/HotAdd/NBDSSL), VM hardware/storage type, and restore workflow used in production.
