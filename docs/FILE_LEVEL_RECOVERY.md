# File-Level Recovery (FLR) — v0.8

Immutavault v0.8 can recover an individual file without restoring and importing the complete VM first.

The FLR data path is deliberately read-only:

```text
immutable restic recovery point
        |
        | restic mount --no-lock
        v
short-lived FUSE view on the vault
        |
        | guestmount --ro / libguestfs
        v
read-only guest filesystem tree
        |
        +--> portal browse
        +--> streamed single-file download
```

The portal does **not** receive prune, forget, repository-maintenance, or snapshot-delete authority. FLR reads the same immutable recovery point used for a full restore.

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

The appliance installer installs these dependencies on supported package-manager paths. `/dev/fuse` must be available to the `immutavault` service identity.

Check manually:

```bash
command -v restic
command -v guestmount
command -v guestunmount
command -v fusermount3
ls -l /dev/fuse
```

## Configuration

FLR settings are part of the validated YAML schema:

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

Limits are intentionally bounded by schema validation. The mount root must be absolute, session TTL is capped, concurrent sessions are limited per user, and individual portal downloads are size-limited.

## Portal workflow

1. Open **Recovery points** for the protected VM.
2. Choose **Files** on the desired point.
3. Immutavault opens a short-lived FLR session owned by the authenticated portal user.
4. Browse directories in the **File-level recovery** panel.
5. Choose **Download** for the required regular file.
6. Close the FLR session. Sessions also expire automatically.

Only `restore_operator` and `admin` roles can open FLR sessions or download files. Admins can clean up another user's session; normal operators cannot access another operator's session.

## Security controls

v0.8 applies these controls before exposing guest data:

- recovery repository mounted read-only with `restic mount --no-lock`;
- guest disk inspection mounted with `guestmount --ro`;
- per-user session ownership;
- random session IDs;
- mode-0700 temporary session directories;
- bounded session lifetime and concurrent-session limit;
- `..` path traversal rejected;
- guest symlinks are never followed for browse/download;
- device nodes/sockets/FIFOs/special files cannot be downloaded;
- only regular files can be streamed;
- maximum single-file download size enforced server-side;
- FLR open/close/download actions recorded in the tamper-evident audit log;
- no repository delete/prune authority is added to the portal.

## Application consistency

v0.8 records an application-consistency attestation inside the protected recovery payload and in the recovery-point catalog.

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

If strict application consistency was requested but the provider reports success without proving an accepted consistency state, Immutavault fails the backup closed **before advancing the CBT checkpoint**. The incremental adapter then invalidates the uncertain cache, preserving the v0.7.1 fail-closed chain policy.

## Production acceptance

Before enabling FLR for important workloads, test a disposable VM containing known files on every filesystem type you need (for example NTFS, ext4, XFS):

1. Create a backup point.
2. Confirm its consistency state matches the intended policy.
3. Open an FLR session.
4. Browse to a known nested directory.
5. Download a known file and verify its SHA-256 against the source.
6. Confirm a symlink cannot be followed as a file-download shortcut.
7. Confirm `../` traversal is rejected.
8. Confirm a second portal user cannot read the first user's session.
9. Close the session and verify the FUSE/libguestfs mounts disappear.
10. Repeat after reboot and after upgrades to restic/libguestfs/qemu/hypervisor tooling.

FLR reduces recovery time for files; it does not replace full isolated restore/boot testing of complete VM recovery points.
