#!/usr/bin/env bash
set -euo pipefail

# Pinned upstream release. Updating this version requires updating and testing both
# architecture digests from the upstream release before shipping a new Immutavault release.
VERSION="0.19.1"
BASE_URL="https://github.com/restic/restic/releases/download/v${VERSION}"
DEST="${RESTIC_DEST:-/usr/local/bin/restic}"

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
for c in curl sha256sum bzip2 mktemp; do command -v "$c" >/dev/null || { echo "$c is required" >&2; exit 1; }; done

case "$(uname -m)" in
  x86_64|amd64)
    ARCH=amd64
    EXPECTED_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
    ;;
  aarch64|arm64)
    ARCH=arm64
    EXPECTED_SHA256=a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465
    ;;
  *) echo "Unsupported architecture for pinned restic binary: $(uname -m)" >&2; exit 2 ;;
esac

ASSET="restic_${VERSION}_linux_${ARCH}.bz2"
URL="${BASE_URL}/${ASSET}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Downloading verified restic v${VERSION} (${ARCH}) from the upstream GitHub release..."
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$TMP/$ASSET" "$URL"
printf '%s  %s\n' "$EXPECTED_SHA256" "$TMP/$ASSET" | sha256sum --check --status || {
  echo "SHA-256 verification failed; refusing to install restic" >&2
  exit 3
}

bzip2 -dc "$TMP/$ASSET" > "$TMP/restic"
chmod 0755 "$TMP/restic"
install -o root -g root -m 0755 "$TMP/restic" "$DEST"
"$(dirname "$0")/check_restic.sh" "$DEST"
printf 'Installed verified and compatible restic v%s to %s\n' "$VERSION" "$DEST"
