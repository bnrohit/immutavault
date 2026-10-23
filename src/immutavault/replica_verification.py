"""Verified replica recovery: restore bytes and check the pinned backup manifest.

Explicit operator-triggered operation. It only reads a replica, uses private
temporary space, and updates the local verification/audit catalog. It cannot
create, power on, shut down, delete, or overwrite any hypervisor VM.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from .integrity import verify_manifest


class ReplicaVerificationError(RuntimeError):
    pass


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if date.tzinfo is None:
        return None
    return date.astimezone(timezone.utc)


def replica_copy_ready(copy: dict[str, Any] | None, *, now: datetime | None = None,
                       max_verification_age_hours: int = 24) -> bool:
    """Fail closed on missing, expired, revoked or superseded replica evidence.

    A passing result proves a previous full manifest restore, not application
    boot correctness, current object-lock protection or complete site failover.
    """
    if not copy or copy.get("status") != "success" or copy.get("verified") not in (1, True):
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or not (1 <= max_verification_age_hours <= 720):
        return False
    verified = _utc(copy.get("verified_at"))
    copied = _utc(copy.get("copied_at"))
    if not verified or not copied or verified < copied:
        return False
    age = (current.astimezone(timezone.utc) - verified).total_seconds()
    return 0 <= age <= max_verification_age_hours * 3600


def _restored_source(root: Path, path: Any) -> Path:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ReplicaVerificationError("recovery point must specify an absolute source path")
    raw = PurePosixPath(path)
    if any(part in (".", "..") for part in path.split("/")) or "\x00" in path:
        raise ReplicaVerificationError("unsafe recovery point source path")
    parts = raw.parts[1:]
    if not parts:
        raise ReplicaVerificationError("recovery point source path is empty")
    result = root.joinpath(*parts)
    if not result.is_relative_to(root):
        raise ReplicaVerificationError("recovery source path escapes verification area")
    return result


def _ensure_safe_manifest(root: Path) -> None:
    """Refuse file/symlink escapes even if a restored manifest is malicious."""
    path = root / ".immutavault-manifest.json"
    if not path.is_file() or path.is_symlink():
        raise ReplicaVerificationError("restored manifest file is missing or unsafe")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ReplicaVerificationError("restored manifest has an invalid schema")
    for entry in data["files"]:
        if not isinstance(entry, dict):
            raise ReplicaVerificationError("restored manifest contains invalid entries")
        name = entry.get("path")
        if not isinstance(name, str) or not name or name.startswith("/") or chr(0) in name:
            raise ReplicaVerificationError("restored manifest contains unsafe file path")
        if any(part in ("", ".", "..") for part in name.split("/")):
            raise ReplicaVerificationError("restored manifest contains traversal path")
        item = root.joinpath(*PurePosixPath(name).parts)
        current = item
        while current != root:
            if current.is_symlink():
                raise ReplicaVerificationError("restored manifest accesses a symbolic-link path")
            current = current.parent


def verify_replica_restore(engine: Any, *, snapshot_id: str, replica_name: str,
                           actor: str) -> dict[str, Any]:
    """Restore FROM the selected replica and compare actual files to primary digest.

    No implicit success for catalog entries or a restic snapshots listing.
    On failure the previously verified flag is cleared so DR fails closed.
    """
    if not actor or not actor.strip():
        raise ReplicaVerificationError("named actor required")
    if not snapshot_id or not replica_name or replica_name == "primary":
        raise ReplicaVerificationError("explicit recovery snapshot and non-primary replica required")
    point = engine.state.get_point(snapshot_id)
    if not point:
        raise ReplicaVerificationError("unknown recovery point")
    copy = engine.state.get_recovery_copy(snapshot_id, replica_name)
    if not copy or copy.get("status") != "success":
        raise ReplicaVerificationError("snapshot is not recorded as copied successfully to selected replica")
    if not point.get("verified") or point.get("suspicious"):
        raise ReplicaVerificationError("primary recovery point is not verified and clean")
    if not isinstance(point.get("manifest_sha256"), str) or len(point["manifest_sha256"]) != 64:
        raise ReplicaVerificationError("recovery point is missing an authenticated manifest digest")
    try:
        # Treat both catalog metadata and restored files as untrusted input.
        _restored_source(Path("/tmp/immutavault-path-check"), point.get("source_path"))
        replica = engine._replica(replica_name)
        health = engine.repo.replica_health(replica)
        if not health.get("ok"):
            raise ReplicaVerificationError("replica target health check failed")
        engine._ensure_path_capacity(engine.cfg.runtime.verify_staging_path,
                                     action="replica verification")
        base = Path(engine.cfg.runtime.verify_staging_path)
        base.mkdir(parents=True, exist_ok=True)
        expected_bytes = point.get("backup_bytes")
        if type(expected_bytes) is not int or expected_bytes <= 0:
            raise ReplicaVerificationError("backup size not recorded; refuse unbounded readback")
        free_bytes = shutil.disk_usage(base).free
        reserve = max(512 * 2**20, expected_bytes // 5)
        if free_bytes < expected_bytes + reserve:
            raise ReplicaVerificationError("not enough private staging capacity for replica readback")
        with TemporaryDirectory(prefix="replica-verify-", dir=base) as temp:
            root = Path(temp)
            engine.repo.restore(snapshot_id, str(root), replica=replica)
            expected = _restored_source(root, point["source_path"])
            if not expected.is_dir() or expected.is_symlink():
                raise ReplicaVerificationError("restored recovery point directory is missing or unsafe")
            relative = expected.relative_to(root)
            current = root
            for component in relative.parts:
                current = current / component
                if current.is_symlink():
                    raise ReplicaVerificationError("restored source includes an unsafe symbolic link")
            _ensure_safe_manifest(expected)
            ok, errors = verify_manifest(expected, expected_digest=point["manifest_sha256"])
            if not ok:
                raise ReplicaVerificationError("restored replica manifest did not verify: " +
                                               "; ".join(errors[:3]))
        engine.state.mark_copy_verified(snapshot_id, replica_name, True)
        engine.state.audit(actor, "replica.restore_verified", "recovery_copy",
                           snapshot_id + ":" + replica_name,
                           {"snapshot_id": snapshot_id, "replica": replica_name,
                            "method": "actual-restic-restore-plus-manifest",
                            "source_vm_unchanged": True})
        return {"ok": True, "snapshot_id": snapshot_id, "replica": replica_name,
                "method": "actual-restic-restore-plus-manifest",
                "hypervisor_changes": False}
    except Exception as exc:
        # Do not place arbitrary exception strings into audit logs: transport
        # errors can contain endpoint credentials or private paths.
        engine.state.mark_copy_verified(snapshot_id, replica_name, False,
                                        error="replica restore verification failed")
        engine.state.audit(actor, "replica.restore_verification_failed", "recovery_copy",
                           snapshot_id + ":" + replica_name,
                           {"snapshot_id": snapshot_id, "replica": replica_name,
                            "error_type": type(exc).__name__})
        raise ReplicaVerificationError(
            "replica restore verification failed; inspect controller logs without sharing secrets"
        ) from exc
