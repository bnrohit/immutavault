from __future__ import annotations

import json
from pathlib import Path

import pytest

from immutavault.adapters.base import VM
from immutavault.adapters.vmware import VMwareAdapter
from immutavault.config import PlatformConfig
from immutavault.consistency import read_consistency
from immutavault.incremental import CHECKPOINT_FILE, IncrementalTransportError, VDDKProvider
from immutavault.runner import CommandResult


def _helper(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_vmware_strict_application_consistency_records_quiesced_attestation(monkeypatch, tmp_path):
    cfg = PlatformConfig(
        name="vc", type="vmware", endpoint="https://vc/sdk", mode="hot-clone-export",
        options={
            "username_env": "U", "password_env": "P",
            "quiesce": True,
            "quiesce_fallback_crash_consistent": False,
            "application_consistency_strict": True,
        },
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    monkeypatch.setattr(
        "immutavault.adapters.vmware.run",
        lambda command, **kwargs: CommandResult(command, 0, "", ""),
    )
    out = VMwareAdapter(cfg, 30).export(VM(id="vm", name="sql01", kind="vmware"), tmp_path)
    value = read_consistency(out)
    assert value is not None
    assert value["state"] == "guest-quiesced"
    assert value["strict"] is True
    assert value["application_consistent"] is True


def test_vmware_strict_application_consistency_rejects_crash_fallback(monkeypatch, tmp_path):
    cfg = PlatformConfig(
        name="vc", type="vmware", endpoint="https://vc/sdk", mode="hot-clone-export",
        options={
            "username_env": "U", "password_env": "P",
            "quiesce": True,
            "quiesce_fallback_crash_consistent": True,
            "application_consistency_strict": True,
        },
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    with pytest.raises(RuntimeError, match="requires quiesce=true"):
        VMwareAdapter(cfg, 30).export(VM(id="vm", name="sql01", kind="vmware"), tmp_path)


def test_vddk_strict_application_consistency_requires_provider_attestation(tmp_path):
    helper = _helper(
        tmp_path / "helper.py",
        r'''
import json, pathlib, sys
if sys.argv[1] == "capabilities":
    print(json.dumps({"protocol_version": 1, "features": ["cbt", "backup", "restore"]}))
    raise SystemExit(0)
req = json.load(sys.stdin)
if sys.argv[1] == "backup":
    dest = pathlib.Path(req["destination"])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "immutavault-vddk-layout.json").write_text(json.dumps({"version": 1, "disks": []}))
    print(json.dumps({"status": "success", "checkpoint": {"disk": "uuid/2"}}))
''',
    )
    provider = VDDKProvider({"vddk_helper": str(helper), "application_consistency_strict": True}, 60)
    destination = tmp_path / "cache"
    with pytest.raises(IncrementalTransportError) as exc:
        provider.backup(
            platform_name="vc", endpoint="https://vc/sdk", vm_id="vm-1", vm_name="sql01",
            destination=destination, env={}, quiesce=True,
        )
    assert exc.value.reason == "application_consistency_unproven"
    assert exc.value.fallback_safe is False
    assert not (destination / CHECKPOINT_FILE).exists()


def test_vddk_strict_application_consistency_accepts_attested_quiescence(tmp_path):
    helper = _helper(
        tmp_path / "helper.py",
        r'''
import json, pathlib, sys
if sys.argv[1] == "capabilities":
    print(json.dumps({"protocol_version": 1, "features": ["cbt", "backup", "restore"]}))
    raise SystemExit(0)
req = json.load(sys.stdin)
if sys.argv[1] == "backup":
    assert req["application_consistency_strict"] is True
    dest = pathlib.Path(req["destination"])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "immutavault-vddk-layout.json").write_text(json.dumps({"version": 1, "disks": [], "flr_disk_images": []}))
    print(json.dumps({
      "status": "success",
      "checkpoint": {"disk": "uuid/3"},
      "consistency": {"state": "application-consistent", "method": "vmware-vss"}
    }))
''',
    )
    provider = VDDKProvider({"vddk_helper": str(helper), "application_consistency_strict": True}, 60)
    destination = tmp_path / "cache"
    result = provider.backup(
        platform_name="vc", endpoint="https://vc/sdk", vm_id="vm-1", vm_name="sql01",
        destination=destination, env={}, quiesce=True,
    )
    assert result.detail["consistency"]["state"] == "application-consistent"
    assert (destination / CHECKPOINT_FILE).is_file()
    assert read_consistency(destination)["application_consistent"] is True
