# Installation and first production run

Immutavault can run on a dedicated physical Linux server or inside a Linux VM. The same software is used in both cases.

## Recommended choices

**Dedicated host / appliance:** best when the server owns local RAID/HBA storage and has 10/25 GbE. Keep the OS on separate disks from the backup repository.

**Virtual machine:** good for the controller/portal when backup data lives on external NFS/S3/another vault. Do not keep the only backup copy on the same virtualization cluster being protected.

## Supported base

- 64-bit Linux on x86_64/amd64 or aarch64.
- Python 3.10+.
- restic.
- rest-server for the primary append-only REST vault role.
- SSH for Proxmox/XCP-ng adapters.
- `govc` for VMware inventory/full hot-clone workflows and recovery checks.
- For native VMware VDDK/CBT: an externally installed, authorized `immutavault-vddk` helper/provider implementing protocol version 1. Broadcom VDDK is not bundled by Immutavault.
- Reliable storage mounted before Immutavault services start.

## 1. Prepare the host

Set a stable DNS name, NTP/time synchronization, a static/reserved management address, and redundant network uplinks where available. Patch the OS first.

Run the non-destructive preflight:

```bash
sudo ./scripts/preflight.sh
```

The installer never partitions or formats a disk.

## 2. Choose the role

Full appliance:

```bash
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
```

Controller-only VM:

```bash
sudo ./scripts/install.sh --role controller
```

Repository-only vault:

```bash
sudo ./scripts/install.sh --role repository --repo-root /srv/immutavault
```

For `all` or `repository` roles, the installer installs pinned rest-server v0.14.0 from the official upstream release only after SHA-256 verification when no compatible daemon is present. Existing binaries are capability-gated and must support append-only mode, authenticated TLS, TLS minimum-version control and htpasswd authentication. Use `--no-rest-server-download` for offline/change-controlled environments.

## 3. Mount storage

Examples:

- local XFS/ext4/ZFS at `/srv/immutavault`;
- TrueNAS/Dell NFS mounted at `/srv/immutavault` or configured as a replica;
- SMB/CIFS mounted with a root-owned credential file and restrictive permissions;
- S3 configured directly under `replicas:`.

Verify mounts before continuing:

```bash
findmnt /srv/immutavault
lsblk -f
```

## 4. Configure

```bash
sudo cp config/immutavault.example.yml /etc/immutavault/immutavault.yml
sudo editor /etc/immutavault/immutavault.yml
sudo editor /etc/immutavault/immutavault.env
```

Secrets stay in `/etc/immutavault/immutavault.env` with mode 0640 and are not committed to Git.

Enable one hypervisor at a time. Use include/exclude patterns so the first backup scope is explicit.

### VMware strict native incremental setup

For enterprise VMware deployments requiring genuine native incrementals, install the authorized helper separately, then start from `config/vmware-incremental.example.yml`.

Recommended policy:

```yaml
mode: vddk
options:
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

Ensure the helper is executable by the Immutavault service identity and that the cache parent is on storage with adequate capacity and permissions. The cache contains recoverable guest data and must remain protected.

Strict mode intentionally fails backup when VDDK/CBT is unavailable, unsafe or ambiguous. It never silently converts the job into a hot-clone/OVF recovery point.

## 5. Validate before scheduling

All commands should succeed for the intended production policy:

```bash
immutavault hardware

sudo -u immutavault bash -c \
  'set -a; source /etc/immutavault/immutavault.env; set +a; \
   immutavault --config /etc/immutavault/immutavault.yml doctor'

sudo -u immutavault bash -c \
  'set -a; source /etc/immutavault/immutavault.env; set +a; \
   immutavault --config /etc/immutavault/immutavault.yml inventory'

sudo -u immutavault bash -c \
  'set -a; source /etc/immutavault/immutavault.env; set +a; \
   immutavault --config /etc/immutavault/immutavault.yml backup --all --dry-run'
```

For strict VMware incremental protection, deliberately test at least one failure condition on a disposable VM/environment—such as temporarily removing helper availability—and confirm dry-run/backup fails rather than reporting hot-clone fallback.

## 6. Perform the first real backup

Start with one non-critical VM using an include filter:

```bash
sudo -u immutavault bash -c \
  'set -a; source /etc/immutavault/immutavault.env; set +a; \
   immutavault --config /etc/immutavault/immutavault.yml backup --all'
```

Confirm:

```bash
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml status
immutavault --config /etc/immutavault/immutavault.yml verify-point --snapshot SNAPSHOT_ID
```

For VMware VDDK/CBT, perform a second backup after changing test data and retain provider telemetry showing changed bytes/source bytes read. This proves the path is operating incrementally rather than merely completing a baseline.

## 7. Prove restore

A backup is not accepted as production-ready until a VM has been restored into an isolated network and booted successfully.

Native VMware VDDK/CBT recovery points require the authorized provider/helper to be available for restore. Immutavault refuses implicit overwrite of an existing target VM.

See `docs/RESTORE.md` and `docs/PRODUCTION_ACCEPTANCE.md`.

## 8. Enable recurring services

Only after validation and a test restore pass:

```bash
sudo systemctl enable --now immutavault-rest-server.service
sudo systemctl enable --now immutavault-portal.service
sudo systemctl enable --now immutavault-backup.timer
sudo systemctl enable --now immutavault-retention.timer
sudo systemctl enable --now immutavault-verify.timer
sudo systemctl enable --now immutavault-state-backup.timer
sudo systemctl enable --now immutavault-health.timer
```

Check:

```bash
systemctl --failed
systemctl list-timers 'immutavault-*'
journalctl -u immutavault-backup.service -n 100 --no-pager
```

## 9. Configure off-site immutable copy

Enable at least one S3/NAS target in `replicas:` and initialize it:

```bash
immutavault --config /etc/immutavault/immutavault.yml replica-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml replica-lock-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml storage-targets
```

Use a separate cloud account/credential boundary from hypervisor administrators whenever possible.

## Upgrade an existing server to v0.7.1

```bash
cd /opt/immutavault
git fetch --all --tags
git checkout v0.7.1
cat VERSION
sudo ./scripts/preflight.sh
./scripts/release_check.sh
```

Expected version output:

```text
0.7.1
```

Do not switch an existing VMware job from a full transport to strict VDDK/CBT until the helper/provider, initial baseline, second incremental run and isolated restore have all passed acceptance testing.
