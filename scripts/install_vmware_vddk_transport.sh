#!/usr/bin/env bash
set -euo pipefail

VDDK_DIR="/opt/vmware-vix-disklib-distrib"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vddk-dir) VDDK_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: sudo $0 [--vddk-dir /opt/vmware-vix-disklib-distrib]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -m)" != "x86_64" && "$(uname -m)" != "amd64" ]]; then
  echo "ERROR: VMware VDDK transport requires an x86-64 backup proxy." >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y nbdkit libnbd-bin python3-libnbd
  # Some Ubuntu repositories package the VDDK plugin separately; tolerate the
  # package being absent because nbdkit builds can also include it directly.
  DEBIAN_FRONTEND=noninteractive apt-get install -y nbdkit-plugin-vddk 2>/dev/null || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y nbdkit libnbd || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y nbdkit libnbd || true
else
  echo "ERROR: unsupported package manager; install nbdkit + VDDK plugin + libnbd/nbdsh manually." >&2
  exit 1
fi

if [[ ! -d "$VDDK_DIR" ]]; then
  cat >&2 <<EOF
VMware VDDK is proprietary and is NOT downloaded or redistributed by Immutavault.
Download the licensed VDDK package from Broadcom/VMware, unpack it to:
  $VDDK_DIR
Then rerun this command.
EOF
  exit 2
fi

command -v nbdkit >/dev/null || { echo "ERROR: nbdkit not installed" >&2; exit 1; }
command -v nbdsh >/dev/null || { echo "ERROR: nbdsh not installed" >&2; exit 1; }

probe="$(nbdkit vddk --dump-plugin "libdir=$VDDK_DIR" 2>&1)" || {
  echo "$probe" >&2
  echo "ERROR: nbdkit VDDK plugin cannot load VDDK from $VDDK_DIR" >&2
  exit 1
}
grep -q 'VixDiskLib_Open=1' <<<"$probe" || {
  echo "ERROR: configured VDDK does not expose VixDiskLib_Open" >&2
  exit 1
}

echo "VDDK transport prerequisites: OK"
echo "VDDK directory: $VDDK_DIR"
echo "Next: open Immutavault Guided Setup, choose VMware 'Automatic incremental + safe full fallback', and enter the vCenter TLS thumbprint."
