from __future__ import annotations

from pathlib import Path

import pytest

from immutavault.adapters.base import VM
from immutavault.adapters.vmware_incremental import VMwareIncrementalAdapter
from immutavault.config import PlatformConfig
from immutavault.incremental import IncrementalResult, IncrementalTransportError, LAYOUT_FILE, TRANSPORT_FILE
from immutavault.integrity import build_manifest, verify_manifest


def cfg(tmp_path: Path, **options) -> PlatformConfig:
    base = {
        "username_env": "TEST_VMWARE_USER",
        "password_env": "TEST_VMWARE_PASS",
        "incremental_cache_root": str(tmp_path / "cache"),
        "incremental_fallback": True,
    }
    base.update(options)
    return PlatformConfig(
        name="vc-main",
        type="vmware",
        endpoint="https://vc/sdk",
        mode="auto",
        include=["*"],
        exclude=[],
        options=base,
    )


class SuccessfulProvider:
    def capabilities(self, *, env):
        return {"available": True, "provider": "test", "transport_modes": ["nbdssl"]}

    def backup(self, *, destination: Path, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / LAYOUT_FILE).write_text('{"version":1}', encoding="utf-8")
        blocks = destination / "disks" / "disk-2000" / "blocks"
        blocks.mkdir(parents=True, exist_ok=True)
        (blocks / "00000000.blk").write_bytes(b"A" * 4096)
        return IncrementalResult(destination, "incremental", changed_bytes=4096, source_bytes_read=4096)

    def restore(self, *, target_name: str, **kwargs):
        return {"name": target_name, "id": "vm-99", "transport": "vddk-cbt"}


class FailingProvider:
    def __init__(self, reason="change_id_reset", fallback_safe=True):
        self.reason = reason
        self.fallback_safe = fallback_safe

    def capabilities(self, *, env):
        return {"available": True}

    def backup(self, *, destination: Path, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.blk").write_bytes(b"partial")
        raise IncrementalTransportError("provider failed", reason=self.reason, fallback_safe=self.fallback_safe)


class FakeFallback:
    def __init__(self):
        self.called = False

    def doctor(self):
        return []

    def export(self, vm, destination: Path, *, dry_run=False):
        self.called = True
        out = destination / vm.name
        if not dry_run:
            out.mkdir(parents=True, exist_ok=True)
            (out / "vm.ovf").write_text("ovf", encoding="utf-8")
        return out


def test_incremental_success_creates_self_contained_snapshot_view(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VMWARE_USER", "u")
    monkeypatch.setenv("TEST_VMWARE_PASS", "p")
    adapter = VMwareIncrementalAdapter(cfg(tmp_path), 60)
    monkeypatch.setattr(adapter, "_provider", lambda: SuccessfulProvider())
    destination = tmp_path / "staging"
    out = adapter.export(VM(id="vm-1", name="sql01"), destination)
    assert (out / LAYOUT_FILE).is_file()
    assert (out / "disks/disk-2000/blocks/00000000.blk").read_bytes() == b"A" * 4096
    cache_file = tmp_path / "cache/vc-main/sql01/disks/disk-2000/blocks/00000000.blk"
    assert cache_file.stat().st_ino == (out / "disks/disk-2000/blocks/00000000.blk").stat().st_ino


def test_change_id_reset_invalidates_cache_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VMWARE_USER", "u")
    monkeypatch.setenv("TEST_VMWARE_PASS", "p")
    adapter = VMwareIncrementalAdapter(cfg(tmp_path), 60)
    monkeypatch.setattr(adapter, "_provider", lambda: FailingProvider("change_id_reset", True))
    fallback = FakeFallback()
    monkeypatch.setattr(adapter, "_fallback_adapter", lambda: fallback)
    out = adapter.export(VM(id="vm-1", name="sql01"), tmp_path / "staging")
    assert fallback.called is True
    assert not (tmp_path / "cache/vc-main/sql01").exists()
    assert "change_id_reset" in (out / TRANSPORT_FILE).read_text()


def test_unsafe_incremental_error_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VMWARE_USER", "u")
    monkeypatch.setenv("TEST_VMWARE_PASS", "p")
    adapter = VMwareIncrementalAdapter(cfg(tmp_path), 60)
    monkeypatch.setattr(adapter, "_provider", lambda: FailingProvider("ambiguous_provider_state", False))
    fallback = FakeFallback()
    monkeypatch.setattr(adapter, "_fallback_adapter", lambda: fallback)
    with pytest.raises(RuntimeError, match="failed closed"):
        adapter.export(VM(id="vm-1", name="sql01"), tmp_path / "staging")
    assert fallback.called is False


def test_strict_mode_reports_missing_native_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VMWARE_USER", "u")
    monkeypatch.setenv("TEST_VMWARE_PASS", "p")
    adapter = VMwareIncrementalAdapter(cfg(tmp_path, incremental_strict=True, vddk_helper=str(tmp_path / "missing")), 60)
    monkeypatch.setattr(adapter, "_fallback_adapter", lambda: FakeFallback())
    problems = adapter.doctor()
    assert any("strict mode is enabled" in p and "no fallback is permitted" in p for p in problems)


def test_reused_manifest_does_not_hash_previous_manifest(tmp_path):
    root = tmp_path / "persistent"
    root.mkdir()
    (root / "data.txt").write_text("one", encoding="utf-8")
    _, first_digest = build_manifest(root)
    ok, _ = verify_manifest(root, expected_digest=first_digest)
    assert ok
    (root / "data.txt").write_text("two", encoding="utf-8")
    manifest, second_digest = build_manifest(root)
    assert all(item["path"] != ".immutavault-manifest.json" for item in manifest["files"])
    ok, errors = verify_manifest(root, expected_digest=second_digest)
    assert ok, errors
