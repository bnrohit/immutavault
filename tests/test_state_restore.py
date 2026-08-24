from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import pytest
from immutavault.state import StateDB


def point():
    now = datetime.now(timezone.utc)
    return {
        "snapshot_id": "abc123", "created_at": now.isoformat(), "platform": "pve", "platform_type": "proxmox",
        "vm_id": "100", "vm_name": "server1", "power_state": "running", "source_path": "/tmp/source",
        "backup_bytes": 100, "data_added": 10, "file_count": 2, "manifest_sha256": "x",
        "immutable_until": (now + timedelta(days=30)).isoformat(), "suspicious": 0, "suspicious_reason": None,
        "verified": 0, "verified_at": None, "metadata_json": json.dumps({}),
    }


def test_four_eyes_restore(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    p = point(); db.upsert_recovery_point(p)
    rid = db.create_restore_request(
        requester="alice", point=p, target_platform="pve", target_name="server1-restore", options={}, status="pending_approval"
    )
    with pytest.raises(PermissionError):
        db.approve_restore(rid, "alice", four_eyes=True)
    db.approve_restore(rid, "bob", four_eyes=True)
    assert db.get_restore_request(rid)["status"] == "approved"


def test_verification_preserves_backup_metadata(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    p = point()
    p["metadata_json"] = json.dumps({"platform_info": {"version": "8.2"}, "vm_kind": "qemu"})
    db.upsert_recovery_point(p)
    db.mark_verified("abc123", True, {"errors": []})
    saved = db.get_point("abc123")
    metadata = json.loads(saved["metadata_json"])
    assert metadata["platform_info"]["version"] == "8.2"
    assert metadata["vm_kind"] == "qemu"
    assert metadata["verification"] == {"errors": []}


def test_audit_hash_chain_detects_tamper(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    db.audit("alice", "backup.complete", "vm", "100", {"ok": True})
    db.audit("bob", "restore.request", "vm", "100", {"snapshot": "abc"})
    ok, errors = db.verify_audit_chain()
    assert ok and not errors
    with db.connect() as conn:
        conn.execute("UPDATE audit_events SET actor='mallory' WHERE id=1")
    ok, errors = db.verify_audit_chain()
    assert not ok
    assert any("hash mismatch" in error for error in errors)


def test_hold_only_extends_immutability(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    p = point(); db.upsert_recovery_point(p)
    original = p["immutable_until"]
    shorter = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    saved = db.extend_immutability("abc123", shorter, actor="admin")
    assert saved == original
    longer = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    saved = db.extend_immutability("abc123", longer, actor="admin")
    assert saved == longer


def test_state_online_backup_and_status(tmp_path):
    db = StateDB(str(tmp_path / "state.db"))
    p = point(); db.upsert_recovery_point(p)
    db.audit("system", "backup.complete", "recovery_point", p["snapshot_id"], {"ok": True})
    status = db.status_summary()
    assert status["recovery_points"]["total"] == 1
    destination = db.backup_database(str(tmp_path / "state-backups"), keep=2)
    assert Path(destination).exists()
    restored = StateDB(destination)
    assert restored.get_point("abc123")["vm_name"] == "server1"
