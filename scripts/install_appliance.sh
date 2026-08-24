#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/srv/immutavault}"
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "Immutavault appliance requires Linux" >&2; exit 1; }
ARCH=$(uname -m)
case "$ARCH" in x86_64|amd64|aarch64) ;; *) echo "Unsupported CPU architecture: $ARCH" >&2; exit 1;; esac

VENDOR=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)
MODEL=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)
echo "Detected hardware: ${VENDOR:-unknown} ${MODEL:-unknown} ($ARCH)"
echo "Vendor-neutral design: Dell PowerEdge, Cisco UCS, Lenovo ThinkSystem and equivalent Linux servers are supported when OS/storage prerequisites are met."

if command -v apt-get >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip python3-setuptools python3-wheel python3-yaml python3-boto3 \
    bzip2 openssh-client openssl apache2-utils smartmontools curl ca-certificates nfs-common cifs-utils
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-pip python3-setuptools python3-wheel python3-pyyaml python3-boto3 \
    bzip2 openssh-clients openssl httpd-tools smartmontools curl ca-certificates nfs-utils cifs-utils
elif command -v yum >/dev/null; then
  yum install -y python3 python3-pip python3-setuptools python3-wheel python3-pyyaml python3-boto3 \
    bzip2 openssh-clients openssl httpd-tools smartmontools curl ca-certificates nfs-utils cifs-utils
else
  echo "No supported package manager detected. Install Python 3.10+, PyYAML, boto3, bzip2, OpenSSH client, OpenSSL, htpasswd and smartmontools manually. The top-level installer will then install/validate the pinned restic binary." >&2
  exit 1
fi

./scripts/install_controller.sh "$ROOT"
id immutavault-store >/dev/null 2>&1 || useradd --system --home "$ROOT" --shell /usr/sbin/nologin immutavault-store
install -d -o root -g immutavault-store -m 0770 "$ROOT/repository"
install -d -o root -g root -m 0755 /etc/immutavault/tls

cat <<EOF2
Base appliance installed.
IMPORTANT: this script intentionally does NOT partition or format disks.
Repository/staging root: $ROOT
The top-level ./scripts/install.sh installer installs/validates pinned upstream restic and rest-server binaries only after SHA-256 verification.
Next:
  1. Mount your dedicated RAID/ZFS/XFS/ext4/NFS storage at the selected root if applicable.
  2. Prefer: sudo ./scripts/install.sh --role all --repo-root "$ROOT" (downloads pinned/SHA-verified restic and rest-server when needed).
  3. For an offline/manual repository install, provide a compatible rest-server >= 0.14.0 and run ./scripts/check_rest_server.sh before ./scripts/install_repository.sh "$ROOT".
  4. Edit /etc/immutavault/immutavault.yml and platform credentials.
  5. Run immutavault doctor, inventory, backup --dry-run, the live data-plane acceptance test, and an isolated VM restore before enabling schedules.
EOF2
