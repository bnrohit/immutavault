from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from immutavault.flr_broker import FLRBrokerClient, FLRBrokerServer


class FakeManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions = {}
        self.owners: dict[str, str] = {}
        self.payload = b"hello from immutable recovery\n"

    def status(self):
        return {"enabled": True, "active_sessions": len(self.owners), "prerequisite_problems": []}

    def open_session(self, point, *, actor):
        sid = f"session-{len(self.owners) + 1}"
        self.owners[sid] = actor
        return {
            "session_id": sid,
            "snapshot_id": point["snapshot_id"],
            "platform": point["platform"],
            "vm_name": point["vm_name"],
            "read_only": True,
        }

    def _owner(self, sid: str, actor: str) -> None:
        if sid not in self.owners:
            raise ValueError("FLR session not found")
        if self.owners[sid] != actor:
            raise PermissionError("FLR session belongs to another portal user")

    def list_directory(self, sid, path, *, actor, admin=False):
        self._owner(sid, actor)
        return {"path": path, "entries": [{"name": "report.txt", "type": "file", "size": len(self.payload)}]}

    def open_file(self, sid, path, *, actor, admin=False):
        self._owner(sid, actor)
        return SimpleNamespace(name=Path(path).name, size=len(self.payload))

    def stream_file(self, file, writer):
        writer.write(self.payload)

    def close_session(self, sid, *, actor=None, force=False):
        if actor is None:
            raise PermissionError("owner required")
        self._owner(sid, actor)
        self.owners.pop(sid)


@pytest.fixture
def broker(tmp_path):
    manager = FakeManager()
    socket_path = tmp_path / "flr.sock"
    server = FLRBrokerServer(str(socket_path), manager, allowed_uid=os.getuid())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield manager, str(socket_path)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def _client(socket_path: str, *, runtime_timeout: int = 900) -> FLRBrokerClient:
    cfg = SimpleNamespace(
        flr=SimpleNamespace(enabled=True),
        runtime=SimpleNamespace(command_timeout_seconds=runtime_timeout),
    )
    return FLRBrokerClient(cfg, socket_path=socket_path)


def test_broker_open_browse_download_and_owner_close(broker):
    manager, socket_path = broker
    client = _client(socket_path)
    point = {"snapshot_id": "abc", "platform": "vc-a", "vm_name": "files", "source_path": "/staging/files"}
    session = client.open_session(point, actor="alice")
    sid = session["session_id"]
    assert client.status()["broker_available"] is True
    listing = client.list_directory(sid, "/", actor="alice")
    assert listing["entries"][0]["name"] == "report.txt"
    remote = client.open_file(sid, "/report.txt", actor="alice")
    output = BytesIO()
    client.stream_file(remote, output)
    assert output.getvalue() == manager.payload

    # portal_v09 passes actor=None, force=True for admins. The broker facade may
    # close only a session this same portal process observed Alice create.
    client.close_session(sid, actor=None, force=True)
    assert sid not in manager.owners


def test_broker_admin_flag_never_bypasses_session_owner(broker):
    _manager, socket_path = broker
    client = _client(socket_path)
    point = {"snapshot_id": "abc", "platform": "vc-a", "vm_name": "files", "source_path": "/staging/files"}
    sid = client.open_session(point, actor="alice")["session_id"]
    with pytest.raises(PermissionError, match="another portal user"):
        client.list_directory(sid, "/", actor="bob", admin=True)
    with pytest.raises(PermissionError, match="another portal user"):
        client.open_file(sid, "/report.txt", actor="bob", admin=True)


def test_unknown_admin_session_cannot_be_force_closed(broker):
    manager, socket_path = broker
    client_a = _client(socket_path)
    sid = client_a.open_session(
        {"snapshot_id": "abc", "platform": "vc-a", "vm_name": "files", "source_path": "/staging/files"},
        actor="alice",
    )["session_id"]
    client_b = _client(socket_path)
    with pytest.raises(PermissionError, match="ownership is unknown"):
        client_b.close_session(sid, actor=None, force=True)
    assert sid in manager.owners


def test_broker_rejects_missing_authenticated_actor(broker):
    _manager, socket_path = broker
    client = _client(socket_path)
    with pytest.raises(PermissionError, match="authenticated actor"):
        client.open_session(
            {"snapshot_id": "abc", "platform": "vc-a", "vm_name": "files", "source_path": "/staging/files"},
            actor="",
        )


def test_operation_timeout_tracks_runtime_window(broker):
    _manager, socket_path = broker
    assert _client(socket_path, runtime_timeout=14400).operation_timeout_seconds == 14400
    assert _client(socket_path, runtime_timeout=10).operation_timeout_seconds == 60
    assert _client(socket_path, runtime_timeout=999999).operation_timeout_seconds == 172800
