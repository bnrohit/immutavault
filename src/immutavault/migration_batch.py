"""Idempotent, explicitly opted-in bulk *request creation* for XCP-ng staging.

Never approve, import, boot or cut over VMs. Every VM still has an independent
four-eyes restore request and all existing engine-side identity/policy checks.
"""
from __future__ import annotations

import json
from typing import Any

from .lock import exclusive_lock
from .migration_reporting import InvalidMigrationPlan, summarize_migration_plan
from .migration_workflow import create_restore_request


class MigrationBatchError(ValueError):
    pass


def create_migration_requests(
    plan: dict[str, Any], *,
    requester: str,
    engine: Any | None = None,
    execute: bool = False,
    max_new_requests: int = 25,
) -> dict[str, Any]:
    """Idempotent request batch; partial success is reported, never rolled back."""
    if not isinstance(requester, str) or not requester.strip():
        raise MigrationBatchError("named requester is required")
    if type(max_new_requests) is not int or not 1 <= max_new_requests <= 100:
        raise MigrationBatchError("max_new_requests must be an integer 1..100")
    try:
        report = summarize_migration_plan(plan)
    except InvalidMigrationPlan as exc:
        raise MigrationBatchError(str(exc)) from exc
    if report["status"] != "READY_FOR_OPERATOR_REVIEW":
        raise MigrationBatchError("cannot create requests from a blocked plan")
    if plan.get("source_family") != "xcpng" or plan.get("destination_family") != "xcpng":
        raise MigrationBatchError("bulk requests currently support XCP-ng-to-XCP-ng only")
    vm_rows = plan.get("vms", [])
    seen = set()
    for row in vm_rows:
        source_id = row.get("source_vm_id")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            raise MigrationBatchError("plan contains duplicate or invalid VM IDs")
        seen.add(source_id)
    if not execute:
        return {
            "dry_run": True, "plan_sha256": plan["plan_sha256"],
            "total_vms": len(vm_rows),
            "request_limit_per_run": max_new_requests,
            "planned_requests": [{
                "source_vm_id": row["source_vm_id"],
                "name": row["source_name"],
                "target_name": row["target_name"],
                "snapshot_id": row["snapshot_id"],
            } for row in vm_rows],
            "changes_made": False,
            "approval_to_execute": False,
            "next": "Use --execute-requests to create individual pending four-eyes requests",
        }
    if engine is None:
        raise MigrationBatchError("authenticated Immutavault engine required for writes")
    if not getattr(engine.cfg.protection, "require_four_eyes_restore", False):
        raise MigrationBatchError("separate four-eyes approval must be enabled")
    lock_file = str(engine.cfg.repository.lock_file) + ".migration-requests"
    with exclusive_lock(lock_file):
        existing = engine.state.list_restore_requests(limit=2000)
        if len(existing) >= 2000:
            raise MigrationBatchError("restore request catalog is too large to safely deduplicate")
        target_platform = plan["destination_platform"]
        pending = []
        skipped = []
        for row in vm_rows:
            matching = [req for req in existing
                        if req.get("target_platform") == target_platform
                        and req.get("target_name") == row["target_name"]]
            if matching:
                if len(matching) != 1:
                    raise MigrationBatchError("duplicate destination request names already exist")
                req = matching[0]
                try:
                    options = json.loads(req.get("options_json") or "{}")
                except (ValueError, TypeError) as exc:
                    raise MigrationBatchError("existing request has invalid options") from exc
                if (not isinstance(options, dict) or
                    options.get("migration_plan_sha256") != plan["plan_sha256"] or
                    req.get("snapshot_id") != row["snapshot_id"]):
                    raise MigrationBatchError("destination request belongs to another plan or snapshot")
                skipped.append({"source_vm_id": row["source_vm_id"],
                                "request_id": req.get("id"),
                                "status": req.get("status")})
                continue
            # Prevalidate the ENTIRE selected batch before any new request is created.
            point = engine.state.get_point(row["snapshot_id"])
            if (not point or point.get("vm_id") != row["source_vm_id"]
                or point.get("platform") != plan["source_platform"]
                or not point.get("verified") or point.get("suspicious")):
                raise MigrationBatchError("batch contains an unavailable or unverified source recovery point")
            pending.append(row)
        created = []
        failed = None
        for row in pending[:max_new_requests]:
            try:
                result = create_restore_request(
                    plan, row["source_vm_id"], engine=engine, requester=requester)
            except Exception as exc:
                failed = {"source_vm_id": row["source_vm_id"],
                          "error_type": type(exc).__name__}
                break  # earlier requests are retained for safe idempotent reruns
            created.append({"source_vm_id": row["source_vm_id"],
                            "request_id": result["request_id"],
                            "state": "pending_independent_approval"})
        return {
            "status": "PARTIAL_FAILURE" if failed else
                      "MORE_TO_REQUEST" if len(pending) > len(created) else "REQUESTED",
            "plan_sha256": plan["plan_sha256"],
            "created": created, "already_exists": skipped,
            "remaining_new_requests": len(pending) - len(created),
            "failed": failed, "approval_to_execute": False,
            "hypervisor_changes": False,
            "next": "Each request must be reviewed and approved separately; staging remains powered off",
        }
