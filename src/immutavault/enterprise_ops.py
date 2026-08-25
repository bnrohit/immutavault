from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import socketserver
import ssl
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .enterprise_auth import Identity, SignedToken
from .enterprise_config import EnterpriseConfig
from .state import StateDB
from .util import safe_component


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _prom_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**values: Any) -> str:
    return "{" + ",".join(f'{key}="{_prom_label(value)}"' for key, value in values.items()) + "}"


@dataclass(frozen=True)
class OperationsSnapshot:
    jobs: list[dict[str, Any]]
    summary: dict[str, Any]


class EnterpriseOps:
    def __init__(self, cfg: EnterpriseConfig, state: StateDB) -> None:
        self.cfg = cfg
        self.state = state
        self._progress_cache: dict[int, tuple[float, int, int]] = {}

    def tenant_for_platform(self, platform: str) -> str:
        return self.cfg.tenant_for_platform(platform)

    @staticmethod
    def _allowed(tenant: str, scopes: tuple[str, ...] | list[str] | set[str]) -> bool:
        return "*" in scopes or tenant in scopes

    def _estimate_running_progress(self, row: dict[str, Any]) -> tuple[int, int, str]:
        """Estimate export progress without reading guest data.

        The exporter doesn't expose a common byte-progress API across VMware,
        Proxmox and XCP-ng. v0.9 therefore estimates the active export from file
        metadata and the previous protected size, then reports 100 only after the
        authoritative jobs table records completion. The UI labels this estimate.
        """
        job_id = int(row["id"])
        cached = self._progress_cache.get(job_id)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < max(1, self.cfg.observability.websocket_poll_seconds):
            return cached[1], cached[2], "exporting"

        root = (
            Path(self.cfg.repository.staging_path)
            / safe_component(str(row["platform"]))
            / safe_component(str(row["vm_name"]))
        )
        current_bytes = 0
        latest_dir: Path | None = None
        try:
            dirs = [item for item in root.iterdir() if item.is_dir()]
            latest_dir = max(dirs, key=lambda item: item.stat().st_mtime) if dirs else None
        except OSError:
            latest_dir = None
        if latest_dir is not None:
            seen = 0
            try:
                for base, _dirs, files in os.walk(latest_dir):
                    for name in files:
                        seen += 1
                        if seen > 50000:
                            break
                        try:
                            current_bytes += (Path(base) / name).stat().st_size
                        except OSError:
                            continue
                    if seen > 50000:
                        break
            except OSError:
                current_bytes = 0

        expected = 0
        with self.state.connect() as conn:
            prev = conn.execute(
                "SELECT backup_bytes FROM recovery_points WHERE platform=? AND vm_id=? AND expired=0 ORDER BY created_at DESC LIMIT 1",
                (row["platform"], row["vm_id"]),
            ).fetchone()
            if prev:
                expected = int(prev["backup_bytes"] or 0)

        if expected > 0 and current_bytes > 0:
            progress = min(85, max(8, 8 + int(min(1.0, current_bytes / expected) * 77)))
        elif current_bytes > 0:
            progress = 25
        else:
            progress = 5
        self._progress_cache[job_id] = (now_mono, progress, current_bytes)
        return progress, current_bytes, "exporting"

    def jobs(self, tenant_scopes: tuple[str, ...] | list[str] | set[str], *, limit: int = 100) -> list[dict[str, Any]]:
        with self.state.connect() as conn:
            rows = conn.execute(
                "SELECT id,started_at,finished_at,platform,vm_id,vm_name,status,snapshot_id,error "
                "FROM jobs ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        now = _utcnow()
        for raw in rows:
            row = dict(raw)
            tenant = self.tenant_for_platform(str(row["platform"]))
            if not self._allowed(tenant, tenant_scopes):
                continue
            started = _parse_time(row.get("started_at"))
            finished = _parse_time(row.get("finished_at"))
            if row["status"] == "running":
                progress, bytes_seen, stage = self._estimate_running_progress(row)
            elif row["status"] == "success":
                progress, bytes_seen, stage = 100, None, "complete"
            else:
                progress, bytes_seen, stage = 100, None, "failed"
            row.update({
                "tenant": tenant,
                "tenant_name": self.cfg.tenant_name(tenant),
                "progress_percent": progress,
                "progress_estimated": row["status"] == "running",
                "stage": stage,
                "staging_bytes_seen": bytes_seen,
                "elapsed_seconds": int(((finished or now) - started).total_seconds()) if started else None,
            })
            out.append(row)
        return out

    def summary(self, tenant_scopes: tuple[str, ...] | list[str] | set[str]) -> dict[str, Any]:
        platforms = {
            p.name: self.tenant_for_platform(p.name)
            for p in self.cfg.platforms
            if p.enabled and self._allowed(self.tenant_for_platform(p.name), tenant_scopes)
        }
        if not platforms:
            return {"tenants": [], "platforms": 0, "recovery_points": 0, "verified": 0, "suspicious": 0, "running_jobs": 0}
        placeholders = ",".join("?" for _ in platforms)
        values = list(platforms)
        with self.state.connect() as conn:
            point = conn.execute(
                f"SELECT COUNT(*) total,SUM(verified) verified,SUM(suspicious) suspicious FROM recovery_points "
                f"WHERE expired=0 AND platform IN ({placeholders})",
                values,
            ).fetchone()
            running = conn.execute(
                f"SELECT COUNT(*) count FROM jobs WHERE status='running' AND platform IN ({placeholders})",
                values,
            ).fetchone()
            pending = conn.execute(
                f"SELECT COUNT(*) count FROM restore_requests WHERE status IN ('pending_approval','approved','ready','running') "
                f"AND source_platform IN ({placeholders})",
                values,
            ).fetchone()
        tenant_ids = sorted(set(platforms.values()))
        return {
            "tenants": tenant_ids,
            "platforms": len(platforms),
            "recovery_points": int(point["total"] or 0),
            "verified": int(point["verified"] or 0),
            "suspicious": int(point["suspicious"] or 0),
            "running_jobs": int(running["count"] or 0),
            "active_restore_requests": int(pending["count"] or 0),
        }

    def snapshot(self, identity: Identity) -> OperationsSnapshot:
        return OperationsSnapshot(
            jobs=self.jobs(identity.tenants, limit=100),
            summary=self.summary(identity.tenants),
        )

    def render_prometheus(self) -> str:
        lines = [
            "# HELP immutavault_build_info Immutavault build information.",
            "# TYPE immutavault_build_info gauge",
            f'immutavault_build_info{{version="{_prom_label(__version__)}"}} 1',
        ]
        audit_ok, _ = self.state.verify_audit_chain()
        lines += [
            "# HELP immutavault_audit_chain_valid Whether the tamper-evident audit chain validates.",
            "# TYPE immutavault_audit_chain_valid gauge",
            f"immutavault_audit_chain_valid {1 if audit_ok else 0}",
        ]

        with self.state.connect() as conn:
            point_rows = conn.execute(
                "SELECT platform,COUNT(*) total,SUM(verified) verified,SUM(suspicious) suspicious "
                "FROM recovery_points WHERE expired=0 GROUP BY platform"
            ).fetchall()
            job_rows = conn.execute(
                "SELECT platform,status,COUNT(*) count FROM jobs GROUP BY platform,status"
            ).fetchall()
            restore_rows = conn.execute(
                "SELECT source_platform,status,COUNT(*) count FROM restore_requests GROUP BY source_platform,status"
            ).fetchall()
            last_success_rows = conn.execute(
                "SELECT platform,MAX(finished_at) finished_at FROM jobs WHERE status='success' GROUP BY platform"
            ).fetchall()
            copy_rows = conn.execute(
                "SELECT p.platform,c.target_name,c.status,COUNT(*) count,SUM(c.verified) verified "
                "FROM recovery_copies c JOIN recovery_points p ON p.snapshot_id=c.snapshot_id "
                "WHERE p.expired=0 GROUP BY p.platform,c.target_name,c.status"
            ).fetchall()
            dr_rows = conn.execute(
                "SELECT direction,status,COUNT(*) count FROM dr_runs GROUP BY direction,status"
            ).fetchall()

        lines += [
            "# HELP immutavault_recovery_points Current non-expired recovery points.",
            "# TYPE immutavault_recovery_points gauge",
            "# HELP immutavault_recovery_points_verified Current verified recovery points.",
            "# TYPE immutavault_recovery_points_verified gauge",
            "# HELP immutavault_recovery_points_suspicious Current suspicious recovery points.",
            "# TYPE immutavault_recovery_points_suspicious gauge",
        ]
        for row in point_rows:
            platform = str(row["platform"]); tenant = self.tenant_for_platform(platform)
            label = _labels(tenant=tenant, platform=platform) if self.cfg.observability.include_platform_labels else _labels(tenant=tenant)
            lines.append(f"immutavault_recovery_points{label} {int(row['total'] or 0)}")
            lines.append(f"immutavault_recovery_points_verified{label} {int(row['verified'] or 0)}")
            lines.append(f"immutavault_recovery_points_suspicious{label} {int(row['suspicious'] or 0)}")

        lines += [
            "# HELP immutavault_backup_jobs_total Backup jobs by result.",
            "# TYPE immutavault_backup_jobs_total counter",
        ]
        for row in job_rows:
            platform = str(row["platform"]); tenant = self.tenant_for_platform(platform)
            labels = {"tenant": tenant, "status": row["status"]}
            if self.cfg.observability.include_platform_labels: labels["platform"] = platform
            lines.append(f"immutavault_backup_jobs_total{_labels(**labels)} {int(row['count'])}")

        lines += [
            "# HELP immutavault_restore_requests Restore requests by current state.",
            "# TYPE immutavault_restore_requests gauge",
        ]
        for row in restore_rows:
            platform = str(row["source_platform"]); tenant = self.tenant_for_platform(platform)
            labels = {"tenant": tenant, "status": row["status"]}
            if self.cfg.observability.include_platform_labels: labels["platform"] = platform
            lines.append(f"immutavault_restore_requests{_labels(**labels)} {int(row['count'])}")

        now = _utcnow()
        latest = {str(row["platform"]): _parse_time(row["finished_at"]) for row in last_success_rows}
        lines += [
            "# HELP immutavault_last_successful_backup_age_seconds Age of the newest successful backup for a platform.",
            "# TYPE immutavault_last_successful_backup_age_seconds gauge",
            "# HELP immutavault_rpo_compliant Whether the latest successful platform backup is within the configured RPO target.",
            "# TYPE immutavault_rpo_compliant gauge",
        ]
        for platform_cfg in self.cfg.platforms:
            if not platform_cfg.enabled:
                continue
            platform = platform_cfg.name; tenant = self.tenant_for_platform(platform)
            stamp = latest.get(platform)
            age = int((now - stamp).total_seconds()) if stamp else -1
            compliant = stamp is not None and age <= self.cfg.protection.rpo_target_minutes * 60
            labels = {"tenant": tenant}
            if self.cfg.observability.include_platform_labels: labels["platform"] = platform
            label = _labels(**labels)
            lines.append(f"immutavault_last_successful_backup_age_seconds{label} {age}")
            lines.append(f"immutavault_rpo_compliant{label} {1 if compliant else 0}")

        lines += [
            "# HELP immutavault_recovery_copies Recovery-point copies by target and status.",
            "# TYPE immutavault_recovery_copies gauge",
            "# HELP immutavault_recovery_copies_verified Verified recovery-point copies.",
            "# TYPE immutavault_recovery_copies_verified gauge",
        ]
        for row in copy_rows:
            platform = str(row["platform"]); tenant = self.tenant_for_platform(platform)
            labels = {"tenant": tenant, "target": row["target_name"], "status": row["status"]}
            if self.cfg.observability.include_platform_labels: labels["platform"] = platform
            label = _labels(**labels)
            lines.append(f"immutavault_recovery_copies{label} {int(row['count'])}")
            lines.append(f"immutavault_recovery_copies_verified{label} {int(row['verified'] or 0)}")

        lines += [
            "# HELP immutavault_dr_runs_total Disaster-recovery orchestration runs.",
            "# TYPE immutavault_dr_runs_total counter",
        ]
        for row in dr_rows:
            lines.append(
                f"immutavault_dr_runs_total{_labels(direction=row['direction'], status=row['status'])} {int(row['count'])}"
            )
        return "\n".join(lines) + "\n"

    def issue_ws_ticket(self, identity: Identity, signer: SignedToken) -> str:
        now = int(time.time())
        return signer.encode({
            "typ": "ws",
            "iat": now,
            "exp": now + self.cfg.observability.websocket_ticket_ttl_seconds,
            "sub": identity.subject,
            "name": identity.name,
            "role": identity.role,
            "tenants": list(identity.tenants),
            "source": identity.source,
            "mfa": identity.mfa,
            "tenant_id": identity.tenant_id,
        })

    @staticmethod
    def identity_from_ws_ticket(ticket: str, signer: SignedToken) -> Identity:
        value = signer.decode(ticket, expected_type="ws")
        tenants = tuple(str(item) for item in value.get("tenants") or [])
        if not tenants:
            raise PermissionError("WebSocket ticket has no tenant scope")
        return Identity(
            subject=str(value.get("sub") or ""),
            name=str(value.get("name") or value.get("sub") or "operator"),
            role=str(value.get("role") or "viewer"),
            tenants=tenants,
            source=str(value.get("source") or "session"),
            mfa=bool(value.get("mfa", False)),
            tenant_id=str(value.get("tenant_id")) if value.get("tenant_id") else None,
        )


class _TelemetryTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class WebSocketTelemetryServer:
    """Minimal RFC 6455 server for one-way scoped operations telemetry.

    It intentionally supports only the browser features Immutavault needs: a
    standards-compliant HTTP upgrade followed by server-to-client text frames.
    Authentication happens through a short-lived HMAC ticket. No external WS
    runtime is required by the appliance.
    """

    def __init__(self, cfg: EnterpriseConfig, ops: EnterpriseOps, signer: SignedToken) -> None:
        self.cfg = cfg
        self.ops = ops
        self.signer = signer
        self.server: _TelemetryTCPServer | None = None
        self.thread: threading.Thread | None = None

    @staticmethod
    def _send_http(sock, code: int, reason: str, body: str = "") -> None:
        payload = body.encode()
        response = (
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(payload)}\r\n"
            "Connection: close\r\nCache-Control: no-store\r\n\r\n"
        ).encode() + payload
        sock.sendall(response)

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        size = len(payload)
        if size < 126:
            header = bytes([0x81, size])
        elif size <= 0xFFFF:
            header = bytes([0x81, 126]) + size.to_bytes(2, "big")
        else:
            header = bytes([0x81, 127]) + size.to_bytes(8, "big")
        return header + payload

    def _handler_class(self):
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(15)
                stream = self.request.makefile("rb")
                try:
                    first = stream.readline(8192).decode("iso-8859-1").strip()
                    parts = first.split()
                    if len(parts) != 3 or parts[0] != "GET":
                        outer._send_http(self.request, 400, "Bad Request"); return
                    headers: dict[str, str] = {}
                    total = len(first)
                    while True:
                        line = stream.readline(8192)
                        total += len(line)
                        if total > 65536:
                            outer._send_http(self.request, 431, "Request Header Fields Too Large"); return
                        if line in {b"\r\n", b"\n", b""}:
                            break
                        decoded = line.decode("iso-8859-1").strip()
                        if ":" in decoded:
                            key, value = decoded.split(":", 1)
                            headers[key.lower().strip()] = value.strip()
                    parsed = urlparse(parts[1])
                    if parsed.path != "/events":
                        outer._send_http(self.request, 404, "Not Found"); return
                    if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in headers.get("connection", "").lower():
                        outer._send_http(self.request, 426, "Upgrade Required"); return
                    if headers.get("sec-websocket-version") != "13":
                        outer._send_http(self.request, 426, "Upgrade Required"); return
                    origin = headers.get("origin")
                    allowed_origins = outer.cfg.observability.websocket_allowed_origins
                    if allowed_origins and origin not in allowed_origins:
                        outer._send_http(self.request, 403, "Forbidden", "WebSocket origin is not allowed"); return
                    ticket = (parse_qs(parsed.query).get("ticket") or [""])[0]
                    try:
                        identity = outer.ops.identity_from_ws_ticket(ticket, outer.signer)
                    except Exception:
                        outer._send_http(self.request, 401, "Unauthorized", "invalid or expired WebSocket ticket"); return
                    key = headers.get("sec-websocket-key", "")
                    try:
                        decoded_key = base64.b64decode(key, validate=True)
                    except Exception:
                        decoded_key = b""
                    if len(decoded_key) != 16:
                        outer._send_http(self.request, 400, "Bad Request"); return
                    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # nosec - RFC6455 requires SHA-1 here
                    response = (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n"
                        "Cache-Control: no-store\r\n\r\n"
                    )
                    self.request.sendall(response.encode())
                    self.request.settimeout(None)
                    while True:
                        snap = outer.ops.snapshot(identity)
                        payload = json.dumps({
                            "type": "operations",
                            "generated_at": _utcnow().isoformat(),
                            "identity": {"role": identity.role, "tenants": list(identity.tenants)},
                            "summary": snap.summary,
                            "jobs": snap.jobs,
                        }, default=str, separators=(",", ":")).encode()
                        self.request.sendall(outer._frame(payload))
                        time.sleep(outer.cfg.observability.websocket_poll_seconds)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    return
                finally:
                    try:
                        stream.close()
                    except OSError:
                        pass

        return Handler

    def start(self, *, tls_cert: str | None = None, tls_key: str | None = None) -> None:
        if not self.cfg.observability.websocket_enabled:
            return
        server = _TelemetryTCPServer(
            (self.cfg.observability.websocket_listen, self.cfg.observability.websocket_port),
            self._handler_class(),
        )
        if tls_cert and tls_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(tls_cert, tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="immutavault-websocket", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
