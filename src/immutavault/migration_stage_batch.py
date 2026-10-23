"""Capped, fail-stop, sequential XCP-ng *powered-off* migration staging.

Batching reduces repetitive administrator commands, not raw copy duration.
There is NO final sync, source shutdown, target boot, parallel SAN copy or
production cutover in this module.
"""
from __future__ import annotations

from typing import Any

from .lock import exclusive_lock
from .migration_reporting import InvalidMigrationPlan, summarize_migration_plan
from .migration_stage import XCPNGMigrationStager


class StagingBatchBlocked(ValueError):
    pass


def stage_approved_batch(
    plan: dict[str, Any],
    requests: dict[str, Any],
    *,
    actor: str,
    engine: Any | None = None,
    execute: bool = False,
    max_jobs: int = 5,
) -> dict[str, Any]:
    if not isinstance(actor, str) or not actor.strip():
        raise StagingBatchBlocked("named operator required")
    if type(max_jobs) is not int or not 1 <= max_jobs <= 25:
        raise StagingBatchBlocked("max_jobs must be an integer from 1 to 25")
    try:
        summary = summarize_migration_plan(plan)
    except InvalidMigrationPlan as exc:
        raise StagingBatchBlocked(str(exc)) from exc
    if summary["status"] != "READY_FOR_OPERATOR_REVIEW":
        raise StagingBatchBlocked("blocked plan cannot stage VMs")
    if plan.get("source_family") != "xcpng" or plan.get("destination_family") != "xcpng":
        raise StagingBatchBlocked("batch staging currently supports XCP-ng-to-XCP-ng only")
    if not isinstance(requests, dict) or not isinstance(requests.get("requests"), list):
        raise StagingBatchBlocked("requests must be a JSON object containing a requests array")
    entries = requests["requests"]
    if not entries:
        raise StagingBatchBlocked("requests array is empty")
    allowed = {r["source_vm_id"]: r for r in plan["vms"]}
    seen_vm, seen_request = set(), set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise StagingBatchBlocked(f"requests[{i}] must be an object")
        vid, rid = entry.get("source_vm_id"), entry.get("request_id")
        if not isinstance(vid, str) or vid not in allowed or vid in seen_vm:
            raise StagingBatchBlocked("request VM ID is missing, duplicated or outside approved plan")
        if type(rid) is not int or rid <= 0 or rid in seen_request:
            raise StagingBatchBlocked("request IDs must be positive and unique")
        seen_vm.add(vid)
        seen_request.add(rid)

    selected = entries[:max_jobs]
    if not execute:
        return {
            "dry_run": True, "plan_sha256": plan["plan_sha256"],
            "selected_requests": selected,
            "remaining_requests": len(entries) - len(selected),
            "hypervisor_changes": False,
            "approved_to_execute": False,
            "warning": "Only independently approved requests can be staged; VMs remain halted",
        }
    if engine is None:
        raise StagingBatchBlocked("authenticated Immutavault engine required for staging")
    lock_path = str(engine.cfg.repository.lock_file) + ".migration-stage-batch"
    with exclusive_lock(lock_path):
        stager = XCPNGMigrationStager(engine, plan)
        # Preflight every selected request before the first import, minimizing
        # partial batches caused by expired approval or wrong target mapping.
        for entry in selected:
            req, row = stager._row_for_request(entry["request_id"])
            if row["source_vm_id"] != entry["source_vm_id"]:
                raise StagingBatchBlocked("request ID is bound to a different VM")
            stager._validate_live_destination(row)

        staged = []
        failed = None
        for entry in selected:
            try:
                result = stager.stage(entry["request_id"], actor=actor, execute=True)
                if result.get("status") != "STAGED_POWERED_OFF":
                    raise StagingBatchBlocked("target was not confirmed powered off")
                staged.append({
                    "source_vm_id": entry["source_vm_id"],
                    "request_id": entry["request_id"],
                    "destination_vm_uuid": result["destination_vm_uuid"],
                    "status": "STAGED_POWERED_OFF",
                })
            except Exception as exc:
                failed = {"source_vm_id": entry["source_vm_id"],
                          "request_id": entry["request_id"],
                          "error_type": type(exc).__name__}
                break
        return {
            "status": "PARTIAL_FAILURE" if failed else
                      "MORE_TO_STAGE" if len(entries) > len(staged) else "STAGED_BATCH",
            "staged": staged,
            "failed": failed,
            "remaining_requests": len(entries) - len(staged),
            "hypervisor_changes": bool(staged) or bool(failed),
            "source_vms_unchanged": True,
            "production_cutover_performed": False,
            "warning": "Partial targets require operator inspection; do not boot or blindly re-run",
        }
