#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-/srv/immutavault}"
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f pyproject.toml && -d src/immutavault && -f VERSION ]] || { echo "Run from the Immutavault repository root" >&2; exit 1; }
for cmd in python3 openssl; do command -v "$cmd" >/dev/null || { echo "$cmd is required" >&2; exit 1; }; done

VERSION=$(tr -d '[:space:]' < VERSION)
BASE=/opt/immutavault
TARGET="$BASE/releases/$VERSION"
CURRENT="$BASE/current"

id immutavault >/dev/null 2>&1 || useradd --system --home /var/lib/immutavault --shell /usr/sbin/nologin immutavault
install -d -m 0755 "$BASE" "$BASE/releases"

BUILD_TMP=$(mktemp -d)
NEW_TARGET=0
cleanup(){
  rm -rf "$BUILD_TMP"
  if [[ $NEW_TARGET -eq 1 ]]; then
    CURRENT_REAL=$(readlink -f "$CURRENT" 2>/dev/null || true)
    [[ "$CURRENT_REAL" == "$TARGET" ]] || rm -rf "$TARGET"
  fi
}
trap cleanup EXIT

python3 -m pip wheel . --no-deps --no-build-isolation -w "$BUILD_TMP" >/dev/null
WHEEL=$(find "$BUILD_TMP" -maxdepth 1 -name 'immutavault-*.whl' -print -quit)
[[ -f "$WHEEL" ]] || { echo "Failed to build Immutavault wheel" >&2; exit 2; }

# Virtualenv console scripts contain absolute interpreter paths. Create the
# environment at its final versioned pathname and switch the live symlink only
# after validation. Never relocate a virtualenv after creation.
if [[ -d "$TARGET" ]]; then
  if [[ -x "$TARGET/bin/immutavault" ]] && "$TARGET/bin/immutavault" --help >/dev/null 2>&1; then
    echo "Validated existing Immutavault runtime at $TARGET"
  else
    CURRENT_REAL=$(readlink -f "$CURRENT" 2>/dev/null || true)
    [[ "$CURRENT_REAL" != "$TARGET" ]] || {
      echo "Current runtime at $TARGET is invalid; refusing destructive in-place replacement" >&2
      exit 3
    }
    rm -rf "$TARGET"
  fi
fi

if [[ ! -d "$TARGET" ]]; then
  NEW_TARGET=1
  python3 -m venv --system-site-packages "$TARGET"
  "$TARGET/bin/pip" install --no-deps --no-index "$WHEEL" >/dev/null
fi

"$TARGET/bin/python" - <<'PYEOF'
import yaml, boto3, immutavault
print(f"Validated Immutavault {immutavault.__version__} runtime dependencies")
PYEOF
"$TARGET/bin/python" -m compileall -q "$TARGET/lib"
"$TARGET/bin/immutavault" --help >/dev/null
"$TARGET/bin/immutavault-setup" --help >/dev/null
"$TARGET/bin/immutavault-flr-broker" --help >/dev/null
"$TARGET/bin/immutavault-management-broker" --help >/dev/null

PREVIOUS=$(readlink -f "$CURRENT" 2>/dev/null || true)
ln -sfn "$TARGET" "${CURRENT}.new"
mv -Tf "${CURRENT}.new" "$CURRENT"
ln -sfn "$CURRENT/bin/immutavault" /usr/local/bin/immutavault
ln -sfn "$CURRENT/bin/immutavault-setup" /usr/local/bin/immutavault-setup
ln -sfn "$CURRENT/bin/immutavault-flr-broker" /usr/local/bin/immutavault-flr-broker
ln -sfn "$CURRENT/bin/immutavault-management-broker" /usr/local/bin/immutavault-management-broker
if [[ -n "$PREVIOUS" && "$PREVIOUS" != "$TARGET" ]]; then
  printf '%s\n' "$PREVIOUS" > "$BASE/previous-release"
fi
NEW_TARGET=0
trap - EXIT
rm -rf "$BUILD_TMP"

install -d -o root -g immutavault -m 0750 /etc/immutavault
install -d -o immutavault -g immutavault -m 0750 /var/lib/immutavault "$ROOT/staging" "$ROOT/restore-staging" "$ROOT/verify-staging"
# Only the root FLR broker mounts guest recovery points. Management-mounted NAS
# targets live under a separate root-owned path and are never mounted by portal.
install -d -o root -g root -m 0700 "$ROOT/flr"
install -d -o root -g immutavault -m 0750 "$ROOT/storage"
if getent group fuse >/dev/null 2>&1 && id -nG immutavault | tr ' ' '\n' | grep -qx fuse; then
  gpasswd -d immutavault fuse >/dev/null 2>&1 || true
fi

if [[ ! -f /etc/immutavault/immutavault.yml ]]; then
  cp config/immutavault.example.yml /etc/immutavault/immutavault.yml
  chown root:immutavault /etc/immutavault/immutavault.yml
  chmod 640 /etc/immutavault/immutavault.yml
fi
if [[ ! -f /etc/immutavault/immutavault.env ]]; then
  umask 077
  cat > /etc/immutavault/immutavault.env <<ENV
RESTIC_PASSWORD=$(openssl rand -base64 48 | tr -d '\n')
IMMUTAVAULT_CUSTOMER_TOKEN=$(openssl rand -hex 32)
IMMUTAVAULT_APPROVER_TOKEN=$(openssl rand -hex 32)
IMMUTAVAULT_ADMIN_TOKEN=$(openssl rand -hex 32)
IMMUTAVAULT_OIDC_SESSION_SECRET=$(openssl rand -base64 48 | tr -d '\n')
IMMUTAVAULT_METRICS_TOKEN=$(openssl rand -hex 32)
IMMUTAVAULT_REPO_ROOT=$ROOT
ENV
elif grep -q '^IMMUTAVAULT_REPO_ROOT=' /etc/immutavault/immutavault.env; then
  sed -i "s|^IMMUTAVAULT_REPO_ROOT=.*|IMMUTAVAULT_REPO_ROOT=$ROOT|" /etc/immutavault/immutavault.env
else
  echo "IMMUTAVAULT_REPO_ROOT=$ROOT" >> /etc/immutavault/immutavault.env
fi

# Enterprise upgrades add these secrets without rotating existing backup or
# portal credentials. They are generated once and remain root-readable only.
if ! grep -q '^IMMUTAVAULT_OIDC_SESSION_SECRET=' /etc/immutavault/immutavault.env; then
  echo "IMMUTAVAULT_OIDC_SESSION_SECRET=$(openssl rand -base64 48 | tr -d '\n')" >> /etc/immutavault/immutavault.env
fi
if ! grep -q '^IMMUTAVAULT_METRICS_TOKEN=' /etc/immutavault/immutavault.env; then
  echo "IMMUTAVAULT_METRICS_TOKEN=$(openssl rand -hex 32)" >> /etc/immutavault/immutavault.env
fi
chown root:immutavault /etc/immutavault/immutavault.env
chmod 640 /etc/immutavault/immutavault.env

echo "Controller installed at $TARGET. The primary portal now includes guided setup; the standalone setup console remains available as a break-glass/local migration aid."
