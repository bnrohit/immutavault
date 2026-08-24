from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import os

import pytest

from immutavault.config import load_config
from immutavault.flr import FLRManager, FLRSession, FLRSettings
from immutavault.restic import ResticRepository


def _manager(tmp_path, *, max_download_bytes=1024 * 1024):
    base = load_config("config/immutavault.example.yml")
    cfg = replace(base, runtime=replace(base.runtime, state_db=str(tmp_path / "state.db")))
    settings = FLRSettings(
        enabled=True,
        mount_root=str(tmp_path / "flr"),
        session_ttl_minutes=30,
        max_download_bytes=max_download_bytes,
        max_sessions_per_user=2,
        max_disks=16,
        mount_wait_seconds=1,
    )
    return FLRManager(cfg, ResticRepository(cfg.repository), settings=settings)


def _inject_session(manager, tmp_path, *, owner="alice"):
    guest = tmp_path / "guest"
    guest.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    session = FLRSession(
        session_id="session1",
        owner=owner,
        snapshot_id="abc123",
        platform="vc",
        vm_name="fileserver",
        source_path="/stage/fileserver",
        root=tmp_path,
        restic_mount=tmp_path / "restic",
        guest_mount=guest,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    manager._sessions[session.session_id] = session
    return session


def test_flr_browses_regular_files_without_following_symlinks(tmp_path):
    manager = _manager(tmp_path)
    session = _inject_session(manager, tmp_path)
    (session.guest_mount / "Users").mkdir()
    (session.guest_mount / "Users" / "budget.xlsx").write_bytes(b"xlsx-data")
    os.symlink("budget.xlsx", session.guest_mount / "Users" / "shortcut.xlsx")

    listing = manager.list_directory("session1", "/Users", actor="alice")
    by_name = {x["name"]: x for x in listing["entries"]}
    assert by_name["budget.xlsx"]["type"] == "file"
    assert by_name["budget.xlsx"]["downloadable"] is True
    assert by_name["shortcut.xlsx"]["type"] == "symlink"
    assert by_name["shortcut.xlsx"]["downloadable"] is False

    recovered = manager.open_file("session1", "/Users/budget.xlsx", actor="alice")
    output = io.BytesIO()
    manager.stream_file(recovered, output, chunk_size=3)
    assert output.getvalue() == b"xlsx-data"
    with pytest.raises(PermissionError, match="does not follow guest symlinks"):
        manager.open_file("session1", "/Users/shortcut.xlsx", actor="alice")


def test_flr_rejects_traversal_and_cross_user_access(tmp_path):
    manager = _manager(tmp_path)
    _inject_session(manager, tmp_path)
    with pytest.raises(ValueError, match="path traversal"):
        manager.list_directory("session1", "/../../etc", actor="alice")
    with pytest.raises(PermissionError, match="another portal user"):
        manager.list_directory("session1", "/", actor="bob")


def test_flr_download_limit_is_enforced(tmp_path):
    manager = _manager(tmp_path, max_download_bytes=4)
    session = _inject_session(manager, tmp_path)
    (session.guest_mount / "large.bin").write_bytes(b"12345")
    with pytest.raises(ValueError, match="above configured FLR download limit"):
        manager.open_file("session1", "/large.bin", actor="alice")


def test_flr_discovers_descriptor_disks_not_vmdk_extents(tmp_path):
    manager = _manager(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    descriptor = source / "disk.vmdk"
    descriptor.write_text("descriptor", encoding="utf-8")
    (source / "disk-flat.vmdk").write_bytes(b"extent")
    (source / "data.qcow2").write_bytes(b"qcow")
    disks = manager._discover_disk_images(source)
    assert descriptor in disks
    assert source / "data.qcow2" in disks
    assert source / "disk-flat.vmdk" not in disks


def test_flr_schema_defaults_are_validated():
    cfg = load_config("config/immutavault.example.yml")
    assert cfg.flr.enabled is True
    assert cfg.flr.mount_root.startswith("/")
    assert 1 <= cfg.flr.session_ttl_minutes <= 240
