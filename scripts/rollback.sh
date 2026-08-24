#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
FILE=/opt/immutavault/previous-release
[[ -s $FILE ]] || { echo "No previous release recorded" >&2; exit 2; }
PREVIOUS=$(cat "$FILE")
[[ -x "$PREVIOUS/bin/immutavault" ]] || { echo "Previous release is unavailable: $PREVIOUS" >&2; exit 3; }
ln -sfn "$PREVIOUS" /opt/immutavault/current.new
mv -Tf /opt/immutavault/current.new /opt/immutavault/current
ln -sfn /opt/immutavault/current/bin/immutavault /usr/local/bin/immutavault
if systemctl is-enabled --quiet immutavault-portal.service 2>/dev/null || systemctl is-active --quiet immutavault-portal.service 2>/dev/null; then
  systemctl try-restart immutavault-portal.service
fi
/usr/local/bin/immutavault --help >/dev/null
echo "Rolled back to $PREVIOUS"
