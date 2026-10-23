"""Migration stage tests use simulated xe output, never a hypervisor."""
from __future__ import annotations

import json
from types import SimpleNamespace
import pytest

from immutavault import migration_stage

POOL = "d5f35590-8145-a2be-a12c-b0666d178661"
HOST = "8c9d4f01-c6e1-4b0b-a8f9-6ba313aa7db6"
SR = "3552d77f-20e5-d077-1779-ff4e64b587b8"
NET = "4fdfc0aa-2cbf-7ede-1d87-0e4106da9236"
VM = "68587bde-7630-4099-a642-2d48e235b493"
VIF = "1ef711c6-d8f6-b143-dce1-71786e077717"


def fake_plan():
    return {
        "source_family": "xcpng", "destination_family": "xcpng",
        "destination_platform": "target", "destination_cluster": POOL,
        "plan_sha256": "abc123", "ready_for_operator_review": True,
        "vms": [{
            "source_vm_id": "source-uuid", "snapshot_id": "snapshot-id",
            "target_name": "migrated-pilot", "destination_host": HOST,
            "disks": [{"target_sr": SR, "size_bytes": 1024}],
            "nics": [{"device": "0", "target_network": NET}],
            "ready_for_operator_review": True,
        }],
    }


class State:
    def __init__(self):
        self.events = []
        self.req = {
            "status": "approved", "snapshot_id": "snapshot-id",
            "target_name": "migrated-pilot", "target_platform": "target",
            "options_json": json.dumps({"migration_plan_sha256": "abc123", "sr_uuid": SR}),
        }
    def get_restore_request(self, _id):
        return self.req
    def audit(self, *args):
        self.events.append(args)


class Engine:
    def __init__(self):
        self.state = State()
        self.cfg = SimpleNamespace(
            runtime=SimpleNamespace(command_timeout_seconds=120),
            protection=SimpleNamespace(require_four_eyes_restore=True),
        )
        self.import_calls = 0
    def _platform(self, name):
        return SimpleNamespace(type="xcpng", enabled=True, name=name)
    def execute_restore(self, *args, **kwargs):
        self.import_calls += 1
        return {"uuid": VM, "platform": "target", "name": "migrated-pilot"}


class Adapter:
    def _ssh(self):
        return ["ssh", "test"]


def fake_run_factory(commands):
    def _run(command, **kwargs):
        remote = command[-1]
        commands.append(remote)
        if remote.startswith("xe pool-list"):
            output = POOL
        elif remote.startswith("xe host-list"):
            output = HOST
        elif remote.startswith("xe sr-param-get"):
            output = "true"
        elif remote.startswith("xe network-list"):
            output = NET
        elif remote.startswith("xe vm-list"):
            output = ""
        elif remote.startswith("xe vif-list"):
            output = VIF
        elif remote.startswith("xe vm-param-get"):
            output = "Halted"
        elif remote.startswith("xe vif-param-get") and "device" in remote:
            output = "0"
        elif remote.startswith("xe vif-param-get") and "network-uuid" in remote:
            output = NET
        elif remote.startswith("xe vif-move"):
            output = ""
        else:
            raise AssertionError("Unexpected command: " + remote)
        return SimpleNamespace(stdout=output, returncode=0, stderr="")
    return _run


def test_stage_dry_run_only_reads(monkeypatch):
    commands = []
    monkeypatch.setattr(migration_stage, "build_adapter", lambda p, t: Adapter())
    monkeypatch.setattr(migration_stage, "run", fake_run_factory(commands))
    engine = Engine()
    result = migration_stage.XCPNGMigrationStager(engine, fake_plan()).stage(
        5, actor="reviewer", execute=False)
    assert result["no_changes"]
    assert engine.import_calls == 0
    assert not any("vif-move" in cmd for cmd in commands)


def test_stage_only_powered_off_and_exact_network_mapping(monkeypatch):
    commands = []
    monkeypatch.setattr(migration_stage, "build_adapter", lambda p, t: Adapter())
    monkeypatch.setattr(migration_stage, "run", fake_run_factory(commands))
    engine = Engine()
    result = migration_stage.XCPNGMigrationStager(engine, fake_plan()).stage(
        5, actor="executor", execute=True)
    assert result["status"] == "STAGED_POWERED_OFF"
    assert engine.import_calls == 1
    assert len([cmd for cmd in commands if "vif-move" in cmd]) == 1
    assert not any("vm-start" in cmd or "vm-shutdown" in cmd for cmd in commands)


def test_unapproved_request_never_imports(monkeypatch):
    monkeypatch.setattr(migration_stage, "build_adapter", lambda p, t: Adapter())
    engine = Engine()
    engine.state.req["status"] = "pending_approval"
    with pytest.raises(migration_stage.StagingBlocked):
        migration_stage.XCPNGMigrationStager(engine, fake_plan()).stage(
            5, actor="operator", execute=True)
    assert engine.import_calls == 0


def test_plan_drift_never_imports(monkeypatch):
    monkeypatch.setattr(migration_stage, "build_adapter", lambda p, t: Adapter())
    engine = Engine()
    engine.state.req["options_json"] = json.dumps({"migration_plan_sha256": "wrong", "sr_uuid": SR})
    with pytest.raises(migration_stage.StagingBlocked):
        migration_stage.XCPNGMigrationStager(engine, fake_plan()).stage(
            5, actor="operator", execute=True)
    assert engine.import_calls == 0
