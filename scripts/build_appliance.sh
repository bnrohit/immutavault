#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
BASE_IMAGE=""
BASE_SHA256=""
OUT="$ROOT/dist/appliance"
FORMAT="all"
DISK_SIZE="64G"

usage(){
  cat <<'USAGE'
Build a pinned Immutavault appliance image from an Ubuntu 24.04 cloud QCOW2.

Usage:
  sudo ./scripts/build_appliance.sh \
    --base-image /path/to/ubuntu-24.04-server-cloudimg-amd64.img \
    --base-image-sha256 <sha256> [--format all|qcow2|ova|vhd] [--out DIR]

The base-image SHA-256 is mandatory. Generated formats:
  qcow2  Proxmox/KVM image
  ova    VMware OVF appliance with streamOptimized VMDK
  vhd    XCP-ng VHD import image (not mislabeled as XVA)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-image) BASE_IMAGE="${2:?missing base image}"; shift 2 ;;
    --base-image-sha256) BASE_SHA256="${2:?missing SHA-256}"; shift 2 ;;
    --out) OUT="${2:?missing output directory}"; shift 2 ;;
    --format) FORMAT="${2:?missing format}"; shift 2 ;;
    --disk-size) DISK_SIZE="${2:?missing disk size}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root; virt-customize needs libguestfs access." >&2; exit 1; }
[[ -f "$BASE_IMAGE" ]] || { echo "--base-image is required" >&2; exit 2; }
[[ "$BASE_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || { echo "--base-image-sha256 must be a 64-character SHA-256 digest" >&2; exit 2; }
case "$FORMAT" in all|qcow2|ova|vhd) ;; *) echo "Unsupported format: $FORMAT" >&2; exit 2;; esac
for cmd in qemu-img virt-customize tar sha256sum; do command -v "$cmd" >/dev/null || { echo "$cmd is required" >&2; exit 3; }; done

printf '%s  %s\n' "${BASE_SHA256,,}" "$BASE_IMAGE" | sha256sum --check --status || {
  echo "Base image SHA-256 mismatch; refusing appliance build." >&2
  exit 4
}

WORK=$(mktemp -d)
cleanup(){ rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$OUT"
QCOW="$OUT/immutavault-${VERSION}.qcow2"
SOURCE_TAR="$WORK/immutavault-source.tar"

# Package only tracked/release source surfaces; never copy local secrets, build
# output, state databases or staging payloads into the appliance.
tar -C "$ROOT" \
  --exclude=.git --exclude=.pytest_cache --exclude=dist --exclude=build \
  --exclude='*.db' --exclude='*.key' --exclude='.env' \
  -cf "$SOURCE_TAR" .

cp --reflink=auto "$BASE_IMAGE" "$QCOW"
qemu-img resize "$QCOW" "$DISK_SIZE" >/dev/null

# Network is enabled only for the controlled image build so apt and the release's
# pinned/checksummed restic/rest-server installers can run inside the guest.
virt-customize -a "$QCOW" --network \
  --hostname immutavault \
  --install 'python3,python3-venv,python3-pip,python3-setuptools,python3-wheel,python3-yaml,python3-boto3,bzip2,openssh-client,openssl,apache2-utils,smartmontools,curl,ca-certificates,nfs-common,cifs-utils,fuse3,libguestfs-tools,qemu-utils' \
  --upload "$SOURCE_TAR:/root/immutavault-source.tar" \
  --run-command 'rm -rf /opt/immutavault-build && mkdir -p /opt/immutavault-build && tar -xf /root/immutavault-source.tar -C /opt/immutavault-build' \
  --run-command 'cd /opt/immutavault-build && ./scripts/install.sh --role all --skip-packages' \
  --run-command 'rm -f /root/immutavault-source.tar && rm -rf /opt/immutavault-build/.pytest_cache /opt/immutavault-build/dist /opt/immutavault-build/build' \
  --run-command 'systemctl disable immutavault-backup.timer immutavault-dr-watch.timer 2>/dev/null || true' \
  --run-command 'cloud-init clean --logs --seed 2>/dev/null || true' \
  --run-command 'truncate -s 0 /etc/machine-id || true'

qemu-img check "$QCOW" >/dev/null

if [[ "$FORMAT" == ova || "$FORMAT" == all ]]; then
  VMDK="$WORK/immutavault-${VERSION}.vmdk"
  OVF="$WORK/immutavault-${VERSION}.ovf"
  MF="$WORK/immutavault-${VERSION}.mf"
  OVA="$OUT/immutavault-${VERSION}.ova"
  qemu-img convert -p -f qcow2 -O vmdk -o subformat=streamOptimized "$QCOW" "$VMDK"
  CAPACITY=$(qemu-img info --output=json "$QCOW" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["virtual-size"]))')
  cat > "$OVF" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1" xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData" xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData" xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References><File ovf:id="file1" ovf:href="immutavault-${VERSION}.vmdk"/></References>
  <DiskSection><Info>Virtual disk</Info><Disk ovf:diskId="vmdisk1" ovf:fileRef="file1" ovf:capacity="$CAPACITY" ovf:capacityAllocationUnits="byte"/></DiskSection>
  <NetworkSection><Info>Logical networks</Info><Network ovf:name="VM Network"><Description>Management network; remap during deployment as required.</Description></Network></NetworkSection>
  <VirtualSystem ovf:id="immutavault"><Info>Immutavault v${VERSION}</Info><Name>immutavault-${VERSION}</Name>
    <OperatingSystemSection ovf:id="101" vmw:osType="ubuntu64Guest"><Info>Ubuntu 24.04 LTS</Info><Description>Ubuntu Linux (64-bit)</Description></OperatingSystemSection>
    <VirtualHardwareSection><Info>Virtual hardware requirements</Info>
      <System><vssd:ElementName>Virtual Hardware Family</vssd:ElementName><vssd:InstanceID>0</vssd:InstanceID><vssd:VirtualSystemIdentifier>immutavault-${VERSION}</vssd:VirtualSystemIdentifier><vssd:VirtualSystemType>vmx-19</vssd:VirtualSystemType></System>
      <Item><rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits><rasd:Description>4 virtual CPUs</rasd:Description><rasd:ElementName>4 virtual CPU(s)</rasd:ElementName><rasd:InstanceID>1</rasd:InstanceID><rasd:ResourceType>3</rasd:ResourceType><rasd:VirtualQuantity>4</rasd:VirtualQuantity></Item>
      <Item><rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits><rasd:Description>8192 MB memory</rasd:Description><rasd:ElementName>8192 MB of memory</rasd:ElementName><rasd:InstanceID>2</rasd:InstanceID><rasd:ResourceType>4</rasd:ResourceType><rasd:VirtualQuantity>8192</rasd:VirtualQuantity></Item>
      <Item><rasd:Address>0</rasd:Address><rasd:Description>SCSI controller</rasd:Description><rasd:ElementName>SCSI controller 0</rasd:ElementName><rasd:InstanceID>3</rasd:InstanceID><rasd:ResourceSubType>VirtualSCSI</rasd:ResourceSubType><rasd:ResourceType>6</rasd:ResourceType></Item>
      <Item><rasd:AddressOnParent>0</rasd:AddressOnParent><rasd:ElementName>Hard disk 1</rasd:ElementName><rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource><rasd:InstanceID>4</rasd:InstanceID><rasd:Parent>3</rasd:Parent><rasd:ResourceType>17</rasd:ResourceType></Item>
      <Item><rasd:AddressOnParent>7</rasd:AddressOnParent><rasd:AutomaticAllocation>true</rasd:AutomaticAllocation><rasd:Connection>VM Network</rasd:Connection><rasd:ElementName>Network adapter 1</rasd:ElementName><rasd:InstanceID>5</rasd:InstanceID><rasd:ResourceSubType>VmxNet3</rasd:ResourceSubType><rasd:ResourceType>10</rasd:ResourceType></Item>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
EOF
  (cd "$WORK" && sha256sum "immutavault-${VERSION}.ovf" "immutavault-${VERSION}.vmdk" > "immutavault-${VERSION}.mf")
  tar -C "$WORK" -cf "$OVA" "immutavault-${VERSION}.ovf" "immutavault-${VERSION}.mf" "immutavault-${VERSION}.vmdk"
fi

if [[ "$FORMAT" == vhd || "$FORMAT" == all ]]; then
  # XCP-ng's documented raw/VHD import path is emitted explicitly. This is not
  # an XVA package and is intentionally not named .xva.
  qemu-img convert -p -f qcow2 -O vpc -o subformat=fixed "$QCOW" "$OUT/immutavault-${VERSION}.vhd"
fi

if [[ "$FORMAT" != qcow2 && "$FORMAT" != all ]]; then
  rm -f "$QCOW"
fi

(
  cd "$OUT"
  rm -f SHA256SUMS
  shopt -s nullglob
  files=(immutavault-${VERSION}.qcow2 immutavault-${VERSION}.ova immutavault-${VERSION}.vhd)
  ((${#files[@]})) || { echo "No appliance artifacts were produced" >&2; exit 6; }
  sha256sum "${files[@]}" > SHA256SUMS
)

echo "Appliance build complete: $OUT"
cat "$OUT/SHA256SUMS"
