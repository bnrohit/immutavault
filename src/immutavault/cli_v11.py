from __future__ import annotations

import argparse
import json
import sys

from . import cli_v10 as v10
from .management_broker import ManagementBrokerClient
from .management_config import load_v11_config
from .policy import ProtectionPolicyRunner
from .portal_v11_final import Portal


MANAGEMENT_COMMANDS = {"policy-list", "policy-run", "policy-dry-run", "management-status"}


def _command_name(argv: list[str]) -> str | None:
    return v10._command_name(argv)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immutavault", description="Immutavault v1.1 management controls")
    parser.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("policy-list", help="list named point-and-click protection policies")
    run_policy = sub.add_parser("policy-run", help="run one named protection policy now")
    run_policy.add_argument("--name", required=True)
    dry = sub.add_parser("policy-dry-run", help="plan one named protection policy without writing backup data")
    dry.add_argument("--name", required=True)
    sub.add_parser("management-status", help="check the local privileged management broker")
    return parser


def _run_management(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    cfg = load_v11_config(args.config)
    if args.command == "policy-list":
        rows = []
        for policy in cfg.management.policies:
            rows.append({
                "id": policy.id,
                "name": policy.name,
                "enabled": policy.enabled,
                "schedule": policy.schedule.on_calendar(),
                "immutable_days": policy.immutable_days,
                "replica_targets": list(policy.replica_targets),
                "selections": [
                    {"platform": row.platform, "vms": list(row.vms)} for row in policy.selections
                ],
            })
        print(json.dumps(rows, indent=2)); return 0
    if args.command in {"policy-run", "policy-dry-run"}:
        result = ProtectionPolicyRunner(cfg).run(
            args.name,
            dry_run=args.command == "policy-dry-run",
            actor="cli",
        )
        print(json.dumps(result, indent=2))
        return 1 if result.get("failed") else 0
    if args.command == "management-status":
        result = ManagementBrokerClient(cfg).status()
        print(json.dumps(result, indent=2))
        return 0 if result.get("broker_available") else 1
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if _command_name(args) in MANAGEMENT_COMMANDS:
        return _run_management(args)

    # Reuse the certified v1.0 command surface but inject the additive v1.1
    # configuration and unified portal. V2V policy remains fail-closed.
    v10.load_v10_config = load_v11_config
    v10.Portal = Portal
    return v10.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
