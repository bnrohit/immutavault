from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import signal
import threading
from typing import Any

from .management_broker import ManagementBrokerServer, _parser
from .management_config import load_v11_config
from .management_service import ValidatedManagementManager
from .runner import run


class FinalValidatedManagementManager(ValidatedManagementManager):
    """Final v1.1 management service fixes and release-level invariants.

    The base v1.1 service deliberately keeps policy/config logic separate. This
    final layer fixes CIFS credential ordering for both temporary and persistent
    mounts so credentials exist before mount options are constructed.
    """

    def _test_mount(self, body: dict[str, Any]) -> dict[str, Any]:
        mount_type, source, _persistent, username, password = self._mount_spec(body)
        tag = "".join(ch if ch.isalnum() else "-" for ch in str(body.get("name") or "target"))[:48]
        target = Path("/run/immutavault") / f"mount-test-{tag}"
        target.mkdir(parents=True, exist_ok=True)
        cred: Path | None = None
        try:
            if mount_type == "cifs":
                cred = target.parent / f".{tag}.credentials"
                cred.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")
                os.chmod(cred, 0o600)
                options = self._mount_options(mount_type, str(cred))
            else:
                options = self._mount_options(mount_type)
            result = run(["mount", "-t", mount_type, "-o", options, source, str(target)], timeout=120, check=False)
            if result.returncode != 0:
                raise RuntimeError("NAS mount test failed: " + (result.stderr or result.stdout).strip())
            mounted = run(["mountpoint", "-q", str(target)], timeout=30, check=False).returncode == 0
            writable = os.access(target, os.W_OK)
            if not mounted or not writable:
                raise RuntimeError("NAS target mounted but is not writable by the appliance")
            return {"ok": True, "mounted": True, "writable": True, "type": mount_type, "source": source}
        finally:
            run(["umount", str(target)], timeout=60, check=False)
            if cred is not None:
                cred.unlink(missing_ok=True)
            try:
                target.rmdir()
            except OSError:
                pass

    def _ensure_persistent_mount(self, body: dict[str, Any]) -> dict[str, Any]:
        mount_type, source, target, username, password = self._mount_spec(body)
        target_path = Path(target)
        target_path.mkdir(parents=True, exist_ok=True)
        unit = run(["systemd-escape", "--path", "--suffix=mount", target], timeout=30).stdout.strip()
        if not unit.endswith(".mount"):
            raise RuntimeError("systemd-escape did not return a mount unit")

        cred_path: Path | None = None
        if mount_type == "cifs":
            safe = "".join(ch if ch.isalnum() else "-" for ch in str(body.get("name") or "share"))[:48]
            cred_path = Path("/etc/immutavault") / f"mount-{safe}.credentials"
            cred_path.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")
            os.chmod(cred_path, 0o600)
            options = self._mount_options(mount_type, str(cred_path))
        else:
            options = self._mount_options(mount_type)

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
        if run(["mountpoint", "-q", target], timeout=30, check=False).returncode != 0:
            raise RuntimeError("systemd reported success but NAS target is not mounted")
        return {
            "mounted": True,
            "unit": unit,
            "type": mount_type,
            "source": source,
            "path": target,
            "credentials_file": str(cred_path) if cred_path else None,
        }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_v11_config(args.config)
    socket_path = args.socket or cfg.management.broker_socket
    manager = FinalValidatedManagementManager(args.config, args.env)
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
