from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from immutavault.integrity import build_manifest
from immutavault.replica_verification import (
    ReplicaVerificationError, _restored_source, replica_copy_ready, verify_replica_restore,
)


NOW = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)


def entry(**overrides):
    base = {"status": "success", "verified": 1,
            "copied_at": (NOW - timedelta(hours=2)).isoformat(),
            "verified_at": (NOW - timedelta(hours=1)).isoformat()}
    base.update(overrides)
    return base


def test_recent_verified_copy_passes():
    assert replica_copy_ready(entry(), now=NOW)


@pytest.mark.parametrize("patch", [
    {"verified": 0}, {"status": "failed"}, {"verified_at": ""},
    {"verified_at": (NOW - timedelta(days=5)).isoformat()},
    {"verified_at": (NOW + timedelta(hours=1)).isoformat()},
    {"copied_at": NOW.isoformat()},
    {"copied_at": "no-date"},
    {"verified_at": NOW.replace(tzinfo=None).isoformat()},
])
def test_stale_unverified_or_superseded_copy_blocks_dr(patch):
    assert not replica_copy_ready(entry(**patch), now=NOW)


def test_catalog_missing_blocks():
    assert not replica_copy_ready(None, now=NOW)


@pytest.mark.parametrize("path", ["/../etc/passwd", "/a/../../secret", "/a/./b", "/",
                                  "relative/path", "/a/\x00b"])
def test_restore_path_traversal_blocked(path):
    with pytest.raises(ReplicaVerificationError):
        _restored_source(Path("/safe"), path)


class State:
    def __init__(self):
        self.status = None
        self.events = []
    def get_point(self, _id):
        return {"verified": 1, "suspicious": 0, "manifest_sha256": self.digest,
                "source_path": "/srv/backups/pilot"}
    def get_recovery_copy(self, snapshot, name):
        return {"status": "success"}
    def mark_copy_verified(self, snapshot, name, verified, error=None):
        self.status = verified
    def audit(self, *args):
        self.events.append(args)


class Repo:
    def __init__(self, *, corrupt=False):
        self.corrupt = corrupt
    def replica_health(self, replica):
        return {"ok": True}
    def restore(self, snapshot, root, *, replica):
        dest = Path(root) / "srv/backups/pilot"
        dest.mkdir(parents=True)
        (dest / "data.txt").write_text("changed" if self.corrupt else "verified")


class Engine:
    def __init__(self, tmp_path, *, corrupt=False):
        self.state = State()
        self.repo = Repo(corrupt=corrupt)
        self.cfg = SimpleNamespace(runtime=SimpleNamespace(verify_staging_path=str(tmp_path)))
        original = tmp_path / "original"
        original.mkdir()
        (original / "data.txt").write_text("verified")
        _, self.state.digest = build_manifest(original)
    def _replica(self, name):
        return SimpleNamespace(name=name)
    def _ensure_path_capacity(self, *args, **kwargs):
        pass


def test_real_readback_manifest_success(tmp_path):
    engine = Engine(tmp_path)
    result = verify_replica_restore(
        engine, snapshot_id="snap1", replica_name="offsite", actor="operator")
    assert result["ok"] is True
    assert result["hypervisor_changes"] is False
    assert engine.state.status is True
    assert not list(tmp_path.glob("replica-verify-*"))


def test_corrupted_readback_revokes_previous_verification(tmp_path):
    engine = Engine(tmp_path, corrupt=True)
    with pytest.raises(ReplicaVerificationError):
        verify_replica_restore(engine, snapshot_id="snap1",
                               replica_name="offsite", actor="operator")
    assert engine.state.status is False
    assert not list(tmp_path.glob("replica-verify-*"))


def test_explicit_actor_required(tmp_path):
    engine = Engine(tmp_path)
    with pytest.raises(ReplicaVerificationError):
        verify_replica_restore(engine, snapshot_id="snap1", replica_name="offsite", actor="")
