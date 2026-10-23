"""Migration CLI with an explicit read-only default and existing four-eyes gate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .management_config import load_v11_config
from .migration_discovery import discover
from .migration_workflow import read_json, plan_migration, create_restore_request


COMMANDS = {"migration-discover", "migration-plan", "migration-request", "migration-stage", "migration-summary", "migration-optimize", "migration-request-batch"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="immutavault", description="Cluster migration planning (safe defaults)")
    p.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    sub = p.add_subparsers(dest="command", required=True)
    opt = sub.add_parser("migration-optimize", help="dry-run migration stage waves, shared bandwidth and bottlenecks")
    opt.add_argument("--plan", required=True, help="immutable, reviewed plan JSON")
    opt.add_argument("--output", help="new JSON output; existing files are never overwritten")
    opt.add_argument("--effective-mib-per-second", type=int, default=100,
                     help="measured aggregate MiB/s across all simultaneous transfers")
    opt.add_argument("--max-parallel", type=int, default=2)
    opt.add_argument("--max-per-host", type=int, default=1)
    opt.add_argument("--max-per-storage", type=int, default=1)
    opt.add_argument("--stage-overhead-minutes", type=int, default=15)
    opt.add_argument("--priorities", help="private JSON map: source VM ID -> integer 0..3")
    opt.add_argument("--dependencies", help="private JSON map: VM ID -> prerequisite VM IDs")

    batch = sub.add_parser("migration-request-batch", help="create separate restore requests; NEVER approve or migrate")
    for s in ("source", "destination", "selection", "plan"):
        batch.add_argument("--" + s, required=True)
    batch.add_argument("--requester", required=True)
    batch.add_argument("--max-new-requests", type=int, default=25)
    batch.add_argument("--execute-requests", action="store_true",
                       help="create individual pending requests; no hypervisor changes")

    summary = sub.add_parser("migration-summary", help="show concise, integrity-checked migration blockers")
    summary.add_argument("--plan", required=True, help="existing SHA256-bound migration plan JSON")
    d = sub.add_parser("migration-discover", help="read-only hypervisor inventory; not proof of live readiness")
    d.add_argument("--platform", required=True, help="configured platform name")
    d.add_argument("--output", required=True, help="new JSON file (never overwritten)")
    pl = sub.add_parser("migration-plan", help="fail-closed capacity and network/storage mapping checks")
    for s in ("source", "destination", "selection"):
        pl.add_argument("--" + s, required=True, help="JSON document")
    pl.add_argument("--output", help="save new plan file; never overwritten")
    req = sub.add_parser("migration-request", help="create approved-path RESTORE REQUEST, never execute")
    for s in ("source", "destination", "selection", "plan"):
        req.add_argument("--" + s, required=True)
    req.add_argument("--vm-id", required=True)
    req.add_argument("--requester", required=True)
    req.add_argument("--execute-request", action="store_true",
                     help="actually create a restore request; does NOT create/power on a VM")
    stage = sub.add_parser("migration-stage", help="restore NEW powered-off XCP-ng VM only after four-eyes approval")
    for s in ("source", "destination", "selection", "plan"):
        stage.add_argument("--" + s, required=True)
    stage.add_argument("--request-id", required=True, type=int)
    stage.add_argument("--actor", required=True)
    stage.add_argument("--execute-stage", action="store_true",
                       help="explicitly restore and map powered-off NEW XCP-ng target VM")
    return p


def _write_new(path: str, data: dict[str, Any]) -> None:
    target = Path(path)
    # O_EXCL protects against replacing an operator's existing plan.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    return plan_migration(read_json(args.source), read_json(args.destination), read_json(args.selection))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "migration-optimize":
            from .migration_optimizer import optimize_migration_plan
            result = optimize_migration_plan(
                read_json(args.plan),
                effective_mib_per_second=args.effective_mib_per_second,
                max_parallel=args.max_parallel,
                max_per_host=args.max_per_host,
                max_per_storage=args.max_per_storage,
                stage_overhead_minutes=args.stage_overhead_minutes,
                priorities=read_json(args.priorities) if args.priorities else None,
                dependencies=read_json(args.dependencies) if args.dependencies else None,
            )
            if args.output:
                _write_new(args.output, result)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0
        if args.command == "migration-request-batch":
            from .migration_batch import create_migration_requests
            fresh = _plan(args)
            saved = read_json(args.plan)
            if fresh["plan_sha256"] != saved.get("plan_sha256"):
                raise PermissionError("migration plan changed/stale; generate and review a new plan")
            if not args.execute_requests:
                report = create_migration_requests(
                    fresh, requester=args.requester,
                    max_new_requests=args.max_new_requests)
            else:
                from .portal_v11_final import ManagedRecoveryEngine
                engine = ManagedRecoveryEngine(load_v11_config(args.config))
                report = create_migration_requests(
                    fresh, engine=engine, requester=args.requester, execute=True,
                    max_new_requests=args.max_new_requests)
            print(json.dumps(report, indent=2))
            return 1 if report.get("status") == "PARTIAL_FAILURE" else 0
        if args.command == "migration-summary":
            from .migration_reporting import summarize_migration_plan
            report = summarize_migration_plan(read_json(args.plan))
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["status"] == "READY_FOR_OPERATOR_REVIEW" else 1
        if args.command == "migration-discover":
            cfg = load_v11_config(args.config)
            platform = next((p for p in cfg.platforms if p.name == args.platform and p.enabled), None)
            if platform is None:
                raise ValueError("unknown or disabled configured platform")
            data = discover(platform, cfg.runtime.command_timeout_seconds)
            _write_new(args.output, data)
            print(json.dumps({"path": args.output, "status": data["discovery_status"],
                              "warnings": data.get("discovery_warnings", [])}, indent=2))
            return 0  # discovery success != cutover readiness
        if args.command == "migration-plan":
            result = _plan(args)
            if args.output:
                _write_new(args.output, result)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0 if result["ready_for_operator_review"] else 1
        if args.command in {"migration-request", "migration-stage"}:
            fresh = _plan(args)
            saved = read_json(args.plan)
            if fresh["plan_sha256"] != saved.get("plan_sha256"):
                raise PermissionError("migration plan changed/stale; create a new plan and review again")
            if not fresh["ready_for_operator_review"]:
                raise PermissionError("migration plan is blocked")
            if args.command == "migration-stage":
                from .portal_v11_final import ManagedRecoveryEngine
                from .migration_stage import XCPNGMigrationStager
                engine = ManagedRecoveryEngine(load_v11_config(args.config))
                result = XCPNGMigrationStager(engine, fresh).stage(
                    args.request_id, actor=args.actor, execute=args.execute_stage)
                print(json.dumps(result, indent=2))
                return 0
            if not args.execute_request:
                print(json.dumps({"dry_run": True, "vm_id": args.vm_id, "plan_sha256": fresh["plan_sha256"],
                                  "no_changes": True,
                                  "next": "use --execute-request to create a separately approved restore request"}, indent=2))
                return 0
            # The existing engine owns immutable point, tenant, certification, RBAC
            # and four-eyes restore enforcement. This only requests approval.
            from .portal_v11_final import ManagedRecoveryEngine
            cfg = load_v11_config(args.config)
            engine = ManagedRecoveryEngine(cfg)
            result = create_restore_request(fresh, args.vm_id, engine=engine, requester=args.requester)
            print(json.dumps(result, indent=2))
            return 0
        return 2
    except (OSError, ValueError, RuntimeError, PermissionError, KeyError, TypeError) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
