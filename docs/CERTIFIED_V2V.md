# Certified Enterprise V2V — Immutavault v1.0

Immutavault v1.0 introduces cross-hypervisor recovery behind an explicit certification policy. The feature is designed for disaster recovery and migration, where a conversion that merely imports disks but does not boot reliably is not a successful recovery.

`certified` in this document means **tested against the Immutavault safety and acceptance profile**. It does not imply certification, endorsement, or support by Broadcom/VMware, Proxmox, Vates, XCP-ng, Red Hat, or any other vendor unless a separate vendor agreement states that explicitly.

## Safety contract

Cross-hypervisor recovery remains fail-closed unless all of the following are true:

1. `v2v.enabled: true` is explicitly configured.
2. The source recovery point passed Immutavault verification when `require_verified_point: true`.
3. The source recovery point is not marked suspicious unless an operator deliberately changes policy.
4. The source and target belong to the same Immutavault tenant.
5. The source recovery-point format is accepted by the selected certified path.
6. Conversion tooling passes capability/version checks.
7. Guest OS, architecture, firmware, disk count, capacity, Secure Boot/TPM state and driver prerequisites fit the certification matrix.
8. Every target NIC has an explicit or deliberately configured fallback mapping.
9. The target VM ID/name does not already exist.
10. The immutable source is restored to staging and its SHA-256 manifest is verified before conversion.
11. The converted target is created as a **new VM**. Source workloads are never modified by V2V.
12. The converted VM remains **powered off** after automatic conversion. Isolated boot acceptance is a separate operator step.

A failure at any stage must not cause automatic conversion through a different hypervisor path.

## Built-in certification matrix

### VMware/vCenter -> Proxmox VE

Status: **built-in v1.0 certified pipeline** for the scope below.

| Dimension | v1.0 built-in scope |
| --- | --- |
| Source recovery format | VMware OVF/export-style recovery point |
| Source transports | `hot-clone-export`, `snapshot-clone-export`, `hot`, `export`, `cold-export` |
| Native VMware VDDK/CBT layout | Not treated as OVF; blocked by built-in path |
| Target | Proxmox VE QEMU/KVM VM |
| Guest architecture | x86_64/amd64 |
| Guest OS families | Linux and Windows when `virt-v2v` reports support |
| Conversion engine | `virt-v2v` >= 2.12.0 with required machine-readable capabilities |
| Disk output | sparse qcow2, imported to configured Proxmox storage |
| Target disk bus | VirtIO |
| Windows VirtIO | signed driver source required (`VIRTIO_WIN` or `/usr/share/virtio-win`) |
| Legacy BIOS | preserved as SeaBIOS |
| UEFI | preserved as OVMF/q35 with a new target EFI disk |
| Source Secure Boot | blocked by default |
| Source vTPM | blocked |
| NICs | VirtIO NICs, MAC preserved where conversion metadata provides it |
| Network attachment | explicit source-network -> Proxmox bridge mapping |
| Existing target overwrite | prohibited |
| Automatic target power-on | prohibited |

The built-in path packages the verified VMware OVF payload into a temporary OVA, inspects it with `virt-v2v-inspector`, converts with `virt-v2v -i ova -o local`, validates output with `qemu-img`, creates a new Proxmox VM, imports the converted disks, maps them to VirtIO buses, remaps NICs, validates the resulting `qm config`, and leaves the VM powered off.

### VMware VDDK/CBT recovery points

A native VDDK/CBT point is a valid Immutavault recovery point, but its native disk layout is not silently reinterpreted as an OVF. The built-in VMware -> Proxmox converter therefore rejects native `vddk`, `vddk-cbt`, `cbt`, or `auto` transport points unless the point itself is an attested full export format.

For planned cross-hypervisor DR, maintain an export-format recovery point according to policy or deploy a separately certified V2V provider that explicitly understands the native incremental layout.

### VMware/Proxmox -> XCP-ng

Status: **blocked in the built-in v1.0 core**.

Upstream `virt-v2v` supports QEMU/KVM output; Immutavault does not pretend that this is equivalent to a native XCP-ng/Xen conversion pipeline. XCP-ng recovery requires a separately tested provider configured through the v1.0 provider protocol. Without that provider, the request is rejected before conversion begins.

### Other source/target pairs

Any pair not listed as built-in requires an enabled, SHA-256-pinned certified provider. Unknown pairs remain blocked.

## Configuration

V2V is disabled after upgrade:

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

The recommended production posture is to leave `allow_suspicious_points: false` and `allow_secure_boot: false` until an environment-specific acceptance profile has explicitly validated those cases.

## Proxmox target policy

Example target:

```yaml
platforms:
  - name: pve-dr
    type: proxmox
    endpoint: pve-dr.example.local
    ssh_user: backupsvc
    options:
      v2v_storage: local-lvm
      v2v_efi_storage: local-lvm
      v2v_default_bridge: vmbr0
      v2v_network_map:
        "VM Network": vmbr0
        "Servers": vmbr20
        "Database": vmbr30
```

`v2v_storage` is mandatory for the built-in path. UEFI conversion also needs valid EFI storage. Network mappings are validated before target creation when possible. A missing mapping fails the conversion when `require_network_mapping: true`.

Do not map production networks casually during testing. For acceptance, map all converted NICs to an isolated recovery VLAN/bridge first.

## VirtIO injection and Windows

`virt-v2v` performs guest-side conversion needed for KVM, including Windows VirtIO driver installation where supported. Immutavault additionally checks that a Windows VirtIO driver source exists before allowing a Windows conversion.

Supported driver discovery:

```text
VIRTIO_WIN=<path-to-driver-tree-or-ISO>
/usr/share/virtio-win
/usr/share/virtio-win/virtio-win.iso
```

Windows guests should be cleanly shut down for a migration test and should not be left in Fast Startup/hibernated state. Application recovery should be validated independently after first boot.

## Firmware behavior

### Legacy BIOS

Legacy BIOS guests are created with Proxmox SeaBIOS and converted disks on VirtIO buses.

### UEFI

UEFI guests are created using OVMF/q35 and a new Proxmox EFI disk. The source EFI variables are not assumed to be portable firmware state.

### Secure Boot

Secure Boot is blocked by default because moving a guest between firmware implementations while claiming equivalent Secure Boot trust would be unsafe. `allow_secure_boot` exists as an explicit policy control, but enabling it is not a substitute for a tested key/enrollment procedure.

### vTPM

Source vTPM is blocked in the built-in v1.0 path. Immutavault does not fabricate or silently discard TPM-protected state. BitLocker, credential guards, measured boot, or other TPM-dependent workloads require a dedicated migration procedure and acceptance test.

## Recovery point eligibility

Use:

```bash
immutavault --config /etc/immutavault/immutavault.yml v2v-plan \
  --snapshot SNAPSHOT_ID \
  --target-platform pve-dr
```

An eligible built-in plan reports:

```json
{
  "allowed": true,
  "source_type": "vmware",
  "target_type": "proxmox",
  "mode": "builtin",
  "certification_id": "immutavault-vmware-proxmox-v1"
}
```

A native VDDK recovery point, unverified point, suspicious point, unsupported target pair or missing target storage/network policy returns `allowed: false` with explicit reasons.

## Tooling preflight

Run:

```bash
./scripts/check_v2v.sh 2.12.0
immutavault --config /etc/immutavault/immutavault.yml v2v-doctor
```

The built-in profile requires:

- `virt-v2v` >= 2.12.0;
- `virt-v2v-inspector`;
- `qemu-img`;
- `virt-v2v --machine-readable` features `input:ova`, `output:local`, `convert:linux`, and `convert:windows`;
- SSH/SCP and Proxmox `qm`, `pvesh`, and related adapter prerequisites;
- signed VirtIO Windows drivers for Windows V2V.

Immutavault does not silently install an unpinned third-party conversion stack. Install and lifecycle-manage a supported `virt-v2v` build that meets the version/capability gate.

## Restore workflow

1. Select an immutable, verified VMware export-format recovery point.
2. Run `v2v-plan` for the intended target.
3. Confirm target storage and isolated bridge mappings.
4. Create a restore request through CLI/API/portal.
5. A separate approver approves it when four-eyes policy is enabled.
6. Execute the restore.
7. Immutavault restores the encrypted source point to staging.
8. SHA-256 manifest verification must pass.
9. Guest conversion/driver/boot adaptation runs in temporary scratch space.
10. A new powered-off target VM is created.
11. Converted disks and NICs are attached and the target configuration is validated.
12. The restore result and certification profile are written to the tamper-evident audit trail.
13. Operator boots the target only on an isolated recovery network.
14. OS, storage, NIC, DNS, application, data integrity and security controls are validated.
15. Production routing/IP ownership changes only after the acceptance record is approved.

## Provider protocol for XCP-ng and other pairs

A provider is an external executable with an absolute path and exact SHA-256 pin:

```yaml
v2v:
  providers:
    - name: site-certified-xcpng
      helper: /usr/local/libexec/immutavault-v2v-xcpng
      sha256: <64-lowercase-hex-digest>
      certification_id: CAB-2026-001
      pairs: ["vmware:xcpng"]
      enabled: true
```

The helper is invoked with JSON on stdin and JSON on stdout. Protocol version is `1`.

### `capabilities`

The provider must attest:

```json
{
  "protocol": 1,
  "certification_id": "CAB-2026-001",
  "pairs": ["vmware:xcpng"],
  "features": ["inspect", "convert", "validate", "rollback"]
}
```

The configured file SHA-256 is verified before every invocation. A changed binary is blocked until configuration is deliberately updated under change control.

### `plan` / `convert`

Immutavault sends the source snapshot/path, source and target families, target name, options and mandatory safety properties. A successful actual conversion must return a result object and explicit validation:

```json
{
  "status": "success",
  "certification_id": "CAB-2026-001",
  "result": {
    "platform": "xcp-dr",
    "name": "server1-v2v"
  },
  "validation": {
    "source_read_only": true,
    "target_new_vm": true,
    "network_mapped": true,
    "rollback_available": true
  }
}
```

Missing/false validation, mismatched certification ID, invalid JSON, helper crash, SHA mismatch or unadvertised pair fails closed.

## Certification acceptance matrix

Before enabling a source/target combination in production, record at least one successful isolated conversion for every materially different class in use:

- guest OS/version;
- Linux distribution/kernel family or Windows release;
- BIOS vs UEFI;
- single/multi-disk;
- GPT/MBR where applicable;
- LVM/storage spaces/dynamic disks where applicable;
- encrypted guest volume behavior;
- one/multiple NICs;
- static vs DHCP guest networking;
- application type (AD/DNS, SQL/database, file server, application server, etc.);
- target storage backend;
- target Proxmox version;
- target bridge/VLAN design.

For each test capture:

1. source snapshot ID and manifest verification result;
2. Immutavault version and V2V certification ID;
3. `virt-v2v`/`qemu-img` versions;
4. source OS/architecture/firmware inspection;
5. target VM ID/name;
6. disk and NIC mapping;
7. isolated boot result;
8. guest storage visibility;
9. network reachability on recovery VLAN only;
10. OS logs/driver health;
11. application start and data-integrity test;
12. shutdown/reboot test;
13. rollback/delete-new-target test;
14. approver/operator identities from the audit trail.

A test of one Windows release or one Linux distribution does not certify every guest in the estate.

## Rollback

The immutable source recovery point is never changed. If target creation/import fails, the built-in Proxmox path attempts to destroy the newly created target VM and temporary imported storage, then removes temporary transfer files. If cleanup itself cannot be proven, the restore is marked failed and the target requires operator review; it is never automatically powered on.

For a conversion that reaches powered-off success but fails isolated boot acceptance, delete/quarantine the new target VM and return to the unchanged immutable recovery point. Do not overwrite the original production VM.

## DR integration

Cross-hypervisor V2V does not remove normal DR fencing requirements. Before putting a converted VM on the production subnet:

- prove the old production instance is fenced or isolated;
- confirm only one site owns the production gateway/IP route;
- validate VLAN/MTU/firewall/DNS dependencies;
- validate the converted guest on an isolated recovery network;
- use the normal Immutavault four-eyes/change-control process for promotion.

Conversion solves the compute-format problem; it does not make split-brain networking safe by itself.

## Deliberate v1.0 boundaries

- Built-in automatic V2V is VMware export-format -> Proxmox KVM only.
- Native VMware VDDK/CBT layouts need an appropriate certified provider or a separate export-format recovery point.
- XCP-ng targets require a separately certified provider.
- Source Secure Boot is blocked by default.
- Source vTPM is blocked.
- ARM/aarch64 guests are outside the built-in v1.0 conversion matrix.
- Automatic cross-tenant conversion remains prohibited.
- Existing target overwrite remains prohibited.
- Automatic target power-on remains prohibited.
- A successful conversion is not declared production-ready until isolated boot/application acceptance passes.
