from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.parse import quote, urlsplit, urlunsplit
from pathlib import Path
from .config import RepositoryConfig, ReplicaConfig
from .runner import run
from .storage import apply_object_lock, ensure_r2_bucket_lock, init_target, restic_options, restic_target_url, s3_preflight, target_env, target_health
from .transport_state import (
    CHAIN_INDEX,
    atomic_json,
    chain_for,
    commit_after_backup,
    dependency_row,
    expand_dependencies,
    marker_for_source,
    prune_dependencies,
)


@dataclass(frozen=True)
class BackupSummary:
    snapshot_id: str
    total_bytes_processed: int = 0
    total_files_processed: int = 0
    data_added: int = 0


class ResticRepository:
    def __init__(self, cfg: RepositoryConfig, timeout: int = 14400) -> None:
        self.cfg = cfg
        self.timeout = timeout

    def _env(self, local: bool = False) -> dict[str, str]:
        env: dict[str, str] = {}
        if "RESTIC_PASSWORD" not in os.environ:
            raise RuntimeError("RESTIC_PASSWORD is not set")
        if local:
            env["RESTIC_REPOSITORY"] = self.cfg.local_path
        else:
            env["RESTIC_REPOSITORY"] = self.cfg.url
            if os.getenv("REST_SERVER_USER") and os.getenv("REST_SERVER_PASSWORD"):
                env["RESTIC_REST_USERNAME"] = os.environ["REST_SERVER_USER"]
                env["RESTIC_REST_PASSWORD"] = os.environ["REST_SERVER_PASSWORD"]
        if self.cfg.cacert:
            env["RESTIC_CACERT"] = self.cfg.cacert
        return env

    def _source_repository_with_auth(self) -> str:
        url = self.cfg.url
        user = os.getenv("REST_SERVER_USER")
        password = os.getenv("REST_SERVER_PASSWORD")
        if not (url.startswith("rest:") and user and password):
            return url
        raw = url[len("rest:"):]
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
        return "rest:" + urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    def init_if_needed(self, *, local: bool = True) -> None:
        repo = Path(self.cfg.local_path)
        repo.mkdir(parents=True, exist_ok=True)
        result = run(["restic", "snapshots", "--json"], timeout=120, env=self._env(local=local), check=False)
        if result.returncode != 0:
            run(["restic", "init"], timeout=120, env=self._env(local=local))

    def backup(self, path: str, tags: list[str]) -> BackupSummary:
        cmd = ["restic", "backup", path, "--json", "--one-file-system"]
        for tag in tags:
            cmd += ["--tag", tag]
        result = run(cmd, timeout=self.timeout, env=self._env(local=False))
        values: dict[str, int | str] = {
            "snapshot_id": "", "total_bytes_processed": 0, "total_files_processed": 0, "data_added": 0
        }
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("message_type") == "summary":
                values["snapshot_id"] = event.get("snapshot_id", "")
                values["total_bytes_processed"] = int(event.get("total_bytes_processed", 0) or 0)
                values["total_files_processed"] = int(event.get("total_files_processed", 0) or 0)
                values["data_added"] = int(event.get("data_added", 0) or 0)
        if not values["snapshot_id"]:
            raise RuntimeError("restic backup completed without returning a snapshot id")
        # Transaction boundary: CBT change IDs are advanced only after restic has
        # durably returned the immutable recovery-point ID. A failed restic run
        # therefore cannot create a skipped-change gap in the next incremental.
        commit_after_backup(path, str(values["snapshot_id"]))
        return BackupSummary(**values)  # type: ignore[arg-type]

    def snapshots(self) -> list[dict]:
        result = run(["restic", "snapshots", "--json"], timeout=300, env=self._env(local=False))
        return json.loads(result.stdout or "[]")

    def _restore_one(self, snapshot_id: str, target: str, replica: ReplicaConfig | None) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        if replica is None:
            run(["restic", "restore", snapshot_id, "--target", target], timeout=self.timeout, env=self._env(local=False))
            return
        run(
            ["restic", *restic_options(replica), "restore", snapshot_id, "--target", target],
            timeout=self.timeout, env=target_env(replica),
        )

    def restore(self, snapshot_id: str, target: str, replica: ReplicaConfig | None = None) -> None:
        self._restore_one(snapshot_id, target, replica)
        chain = chain_for(snapshot_id)
        if len(chain) <= 1:
            return
        root = Path(target)
        layers: list[dict] = []
        chain_root = root / ".immutavault-chain"
        chain_root.mkdir(parents=True, exist_ok=True)
        for index, sid in enumerate(chain):
            row = dependency_row(sid)
            if not row:
                raise RuntimeError(f"missing CBT dependency metadata for {sid}")
            if sid == snapshot_id:
                layer_root = root
            else:
                layer_root = chain_root / f"{index:03d}-{sid[:16]}"
                self._restore_one(sid, str(layer_root), replica)
            source_path = str(row.get("source_path") or "")
            if not source_path:
                raise RuntimeError(f"CBT dependency {sid} has no source path")
            source = layer_root / source_path.lstrip("/")
            if not source.exists():
                raise RuntimeError(f"restored CBT layer {sid} is missing expected source path {source}")
            marker = marker_for_source(source)
            kind = str((marker or {}).get("kind") or row.get("kind") or "")
            layers.append({"snapshot_id": sid, "kind": kind, "source": str(source)})
        if not layers or layers[0].get("kind") != "baseline":
            raise RuntimeError("CBT restore chain does not start with a full baseline")
        atomic_json(root / CHAIN_INDEX, {"schema": 1, "transport": "vmware-cbt-vddk", "layers": layers})

    def retention(self, *, protected_snapshot_ids: set[str] | None = None) -> list[str]:
        """Apply GFS policy while preserving every retained CBT ancestor."""
        r = self.cfg.retention
        protected = set(protected_snapshot_ids or set())
        policy = [
            "--group-by", "tags", "--keep-within", f"{r.keep_within_days}d",
            "--keep-daily", str(r.keep_daily), "--keep-weekly", str(r.keep_weekly),
            "--keep-monthly", str(r.keep_monthly), "--keep-yearly", str(r.keep_yearly),
            "--keep-last", str(r.min_restore_points),
        ]
        local_env = self._env(local=True)
        all_result = run(["restic", "snapshots", "--json"], timeout=300, env=local_env)
        existing = {str(row.get("id") or "") for row in json.loads(all_result.stdout or "[]") if row.get("id")}
        preview = run(["restic", "forget", "--dry-run", "--json", *policy], timeout=self.timeout, env=local_env)
        groups = json.loads(preview.stdout or "[]")
        candidates: set[str] = set()
        for group in groups:
            for snap in group.get("remove", []):
                sid = str(snap.get("id", ""))
                if sid:
                    candidates.add(sid)
        # Determine the snapshots policy intends to keep, then recursively protect
        # their CBT baselines/parents. This prevents a valid incremental from
        # outliving the full/delta layers required to restore it.
        kept = (existing - candidates) | protected
        protected |= expand_dependencies(kept)
        remove = sorted(sid for sid in candidates if sid not in protected)
        if remove:
            run(["restic", "forget", *remove], timeout=self.timeout, env=local_env)
            run(["restic", "prune"], timeout=self.timeout, env=local_env)
        prune_dependencies(existing - set(remove))
        return remove

    def check(self) -> None:
        pct = self.cfg.verify_percent
        cmd = ["restic", "check"]
        if pct > 0:
            cmd += ["--read-data-subset", f"{pct}%"]
        run(cmd, timeout=self.timeout, env=self._env(local=True))

    def init_replica(self, replica: ReplicaConfig) -> dict:
        return init_target(replica)

    def replica_health(self, replica: ReplicaConfig) -> dict:
        h = target_health(replica)
        return {"ok": h.ok, "problems": h.problems, **h.details}

    def copy_snapshot(self, snapshot_id: str, replica: ReplicaConfig, *, immutable_days: int | None = None) -> dict:
        if not replica.enabled:
            return {"status": "disabled"}
        primary_password = os.environ.get("RESTIC_PASSWORD")
        if not primary_password:
            raise RuntimeError("RESTIC_PASSWORD is not set")
        env = target_env(replica)
        if replica.backend == "s3" and replica.object_lock_enabled:
            s3_preflight(replica)
        env["RESTIC_FROM_REPOSITORY"] = self._source_repository_with_auth()
        env["RESTIC_FROM_PASSWORD"] = primary_password
        run(["restic", *restic_options(replica), "copy", snapshot_id], timeout=self.timeout, env=env)
        probe = run(
            ["restic", *restic_options(replica), "snapshots", snapshot_id, "--json"],
            timeout=300, env=env,
        )
        snapshots = json.loads(probe.stdout or "[]")
        if not snapshots:
            raise RuntimeError(f"replica {replica.name} did not expose copied snapshot {snapshot_id}")
        if replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled:
            lock_result = ensure_r2_bucket_lock(replica, minimum_days=immutable_days)
        else:
            lock_result = apply_object_lock(replica, minimum_days=immutable_days)
        return {
            "status": "success",
            "repository": restic_target_url(replica),
            "backend": replica.backend,
            "provider": replica.provider if replica.backend == "s3" else None,
            "object_lock": lock_result,
        }
