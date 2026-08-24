#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/srv/immutavault}"
REPO="$ROOT/repository"
STAGING="$ROOT/staging"
CONFIG_DIR=/etc/immutavault
STATE_DIR=/var/lib/immutavault

if [[ $EUID -ne 0 ]]; then
  echo "Run as root" >&2
  exit 1
fi

command -v restic >/dev/null || { echo "Install restic first" >&2; exit 1; }
"$(dirname "$0")/check_restic.sh" "$(command -v restic)"
command -v rest-server >/dev/null || { echo "Install rest-server first and place it in PATH" >&2; exit 1; }
"$(dirname "$0")/check_rest_server.sh" "$(command -v rest-server)"

id immutavault >/dev/null 2>&1 || useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin immutavault
id immutavault-store >/dev/null 2>&1 || useradd --system --home "$ROOT" --shell /usr/sbin/nologin immutavault-store
install -d -o root -g root -m 0755 "$ROOT"
install -d -o root -g immutavault-store -m 0770 "$REPO"
install -d -o immutavault -g immutavault -m 0750 "$STAGING" "$ROOT/restore-staging" "$ROOT/verify-staging" "$STATE_DIR"
install -d -o root -g root -m 0755 "$CONFIG_DIR" "$CONFIG_DIR/tls"
# Storage daemon gets only its non-secret repository root. Never pass the controller's
# RESTIC_PASSWORD, portal tokens, hypervisor credentials, or cloud credentials to rest-server.
printf 'IMMUTAVAULT_REPO_ROOT=%s\n' "$ROOT" > "$CONFIG_DIR/repository.env"
chown root:root "$CONFIG_DIR/repository.env"
chmod 600 "$CONFIG_DIR/repository.env"

if [[ ! -f "$CONFIG_DIR/immutavault.env" ]]; then
  umask 077
  : > "$CONFIG_DIR/immutavault.env"
fi
# Idempotently create every secret this role needs. This works whether the
# controller or repository role was installed first.
if ! grep -q '^RESTIC_PASSWORD=' "$CONFIG_DIR/immutavault.env"; then
  echo "RESTIC_PASSWORD=$(openssl rand -base64 48 | tr -d '\n')" >> "$CONFIG_DIR/immutavault.env"
fi
if ! grep -q '^REST_SERVER_USER=' "$CONFIG_DIR/immutavault.env"; then
  echo 'REST_SERVER_USER=backupwriter' >> "$CONFIG_DIR/immutavault.env"
fi
if ! grep -q '^REST_SERVER_PASSWORD=' "$CONFIG_DIR/immutavault.env"; then
  echo "REST_SERVER_PASSWORD=$(openssl rand -base64 36 | tr -d '\n')" >> "$CONFIG_DIR/immutavault.env"
fi
for var in IMMUTAVAULT_CUSTOMER_TOKEN IMMUTAVAULT_APPROVER_TOKEN IMMUTAVAULT_ADMIN_TOKEN; do
  if ! grep -q "^${var}=" "$CONFIG_DIR/immutavault.env"; then
    echo "${var}=$(openssl rand -hex 32)" >> "$CONFIG_DIR/immutavault.env"
  fi
done
if grep -q '^IMMUTAVAULT_REPO_ROOT=' "$CONFIG_DIR/immutavault.env"; then
  sed -i "s|^IMMUTAVAULT_REPO_ROOT=.*|IMMUTAVAULT_REPO_ROOT=$ROOT|" "$CONFIG_DIR/immutavault.env"
else
  echo "IMMUTAVAULT_REPO_ROOT=$ROOT" >> "$CONFIG_DIR/immutavault.env"
fi
chown root:immutavault "$CONFIG_DIR/immutavault.env"
chmod 640 "$CONFIG_DIR/immutavault.env"

set -a
# shellcheck disable=SC1091
source "$CONFIG_DIR/immutavault.env"
set +a

if [[ ! -f "$REPO/config" ]]; then
  RESTIC_REPOSITORY="$REPO" restic init
fi
chgrp -R immutavault-store "$REPO"
chmod -R g+rwX "$REPO"

# rest-server htpasswd file. htpasswd is intentionally generated from a
# separate transport credential, not the restic repository encryption key.
if command -v htpasswd >/dev/null; then
  htpasswd -Bbc "$ROOT/.htpasswd" "$REST_SERVER_USER" "$REST_SERVER_PASSWORD"
  chown root:immutavault-store "$ROOT/.htpasswd"
  chmod 640 "$ROOT/.htpasswd"
else
  echo "WARNING: apache2-utils/htpasswd is missing; create $ROOT/.htpasswd before enabling rest-server" >&2
fi

# Generate a self-signed TLS certificate when no certificate exists. Replace it
# with your internal/public PKI certificate for production if desired.
if [[ ! -f "$CONFIG_DIR/tls/server.crt" || ! -f "$CONFIG_DIR/tls/server.key" ]]; then
  FQDN="${BACKUP_FQDN:-$(hostname -f 2>/dev/null || hostname)}"
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
    -keyout "$CONFIG_DIR/tls/server.key" \
    -out "$CONFIG_DIR/tls/server.crt" \
    -subj "/CN=$FQDN" \
    -addext "subjectAltName=DNS:$FQDN"
  cp "$CONFIG_DIR/tls/server.crt" "$CONFIG_DIR/tls/ca.crt"
  chown root:immutavault-store "$CONFIG_DIR/tls/server.key"
  chmod 640 "$CONFIG_DIR/tls/server.key"
  chown root:root "$CONFIG_DIR/tls/server.crt" "$CONFIG_DIR/tls/ca.crt"
  chmod 644 "$CONFIG_DIR/tls/server.crt" "$CONFIG_DIR/tls/ca.crt"
fi

# Trust the vault CA system-wide so restic copy can read the internal REST source
# while writing to public S3 endpoints without replacing the public CA bundle.
if command -v update-ca-certificates >/dev/null; then
  cp "$CONFIG_DIR/tls/ca.crt" /usr/local/share/ca-certificates/immutavault-vault.crt
  update-ca-certificates >/dev/null
elif command -v update-ca-trust >/dev/null; then
  cp "$CONFIG_DIR/tls/ca.crt" /etc/pki/ca-trust/source/anchors/immutavault-vault.crt
  update-ca-trust extract
fi

# Generate a separate portal TLS key readable by the controller account.
# Do not reuse the repository daemon's private key across trust zones.
if [[ ! -f "$CONFIG_DIR/tls/portal.crt" || ! -f "$CONFIG_DIR/tls/portal.key" ]]; then
  FQDN="${BACKUP_FQDN:-$(hostname -f 2>/dev/null || hostname)}"
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
    -keyout "$CONFIG_DIR/tls/portal.key" \
    -out "$CONFIG_DIR/tls/portal.crt" \
    -subj "/CN=$FQDN" \
    -addext "subjectAltName=DNS:$FQDN"
  chown root:immutavault "$CONFIG_DIR/tls/portal.key"
  chmod 640 "$CONFIG_DIR/tls/portal.key"
  chown root:root "$CONFIG_DIR/tls/portal.crt"
  chmod 644 "$CONFIG_DIR/tls/portal.crt"
fi

# Keep the installed YAML consistent with a non-default --repo-root.
if [[ -f "$CONFIG_DIR/immutavault.yml" ]]; then
  IMMUTAVAULT_REPO_ROOT="$ROOT" python3 - <<'PYCFG'
import os
from pathlib import Path
import yaml
path = Path('/etc/immutavault/immutavault.yml')
data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
root = os.environ['IMMUTAVAULT_REPO_ROOT'].rstrip('/')
repo = data.setdefault('repository', {})
repo['local_path'] = f'{root}/repository'
repo['staging_path'] = f'{root}/staging'
runtime = data.setdefault('runtime', {})
runtime['restore_staging_path'] = f'{root}/restore-staging'
runtime['verify_staging_path'] = f'{root}/verify-staging'
path.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
PYCFG
  chown root:immutavault "$CONFIG_DIR/immutavault.yml"
  chmod 640 "$CONFIG_DIR/immutavault.yml"
fi

echo "Repository initialized at $REPO"
echo "Next: install systemd units from ./systemd and configure TLS before exposing ports 8000/8787."
