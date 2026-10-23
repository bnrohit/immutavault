"""Deterministic, bandwidth-aware migration wave scheduling.

This module plans *powered-off restore staging* only. It never connects to a
hypervisor, modifies a VM, or makes a downtime/zero-data-loss promise. The
network budget is shared by all concurrent jobs, not multiplied per worker.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from math import ceil
from typing import Any

from .migration_reporting import InvalidMigrationPlan, summarize_migration_plan


class MigrationOptimizationError(ValueError):
    """Unsafe schedule input or a blocked/altered migration plan."""


def _positive(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise MigrationOptimizationError(f"{label} must be an integer from 1 to {maximum}")
    return value


def _vm_resources(row: dict[str, Any]) -> tuple[int, str, tuple[str, ...]]:
    disks = row.get("disks")
    host = row.get("destination_host")
    if not isinstance(disks, list) or not disks:
        raise MigrationOptimizationError("planned VM is missing disk layout")
    if not isinstance(host, str) or not host:
        raise MigrationOptimizationError("planned VM has no destination host")
    sr_ids = []
    bytes_total = 0
    for disk in disks:
        if not isinstance(disk, dict) or disk.get("valid") is not True:
            raise MigrationOptimizationError("planned VM contains unverified disk mapping")
        sr = disk.get("target_sr")
        size = disk.get("size_bytes")
        if not isinstance(sr, str) or not sr or type(size) is not int or size <= 0:
            raise MigrationOptimizationError("planned VM contains invalid storage/size")
        sr_ids.append(sr)
        bytes_total += size
    return bytes_total, host, tuple(sorted(set(sr_ids)))


def optimize_migration_plan(
    plan: dict[str, Any],
    *,
    effective_mib_per_second: int = 100,
    max_parallel: int = 2,
    max_per_host: int = 1,
    max_per_storage: int = 1,
    stage_overhead_minutes: int = 15,
    priorities: dict[str, int] | None = None,
    dependencies: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Produce capacity-aware waves; default does not overload a shared SR.

    Dependencies enforce wave order. Priorties are 0 (low) to 3 (urgent).
    All estimates use full *provisioned* disk bytes and shared throughput.
    Execution remains disabled and must recheck source and target live.
    """
    for key, val, hi in (
        ("effective_mib_per_second", effective_mib_per_second, 100000),
        ("max_parallel", max_parallel, 16),
        ("max_per_host", max_per_host, 8),
        ("max_per_storage", max_per_storage, 8),
    ):
        _positive(val, key, hi)
    if type(stage_overhead_minutes) is not int or not 0 <= stage_overhead_minutes <= 1440:
        raise MigrationOptimizationError("stage_overhead_minutes must be integer 0..1440")
    try:
        review = summarize_migration_plan(plan)  # validates plan SHA-256
    except InvalidMigrationPlan as exc:
        raise MigrationOptimizationError(str(exc)) from exc
    if review["status"] != "READY_FOR_OPERATOR_REVIEW":
        raise MigrationOptimizationError("migration plan is blocked; resolve readiness checks first")
    if plan.get("approved_to_execute") is not False:
        raise MigrationOptimizationError("plan execution field is unexpected")
    if plan.get("source_family") != plan.get("destination_family"):
        raise MigrationOptimizationError("cross-family staging is not supported by this scheduler")

    rows: dict[str, dict[str, Any]] = {}
    for row in plan["vms"]:
        vm_id = row.get("source_vm_id")
        if not isinstance(vm_id, str) or not vm_id or vm_id in rows:
            raise MigrationOptimizationError("VM identifiers must be unique and nonempty")
        if row.get("ready_for_operator_review") is not True:
            raise MigrationOptimizationError("cannot schedule a VM that failed preflight")
        nbytes, host, srs = _vm_resources(row)
        rows[vm_id] = {
            "source_vm_id": vm_id, "source_name": row.get("source_name"),
            "target_name": row.get("target_name"),
            "destination_host": host, "target_storage": list(srs),
            "logical_disk_bytes": nbytes,
            "snapshot_id": row.get("snapshot_id"),
        }
    if not rows:
        raise MigrationOptimizationError("plan contains no VMs")

    priorities = priorities or {}
    dependencies = dependencies or {}
    if not isinstance(priorities, dict) or not isinstance(dependencies, dict):
        raise MigrationOptimizationError("priorities and dependencies must be JSON objects")
    if set(priorities) - rows.keys() or set(dependencies) - rows.keys():
        raise MigrationOptimizationError("priority or dependency references unknown source VM")
    for vm_id, pri in priorities.items():
        if type(pri) is not int or not 0 <= pri <= 3:
            raise MigrationOptimizationError(f"priority for {vm_id} must be integer 0..3")
    edges = {}
    for vm_id, parents in dependencies.items():
        if not isinstance(parents, list) or any(not isinstance(x, str) for x in parents):
            raise MigrationOptimizationError(f"dependencies for {vm_id} must be VM ID strings")
        if len(parents) != len(set(parents)) or vm_id in parents or set(parents) - rows.keys():
            raise MigrationOptimizationError(f"invalid, repeated or missing VM dependency for {vm_id}")
        edges[vm_id] = set(parents)

    rate = effective_mib_per_second * 2**20  # total sustained bandwidth, not per VM
    overhead = stage_overhead_minutes * 60
    remaining = set(rows)
    waves: list[dict[str, Any]] = []
    serial_seconds = sum(ceil(r["logical_disk_bytes"] / rate) + overhead for r in rows.values())
    while remaining:
        # Even with an unready parent in a previous wave, no member may share
        # the *same* wave with a dependent. These are operator stages, not boots.
        available = [vid for vid in remaining if not (edges.get(vid, set()) & remaining)]
        if not available:
            raise MigrationOptimizationError("dependency cycle prevents safe wave ordering")
        available.sort(key=lambda vid: (
            -priorities.get(vid, 1), rows[vid]["logical_disk_bytes"], vid))
        host_use: Counter[str] = Counter()
        sr_use: Counter[str] = Counter()
        chosen = []
        for vid in available:
            row = rows[vid]
            if len(chosen) >= max_parallel:
                break
            if host_use[row["destination_host"]] >= max_per_host:
                continue
            if any(sr_use[sr] >= max_per_storage for sr in row["target_storage"]):
                continue
            chosen.append(vid)
            host_use[row["destination_host"]] += 1
            sr_use.update(row["target_storage"])
        if not chosen:
            raise MigrationOptimizationError("unable to schedule even one VM")
        total_bytes = sum(rows[vid]["logical_disk_bytes"] for vid in chosen)
        # Transfers contend for one shared bottleneck. Parallelism can
        # overlap job setup, but does NOT multiply shared bandwidth.
        duration = ceil(total_bytes / rate) + overhead
        waves.append({
            "wave": len(waves) + 1,
            "source_vm_ids": chosen,
            "vms": [rows[vid] for vid in chosen],
            "logical_disk_bytes": total_bytes,
            "illustrative_stage_seconds": duration,
            "requires_independent_approved_request_per_vm": True,
            "execution_enabled": False,
        })
        remaining.difference_update(chosen)

    wave_seconds = sum(w["illustrative_stage_seconds"] for w in waves)
    canonical = {
        "plan_sha256": plan["plan_sha256"],
        "effective_mib_per_second": effective_mib_per_second,
        "max_parallel": max_parallel,
        "max_per_host": max_per_host,
        "max_per_storage": max_per_storage,
        "stage_overhead_minutes": stage_overhead_minutes,
        "priorities": priorities,
        "dependencies": dependencies,
        "waves": waves,
    }
    digest = sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": 1,
        "source_plan_sha256": plan["plan_sha256"],
        "schedule_sha256": digest,
        "mode": "READ_ONLY_RESTORE_STAGING_WAVES",
        "ready_for_operator_review": True,
        "approved_to_execute": False,
        "total_vms": len(rows), "total_waves": len(waves),
        "total_logical_disk_bytes": sum(r["logical_disk_bytes"] for r in rows.values()),
        "effective_mib_per_second": effective_mib_per_second,
        "max_parallel": max_parallel,
        "max_per_host": max_per_host,
        "max_per_storage": max_per_storage,
        "estimated_current_sequential_stage_seconds": serial_seconds,
        "hypothetical_parallel_wave_seconds": wave_seconds,
        "hypothetical_overhead_savings_seconds": max(0, serial_seconds - wave_seconds),
        "parallel_stage_execution_implemented": False,
        "waves": waves,
        "warnings": [
            "No VMs were moved. This is an advisory order for powered-off staging only.",
            "Current batch staging is sequential. Parallel-wave durations and time savings are hypothetical future targets, NOT current product performance.",
            "Throughput is a shared effective budget. Logical disk size is NOT a measured transfer size.",
            "Times exclude export, encryption, backup generation, application verification, retries and cutover.",
            "No source shutdown, final delta sync, target production boot or rollback is implemented.",
            "Each VM still requires its own independently approved restore request and live preflight.",
        ],
    }
