from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import signal
import tempfile
import threading
from typing import Any

import yaml

from .management_broker import ManagementBrokerServer, ManagementManager, _parser
from .management_config import load_v11_config
from .recovery_test import RecoveryTestTarget
from .runner import run
from .setup_wizard import _env_write


class ValidatedManagementManager(ManagementManager):
    """Root-side management policy implementation.

    The network portal never writes /etc, creates mounts, or manages systemd.
    Every persistent mutation is built as a complete candidate config and must
    pass the additive v1.1 schema (including enterprise tenant rules) before it
    replaces the live file.
    """

    def _validate_data(self, data: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix="immutavault-v11-", suffix=".yml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, sort_keys=False)
            load_v11_config(name)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _commit(self, data: dict[str, Any], env_updates: dict[str, str] | None = None) -> None:
        self._validate_data(data)
        previous = self.config_path.read_bytes() if self.config_path.exists() else None
        try:
            self._atomic_v11(data)
            if env_updates:
                _env_write(self.env_path, env_updates)
                self.setup._reload_env()
            load_v11_config(self.config_path)
        except Exception:
            if previous is not None:
                self.config_path.write_bytes(previous)
                os.chmod(self.config_path, 0o640)
            raise

    def save_platform(self, body: dict[str, Any]) -> dict[str, Any]:
        builder = getattr(self.setup, "_platform", None) or getattr(self.setup, "_platform_payload")
        platform, env = builder(body)
        data = self._data()
        previous = next((p for p in (data.get("platforms") or []) if p.get("name") == platform["name"]), None)
        # Editing credentials/endpoint must not silently expand a previously
        # checkbox-scoped platform back to every VM.
        if previous and previous.get("include") and previous.get("include") != ["*"]:
            platform["include"] = list(previous["include"])
            platform["exclude"] = list(previous.get("exclude") or [])
        data["platforms"] = [p for p in (data.get("platforms") or []) if p.get("name") != platform["name"]] + [platform]
        self._commit(data, env)
        return {"saved": platform["name"], "type": platform["type"], "credential_envs": sorted(k for k, v in env.items() if v)}

    def save_selection(self, platform: str, vms: list[str]) -> dict[str, Any]:
        selected = list(dict.fromkeys(str(vm).strip() for vm in vms if str(vm).strip()))
        if not selected:
            raise ValueError("select at least one VM")
        if any(any(ch in vm for ch in "*?[]") for vm in selected):
            raise ValueError("web protection selection requires exact VM names")
        data = self._data()
        found = False
        for item in data.get("platforms") or []:
            if item.get("name") == platform:
                item["include"], item["exclude"] = selected, []
                found = True
                break
        if not found:
            raise ValueError(f"unknown platform {platform}")
        self._commit(data)
        return {"platform": platform, "selected": len(selected), "vms": selected}

    @staticmethod
    def _safe_mount_value(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text or any(ch in text for ch in "\r\n\x00"):
            raise ValueError(f"{name} is required and cannot contain control characters")
        return text

    def _mount_spec(self, body: dict[str, Any]) -> tuple[str, str, str, str | None, str | None]:
        name = str(body.get("name") or "").strip()
        mount_type = str(body.get("mount_type") or "").strip().lower()
        source = self._safe_mount_value(body.get("mount_source"), "mount source")
        if mount_type not in {"nfs", "cifs"}:
            raise ValueError("direct NAS connection mount_type must be nfs or cifs")
        if mount_type == "nfs" and ":" not in source:
            raise ValueError("NFS source must look like server:/export")
        if mount_type == "cifs" and not source.startswith("//"):
            raise ValueError("SMB source must look like //server/share")
        root = Path("/srv/immutavault/storage").resolve()
        requested = str(body.get("path") or "").strip()
        path = Path(requested or (root / name)).resolve()
        if path == root or root not in path.parents:
            raise ValueError("wizard-managed NAS mounts must live below /srv/immutavault/storage")
        username = str(body.get("mount_username") or "").strip() or None
        password = str(body.get("mount_password") or "") or None
        if mount_type == "cifs" and (not username or password is None):
            raise ValueError("SMB username and password are required")
        return mount_type, source, str(path), username, password

    @staticmethod
    def _mount_options(mount_type: str, credentials: str | None = None) -> str:
        if mount_type == "nfs":
            return "rw,hard,noatime,nosuid,nodev,_netdev"
        if not credentials:
            raise ValueError("SMB credentials path is required")
        return f"rw,credentials={credentials},vers=3.1.1,nosuid,nodev,noexec,_netdev"

    def _test_mount(self, body: dict[str, Any]) -> dict[str, Any]:
        mount_type, source, _persistent, username, password = self._mount_spec(body)
        tag = "".join(ch if ch.isalnum() else "-" for ch in str(body.get("name") or "target"))[:48]
        target = Path("/run/immutavault") / f"mount-test-{tag}"
        target.mkdir(parents=True, exist_ok=True)
        cred: Path | None = None
        try:
            options = self._mount_options(mount_type)
            if mount_type == "cifs":
                cred = target.parent / f".{tag}.credentials"
                cred.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")
                os.chmod(cred, 0o600)
                options = self._mount_options(mount_type, str(cred))
            result = run(["mount", "-t", mount_type, "-o", options, source, str(target)], timeout=120, check=False)
            if result.returncode != 0:
                raise RuntimeError("NAS mount test failed: " + (result.stderr or result.stdout).strip())
            mounted = run(["mountpoint", "-q", str(target)], timeout=30, check=False).returncode == 0
            writable = os.access(target, os.W_OK)
            return {"ok": mounted and writable, "mounted": mounted, "writable": writable, "type": mount_type, "source": source}
        finally:
            run(["umount", str(target)], timeout=60, check=False)
            if cred:
                cred.unlink(missing_ok=True)
            try:
                target.rmdir()
            except OSError:
                pass

    def _ensure_persistent_mount(self, body: dict[str, Any]) -> dict[str, Any]:
        mount_type, source, target, username, password = self._mount_spec(body)
        target_path = Path(target)
        target_path.mkdir(parents=True, exist_ok=True)
        unit_result = run(["systemd-escape", "--path", "--suffix=mount", target], timeout=30)
        unit = unit_result.stdout.strip()
        if not unit.endswith(".mount"):
            raise RuntimeError("systemd-escape did not return a mount unit")
        cred_path: Path | None = None
        options = self._mount_options(mount_type)
        if mount_type == "cifs":
            safe = "".join(ch if ch.isalnum() else "-" for ch in str(body.get("name") or "share"))[:48]
            cred_path = Path("/etc/immutavault") / f"mount-{safe}.credentials"
            cred_path.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")
            os.chmod(cred_path, 0o600)
            options = self._mount_options(mount_type, str(cred_path))
        unit_path = Path("/etc/systemd/system") / unit
        content = (
            "[Unit]\nDescription=Immutavault managed backup storage mount\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Mount]\n"
            f"What={source}\nWhere={target}\nType={mount_type}\nOptions={options}\n"
            "TimeoutSec=120\n\n[Install]\nWantedBy=multi-user.target\n"
        )
        unit_path.write_text(content, encoding="utf-8")
        os.chmod(unit_path, 0o644)
        run(["systemctl", "daemon-reload"], timeout=60)
        result = run(["systemctl", "enable", "--now", unit], timeout=180, check=False)
        if result.returncode != 0:
            raise RuntimeError("could not activate NAS mount: " + (result.stderr or result.stdout).strip())
        check = run(["mountpoint", "-q", target], timeout=30, check=False)
        if check.returncode != 0:
            raise RuntimeError("systemd reported success but NAS target is not mounted")
        return {"mounted": True, "unit": unit, "type": mount_type, "source": source, "path": target, "credentials_file": str(cred_path) if cred_path else None}

    def test_storage(self, body: dict[str, Any]) -> dict[str, Any]:
        if str(body.get("backend") or "") == "filesystem" and str(body.get("mount_source") or "").strip():
            return self._test_mount(body)
        return self.setup.test_storage(body)

    def save_storage(self, body: dict[str, Any]) -> dict[str, Any]:
        builder = getattr(self.setup, "_replica")
        replica, env = builder(body)
        mount: dict[str, Any] | None = None
        if replica["backend"] == "filesystem" and str(body.get("mount_source") or "").strip():
            mount_type, source, target, _username, _password = self._mount_spec(body)
            replica["path"] = target
            replica["mount_required"] = True
            replica["mount_source"] = source
            replica["mount_type"] = mount_type
        data = self._data()
        data["replicas"] = [r for r in (data.get("replicas") or []) if r.get("name") != replica["name"]] + [replica]
        self._validate_data(data)
        if replica["backend"] == "filesystem" and str(body.get("mount_source") or "").strip():
            mount = self._ensure_persistent_mount(body)
        self._commit(data, env)
        return {"saved": replica["name"], "backend": replica["backend"], "mount": mount}

    def _sync_timer(self, policy_id: str) -> dict[str, Any]:
        result = super()._sync_timer(policy_id)
        if result.get("scheduled"):
            # Named scheduled policies supersede the legacy broad timer; running
            # both would create duplicate backups and surprise operators.
            legacy = run(["systemctl", "disable", "--now", "immutavault-backup.timer"], timeout=60, check=False)
            result["legacy_backup_timer_disabled"] = legacy.returncode == 0
        return result

    def save_dr_test_network(self, platform: str, network: str) -> dict[str, Any]:
        cfg = load_v11_config(self.config_path)
        target = next((p for p in cfg.platforms if p.name == platform and p.enabled), None)
        if not target:
            raise ValueError("unknown or disabled target platform")
        check = RecoveryTestTarget(cfg, target).validate_network(network)
        result = super().save_dr_test_network(platform, network)
        result["preflight"] = check
        return result

    def dispatch(self, action: str, body: dict[str, Any]) -> Any:
        if action == "platform_save": return self.save_platform(body)
        if action == "protection_save": return self.save_selection(str(body.get("platform") or ""), list(body.get("vms") or []))
        if action == "storage_test": return self.test_storage(body)
        if action == "storage_save": return self.save_storage(body)
        if action == "dr_test_network_save": return self.save_dr_test_network(str(body.get("platform") or ""), str(body.get("network") or ""))
        return super().dispatch(action, body)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_v11_config(args.config)
    socket_path = args.socket or cfg.management.broker_socket
    manager = ValidatedManagementManager(args.config, args.env)
    allowed_uid = pwd.getpwnam(args.allowed_user).pw_uid
    try:
        socket_gid = grp.getgrnam("immutavault").gr_gid
    except KeyError:
        socket_gid = None
    server = ManagementBrokerServer(socket_path, manager, allowed_uid=allowed_uid, socket_gid=socket_gid)

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
