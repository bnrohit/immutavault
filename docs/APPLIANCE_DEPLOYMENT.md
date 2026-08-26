# Appliance Deployment — v1.1

This document covers the release bootstrap and reproducible appliance-image builder added in Immutavault v1.1.0.

## Security model

A prebuilt image should not become a new unverified supply-chain path. The builder therefore requires an operator-supplied Ubuntu 24.04 cloud image and its exact SHA-256. The build stops before customization if the digest does not match.

Generated artifacts receive a `SHA256SUMS` file. Preserve that file with the image and verify the image again before importing it into a hypervisor.

## One-line bootstrap

After the immutable `v1.1.0` tag exists:

```bash
curl -fsSL https://raw.githubusercontent.com/bnrohit/immutavault/v1.1.0/scripts/bootstrap.sh \
  | sudo bash -s -- --enable-services
```

The bootstrap downloads only the requested release tag. It does not follow `main` or a moving `latest` branch.

For an independently approved source-archive digest:

```bash
export IMMUTAVAULT_ARCHIVE_SHA256='<approved sha256>'
curl -fsSL https://raw.githubusercontent.com/bnrohit/immutavault/v1.1.0/scripts/bootstrap.sh \
  | sudo -E bash -s -- --enable-services
```

You may also specify:

```text
--version X.Y.Z
--role all|controller|repository
--repo-root /path
--enable-services
```

The release does not claim that `get.immutavault.io` is live. Do not put that vanity URL into operational documentation until DNS/TLS/hosting are under project control and the content is pinned to the audited bootstrap.

## Build prerequisites

The build host needs:

- `qemu-img` / qemu-utils;
- `virt-customize` / libguestfs-tools;
- tar;
- sha256sum;
- outbound package access while customizing the guest.

Use a specific Ubuntu 24.04 server cloud QCOW2 image and obtain its SHA-256 from the same trusted distribution channel.

## Build all appliance formats

```bash
sudo ./scripts/build_appliance.sh \
  --base-image /srv/images/ubuntu-24.04-server-cloudimg-amd64.img \
  --base-image-sha256 '<exact sha256>' \
  --format all \
  --out dist/appliance
```

The builder:

1. verifies the base image SHA-256;
2. copies/resizes the QCOW2;
3. packages release source without `.git`, local secrets, databases or build output;
4. customizes the guest with required OS packages;
5. runs the normal Immutavault installer inside the image;
6. leaves broad backup/automatic DR schedules disabled;
7. cleans cloud-init identity/log state and machine-id;
8. checks the resulting QCOW2;
9. converts the QCOW2 to requested hypervisor formats;
10. emits `SHA256SUMS`.

## Proxmox / KVM

Artifact:

```text
immutavault-1.1.0.qcow2
```

Import the disk using your normal Proxmox/KVM image workflow, create a VM with at least the planned controller resources, attach the disk, boot it, then run the normal first-boot validation and management setup.

The generated image's sample virtual hardware target is 4 vCPU / 8 GiB RAM. Production sizing depends on concurrent exports, verification, FLR/V2V staging and repository throughput.

## VMware

Artifact:

```text
immutavault-1.1.0.ova
```

The OVA contains:

- an OVF descriptor;
- a stream-optimized VMDK;
- an internal SHA-256 manifest for the OVF/VMDK pair.

Import the OVA, map its management NIC to the intended management network, review CPU/RAM/storage placement, and boot it. Do not place the management interface directly on an untrusted network.

A successful OVA import is not a substitute for the production acceptance checklist. Run `doctor`, management-broker status, repository checks and an isolated restore before scheduling production backups.

## XCP-ng

Artifact:

```text
immutavault-1.1.0.vhd
```

v1.1 intentionally emits an explicit VHD import artifact rather than fabricating an `.xva`. XVA is a separate appliance format containing XenServer/XCP-ng metadata and specially packaged disk content; renaming a QCOW2/VHD to `.xva` would be incorrect.

Use a supported XCP-ng/Xen Orchestra VHD/disk import workflow, then create/attach the VM metadata as appropriate for the site. A native `.xva` appliance should only be published after a dedicated XVA builder and import/boot CI acceptance exist.

## Verify artifacts

On the build host:

```bash
cd dist/appliance
sha256sum -c SHA256SUMS
```

Repeat that verification after copying the artifact to the hypervisor/import workstation.

## First boot

The appliance is deliberately not shipped with customer credentials, TLS private keys, OIDC client secrets, vCenter credentials, storage secrets or a pre-populated state database.

After first boot:

```bash
sudo systemctl status immutavault-management.service immutavault-flr.service
sudo -u immutavault bash -c \
  'set -a; source /etc/immutavault/immutavault.env; set +a; immutavault --config /etc/immutavault/immutavault.yml management-status'
```

Configure valid TLS/OIDC or a temporary local break-glass workflow before exposing the portal beyond a management network.

Then use the unified Setup & Manage workflow:

```text
Hypervisor -> Test/Discover -> exact VMs -> Storage -> Protection Policy -> dry-run -> real backup -> verify -> isolated restore
```

## Image publication rule

Do not call an image “official” unless all of the following are recorded:

- Immutavault release tag and commit SHA;
- base Ubuntu image filename and SHA-256;
- builder version/commit;
- generated artifact SHA-256;
- successful import/boot test on the named target hypervisor family;
- release CI for the source tree is green.

This keeps appliance convenience from bypassing the same evidence expected of the source release.
