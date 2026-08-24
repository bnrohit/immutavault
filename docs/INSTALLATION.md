# Installation and first production run

Immutavault can run either on a dedicated physical Linux server or inside a Linux VM. The same software is used in both cases.

## Recommended choices

**Dedicated host / appliance:** best when the server owns local RAID/HBA storage and has 10/25 GbE. Keep the OS on separate disks from the backup repository.

**Virtual machine:** good for the controller/portal when backup data lives on external NFS/S3/another vault. Do not keep the only backup copy on the same virtualization cluster being protected.

## Supported base

- 64-bit Linux on x86_64/amd64 or aarch64.
- Python 3.10+.
- restic.
- rest-server for the primary append-only REST vault role.
- SSH for Proxmox/XCP-ng adapters; `govc` for the VMware fallback adapter.
- Reliable storage mounted before Immutavault services start.

## 1. Prepare the host

Set a stable DNS name, NTP/time synchronization, a static/reserved management address, and redundant network uplinks where available. Patch the OS first.

Run the non-destructive preflight:

```bash
sudo ./scripts/preflight.sh
```

The installer never partitions or formats a disk.

## 2. Choose the role

Full appliance (controller + repository on one Linux server):

```bash
sudo ./scripts/install.sh --role all
```

Controller-only VM (repository lives elsewhere):

```bash
sudo ./scripts/install.sh --role controller
```

Repository-only vault:

```bash
sudo ./scripts/install.sh --role repository --repo-root /srv/immutavault
```

For `all` or `repository` roles, the top-level installer installs pinned `rest-server` v0.14.0 from the official upstream release **only after SHA-256 verification** when no compatible daemon is present. Existing binaries are capability-gated and must be v0.14.0+ with `--append-only`, authenticated TLS, `--tls-min-ver`, and htpasswd support. Use `--no-rest-server-download` for offline/change-controlled environments and provide a compatible binary yourself.

## 3. Mount storage

Examples:

- local XFS/ext4/ZFS: mount at `/srv/immutavault`;
- TrueNAS/Dell NFS: mount the export at `/srv/immutavault` or configure it as a replica;
- SMB/CIFS: mount with a root-owned credential file and restrictive permissions;
- S3: no filesystem mount is required; configure it under `replicas:`.

Verify the mount before continuing:

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

## 5. Validate before scheduling

All four commands should succeed:

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

## 6. Perform the first real backup

Start with one non-critical VM using an include filter, then:

```bash
sudo -u immutavault bash -c \
  'set -a; source /etc/immutavault/immutavault.env; set +a; \
   immutavault --config /etc/immutavault/immutavault.yml backup --all'
```

Confirm:

```bash
immutavault --config /etc/immutavault/immutavault.yml recovery-points
immutavault --config /etc/immutavault/immutavault.yml status
```

Perform a full staging verification of that point before adding production workloads:

```bash
immutavault --config /etc/immutavault/immutavault.yml verify-point --snapshot SNAPSHOT_ID
```

## 7. Enable recurring services

Only after the validation and a test restore pass:

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

## 8. Configure off-site immutable copy

Enable one S3/NAS target in `replicas:` and initialize it:

```bash
immutavault --config /etc/immutavault/immutavault.yml replica-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml replica-lock-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml storage-targets
```

Use a separate cloud account/credential boundary from the hypervisor admins whenever possible.

## 9. Prove restore

A backup is not accepted as production-ready until a VM has been restored into an isolated network and booted successfully. See `docs/RESTORE.md`.
