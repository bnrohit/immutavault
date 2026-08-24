from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.parse import quote, urlsplit, urlunsplit
from pathlib import Path
from .config import RepositoryConfig, ReplicaConfig
from .runner import run
from .storage import apply_object_lock, init_target, restic_options, restic_target_url, s3_preflight, target_env, target_health


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
        """Return primary REST URL with transport credentials embedded in the environment-only source URL.

        restic does not provide FROM-prefixed REST backend credential variables. Embedding credentials
        in RESTIC_FROM_REPOSITORY keeps them out of command-line process arguments while allowing a
        copy to use different destination backend credentials.
        """
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
        return BackupSummary(**values)  # type: ignore[arg-type]

    def snapshots(self) -> list[dict]:
        result = run(["restic", "snapshots", "--json"], timeout=300, env=self._env(local=False))
        return json.loads(result.stdout or "[]")

    def restore(self, snapshot_id: str, target: str, replica: ReplicaConfig | None = None) -> None:
        Path(target).mkdir(parents=True, exist_ok=True)
        if replica is None:
            run(["restic", "restore", snapshot_id, "--target", target], timeout=self.timeout, env=self._env(local=False))
            return
        env = target_env(replica)
        run(
            ["restic", *restic_options(replica), "restore", snapshot_id, "--target", target],
            timeout=self.timeout, env=env,
        )

    def retention(self, *, protected_snapshot_ids: set[str] | None = None) -> list[str]:
        """Apply GFS policy without deleting state-protected snapshots.

        We first ask restic for a JSON dry-run, then remove only snapshots not
        protected by Immutavault's immutable_until catalog. This is intentionally
        executed against the local repository by the root-only maintenance job.
        """
        r = self.cfg.retention
        protected = protected_snapshot_ids or set()
        policy = [
            "--group-by", "tags", "--keep-within", f"{r.keep_within_days}d",
            "--keep-daily", str(r.keep_daily), "--keep-weekly", str(r.keep_weekly),
            "--keep-monthly", str(r.keep_monthly), "--keep-yearly", str(r.keep_yearly),
            "--keep-last", str(r.min_restore_points),
        ]
        preview = run(
            ["restic", "forget", "--dry-run", "--json", *policy],
            timeout=self.timeout, env=self._env(local=True),
        )
        groups = json.loads(preview.stdout or "[]")
        remove: list[str] = []
        for group in groups:
            for snap in group.get("remove", []):
                sid = str(snap.get("id", ""))
                if sid and sid not in protected:
                    remove.append(sid)
        remove = sorted(set(remove))
        if remove:
            run(["restic", "forget", *remove], timeout=self.timeout, env=self._env(local=True))
            run(["restic", "prune"], timeout=self.timeout, env=self._env(local=True))
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
        # Confirm the snapshot is actually visible in the destination before considering the copy healthy.
        probe = run(
            ["restic", *restic_options(replica), "snapshots", snapshot_id, "--json"],
            timeout=300, env=env,
        )
        snapshots = json.loads(probe.stdout or "[]")
        if not snapshots:
            raise RuntimeError(f"replica {replica.name} did not expose copied snapshot {snapshot_id}")
        lock_result = apply_object_lock(replica, minimum_days=immutable_days)
        if replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled:
            lock_result = {
                "enabled": True, "kind": "cloudflare_r2_bucket_lock",
                "rule_id_base": replica.r2_lock_rule_id, "days": max(replica.r2_bucket_lock_days, immutable_days or 0),
                "note": "provider-native R2 Bucket Locks protect persistent restic namespaces and exclude transient locks/; configure/verify with replica-lock-init",
            }
        return {
            "status": "success",
            "repository": restic_target_url(replica),
            "backend": replica.backend,
            "provider": replica.provider if replica.backend == "s3" else None,
            "object_lock": lock_result,
        }
