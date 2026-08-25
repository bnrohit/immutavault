from __future__ import annotations

import argparse
from dataclasses import dataclass
import grp
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import socketserver
import stat
import struct
import threading
from typing import Any, BinaryIO

from .enterprise_flr import EnterpriseFLRManager
from .flr import FLRManager
from .restic import ResticRepository
from .v2v_config import load_v10_config


DEFAULT_SOCKET = "/run/immutavault/flr.sock"
MAX_MESSAGE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RemoteFLRFile:
    session_id: str
    user_path: str
    actor: str
    name: str
    size: int


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PermissionError):
        kind = "permission"
    elif isinstance(exc, ValueError):
        kind = "value"
    else:
        kind = "runtime"
    return {"ok": False, "error_type": kind, "error": str(exc)}


def _raise_remote(payload: dict[str, Any]) -> None:
    message = str(payload.get("error") or "FLR broker request failed")
    kind = str(payload.get("error_type") or "runtime")
    if kind == "permission":
        raise PermissionError(message)
    if kind == "value":
        raise ValueError(message)
    raise RuntimeError(message)


def _json_line(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("FLR broker message exceeds maximum size")
    return encoded


def _read_json_line(reader: BinaryIO) -> dict[str, Any]:
    line = reader.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        raise RuntimeError("FLR broker closed the connection")
    if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
        raise ValueError("FLR broker message exceeds maximum size")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FLR broker returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("FLR broker response must be a JSON object")
    return value


class FLRBrokerClient:
    """Portal-side FLR facade.

    The network-facing portal never mounts FUSE filesystems and never needs
    /dev/fuse. It asks the local broker to perform privileged mount operations
    over a root-owned Unix socket. Session ownership remains actor-bound.
    """

    def __init__(self, cfg: Any, repo: Any | None = None, *, socket_path: str | None = None) -> None:
        self.cfg = cfg
        self.socket_path = socket_path or os.getenv("IMMUTAVAULT_FLR_BROKER_SOCKET", DEFAULT_SOCKET)
        self.enabled = bool(getattr(getattr(cfg, "flr", None), "enabled", True))
        self._owners: dict[str, str] = {}
        self._lock = threading.RLock()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"FLR broker is unavailable at {self.socket_path}: {exc}") from exc
        return sock

    def _request(self, action: str, **fields: Any) -> dict[str, Any]:
        with self._connect() as sock:
            stream = sock.makefile("rwb", buffering=0)
            stream.write(_json_line({"action": action, **fields}))
            payload = _read_json_line(stream)
        if payload.get("ok") is not True:
            _raise_remote(payload)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("FLR broker response omitted result object")
        return result

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "broker_available": False, "active_sessions": 0, "prerequisite_problems": []}
        try:
            value = self._request("status")
            value["broker_available"] = True
            return value
        except Exception as exc:
            return {
                "enabled": True,
                "broker_available": False,
                "active_sessions": 0,
                "prerequisite_problems": [str(exc)],
            }

    def open_session(self, point: dict[str, Any], *, actor: str) -> dict[str, Any]:
        allowed = {
            "snapshot_id": str(point["snapshot_id"]),
            "platform": str(point["platform"]),
            "vm_name": str(point["vm_name"]),
            "source_path": str(point["source_path"]),
        }
        result = self._request("open", point=allowed, actor=actor)
        session_id = str(result.get("session_id") or "")
        if not session_id:
            raise RuntimeError("FLR broker returned a session without an id")
        with self._lock:
            self._owners[session_id] = actor
        return result

    def list_directory(self, session_id: str, user_path: str, *, actor: str, admin: bool = False) -> dict[str, Any]:
        # Admin is deliberately ignored. Active mounted guest filesystems remain owner-only.
        return self._request("list", session_id=session_id, user_path=user_path, actor=actor)

    def open_file(self, session_id: str, user_path: str, *, actor: str, admin: bool = False) -> RemoteFLRFile:
        result = self._request("stat", session_id=session_id, user_path=user_path, actor=actor)
        return RemoteFLRFile(
            session_id=session_id,
            user_path=user_path,
            actor=actor,
            name=str(result["name"]),
            size=int(result["size"]),
        )

    def stream_file(self, file: RemoteFLRFile, writer: BinaryIO, *, chunk_size: int = 1024 * 1024) -> None:
        with self._connect() as sock:
            stream = sock.makefile("rwb")
            stream.write(_json_line({
                "action": "download",
                "session_id": file.session_id,
                "user_path": file.user_path,
                "actor": file.actor,
            }))
            stream.flush()
            header = _read_json_line(stream)
            if header.get("ok") is not True:
                _raise_remote(header)
            result = header.get("result") or {}
            size = int(result.get("size", -1))
            if size != file.size:
                raise RuntimeError("FLR file changed between validation and streaming")
            remaining = size
            while remaining:
                chunk = stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise RuntimeError("FLR broker ended file stream early")
                writer.write(chunk)
                remaining -= len(chunk)

    def close_session(self, session_id: str, *, actor: str | None = None, force: bool = False) -> None:
        # portal_v09 historically passed actor=None, force=True for admins. Keep
        # owner-only semantics by recovering only an owner this client observed.
        if actor is None:
            if not force:
                raise PermissionError("FLR session close requires its owner")
            with self._lock:
                actor = self._owners.get(session_id)
            if actor is None:
                raise PermissionError("FLR session ownership is unknown to this portal process")
        self._request("close", session_id=session_id, actor=actor)
        with self._lock:
            self._owners.pop(session_id, None)


class _ThreadingUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class FLRBrokerServer(_ThreadingUnixStreamServer):
    def __init__(
        self,
        socket_path: str,
        manager: EnterpriseFLRManager,
        *,
        allowed_uid: int,
        socket_gid: int | None = None,
    ) -> None:
        self.manager = manager
        self.allowed_uid = allowed_uid
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o750)
        if path.exists() or path.is_symlink():
            info = os.lstat(path)
            if not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError(f"refusing to replace non-socket FLR broker path: {path}")
            path.unlink()
        self.socket_path = str(path)
        super().__init__(self.socket_path, _FLRBrokerHandler)
        os.chmod(self.socket_path, 0o660)
        if socket_gid is not None and os.geteuid() == 0:
            os.chown(self.socket_path, 0, socket_gid)

    def authorize(self, connection: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            raise PermissionError("FLR broker requires Linux SO_PEERCRED support")
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        if uid not in {0, self.allowed_uid}:
            raise PermissionError("FLR broker rejected unauthorized local peer")

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "")
        if action == "status":
            return self.manager.status()
        if action == "open":
            point = request.get("point")
            if not isinstance(point, dict):
                raise ValueError("FLR open requires a recovery-point object")
            required = ("snapshot_id", "platform", "vm_name", "source_path")
            if any(not isinstance(point.get(key), str) or not point.get(key) for key in required):
                raise ValueError("FLR recovery-point object is incomplete")
            return self.manager.open_session(point, actor=str(request.get("actor") or ""))
        if action == "list":
            return self.manager.list_directory(
                str(request.get("session_id") or ""),
                str(request.get("user_path") or "/"),
                actor=str(request.get("actor") or ""),
                admin=False,
            )
        if action == "stat":
            file = self.manager.open_file(
                str(request.get("session_id") or ""),
                str(request.get("user_path") or ""),
                actor=str(request.get("actor") or ""),
                admin=False,
            )
            return {"name": file.name, "size": file.size}
        if action == "close":
            actor = str(request.get("actor") or "")
            if not actor:
                raise PermissionError("FLR session close requires an actor")
            session_id = str(request.get("session_id") or "")
            self.manager.close_session(session_id, actor=actor, force=False)
            return {"session_id": session_id, "status": "closed"}
        raise ValueError(f"unsupported FLR broker action: {action!r}")

    def cleanup(self) -> None:
        with self.manager._lock:
            session_ids = list(self.manager._sessions)
        for session_id in session_ids:
            FLRManager.close_session(self.manager, session_id, force=True)
        try:
            Path(self.socket_path).unlink()
        except FileNotFoundError:
            pass


class _FLRBrokerHandler(socketserver.StreamRequestHandler):
    server: FLRBrokerServer

    def handle(self) -> None:
        try:
            self.server.authorize(self.request)
            request = _read_json_line(self.rfile)
            action = str(request.get("action") or "")
            if action == "download":
                file = self.server.manager.open_file(
                    str(request.get("session_id") or ""),
                    str(request.get("user_path") or ""),
                    actor=str(request.get("actor") or ""),
                    admin=False,
                )
                self.wfile.write(_json_line({"ok": True, "result": {"name": file.name, "size": file.size}}))
                self.wfile.flush()
                self.server.manager.stream_file(file, self.wfile)
                self.wfile.flush()
                return
            result = self.server.dispatch(request)
            self.wfile.write(_json_line({"ok": True, "result": result}))
            self.wfile.flush()
        except Exception as exc:
            try:
                self.wfile.write(_json_line(_error_payload(exc)))
                self.wfile.flush()
            except Exception:
                return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immutavault-flr-broker", description="Privilege-separated Immutavault FLR mount broker")
    parser.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    parser.add_argument("--socket", default=os.getenv("IMMUTAVAULT_FLR_BROKER_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--allowed-user", default=os.getenv("IMMUTAVAULT_FLR_ALLOWED_USER", "immutavault"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_v10_config(args.config)
    repo = ResticRepository(cfg.repository, cfg.runtime.command_timeout_seconds)
    manager = EnterpriseFLRManager(cfg, repo)
    allowed_uid = pwd.getpwnam(args.allowed_user).pw_uid
    try:
        socket_gid = grp.getgrnam("immutavault").gr_gid
    except KeyError:
        socket_gid = None
    server = FLRBrokerServer(args.socket, manager, allowed_uid=allowed_uid, socket_gid=socket_gid)

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        server.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
