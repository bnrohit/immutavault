from __future__ import annotations

from pathlib import Path

import pytest

from immutavault.adapters.base import VM
from immutavault.adapters.vmware_incremental import VMwareIncrementalAdapter
from immutavault.config import PlatformConfig
from immutavault.incremental import IncrementalTransportError, VDDKProvider


def _cfg(tmp_path: Path, **options) -> PlatformConfig:
    values = {
        "username_env": "V07_USER",
        "password_env": "V07_PASS",
        "incremental_cache_root": str(tmp_path / "cache"),
        "incremental_fallback": True,
    }
    values.update(options)
    return PlatformConfig(
        name="vc-main",
        type="vmware",
        endpoint="https://vc/sdk",
        mode="auto",
        include=["*"],
        exclude=[],
        options=values,
    )


class PartialFailure:
    def capabilities(self, *, env):
        return {"available": True}

    def backup(self, *, destination: Path, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partially-written-block").write_bytes(b"unsafe")
        raise IncrementalTransportError("partial provider failure", reason="provider_io_error", fallback_safe=False)


class SafeMissingProvider:
    def capabilities(self, *, env):
        return {"available": False, "reason": "helper_missing"}

    def backup(self, **kwargs):
        raise AssertionError("backup must not start after a failed capability probe")


class FakeFallback:
    def doctor(self):
        return []

    def export(self, vm, destination: Path, *, dry_run=False):
        out = destination / vm.name
        if not dry_run:
            out.mkdir(parents=True, exist_ok=True)
            (out / "vm.ovf").write_text("ovf", encoding="utf-8")
        return out


def test_provider_cache_directory_is_private(tmp_path):
    dest = tmp_path / "cache"
    VDDKProvider._secure_dir(dest)
    assert dest.stat().st_mode & 0o777 == 0o700


def test_any_started_provider_failure_discards_partially_written_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("V07_USER", "user")
    monkeypatch.setenv("V07_PASS", "pass")
    adapter = VMwareIncrementalAdapter(_cfg(tmp_path), 60)
    monkeypatch.setattr(adapter, "_provider", lambda: PartialFailure())
    with pytest.raises(RuntimeError, match="cannot safely fall back"):
        adapter.export(VM(id="vm-1", name="sql01"), tmp_path / "staging")
    assert not (tmp_path / "cache/vc-main/sql01").exists()


def test_missing_helper_uses_full_fallback_without_destroying_good_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("V07_USER", "user")
    monkeypatch.setenv("V07_PASS", "pass")
    adapter = VMwareIncrementalAdapter(_cfg(tmp_path), 60)
    monkeypatch.setattr(adapter, "_provider", lambda: SafeMissingProvider())
    monkeypatch.setattr(adapter, "_fallback_adapter", lambda: FakeFallback())
    cache = tmp_path / "cache/vc-main/sql01"
    cache.mkdir(parents=True)
    (cache / ".immutavault-cbt-checkpoint.json").write_text('{"disk":"uuid/7"}', encoding="utf-8")
    out = adapter.export(VM(id="vm-1", name="sql01"), tmp_path / "staging")
    marker = (out / ".immutavault-transport.json").read_text(encoding="utf-8")
    assert '"mode": "fallback-full"' in marker
    assert '"fallback_reason": "helper_missing"' in marker
    assert '"native_cache_invalidated": false' in marker
    assert (cache / ".immutavault-cbt-checkpoint.json").is_file()


def test_explicit_hot_clone_mode_is_not_native_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("V07_USER", "user")
    monkeypatch.setenv("V07_PASS", "pass")
    original = _cfg(tmp_path)
    legacy = PlatformConfig(**{**original.__dict__, "mode": "hot-clone-export"})
    adapter = VMwareIncrementalAdapter(legacy, 60)
    assert adapter._incremental_mode() is False
