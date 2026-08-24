#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y frr frr-pythontools iproute2 iputils-arping bridge-utils
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y frr iproute iputils bridge-utils
elif command -v yum >/dev/null 2>&1; then
  yum install -y frr iproute iputils bridge-utils
else
  echo "Install FRR, iproute2, bridge, and arping manually." >&2
  exit 1
fi

if [[ -f /etc/frr/daemons ]]; then
  sed -ri 's/^ospfd=.*/ospfd=yes/' /etc/frr/daemons
fi
cat >/etc/sysctl.d/90-immutavault-dr.conf <<'EOF'
net.ipv4.ip_forward=1
EOF
sysctl --system >/dev/null
systemctl enable --now frr

cat <<'EOF'
DR gateway prerequisites installed.
No VLAN, VXLAN, gateway IP, or OSPF route was created by this installer.

Next:
  1. Configure this gateway under disaster_recovery.sites.
  2. Confirm its underlay/trunk interfaces and VTEP address.
  3. Run: immutavault --config /etc/immutavault/immutavault.yml dr-network plan --site SITE
  4. Review every command.
  5. Apply with: ... dr-network prepare --site SITE --execute
EOF
