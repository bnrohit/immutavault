from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import socketserver
import stat
import struct
import tempfile
import threading
from typing import Any, BinaryIO

import yaml

from .management_config import POLICY_ID_RE, load_v11_config
from .policy import ProtectionPolicyRunner
from .runner import run
from .setup_wizard import SetupManager, _env_write, _metadata


DEFAULT_SOCKET = "/run/immutavault/manage.sock"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


def _json_line(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8") + b"\n"
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("management broker message exceeds maximum size")
    return payload


def _read_json_line(reader: BinaryIO) -> dict[str, Any]:
    line = reader.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        raise RuntimeError("management broker closed the connection")
    if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
        raise ValueError("management broker message exceeds maximum size")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("management broker returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("management broker message must be a JSON object")
    return value


def _error(exc: Exception) -> dict[str, Any]:
    kind = "permission" if isinstance(exc, PermissionError) else "value" if isinstance(exc, ValueError) else "runtime"
    return {"ok": False, "error_type": kind, "error": str(exc)}


def _raise_remote(payload: dict[str, Any]) -> None:
    message = str(payload.get("error") or "management broker request failed")
    if payload.get("error_type") == "permission":
        raise PermissionError(message)
    if payload.get("error_type") == "value":
        raise ValueError(message)
    raise RuntimeError(message)


class ManagementBrokerClient:
    def __init__(self, cfg: Any, *, socket_path: str | None = None) -> None:
        self.cfg = cfg
        self.socket_path = socket_path or getattr(cfg.management, "broker_socket", DEFAULT_SOCKET)
        self.timeout = max(60, int(getattr(cfg.runtime, "command_timeout_seconds", 3600)))

    def request(self, action: str, **fields: Any) -> Any:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(self.socket_path)
            sock.settimeout(self.timeout)
            stream = sock.makefile("rwb")
            stream.write(_json_line({"action": action, **fields})); stream.flush()
            payload = _read_json_line(stream)
        except OSError as exc:
            raise RuntimeError(f"management broker unavailable at {self.socket_path}: {exc}") from exc
        finally:
            sock.close()
        if payload.get("ok") is not True:
            _raise_remote(payload)
        return payload.get("result")

    def status(self) -> dict[str, Any]:
        try:
            result = self.request("status")
            result["broker_available"] = True
            return result
        except Exception as exc:
            return {"broker_available": False, "error": str(exc)}


class ManagementManager:
    def __init__(self, config_path: str, env_path: str) -> None:
        self.config_path = Path(config_path)
        self.env_path = Path(env_path)
        self.setup = SetupManager(config_path, env_path)

    def _data(self) -> dict[str, Any]:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be a mapping")
        return raw

    def _atomic_v11(self, data: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        uid, gid, mode = _metadata(self.config_path, 0o640)
        fd, tmp = tempfile.mkstemp(prefix=self.config_path.name + ".manage.", dir=str(self.config_path.parent))
        try:
            os.fchmod(fd, mode)
            try:
                os.fchown(fd, uid, gid)
            except PermissionError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, sort_keys=False)
                handle.flush(); os.fsync(handle.fileno())
            load_v11_config(tmp)
            if self.config_path.exists():
                backup = self.config_path.with_suffix(self.config_path.suffix + ".management-backup")
                backup.write_bytes(self.config_path.read_bytes())
                os.chmod(backup, mode)
            os.replace(tmp, self.config_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _policy_payload(body: dict[str, Any]) -> dict[str, Any]:
        policy_id = str(body.get("id") or "").strip().lower()
        if not POLICY_ID_RE.fullmatch(policy_id):
            raise ValueError("policy id must use lowercase letters, digits, dots, underscores, or hyphens")
        name = str(body.get("name") or policy_id).strip()
        selections = body.get("selections") or []
        if not isinstance(selections, list):
            raise ValueError("policy selections must be a list")
        schedule = body.get("schedule") or {"frequency": "manual"}
        if not isinstance(schedule, dict):
            raise ValueError("policy schedule must be a mapping")
        return {
            "id": policy_id,
            "name": name,
            "enabled": bool(body.get("enabled", True)),
            "selections": selections,
            "schedule": schedule,
            "immutable_days": int(body.get("immutable_days") or 30),
            "replica_targets": list(body.get("replica_targets") or []),
            "verify_after_backup": bool(body.get("verify_after_backup", True)),
        }

    def _timer_path(self, policy_id: str) -> Path:
        return Path("/etc/systemd/system") / f"immutavault-policy-{policy_id}.timer"

    def _sync_timer(self, policy_id: str) -> dict[str, Any]:
        cfg = load_v11_config(self.config_path)
        policy = next((item for item in cfg.management.policies if item.id == policy_id), None)
        path = self._timer_path(policy_id)
        unit = f"immutavault-policy-{policy_id}.timer"
        calendar = policy.schedule.on_calendar() if policy and policy.enabled else None
        if not calendar:
            if path.exists():
                run(["systemctl", "disable", "--now", unit], timeout=60, check=False)
                path.unlink(missing_ok=True)
                run(["systemctl", "daemon-reload"], timeout=60, check=False)
            return {"policy_id": policy_id, "scheduled": False, "unit": unit}
        content = (
            "[Unit]\n"
            f"Description=Immutavault protection policy {policy_id}\n\n"
            "[Timer]\n"
            f"OnCalendar={calendar}\n"
            "Persistent=true\n"
            "RandomizedDelaySec=60\n"
            f"Unit=immutavault-policy@{policy_id}.service\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o644)
        run(["systemctl", "daemon-reload"], timeout=60)
        result = run(["systemctl", "enable", "--now", unit], timeout=60, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"could not enable policy timer {unit}: {(result.stderr or result.stdout).strip()}")
        return {"policy_id": policy_id, "scheduled": True, "on_calendar": calendar, "unit": unit}

    def list_policies(self) -> list[dict[str, Any]]:
        cfg = load_v11_config(self.config_path)
        rows: list[dict[str, Any]] = []
        for policy in cfg.management.policies:
            rows.append({
                "id": policy.id,
                "name": policy.name,
                "enabled": policy.enabled,
                "schedule": {
                    "frequency": policy.schedule.frequency,
                    "time": policy.schedule.time,
                    "weekdays": list(policy.schedule.weekdays),
                    "every_hours": policy.schedule.every_hours,
                    "on_calendar": policy.schedule.on_calendar(),
                },
                "immutable_days": policy.immutable_days,
                "replica_targets": list(policy.replica_targets),
                "verify_after_backup": policy.verify_after_backup,
                "selections": [
                    {"platform": item.platform, "vms": list(item.vms)} for item in policy.selections
                ],
            })
        return rows

    def save_policy(self, body: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy_payload(body)
        data = self._data()
        management = dict(data.get("management") or {})
        rows = [row for row in (management.get("policies") or []) if row.get("id") != policy["id"]]
        rows.append(policy)
        management["enabled"] = True
        management["policies"] = rows
        data["management"] = management
        self._atomic_v11(data)
        timer = self._sync_timer(policy["id"])
        return {"saved": policy["id"], "timer": timer}

    def delete_policy(self, policy_id: str) -> dict[str, Any]:
        if not POLICY_ID_RE.fullmatch(policy_id):
            raise ValueError("invalid policy id")
        data = self._data()
        management = dict(data.get("management") or {})
        before = list(management.get("policies") or [])
        management["policies"] = [row for row in before if row.get("id") != policy_id]
        if len(before) == len(management["policies"]):
            raise ValueError(f"unknown protection policy: {policy_id}")
        data["management"] = management
        self._atomic_v11(data)
        timer = self._sync_timer(policy_id)
        return {"deleted": policy_id, "timer": timer}

    def run_policy(self, policy_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        if not POLICY_ID_RE.fullmatch(policy_id):
            raise ValueError("invalid policy id")
        if dry_run:
            cfg = load_v11_config(self.config_path)
            return ProtectionPolicyRunner(cfg).run(policy_id, dry_run=True, actor="management-broker")
        result = run(["systemctl", "start", f"immutavault-policy@{policy_id}.service"], timeout=60, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"could not start protection policy: {(result.stderr or result.stdout).strip()}")
        return {"policy_id": policy_id, "accepted": True, "unit": f"immutavault-policy@{policy_id}.service"}

    def save_dr_test_network(self, platform: str, network: str) -> dict[str, Any]:
        platform, network = platform.strip(), network.strip()
        cfg = load_v11_config(self.config_path)
        if platform not in {item.name for item in cfg.platforms if item.enabled}:
            raise ValueError("unknown or disabled target platform")
        if not network:
            raise ValueError("isolated DR test network is required")
        data = self._data(); management = dict(data.get("management") or {})
        networks = dict(management.get("dr_test_networks") or {})
        values = networks.get(platform) or []
        if isinstance(values, str): values = [values]
        networks[platform] = list(dict.fromkeys([*values, network]))
        management["dr_test_networks"] = networks; data["management"] = management
        self._atomic_v11(data)
        return {"platform": platform, "allowed_networks": networks[platform]}

    def status(self) -> dict[str, Any]:
        base = self.setup.status(); cfg = load_v11_config(self.config_path)
        base.update({
            "policies": len(cfg.management.policies),
            "management_enabled": cfg.management.enabled,
            "dr_test_networks": {key: list(value) for key, value in cfg.management.dr_test_networks.items()},
        })
        return base

    def dispatch(self, action: str, body: dict[str, Any]) -> Any:
        if action == "status": return self.status()
        if action == "dashboard": return self.setup.dashboard()
        if action == "platforms": return self.setup.platforms()
        if action == "platform_test": return self.setup.test_platform(body)
        if action == "platform_save": return self.setup.save_platform(body)
        if action == "platform_discover": return self.setup.discover(str(body.get("name") or ""))
        if action == "protection_save": return self.setup.save_selection(str(body.get("platform") or ""), list(body.get("vms") or []))
        if action == "storage_test": return self.setup.test_storage(body)
        if action == "storage_save": return self.setup.save_storage(body)
        if action == "storage_init": return self.setup.init_storage(str(body.get("name") or ""))
        if action == "doctor": return self.setup.doctor()
        if action == "backup_dry_run": return self.setup.backup(True)
        if action == "backup_run": return self.setup.backup(False)
        if action == "immutable_verify": return self.setup.verify_immutable_copies()
        if action == "policies": return self.list_policies()
        if action == "policy_save": return self.save_policy(body)
        if action == "policy_delete": return self.delete_policy(str(body.get("id") or ""))
        if action == "policy_run": return self.run_policy(str(body.get("id") or ""), dry_run=bool(body.get("dry_run", False)))
        if action == "dr_test_network_save": return self.save_dr_test_network(str(body.get("platform") or ""), str(body.get("network") or ""))
        raise ValueError(f"unsupported management action: {action!r}")


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class ManagementBrokerServer(_ThreadingUnixServer):
    def __init__(self, socket_path: str, manager: ManagementManager, *, allowed_uid: int, socket_gid: int | None = None) -> None:
        self.manager = manager; self.allowed_uid = allowed_uid
        path = Path(socket_path); path.parent.mkdir(parents=True, exist_ok=True); os.chmod(path.parent, 0o750)
        if path.exists() or path.is_symlink():
            info = os.lstat(path)
            if not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError(f"refusing to replace non-socket management broker path: {path}")
            path.unlink()
        self.socket_path = str(path)
        super().__init__(self.socket_path, _Handler)
        os.chmod(self.socket_path, 0o660)
        if socket_gid is not None and os.geteuid() == 0:
            os.chown(self.socket_path, 0, socket_gid)

    def authorize(self, connection: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            raise PermissionError("management broker requires Linux SO_PEERCRED support")
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        if uid not in {0, self.allowed_uid}:
            raise PermissionError("management broker rejected unauthorized local peer")

    def cleanup(self) -> None:
        try: Path(self.socket_path).unlink()
        except FileNotFoundError: pass


class _Handler(socketserver.StreamRequestHandler):
    server: ManagementBrokerServer
    def handle(self) -> None:
        try:
            self.server.authorize(self.request)
            request = _read_json_line(self.rfile)
            action = str(request.get("action") or "")
            body = request.get("body") or {}
            if not isinstance(body, dict):
                raise ValueError("management request body must be an object")
            result = self.server.manager.dispatch(action, body)
            self.wfile.write(_json_line({"ok": True, "result": result})); self.wfile.flush()
        except Exception as exc:
            try:
                self.wfile.write(_json_line(_error(exc))); self.wfile.flush()
            except Exception: pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immutavault-management-broker")
    parser.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    parser.add_argument("--env", default="/etc/immutavault/immutavault.env")
    parser.add_argument("--socket", default=os.getenv("IMMUTAVAULT_MANAGEMENT_BROKER_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--allowed-user", default=os.getenv("IMMUTAVAULT_MANAGEMENT_ALLOWED_USER", "immutavault"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_v11_config(args.config)
    socket_path = args.socket or cfg.management.broker_socket
    manager = ManagementManager(args.config, args.env)
    allowed_uid = pwd.getpwnam(args.allowed_user).pw_uid
    try: socket_gid = grp.getgrnam("immutavault").gr_gid
    except KeyError: socket_gid = None
    server = ManagementBrokerServer(socket_path, manager, allowed_uid=allowed_uid, socket_gid=socket_gid)
    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try: server.serve_forever(poll_interval=0.5)
    finally: server.server_close(); server.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
