#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f pyproject.toml && -f VERSION ]] || { echo "Run from repository root" >&2; exit 1; }
VERSION=$(tr -d '[:space:]' < VERSION)
TARGET="/opt/immutavault/releases/$VERSION"
CURRENT="/opt/immutavault/current"
mkdir -p /opt/immutavault/releases
[[ ! -e "$TARGET" ]] || { echo "Release already installed: $TARGET" >&2; exit 2; }

BUILD_TMP=$(mktemp -d)
trap 'rm -rf "$BUILD_TMP"' EXIT
if ! python3 -m pip wheel . --no-deps --no-build-isolation -w "$BUILD_TMP" >/dev/null; then
  echo "Wheel build failed; current release was not changed" >&2
  exit 3
fi
WHEEL=$(find "$BUILD_TMP" -maxdepth 1 -name 'immutavault-*.whl' -print -quit)
[[ -f "$WHEEL" ]] || { echo "Wheel build did not produce an artifact" >&2; exit 3; }
python3 -m venv --system-site-packages "$TARGET"
if ! "$TARGET/bin/pip" install --no-deps --no-index "$WHEEL" >/dev/null; then
  rm -rf "$TARGET"
  echo "Install failed; current release was not changed" >&2
  exit 3
fi
"$TARGET/bin/python" - <<'PY'
import yaml, boto3, immutavault
print(f"Runtime dependencies OK for Immutavault {immutavault.__version__}")
PY
"$TARGET/bin/python" -m compileall -q "$TARGET/lib"
"$TARGET/bin/immutavault" --help >/dev/null
"$TARGET/bin/immutavault-setup" --help >/dev/null

# Validate the installed production configuration before changing the live symlink.
if [[ -f /etc/immutavault/immutavault.yml ]]; then
  "$TARGET/bin/python" - <<'PY'
from immutavault.config import load_config
load_config('/etc/immutavault/immutavault.yml')
print('Production configuration parses with new release')
PY
fi

PREVIOUS=$(readlink -f "$CURRENT" 2>/dev/null || true)
ln -sfn "$TARGET" "${CURRENT}.new"
mv -Tf "${CURRENT}.new" "$CURRENT"
ln -sfn "$CURRENT/bin/immutavault" /usr/local/bin/immutavault
ln -sfn "$CURRENT/bin/immutavault-setup" /usr/local/bin/immutavault-setup
printf '%s\n' "$PREVIOUS" > /opt/immutavault/previous-release

# Only the long-running portal needs a restart. Timers/oneshots use the new
# /usr/local/bin target on their next run; rest-server is an independent binary.
if systemctl is-enabled --quiet immutavault-portal.service 2>/dev/null || systemctl is-active --quiet immutavault-portal.service 2>/dev/null; then
  systemctl try-restart immutavault-portal.service
  sleep 1
fi
if ! /usr/local/bin/immutavault --help >/dev/null; then
  echo "New release smoke test failed; rolling back" >&2
  ./scripts/rollback.sh
  exit 4
fi

echo "Upgraded atomically to Immutavault $VERSION. Repository data was not modified."
