# Availability and control-plane survival

No software running on one chassis can guarantee zero downtime. Immutavault separates **recovery-data durability** from **control-plane availability**.

## Single appliance

A single dedicated server is appropriate for a primary vault when combined with an off-site immutable copy. Independent systemd services restart the portal/repository automatically and persistent timers catch up after reboot. This does not survive complete chassis, power, storage-controller or site loss by itself.

## Production topology

Use at least two failure domains:

```text
Primary vault/controller  ---> immutable S3 or DR vault
        |
        +---- online state DB backups every 5 min ---> warm standby controller at DR/third site
```

For automatic site DR, the orchestration controller itself must survive the primary failure. Set `disaster_recovery.control_plane_site` to the DR/third-site location. The parser refuses unattended failover when the declared control plane resides only at the primary site.

## SQLite control plane

v0.5 uses SQLite/WAL intentionally for simple, robust single-writer operation. **Do not run two active controllers concurrently against one SQLite database on NFS.** Maintain one authoritative writer and a warm standby.

`immutavault-state-backup.timer` uses SQLite's online backup API every five minutes. Place/replicate `runtime.state_backup_path` into a separate failure domain and routinely test restoration on the standby.

## Standby promotion

Use `scripts/promote_standby.sh` on the standby with an explicitly selected state backup. It validates SQLite integrity, atomically installs the catalog, validates the Immutavault audit chain and only starts services requested by the operator. Do not activate both primary and standby controllers as writers.

## Data availability vs RTO

An off-site immutable snapshot can survive primary-vault loss, but RTO still includes restore/import/boot time. Instant VM execution from deduplicated backup is not implemented in v0.5. If a workload needs seconds-level failover, use application clustering/native replication or a separately certified continuous-replication transport rather than claiming a daily/hourly backup provides zero downtime.
