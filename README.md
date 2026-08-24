# Immutavault v0.7.0

Immutavault is an open, vendor-neutral **immutable VM backup, recovery, replication and disaster-recovery orchestrator** for VMware/vCenter, Proxmox VE and XCP-ng.

Its core security rule is simple: **the identity that creates backups does not receive prune/delete authority.** Normal backups write through authenticated TLS to append-only `rest-server`; expiration/prune is a separate root-only operation. Restores are equally conservative: Immutavault restores as a **new VM** and refuses implicit production overwrite.

> **Readiness:** v0.7.0 adds a software-tested native VMware CBT/VDDK incremental transport with automatic full-backup fallback. The normal CI suite validates Python 3.10–3.14, packaging, configuration, systemd assets, backup/restore controls, DR safety, transport-state/fallback logic, and a real TLS append-only restic/rest-server data-plane exercise. VMware VDDK is proprietary and is not bundled in CI, so CBT/VDDK must still be acceptance-tested against the actual licensed VDDK and vCenter estate before production enablement.

## v0.7 headline: VMware incremental-forever transport

`mode: auto` now prefers VMware **Changed Block Tracking (CBT) + VDDK** after a safe full baseline:

```text
First point
running VM
  ↓
quiesced snapshot
  ↓
same-snapshot temporary clone
  ↓
full OVF/VMDK baseline
  ↓
commit CBT change IDs only after restic succeeds

Next points
running VM
  ↓
quiesced snapshot
  ↓
QueryChangedDiskAreas(previous changeId)
  ↓
VDDK via nbdkit/libnbd
  ↓
read changed extents only
  ↓
immutable delta recovery point
```

The existing `hot-clone-export` transport remains supported and is the automatic fallback when incremental safety cannot be proven.

A fresh full baseline is taken automatically when CBT/VDDK is unavailable, CBT is disabled/reset, a change-ID epoch changes, VM disk/hardware/network layout changes, `QueryChangedDiskAreas` fails, a VDDK read fails, or the configured maximum chain length is reached. `vddk-cbt-strict` is available for environments that prefer a failed backup over fallback.

CBT state advances **only after the immutable restic snapshot succeeds**. Retention and replication are chain-aware, so a retained/off-site delta keeps and copies every baseline/parent required to reconstruct it. Parent IDs are stored both in the transport marker and in the immutable restic snapshot tags.

See [`docs/NATIVE_INCREMENTAL.md`](docs/NATIVE_INCREMENTAL.md).

## Guided management appliance

The browser setup console is designed so an operator does not need to write YAML for normal onboarding:

```text
1. Add Hypervisor
   VMware / Proxmox / XCP-ng
       ↓
   Test connection + discover VMs
       ↓
2. Select VMs
   checkbox exact workloads
       ↓
3. Storage / Cloud
   Wasabi / B2 / e2 / R2 / AWS / S3 / NFS / SMB / NAS / second vault
       ↓
4. DR Site
   map source + same-family DR hypervisor
   VLAN / VNI / subnet / gateway / VTEP
   preview VXLAN + FRR/OSPF plan
       ↓
5. Test and Start
   doctor → dry run → first backup → schedules
```

For VMware v0.7 the wizard also exposes:

- Automatic incremental + safe full fallback;
- full hot-clone only;
- strict CBT/VDDK;
- VDDK directory;
- vCenter VDDK TLS thumbprint;
- VDDK transport order (`san:hotadd:nbdssl:nbd` by default);
- optional automatic CBT enablement.

The console writes back through the same validated configuration schema used by the CLI. Secrets remain in the protected environment file rather than YAML.

The first dashboard cards are **RPO Status** and **Immutable-Copy Verification**.

## Supported protection paths

### VMware/vCenter

- v0.7 CBT/VDDK changed-block transport;
- safe full hot-clone/OVF fallback;
- quiesced VMware snapshots when VMware Tools supports them;
- explicit crash-consistent fallback policy;
- per-vCenter credentials/CA/datacenter/datastore/network/resource-pool mapping;
- restore as a new VM;
- VDDK delta application only while the newly restored target is powered off.

### Proxmox VE

- inventory through the Proxmox CLI over SSH;
- online `vzdump --mode snapshot` protection;
- safe `qmrestore` / `pct restore` into a new target;
- per-platform SSH keys.

### XCP-ng

- snapshot-to-template XVA export;
- template-aware import and `vm-install` recovery;
- per-platform SSH keys.

Native PBS/Xen Orchestra incremental transports remain future work; v0.7 does not pretend full archive exports are equivalent to those native data movers.

## Immutable storage

Primary vault:

- encrypted/deduplicated restic;
- authenticated TLS append-only `rest-server` writer;
- separate `immutavault` controller and `immutavault-store` OS identities;
- root-only retention/prune;
- SHA-256 manifests and staged verification;
- tamper-evident audit chain;
- suspicious/ransomware-churn preservation policy.

Replica targets:

- Wasabi;
- IDrive e2;
- Backblaze B2;
- AWS S3;
- Cloudflare R2;
- MinIO / Ceph / custom S3;
- NFS / SMB / TrueNAS / Dell or other mounted filesystems;
- another Immutavault REST vault.

S3 Object Lock is supported where the provider implements it. Cloudflare R2 uses its native Bucket Lock model and is kept distinct from S3 Compliance Object Lock.

## Disaster recovery

Immutavault provides two-site DR orchestration with:

- off-site recovery-point replication;
- RPO checks;
- target hypervisor/gateway preflight;
- explicit fencing plus independent fencing verification;
- maintenance suppression and probe quorum;
- recovery boot order and health checks;
- VXLAN recovery VLANs;
- Linux FRR/OSPF route ownership;
- same-IP recovery for explicitly configured stretched recovery networks;
- planned failback.

Automatic cross-hypervisor conversion remains blocked until separately certified. Automatic site failover stays off until a real controlled failover/failback drill has passed.

## Fast install — Ubuntu 24.04 LTS

```bash
git clone https://github.com/bnrohit/immutavault.git
cd immutavault
git checkout v0.7.0

sudo ./scripts/preflight.sh
sudo ./scripts/install.sh --role all --repo-root /srv/immutavault
sudo ./scripts/data_plane_acceptance.sh
sudo ./scripts/launch_setup_console.sh
```

The setup launcher prints the browser URL and one-time setup token.

Main configuration:

```text
/etc/immutavault/immutavault.yml
```

Secrets:

```text
/etc/immutavault/immutavault.env
```

## VMware VDDK setup

Immutavault does **not** download or redistribute VMware VDDK. Obtain the licensed VDDK package through Broadcom/VMware and install/unpack it on an x86-64 backup proxy, then validate the open-source integration prerequisites:

```bash
sudo ./scripts/install_vmware_vddk_transport.sh \
  --vddk-dir /opt/vmware-vix-disklib-distrib
```

Recommended VMware configuration:

```yaml
platforms:
  - name: vc-primary
    type: vmware
    enabled: true
    endpoint: https://vcenter.example.local/sdk
    mode: auto
    include: ["DC01", "SQL01", "FILE01"]
    options:
      username_env: VC_PRIMARY_USERNAME
      password_env: VC_PRIMARY_PASSWORD
      quiesce: true
      quiesce_fallback_crash_consistent: false
      cbt_auto_enable: true
      incremental_fallback: true
      cbt_max_chain_length: 32
      vddk_libdir: /opt/vmware-vix-disklib-distrib
      vddk_thumbprint: "AA:BB:CC:DD:..."
      vddk_transports: "san:hotadd:nbdssl:nbd"
```

## First production test

Start with one disposable/non-critical VM:

```text
running VM
  ↓
full baseline
  ↓
change known guest data
  ↓
CBT incremental #1
  ↓
change different guest data
  ↓
CBT incremental #2
  ↓
copy complete chain to immutable DR storage
  ↓
restore incremental #2 as NEW VM
  ↓
boot + application validation
  ↓
reset/disable CBT
  ↓
verify next backup becomes fresh full baseline
```

Only after that passes should CBT/VDDK be enabled broadly.

## Validation

Run the complete source-tree release suite:

```bash
./scripts/release_check.sh
```

Run the real append-only repository acceptance test on the appliance:

```bash
sudo ./scripts/data_plane_acceptance.sh
```

For the full production acceptance and DR drill, see [`docs/PRODUCTION_ACCEPTANCE.md`](docs/PRODUCTION_ACCEPTANCE.md).

## Security boundaries

- backup writer cannot prune/delete the primary repository;
- retention is a separate privileged path;
- restore never silently overwrites production;
- setup console requires TLS when exposed beyond loopback;
- secrets are separated from YAML;
- VDDK password is passed to nbdkit through a mode-0600 temporary password file, not plaintext command arguments;
- DR promotion requires fencing safeguards;
- cross-hypervisor automatic restore remains blocked unless separately certified.

## License

Apache-2.0 for Immutavault source. Third-party products and libraries, including VMware VDDK, remain subject to their own licenses and distribution terms.
