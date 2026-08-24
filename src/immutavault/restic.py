from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.parse import quote, urlsplit, urlunsplit
from pathlib import Path
from .config import RepositoryConfig, ReplicaConfig
from .runner import run
from .storage import apply_object_lock, ensure_r2_bucket_lock, init_target, restic_options, restic_target_url, s3_preflight, target_env, target_health
from .transport_state import CHAIN_INDEX, atomic_json, chain_for, commit_after_backup, expand_dependencies, find_transport_marker, marker_for_source, prune_dependencies

CBT_PARENT_TAG = "immutavault-cbt-parent:"


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
        url = self.cfg.url; user = os.getenv("REST_SERVER_USER"); password = os.getenv("REST_SERVER_PASSWORD")
        if not (url.startswith("rest:") and user and password):
            return url
        parsed = urlsplit(url[len("rest:"):]); host = parsed.hostname or ""
        if parsed.port: host = f"{host}:{parsed.port}"
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
        return "rest:" + urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    @staticmethod
    def _parent_from_snapshot(row: dict) -> str:
        for tag in row.get("tags") or []:
            text = str(tag)
            if text.startswith(CBT_PARENT_TAG):
                return text[len(CBT_PARENT_TAG):]
        return ""

    @classmethod
    def _chain_from_rows(cls, snapshot_id: str, rows: list[dict]) -> list[str]:
        parents = {str(row.get("id") or ""): cls._parent_from_snapshot(row) for row in rows if row.get("id")}
        order: list[str] = []; current = snapshot_id; seen: set[str] = set()
        while current:
            if current in seen: raise RuntimeError("CBT snapshot-tag dependency loop detected")
            seen.add(current); order.append(current)
            current = parents.get(current, "")
            if len(order) > 256: raise RuntimeError("CBT chain exceeds safety depth")
        order.reverse(); return order

    def _source_chain(self, snapshot_id: str, *, local: bool = False) -> list[str]:
        result = run(["restic", "snapshots", "--json"], timeout=300, env=self._env(local=local))
        rows = json.loads(result.stdout or "[]")
        chain = self._chain_from_rows(snapshot_id, rows)
        if len(chain) == 1:
            cached = chain_for(snapshot_id)
            if len(cached) > 1: chain = cached
        return chain

    def init_if_needed(self, *, local: bool = True) -> None:
        Path(self.cfg.local_path).mkdir(parents=True, exist_ok=True)
        result = run(["restic", "snapshots", "--json"], timeout=120, env=self._env(local=local), check=False)
        if result.returncode != 0: run(["restic", "init"], timeout=120, env=self._env(local=local))

    def backup(self, path: str, tags: list[str]) -> BackupSummary:
        marker = marker_for_source(path) or {}
        effective_tags = list(tags)
        if marker.get("transport") == "vmware-cbt-vddk" and marker.get("kind") == "delta":
            parent = str(marker.get("parent_snapshot_id") or "")
            if not parent: raise RuntimeError("CBT delta is missing parent recovery-point ID")
            effective_tags.append(CBT_PARENT_TAG + parent)
        cmd = ["restic", "backup", path, "--json", "--one-file-system"]
        for tag in effective_tags: cmd += ["--tag", tag]
        result = run(cmd, timeout=self.timeout, env=self._env(local=False))
        values: dict[str, int | str] = {"snapshot_id": "", "total_bytes_processed": 0, "total_files_processed": 0, "data_added": 0}
        for line in result.stdout.splitlines():
            try: event = json.loads(line)
            except json.JSONDecodeError: continue
            if event.get("message_type") == "summary":
                values["snapshot_id"] = event.get("snapshot_id", "")
                values["total_bytes_processed"] = int(event.get("total_bytes_processed", 0) or 0)
                values["total_files_processed"] = int(event.get("total_files_processed", 0) or 0)
                values["data_added"] = int(event.get("data_added", 0) or 0)
        if not values["snapshot_id"]: raise RuntimeError("restic backup completed without returning a snapshot id")
        commit_after_backup(path, str(values["snapshot_id"]))
        return BackupSummary(**values)  # type: ignore[arg-type]

    def snapshots(self) -> list[dict]:
        result = run(["restic", "snapshots", "--json"], timeout=300, env=self._env(local=False))
        return json.loads(result.stdout or "[]")

    def _restore_one(self, snapshot_id: str, target: str, replica: ReplicaConfig | None) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        if replica is None:
            run(["restic", "restore", snapshot_id, "--target", target], timeout=self.timeout, env=self._env(local=False)); return
        run(["restic", *restic_options(replica), "restore", snapshot_id, "--target", target], timeout=self.timeout, env=target_env(replica))

    def restore(self, snapshot_id: str, target: str, replica: ReplicaConfig | None = None) -> None:
        """Restore current point, then walk immutable parent markers back to baseline."""
        root = Path(target); self._restore_one(snapshot_id, target, replica)
        marker_path = find_transport_marker(root)
        marker = marker_for_source(root)
        if not marker or marker.get("transport") != "vmware-cbt-vddk" or marker.get("kind") != "delta": return
        if marker_path is None: raise RuntimeError("CBT transport marker could not be located")
        layers = [{"snapshot_id": snapshot_id, "kind": "delta", "source": str(marker_path.parent)}]
        current_marker = marker; seen = {snapshot_id}; chain_root = root / ".immutavault-chain"; chain_root.mkdir(parents=True, exist_ok=True)
        index = 0
        while current_marker.get("kind") == "delta":
            parent = str(current_marker.get("parent_snapshot_id") or "")
            if not parent: raise RuntimeError("CBT delta is missing parent recovery-point ID")
            if parent in seen: raise RuntimeError("CBT restore dependency loop detected")
            seen.add(parent); layer_root = chain_root / f"{index:03d}-{parent[:16]}"; index += 1
            self._restore_one(parent, str(layer_root), replica)
            parent_path = find_transport_marker(layer_root); parent_marker = marker_for_source(layer_root)
            if parent_path is None or not parent_marker: raise RuntimeError(f"CBT parent {parent} is missing transport metadata")
            kind = str(parent_marker.get("kind") or "")
            if kind not in {"baseline", "delta"}: raise RuntimeError(f"CBT parent {parent} has invalid layer kind {kind}")
            layers.append({"snapshot_id": parent, "kind": kind, "source": str(parent_path.parent)})
            current_marker = parent_marker
        layers.reverse()
        if layers[0]["kind"] != "baseline": raise RuntimeError("CBT restore chain does not terminate at a full baseline")
        atomic_json(root / CHAIN_INDEX, {"schema": 1, "transport": "vmware-cbt-vddk", "layers": layers})

    def retention(self, *, protected_snapshot_ids: set[str] | None = None) -> list[str]:
        """Apply GFS policy while preserving all ancestors of retained CBT points."""
        r = self.cfg.retention; protected = set(protected_snapshot_ids or set()); local_env = self._env(local=True)
        policy = ["--group-by", "tags", "--keep-within", f"{r.keep_within_days}d", "--keep-daily", str(r.keep_daily), "--keep-weekly", str(r.keep_weekly), "--keep-monthly", str(r.keep_monthly), "--keep-yearly", str(r.keep_yearly), "--keep-last", str(r.min_restore_points)]
        all_result = run(["restic", "snapshots", "--json"], timeout=300, env=local_env); rows = json.loads(all_result.stdout or "[]")
        existing = {str(row.get("id") or "") for row in rows if row.get("id")}
        preview = run(["restic", "forget", "--dry-run", "--json", *policy], timeout=self.timeout, env=local_env)
        candidates = {str(snap.get("id")) for group in json.loads(preview.stdout or "[]") for snap in group.get("remove", []) if snap.get("id")}
        kept = (existing - candidates) | protected
        parent_map = {str(row.get("id")): self._parent_from_snapshot(row) for row in rows if row.get("id")}
        for sid in list(kept):
            current = sid; seen: set[str] = set()
            while current and current not in seen:
                seen.add(current); parent = parent_map.get(current, "")
                if parent: protected.add(parent)
                current = parent
        protected |= expand_dependencies(kept)
        remove = sorted(sid for sid in candidates if sid not in protected)
        if remove:
            run(["restic", "forget", *remove], timeout=self.timeout, env=local_env); run(["restic", "prune"], timeout=self.timeout, env=local_env)
        prune_dependencies(existing - set(remove)); return remove

    def check(self) -> None:
        cmd = ["restic", "check"]
        if self.cfg.verify_percent > 0: cmd += ["--read-data-subset", f"{self.cfg.verify_percent}%"]
        run(cmd, timeout=self.timeout, env=self._env(local=True))

    def init_replica(self, replica: ReplicaConfig) -> dict: return init_target(replica)

    def replica_health(self, replica: ReplicaConfig) -> dict:
        h = target_health(replica); return {"ok": h.ok, "problems": h.problems, **h.details}

    def copy_snapshot(self, snapshot_id: str, replica: ReplicaConfig, *, immutable_days: int | None = None) -> dict:
        if not replica.enabled: return {"status": "disabled"}
        primary_password = os.environ.get("RESTIC_PASSWORD")
        if not primary_password: raise RuntimeError("RESTIC_PASSWORD is not set")
        env = target_env(replica)
        if replica.backend == "s3" and replica.object_lock_enabled: s3_preflight(replica)
        env["RESTIC_FROM_REPOSITORY"] = self._source_repository_with_auth(); env["RESTIC_FROM_PASSWORD"] = primary_password
        chain = self._source_chain(snapshot_id, local=False); copied: list[str] = []
        for sid in chain:
            run(["restic", *restic_options(replica), "copy", sid], timeout=self.timeout, env=env)
            probe = run(["restic", *restic_options(replica), "snapshots", sid, "--json"], timeout=300, env=env)
            if not json.loads(probe.stdout or "[]"): raise RuntimeError(f"replica {replica.name} did not expose required CBT chain snapshot {sid}")
            copied.append(sid)
        lock_result = ensure_r2_bucket_lock(replica, minimum_days=immutable_days) if replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled else apply_object_lock(replica, minimum_days=immutable_days)
        return {"status": "success", "repository": restic_target_url(replica), "backend": replica.backend, "provider": replica.provider if replica.backend == "s3" else None, "object_lock": lock_result, "chain_snapshots": copied}
