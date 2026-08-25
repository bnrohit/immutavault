from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from immutavault.config import load_config
from immutavault.enterprise_flr import EnterpriseFLRManager
from immutavault.flr import FLRSession, FLRSettings
from immutavault.restic import ResticRepository


def _manager(tmp_path):
    base = load_config("config/immutavault.example.yml")
    cfg = replace(base, runtime=replace(base.runtime, state_db=str(tmp_path / "state.db")))
    settings = FLRSettings(
        enabled=True,
        mount_root=str(tmp_path / "flr"),
        session_ttl_minutes=30,
        max_download_bytes=1024 * 1024,
        max_sessions_per_user=2,
        max_disks=16,
        mount_wait_seconds=1,
    )
    return EnterpriseFLRManager(cfg, ResticRepository(cfg.repository), settings=settings)


def _session(manager, tmp_path, *, owner="oidc:t:alice", expired=False):
    root = tmp_path / "session-root"
    guest = root / "guest"
    restic = root / "restic"
    guest.mkdir(parents=True)
    restic.mkdir()
    now = datetime.now(timezone.utc)
    session = FLRSession(
        session_id="sid",
        owner=owner,
        snapshot_id="snap",
        platform="vc-campus-a",
        vm_name="fileserver",
        source_path="/stage/fileserver",
        root=root,
        restic_mount=restic,
        guest_mount=guest,
        created_at=now - timedelta(minutes=60) if expired else now,
        expires_at=now - timedelta(minutes=1) if expired else now + timedelta(minutes=30),
    )
    manager._sessions[session.session_id] = session
    return session


def test_admin_flag_does_not_bypass_enterprise_flr_owner(tmp_path):
    manager = _manager(tmp_path)
    session = _session(manager, tmp_path)
    (session.guest_mount / "payroll.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError, match="another portal user"):
        manager.list_directory("sid", "/", actor="oidc:t:bob", admin=True)
    with pytest.raises(PermissionError, match="another portal user"):
        manager.open_file("sid", "/payroll.txt", actor="oidc:t:bob", admin=True)


def test_active_session_cannot_be_force_closed_by_non_owner_admin(tmp_path):
    manager = _manager(tmp_path)
    _session(manager, tmp_path)
    with pytest.raises(PermissionError, match="only be closed by their owner"):
        manager.close_session("sid", actor=None, force=True)


def test_expired_session_can_be_cleaned_up(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _session(manager, tmp_path, expired=True)
    monkeypatch.setattr(manager, "_cleanup_mount_paths", lambda *args, **kwargs: None)
    manager.close_session("sid", actor=None, force=True)
    assert "sid" not in manager._sessions
