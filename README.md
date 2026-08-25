# Immutavault v1.0.1

Immutavault is an open, vendor-neutral **immutable VM backup, recovery, certified V2V conversion, replication, file-level recovery and disaster-recovery orchestrator** for VMware/vCenter, Proxmox VE and XCP-ng.

The security principle remains simple: **the identity that creates backups does not receive prune/delete authority.** The controller writes through an append-only repository service, retention/prune is separated, recovery creates a **new VM** by default, and v1.0 cross-hypervisor conversion stays fail-closed unless a tested conversion profile explicitly permits the exact source and target families.

> **Readiness statement:** v1.0.1 is a production-pilot hardening release for certified cross-hypervisor recovery. The built-in certification profile supports verified VMware **export-format** recovery points to new powered-off Proxmox KVM VMs using `virt-v2v` >= 2.12.0, VirtIO disk/NIC adaptation, BIOS/UEFI handling and explicit network remapping. v1.0.1 additionally isolates file-level recovery mount privileges behind a local FLR broker and validates Proxmox image storage/bridges before target creation. Native VMware VDDK/CBT layouts and XCP-ng targets are not silently guessed: they require an appropriate separately certified provider or remain blocked. Production use still requires the acceptance matrix in `docs/CERTIFIED_V2V.md` on the actual guests, hypervisors, storage and recovery networks.

## v1.0.1 — Hardening & operational cleanup

- **Privilege-separated FLR broker:** the network-facing portal no longer performs FUSE/libguestfs mounts and runs with `NoNewPrivileges=true`, `PrivateDevices=true`, and an empty capability set. A local root broker owns `/dev/fuse` access inside a private mount namespace and accepts only authenticated local Unix-socket peers.
- **Owner-bound FLR sessions:** browse, download and close operations stay bound to the identity that created the session. The old admin close edge case is handled without restoring cross-user mounted-session access.
- **V2V target readiness gate:** the built-in VMware -> Proxmox path checks that configured storage is enabled, active and accepts VM-image content before conversion, then rechecks storage and verifies every mapped Linux bridge before VM creation.
- **Fail-fast DR behavior:** an offline datastore or missing recovery bridge is rejected before a target VM is created; conversion never falls through to an uncertified path.
- **Release contract cleanup:** CI now verifies broker privilege separation, wheel entry points, systemd hardening and V2V target-readiness checks as part of the release gate.

## v1.0 — Certified Enterprise DR & Seamless V2V

### Built-in certified path: VMware -> Proxmox

The built-in v1.0 path accepts an immutable, verified VMware OVF/export-style recovery point and converts it into a **new Proxmox QEMU/KVM VM**. It never modifies the source VM and never overwrites an existing target VM.

Pipeline:

1. Re-evaluate V2V policy at restore request and again at execution.
2. Require a verified recovery point and reject suspicious points by default.
3. Require an attested VMware export transport; native CBT/VDDK layouts are not mislabeled as OVF.
4. Verify that the target Proxmox image storage is enabled and active.
5. Restore the encrypted recovery point to isolated staging.
6. Verify the recovery-point SHA-256 manifest before conversion.
7. Reject unsafe OVF file references and symlinked source content.
8. Inspect the guest with `virt-v2v-inspector`.
9. Enforce the certified guest architecture/firmware/disk limits.
10. Convert with `virt-v2v` into sparse qcow2 output.
11. Validate converted images with `qemu-img`.
12. Recheck target storage and validate every mapped Proxmox bridge.
13. Create a new Proxmox VM with preserved CPU/RAM intent.
14. Attach converted disks on VirtIO buses.
15. Preserve BIOS as SeaBIOS or UEFI as OVMF/q35.
16. Map each NIC to an explicitly configured Proxmox bridge and preserve MAC where available.
17. Validate the resulting Proxmox configuration.
18. Leave the converted VM **powered off** for isolated boot/application acceptance.

Built-in certification ID:

```text
immutavault-vmware-proxmox-v1
```

### Source-format guardrail

The built-in VMware -> Proxmox path is certified for VMware export-style points produced by transports such as:

```text
hot-clone-export
snapshot-clone-export
hot
export
cold-export
```

A native `vddk`, `vddk-cbt`, `cbt`, or `auto` point is a valid Immutavault recovery point, but it is **not automatically an OVF**. v1.0 refuses to guess. Use a separately certified provider that understands the native layout or maintain an export-format point for cross-hypervisor DR.

### Windows VirtIO injection

For Windows guests, the conversion host must provide signed VirtIO drivers through `VIRTIO_WIN` or the normal `/usr/share/virtio-win` installation. If the driver source is unavailable, Windows V2V fails before target creation.

### BIOS / UEFI

- Legacy BIOS guests are created with SeaBIOS.
- UEFI guests are created with OVMF/q35 and a new target EFI disk.
- Source Secure Boot is blocked by default because v1.0 does not claim to migrate the source firmware trust state.
- Source vTPM is blocked; Immutavault does not fabricate or silently discard TPM-protected state.

### NIC remapping

Each target NIC is attached to a Proxmox bridge selected from the target policy:

```yaml
options:
  v2v_storage: local-lvm
  v2v_efi_storage: local-lvm
  v2v_default_bridge: vmbr0
  v2v_network_map:
    "VM Network": vmbr0
    "Servers": vmbr20
    "Database": vmbr30
```

With `require_network_mapping: true`, missing mappings fail closed. v1.0.1 also verifies that every selected bridge exists on the Proxmox node before VM creation. For DR acceptance, map converted VMs to an isolated recovery VLAN/bridge before any production cutover.

### XCP-ng and other conversion pairs

Upstream `virt-v2v` targets QEMU/KVM, so Immutavault does **not** pretend that its built-in KVM pipeline is an XCP-ng converter. VMware -> XCP-ng, Proxmox -> XCP-ng and other non-built-in pairs remain blocked unless an administrator configures a separately tested provider.

External providers are executable files pinned by exact SHA-256 and a `certification_id`. Protocol v1 requires the provider to advertise `inspect`, `convert`, `validate`, and `rollback`, and a successful conversion must explicitly attest:

```text
source_read_only=true
target_new_vm=true
network_mapped=true
rollback_available=true
```

A binary hash change, provider crash, malformed JSON, mismatched certification ID, unadvertised pair or incomplete validation fails closed.

## V2V policy example

V2V remains disabled after upgrade:

```yaml
v2v:
  enabled: false
  builtin_vmware_to_proxmox: true
  require_verified_point: true
  allow_suspicious_points: false
  virt_v2v_min_version: "2.12.0"
  max_disks: 16
  max_virtual_bytes: 70368744177664
  require_network_mapping: true
  allow_uefi: true
  allow_secure_boot: false
  provider_timeout_seconds: 14400
  providers: []
```

Start from `config/enterprise-v1.0.example.yml` and enable V2V only after the isolated acceptance matrix has passed.

## V2V preflight and planning

```bash
./scripts/check_v2v.sh 2.12.0
immutavault --config /etc/immutavault/immutavault.yml v2v-doctor
immutavault --config /etc/immutavault/immutavault.yml v2v-plan \
  --snapshot SNAPSHOT_ID \
  --target-platform pve-dr
```

A valid plan reports `allowed: true` with its certification ID. Unverified/suspicious points, native VDDK layouts for the built-in OVF path, unknown target pairs, missing target storage or unsupported provider state report `allowed: false` with the reason. Execution additionally performs live Proxmox storage and bridge readiness checks before target creation.

## v0.9 enterprise operations retained

v1.0 keeps the v0.9 enterprise control plane:

- tenant ownership for every hypervisor platform;
- tenant-scoped VM, recovery-point, FLR and restore authorization;
- Microsoft Entra ID / generic OIDC authorization-code login with PKCE;
- explicit MFA-evidence enforcement;
- group/app-role RBAC and tenant mappings;
- Prometheus/OpenMetrics-compatible metrics;
- short-lived-ticket WebSocket operations telemetry;
- Grafana, Datadog and PagerDuty integration patterns;
- full audit/system-health endpoints restricted to global administrators.

Cross-hypervisor V2V remains subject to the same tenant boundary. Cross-tenant conversion is prohibited.

## v0.8 granular recovery retained and hardened

- Read-only file-level recovery through restic FUSE + libguestfs.
- A privilege-separated **FLR broker** performs mount operations; the portal has no direct `/dev/fuse` access.
- Owner-scoped, short-lived FLR sessions.
- Unix-socket peer credential validation between portal and broker.
- Private mount namespace for recovery mounts.
- Path traversal and symlink protections.
- Single-file downloads without full VM import.
- Application-consistency metadata stored with recovery points.

## VMware strict native incremental protection retained

Broadcom VDDK is **not bundled** or redistributed. Install an authorized compatible `immutavault-vddk` helper separately.

```yaml
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vcenter.example.local/sdk
    mode: vddk
    include: ["*"]
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      vddk_helper: /usr/local/bin/immutavault-vddk
      incremental_strict: true
      incremental_fallback: false
      incremental_cache_root: /var/cache/immutavault/vddk
      quiesce: true
      quiesce_fallback_crash_consistent: false
      application_consistency_strict: true
      vddk_transport_order: [san, hotadd, nbdssl]
```

`incremental_strict: true` prevents automatic hot-clone fallback, fake incremental recovery points and unsafe CBT checkpoint advancement after ambiguous provider state.

## Immutable recovery and DR capabilities

- VMware/vCenter, Proxmox VE and XCP-ng protection adapters.
- Encrypted, deduplicated restic repository.
- Authenticated TLS append-only repository writer path.
- Separate controller/repository identities.
- GFS retention with protected immutable windows.
- SHA-256 manifest verification and staged recovery-point verification.
- Backup-churn/ransomware anomaly detection with suspicious-point preservation.
- Tamper-evident SHA-256 audit chain.
- Four-eyes restore approval.
- S3-compatible replicas and provider immutability where supported.
- Cloudflare R2 Bucket Lock kept distinct from S3 Object Lock.
- NFS/SMB/filesystem replicas.
- Online SQLite control-plane backups.
- Versioned atomic application upgrade/rollback.
- Multi-site DR orchestration, fencing, VXLAN recovery networks and FRR/OSPF ownership controls.
- Certified V2V restore auditing with source snapshot, conversion profile and validation metadata.

## Fast install on Ubuntu 24.04 LTS

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
git checkout v1.0.1
sudo ./scripts/preflight.sh
./scripts/release_check.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
sudo ./scripts/launch_setup_console.sh
```

The base installer includes libguestfs/qemu tooling used by FLR, but it does not claim that every distribution package provides the certified `virt-v2v` version. Install/lifecycle-manage a supported `virt-v2v` >= 2.12.0 build and pass `check_v2v.sh` before enabling built-in V2V.

Do not enable recurring production schedules or cross-hypervisor promotion until `doctor`, inventory, dry-run, a real backup, recovery-point verification, FLR, same-family restore and the applicable V2V acceptance tests all pass.

## Core commands

```bash
immutavault --config /etc/immutavault/immutavault.yml doctor
immutavault --config /etc/immutavault/immutavault.yml status
immutavault --config /etc/immutavault/immutavault.yml inventory
immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run
immutavault --config /etc/immutavault/immutavault.yml backup --all
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml v2v-doctor
immutavault --config /etc/immutavault/immutavault.yml v2v-plan --snapshot SNAPSHOT --target-platform TARGET
immutavault --config /etc/immutavault/immutavault.yml portal
```

## Production V2V acceptance

A successful conversion is **not** automatically a production-ready recovery. The converted target intentionally stays powered off. Before production promotion:

1. Boot it on an isolated recovery network.
2. Confirm OS boot and storage visibility.
3. Validate VirtIO/storage/network drivers.
4. Verify NIC/VLAN mapping and MAC behavior.
5. Validate filesystem/application/database consistency.
6. Test reboot and clean shutdown.
7. Validate monitoring, backup agent/application dependencies and security controls.
8. Prove rollback by deleting/quarantining the new target without touching the immutable source.
9. Fence/isolate the original production workload before assigning production IP/routing ownership.
10. Record the source snapshot, tool versions, certification ID and acceptance evidence under change control.

See `docs/CERTIFIED_V2V.md` for the full guest/firmware/storage/network matrix.

## Important boundaries

- Built-in v1.0 V2V is VMware **export-format** -> Proxmox KVM; it is not a universal converter.
- Native VMware VDDK/CBT layouts need a suitable certified provider or an export-format point for built-in V2V.
- XCP-ng conversion targets require a separately certified provider.
- Source Secure Boot is blocked by default; source vTPM is blocked.
- Built-in guest architecture is x86_64/amd64.
- Windows V2V requires signed VirtIO driver media/tree.
- Automatic target overwrite and automatic post-conversion power-on are prohibited.
- Cross-tenant V2V is prohibited.
- Conversion does not replace DR fencing, routing ownership, application validation or change control.
- The portal is intentionally unprivileged for FLR; if `immutavault-flr.service` is unavailable, FLR fails closed instead of mounting directly in the web process.
- VDDK/CBT is incremental backup, not CDP; RPO still depends on schedule and successful runs.
- Application consistency depends on guest/application quiescing and must be tested per workload.
- Prometheus is monitoring, not a restore-control surface.
- Entra/OIDC MFA evidence checks do not replace Conditional Access.
- Automatic DR is not enabled by installation.

## Documentation

- `docs/CERTIFIED_V2V.md` — v1.0/v1.0.1 conversion matrix, provider protocol, target readiness and acceptance procedure
- `docs/ENTERPRISE_OPERATIONS.md` — v0.9 tenancy, Entra/OIDC, Prometheus, WebSockets and NOC/SOC integrations
- `docs/FILE_LEVEL_RECOVERY.md` — granular read-only file recovery and v1.0.1 FLR broker security boundary
- `docs/VMWARE_BACKUP.md` — VDDK/CBT, application consistency and fallback policy
- `docs/INCREMENTAL_STRICT_MODE.md` — fail-closed native incremental policy
- `docs/PRODUCTION_ACCEPTANCE.md` — general go-live gates
- `docs/INSTALLATION.md` — installation
- `docs/OPERATIONS.md` — day-2 operations
- `docs/RESTORE.md` — restore runbook
- `docs/DR_RUNBOOK.md` — failover/failback and fencing
- `docs/HIGH_AVAILABILITY.md` — HA design
- `docs/SECURITY.md` — security model
- `docs/ARCHITECTURE.md` — components and trust boundaries
- `docs/CLOUD_STORAGE.md` — S3/NFS/SMB targets
- `docs/VEEAM_GAP_MATRIX.md` — enterprise capability comparison

## License

Apache-2.0. See `LICENSE`.
