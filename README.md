# Immutavault v0.7.1

Immutavault is an open, vendor-neutral **immutable VM backup, recovery, replication and disaster-recovery orchestrator** for VMware/vCenter, Proxmox VE and XCP-ng. It can run on a dedicated Linux server or Linux VM and use local RAID/ZFS, NFS/SMB/NAS, a second Immutavault vault, or S3-compatible object storage for additional recovery copies.

The security principle is simple: **the identity that creates backups does not receive prune/delete authority.** The normal controller writes through an append-only REST service; a separate root-only maintenance path performs expiration. Recovery is similarly conservative: customers can choose recovery points, but Immutavault restores as a **new VM** and refuses implicit production overwrite.

> **Readiness statement:** v0.7.1 is a production-pilot candidate. It adds a capability-gated VMware VDDK/CBT provider contract and a strict native-incremental policy that fails closed on unavailable, unsafe, malformed, or ambiguous provider state. Broadcom VDDK is not redistributed by Immutavault; an authorized `immutavault-vddk` provider/helper must be installed separately. Production acceptance still requires the live tests in `docs/PRODUCTION_ACCEPTANCE.md` on the actual environment.

## What v0.7.1 includes

- VMware/vCenter native VDDK/CBT backup path through an external authorized provider/helper.
- **Strict incremental mode:** `incremental_strict: true` means any native incremental failure fails the backup; no OVF/hot-clone fallback and no fake recovery point.
- Non-strict fallback is allow-listed and fail-closed. Unknown reasons cannot fall back even when a provider claims fallback is safe.
- In-flight native provider failures invalidate the per-VM CBT cache before any further action.
- Existing VMware `hot-clone-export` remains available as an explicit full-backup transport and as a controlled non-strict fallback only when policy permits it.
- VMware quiesce policy remains explicit; no silent crash-consistent downgrade unless configured.
- Proxmox inventory, online `vzdump --mode snapshot`, safe `qmrestore`/`pct restore`, and cleanup guards.
- XCP-ng inventory, snapshot-to-template XVA export, template-aware import/`vm-install`, and cleanup of temporary recovery templates.
- Encrypted, deduplicated restic repository.
- Pinned/SHA-256-verified restic 0.19.1 and capability-gated rest-server 0.14.0 installation paths.
- Authenticated TLS append-only `rest-server` writer endpoint.
- Separate controller and repository OS identities.
- Root-only GFS retention/prune with immutable-catalog protection.
- Default 30-day immutability with daily/weekly/monthly/yearly retention.
- SHA-256 manifest verification and staged recovery-point verification.
- Ransomware/churn anomaly detection and extended preservation for suspicious points.
- Tamper-evident SHA-256 audit chain.
- Customer recovery portal/API, scoped roles, four-eyes approval and restore-source selection.
- S3-compatible replicas including Wasabi, IDrive e2, Backblaze B2, AWS S3, MinIO, Ceph and custom endpoints.
- S3 Object Lock support where the provider implements it.
- Cloudflare R2 Bucket Lock support with a rolling Date horizon, kept distinct from S3 Compliance Object Lock.
- Filesystem replicas for NFS/SMB/TrueNAS/Dell or other mounted storage.
- Online SQLite control-plane backups every five minutes.
- Versioned application installs with atomic symlink upgrade and rollback.
- Persistent systemd timers and independent portal/repository/backup/retention services.
- Two-site DR orchestration with fencing, probe quorum, VXLAN recovery VLANs, FRR/OSPF ownership, boot order, health checks, failover and planned failback.
- Same-IP DR for explicitly configured stretched recovery VLANs: only the active site owns the gateway IP and advertises the subnet.
- Automatic cross-hypervisor conversion is blocked until a conversion path is separately certified.
- Guided browser setup for hypervisors, VM selection, storage/cloud and staged DR configuration.
- Audit-first dashboard with **RPO Status** and **Immutable-Copy Verification** front and center.

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

For unattended site failover, do not place the only DR controller at the primary site. Run the active controller at the DR/third site or maintain a tested warm standby there with replicated state backups. See `docs/HIGH_AVAILABILITY.md`.

## Hardware

Immutavault does not require proprietary backup hardware. A practical controller/vault starts at 4 CPU threads and 8 GiB RAM; 8+ cores, 16-32+ GiB RAM, ECC memory, redundant power and 10/25 GbE are recommended for production. Backup capacity is determined primarily by protected data, change rate, retention, deduplication and replica policy.

Use separate failure domains. A single physical server can be a good primary vault, but cannot guarantee zero downtime or protect against complete chassis/site loss by itself.

## Fast install on Ubuntu 24.04 LTS

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
git checkout v0.7.1
sudo ./scripts/preflight.sh
./scripts/release_check.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
sudo ./scripts/launch_setup_console.sh
```

The installer never partitions or formats disks automatically. Do not enable recurring production backups until `doctor`, inventory, dry-run, **one real backup and one isolated restore/boot** all pass.

## VMware strict native incremental protection

For enterprise VMware environments where every scheduled point must be a genuine native incremental point, use:

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: vddk
    include:
      - "*"
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_strict: true
      incremental_fallback: false
      incremental_cache_root: /var/cache/immutavault/vddk
      quiesce: true
      quiesce_fallback_crash_consistent: false
      vddk_transport_order:
        - san
        - hotadd
        - nbdssl
```

Strict-mode behavior:

| VDDK / CBT state | Result |
| --- | --- |
| Healthy CBT + valid change IDs | Native incremental backup |
| Helper missing | Backup fails |
| CBT disabled/uninitialized/unsupported | Backup fails |
| CBT generation/change ID reset | Backup fails |
| Unsupported disk | Backup fails |
| Unsafe or ambiguous provider state | Fail closed |
| Invalid/corrupt checkpoint | Fail closed |
| Provider omits `fallback_safe` | Fail closed |
| Provider crashes unexpectedly | Fail closed |
| Hot-clone fallback | Never attempted |

`incremental_strict: true` is absolute. It prevents automatic hot-clone/OVF fallback, prevents a fallback recovery point from being presented as incremental, and does not advance a valid CBT chain after an uncertain native run.

Broadcom VDDK itself is **not bundled**. The configured helper must implement the documented protocol-v1 capability/backup/restore contract. See `docs/VMWARE_BACKUP.md`.

## Controlled non-strict fallback

Non-strict mode is for environments where a fresh full backup is acceptable for a narrowly recognized condition. It requires `incremental_fallback: true`, an allow-listed reason, and for provider-stage failures an explicit `fallback_safe: true`.

For example, a provider-stage reset may be eligible only when the provider returns:

```json
{
  "status": "fallback",
  "reason": "change_id_reset",
  "fallback_safe": true
}
```

Omitted `fallback_safe`, unknown reasons, malformed output and unexpected provider exceptions fail closed.

## Backup and recovery

```bash
immutavault --config /etc/immutavault/immutavault.yml doctor
immutavault --config /etc/immutavault/immutavault.yml inventory
immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run
immutavault --config /etc/immutavault/immutavault.yml backup --all
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml verify-point --snapshot SNAPSHOT_ID
```

Create and approve restore requests through the CLI or recovery portal. Always preview before execution. Immutavault refuses automatic overwrite of an existing target VM.

## Replicas and immutable cloud

```bash
immutavault --config /etc/immutavault/immutavault.yml storage-targets
immutavault --config /etc/immutavault/immutavault.yml replica-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml replica-lock-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml replica-lock-status --name wasabi-immutable
```

NFS/SMB/TrueNAS/Dell storage should be mounted by the OS first and then configured as a filesystem replica or primary repository root.

## DR failover

DR is opt-in and disabled by default. Validate `dr-plan`, `dr-preflight`, `dr-sync`, fencing and network ownership before any controlled promotion. Never enable unattended failover without tested fencing and separate fencing verification. See `docs/DR_RUNBOOK.md`.

## Release validation

Every release candidate must pass:

```bash
./scripts/release_check.sh
```

The release gate verifies version consistency, documentation/version alignment, strict VMware incremental example policy, secret/state exclusions, Python compilation, shell parsing, the full test suite, production configuration, CLI startup, systemd syntax, wheel build/reinstall and the pinned/checksummed rest-server installation contract.

GitHub CI additionally runs the Python matrix and a real authenticated TLS append-only restic/rest-server data-plane backup/restore test.

A real environment must additionally pass `docs/PRODUCTION_ACCEPTANCE.md` before being declared production-ready.

## Important limits

- Broadcom VDDK is not redistributed or bundled. Native VMware incremental protection requires an authorized compatible helper/provider.
- VDDK/CBT is backup incrementality, **not CDP**. RPO is bounded by the configured backup schedule and successful provider runs.
- `hot-clone-export` remains a full-image transport; select it explicitly when that is the intended policy.
- Current Proxmox and XCP-ng paths are snapshot/export based, not PBS/XO native incrementals.
- Application consistency depends on guest/application quiescing and should be tested per workload.
- Same-IP DR requires carefully designed routed underlay/VXLAN/MTU/firewall/OSPF and fencing.
- Cross-hypervisor automatic conversion is blocked in the safe core.
- Automatic DR is not enabled by installation.

These are deliberate safety boundaries rather than marketing claims.

## Documentation

- `docs/QUICKSTART.md` - lab quick start
- `docs/SETUP_CONSOLE.md` - guided browser configuration
- `docs/INSTALLATION.md` - production installation
- `docs/PRODUCTION_ACCEPTANCE.md` - go-live gates
- `docs/OPERATIONS.md` - day-2 operations
- `docs/RESTORE.md` - restore runbook
- `docs/DR_RUNBOOK.md` - failover/failback
- `docs/HIGH_AVAILABILITY.md` - control-plane and data-plane HA
- `docs/VMWARE_BACKUP.md` - native VDDK/CBT, strict mode and hot-clone fallback policy
- `docs/INCREMENTAL_STRICT_MODE.md` - v0.7.1 fail-closed incremental policy
- `docs/REALTIME_READINESS.md` - RPO/RTO and live-data-plane boundaries
- `docs/CLOUD_STORAGE.md` - S3/NFS/SMB targets
- `docs/SECURITY.md` - security model
- `docs/ARCHITECTURE.md` - components/trust boundaries
- `docs/COMPATIBILITY.md` - compatibility policy
- `docs/VEEAM_GAP_MATRIX.md` - implemented vs future enterprise features

## License

Apache-2.0. See `LICENSE`.
