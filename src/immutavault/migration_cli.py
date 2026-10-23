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


COMMANDS = {"migration-discover", "migration-plan", "migration-request", "migration-stage"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="immutavault", description="Cluster migration planning (safe defaults)")
    p.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    sub = p.add_subparsers(dest="command", required=True)
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
