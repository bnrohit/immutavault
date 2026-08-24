# Day-2 operations

## Daily health

```bash
immutavault --config /etc/immutavault/immutavault.yml status
systemctl --failed
systemctl list-timers 'immutavault-*'
```

`status` combines repository/platform/storage preflight, the tamper-evident audit-chain check, catalog counts, recent backup failures, and restore-request state.

The health timer runs every five minutes. A non-zero exit code is suitable for monitoring integrations through systemd, a SIEM agent, or an external service monitor.

## Catalog resilience

The SQLite catalog uses WAL mode. An online database backup is created every five minutes by `immutavault-state-backup.timer` using SQLite's backup API rather than copying a live database file blindly.

Default: keep 288 backups (24 hours at five-minute intervals). Replicate `/var/lib/immutavault/state-backups/` to the standby/DR management plane if using a warm standby.

The encrypted backup data itself remains in restic and is not dependent on the SQLite catalog for cryptographic integrity; the catalog provides Immutavault policy, UI, approvals, and metadata.

## Upgrades

Install from a clean release checkout:

```bash
sudo ./scripts/upgrade.sh
```

The upgrade installs a versioned virtual environment, smoke-tests it, then atomically switches `/opt/immutavault/current`. Repository data is not rewritten by the application upgrade.

Rollback:

```bash
sudo ./scripts/rollback.sh
```

Do not run retention/prune while manually changing repository storage or recovering the catalog.

## Service isolation

- `immutavault-portal.service`: UI/API only; restarting it does not stop scheduled backup data already stored.
- `immutavault-backup.service`: one-shot backup worker.
- `immutavault-rest-server.service`: append-only repository ingress.
- `immutavault-retention.service`: root-only delete/prune authority.
- `immutavault-verify.service`: repository verification.
- `immutavault-state-backup.service`: online catalog snapshot.

This separation prevents a web/UI failure from becoming a repository delete capability or a backup-engine outage.

## Capacity

Keep staging free-space headroom above the configured minimum. For export-based VMware/XCP-ng backups, staging may need space close to the largest VM export. Native delta transports reduce this requirement when integrated.

For a 50 TB protected estate, local usable capacity should be sized from change rate, retention and deduplication—not simply equal to source capacity. Maintain an additional immutable/off-site copy.
