"""Standalone replica validation CLI routed from the v1.1 command surface."""
from __future__ import annotations

import argparse
import json
import sys

from .management_config import load_v11_config
from .replica_verification import replica_copy_ready, verify_replica_restore, verify_due_replica

COMMANDS = {"replica-verify", "replica-readiness", "replica-coverage", "replica-verify-due"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="immutavault", description="Replica recovery checks")
    parser.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    sub = parser.add_subparsers(dest="command", required=True)
    due = sub.add_parser("replica-verify-due", help="verify at most one due replica with a size cap")
    due.add_argument("--name", required=True)
    due.add_argument("--actor", required=True)
    due.add_argument("--max-backup-gib", type=int, default=100)
    due.add_argument("--execute-readback", action="store_true")
    verify = sub.add_parser("replica-verify", help="read replica, actually restore and verify bytes")
    ready = sub.add_parser("replica-readiness", help="read-only recovery proof status")
    for subcmd in (verify, ready):
        subcmd.add_argument("--snapshot", required=True)
        subcmd.add_argument("--name", required=True, help="configured non-primary replica")
    coverage = sub.add_parser("replica-coverage", help="list unverified or absent copies across recent recovery points")
    coverage.add_argument("--limit", type=int, default=100)
    coverage.add_argument("--platform")
    verify.add_argument("--actor", required=True)
    verify.add_argument("--execute-readback", action="store_true",
                        help="perform a potentially large temporary readback; no hypervisor changes")
    args = parser.parse_args(argv)
    try:
        from .portal_v11_final import ManagedRecoveryEngine
        engine = ManagedRecoveryEngine(load_v11_config(args.config))
        if args.command == "replica-verify-due":
            report = verify_due_replica(engine, replica_name=args.name, actor=args.actor,
                                        max_backup_gib=args.max_backup_gib,
                                        execute=args.execute_readback)
            print(json.dumps(report, indent=2))
            return 1 if report["status"] == "NO_ELIGIBLE_COPY" else 0
        if args.command == "replica-coverage":
            if not 1 <= args.limit <= 1000:
                raise ValueError("limit must be from 1 to 1000")
            rows = []
            for point in engine.state.list_points(platform=args.platform, limit=args.limit):
                for replica in engine.cfg.replicas:
                    if not replica.enabled:
                        continue
                    copy = engine.state.get_recovery_copy(point["snapshot_id"], replica.name)
                    status = replica_copy_ready(copy)
                    rows.append({"vm": point["vm_name"], "platform": point["platform"],
                                 "snapshot_id": point["snapshot_id"], "replica": replica.name,
                                 "ready": bool(point.get("verified") and not point.get("suspicious") and status),
                                 "copied": bool(copy and copy.get("status") == "success"),
                                 "readback_verified": status})
            healthy = bool(rows) and all(row["ready"] for row in rows)
            print(json.dumps({"ready": healthy, "copy_count": len(rows), "copies": rows,
                              "warning": "catalog proof only; test restores and object retention policies separately"}, indent=2))
            return 0 if healthy else 1
        if args.command == "replica-verify":
            if not args.execute_readback:
                print(json.dumps({"dry_run": True, "snapshot_id": args.snapshot,
                                  "replica": args.name, "hypervisor_changes": False,
                                  "note": "add --execute-readback to restore and hash the replica copy"}, indent=2))
                return 0
            report = verify_replica_restore(
                engine, snapshot_id=args.snapshot, replica_name=args.name, actor=args.actor)
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.command == "replica-readiness":
            point = engine.state.get_point(args.snapshot)
            copy = engine.state.get_recovery_copy(args.snapshot, args.name)
            ready_flag = bool(point and point.get("verified") and not point.get("suspicious")
                              and replica_copy_ready(copy))
            print(json.dumps({"snapshot_id": args.snapshot, "replica": args.name,
                              "ready": ready_flag,
                              "backup_verified": bool(point and point.get("verified")),
                              "backup_suspicious": bool(point and point.get("suspicious")),
                              "replica_recently_restored_and_verified": replica_copy_ready(copy),
                              "warning": "proof is a past restore, not a live cutover certification"}, indent=2))
            return 0 if ready_flag else 1
        return 2
    except (OSError, ValueError, RuntimeError, PermissionError, KeyError, TypeError) as exc:
        print("replica verification failed (" + type(exc).__name__ + "); inspect protected controller logs",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
