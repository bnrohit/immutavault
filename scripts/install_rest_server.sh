#!/usr/bin/env bash
set -euo pipefail

VERSION="${REST_SERVER_VERSION:-0.14.0}"
BASE_URL="https://github.com/restic/rest-server/releases/download/v${VERSION}"
DEST="${REST_SERVER_DEST:-/usr/local/bin/rest-server}"

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
for c in curl sha256sum tar mktemp; do command -v "$c" >/dev/null || { echo "$c is required" >&2; exit 1; }; done

case "$(uname -m)" in
  x86_64|amd64)
    ARCH=amd64
    EXPECTED_SHA256=4c9c95bc079a0334e81fad379b19dc5c3353c71c2c88d652cafce2081c2b1c66
    ;;
  aarch64|arm64)
    ARCH=arm64
    EXPECTED_SHA256=cef139cbe8b27b16bda731d17f093b0aa466b8c60b136c12d78b6f2bff3daf22
    ;;
  *) echo "Unsupported architecture for pinned rest-server binary: $(uname -m)" >&2; exit 2 ;;
esac

ASSET="rest-server_${VERSION}_linux_${ARCH}.tar.gz"
URL="${BASE_URL}/${ASSET}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Downloading verified rest-server v${VERSION} (${ARCH}) from the upstream GitHub release..."
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$TMP/$ASSET" "$URL"
printf '%s  %s\n' "$EXPECTED_SHA256" "$TMP/$ASSET" | sha256sum --check --status || {
  echo "SHA-256 verification failed; refusing to install rest-server" >&2
  exit 3
}

tar -xzf "$TMP/$ASSET" -C "$TMP"
BIN=$(find "$TMP" -type f -name rest-server -perm -u+x -print -quit)
[[ -n "$BIN" ]] || { echo "Verified archive did not contain an executable rest-server" >&2; exit 4; }
install -o root -g root -m 0755 "$BIN" "$DEST"
"$(dirname "$0")/check_rest_server.sh" "$DEST"
printf 'Installed verified and compatible rest-server v%s to %s\n' "$VERSION" "$DEST"
