# Cloud and NAS Storage Fabric

Immutavault v0.5.1 uses a tiered repository model:

1. **Primary vault** — append-only `rest-server` backed by local RAID/ZFS/XFS/ext4 or by a mounted enterprise NFS/SMB filesystem.
2. **Replica targets** — one or more independently encrypted restic repositories on S3-compatible object storage, another Immutavault REST vault, or a mounted filesystem.
3. **Restore source selection** — a recovery request can choose `primary` or any successful replica copy recorded in the recovery catalog.

This model keeps the fast local restore path while allowing an off-site failure domain.

## Supported target types

| Type | Configuration | Typical products | Immutability |
|---|---|---|---|
| REST | `backend: rest` | another Immutavault / rest-server | append-only writer + separate maintenance identity |
| S3 | `backend: s3` | AWS S3, Wasabi, IDrive e2, Backblaze B2, Cloudflare R2, MinIO, Ceph RGW, other compatible endpoints | provider Object Lock when the provider supports it |
| Filesystem | `backend: filesystem` | TrueNAS NFS/SMB, Dell PowerStore/Unity/PowerScale shares, Windows SMB, Linux NFS | depends on NAS snapshots/WORM plus Immutavault credential separation |

## Provider behavior

### Wasabi

Use a bucket created with Object Lock enabled. Set `object_lock_enabled: true`. Immutavault runs an S3 preflight and, after a successful restic copy, applies retention to persistent repository objects.

### IDrive e2

Use the region endpoint supplied by IDrive e2 and a bucket with Object Lock configured. Set provider `idrive_e2`.

### Backblaze B2

Use the B2 S3-compatible endpoint and S3 application keys. Restic itself recommends the S3-compatible API for B2. Object Lock is supported.

### Cloudflare R2

R2 is a valid S3-compatible encrypted replica target. Its S3 compatibility surface does not implement S3 Object Lock, so `object_lock_enabled: true` is rejected. Immutavault instead supports Cloudflare-native **Bucket Locks** with `r2_bucket_lock_enabled: true`. These are provider retention rules, but they are administered through Cloudflare's API rather than the S3 Object Lock API.

### Generic S3 / MinIO / Ceph

Set `provider: custom`, `minio`, or `ceph`, then provide endpoint, region, bucket, and credentials. If `object_lock_enabled: true`, the target must implement the S3 Object Lock calls used by Immutavault.

## Why Immutavault does not use bucket-default Object Lock for restic

Restic creates transient objects under its `locks/` namespace and must delete them normally. Blanket default Object Lock on every object can make those transient locks undeletable and interfere with repository operation.

Immutavault therefore:

1. copies the recovery point,
2. verifies the snapshot exists in the destination,
3. lists the repository objects,
4. excludes the transient `locks/` namespace,
5. applies S3 retention to persistent objects,
6. never shortens an existing longer retention date.

This gives provider-enforced retention without intentionally freezing restic's own transient lock files.

## TrueNAS / Dell / NFS / SMB

The strongest on-prem design is to mount the storage on the vault server and let `rest-server` own the repository. The backup controller still writes only through the append-only REST interface; it does not receive direct filesystem delete access.

Example NFS mount:

```bash
sudo mkdir -p /srv/immutavault
sudo mount -t nfs4 truenas.example.local:/mnt/pool/immutavault /srv/immutavault
findmnt /srv/immutavault
```

Example SMB mount using a protected credentials file:

```bash
sudo install -m 600 -o root -g root /dev/null /root/.immutavault-smb
sudoedit /root/.immutavault-smb
sudo mkdir -p /srv/immutavault
sudo mount -t cifs //nas.example.local/immutavault /srv/immutavault \
  -o credentials=/root/.immutavault-smb,vers=3.1.1,seal
findmnt /srv/immutavault
```

For Linux repositories, NFS is normally preferred over SMB when both are available. If SMB is used, test the exact kernel/NAS combination before production.

## Initialize and test a replica

```bash
set -a
source /etc/immutavault/immutavault.env
set +a

immutavault --config /etc/immutavault/immutavault.yml replica-init --name wasabi-immutable
immutavault --config /etc/immutavault/immutavault.yml storage-targets
immutavault --config /etc/immutavault/immutavault.yml doctor
```

## Restore from cloud/NAS

The portal displays every successful copy for each recovery point. In the CLI, pass the source in restore options:

```bash
immutavault --config /etc/immutavault/immutavault.yml restore-request \
  --snapshot SNAPSHOT_ID \
  --requester customer1 \
  --target-platform pve-cluster-1 \
  --target-name app01-recovered \
  --options-json '{"source_repository":"wasabi-immutable"}'
```

The same manifest validation and safe-new-VM import workflow is used regardless of which repository copy is selected.


## Cloudflare R2 Bucket Locks

Cloudflare R2 is supported through its S3-compatible data path. R2 does **not** expose AWS/S3 Object Lock headers or Object Lock configuration through its S3 compatibility API. It does, however, provide a separate Cloudflare-native **Bucket Locks** API that prevents deletion and overwriting for a prefix for a configured age, date, or indefinitely.

Immutavault keeps those mechanisms separate:

- `object_lock_enabled: true` is for genuine S3 Object Lock providers such as Wasabi, IDrive e2, Backblaze B2, AWS S3, or compatible appliances that implement the required API.
- `r2_bucket_lock_enabled: true` is for Cloudflare R2 provider-native Bucket Locks.

Configure an R2 retention rule with a dedicated administrative token:

```bash
immutavault --config /etc/immutavault/immutavault.yml replica-lock-init --name cloudflare-r2
immutavault --config /etc/immutavault/immutavault.yml replica-lock-status --name cloudflare-r2
```

The lock initializer reads all existing R2 lock rules, preserves unrelated rules, and creates separate rules for restic's persistent `data/`, `index/`, `snapshots/`, `keys/`, and `config` namespaces. It deliberately excludes transient `locks/`, which restic must be able to create and delete. For deduplicated restic repositories, Immutavault uses **Date** rules rather than simple object-age rules. A new snapshot can reference an old data pack; therefore the retention horizon is refreshed after every successful R2 copy so that old shared packs remain protected for the newest recovery point's full immutability window. Indefinite or later Date rules are preserved and never shortened.

Because Cloudflare Bucket Locks are an administratively mutable bucket policy, they are **not equivalent to S3 Compliance Object Lock**. Keep a dedicated, least-privilege Cloudflare bucket-configuration API token in the protected controller environment if automated R2 immutability is enabled; Immutavault fails the replica operation if it cannot refresh the rolling horizon. For strict WORM semantics where even administrators cannot shorten retention, prefer a provider/bucket using S3 Object Lock **COMPLIANCE** mode.


## Large-repository Object Lock scaling note

For genuine S3 Object Lock targets, Immutavault deliberately extends retention on persistent restic objects because deduplicated packs may be shared by newer snapshots. This conservative policy is correctness-first but can require many provider API calls on very large repositories (for example tens of TB with many pack objects). Measure lock-application duration and API cost during the production pilot. A future index-aware delta locker can reduce this to newly referenced objects without weakening the recovery-point retention guarantee.
