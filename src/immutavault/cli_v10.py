from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import cli as legacy
from .portal_v10 import Portal
from .v2v_engine import CertifiedBackupEngine
from .v2v_config import load_v10_config


V2V_COMMANDS = {"v2v-doctor", "v2v-plan"}


def _command_name(argv: list[str]) -> str | None:
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--config":
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return item
    return None


def _v2v_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immutavault", description="Certified cross-hypervisor recovery controls")
    parser.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("v2v-doctor", help="validate conversion tooling and pinned provider capabilities")
    plan = sub.add_parser("v2v-plan", help="evaluate a recovery point against a target without executing conversion")
    plan.add_argument("--snapshot", required=True)
    plan.add_argument("--target-platform", required=True)
    plan.add_argument("--options-json", default="{}")
    return parser


def _run_v2v(argv: list[str]) -> int:
    args = _v2v_parser().parse_args(argv)
    cfg = load_v10_config(args.config)
    engine = CertifiedBackupEngine(cfg)
    if args.command == "v2v-doctor":
        result = engine.v2v.doctor()
        print(json.dumps(result, indent=2))
        return 1 if any(result.values()) else 0
    if args.command == "v2v-plan":
        try:
            options: dict[str, Any] = json.loads(args.options_json)
        except json.JSONDecodeError as exc:
            print(f"invalid --options-json: {exc}", file=sys.stderr)
            return 2
        if not isinstance(options, dict):
            print("--options-json must decode to a JSON object", file=sys.stderr)
            return 2
        result = engine.v2v_plan(args.snapshot, args.target_platform, options)
        print(json.dumps(result, indent=2))
        return 0 if result.get("allowed") else 1
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if _command_name(args) in V2V_COMMANDS:
        return _run_v2v(args)

    # Reuse the established v0.9 command surface while replacing its injected
    # configuration loader, engine and portal with the v1.0 certified variants.
    # This avoids forking two large command implementations and preserves all
    # existing operational behavior for non-V2V commands.
    legacy.load_enterprise_config = load_v10_config
    legacy.BackupEngine = CertifiedBackupEngine
    legacy.Portal = Portal
    return legacy.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
