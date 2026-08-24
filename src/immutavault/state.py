from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterator, Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  platform TEXT NOT NULL,
  vm_id TEXT NOT NULL,
  vm_name TEXT NOT NULL,
  status TEXT NOT NULL,
  snapshot_id TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_vm ON jobs(platform, vm_id, started_at);

CREATE TABLE IF NOT EXISTS recovery_points (
  snapshot_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  platform TEXT NOT NULL,
  platform_type TEXT NOT NULL,
  vm_id TEXT NOT NULL,
  vm_name TEXT NOT NULL,
  power_state TEXT,
  source_path TEXT NOT NULL,
  backup_bytes INTEGER NOT NULL DEFAULT 0,
  data_added INTEGER NOT NULL DEFAULT 0,
  file_count INTEGER NOT NULL DEFAULT 0,
  manifest_sha256 TEXT,
  immutable_until TEXT NOT NULL,
  suspicious INTEGER NOT NULL DEFAULT 0,
  suspicious_reason TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_points_vm ON recovery_points(platform, vm_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_points_created ON recovery_points(created_at DESC);

CREATE TABLE IF NOT EXISTS recovery_copies (
  snapshot_id TEXT NOT NULL,
  target_name TEXT NOT NULL,
  backend TEXT NOT NULL,
  provider TEXT,
  repository TEXT NOT NULL,
  status TEXT NOT NULL,
  copied_at TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  verified_at TEXT,
  immutable_until TEXT,
  object_lock_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  PRIMARY KEY(snapshot_id,target_name),
  FOREIGN KEY(snapshot_id) REFERENCES recovery_points(snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_copies_target ON recovery_copies(target_name,status,copied_at DESC);

CREATE TABLE IF NOT EXISTS restore_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  requester TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  source_platform TEXT NOT NULL,
  vm_id TEXT NOT NULL,
  vm_name TEXT NOT NULL,
  target_platform TEXT NOT NULL,
  target_name TEXT NOT NULL,
  options_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  result_json TEXT,
  error TEXT,
  FOREIGN KEY(snapshot_id) REFERENCES recovery_points(snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_restore_status ON restore_requests(status, created_at DESC);

CREATE TABLE IF NOT EXISTS dr_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  direction TEXT NOT NULL,
  source_site TEXT NOT NULL,
  target_site TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_dr_runs ON dr_runs(direction,status,started_at DESC);

CREATE TABLE IF NOT EXISTS dr_workloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  workload_name TEXT NOT NULL,
  snapshot_id TEXT,
  source_platform TEXT NOT NULL,
  target_platform TEXT NOT NULL,
  target_name TEXT,
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  FOREIGN KEY(run_id) REFERENCES dr_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_dr_workloads ON dr_workloads(run_id,status,id);

CREATE TABLE IF NOT EXISTS dr_health (
  name TEXT PRIMARY KEY,
  checked_at TEXT NOT NULL,
  healthy INTEGER NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT,
  event_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateDB:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(recovery_points)").fetchall()}
            if "expired" not in cols:
                conn.execute("ALTER TABLE recovery_points ADD COLUMN expired INTEGER NOT NULL DEFAULT 0")
            if "expired_at" not in cols:
                conn.execute("ALTER TABLE recovery_points ADD COLUMN expired_at TEXT")
            audit_cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "prev_hash" not in audit_cols:
                conn.execute("ALTER TABLE audit_events ADD COLUMN prev_hash TEXT")
            if "event_hash" not in audit_cols:
                conn.execute("ALTER TABLE audit_events ADD COLUMN event_hash TEXT")
            self._backfill_audit_hashes(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _audit_hash(created_at: str, actor: str, action: str, object_type: str, object_id: str, details_json: str, prev_hash: str) -> str:
        payload = json.dumps({
            "created_at": created_at, "actor": actor, "action": action,
            "object_type": object_type, "object_id": object_id,
            "details_json": details_json, "prev_hash": prev_hash,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _backfill_audit_hashes(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
        prev_hash = "GENESIS"
        for row in rows:
            expected = self._audit_hash(
                row["created_at"], row["actor"], row["action"], row["object_type"],
                row["object_id"], row["details_json"], prev_hash,
            )
            if not row["event_hash"]:
                conn.execute(
                    "UPDATE audit_events SET prev_hash=?, event_hash=? WHERE id=?",
                    (prev_hash, expected, row["id"]),
                )
                event_hash = expected
            else:
                # Preserve existing hashes. verify_audit_chain() will surface tampering.
                event_hash = str(row["event_hash"])
            prev_hash = event_hash

    def start_job(self, platform: str, vm_id: str, vm_name: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(started_at,platform,vm_id,vm_name,status) VALUES(?,?,?,?,?)",
                (utcnow(), platform, vm_id, vm_name, "running"),
            )
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, *, status: str, snapshot_id: str | None = None, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET finished_at=?, status=?, snapshot_id=?, error=? WHERE id=?",
                (utcnow(), status, snapshot_id, error, job_id),
            )

    def upsert_recovery_point(self, point: dict[str, Any]) -> None:
        columns = [
            "snapshot_id", "created_at", "platform", "platform_type", "vm_id", "vm_name", "power_state",
            "source_path", "backup_bytes", "data_added", "file_count", "manifest_sha256", "immutable_until",
            "suspicious", "suspicious_reason", "verified", "verified_at", "metadata_json",
        ]
        values = [point.get(k) for k in columns]
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO recovery_points({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                values,
            )

    def upsert_recovery_copy(
        self, *, snapshot_id: str, target_name: str, backend: str, repository: str,
        provider: str | None = None, status: str = "success", immutable_until: str | None = None,
        object_lock: dict[str, Any] | None = None, error: str | None = None, verified: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO recovery_copies(
                    snapshot_id,target_name,backend,provider,repository,status,copied_at,verified,verified_at,
                    immutable_until,object_lock_json,error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_id,target_name) DO UPDATE SET
                    backend=excluded.backend,provider=excluded.provider,repository=excluded.repository,status=excluded.status,
                    copied_at=excluded.copied_at,verified=excluded.verified,verified_at=excluded.verified_at,
                    immutable_until=excluded.immutable_until,object_lock_json=excluded.object_lock_json,error=excluded.error
                """,
                (snapshot_id,target_name,backend,provider,repository,status,utcnow() if status == "success" else None,
                 1 if verified else 0, utcnow() if verified else None, immutable_until,
                 json.dumps(object_lock or {}, sort_keys=True), error),
            )

    def list_recovery_copies(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recovery_copies WHERE snapshot_id=? ORDER BY target_name", (snapshot_id,)
            ).fetchall()
            out = [dict(r) for r in rows]
        for item in out:
            try:
                item["object_lock"] = json.loads(item.pop("object_lock_json") or "{}")
            except json.JSONDecodeError:
                item["object_lock"] = {}
        return out

    def get_recovery_copy(self, snapshot_id: str, target_name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recovery_copies WHERE snapshot_id=? AND target_name=?", (snapshot_id,target_name)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
        try:
            item["object_lock"] = json.loads(item.pop("object_lock_json") or "{}")
        except json.JSONDecodeError:
            item["object_lock"] = {}
        return item

    def mark_copy_verified(self, snapshot_id: str, target_name: str, verified: bool, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE recovery_copies SET verified=?,verified_at=?,error=? WHERE snapshot_id=? AND target_name=?",
                (1 if verified else 0, utcnow() if verified else None, error, snapshot_id, target_name),
            )

    def latest_point(self, platform: str, vm_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recovery_points WHERE platform=? AND vm_id=? AND expired=0 ORDER BY created_at DESC LIMIT 1",
                (platform, vm_id),
            ).fetchone()
            return dict(row) if row else None

    def protected_snapshot_ids(self, *, at: str | None = None) -> set[str]:
        at = at or utcnow()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_id FROM recovery_points WHERE immutable_until > ? AND expired=0", (at,)
            ).fetchall()
            return {str(r["snapshot_id"]) for r in rows}

    def get_point(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM recovery_points WHERE snapshot_id=? AND expired=0", (snapshot_id,)).fetchone()
            return dict(row) if row else None

    def list_points(self, *, platform: str | None = None, vm_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        if vm_id:
            clauses.append("vm_id=?")
            params.append(vm_id)
        clauses.append("expired=0")
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM recovery_points{where} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
            return [dict(r) for r in rows]

    def list_vms(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.platform,p.platform_type,p.vm_id,p.vm_name,
                       MAX(p.created_at) AS latest_point,
                       COUNT(*) AS restore_points,
                       MAX(p.verified) AS has_verified_point,
                       MAX(p.suspicious) AS has_suspicious_point
                FROM recovery_points p
                WHERE p.expired=0
                GROUP BY p.platform,p.platform_type,p.vm_id,p.vm_name
                ORDER BY p.platform,p.vm_name
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def extend_immutability(self, snapshot_id: str, immutable_until: str, *, actor: str, reason: str = "manual hold") -> str:
        # The API can only extend a point. Shortening immutability is intentionally unsupported.
        with self.connect() as conn:
            row = conn.execute(
                "SELECT immutable_until FROM recovery_points WHERE snapshot_id=? AND expired=0", (snapshot_id,)
            ).fetchone()
            if not row:
                raise ValueError("unknown recovery point")
            current = str(row["immutable_until"])
            new_until = max(current, immutable_until)
            conn.execute(
                "UPDATE recovery_points SET immutable_until=? WHERE snapshot_id=?",
                (new_until, snapshot_id),
            )
        self.audit(actor, "recovery.hold", "recovery_point", snapshot_id, {"immutable_until": new_until, "reason": reason})
        return new_until

    def mark_points_expired(self, snapshot_ids: list[str]) -> None:
        if not snapshot_ids:
            return
        now = utcnow()
        with self.connect() as conn:
            conn.executemany(
                "UPDATE recovery_points SET expired=1,expired_at=? WHERE snapshot_id=?",
                [(now, sid) for sid in snapshot_ids],
            )

    def mark_verified(self, snapshot_id: str, verified: bool, details: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM recovery_points WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if not row:
                raise ValueError("unknown recovery point")
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            metadata["verification"] = details or {}
            conn.execute(
                "UPDATE recovery_points SET verified=?, verified_at=?, metadata_json=? WHERE snapshot_id=?",
                (1 if verified else 0, utcnow() if verified else None, json.dumps(metadata), snapshot_id),
            )


    def status_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            jobs = conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
            points = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN verified=1 THEN 1 ELSE 0 END) AS verified,
                       SUM(CASE WHEN suspicious=1 THEN 1 ELSE 0 END) AS suspicious
                FROM recovery_points WHERE expired=0
                """
            ).fetchone()
            restores = conn.execute(
                "SELECT status, COUNT(*) AS count FROM restore_requests GROUP BY status"
            ).fetchall()
            dr_runs = conn.execute(
                "SELECT status, COUNT(*) AS count FROM dr_runs GROUP BY status"
            ).fetchall()
            last_dr = conn.execute(
                "SELECT id,direction,source_site,target_site,status,started_at,finished_at,error FROM dr_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            last_success = conn.execute(
                "SELECT finished_at,platform,vm_name,snapshot_id FROM jobs WHERE status='success' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            last_failure = conn.execute(
                "SELECT finished_at,platform,vm_name,error FROM jobs WHERE status='failed' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return {
            "jobs": {str(r["status"]): int(r["count"]) for r in jobs},
            "recovery_points": {
                "total": int(points["total"] or 0),
                "verified": int(points["verified"] or 0),
                "suspicious": int(points["suspicious"] or 0),
            },
            "restore_requests": {str(r["status"]): int(r["count"]) for r in restores},
            "dr_runs": {str(r["status"]): int(r["count"]) for r in dr_runs},
            "last_dr_run": dict(last_dr) if last_dr else None,
            "last_successful_backup": dict(last_success) if last_success else None,
            "last_failed_backup": dict(last_failure) if last_failure else None,
        }

    def backup_database(self, destination_dir: str, keep: int = 288) -> str:
        if keep < 1:
            raise ValueError("state backup keep must be >= 1")
        dest_dir = Path(destination_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = dest_dir / f"state-{stamp}.db"
        with sqlite3.connect(self.path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        destination.chmod(0o600)
        backups = sorted(dest_dir.glob("state-*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            old.unlink(missing_ok=True)
        return str(destination)

    def create_restore_request(
        self, *, requester: str, point: dict[str, Any], target_platform: str, target_name: str,
        options: dict[str, Any], status: str,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO restore_requests(
                    created_at,requester,snapshot_id,source_platform,vm_id,vm_name,target_platform,target_name,options_json,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (utcnow(), requester, point["snapshot_id"], point["platform"], point["vm_id"], point["vm_name"],
                 target_platform, target_name, json.dumps(options), status),
            )
            rid = int(cur.lastrowid)
        self.audit(requester, "restore.request", "restore_request", str(rid), {"snapshot_id": point["snapshot_id"]})
        return rid

    def get_restore_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM restore_requests WHERE id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def list_restore_requests(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM restore_requests ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def approve_restore(self, request_id: int, approver: str, *, four_eyes: bool = True) -> None:
        req = self.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        if req["status"] != "pending_approval":
            raise ValueError(f"restore request is not pending approval: {req['status']}")
        if four_eyes and req["requester"] == approver:
            raise PermissionError("four-eyes policy requires a different approver")
        with self.connect() as conn:
            conn.execute(
                "UPDATE restore_requests SET status='approved',approved_by=?,approved_at=? WHERE id=?",
                (approver, utcnow(), request_id),
            )
        self.audit(approver, "restore.approve", "restore_request", str(request_id), {})

    def start_restore(self, request_id: int, actor: str) -> dict[str, Any]:
        req = self.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        if req["status"] not in {"approved", "ready"}:
            raise ValueError(f"restore request cannot execute from status {req['status']}")
        with self.connect() as conn:
            conn.execute("UPDATE restore_requests SET status='running',started_at=? WHERE id=?", (utcnow(), request_id))
        self.audit(actor, "restore.execute", "restore_request", str(request_id), {})
        return self.get_restore_request(request_id) or req

    def finish_restore(self, request_id: int, *, success: bool, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE restore_requests SET status=?,finished_at=?,result_json=?,error=? WHERE id=?",
                ("success" if success else "failed", utcnow(), json.dumps(result or {}), error, request_id),
            )

    def create_dr_run(self, *, direction: str, source_site: str, target_site: str, details: dict[str, Any] | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO dr_runs(started_at,direction,source_site,target_site,status,details_json) VALUES(?,?,?,?,?,?)",
                (utcnow(), direction, source_site, target_site, "running", json.dumps(details or {}, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def finish_dr_run(self, run_id: int, *, success: bool, details: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE dr_runs SET finished_at=?,status=?,details_json=?,error=? WHERE id=?",
                (utcnow(), "success" if success else "failed", json.dumps(details or {}, sort_keys=True), error, run_id),
            )

    def add_dr_workload(
        self, *, run_id: int, workload_name: str, source_platform: str, target_platform: str, status: str,
        snapshot_id: str | None = None, target_name: str | None = None, result: dict[str, Any] | None = None, error: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO dr_workloads(run_id,workload_name,snapshot_id,source_platform,target_platform,target_name,status,result_json,error)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (run_id, workload_name, snapshot_id, source_platform, target_platform, target_name, status, json.dumps(result or {}, sort_keys=True), error),
            )
            return int(cur.lastrowid)

    def list_dr_workloads(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM dr_workloads WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            out = [dict(r) for r in rows]
        for item in out:
            try:
                item["result"] = json.loads(item.pop("result_json") or "{}")
            except json.JSONDecodeError:
                item["result"] = {}
        return out

    def latest_dr_run(self, *, direction: str | None = None, status: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM dr_runs WHERE 1=1"; params: list[Any] = []
        if direction:
            query += " AND direction=?"; params.append(direction)
        if status:
            query += " AND status=?"; params.append(status)
        query += " ORDER BY started_at DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
            if not row:
                return None
            item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except json.JSONDecodeError:
            item["details"] = {}
        return item

    def update_dr_health(self, name: str, *, healthy: bool, detail: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT consecutive_failures FROM dr_health WHERE name=?", (name,)).fetchone()
            failures = 0 if healthy else (int(row["consecutive_failures"]) + 1 if row else 1)
            conn.execute(
                """INSERT INTO dr_health(name,checked_at,healthy,consecutive_failures,detail_json) VALUES(?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET checked_at=excluded.checked_at,healthy=excluded.healthy,
                consecutive_failures=excluded.consecutive_failures,detail_json=excluded.detail_json""",
                (name, utcnow(), 1 if healthy else 0, failures, json.dumps(detail, sort_keys=True)),
            )
        return {"name": name, "healthy": healthy, "consecutive_failures": failures, "checked_at": utcnow(), "detail": detail}

    def get_dr_health(self, name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM dr_health WHERE name=?", (name,)).fetchone()
            if not row:
                return None
            item = dict(row)
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            item["detail"] = {}
        return item

    def audit(self, actor: str, action: str, object_type: str, object_id: str, details: dict[str, Any]) -> None:
        created_at = utcnow()
        details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            row = conn.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = str(row["event_hash"]) if row and row["event_hash"] else "GENESIS"
            event_hash = self._audit_hash(created_at, actor, action, object_type, object_id, details_json, prev_hash)
            conn.execute(
                "INSERT INTO audit_events(created_at,actor,action,object_type,object_id,details_json,prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                (created_at, actor, action, object_type, object_id, details_json, prev_hash, event_hash),
            )

    def verify_audit_chain(self) -> tuple[bool, list[str]]:
        errors: list[str] = []
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC").fetchall()
        prev_hash = "GENESIS"
        for row in rows:
            if (row["prev_hash"] or "") != prev_hash:
                errors.append(f"audit event {row['id']} previous hash mismatch")
            expected = self._audit_hash(
                row["created_at"], row["actor"], row["action"], row["object_type"],
                row["object_id"], row["details_json"], prev_hash,
            )
            if (row["event_hash"] or "") != expected:
                errors.append(f"audit event {row['id']} hash mismatch")
            prev_hash = str(row["event_hash"] or expected)
        return not errors, errors

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
