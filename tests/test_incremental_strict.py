from pathlib import Path

import pytest

from immutavault.adapters.base import VM
from immutavault.adapters.vmware import VMwareAdapter
from immutavault.adapters.vmware_incremental import (
    VMwareIncrementalAdapter,
    incremental_fallback_allowed,
)
from immutavault.config import PlatformConfig
from immutavault.incremental import IncrementalTransportError, VDDKProvider


def _cfg(*, strict: bool, fallback: bool = True) -> PlatformConfig:
    return PlatformConfig(
        name="vc-main",
        type="vmware",
        endpoint="https://vc.example/sdk",
        mode="vddk",
        include=["*"],
        exclude=[],
        options={
            "username_env": "VC_USER",
            "password_env": "VC_PASS",
            "incremental_strict": strict,
            "incremental_fallback": fallback,
            "incremental_cache_root": "/tmp/immutavault-test-vddk",
        },
    )


def test_incremental_strict_blocks_even_explicitly_safe_provider_fallback():
    assert incremental_fallback_allowed(
        strict=True,
        configured=True,
        reason="change_id_reset",
        fallback_safe=True,
    ) is False


def test_ambiguous_or_unknown_provider_reason_always_fails_closed():
    assert incremental_fallback_allowed(
        strict=False,
        configured=True,
        reason="provider_error",
        fallback_safe=True,
    ) is False
    assert incremental_fallback_allowed(
        strict=False,
        configured=True,
        reason="checkpoint_corrupt",
        fallback_safe=True,
    ) is False


def test_safe_reason_still_requires_provider_fallback_safe_true():
    assert incremental_fallback_allowed(
        strict=False,
        configured=True,
        reason="change_id_reset",
        fallback_safe=False,
    ) is False
    assert incremental_fallback_allowed(
        strict=False,
        configured=True,
        reason="change_id_reset",
        fallback_safe=True,
    ) is True


def test_capability_probe_failure_is_ambiguous_and_not_fallback_safe():
    assert incremental_fallback_allowed(
        strict=False,
        configured=True,
        reason="capability_probe_failed",
        fallback_safe=True,
        capability_stage=True,
    ) is False
    assert incremental_fallback_allowed(
        strict=False,
        configured=True,
        reason="helper_missing",
        fallback_safe=True,
        capability_stage=True,
    ) is True


def test_strict_adapter_never_calls_hot_clone_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_USER", "user")
    monkeypatch.setenv("VC_PASS", "pass")
    adapter = VMwareIncrementalAdapter(_cfg(strict=True), 60)

    class Provider:
        def capabilities(self, *, env):
            return {"available": True, "reason": None}

        def backup(self, **kwargs):
            raise IncrementalTransportError(
                "CBT generation changed",
                reason="change_id_reset",
                fallback_safe=True,
            )

    monkeypatch.setattr(adapter, "_provider", lambda: Provider())
    fallback_called = {"value": False}

    def forbidden_fallback(self, vm, destination, *, dry_run=False):
        fallback_called["value"] = True
        return destination / vm.name

    monkeypatch.setattr(VMwareAdapter, "export", forbidden_fallback)

    with pytest.raises(RuntimeError, match="incremental_strict=true"):
        adapter.export(VM(id="vm-1", name="sql01"), tmp_path)
    assert fallback_called["value"] is False


def test_nonstrict_safe_provider_state_can_use_full_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_USER", "user")
    monkeypatch.setenv("VC_PASS", "pass")
    adapter = VMwareIncrementalAdapter(_cfg(strict=False), 60)

    class Provider:
        def capabilities(self, *, env):
            return {"available": True, "reason": None}

        def backup(self, **kwargs):
            raise IncrementalTransportError(
                "CBT generation changed",
                reason="change_id_reset",
                fallback_safe=True,
            )

    monkeypatch.setattr(adapter, "_provider", lambda: Provider())

    def allowed_fallback(self, vm, destination, *, dry_run=False):
        out = destination / vm.name
        out.mkdir(parents=True, exist_ok=True)
        return out

    monkeypatch.setattr(VMwareAdapter, "export", allowed_fallback)
    result = adapter.export(VM(id="vm-1", name="sql01"), tmp_path)
    assert result == tmp_path / "sql01"
    assert (result / ".immutavault-transport.json").is_file()


def test_nonstrict_ambiguous_provider_state_does_not_call_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_USER", "user")
    monkeypatch.setenv("VC_PASS", "pass")
    adapter = VMwareIncrementalAdapter(_cfg(strict=False), 60)

    class Provider:
        def capabilities(self, *, env):
            return {"available": True, "reason": None}

        def backup(self, **kwargs):
            raise IncrementalTransportError(
                "provider returned inconsistent metadata",
                reason="provider_error",
                fallback_safe=True,
            )

    monkeypatch.setattr(adapter, "_provider", lambda: Provider())
    fallback_called = {"value": False}

    def forbidden_fallback(self, vm, destination, *, dry_run=False):
        fallback_called["value"] = True
        return destination / vm.name

    monkeypatch.setattr(VMwareAdapter, "export", forbidden_fallback)

    with pytest.raises(RuntimeError, match="unsafe/ambiguous provider state"):
        adapter.export(VM(id="vm-1", name="sql01"), tmp_path)
    assert fallback_called["value"] is False


def test_provider_omitting_fallback_safe_defaults_to_fail_closed(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1] == 'capabilities':\n"
        "    print(json.dumps({'protocol_version': 1, 'features': ['cbt','backup','restore']}))\n"
        "else:\n"
        "    print(json.dumps({'status': 'fallback', 'reason': 'change_id_reset', 'error': 'reset'}))\n"
        "    raise SystemExit(20)\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
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
    assert exc.value.fallback_safe is False
