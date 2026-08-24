#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

try:
    import yaml
except ImportError:
    print("PyYAML is required; run this from an installed Immutavault environment", file=sys.stderr)
    raise SystemExit(2)

from immutavault.config import load_config
from immutavault.adapters.vmware_incremental import VMwareIncrementalAdapter


def load_env(path: str) -> None:
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Immutavault VMware native incremental/VDDK readiness")
    parser.add_argument("--config", default="/etc/immutavault/immutavault.yml")
    parser.add_argument("--env", default="/etc/immutavault/immutavault.env")
    parser.add_argument("--platform", help="VMware platform name; default checks all VMware platforms")
    args = parser.parse_args()

    load_env(args.env)
    cfg = load_config(args.config)
    selected = [p for p in cfg.platforms if p.type == "vmware" and (not args.platform or p.name == args.platform)]
    if not selected:
        print("No matching VMware platform found", file=sys.stderr)
        return 2

    failed = False
    rows = []
    for platform in selected:
        adapter = VMwareIncrementalAdapter(platform, cfg.runtime.command_timeout_seconds)
        try:
            caps = adapter._provider().capabilities(env=adapter._govc_env())
        except Exception as exc:
            caps = {"available": False, "reason": str(exc)}
        fallback = adapter._fallback_allowed()
        native_requested = adapter._incremental_mode()
        effective = "native-incremental" if native_requested and caps.get("available") else (
            "hot-clone-export-fallback" if native_requested and fallback else platform.mode
        )
        row = {
            "platform": platform.name,
            "configured_mode": platform.mode,
            "native_requested": native_requested,
            "provider": caps,
            "fallback_allowed": fallback,
            "effective_if_run_now": effective,
        }
        rows.append(row)
        if native_requested and not caps.get("available") and not fallback:
            failed = True

    print(json.dumps(rows, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
