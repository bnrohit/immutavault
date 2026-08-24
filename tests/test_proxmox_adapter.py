from immutavault.adapters.base import VM
from immutavault.adapters.proxmox import ProxmoxAdapter
from immutavault.config import PlatformConfig
from immutavault.runner import CommandResult


def test_proxmox_vzdump_cleanup_runs_on_transfer_failure(monkeypatch, tmp_path):
    cfg = PlatformConfig(name="pve", type="proxmox", endpoint="pve", ssh_user="backup", mode="vzdump")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        text = " ".join(command)
        if command and command[0] == "scp":
            raise RuntimeError("copy failed")
        if "vzdump " in text:
            return CommandResult(command, 0, "vzdump-qemu-100.vma\n", "")
        return CommandResult(command, 0, "", "")
    monkeypatch.setattr("immutavault.adapters.proxmox.run", fake_run)
    import pytest
    with pytest.raises(RuntimeError, match="copy failed"):
        ProxmoxAdapter(cfg, 30).export(VM(id="100", name="prod", kind="qemu"), tmp_path)
    assert any("rm -rf" in " ".join(c) for c in calls)
