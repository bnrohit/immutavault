from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import time
from typing import Any

from .adapters import build_adapter
from .dr_config import DRVMConfig
from .dr_network import DRNetworkManager
from .health import run_health_checks
from .runner import run


class DROrchestrator:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.cfg = engine.cfg.dr
        self.net = DRNetworkManager(self.cfg, timeout=min(600, engine.cfg.runtime.command_timeout_seconds))

    def _enabled(self) -> None:
        if not self.cfg.enabled:
            raise RuntimeError("disaster_recovery.enabled is false")

    def _platform(self, name: str):
        return self.engine._platform(name)

    def _replica(self):
        return self.engine._replica(self.cfg.replica)

    def _latest_point(self, workload: DRVMConfig) -> dict[str, Any] | None:
        for point in self.engine.state.list_points(platform=workload.source_platform, limit=1000):
            if point["vm_name"] == workload.name:
                return point
        return None

    def _rpo_minutes(self, created_at: str) -> int:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - created).total_seconds() // 60))

    def plan(self) -> dict[str, Any]:
        self._enabled()
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for workload in sorted(self.cfg.workloads, key=lambda x: (x.boot_order, x.name)):
            point = self._latest_point(workload)
            src = self._platform(workload.source_platform); dst = self._platform(workload.target_platform)
            row: dict[str, Any] = {
                "workload": workload.name, "source_platform": src.name, "target_platform": dst.name,
                "source_type": src.type, "target_type": dst.type, "boot_order": workload.boot_order,
            }
            if src.type != dst.type:
                msg = f"{workload.name}: cross-hypervisor automatic DR is blocked ({src.type} -> {dst.type}); configure the same hypervisor family or a tested conversion plugin"
                errors.append(msg); row["error"] = msg; rows.append(row); continue
            if not point:
                msg = f"{workload.name}: no recovery point found on {src.name}"; errors.append(msg); row["error"] = msg; rows.append(row); continue
            copy = self.engine.state.get_recovery_copy(str(point["snapshot_id"]), self.cfg.replica)
            rpo = self._rpo_minutes(str(point["created_at"]))
            row.update({
                "snapshot_id": point["snapshot_id"], "created_at": point["created_at"], "rpo_minutes": rpo,
                "verified": bool(point["verified"]), "suspicious": bool(point["suspicious"]),
                "replica_ready": bool(copy and copy.get("status") == "success"),
                "replica": self.cfg.replica,
            })
            if not point["verified"]:
                errors.append(f"{workload.name}: latest recovery point is not verified")
            if point["suspicious"]:
                errors.append(f"{workload.name}: latest recovery point is anomaly-flagged")
            if rpo > self.cfg.rpo_max_minutes:
                errors.append(f"{workload.name}: latest point is {rpo} minutes old; DR RPO limit is {self.cfg.rpo_max_minutes}")
            if not row["replica_ready"]:
                errors.append(f"{workload.name}: latest point is not present on DR replica {self.cfg.replica}")
            rows.append(row)
        return {
            "ready": not errors, "primary_site": self.cfg.primary_site, "dr_site": self.cfg.dr_site,
            "replica": self.cfg.replica, "rpo_max_minutes": self.cfg.rpo_max_minutes,
            "network": {"primary": self.net.plan(self.cfg.primary_site), "dr": self.net.plan(self.cfg.dr_site)},
            "workloads": rows, "errors": errors,
        }

    def preflight(self, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate the recovery source, DR gateway and target hypervisors before fencing primary."""
        self._enabled()
        plan = plan or self.plan()
        problems: list[str] = list(plan.get("errors", []))
        details: dict[str, Any] = {"plan_ready": bool(plan.get("ready"))}
        try:
            replica = self._replica()
            health = self.engine.repo.replica_health(replica)
            details["replica"] = health
            if not health.get("ok"):
                problems.extend(f"DR replica: {x}" for x in health.get("problems", []))
        except Exception as exc:
            details["replica"] = {"ok": False, "error": str(exc)}
            problems.append(f"DR replica preflight failed: {exc}")
        target_results: dict[str, list[str]] = {}
        for name in sorted({w.target_platform for w in self.cfg.workloads}):
            try:
                platform = self._platform(name)
                issues = build_adapter(platform, self.engine.cfg.runtime.command_timeout_seconds).doctor()
            except Exception as exc:
                issues = [str(exc)]
            target_results[name] = issues
            problems.extend(f"target {name}: {issue}" for issue in issues)
        details["targets"] = target_results
        try:
            network = self.net.preflight(self.cfg.dr_site)
            details["dr_gateway"] = network
            if not network.get("ok"):
                problems.append(f"DR gateway preflight failed: {network.get('error') or 'unreachable or missing prerequisites'}")
        except Exception as exc:
            details["dr_gateway"] = {"ok": False, "error": str(exc)}
            problems.append(f"DR gateway preflight failed: {exc}")
        return {"ok": not problems, "problems": problems, "details": details}

    def maintenance(self, action: str = "status", *, actor: str = "operator") -> dict[str, Any]:
        self._enabled()
        path = Path(self.cfg.maintenance_file)
        action = action.lower()
        if action not in {"on", "off", "status"}:
            raise ValueError("DR maintenance action must be on, off, or status")
        if action == "on":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
            self.engine.state.audit(actor, "dr.maintenance.on", "site", self.cfg.primary_site, {"file": str(path)})
        elif action == "off":
            path.unlink(missing_ok=True)
            self.engine.state.audit(actor, "dr.maintenance.off", "site", self.cfg.primary_site, {"file": str(path)})
        return {"maintenance": path.exists(), "file": str(path)}

    def sync(self) -> dict[str, Any]:
        self._enabled(); replica = self._replica(); copied: list[dict[str, Any]] = []; errors: list[str] = []
        for workload in self.cfg.workloads:
            point = self._latest_point(workload)
            if not point:
                errors.append(f"{workload.name}: no recovery point"); continue
            if point["suspicious"]:
                errors.append(f"{workload.name}: latest point is suspicious and was not promoted to DR"); continue
            existing = self.engine.state.get_recovery_copy(str(point["snapshot_id"]), replica.name)
            if existing and existing.get("status") == "success":
                copied.append({"workload": workload.name, "snapshot_id": point["snapshot_id"], "status": "already-present"}); continue
            try:
                result = self.engine.repo.copy_snapshot(str(point["snapshot_id"]), replica, immutable_days=self.engine.cfg.repository.retention.keep_within_days)
                self.engine.state.upsert_recovery_copy(snapshot_id=str(point["snapshot_id"]), target_name=replica.name, backend=replica.backend, provider=replica.provider if replica.backend == "s3" else None, repository=result.get("repository", ""), status="success", immutable_until=point["immutable_until"], object_lock=result.get("object_lock") or {})
                copied.append({"workload": workload.name, "snapshot_id": point["snapshot_id"], "status": "copied", "result": result})
            except Exception as exc:
                errors.append(f"{workload.name}: {exc}")
        self.engine.state.audit("dr-sync", "dr.sync", "dr_replica", replica.name, {"copied": copied, "errors": errors})
        return {"ok": not errors, "replica": replica.name, "workloads": copied, "errors": errors}

    def _run_fence_command(self, env_name: str | None, *, label: str) -> None:
        if not env_name:
            raise RuntimeError(f"{label} command environment variable is not configured")
        command = os.getenv(env_name)
        if not command:
            raise RuntimeError(f"{label} command environment variable {env_name} is empty")
        result = run(shlex.split(command), timeout=300, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{label} command failed: {result.stderr.strip()}")

    def _fence_primary(self, *, manual_confirmed: bool, automatic: bool) -> None:
        fence = self.cfg.fence
        if fence.mode == "command":
            self._run_fence_command(fence.command_env, label="primary fence")
            if fence.verify_command_env:
                self._run_fence_command(fence.verify_command_env, label="primary fence verification")
            self.engine.state.audit("dr-orchestrator", "dr.fence", "site", self.cfg.primary_site, {"mode": "command"})
            return
        if automatic:
            raise RuntimeError("automatic DR promotion requires fence.mode=command; manual fencing cannot safely auto-failover")
        if not manual_confirmed:
            raise RuntimeError("promotion requires --confirm-primary-fenced after the primary workloads/network have been isolated")
        self.engine.state.audit("dr-orchestrator", "dr.fence", "site", self.cfg.primary_site, {"mode": "manual-confirmation"})

    def _wait_workload_health(self, workload: DRVMConfig) -> dict[str, Any]:
        if not workload.health_checks:
            return {"ok": True, "checks": [], "note": "no workload health checks configured"}
        attempts = max(x.attempts for x in workload.health_checks)
        interval = min(x.interval_seconds for x in workload.health_checks)
        last: dict[str, Any] = {"ok": False, "checks": []}
        for attempt in range(1, attempts + 1):
            last = run_health_checks(workload.health_checks)
            last["attempt"] = attempt
            if last["ok"]:
                return last
            if attempt < attempts:
                time.sleep(interval)
        return last

    def _already_failed_over(self) -> bool:
        failover = self.engine.state.latest_dr_run(direction="failover", status="success")
        failback = self.engine.state.latest_dr_run(direction="failback", status="success")
        return bool(failover and (not failback or str(failover["started_at"]) > str(failback["started_at"])))

    def promote(self, *, execute: bool = False, confirm_primary_fenced: bool = False, automatic: bool = False) -> dict[str, Any]:
        self._enabled(); plan = self.plan()
        if not execute:
            return {"mode": "plan", **plan}
        if self._already_failed_over():
            return {"status": "already-failed-over", "message": "a successful DR failover is already active; fail back or demote it before promoting again"}
        if not plan["ready"]:
            raise RuntimeError("DR promotion blocked: " + "; ".join(plan["errors"]))
        preflight = self.preflight(plan)
        if not preflight["ok"]:
            raise RuntimeError("DR promotion preflight failed before fencing primary: " + "; ".join(preflight["problems"]))
        self._fence_primary(manual_confirmed=confirm_primary_fenced, automatic=automatic)
        run_id = self.engine.state.create_dr_run(direction="failover", source_site=self.cfg.primary_site, target_site=self.cfg.dr_site, details={"replica": self.cfg.replica})
        restored: list[tuple[DRVMConfig, Any, dict[str, Any]]] = []
        try:
            self.net.prepare(self.cfg.dr_site)
            self.net.deactivate(self.cfg.primary_site, tolerate_unreachable=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for workload, row in zip(sorted(self.cfg.workloads, key=lambda x: (x.boot_order, x.name)), plan["workloads"]):
                target_name = workload.target_name or f"{workload.name}-dr-{stamp}"
                point = self.engine.state.get_point(str(row["snapshot_id"]))
                assert point is not None
                options = dict(workload.restore_options); options["source_repository"] = self.cfg.replica
                rid = self.engine.state.create_restore_request(requester="dr-orchestrator", point=point, target_platform=workload.target_platform, target_name=target_name, options=options, status="ready")
                result = self.engine.execute_restore(rid, actor="dr-orchestrator")
                adapter = build_adapter(self._platform(workload.target_platform), self.engine.cfg.runtime.command_timeout_seconds)
                restored.append((workload, adapter, result))
                self.engine.state.add_dr_workload(run_id=run_id, workload_name=workload.name, snapshot_id=str(row["snapshot_id"]), source_platform=workload.source_platform, target_platform=workload.target_platform, target_name=target_name, status="restored", result=result)
            self.net.activate(self.cfg.dr_site)
            health: dict[str, Any] = {}
            for workload, adapter, result in restored:
                adapter.power_on(result)
                check = self._wait_workload_health(workload); health[workload.name] = check
                if not check["ok"]:
                    raise RuntimeError(f"DR workload {workload.name} failed health checks")
            details = {"replica": self.cfg.replica, "health": health, "workloads": [x[0].name for x in restored]}
            self.engine.state.finish_dr_run(run_id, success=True, details=details)
            self.engine.state.audit("dr-orchestrator", "dr.failover.complete", "dr_run", str(run_id), details)
            return {"status": "success", "run_id": run_id, **details}
        except Exception as exc:
            self.net.deactivate(self.cfg.dr_site, tolerate_unreachable=True)
            for workload, adapter, result in reversed(restored):
                try: adapter.power_off(result)
                except Exception: pass
            self.engine.state.finish_dr_run(run_id, success=False, error=str(exc), details={"restored": [x[0].name for x in restored]})
            self.engine.state.audit("dr-orchestrator", "dr.failover.failed", "dr_run", str(run_id), {"error": str(exc)})
            raise

    def demote_dr(self, *, execute: bool = False) -> dict[str, Any]:
        self._enabled(); last = self.engine.state.latest_dr_run(direction="failover", status="success")
        if not last:
            return {"status": "nothing-to-demote"}
        workloads = self.engine.state.list_dr_workloads(int(last["id"]))
        plan = {"run_id": last["id"], "workloads": workloads, "network": self.net.deactivate(self.cfg.dr_site, dry_run=True)}
        if not execute:
            return {"mode": "plan", **plan}
        self.net.deactivate(self.cfg.dr_site, tolerate_unreachable=False)
        by_name = {x.name: x for x in self.cfg.workloads}
        for item in reversed(workloads):
            cfg = by_name.get(item["workload_name"])
            if not cfg: continue
            adapter = build_adapter(self._platform(cfg.target_platform), self.engine.cfg.runtime.command_timeout_seconds)
            adapter.power_off(item["result"])
        self.engine.state.audit("dr-orchestrator", "dr.demote", "site", self.cfg.dr_site, {"failover_run": last["id"]})
        return {"status": "demoted", **plan}

    def failback(self, *, execute: bool = False, confirm_primary_isolated: bool = False) -> dict[str, Any]:
        self._enabled(); last = self.engine.state.latest_dr_run(direction="failover", status="success")
        if not last or not self._already_failed_over():
            return {"status": "no-active-failover"}
        workloads = self.engine.state.list_dr_workloads(int(last["id"]))
        plan = {"source_run": last["id"], "workloads": workloads, "target_site": self.cfg.primary_site, "note": "final DR backups are taken, restored powered-off to primary, DR route/workloads are withdrawn, then primary route/workloads are activated"}
        if not execute:
            return {"mode": "plan", **plan}
        if not confirm_primary_isolated:
            raise RuntimeError("failback requires --confirm-primary-isolated: original primary workloads must be powered off/isolated before same-IP recovery")
        run_id = self.engine.state.create_dr_run(direction="failback", source_site=self.cfg.dr_site, target_site=self.cfg.primary_site, details={"source_failover_run": last["id"]})
        by_name = {x.name: x for x in self.cfg.workloads}; primary_restored: list[tuple[DRVMConfig, Any, dict[str, Any]]] = []
        try:
            self.net.prepare(self.cfg.primary_site); self.net.deactivate(self.cfg.primary_site, tolerate_unreachable=False)
            final_points: dict[str, str] = {}
            for item in workloads:
                workload = by_name[item["workload_name"]]; dr_platform = self._platform(workload.target_platform); adapter = build_adapter(replace(dr_platform, include=["*"], exclude=[]), self.engine.cfg.runtime.command_timeout_seconds)
                match = next((vm for vm in adapter.inventory() if vm.name == item["target_name"]), None)
                if not match: raise RuntimeError(f"cannot find active DR VM {item['target_name']} for final failback backup")
                try: info = adapter.platform_info()
                except Exception as exc: info = {"error": str(exc)}
                result = self.engine._backup_vm(dr_platform, adapter, match, platform_info=info, dry_run=False)
                if result.get("status") != "success": raise RuntimeError(f"final backup failed for {workload.name}: {result.get('error')}")
                final_points[workload.name] = str(result["snapshot"])
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for workload in sorted(self.cfg.workloads, key=lambda x: (x.boot_order, x.name)):
                point = self.engine.state.get_point(final_points[workload.name]); assert point is not None
                target_name = f"{workload.name}-failback-{stamp}"
                rid = self.engine.state.create_restore_request(requester="dr-orchestrator", point=point, target_platform=workload.source_platform, target_name=target_name, options=dict(workload.restore_options), status="ready")
                result = self.engine.execute_restore(rid, actor="dr-orchestrator")
                adapter = build_adapter(self._platform(workload.source_platform), self.engine.cfg.runtime.command_timeout_seconds)
                primary_restored.append((workload, adapter, result))
                self.engine.state.add_dr_workload(run_id=run_id, workload_name=workload.name, snapshot_id=point["snapshot_id"], source_platform=workload.target_platform, target_platform=workload.source_platform, target_name=target_name, status="restored", result=result)
            self.net.deactivate(self.cfg.dr_site, tolerate_unreachable=False)
            for item in reversed(workloads):
                workload = by_name[item["workload_name"]]; adapter = build_adapter(self._platform(workload.target_platform), self.engine.cfg.runtime.command_timeout_seconds); adapter.power_off(item["result"])
            self.net.activate(self.cfg.primary_site)
            health: dict[str, Any] = {}
            for workload, adapter, result in primary_restored:
                adapter.power_on(result); check = self._wait_workload_health(workload); health[workload.name] = check
                if not check["ok"]: raise RuntimeError(f"failback workload {workload.name} failed health checks")
            details={"source_failover_run":last["id"],"health":health,"final_snapshots":final_points}
            self.engine.state.finish_dr_run(run_id,success=True,details=details); self.engine.state.audit("dr-orchestrator","dr.failback.complete","dr_run",str(run_id),details)
            return {"status":"success","run_id":run_id,**details}
        except Exception as exc:
            self.engine.state.finish_dr_run(run_id,success=False,error=str(exc)); self.engine.state.audit("dr-orchestrator","dr.failback.failed","dr_run",str(run_id),{"error":str(exc)}); raise

    def watch(self) -> dict[str, Any]:
        self._enabled()
        maintenance = Path(self.cfg.maintenance_file).exists()
        if maintenance:
            return {
                "maintenance": True,
                "maintenance_file": self.cfg.maintenance_file,
                "auto_failover": self.cfg.auto_failover,
                "action": "suppressed-maintenance",
            }
        # The systemd evaluator wakes once per minute. Respect larger configured
        # intervals so a five-minute policy cannot accidentally fail over in five minutes
        # merely because the timer itself runs every minute.
        previous = self.engine.state.get_dr_health("primary-site")
        if self.cfg.check_interval_seconds > 60 and previous and previous.get("checked_at"):
            try:
                checked = datetime.fromisoformat(str(previous["checked_at"]))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - checked).total_seconds()
                if age < self.cfg.check_interval_seconds:
                    return {
                        "action": "interval-not-due",
                        "next_check_in_seconds": max(1, int(self.cfg.check_interval_seconds - age)),
                        "health": previous,
                        "auto_failover": self.cfg.auto_failover,
                    }
            except ValueError:
                pass
        checks = run_health_checks(self.cfg.primary_probes)
        results = checks.get("checks", [])
        failed = sum(1 for item in results if not item.get("ok"))
        required = self.cfg.primary_failure_quorum or max(1, len(results))
        site_healthy = failed < required
        detail = {
            **checks,
            "failed_probes": failed,
            "failure_quorum": required,
            "site_healthy": site_healthy,
        }
        state = self.engine.state.update_dr_health("primary-site", healthy=site_healthy, detail=detail)
        result: dict[str, Any] = {
            "health": state,
            "probes": detail,
            "auto_failover": self.cfg.auto_failover,
            "threshold": self.cfg.failure_threshold,
            "check_interval_seconds": self.cfg.check_interval_seconds,
        }
        if site_healthy or state["consecutive_failures"] < self.cfg.failure_threshold or not self.cfg.auto_failover:
            return result
        if self._already_failed_over():
            result["action"] = "already-failed-over"
            return result
        promoted = self.promote(execute=True, automatic=True)
        result["action"] = "promoted"
        result["promotion"] = promoted
        return result
