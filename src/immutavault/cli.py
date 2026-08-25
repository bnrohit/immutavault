from __future__ import annotations

import argparse
import json
import sys

from .enterprise_config import load_enterprise_config
from .engine import BackupEngine
from .hardware import hardware_report
from .lock import exclusive_lock
from .portal_enterprise import Portal


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="immutavault", description="Immutable multi-hypervisor backup and recovery vault")
    p.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")
    sub.add_parser("state-backup")
    sub.add_parser("hardware")
    sub.add_parser("inventory")
    sub.add_parser("platform-info")
    sub.add_parser("storage-targets")
    replica_init = sub.add_parser("replica-init")
    replica_init.add_argument("--name", required=True)
    replica_lock_init = sub.add_parser("replica-lock-init")
    replica_lock_init.add_argument("--name", required=True)
    replica_lock_status = sub.add_parser("replica-lock-status")
    replica_lock_status.add_argument("--name", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--all", action="store_true", help="back up every selected VM")
    backup.add_argument("--dry-run", action="store_true")
    sub.add_parser("retention")
    sub.add_parser("verify")
    points = sub.add_parser("recovery-points")
    points.add_argument("--platform")
    points.add_argument("--vm-id")
    verify_point = sub.add_parser("verify-point")
    verify_point.add_argument("--snapshot", required=True)
    hold = sub.add_parser("hold-point")
    hold.add_argument("--snapshot", required=True)
    hold.add_argument("--days", required=True, type=int)
    hold.add_argument("--actor", required=True)
    hold.add_argument("--reason", default="manual hold")
    sub.add_parser("audit-verify")
    request = sub.add_parser("restore-request")
    request.add_argument("--snapshot", required=True)
    request.add_argument("--requester", required=True)
    request.add_argument("--target-platform", required=True)
    request.add_argument("--target-name")
    request.add_argument("--options-json", default="{}")
    approve = sub.add_parser("restore-approve")
    approve.add_argument("--request-id", required=True, type=int)
    approve.add_argument("--approver", required=True)
    execute = sub.add_parser("restore-execute")
    execute.add_argument("--request-id", required=True, type=int)
    execute.add_argument("--actor", required=True)
    execute.add_argument("--dry-run", action="store_true")
    sub.add_parser("restore-requests")
    sub.add_parser("portal")
    sub.add_parser("dr-plan")
    sub.add_parser("dr-preflight")
    sub.add_parser("dr-sync")
    dr_net = sub.add_parser("dr-network")
    dr_net.add_argument("action", choices=["plan", "prepare", "activate", "deactivate", "status"])
    dr_net.add_argument("--site", required=True)
    dr_net.add_argument("--execute", action="store_true")
    dr_promote = sub.add_parser("dr-promote")
    dr_promote.add_argument("--execute", action="store_true")
    dr_promote.add_argument("--confirm-primary-fenced", action="store_true")
    dr_demote = sub.add_parser("dr-demote")
    dr_demote.add_argument("--execute", action="store_true")
    dr_failback = sub.add_parser("dr-failback")
    dr_failback.add_argument("--execute", action="store_true")
    dr_failback.add_argument("--confirm-primary-isolated", action="store_true")
    sub.add_parser("dr-watch")
    dr_maint = sub.add_parser("dr-maintenance")
    dr_maint.add_argument("action", choices=["on", "off", "status"])
    dr_maint.add_argument("--actor", default="operator")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hardware":
        print(json.dumps(hardware_report(), indent=2)); return 0

    cfg = load_enterprise_config(args.config)
    engine = BackupEngine(cfg)
    if args.command == "doctor":
        result = engine.doctor(); print(json.dumps(result, indent=2)); return 1 if any(result.values()) else 0
    if args.command == "status":
        result = engine.status(); print(json.dumps(result, indent=2)); return 0 if result.get("healthy") else 1
    if args.command == "state-backup":
        print(json.dumps(engine.backup_state_database(), indent=2)); return 0
    if args.command == "inventory":
        print(json.dumps({k: [vm.__dict__ for vm in v] for k, v in engine.inventory().items()}, indent=2)); return 0
    if args.command == "platform-info":
        print(json.dumps(engine.platform_info(), indent=2)); return 0
    if args.command == "storage-targets":
        print(json.dumps(engine.storage_targets(), indent=2)); return 0
    if args.command == "replica-init":
        print(json.dumps(engine.init_replica(args.name), indent=2)); return 0
    if args.command == "replica-lock-init":
        print(json.dumps(engine.init_replica_lock(args.name), indent=2)); return 0
    if args.command == "replica-lock-status":
        print(json.dumps(engine.replica_lock_status(args.name), indent=2)); return 0
    if args.command == "backup":
        if not args.all:
            print("refusing to guess scope: pass --all after configuring include/exclude patterns", file=sys.stderr); return 2
        result = engine.backup_all(dry_run=args.dry_run); print(json.dumps(result, indent=2)); return 1 if any(x.get("status") == "failed" for x in result) else 0
    if args.command == "retention":
        with exclusive_lock(cfg.repository.lock_file): removed = engine.retention()
        print(json.dumps({"status":"complete","removed":removed,"immutable_window_days":cfg.repository.retention.keep_within_days}, indent=2)); return 0
    if args.command == "verify":
        with exclusive_lock(cfg.repository.lock_file): engine.repo.check()
        print("repository verification completed"); return 0
    if args.command == "recovery-points":
        print(json.dumps(engine.list_recovery_points(platform=args.platform, vm_id=args.vm_id), indent=2)); return 0
    if args.command == "verify-point":
        ok = engine.verify_recovery_point(args.snapshot, actor="cli"); print(json.dumps({"snapshot":args.snapshot,"verified":ok}, indent=2)); return 0 if ok else 1
    if args.command == "hold-point":
        until = engine.hold_recovery_point(args.snapshot, actor=args.actor, days=args.days, reason=args.reason)
        print(json.dumps({"snapshot": args.snapshot, "immutable_until": until}, indent=2)); return 0
    if args.command == "audit-verify":
        ok, errors = engine.state.verify_audit_chain(); print(json.dumps({"valid": ok, "errors": errors}, indent=2)); return 0 if ok else 1
    if args.command == "restore-request":
        options = json.loads(args.options_json)
        rid = engine.request_restore(snapshot_id=args.snapshot, requester=args.requester, target_platform=args.target_platform, target_name=args.target_name, options=options)
        print(json.dumps(engine.state.get_restore_request(rid), indent=2)); return 0
    if args.command == "restore-approve":
        engine.approve_restore(args.request_id, args.approver); print(json.dumps(engine.state.get_restore_request(args.request_id), indent=2)); return 0
    if args.command == "restore-execute":
        print(json.dumps(engine.execute_restore(args.request_id, actor=args.actor, dry_run=args.dry_run), indent=2)); return 0
    if args.command == "restore-requests":
        print(json.dumps(engine.state.list_restore_requests(), indent=2)); return 0
    if args.command == "portal":
        Portal(cfg).serve(); return 0
    if args.command == "dr-plan":
        print(json.dumps(engine.dr_orchestrator().plan(), indent=2)); return 0
    if args.command == "dr-preflight":
        result = engine.dr_orchestrator().preflight(); print(json.dumps(result, indent=2)); return 0 if result.get("ok") else 1
    if args.command == "dr-sync":
        result = engine.dr_orchestrator().sync(); print(json.dumps(result, indent=2)); return 0 if result.get("ok") else 1
    if args.command == "dr-network":
        dr = engine.dr_orchestrator(); net = dr.net
        if args.action == "plan": result = net.plan(args.site)
        elif args.action == "status": result = net.status(args.site)
        elif not args.execute:
            if args.action == "prepare": preview = net.plan(args.site)
            elif args.action == "activate": preview = net.activate(args.site, dry_run=True)
            else: preview = net.deactivate(args.site, dry_run=True)
            result = {"mode": "plan", "requested_action": args.action, "site": args.site, "preview": preview}
        elif args.action == "prepare": result = net.prepare(args.site)
        elif args.action == "activate": result = net.activate(args.site)
        else: result = net.deactivate(args.site)
        print(json.dumps(result, indent=2)); return 0
    if args.command == "dr-promote":
        print(json.dumps(engine.dr_orchestrator().promote(execute=args.execute, confirm_primary_fenced=args.confirm_primary_fenced), indent=2)); return 0
    if args.command == "dr-demote":
        print(json.dumps(engine.dr_orchestrator().demote_dr(execute=args.execute), indent=2)); return 0
    if args.command == "dr-failback":
        print(json.dumps(engine.dr_orchestrator().failback(execute=args.execute, confirm_primary_isolated=args.confirm_primary_isolated), indent=2)); return 0
    if args.command == "dr-watch":
        print(json.dumps(engine.dr_orchestrator().watch(), indent=2)); return 0
    if args.command == "dr-maintenance":
        print(json.dumps(engine.dr_orchestrator().maintenance(args.action, actor=args.actor), indent=2)); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
