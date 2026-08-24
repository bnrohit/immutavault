from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

SMALL_FILE_HASH_LIMIT = 64 * 1024 * 1024
MANIFEST_NAME = ".immutavault-manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: Path) -> tuple[dict[str, Any], str]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    # Persistent incremental roots are intentionally reused. Never include the
    # previous manifest inside the next manifest or verification becomes
    # self-referential and the old manifest hash can never match the new file.
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != MANIFEST_NAME):
        stat = path.stat()
        total_bytes += stat.st_size
        records.append({
            "path": str(path.relative_to(root)),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(path) if stat.st_size <= SMALL_FILE_HASH_LIMIT else None,
        })
    manifest: dict[str, Any] = {"version": 1, "files": records, "total_bytes": total_bytes, "file_count": len(records)}
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest, digest


def verify_manifest(root: Path, *, expected_digest: str | None = None) -> tuple[bool, list[str]]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        return False, ["manifest missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if expected_digest:
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        actual_digest = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            errors.append("manifest digest mismatch")
    for item in manifest.get("files", []):
        path = root / item["path"]
        if not path.is_file():
            errors.append(f"missing: {item['path']}")
            continue
        if path.stat().st_size != int(item["size"]):
            errors.append(f"size mismatch: {item['path']}")
            continue
        if item.get("sha256") and sha256_file(path) != item["sha256"]:
            errors.append(f"hash mismatch: {item['path']}")
    return not errors, errors
