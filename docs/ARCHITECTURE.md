# Architecture

## Trust zones

1. **Hypervisor zone** — vCenter/ESXi, Proxmox VE, XCP-ng. Credentials here may read inventory and request snapshot/export operations.
2. **Controller/staging zone** — temporary VM exports and restore staging. Data is mutable and disposable after commit/recovery.
3. **Append-only repository service** — network writers can add restic objects but cannot prune/delete old recovery points.
4. **Repository filesystem identity** — `immutavault-store` owns storage access; the controller does not receive direct repository filesystem rights.
5. **Retention authority** — root-only local maintenance process with deletion rights. It evaluates the recovery catalog before deletion.
6. **Recovery control plane** — browser/API portal with scoped identities and optional four-eyes approval.
7. **Optional second failure domain** — a separately administered encrypted replica, WORM/object lock target, or offline media.

## Backup data flow

```text
inventory -> platform version/capability capture -> snapshot/export -> staging
          -> manifest -> encrypted restic append-only commit -> recovery catalog
          -> anomaly evaluation -> optional replica -> staging cleanup
```

The SQLite catalog records the source platform, VM identity, source version information, size/churn indicators, integrity state, restore-point ID and immutable-until timestamp.

## Retention safety

Because each backup staging path contains a timestamp, restic retention groups snapshots by the stable backup tags instead of their changing source path. Immutavault first runs the restic retention policy in `--dry-run --json` mode, removes any snapshot IDs that are still protected by the catalog, then forgets only the remaining candidates and prunes unreachable data.

This design also prevents a suspicious recovery point from being silently removed just because it falls outside the ordinary window.

## Restore data flow

```text
user chooses point -> restore request -> independent approval (optional/usually required)
                  -> encrypted snapshot restore to staging
                  -> manifest validation
                  -> target collision check
                  -> hypervisor import as NEW VM
                  -> audit event + staging cleanup
```

Cross-hypervisor conversion is not performed automatically in the safe core.

## Why not direct NFS/SMB writes from the hypervisor?

Giving a production host normal filesystem write access to old backups often also gives it enough rights to rename or delete them. Immutavault places an append-only protocol boundary between backup writers and repository storage.
