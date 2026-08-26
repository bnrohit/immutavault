#!/usr/bin/env bash
set -euo pipefail

ROLE="all"
REPO_ROOT="/srv/immutavault"
ENABLE=0
SKIP_PACKAGES=0
INSTALL_REST_SERVER=1
INSTALL_RESTIC=1

usage() {
  cat <<'USAGE'
Immutavault production installer

Usage:
  sudo ./scripts/install.sh [options]

Options:
  --role all|controller|repository   Install full appliance (default), controller only, or repository only
  --repo-root PATH                   Repository/staging root (default: /srv/immutavault)
  --enable-services                  Install and enable systemd units after validation
  --skip-packages                    Do not install OS packages (for prebuilt/minimal images)
  --no-restic-download               Do not download the pinned/SHA-verified upstream restic binary
  --no-rest-server-download          Do not download the pinned/SHA-verified upstream rest-server binary
  -h, --help                         Show this help

Examples:
  sudo ./scripts/install.sh --role all --enable-services
  sudo ./scripts/install.sh --role controller --skip-packages
  sudo ./scripts/install.sh --role repository --repo-root /backup/immutavault
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="${2:?missing role}"; shift 2 ;;
    --repo-root) REPO_ROOT="${2:?missing path}"; shift 2 ;;
    --enable-services) ENABLE=1; shift ;;
    --skip-packages) SKIP_PACKAGES=1; shift ;;
    --no-restic-download) INSTALL_RESTIC=0; shift ;;
    --no-rest-server-download) INSTALL_REST_SERVER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f pyproject.toml && -d src/immutavault ]] || { echo "Run from the Immutavault repository root" >&2; exit 1; }
case "$ROLE" in all|controller|repository) ;; *) echo "Invalid role: $ROLE" >&2; exit 2;; esac

if [[ $SKIP_PACKAGES -eq 0 ]]; then
  ./scripts/install_appliance.sh "$REPO_ROOT"
else
  ./scripts/install_controller.sh "$REPO_ROOT"
fi

RESTIC_OK=0
if command -v restic >/dev/null 2>&1; then
  if ./scripts/check_restic.sh "$(command -v restic)" >/dev/null 2>&1; then
    RESTIC_OK=1
  else
    echo "Existing restic is older/incompatible with this Immutavault release." >&2
  fi
fi
if [[ $RESTIC_OK -eq 0 && $INSTALL_RESTIC -eq 1 ]]; then
  ./scripts/install_restic.sh
  RESTIC_OK=1
fi
if [[ $RESTIC_OK -eq 0 ]]; then
  cat >&2 <<WARN
A compatible restic >= 0.19.1 is required for role '$ROLE'.
Install the official compatible binary or rerun without --no-restic-download so Immutavault can install the pinned/SHA-verified upstream release.
No services were enabled.
WARN
  exit 3
fi

if [[ "$ROLE" == repository || "$ROLE" == all ]]; then
  REST_SERVER_OK=0
  if command -v rest-server >/dev/null 2>&1; then
    if ./scripts/check_rest_server.sh "$(command -v rest-server)" >/dev/null 2>&1; then
      REST_SERVER_OK=1
    else
      echo "Existing rest-server is incompatible with Immutavault security requirements." >&2
    fi
  fi
  if [[ $REST_SERVER_OK -eq 0 && $INSTALL_REST_SERVER -eq 1 ]]; then
    ./scripts/install_rest_server.sh
    REST_SERVER_OK=1
  fi
  if [[ $REST_SERVER_OK -eq 0 ]]; then
    cat >&2 <<WARN
A compatible rest-server >= 0.14.0 is required for role '$ROLE'.
It must support --append-only, authenticated TLS, --tls-min-ver, and the configured htpasswd file.
Install the official compatible binary or rerun without --no-rest-server-download so Immutavault can install the pinned/SHA-verified upstream release.
No repository services were enabled.
WARN
    exit 3
  fi
  ./scripts/install_repository.sh "$REPO_ROOT"
fi

install -m 0644 systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

# Validate before enabling recurring jobs. Production uses the same controller
# identity/environment as the service and policy workers.
if [[ -x /usr/local/bin/immutavault && -f /etc/immutavault/immutavault.yml && -f /etc/immutavault/immutavault.env ]]; then
  set +e
  runuser -u immutavault -- bash -c 'set -a; source /etc/immutavault/immutavault.env; set +a; /usr/local/bin/immutavault --config /etc/immutavault/immutavault.yml doctor'
  DOCTOR_RC=$?
  set -e
  if [[ $DOCTOR_RC -ne 0 ]]; then
    echo "Preflight found unresolved items. Services were installed but recurring backup jobs will not be enabled automatically." >&2
    ENABLE=0
  fi
fi

if [[ $ENABLE -eq 1 ]]; then
  if command -v rest-server >/dev/null 2>&1 && [[ "$ROLE" != controller ]]; then
    systemctl enable --now immutavault-rest-server.service
  fi
  if [[ "$ROLE" == all || "$ROLE" == controller ]]; then
    # Portal is intentionally the least-privileged process. Mount and config
    # mutations are isolated behind separate local SO_PEERCRED brokers.
    systemctl enable --now immutavault-flr.service
    systemctl enable --now immutavault-management.service
    systemctl enable --now immutavault-portal.service
    systemctl enable --now immutavault-state-backup.timer immutavault-health.timer
    # Existing installations retain the broad timer until the first scheduled
    # named protection policy is saved. The management broker then disables the
    # broad timer to prevent duplicate runs.
    systemctl enable --now immutavault-backup.timer
  fi
  if [[ "$ROLE" == all ]]; then
    systemctl enable --now immutavault-retention.timer immutavault-verify.timer
  fi
  # DR timers are deliberately NOT enabled by the generic installer. Production
  # promotion stays opt-in until isolated restore/failover/failback acceptance.
fi

cat <<EOF2

Immutavault installation completed.
Role:       $ROLE
Repository: $REPO_ROOT

Primary experience:
  Open the configured Immutavault portal and sign in as a global admin.
  Use Setup & Manage -> Add Hypervisor -> Discover VMs -> Storage -> Protection Policy.

CLI validation:
  immutavault hardware
  immutavault --config /etc/immutavault/immutavault.yml management-status
  immutavault --config /etc/immutavault/immutavault.yml doctor
  immutavault --config /etc/immutavault/immutavault.yml policy-list

The standalone root-run setup console remains available for break-glass/local bootstrap:
  sudo ./scripts/launch_setup_console.sh

Do not enable production schedules until doctor + discovery + dry-run + an isolated restore test pass.
DR timers remain disabled until a controlled failover/failback acceptance test succeeds.
EOF2
