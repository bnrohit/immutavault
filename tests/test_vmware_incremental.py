from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from immutavault.adapters import build_adapter
from immutavault.adapters.vmware_incremental import IncrementalUnavailable, VMwareIncrementalAdapter, normalize_extents
from immutavault.config import PlatformConfig, RepositoryConfig, ReplicaConfig
from immutavault.restic import ResticRepository
from immutavault.transport_state import (
    TRANSPORT_MARKER,
    atomic_json,
    chain_for,
    commit_after_backup,
    dependency_row,
    expand_dependencies,
    read_json,
    state_path,
)


def platform(mode: str = "auto") -> PlatformConfig:
    return PlatformConfig(
        name="vc-main", type="vmware", endpoint="https://vc.example/sdk", mode=mode,
        options={"username_env": "VC_USER", "password_env": "VC_PASS", "incremental_fallback": True},
    )


def marker(root: Path, *, kind: str, parent: str | None = None, baseline: str | None = None, change: str = "52 ab/cd") -> None:
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / TRANSPORT_MARKER, {
        "schema": 1, "transport": "vmware-cbt-vddk", "kind": kind, "seeded": True,
        "platform": "vc-main", "vm_id": "vm-101", "vm_name": "SQL01",
        "parent_snapshot_id": parent, "baseline_snapshot_id": baseline,
        "config_fingerprint": "fingerprint",
        "disks": [{"ordinal": 0, "capacity": 1024, "change_id": change}],
    })


def test_extent_normalization_merges_and_sector_aligns():
    assert normalize_extents([(3, 100), (90, 900), (2048, 512)], 4096) == [
        {"start": 0, "length": 1024}, {"start": 2048, "length": 512}
    ]


def test_builder_routes_vmware_to_incremental_adapter():
    assert isinstance(build_adapter(platform("hot-clone-export"), 60), VMwareIncrementalAdapter)


def test_auto_mode_without_state_uses_full_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMUTAVAULT_CBT_STATE_DIR", str(tmp_path / "state"))
    adapter = VMwareIncrementalAdapter(platform("auto"), 60)
    called = []
    monkeypatch.setattr(adapter, "_baseline", lambda vm, destination, reason: called.append(reason) or destination / vm.name)
    result = adapter.export(SimpleNamespace(id="vm-101", name="SQL01"), tmp_path / "stage")
    assert result.name == "SQL01"
    assert "initial CBT baseline" in called[0]


def test_auto_mode_falls_back_when_vddk_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMUTAVAULT_CBT_STATE_DIR", str(tmp_path / "state"))
    atomic_json(state_path("vc-main", "vm-101"), {
        "schema": 1, "platform": "vc-main", "vm_id": "vm-101", "baseline_snapshot_id": "base",
        "last_snapshot_id": "base", "chain_snapshot_ids": ["base"], "config_fingerprint": "x", "disks": [],
    })
    adapter = VMwareIncrementalAdapter(platform("auto"), 60)
    monkeypatch.setattr(adapter, "_vddk_problems", lambda: ["VDDK missing"])
    called = []
    monkeypatch.setattr(adapter, "_baseline", lambda vm, destination, reason: called.append(reason) or destination / vm.name)
    adapter.export(SimpleNamespace(id="vm-101", name="SQL01"), tmp_path / "stage")
    assert called == ["VDDK missing"]


def test_strict_mode_refuses_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMUTAVAULT_CBT_STATE_DIR", str(tmp_path / "state"))
    atomic_json(state_path("vc-main", "vm-101"), {
        "schema": 1, "platform": "vc-main", "vm_id": "vm-101", "baseline_snapshot_id": "base",
        "last_snapshot_id": "base", "chain_snapshot_ids": ["base"], "config_fingerprint": "x", "disks": [],
    })
    adapter = VMwareIncrementalAdapter(platform("vddk-cbt-strict"), 60)
    monkeypatch.setattr(adapter, "_vddk_problems", lambda: ["VDDK missing"])
    with pytest.raises(IncrementalUnavailable, match="VDDK missing"):
        adapter.export(SimpleNamespace(id="vm-101", name="SQL01"), tmp_path / "stage")


def test_cbt_epoch_change_forces_new_baseline():
    adapter = VMwareIncrementalAdapter(platform(), 60)
    state = {"config_fingerprint": "fp", "disks": [{"ordinal": 0, "capacity": 1024, "change_id": "epoch-a/1"}]}
    with pytest.raises(IncrementalUnavailable, match="epoch"):
        adapter._validate(state, [{"ordinal": 0, "capacity": 1024, "change_id": "epoch-b/2"}], "fp")


def test_cbt_state_commits_only_after_recovery_id_and_tracks_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMUTAVAULT_CBT_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "base"; marker(base, kind="baseline")
    commit_after_backup(base, "snap-base")
    state = read_json(state_path("vc-main", "vm-101"))
    assert state["last_snapshot_id"] == "snap-base"
    delta = tmp_path / "delta"; marker(delta, kind="delta", parent="snap-base", baseline="snap-base", change="52 ab/next")
    commit_after_backup(delta, "snap-delta")
    state = read_json(state_path("vc-main", "vm-101"))
    assert state["chain_snapshot_ids"] == ["snap-base", "snap-delta"]
    assert chain_for("snap-delta") == ["snap-base", "snap-delta"]
    assert expand_dependencies({"snap-delta"}) == {"snap-base"}
    assert dependency_row("snap-base")["source_path"] == str(base)


def test_delta_commit_rejects_wrong_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMUTAVAULT_CBT_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "base"; marker(base, kind="baseline"); commit_after_backup(base, "snap-base")
    bad = tmp_path / "bad"; marker(bad, kind="delta", parent="other", baseline="snap-base")
    with pytest.raises(RuntimeError, match="parent"):
        commit_after_backup(bad, "snap-bad")


def test_restic_backup_commits_transport_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIC_PASSWORD", "pw")
    repo = ResticRepository(RepositoryConfig(url="/repo", local_path="/repo", staging_path=str(tmp_path)), 60)
    events = []
    monkeypatch.setattr("immutavault.restic.commit_after_backup", lambda path, sid: events.append((path, sid)))
    monkeypatch.setattr("immutavault.restic.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"message_type":"summary","snapshot_id":"abc","data_added":12}) + "\n"))
    summary = repo.backup(str(tmp_path / "stage"), ["vm:test"])
    assert summary.snapshot_id == "abc"
    assert events == [(str(tmp_path / "stage"), "abc")]


def test_replication_copies_entire_incremental_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIC_PASSWORD", "pw")
    repo = ResticRepository(RepositoryConfig(url="/source", local_path="/source", staging_path=str(tmp_path)), 60)
    replica = ReplicaConfig(name="dr", backend="filesystem", path=str(tmp_path / "dr"), password_env="RESTIC_DR_PASSWORD")
    monkeypatch.setenv("RESTIC_DR_PASSWORD", "pw2")
    monkeypatch.setattr("immutavault.restic.chain_for", lambda sid: ["base", "delta", sid])
    monkeypatch.setattr("immutavault.restic.apply_object_lock", lambda *a, **k: {"enabled": False})
    commands = []
    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if "snapshots" in cmd:
            return SimpleNamespace(stdout='[{"id":"ok"}]')
        return SimpleNamespace(stdout="")
    monkeypatch.setattr("immutavault.restic.run", fake_run)
    result = repo.copy_snapshot("latest", replica)
    copied = [cmd[-1] for cmd in commands if "copy" in cmd]
    assert copied == ["base", "delta", "latest"]
    assert result["chain_snapshots"] == ["base", "delta", "latest"]


def test_vddk_password_is_passed_by_secure_file_reference():
    text = Path(__import__("immutavault.adapters.vmware_incremental", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "password=+{password}" in text
    assert "f\"password={env['GOVC_PASSWORD']}\"" not in text
