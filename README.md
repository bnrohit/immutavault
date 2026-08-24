# Immutavault v0.5.1

Immutavault is an open, vendor-neutral **immutable VM backup, recovery, replication and disaster-recovery orchestrator** for VMware/vCenter, Proxmox VE and XCP-ng. It can run on a dedicated Linux server or Linux VM and use local RAID/ZFS, NFS/SMB/NAS, a second Immutavault vault, or S3-compatible object storage for additional recovery copies.

The security principle is simple: **the identity that creates backups does not receive prune/delete authority.** The normal controller writes through an append-only REST service; a separate root-only maintenance path performs expiration. Recovery is similarly conservative: customers can choose recovery points, but Immutavault restores as a **new VM** and refuses implicit production overwrite.

> **Readiness statement:** v0.5.1 has a repeatable local release suite plus a live restic/rest-server data-plane CI test covering configuration, storage policy, recovery control, systemd assets, installers, VMware/Proxmox/XCP-ng adapter behavior, DR fencing/quorum/network planning and packaging. It is a production **pilot candidate**, not a blanket certification for every server, hypervisor release, guest OS or network. Production acceptance requires the live tests in `docs/PRODUCTION_ACCEPTANCE.md` on the actual environment.

## What v0.5.1 includes

- VMware/vCenter inventory and **hot snapshot -> powered-off temporary clone -> OVF export** backup path, keeping the protected VM running. Strict quiesce policy is configurable; no silent crash-consistent fallback unless explicitly allowed.
- Proxmox inventory, online `vzdump --mode snapshot`, safe `qmrestore`/`pct restore`, and cleanup guards.
- XCP-ng inventory, supported snapshot-to-template XVA export, template-aware import/`vm-install`, and cleanup of the temporary recovery template.
- Encrypted, deduplicated restic repository.
- Pinned/SHA-256-verified restic 0.19.1 and capability-gated rest-server 0.14.0 install paths.
- Authenticated TLS `rest-server` append-only writer endpoint.
- Separate `immutavault` controller and `immutavault-store` repository OS identities.
- Root-only GFS retention/prune with immutable-catalog protection.
- Default 30-day immutability; daily/weekly/monthly/yearly retention.
- SHA-256 manifest verification and full staged recovery-point verification.
- Ransomware/churn anomaly detection and longer preservation for suspicious points.
- Tamper-evident SHA-256 audit chain.
- Customer recovery portal/API, scoped roles, four-eyes approval and restore-source selection.
- S3-compatible replicas: Wasabi, IDrive e2, Backblaze B2, AWS S3, MinIO, Ceph and custom endpoints.
- S3 Object Lock support where the provider implements it.
- Cloudflare R2 support with Cloudflare-native Bucket Locks using a rolling **Date** horizon refreshed after successful copies; kept distinct from S3 Compliance Object Lock.
- Filesystem replicas for NFS/SMB/TrueNAS/Dell or other mounted storage.
- Online SQLite control-plane backups every five minutes.
- Versioned application installs, atomic symlink upgrade and rollback.
- Persistent systemd timers and independent portal/repository/backup/retention services.
- Two-site DR orchestration with explicit fencing, maintenance suppression, probe quorum, VXLAN recovery VLANs, FRR/OSPF route ownership, boot order, workload health checks, failover and planned failback.
- Same-IP DR for explicitly configured stretched recovery VLANs: only the active site owns the gateway IP and advertises the subnet.
- Automatic cross-hypervisor conversion is **blocked** until a conversion path is separately certified.

## Recommended production topology

```text
 Primary hypervisors                       Separate failure domain
 VMware / Proxmox / XCP-ng                        DR site
          |                                           |
          | backup                                    | DR compute
          v                                           v
 +----------------------+                    +----------------------+
 | Immutavault control  |                    | DR hypervisor        |
 | plane / portal       |                    +----------+-----------+
 +----------+-----------+                               |
            | HTTPS append-only                         | recovery VLANs
            v                                           v
 +----------------------+                    +----------------------+
 | Primary vault        |                    | Linux DR gateway     |
 | rest-server + repo   |                    | VXLAN + FRR/OSPF     |
 +----------+-----------+                    +----------+-----------+
            |                                           |
            +---- encrypted replica -------------------+
            |
            +---- Wasabi / e2 / B2 / R2 / NFS / NAS
```

For unattended site failover, **do not place the only DR controller at the primary site**. Run the active controller at the DR/third site or maintain a tested warm standby there with replicated state backups. See `docs/HIGH_AVAILABILITY.md`.

## Hardware

Immutavault does not require proprietary Dell/Cisco/Lenovo backup hardware. A practical controller/vault starts at 4 CPU threads and 8 GiB RAM; 8+ cores, 16-32+ GiB RAM, ECC memory, redundant power and 10/25 GbE are recommended for production. Backup capacity is determined primarily by protected data, change rate, retention, deduplication and replica policy—not by the controller CPU.

Use separate failure domains. A single physical server can be a good primary vault, but cannot guarantee zero downtime or protect against complete chassis/site loss by itself.

## Fast install on Ubuntu 24.04 LTS

The easiest dedicated appliance install is:

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
sudo ./scripts/preflight.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
```

The all/repository installer can fetch **pinned rest-server v0.14.0 from the official upstream GitHub release and verifies its SHA-256 before installation**. Use `--no-rest-server-download` if your change-control policy requires supplying the binary yourself.

Nothing partitions or formats disks automatically.

After editing `/etc/immutavault/immutavault.yml` and `/etc/immutavault/immutavault.env`:

```bash
sudo -u immutavault bash -c '
  set -a
  source /etc/immutavault/immutavault.env
  set +a
  immutavault --config /etc/immutavault/immutavault.yml doctor
'

sudo -u immutavault bash -c '
  set -a
  source /etc/immutavault/immutavault.env
  set +a
  immutavault --config /etc/immutavault/immutavault.yml inventory
'

sudo -u immutavault bash -c '
  set -a
  source /etc/immutavault/immutavault.env
  set +a
  immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run
'
```

Do not enable recurring production backup jobs until `doctor`, inventory, dry-run, **one real backup and one isolated restore/boot** all pass.

When ready:

```bash
sudo systemctl enable --now immutavault-rest-server.service
sudo systemctl enable --now immutavault-portal.service
sudo systemctl enable --now immutavault-backup.timer
sudo systemctl enable --now immutavault-state-backup.timer
sudo systemctl enable --now immutavault-health.timer
sudo systemctl enable --now immutavault-retention.timer
sudo systemctl enable --now immutavault-verify.timer
```

The generic installer deliberately **does not enable DR auto-failover timers**. Those are enabled only after a controlled failover/failback acceptance drill.

## Installation roles

```bash
# Complete controller + repository appliance
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault

# Controller/portal only; repository is elsewhere
sudo ./scripts/install.sh --role controller

# Append-only repository node only
sudo ./scripts/install.sh --role repository --repo-root /backup/immutavault
```

See `docs/INSTALLATION.md` for NFS/SMB, vCenter, Proxmox, XCP-ng and S3 prerequisites.

## VMware backup behavior

The recommended VMware mode is:

```yaml
mode: hot-clone-export
options:
  quiesce: true
  quiesce_fallback_crash_consistent: false
```

Immutavault creates a short-lived source snapshot, creates a powered-off temporary clone from that point in time, exports that clone, removes the clone, then consolidates/removes the source snapshot. There is **no planned power-off of the protected VM**, although VMware snapshots can cause a brief stun and quiescing depends on VMware Tools/application behavior.

This is safer than cold OVF export, but it is not yet VDDK/CBT incremental transport. Large VMware estates should treat native CBT/VDDK as the next performance/RPO milestone. See `docs/VMWARE_BACKUP.md`.

## Backup and recovery

```bash
immutavault --config /etc/immutavault/immutavault.yml backup --all
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml verify-point --snapshot SNAPSHOT_ID
```

Create a restore request:

```bash
immutavault --config /etc/immutavault/immutavault.yml restore-request \
  --snapshot SNAPSHOT_ID \
  --requester operator1 \
  --target-platform vc-dr \
  --target-name SERVER01-RECOVERY
```

A different identity approves when four-eyes policy is enabled:

```bash
immutavault --config /etc/immutavault/immutavault.yml restore-approve \
  --request-id REQUEST_ID --approver backup-admin
```

Always preview before execution:

```bash
immutavault --config /etc/immutavault/immutavault.yml restore-execute \
  --request-id REQUEST_ID --actor backup-admin --dry-run
```

Then execute. Immutavault refuses automatic overwrite of an existing target VM.

## Replicas and immutable cloud

List/configure destinations:

```bash
immutavault --config /etc/immutavault/immutavault.yml storage-targets
immutavault --config /etc/immutavault/immutavault.yml replica-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml replica-lock-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml replica-lock-status --name wasabi-immutable
```

NFS/SMB/TrueNAS/Dell storage should be mounted by the OS first and then configured as a `filesystem` replica or as the primary repository root.

## DR failover

DR is opt-in and disabled by default. First validate:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-plan
immutavault --config /etc/immutavault/immutavault.yml dr-preflight
immutavault --config /etc/immutavault/immutavault.yml dr-sync
immutavault --config /etc/immutavault/immutavault.yml dr-network plan --site offshore-dr
```

A controlled failover is previewed before execution:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-promote
```

Only after the primary has been truly fenced/isolated:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-promote \
  --execute --confirm-primary-fenced
```

Failback is also plan-first:

```bash
immutavault --config /etc/immutavault/immutavault.yml dr-failback
immutavault --config /etc/immutavault/immutavault.yml dr-failback \
  --execute --confirm-primary-isolated
```

See `docs/DR_RUNBOOK.md`. Never enable unattended failover without tested fencing **and separate fencing verification**.

## Release validation

Every release candidate must pass:

```bash
./scripts/release_check.sh
```

This verifies version consistency, secret/state exclusions, Python compilation, shell parsing, the full test suite, production example config, CLI startup, systemd syntax, wheel creation/re-install, and the pinned/checksummed rest-server installer contract.

A real environment must additionally pass `docs/PRODUCTION_ACCEPTANCE.md` before being declared production-ready.

## Important limits

- No software on one server can promise literal zero downtime.
- The VMware hot-clone transport avoids planned source shutdown, but is not CBT/CDP.
- Current Proxmox and XCP-ng paths are snapshot/export based, not PBS/XO native incrementals.
- Application consistency depends on guest/application quiescing and should be tested per workload.
- Same-IP DR requires carefully designed routed underlay/VXLAN/MTU/firewall/OSPF and fencing.
- Cross-hypervisor automatic conversion is blocked in the safe core.
- Automatic DR is not enabled by installation.

These are deliberate safety boundaries rather than marketing claims.

## Documentation

- `docs/QUICKSTART.md` - lab quick start
- `docs/INSTALLATION.md` - production installation
- `docs/PRODUCTION_ACCEPTANCE.md` - go-live gates
- `docs/OPERATIONS.md` - day-2 operations
- `docs/RESTORE.md` - restore runbook
- `docs/DR_RUNBOOK.md` - failover/failback
- `docs/HIGH_AVAILABILITY.md` - control-plane and data-plane HA
- `docs/VMWARE_BACKUP.md` - VMware hot backup behavior
- `docs/REALTIME_READINESS.md` - exact RPO/RTO and live-data-plane boundaries
- `docs/CLOUD_STORAGE.md` - S3/NFS/SMB targets
- `docs/SECURITY.md` - security model
- `docs/ARCHITECTURE.md` - components/trust boundaries
- `docs/COMPATIBILITY.md` - compatibility policy
- `docs/VEEAM_GAP_MATRIX.md` - implemented vs future enterprise features

## License

Apache-2.0. See `LICENSE`.
