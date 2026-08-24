from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from immutavault.incremental import (
    CHECKPOINT_FILE,
    LAYOUT_FILE,
    TRANSPORT_FILE,
    IncrementalTransportError,
    VDDKProvider,
)


def _helper(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_missing_vddk_helper_is_capability_miss(tmp_path):
    provider = VDDKProvider({"vddk_helper": str(tmp_path / "missing")}, 60)
    caps = provider.capabilities(env={})
    assert caps["available"] is False
    assert caps["reason"] == "helper_missing"


def test_vddk_provider_uses_json_stdin_and_persists_checkpoint(tmp_path):
    helper = _helper(
        tmp_path / "helper.py",
        r'''
import json, os, pathlib, sys
if sys.argv[1] == "capabilities":
    print(json.dumps({"protocol_version": 1, "provider": "test-vddk", "features": ["cbt", "backup", "restore"], "transport_modes": ["nbdssl"]}))
    raise SystemExit(0)
req = json.load(sys.stdin)
assert os.environ.get("GOVC_PASSWORD") == "secret-not-on-argv"
assert "secret-not-on-argv" not in json.dumps(req)
if sys.argv[1] == "backup":
    dest = pathlib.Path(req["destination"])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "immutavault-vddk-layout.json").write_text(json.dumps({"version": 1, "disks": []}))
    print(json.dumps({"status": "success", "mode": "baseline" if not req.get("previous_checkpoint") else "incremental", "checkpoint": {"disk-2000": "uuid/1"}, "changed_bytes": 4096, "source_bytes_read": 4096, "transport": "nbdssl"}))
elif sys.argv[1] == "restore":
    print(json.dumps({"status": "success", "result": {"name": req["target_name"], "id": "vm-42"}}))
''',
    )
    provider = VDDKProvider({"vddk_helper": str(helper)}, 60)
    destination = tmp_path / "cache"
    env = {"GOVC_PASSWORD": "secret-not-on-argv"}
    first = provider.backup(
        platform_name="vc-main",
        endpoint="https://vc/sdk",
        vm_id="vm-21",
        vm_name="sql01",
        destination=destination,
        env=env,
        quiesce=True,
    )
    assert first.mode == "baseline"
    assert first.changed_bytes == 4096
    assert json.loads((destination / CHECKPOINT_FILE).read_text())["disk-2000"] == "uuid/1"
    assert json.loads((destination / TRANSPORT_FILE).read_text())["provider"] == "vddk-cbt"
    assert (destination / LAYOUT_FILE).is_file()

    second = provider.backup(
        platform_name="vc-main",
        endpoint="https://vc/sdk",
        vm_id="vm-21",
        vm_name="sql01",
        destination=destination,
        env=env,
        quiesce=True,
    )
    assert second.mode == "incremental"

    restored = provider.restore(source=destination, target_name="sql01-restore", options={}, env=env)
    assert restored["name"] == "sql01-restore"
    assert restored["transport"] == "vddk-cbt"


def test_provider_refuses_success_without_recoverable_layout(tmp_path):
    helper = _helper(
        tmp_path / "helper.py",
        r'''
import json, sys
if sys.argv[1] == "capabilities":
    print(json.dumps({"protocol_version": 1, "features": ["cbt", "backup", "restore"]}))
else:
    print(json.dumps({"status": "success", "checkpoint": {"disk": "uuid/1"}}))
''',
    )
    provider = VDDKProvider({"vddk_helper": str(helper)}, 60)
    with pytest.raises(IncrementalTransportError, match="did not create"):
        provider.backup(
            platform_name="vc",
            endpoint="https://vc/sdk",
            vm_id="vm-1",
            vm_name="vm1",
            destination=tmp_path / "cache",
            env={},
            quiesce=True,
        )


def test_provider_surfaces_typed_safe_fallback_reason(tmp_path):
    helper = _helper(
        tmp_path / "helper.py",
        r'''
import json, sys
if sys.argv[1] == "capabilities":
    print(json.dumps({"protocol_version": 1, "features": ["cbt", "backup", "restore"]}))
else:
    print(json.dumps({"status": "fallback", "reason": "change_id_reset", "fallback_safe": True, "error": "CBT UUID changed"}))
    raise SystemExit(20)
''',
    )
    provider = VDDKProvider({"vddk_helper": str(helper)}, 60)
    with pytest.raises(IncrementalTransportError) as exc:
        provider.backup(
            platform_name="vc",
            endpoint="https://vc/sdk",
            vm_id="vm-1",
            vm_name="vm1",
            destination=tmp_path / "cache",
            env={},
            quiesce=True,
        )
    assert exc.value.reason == "change_id_reset"
    assert exc.value.fallback_safe is True
