from __future__ import annotations

import shutil
import socket
import ssl
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .dr_config import HealthCheckConfig
from .runner import run


def run_health_check(check: HealthCheckConfig) -> dict:
    try:
        if check.type == "icmp":
            if not check.host:
                raise ValueError("icmp health check requires host")
            if shutil.which("ping") is None:
                raise RuntimeError("ping command is not installed")
            result = run(["ping", "-c", "1", "-W", str(check.timeout_seconds), check.host], timeout=check.timeout_seconds + 2, check=False)
            return {"ok": result.returncode == 0, "type": check.type, "target": check.host, "detail": result.stderr.strip() or result.stdout.strip()[-200:]}
        if check.type == "tcp":
            if not check.host or not check.port:
                raise ValueError("tcp health check requires host and port")
            with socket.create_connection((check.host, check.port), timeout=check.timeout_seconds):
                pass
            return {"ok": True, "type": check.type, "target": f"{check.host}:{check.port}"}
        if check.type in {"http", "https"}:
            url = check.url or f"{check.type}://{check.host}:{check.port or (443 if check.type == 'https' else 80)}/"
            req = Request(url, method="GET", headers={"User-Agent": "Immutavault-DR/0.5"})
            ctx = ssl.create_default_context() if check.type == "https" else None
            with urlopen(req, timeout=check.timeout_seconds, context=ctx) as resp:
                code = int(resp.status)
            return {"ok": code == check.expected_status, "type": check.type, "target": url, "status": code}
        raise ValueError(f"unsupported health check: {check.type}")
    except (OSError, RuntimeError, ValueError, URLError, HTTPError) as exc:
        return {"ok": False, "type": check.type, "target": check.url or check.host, "error": str(exc)}


def run_health_checks(checks: list[HealthCheckConfig]) -> dict:
    results = [run_health_check(x) for x in checks]
    return {"ok": all(x.get("ok") for x in results) if results else True, "checks": results}
