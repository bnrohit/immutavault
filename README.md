# Immutavault v1.1.0

Immutavault is an open, vendor-neutral **immutable VM backup, recovery, certified V2V conversion, file-level recovery, replication and disaster-recovery platform** for VMware/vCenter, Proxmox VE and XCP-ng.

v1.1.0 is the **Unified Management & Appliance** release. It keeps the fail-closed V2V, immutable repository, tenant isolation, OIDC/MFA, FLR broker and target readiness controls from v1.0.1, while making normal deployment and day-2 operation substantially closer to a commercial backup appliance.

The security principle remains: **the identity that creates backups does not receive prune/delete authority.** The controller writes through an append-only repository path, privileged mount/configuration operations are isolated behind local Unix-socket brokers, recovery creates a **new VM** by default, and cross-hypervisor conversion stays blocked unless the exact source/target path passes certified policy.

## v1.1.0 — Unified management

### One console

The normal portal now combines:

- RPO and immutable-copy status;
- live backup progress through WebSockets;
- protected VM inventory;
- named protection policies;
- recovery-point browsing;
- browser file-level recovery and download;
- full-VM restore approval/execution;
- global-admin guided onboarding;
- storage onboarding;
- isolated DR-test preparation.

The network-facing portal remains deliberately unprivileged. Configuration/mount mutations go through a root-side **management broker** over a local Unix socket authenticated with Linux `SO_PEERCRED`. File-level mount operations remain in the separate **FLR broker**. The portal keeps `NoNewPrivileges=true`, `PrivateDevices=true`, and an empty capability set.

### Point-and-click protection policies

A policy is a validated configuration object, not an ad-hoc shell command. Operators can select exact VM names, choose a schedule, set immutable retention, choose replica destinations, and require verification after backup.

Example:

```yaml
management:
  enabled: true
  policies:
    - id: daily-production
      name: Daily Production
      enabled: true
      selections:
        - platform: vc-primary
          vms: [app01, db01]
      schedule:
        frequency: daily
        time: "22:00"
      immutable_days: 30
      replica_targets: [wasabi-dr]
      verify_after_backup: true
```

Checkbox-created policies require **exact VM names**. Wildcards are rejected. An empty `replica_targets` list means the immutable primary repository only; Immutavault never interprets an empty list as “copy everywhere.” When the first scheduled named policy is saved, the management service disables the legacy broad backup timer so duplicate jobs are not silently created.

CLI parity is retained:

```bash
immutavault --config /etc/immutavault/immutavault.yml policy-list
immutavault --config /etc/immutavault/immutavault.yml policy-dry-run --name daily-production
immutavault --config /etc/immutavault/immutavault.yml policy-run --name daily-production
```

### Guided hypervisor discovery

A global administrator can add VMware/vCenter, Proxmox or XCP-ng credentials, test the connection, discover inventory, and save the exact VMs to protect. Credential values stay in the protected environment file; YAML stores environment-variable references. Candidate configuration is validated against the complete v1.1 schema—including tenant ownership—before it can replace the live configuration.

### Storage onboarding

The guided storage flow supports:

- Wasabi and other S3-compatible object storage;
- AWS S3;
- Cloudflare R2 with its native Bucket Lock semantics;
- MinIO / Ceph / custom S3 endpoints;
- a second Immutavault REST vault;
- existing filesystem storage;
- direct NFS exports;
- direct SMB 3.1.1 shares.

Wizard-managed NFS/SMB mounts are constrained below `/srv/immutavault/storage`. Test mounts are temporary. Persistent mounts use generated systemd mount units; SMB credentials are written separately with restrictive permissions. NFS/SMB connection and writeability are checked before the target is accepted.

### One-click isolated DR tests

A recovery test is intentionally **not** production failover. It uses the normal certified restore engine and therefore retains tenant boundaries, recovery-point verification, anomaly blocking and four-eyes approval.

Before a test network can be used, a global administrator must explicitly allow-list it and the target hypervisor must confirm it exists. The test workflow then:

1. requires a verified, non-suspicious recovery point;
2. restores a new powered-off VM;
3. remaps every virtual NIC to the allow-listed isolated network before power-on;
4. boots the disposable VM;
5. checks that it remains running during the configured boot validation window;
6. powers it off;
7. destroys the disposable VM by default;
8. records the test and cleanup outcome in the audit chain.

A failed power-off or required cleanup is a failed DR test. The source VM and immutable recovery point are never modified.

Isolation implementations are explicit: VMware uses target port-group remapping, Proxmox QEMU rewrites each `netN` bridge, and XCP-ng moves every VIF to the selected network. Proxmox LXC DR-test remapping is blocked in this release rather than guessed.

## Appliance and one-line installation

### Bootstrap installer

For a clean Ubuntu 24.04 LTS VM after the immutable `v1.1.0` tag exists:

```bash
curl -fsSL https://raw.githubusercontent.com/bnrohit/immutavault/v1.1.0/scripts/bootstrap.sh | sudo bash -s -- --enable-services
```

For stronger supply-chain pinning, set `IMMUTAVAULT_ARCHIVE_SHA256` to the approved source-archive digest before running the bootstrap. The script accepts only a release-style version and downloads that exact GitHub tag; it does not silently follow `main`.

`get.immutavault.io` is **not** claimed as a live endpoint by this release. A vanity-domain bootstrap should only be documented after the domain is actually controlled and serving the pinned installer.

### Appliance image builder

`scripts/build_appliance.sh` builds from an operator-supplied Ubuntu 24.04 cloud image whose SHA-256 must be supplied and verified. It can emit:

- `immutavault-1.1.0.qcow2` for Proxmox/KVM;
- `immutavault-1.1.0.ova` containing a stream-optimized VMDK for VMware;
- `immutavault-1.1.0.vhd` for documented XCP-ng VHD import workflows;
- `SHA256SUMS` for generated artifacts.

The release deliberately does **not** rename a VHD/QCOW2 file to `.xva`. A native XVA package has different metadata/packaging requirements and remains unpublished until its builder is separately validated. See `docs/APPLIANCE_DEPLOYMENT.md`.

## v1.0/v1.0.1 certified V2V retained

The built-in certification path remains **VMware export-format -> Proxmox QEMU/KVM**. It accepts an immutable, verified VMware OVF/export-style recovery point and converts it to a new Proxmox VM. It never overwrites the source or an existing target and leaves the conversion target **powered off** until an isolated acceptance workflow intentionally boots it.

Built-in certification ID:

```text
immutavault-vmware-proxmox-v1
```

The pipeline retains:

- source recovery-point SHA-256 manifest verification;
- `virt-v2v` >= 2.12.0;
- Windows signed **VirtIO** driver injection requirements;
- target storage and bridge **target readiness** checks;
- BIOS/UEFI conversion rules;
- explicit NIC mapping;
- source **Secure Boot** blocked by default;
- source **vTPM** blocked;
- rollback/delete of the new target without touching the immutable source.

Native VMware VDDK/CBT layouts are not mislabeled as OVF. VMware/Proxmox -> XCP-ng and other non-built-in pairs require a separately SHA-256-pinned **certified provider** or remain blocked. See `docs/CERTIFIED_V2V.md`.

V2V remains opt-in:

```yaml
v2v:
  enabled: false
  builtin_vmware_to_proxmox: true
  require_verified_point: true
  allow_suspicious_points: false
  virt_v2v_min_version: "2.12.0"
  require_network_mapping: true
  allow_uefi: true
  allow_secure_boot: false
```

## VMware strict native incremental protection retained

Broadcom VDDK is **not bundled** or redistributed. Install an authorized compatible helper separately.

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: vddk
    include: ["*"]
    options:
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_strict: true
      incremental_fallback: false
      quiesce: true
      quiesce_fallback_crash_consistent: false
      application_consistency_strict: true
```

`incremental_strict: true` is fail closed: ambiguous CBT/provider state cannot silently become a fake incremental point or advance the checkpoint. `fallback_safe` remains an explicitly tracked decision in the native transport contract.

## Enterprise controls retained

- multi-tenant ownership of every hypervisor platform;
- cross-tenant restore/V2V prohibition;
- Microsoft Entra ID / generic OIDC authorization-code login with PKCE;
- MFA evidence enforcement;
- group/app-role RBAC and tenant mappings;
- Prometheus/OpenMetrics metrics;
- WebSocket progress telemetry with short-lived signed tickets;
- tamper-evident audit-chain verification;
- four-eyes restore approval;
- immutable S3/R2 copies where provider capabilities support them;
- online SQLite control-plane backup;
- versioned controller install/rollback model.

OIDC MFA evidence checking supports Zero-Trust controls but does not replace Entra Conditional Access, identity lifecycle governance, network policy, or recovery testing.

## File-level recovery retained

Read-only file-level recovery uses restic FUSE + libguestfs in the local FLR broker. Owner-scoped sessions, path traversal checks, guest-symlink restrictions, maximum download size, private mount namespaces and `SO_PEERCRED` peer validation remain mandatory. If the FLR broker is unavailable, the portal fails closed instead of mounting guest filesystems itself.

## Install from source

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
git checkout v1.1.0
sudo ./scripts/preflight.sh
./scripts/release_check.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault --enable-services
```

Then sign in to the portal and use **Setup & Manage**. The standalone setup console is retained for local/break-glass bootstrap:

```bash
sudo ./scripts/launch_setup_console.sh
```

Do not enable production schedules or DR promotion until `doctor`, discovery, policy dry-run, a real backup, immutable-copy verification, FLR, same-family restore, and the applicable isolated/V2V acceptance tests all pass.

## Core commands

```bash
immutavault --config /etc/immutavault/immutavault.yml doctor
immutavault --config /etc/immutavault/immutavault.yml status
immutavault --config /etc/immutavault/immutavault.yml inventory
immutavault --config /etc/immutavault/immutavault.yml storage-targets
immutavault --config /etc/immutavault/immutavault.yml policy-list
immutavault --config /etc/immutavault/immutavault.yml management-status
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml v2v-doctor
immutavault --config /etc/immutavault/immutavault.yml v2v-plan --snapshot SNAPSHOT --target-platform TARGET
immutavault --config /etc/immutavault/immutavault.yml portal
```

## Important boundaries

- Built-in V2V is not a universal converter.
- XCP-ng conversion targets still require an appropriate certified provider.
- Automatic target overwrite is prohibited.
- Production DR fencing/routing ownership is separate from an isolated DR test.
- Automatic DR is not enabled by installation.
- Application consistency depends on guest/application quiescing and must be proven per workload.
- VDDK/CBT is incremental backup, not CDP; RPO still depends on successful schedule execution.
- A generated appliance image is only as trustworthy as its pinned base-image digest and the release source used to build it.

## Documentation

- `docs/UNIFIED_MANAGEMENT.md` — v1.1 setup, policies, NAS onboarding and isolated DR tests
- `docs/APPLIANCE_DEPLOYMENT.md` — bootstrap and QCOW2/OVA/VHD appliance build/deployment
- `docs/CERTIFIED_V2V.md` — certified conversion matrix and provider protocol
- `docs/ENTERPRISE_OPERATIONS.md` — tenancy, Entra/OIDC, metrics and WebSockets
- `docs/FILE_LEVEL_RECOVERY.md` — granular recovery and FLR broker boundary
- `docs/VMWARE_BACKUP.md` — VDDK/CBT and application-consistency policy
- `docs/PRODUCTION_ACCEPTANCE.md` — go-live gates
- `docs/DR_RUNBOOK.md` — production failover/failback and fencing
- `docs/SECURITY.md` — trust boundaries and hardening

## License

Apache-2.0. See `LICENSE`.
