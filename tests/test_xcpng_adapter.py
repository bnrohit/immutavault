from immutavault.adapters.base import VM
from immutavault.adapters.xcpng import XCPNGAdapter
from immutavault.config import PlatformConfig
from immutavault.runner import CommandResult


def test_xcpng_snapshot_export_and_cleanup(monkeypatch, tmp_path):
    cfg = PlatformConfig(name="xcp", type="xcpng", endpoint="xcp", ssh_user="backup", mode="snapshot-export")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if any("xe vm-snapshot" in part for part in command):
            return CommandResult(command, 0, "snapshot-uuid\n", "")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.xcpng.run", fake_run)
    out = XCPNGAdapter(cfg, 30).export(VM(id="vm-uuid", name="prod", kind="xcpng"), tmp_path)
    assert out.name.endswith(".xva")
    joined = [" ".join(c) for c in calls]
    assert any("xe vm-snapshot" in c for c in joined)
    assert any("xe vm-export uuid=snapshot-uuid" in c for c in joined)
    assert any("snapshot-uninstall snapshot-uuid=snapshot-uuid" in c for c in joined)


def test_xcpng_cleanup_runs_when_export_fails(monkeypatch, tmp_path):
    cfg = PlatformConfig(name="xcp", type="xcpng", endpoint="xcp", ssh_user="backup", mode="snapshot-export")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        text = " ".join(command)
        if "xe vm-snapshot" in text:
            return CommandResult(command, 0, "snapshot-uuid\n", "")
        if "xe vm-export" in text:
            raise RuntimeError("export failed")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.xcpng.run", fake_run)
    import pytest
    with pytest.raises(RuntimeError, match="export failed"):
        XCPNGAdapter(cfg, 30).export(VM(id="vm-uuid", name="prod", kind="xcpng"), tmp_path)
    joined = [" ".join(c) for c in calls]
    assert any("rm -f" in c for c in joined)
    assert any("snapshot-uninstall" in c for c in joined)
