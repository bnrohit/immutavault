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
    assert any("xe snapshot-export-to-template snapshot-uuid=snapshot-uuid" in c for c in joined)
    assert any("snapshot-uninstall snapshot-uuid=snapshot-uuid" in c for c in joined)


def test_xcpng_cleanup_runs_when_export_fails(monkeypatch, tmp_path):
    cfg = PlatformConfig(name="xcp", type="xcpng", endpoint="xcp", ssh_user="backup", mode="snapshot-export")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        text = " ".join(command)
        if "xe vm-snapshot" in text:
            return CommandResult(command, 0, "snapshot-uuid\n", "")
        if "xe snapshot-export-to-template" in text:
            raise RuntimeError("export failed")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.xcpng.run", fake_run)
    import pytest
    with pytest.raises(RuntimeError, match="export failed"):
        XCPNGAdapter(cfg, 30).export(VM(id="vm-uuid", name="prod", kind="xcpng"), tmp_path)
    joined = [" ".join(c) for c in calls]
    assert any("rm -f" in c for c in joined)
    assert any("snapshot-uninstall" in c for c in joined)



def test_xcpng_restore_imported_snapshot_template_is_instantiated_as_vm(monkeypatch, tmp_path):
    cfg = PlatformConfig(
        name="xcp", type="xcpng", endpoint="xcp", ssh_user="backup", mode="snapshot-export",
        options={"restore_sr_uuid": "sr-1"},
    )
    xva = tmp_path / "backup.xva"
    xva.write_bytes(b"xva")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        text = " ".join(command)
        if "xe vm-list name-label=restored" in text:
            return CommandResult(command, 0, "", "")
        if "xe vm-import" in text:
            return CommandResult(command, 0, "template-uuid\n", "")
        if "param-name=is-a-template" in text:
            return CommandResult(command, 0, "true\n", "")
        if "xe vm-install" in text:
            return CommandResult(command, 0, "restored-vm-uuid\n", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr("immutavault.adapters.xcpng.run", fake_run)
    result = XCPNGAdapter(cfg, 30).restore(tmp_path, target_name="restored", options={})
    assert result["uuid"] == "restored-vm-uuid"
    joined = [" ".join(c) for c in calls]
    assert any("xe vm-import" in c and "sr-uuid=sr-1" in c for c in joined)
    assert any("xe vm-install template-uuid=template-uuid" in c and "new-name-label=restored" in c for c in joined)
    assert any("xe template-uninstall template-uuid=template-uuid --force" in c for c in joined)


def test_xcpng_restore_historical_vm_xva_remains_supported(monkeypatch, tmp_path):
    cfg = PlatformConfig(name="xcp", type="xcpng", endpoint="xcp", ssh_user="backup", mode="snapshot-export")
    (tmp_path / "backup.xva").write_bytes(b"xva")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        text = " ".join(command)
        if "xe vm-list name-label=restored" in text:
            return CommandResult(command, 0, "", "")
        if "xe vm-import" in text:
            return CommandResult(command, 0, "historical-vm-uuid\n", "")
        if "param-name=is-a-template" in text:
            return CommandResult(command, 0, "false\n", "")
        return CommandResult(command, 0, "", "")

    monkeypatch.setattr("immutavault.adapters.xcpng.run", fake_run)
    result = XCPNGAdapter(cfg, 30).restore(tmp_path, target_name="restored", options={})
    assert result["uuid"] == "historical-vm-uuid"
    joined = [" ".join(c) for c in calls]
    assert any("xe vm-param-set uuid=historical-vm-uuid name-label=restored" in c for c in joined)
    assert not any("xe template-uninstall" in c for c in joined)
