#!/usr/bin/env bash
set -euo pipefail

VERSION="${IMMUTAVAULT_VERSION:-1.1.0}"
ROLE="all"
REPO_ROOT="/srv/immutavault"
ENABLE_SERVICES=0
ARCHIVE_SHA256="${IMMUTAVAULT_ARCHIVE_SHA256:-}"

usage() {
  cat <<'USAGE'
Immutavault pinned release bootstrap

Usage:
  curl -fsSL https://raw.githubusercontent.com/bnrohit/immutavault/v1.1.0/scripts/bootstrap.sh | sudo bash -s -- [options]

Options:
  --version X.Y.Z       Exact release version (default: 1.1.0)
  --role ROLE           all, controller, or repository (default: all)
  --repo-root PATH      Repository/staging root (default: /srv/immutavault)
  --enable-services     Enable normal services after installer validation
  -h, --help            Show this help

Supply IMMUTAVAULT_ARCHIVE_SHA256 to require an exact source-archive digest.
The bootstrap never follows main/latest implicitly.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:?missing version}"; shift 2 ;;
    --role) ROLE="${2:?missing role}"; shift 2 ;;
    --repo-root) REPO_ROOT="${2:?missing path}"; shift 2 ;;
    --enable-services) ENABLE_SERVICES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run bootstrap as root (for example, pipe to sudo bash)." >&2; exit 1; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must be X.Y.Z" >&2; exit 2; }
case "$ROLE" in all|controller|repository) ;; *) echo "Invalid role: $ROLE" >&2; exit 2;; esac
for cmd in curl tar sha256sum mktemp; do command -v "$cmd" >/dev/null || { echo "$cmd is required" >&2; exit 3; }; done

TMP=$(mktemp -d)
cleanup(){ rm -rf "$TMP"; }
trap cleanup EXIT
ARCHIVE="$TMP/immutavault-v${VERSION}.tar.gz"
URL="https://github.com/bnrohit/immutavault/archive/refs/tags/v${VERSION}.tar.gz"

echo "Downloading immutable release tag v${VERSION}..."
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$ARCHIVE" "$URL"

if [[ -n "$ARCHIVE_SHA256" ]]; then
  [[ "$ARCHIVE_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || { echo "IMMUTAVAULT_ARCHIVE_SHA256 must be a 64-character SHA-256 digest" >&2; exit 4; }
  printf '%s  %s\n' "${ARCHIVE_SHA256,,}" "$ARCHIVE" | sha256sum --check --status || {
    echo "Source archive SHA-256 mismatch; refusing installation." >&2
    exit 4
  }
  echo "Verified source archive SHA-256."
else
  echo "NOTICE: no IMMUTAVAULT_ARCHIVE_SHA256 supplied; source is pinned to tag v${VERSION} and protected by GitHub HTTPS, but not independently digest-pinned." >&2
fi

tar -xzf "$ARCHIVE" -C "$TMP"
SOURCE="$TMP/immutavault-${VERSION}"
[[ -f "$SOURCE/VERSION" && -x "$SOURCE/scripts/install.sh" ]] || { echo "Downloaded archive does not contain the expected Immutavault release layout" >&2; exit 5; }
[[ "$(tr -d '[:space:]' < "$SOURCE/VERSION")" == "$VERSION" ]] || { echo "Release VERSION does not match requested tag" >&2; exit 5; }

cd "$SOURCE"
./scripts/preflight.sh
./scripts/release_check.sh
ARGS=(--role "$ROLE" --repo-root "$REPO_ROOT")
if [[ $ENABLE_SERVICES -eq 1 ]]; then ARGS+=(--enable-services); fi
./scripts/install.sh "${ARGS[@]}"

echo "Immutavault v${VERSION} bootstrap completed."
