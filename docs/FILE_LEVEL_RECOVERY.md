# File-Level Recovery (FLR) — v1.0.1

Immutavault can recover an individual file without restoring and importing the complete VM first. v1.0.1 keeps the v0.8 read-only recovery model but moves all mount privilege out of the network-facing portal.

The FLR data path is deliberately read-only and privilege-separated:

```text
HTTPS portal (User=immutavault, no /dev/fuse, no capabilities)
        |
        | owner-bound JSON request over /run/immutavault/flr.sock
        v
local FLR broker (root, private mount namespace, SO_PEERCRED)
        |
        | restic mount --no-lock
        v
short-lived FUSE view on the vault
        |
        | guestmount --ro / libguestfs
        v
read-only guest filesystem tree
        |
        +--> brokered directory listing
        +--> brokered streamed single-file download
```

The portal does **not** receive prune, forget, repository-maintenance, snapshot-delete, FUSE-mount, or host-device authority. FLR reads the same immutable recovery point used for a full restore.

## Supported disk exposure

FLR can inspect standard VM disk images visible inside the recovery point:

- VMware VMDK descriptors and their extents;
- QCOW2;
- RAW/IMG;
- VHD/VHDX where libguestfs/qemu supports the guest format.

For the external VDDK/CBT provider path, the provider can publish read-only mountable images in `immutavault-vddk-layout.json` using `flr_disk_images` (or `flr_disks`). If a native block layout cannot expose a mountable image, Immutavault fails the FLR request explicitly instead of reconstructing or pretending to expose a safe filesystem.

Example provider layout fragment:

```json
{
  "version": 1,
  "flr_disk_images": [
    {"path": "disks/disk-2000.vmdk"}
  ]
}
```

The VDDK libraries themselves remain external and are not bundled by Immutavault.

## Host prerequisites

On the vault/controller that serves FLR, install:

- FUSE 3 (`fuse3` / `fusermount3`);
- libguestfs tools (`guestmount`, `guestunmount`);
- qemu image tooling for guest disk format support;
- restic 0.19.1+.

The appliance installer installs these dependencies on supported package-manager paths. `/dev/fuse` must be available to **`immutavault-flr.service`**, not to the `immutavault` portal identity. The installer removes the dedicated portal user from the `fuse` group on upgrade when possible.

Check manually:

```bash
./scripts/check_flr.sh
systemctl status immutavault-flr.service
systemctl status immutavault-portal.service
ls -l /run/immutavault/flr.sock
ls -l /dev/fuse
```

The expected portal hardening is:

```text
NoNewPrivileges=true
PrivateDevices=true
CapabilityBoundingSet=
```

The FLR broker uses a private mount namespace, verifies Linux `SO_PEERCRED` on each Unix-socket connection, and owns the root-only FLR mount tree.

## Configuration

FLR settings remain part of the validated YAML schema:

```yaml
flr:
  enabled: true
  mount_root: /srv/immutavault/flr
  session_ttl_minutes: 30
  max_download_bytes: 5368709120
  max_sessions_per_user: 2
  max_disks: 16
  mount_wait_seconds: 30
```

The broker socket defaults to `/run/immutavault/flr.sock`. It can be overridden for controlled packaging/testing with `IMMUTAVAULT_FLR_BROKER_SOCKET`; normal appliances should use the default systemd-managed runtime path.

Limits are intentionally bounded by schema validation. The mount root must be absolute, session TTL is capped, concurrent sessions are limited per user, and individual portal downloads are size-limited.

## Portal workflow

1. Open **Recovery points** for the protected VM.
2. Choose **Files** on the desired point.
3. The portal asks the local FLR broker to open a short-lived session owned by the authenticated identity.
4. The broker mounts the immutable point and guest filesystems read-only.
5. Browse directories in the **File-level recovery** panel.
6. Choose **Download** for the required regular file.
7. Close the FLR session. Sessions also expire automatically.

Only `restore_operator` and `admin` roles can open FLR sessions or download files. **Admin role does not bypass active FLR session ownership.** A global or tenant admin can create a fresh session against any recovery point already authorized to that identity, but cannot attach to another user's mounted filesystem session.

## Security controls

v1.0.1 applies these controls before exposing guest data:

- recovery repository mounted read-only with `restic mount --no-lock`;
- guest disk inspection mounted with `guestmount --ro`;
- mount operations isolated from the network portal in `immutavault-flr.service`;
- Unix-socket peer validation using Linux `SO_PEERCRED`;
- portal runs with `NoNewPrivileges=true`, `PrivateDevices=true`, and an empty capability set;
- FLR broker uses a private mount namespace;
- per-user session ownership with no admin browse/download bypass;
- random session IDs;
- root-owned mode-0700 temporary session directories;
- bounded session lifetime and concurrent-session limit;
- `..` path traversal rejected;
- guest symlinks are never followed for browse/download;
- device nodes/sockets/FIFOs/special files cannot be downloaded;
- only regular files can be streamed;
- maximum single-file download size enforced server-side;
- FLR open/close/download actions recorded in the tamper-evident audit log;
- no repository delete/prune authority is added to the portal or broker.

## Application consistency

Immutavault records an application-consistency attestation inside the protected recovery payload and in the recovery-point catalog.

For VMware `hot-clone-export`:

```yaml
options:
  quiesce: true
  quiesce_fallback_crash_consistent: false
  application_consistency_strict: true
```

A successful Tools-quiesced VMware snapshot is recorded as `guest-quiesced`. This indicates VMware accepted guest quiescing; actual application/VSS coverage still depends on VMware Tools, VSS/application writers, and workload-specific testing.

For native VDDK/CBT with `application_consistency_strict: true`, the external provider must return an explicit consistency attestation such as:

```json
{
  "status": "success",
  "consistency": {
    "state": "application-consistent",
    "method": "vmware-vss"
  }
}
```

If strict application consistency was requested but the provider reports success without proving an accepted consistency state, Immutavault fails the backup closed **before advancing the CBT checkpoint**. The incremental adapter then invalidates the uncertain cache, preserving the fail-closed chain policy.

## Production acceptance

Before enabling FLR for important workloads, test a disposable VM containing known files on every filesystem type you need (for example NTFS, ext4, XFS):

1. Create a backup point.
2. Confirm its consistency state matches the intended policy.
3. Verify `immutavault-portal.service` is hardened and `immutavault-flr.service` owns the broker socket.
4. Open an FLR session.
5. Browse to a known nested directory.
6. Download a known file and verify its SHA-256 against the source.
7. Confirm a symlink cannot be followed as a file-download shortcut.
8. Confirm `../` traversal is rejected.
9. Confirm a second portal user, including an admin, cannot read the first user's active session.
10. Close the session and verify the broker's FUSE/libguestfs mounts disappear.
11. Stop the FLR broker and confirm the portal reports FLR unavailable instead of attempting a direct mount.
12. Repeat after reboot and after upgrades to restic/libguestfs/qemu/hypervisor tooling.

FLR reduces recovery time for files; it does not replace full isolated restore/boot testing of complete VM recovery points.
