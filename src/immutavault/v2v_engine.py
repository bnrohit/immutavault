from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from .adapters import build_adapter
from .config import ReplicaConfig
from .engine import BackupEngine
from .integrity import verify_manifest
from .v2v import V2VManager
from .v2v_config import V10Config


class CertifiedBackupEngine(BackupEngine):
    """BackupEngine extension that unlocks only explicitly certified V2V paths.

    Same-family restores continue through the mature native adapter path. Cross-
    hypervisor restores must pass V2V policy at request time and again at
    execution time, after the immutable source has been restored and verified.
    """

    cfg: V10Config

    def __init__(self, cfg: V10Config) -> None:
        super().__init__(cfg)
        self.v2v = V2VManager(cfg)

    def doctor(self) -> dict[str, list[str]]:
        result = super().doctor()
        if self.cfg.v2v.enabled:
            for name, problems in self.v2v.doctor().items():
                result[f"v2v:{name}"] = list(problems)
        return result

    def v2v_plan(self, snapshot_id: str, target_platform: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        target = self._platform(target_platform)
        if not target.enabled:
            raise ValueError("target platform is disabled")
        return self.v2v.plan(point, target, options or {}).to_dict()

    def request_restore(
        self, *, snapshot_id: str, requester: str, target_platform: str, target_name: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> int:
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        target = self._platform(target_platform)
        if target.type == point["platform_type"]:
            return super().request_restore(
                snapshot_id=snapshot_id, requester=requester, target_platform=target_platform,
                target_name=target_name, options=options,
            )
        requested_options = dict(options or {})
        plan = self.v2v.plan(point, target, requested_options)
        if not plan.allowed:
            raise ValueError("cross-hypervisor recovery blocked: " + "; ".join(plan.reasons))
        target_name = target_name or f"{point['vm_name']}-v2v-{snapshot_id[:8]}"
        requested_options["v2v"] = {
            "mode": plan.mode,
            "provider": plan.provider,
            "certification_id": plan.certification_id,
            "source_type": plan.source_type,
            "target_type": plan.target_type,
        }
        status = "pending_approval" if self.cfg.protection.require_four_eyes_restore else "ready"
        request_id = self.state.create_restore_request(
            requester=requester,
            point=point,
            target_platform=target.name,
            target_name=target_name,
            options=requested_options,
            status=status,
        )
        self.state.audit(requester, "v2v.restore.requested", "restore_request", str(request_id), {
            "snapshot_id": snapshot_id,
            "source_type": plan.source_type,
            "target_type": plan.target_type,
            "certification_id": plan.certification_id,
            "provider": plan.provider,
        })
        return request_id

    def execute_restore(self, request_id: int, *, actor: str, dry_run: bool = False) -> dict[str, Any]:
        req = self.state.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        point = self.state.get_point(req["snapshot_id"])
        if not point:
            raise RuntimeError("recovery point metadata missing")
        target_cfg = self._platform(req["target_platform"])
        if target_cfg.type == point["platform_type"]:
            return super().execute_restore(request_id, actor=actor, dry_run=dry_run)
        if dry_run:
            if req["status"] not in {"pending_approval", "approved", "ready"}:
                raise ValueError(f"restore request cannot be planned from status {req['status']}")
        options = json.loads(req.get("options_json") or "{}")
        plan = self.v2v.plan(point, target_cfg, options)
        if not plan.allowed:
            raise RuntimeError("cross-hypervisor recovery blocked at execution: " + "; ".join(plan.reasons))
        source_repository = str(options.get("source_repository", "primary"))
        replica: ReplicaConfig | None = None
        if source_repository != "primary":
            replica = self._replica(source_repository)
            copy = self.state.get_recovery_copy(point["snapshot_id"], source_repository)
            if not copy or copy.get("status") != "success":
                raise RuntimeError(f"recovery point is not available on requested source repository {source_repository}")
        restore_root = Path(self.cfg.runtime.restore_staging_path) / f"request-{request_id}"
        restored_source = restore_root / str(point["source_path"]).lstrip("/")
        if dry_run:
            return {
                "request_id": request_id,
                "snapshot": point["snapshot_id"],
                "source_type": point["platform_type"],
                "target_platform": target_cfg.name,
                "target_type": target_cfg.type,
                "target_name": req["target_name"],
                "source_repository": source_repository,
                "v2v": plan.to_dict(),
                "note": "execution restores and verifies the immutable point before conversion; the source VM is never modified and the target is created as a new powered-off VM",
            }
        self._ensure_path_capacity(self.cfg.runtime.restore_staging_path, action="V2V restore")
        target_adapter = build_adapter(target_cfg, self.cfg.runtime.command_timeout_seconds)
        target_problems = target_adapter.doctor()
        if target_problems:
            self.state.audit(actor, "v2v.restore.preflight_failed", "restore_request", str(request_id), {"problems": target_problems})
            raise RuntimeError("V2V target preflight failed: " + "; ".join(target_problems))
        req = self.state.start_restore(request_id, actor)
        try:
            shutil.rmtree(restore_root, ignore_errors=True)
            self.repo.restore(point["snapshot_id"], str(restore_root), replica=replica)
            if not restored_source.exists():
                raise RuntimeError(f"restic restore completed but expected source path is missing: {restored_source}")
            ok, errors = verify_manifest(restored_source, expected_digest=point.get("manifest_sha256"))
            if not ok:
                raise RuntimeError("recovery point manifest verification failed before V2V: " + "; ".join(errors[:10]))
            result = self.v2v.execute(
                source=restored_source,
                point=point,
                target=target_cfg,
                target_name=req["target_name"],
                options=options,
                dry_run=False,
            )
            result["source_repository"] = source_repository
            result["source_snapshot"] = point["snapshot_id"]
            self.state.finish_restore(request_id, success=True, result=result)
            self.state.audit(actor, "v2v.restore.complete", "restore_request", str(request_id), result)
            shutil.rmtree(restore_root, ignore_errors=True)
            return result
        except Exception as exc:
            self.state.finish_restore(request_id, success=False, error=str(exc))
            self.state.audit(actor, "v2v.restore.failed", "restore_request", str(request_id), {
                "error": str(exc),
                "source_type": point["platform_type"],
                "target_type": target_cfg.type,
                "certification_id": plan.certification_id,
            })
            if not self.cfg.runtime.keep_staging_on_failure:
                shutil.rmtree(restore_root, ignore_errors=True)
            raise
