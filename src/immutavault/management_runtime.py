from __future__ import annotations

import grp
import os
import pwd
import signal
import threading
import time
from typing import Any

from .management_broker import ManagementBrokerServer, _parser
from .management_config import load_v11_config
from .management_service import ValidatedManagementManager
from .runner import run


class RuntimeManagementManager(ValidatedManagementManager):
    """Validated management service plus delayed portal refresh.

    Config-changing HTTP requests must finish before the network portal restarts.
    A transient systemd job therefore performs a try-restart shortly after the
    broker has already returned the successful commit to the browser.
    """

    @staticmethod
    def _schedule_portal_refresh() -> bool:
        if not os.path.isdir("/run/systemd/system"):
            return False
        unit = f"immutavault-portal-refresh-{os.getpid()}-{time.time_ns()}"
        result = run([
            "systemd-run", "--unit", unit, "--collect", "--on-active=2s",
            "/bin/systemctl", "try-restart", "immutavault-portal.service",
        ], timeout=30, check=False)
        return result.returncode == 0

    def _commit(self, data: dict[str, Any], env_updates: dict[str, str] | None = None) -> None:
        super()._commit(data, env_updates)
        self._schedule_portal_refresh()

    def save_policy(self, body: dict[str, Any]) -> dict[str, Any]:
        result = super().save_policy(body)
        result["portal_refresh_scheduled"] = self._schedule_portal_refresh()
        return result

    def delete_policy(self, policy_id: str) -> dict[str, Any]:
        result = super().delete_policy(policy_id)
        result["portal_refresh_scheduled"] = self._schedule_portal_refresh()
        return result

    def save_dr_test_network(self, platform: str, network: str) -> dict[str, Any]:
        result = super().save_dr_test_network(platform, network)
        # The inherited method writes through _atomic_v11 rather than _commit.
        result["portal_refresh_scheduled"] = self._schedule_portal_refresh()
        return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_v11_config(args.config)
    socket_path = args.socket or cfg.management.broker_socket
    manager = RuntimeManagementManager(args.config, args.env)
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
