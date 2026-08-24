from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import shutil
from typing import Any

from . import __version__
from .adapters import build_adapter
from .adapters.base import VM
from .anomaly import detect_backup_anomaly
from .config import Config, PlatformConfig, ReplicaConfig
from .integrity import build_manifest, verify_manifest
from .lock import exclusive_lock
from .restic import ResticRepository
from .runner import run
from .state import StateDB
from .storage import apply_object_lock, ensure_r2_bucket_lock, r2_bucket_lock_status, restic_target_url
from .util import safe_component
from .dr import DROrchestrator


class BackupEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = StateDB(cfg.runtime.state_db)
        self.repo = ResticRepository(cfg.repository, cfg.runtime.command_timeout_seconds)

    def dr_orchestrator(self) -> DROrchestrator:
        return DROrchestrator(self)

    def doctor(self) -> dict[str, list[str]]:
        results: dict[str, list[str]] = {"repository": []}
        restic_bin = shutil.which("restic")
        if restic_bin is None:
            results["repository"].append("restic is not installed")
        else:
            version = run([restic_bin, "version"], timeout=30, check=False)
            match = re.search(r"restic (\d+)\.(\d+)\.(\d+)", version.stdout + version.stderr)
            if version.returncode != 0 or not match:
                results["repository"].append("restic version/capability probe failed")
            else:
                parsed = tuple(int(x) for x in match.groups())
                if parsed < (0, 19, 1):
                    results["repository"].append(
                        f"restic {'.'.join(map(str, parsed))} is older than the tested minimum 0.19.1"
                    )
        staging = Path(self.cfg.repository.staging_path)
        try:
            staging.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(staging)
            free_pct = (usage.free / usage.total * 100) if usage.total else 0
            if free_pct < self.cfg.runtime.staging_min_free_percent:
                results["repository"].append(
                    f"staging free space is {free_pct:.1f}%, below configured minimum {self.cfg.runtime.staging_min_free_percent}%"
                )
        except OSError as exc:
            results["repository"].append(f"staging path unavailable: {exc}")
        for replica in self.cfg.replicas:
            if not replica.enabled:
                continue
            try:
                health = self.repo.replica_health(replica)
                results[f"storage:{replica.name}"] = list(health.get("problems", []))
            except Exception as exc:
                results[f"storage:{replica.name}"] = [str(exc)]
        for platform in self.cfg.platforms:
            if not platform.enabled:
                continue
            results[platform.name] = build_adapter(platform, self.cfg.runtime.command_timeout_seconds).doctor()
        return results


    def status(self) -> dict[str, Any]:
        doctor = self.doctor()
        audit_ok, audit_errors = self.state.verify_audit_chain()
        return {
            "healthy": not any(doctor.values()) and audit_ok,
            "components": doctor,
            "audit_chain": {"valid": audit_ok, "errors": audit_errors},
            "catalog": self.state.status_summary(),
        }

    def backup_state_database(self) -> dict[str, Any]:
        path = self.state.backup_database(
            self.cfg.runtime.state_backup_path, keep=self.cfg.runtime.state_backup_keep
        )
        self.state.audit("system", "state.backup", "state_database", path, {"keep": self.cfg.runtime.state_backup_keep})
        return {"status": "complete", "path": path, "keep": self.cfg.runtime.state_backup_keep}

    def platform_info(self) -> dict[str, dict[str, Any]]:
        info: dict[str, dict[str, Any]] = {}
        for platform in self.cfg.platforms:
            if platform.enabled:
                adapter = build_adapter(platform, self.cfg.runtime.command_timeout_seconds)
                try:
                    info[platform.name] = adapter.platform_info()
                except Exception as exc:
                    info[platform.name] = {"name": platform.name, "type": platform.type, "error": str(exc)}
        return info

    def inventory(self) -> dict[str, list[VM]]:
        inventory: dict[str, list[VM]] = {}
        for platform in self.cfg.platforms:
            if platform.enabled:
                inventory[platform.name] = build_adapter(platform, self.cfg.runtime.command_timeout_seconds).inventory()
        return inventory

    def _ensure_path_capacity(self, path: str | Path, *, action: str) -> None:
        staging = Path(path)
        staging.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(staging)
        free_pct = usage.free / usage.total * 100 if usage.total else 0
        if free_pct < self.cfg.runtime.staging_min_free_percent:
            raise RuntimeError(
                f"{action} blocked: staging filesystem only has {free_pct:.1f}% free; "
                f"minimum is {self.cfg.runtime.staging_min_free_percent}%"
            )

    def _ensure_staging_capacity(self) -> None:
        self._ensure_path_capacity(self.cfg.repository.staging_path, action="backup")

    def backup_all(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with exclusive_lock(self.cfg.repository.lock_file):
            self._ensure_staging_capacity()
            for platform in self.cfg.platforms:
                if not platform.enabled:
                    continue
                adapter = build_adapter(platform, self.cfg.runtime.command_timeout_seconds)
                try:
                    platform_info = adapter.platform_info()
                except Exception as exc:
                    platform_info = {"error": str(exc)}
                for vm in adapter.inventory():
                    results.append(self._backup_vm(platform, adapter, vm, platform_info=platform_info, dry_run=dry_run))
        return results

    def _backup_vm(self, platform: PlatformConfig, adapter, vm: VM, *, platform_info: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        staging_root = Path(self.cfg.repository.staging_path) / safe_component(platform.name) / safe_component(vm.name) / stamp
        if dry_run:
            exported = adapter.export(vm, staging_root, dry_run=True)
            return {"platform": platform.name, "vm": vm.name, "status": "dry-run", "would_export": str(exported)}

        staging_root.mkdir(parents=True, exist_ok=True)
        job_id = self.state.start_job(platform.name, vm.id, vm.name)
        try:
            exported = adapter.export(vm, staging_root)
            manifest, manifest_digest = build_manifest(staging_root)
            summary = self.repo.backup(
                str(staging_root),
                tags=[
                    f"immutavault:v{__version__}", f"platform:{platform.type}", f"source:{platform.name}",
                    f"vm-id:{vm.id}", f"vm:{vm.name}", f"vm-kind:{vm.kind}",
                ],
            )
            previous = self.state.latest_point(platform.name, vm.id)
            suspicious, reason = detect_backup_anomaly(
                previous_bytes=int(previous["backup_bytes"]) if previous else None,
                current_bytes=int(manifest["total_bytes"]),
                data_added=summary.data_added,
                data_added_ratio_threshold=self.cfg.protection.anomaly_data_added_ratio,
                size_change_ratio_threshold=self.cfg.protection.anomaly_size_change_ratio,
            )
            immutable_days = self.cfg.repository.retention.keep_within_days
            if suspicious:
                immutable_days = max(immutable_days, self.cfg.protection.preserve_suspicious_points_days)
            immutable_until = (now + timedelta(days=immutable_days)).isoformat()
            point = {
                "snapshot_id": summary.snapshot_id,
                "created_at": now.isoformat(),
                "platform": platform.name,
                "platform_type": platform.type,
                "vm_id": vm.id,
                "vm_name": vm.name,
                "power_state": vm.power_state,
                "source_path": str(staging_root),
                "backup_bytes": int(manifest["total_bytes"]),
                "data_added": summary.data_added,
                "file_count": int(manifest["file_count"]),
                "manifest_sha256": manifest_digest,
                "immutable_until": immutable_until,
                "suspicious": 1 if suspicious else 0,
                "suspicious_reason": reason,
                "verified": 0,
                "verified_at": None,
                "metadata_json": json.dumps({"exported": str(exported), "vm_kind": vm.kind, "platform_info": platform_info}),
            }
            self.state.upsert_recovery_point(point)
            self.state.upsert_recovery_copy(
                snapshot_id=summary.snapshot_id, target_name="primary", backend="rest",
                repository=self.cfg.repository.url, status="success", immutable_until=immutable_until,
                object_lock={"type": "append-only-vault", "logical_immutability": True},
            )
            self.state.finish_job(job_id, status="success", snapshot_id=summary.snapshot_id)
            self.state.audit("system", "backup.complete", "recovery_point", summary.snapshot_id, {
                "platform": platform.name, "vm": vm.name, "suspicious": suspicious, "reason": reason,
            })

            replica_results: dict[str, Any] = {}
            for replica in self.cfg.replicas:
                if not replica.enabled:
                    continue
                try:
                    copied = self.repo.copy_snapshot(summary.snapshot_id, replica, immutable_days=immutable_days)
                    replica_results[replica.name] = copied
                    lock_info = copied.get("object_lock") or {}
                    self.state.upsert_recovery_copy(
                        snapshot_id=summary.snapshot_id, target_name=replica.name, backend=replica.backend,
                        provider=replica.provider if replica.backend == "s3" else None,
                        repository=restic_target_url(replica), status="success",
                        immutable_until=lock_info.get("retain_until") or immutable_until, object_lock=lock_info,
                    )
                    self.state.audit("system", "replica.complete", "recovery_point", summary.snapshot_id, {
                        "target": replica.name, "backend": replica.backend, "provider": replica.provider,
                        "object_lock": lock_info,
                    })
                except Exception as exc:  # primary backup remains valid; replication failure is surfaced explicitly
                    replica_results[replica.name] = {"status": "failed", "error": str(exc)}
                    try:
                        repo_url = restic_target_url(replica)
                    except Exception:
                        repo_url = "unresolved"
                    self.state.upsert_recovery_copy(
                        snapshot_id=summary.snapshot_id, target_name=replica.name, backend=replica.backend,
                        provider=replica.provider if replica.backend == "s3" else None,
                        repository=repo_url, status="failed", error=str(exc),
                    )
                    self.state.audit("system", "replica.failed", "recovery_point", summary.snapshot_id, {
                        "target": replica.name, "error": str(exc),
                    })

            if self.cfg.protection.verify_after_backup:
                verified = self.verify_recovery_point(summary.snapshot_id, actor="system")
            else:
                verified = None
            shutil.rmtree(staging_root, ignore_errors=True)
            return {
                "platform": platform.name,
                "vm": vm.name,
                "status": "success",
                "snapshot": summary.snapshot_id,
                "bytes": int(manifest["total_bytes"]),
                "data_added": summary.data_added,
                "immutable_until": immutable_until,
                "suspicious": suspicious,
                "suspicious_reason": reason,
                "replicas": replica_results,
                "verified": verified,
            }
        except Exception as exc:
            self.state.finish_job(job_id, status="failed", error=str(exc))
            self.state.audit("system", "backup.failed", "vm", f"{platform.name}:{vm.id}", {"error": str(exc)})
            if not self.cfg.runtime.keep_staging_on_failure:
                shutil.rmtree(staging_root, ignore_errors=True)
            return {"platform": platform.name, "vm": vm.name, "status": "failed", "error": str(exc)}

    def retention(self) -> list[str]:
        protected = self.state.protected_snapshot_ids()
        removed = self.repo.retention(protected_snapshot_ids=protected)
        self.state.mark_points_expired(removed)
        self.state.audit("root-maintenance", "retention.complete", "repository", "primary", {
            "removed": removed, "protected_count": len(protected),
        })
        return removed

    def list_recovery_points(self, *, platform: str | None = None, vm_id: str | None = None) -> list[dict[str, Any]]:
        points = self.state.list_points(platform=platform, vm_id=vm_id)
        now = datetime.now(timezone.utc)
        for point in points:
            score = 100
            if not point.get("verified"):
                score -= 25
            if point.get("suspicious"):
                score -= 60
            try:
                immutable = datetime.fromisoformat(str(point["immutable_until"])) > now
            except (ValueError, TypeError):
                immutable = False
            if not immutable:
                score -= 10
            point["recovery_score"] = max(0, score)
            point["recovery_status"] = (
                "avoid-suspicious" if point.get("suspicious") else
                "preferred" if point.get("verified") and immutable else
                "verify-first" if not point.get("verified") else "available"
            )
            point["copies"] = self.state.list_recovery_copies(str(point["snapshot_id"]))
            point["available_restore_sources"] = [
                c["target_name"] for c in point["copies"] if c.get("status") == "success"
            ]
        return points

    def storage_targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = [{
            "name": "primary", "backend": "rest", "repository": self.cfg.repository.url,
            "role": "on-prem append-only primary", "enabled": True,
        }]
        for replica in self.cfg.replicas:
            entry = {
                "name": replica.name, "backend": replica.backend,
                "provider": replica.provider if replica.backend == "s3" else None,
                "repository": restic_target_url(replica), "enabled": replica.enabled,
                "object_lock_enabled": replica.object_lock_enabled,
                "r2_bucket_lock_enabled": replica.r2_bucket_lock_enabled,
            }
            if replica.enabled:
                try:
                    entry["health"] = self.repo.replica_health(replica)
                except Exception as exc:
                    entry["health"] = {"ok": False, "problems": [str(exc)]}
            targets.append(entry)
        return targets

    def init_replica(self, name: str) -> dict[str, Any]:
        replica = self._replica(name)
        result = self.repo.init_replica(replica)
        self.state.audit("admin", "replica.init", "storage_target", name, result)
        return result

    def init_replica_lock(self, name: str) -> dict[str, Any]:
        replica = self._replica(name)
        if replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled:
            result = ensure_r2_bucket_lock(replica, minimum_days=self.cfg.repository.retention.keep_within_days)
        elif replica.backend == "s3" and replica.object_lock_enabled:
            # S3 Object Lock must already be enabled at the bucket level. This confirms it;
            # object retention itself is applied after successful snapshot copies.
            from .storage import s3_preflight
            result = s3_preflight(replica)
        else:
            raise ValueError(f"replica {name} has no provider-side immutability configured")
        self.state.audit("admin", "replica.lock_init", "storage_target", name, result)
        return result

    def replica_lock_status(self, name: str) -> dict[str, Any]:
        replica = self._replica(name)
        if replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled:
            return r2_bucket_lock_status(replica)
        if replica.backend == "s3" and replica.object_lock_enabled:
            from .storage import s3_preflight
            return s3_preflight(replica)
        return {"enabled": False, "kind": "none"}

    def hold_recovery_point(self, snapshot_id: str, *, actor: str, days: int, reason: str = "manual hold") -> str:
        if days < 1 or days > 36500:
            raise ValueError("hold days must be between 1 and 36500")
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        saved = self.state.extend_immutability(snapshot_id, until, actor=actor, reason=reason)
        # Restic data packs are deduplicated and may be shared by many snapshots. For an S3
        # Object-Lock replica, conservatively extend persistent repository-object retention
        # rather than pretending a single snapshot can be isolated at object level.
        for replica in self.cfg.replicas:
            provider_lock = replica.enabled and replica.backend == "s3" and (
                replica.object_lock_enabled or (replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled)
            )
            if not provider_lock:
                continue
            copy = self.state.get_recovery_copy(snapshot_id, replica.name)
            if not copy or copy.get("status") != "success":
                continue
            try:
                if replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled:
                    lock = ensure_r2_bucket_lock(replica, minimum_days=days)
                else:
                    lock = apply_object_lock(replica, minimum_days=days)
                self.state.audit(actor, "replica.hold_extended", "storage_target", replica.name, {
                    "snapshot_id": snapshot_id, "requested_until": saved, "provider_lock": lock,
                })
            except Exception as exc:
                self.state.audit(actor, "replica.hold_extend_failed", "storage_target", replica.name, {
                    "snapshot_id": snapshot_id, "error": str(exc),
                })
        return saved

    def request_restore(
        self, *, snapshot_id: str, requester: str, target_platform: str, target_name: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> int:
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        target = self._platform(target_platform)
        if not target.enabled:
            raise ValueError("target platform is disabled")
        if target.type != point["platform_type"]:
            raise ValueError(
                f"cross-hypervisor restore from {point['platform_type']} to {target.type} is not enabled in the safe core; "
                "restore to the same hypervisor family or install a tested conversion plugin"
            )
        target_name = target_name or f"{point['vm_name']}-restore-{snapshot_id[:8]}"
        status = "pending_approval" if self.cfg.protection.require_four_eyes_restore else "ready"
        return self.state.create_restore_request(
            requester=requester,
            point=point,
            target_platform=target.name,
            target_name=target_name,
            options=options or {},
            status=status,
        )

    def approve_restore(self, request_id: int, approver: str) -> None:
        self.state.approve_restore(
            request_id, approver, four_eyes=self.cfg.protection.require_four_eyes_restore
        )

    def execute_restore(self, request_id: int, *, actor: str, dry_run: bool = False) -> dict[str, Any]:
        req = self.state.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        if dry_run:
            if req["status"] not in {"pending_approval", "approved", "ready"}:
                raise ValueError(f"restore request cannot be planned from status {req['status']}")
        point = self.state.get_point(req["snapshot_id"])
        if not point:
            raise RuntimeError("recovery point metadata missing")
        target_cfg = self._platform(req["target_platform"])
        adapter = build_adapter(target_cfg, self.cfg.runtime.command_timeout_seconds)
        options = json.loads(req.get("options_json") or "{}")
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
                "target_platform": target_cfg.name,
                "target_name": req["target_name"],
                "staging": str(restored_source),
                "source_repository": source_repository,
                "note": "execution will restore the encrypted snapshot to staging, validate its manifest, then import as a NEW VM",
            }
        self._ensure_path_capacity(self.cfg.runtime.restore_staging_path, action="restore")
        problems = adapter.doctor()
        if problems:
            self.state.audit(actor, "restore.preflight_failed", "restore_request", str(request_id), {"problems": problems})
            raise RuntimeError("restore target preflight failed: " + "; ".join(problems))
        req = self.state.start_restore(request_id, actor)
        try:
            shutil.rmtree(restore_root, ignore_errors=True)
            self.repo.restore(point["snapshot_id"], str(restore_root), replica=replica)
            if not restored_source.exists():
                raise RuntimeError(f"restic restore completed but expected source path is missing: {restored_source}")
            ok, errors = verify_manifest(restored_source, expected_digest=point.get("manifest_sha256"))
            if not ok:
                raise RuntimeError("recovery point manifest verification failed: " + "; ".join(errors[:10]))
            result = adapter.restore(restored_source, target_name=req["target_name"], options=options, dry_run=False)
            if isinstance(result, dict):
                result["source_repository"] = source_repository
            self.state.finish_restore(request_id, success=True, result=result)
            self.state.audit(actor, "restore.complete", "restore_request", str(request_id), result)
            shutil.rmtree(restore_root, ignore_errors=True)
            return result
        except Exception as exc:
            self.state.finish_restore(request_id, success=False, error=str(exc))
            self.state.audit(actor, "restore.failed", "restore_request", str(request_id), {"error": str(exc)})
            if not self.cfg.runtime.keep_staging_on_failure:
                shutil.rmtree(restore_root, ignore_errors=True)
            raise

    def verify_recovery_point(self, snapshot_id: str, *, actor: str = "system") -> bool:
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        self._ensure_path_capacity(self.cfg.runtime.verify_staging_path, action="verification")
        root = Path(self.cfg.runtime.verify_staging_path) / safe_component(snapshot_id)
        expected = root / str(point["source_path"]).lstrip("/")
        shutil.rmtree(root, ignore_errors=True)
        try:
            self.repo.restore(snapshot_id, str(root))
            if not expected.exists():
                self.state.mark_verified(snapshot_id, False, {"error": "restored path missing"})
                return False
            ok, errors = verify_manifest(expected, expected_digest=point.get("manifest_sha256"))
            self.state.mark_verified(snapshot_id, ok, {"errors": errors[:20]})
            self.state.audit(actor, "recovery.verify", "recovery_point", snapshot_id, {"success": ok, "errors": errors[:20]})
            return ok
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _replica(self, name: str) -> ReplicaConfig:
        for replica in self.cfg.replicas:
            if replica.name == name and replica.enabled:
                return replica
        raise ValueError(f"unknown or disabled replica: {name}")

    def _platform(self, name: str) -> PlatformConfig:
        for platform in self.cfg.platforms:
            if platform.name == name:
                return platform
        raise ValueError(f"unknown platform: {name}")
