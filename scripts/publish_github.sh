#!/usr/bin/env bash
set -euo pipefail

REPO_NAME="${1:-immutavault}"
VISIBILITY="${2:-private}"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required." >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Authenticate first with: gh auth login" >&2
  exit 1
fi
if [[ "$VISIBILITY" != "private" && "$VISIBILITY" != "public" && "$VISIBILITY" != "internal" ]]; then
  echo "Visibility must be private, public, or internal" >&2
  exit 2
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || git init -b main
git add .
if ! git diff --cached --quiet; then
  git commit -m "Initial Immutavault release"
fi

gh repo create "$REPO_NAME" "--$VISIBILITY" --source . --remote origin --push
