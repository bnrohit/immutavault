from immutavault.adapters.vmware import VMwareAdapter
from immutavault.config import PlatformConfig
from immutavault.runner import CommandResult


def test_vmware_uses_per_platform_credentials(monkeypatch):
    cfg = PlatformConfig(
        name="vc-dr", type="vmware", endpoint="https://vc-dr.example/sdk", mode="hot-clone-export",
        options={"username_env": "VC_DR_USER", "password_env": "VC_DR_PASS", "datacenter": "DR-DC", "tls_ca_certs": "/etc/ca.pem"},
    )
    monkeypatch.setenv("VC_DR_USER", "backup@vsphere.local")
    monkeypatch.setenv("VC_DR_PASS", "secret")
    monkeypatch.setattr("immutavault.adapters.vmware.shutil.which", lambda name: "/usr/local/bin/govc")
    captured = []
    def fake_run(command, **kwargs):
        captured.append(kwargs.get("env", {}))
        if len(command) >= 3 and command[1] == "import.ovf" and command[2] == "-h":
            return CommandResult(command, 0, "Usage: govc import.ovf -net string -options string", "")
        if command[-1:] == ["-h"]:
            return CommandResult(command, 0, "Usage", "")
        return CommandResult(command, 0, '{"about": {}}', "")
    monkeypatch.setattr("immutavault.adapters.vmware.run", fake_run)
    adapter = VMwareAdapter(cfg, 30)
    assert adapter.doctor() == []
    env = captured[0]
    assert env["GOVC_URL"] == "https://vc-dr.example/sdk"
    assert env["GOVC_USERNAME"] == "backup@vsphere.local"
    assert env["GOVC_DATACENTER"] == "DR-DC"
    assert env["GOVC_TLS_CA_CERTS"] == "/etc/ca.pem"
    assert env["GOVC_INSECURE"] == "false"


def test_vmware_insecure_is_not_production_healthy(monkeypatch):
    cfg = PlatformConfig(name="vc", type="vmware", endpoint="https://vc/sdk", options={"insecure": True})
    monkeypatch.setenv("GOVC_USERNAME", "u")
    monkeypatch.setenv("GOVC_PASSWORD", "p")
    monkeypatch.setattr("immutavault.adapters.vmware.shutil.which", lambda name: "/usr/bin/govc")
    monkeypatch.setattr("immutavault.adapters.vmware.run", lambda *a, **k: CommandResult([], 0, "{}", ""))
    problems = VMwareAdapter(cfg, 30).doctor()
    assert any("TLS verification is disabled" in p for p in problems)


def test_vmware_hot_clone_export_keeps_source_running_and_cleans_up(monkeypatch, tmp_path):
    from immutavault.adapters.base import VM
    cfg = PlatformConfig(
        name="vc", type="vmware", endpoint="https://vc/sdk", mode="hot-clone-export",
        options={"username_env": "U", "password_env": "P", "quiesce": True},
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.vmware.run", fake_run)
    monkeypatch.setattr("immutavault.adapters.vmware.uuid.uuid4", lambda: type("U", (), {"hex": "1234567890abcdef"})())
    adapter = VMwareAdapter(cfg, 30)
    out = adapter.export(VM(id="/DC/vm/prod", name="prod", kind="vmware"), tmp_path)
    assert out == tmp_path / "prod"
    assert commands[0][:3] == ["govc", "snapshot.create", "-vm"]
    assert "-q=true" in commands[0]
    assert commands[1][:2] == ["govc", "vm.clone"]
    assert "-on=false" in commands[1]
    assert commands[2][:2] == ["govc", "export.ovf"]
    assert commands[-2][:2] == ["govc", "vm.destroy"]
    assert commands[-1][:2] == ["govc", "snapshot.remove"]
    assert "-c=true" in commands[-1]


def test_vmware_quiesce_failure_is_strict_by_default(monkeypatch, tmp_path):
    from immutavault.adapters.base import VM
    cfg = PlatformConfig(
        name="vc", type="vmware", endpoint="https://vc/sdk", mode="hot-clone-export",
        options={"username_env": "U", "password_env": "P", "quiesce": True, "quiesce_fallback_crash_consistent": False},
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "snapshot.create":
            return CommandResult(command, 1, "", "tools quiesce unavailable")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.vmware.run", fake_run)
    import pytest
    with pytest.raises(RuntimeError, match="snapshot failed"):
        VMwareAdapter(cfg, 30).export(VM(id="vm", name="vm", kind="vmware"), tmp_path)
    assert not any(c[1] == "vm.clone" for c in calls)


def test_vmware_quiesce_can_explicitly_fallback_to_crash_consistent(monkeypatch, tmp_path):
    from immutavault.adapters.base import VM
    cfg = PlatformConfig(
        name="vc", type="vmware", endpoint="https://vc/sdk", mode="hot-clone-export",
        options={"username_env": "U", "password_env": "P", "quiesce": True, "quiesce_fallback_crash_consistent": True},
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    calls = []
    attempts = {"snapshot": 0}
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "snapshot.create":
            attempts["snapshot"] += 1
            if attempts["snapshot"] == 1:
                return CommandResult(command, 1, "", "quiesce failed")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.vmware.run", fake_run)
    VMwareAdapter(cfg, 30).export(VM(id="vm", name="vm", kind="vmware"), tmp_path)
    snapshots = [c for c in calls if c[1] == "snapshot.create"]
    assert len(snapshots) == 2
    assert "-q=true" in snapshots[0]
    assert "-q=false" in snapshots[1]


def test_vmware_success_is_not_reported_if_snapshot_cleanup_fails(monkeypatch, tmp_path):
    from immutavault.adapters.base import VM
    cfg = PlatformConfig(
        name="vc", type="vmware", endpoint="https://vc/sdk", mode="hot-clone-export",
        options={"username_env": "U", "password_env": "P", "quiesce": False},
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    def fake_run(command, **kwargs):
        if command[1] == "snapshot.remove":
            return CommandResult(command, 1, "", "consolidation required")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.vmware.run", fake_run)
    import pytest
    with pytest.raises(RuntimeError, match="cleanup requires operator attention"):
        VMwareAdapter(cfg, 30).export(VM(id="vm", name="vm", kind="vmware"), tmp_path)


def test_vmware_restore_can_remap_network_on_different_vcenter(monkeypatch, tmp_path):
    cfg = PlatformConfig(
        name="vc-dr", type="vmware", endpoint="https://vc-dr/sdk", mode="hot-clone-export",
        options={"username_env": "U", "password_env": "P"},
    )
    monkeypatch.setenv("U", "u")
    monkeypatch.setenv("P", "p")
    (tmp_path / "vm.ovf").write_text("<Envelope/>", encoding="utf-8")
    result = VMwareAdapter(cfg, 30).restore(
        tmp_path,
        target_name="restored",
        options={"network": "DR-Servers"},
        dry_run=True,
    )
    assert result["command"][:4] == ["govc", "import.ovf", "-name", "restored"]
    assert "-net" in result["command"]
    assert "DR-Servers" in result["command"]



def test_vmware_doctor_rejects_govc_without_network_mapping(monkeypatch):
    cfg = PlatformConfig(name="vc", type="vmware", endpoint="https://vc/sdk", options={"username_env": "U", "password_env": "P"})
    monkeypatch.setenv("U", "u"); monkeypatch.setenv("P", "p")
    monkeypatch.setattr("immutavault.adapters.vmware.shutil.which", lambda name: "/usr/bin/govc")
    def fake_run(command, **kwargs):
        if command[-1:] == ["-h"]:
            return CommandResult(command, 0, "Usage without network mapping", "")
        return CommandResult(command, 0, "{}", "")
    monkeypatch.setattr("immutavault.adapters.vmware.run", fake_run)
    problems = VMwareAdapter(cfg, 30).doctor()
    assert any("lacks -net mapping" in p for p in problems)
