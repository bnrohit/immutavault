from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from immutavault.v2v import ConvertedBundle, ConvertedDisk, ConvertedNIC, V2VInspection
from immutavault.v2v_cert import CertifiedV2VManager
from immutavault.v2v_config import load_v10_config


EXAMPLE = Path("config/enterprise-v1.0.example.yml")


class FakeAdapter:
    def __init__(self, cfg, timeout):
        self.cfg = cfg

    def _ssh(self):
        return ["ssh", "pve.example"]


def _bundle(tmp_path):
    inspection = V2VInspection(
        os_family="linux", distro="ubuntu", osinfo="ubuntu24.04", arch="x86_64",
        firmware="uefi", disk_count=1, virtual_bytes=2 * 1024**3,
        secure_boot=False, source_tpm=False, source_networks=("Servers",),
    )
    return ConvertedBundle(
        domain_xml=tmp_path / "guest.xml",
        disks=(ConvertedDisk(tmp_path / "disk.qcow2", "qcow2", 2 * 1024**3),),
        nics=(ConvertedNIC("52:54:00:12:34:56", "Servers"),),
        memory_mib=4096, vcpus=4, firmware="uefi", inspection=inspection,
    )


def test_target_storage_and_bridge_preflight_succeeds(monkeypatch, tmp_path):
    cfg = load_v10_config(EXAMPLE)
    manager = CertifiedV2VManager(cfg)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    monkeypatch.setattr("immutavault.v2v_cert.ProxmoxAdapter", FakeAdapter)

    seen = []
    def fake_run(command, **kwargs):
        seen.append(command[-1])
        if command[-1].startswith("pvesm status"):
            return SimpleNamespace(returncode=0, stdout="Name Type Status Total Used Available %\nlocal-lvm lvmthin active 1000 100 900 10%\n", stderr="")
        if command[-1].startswith("ip -o link show dev"):
            return SimpleNamespace(returncode=0, stdout="10: vmbr20: <BROADCAST,UP>\n", stderr="")
        raise AssertionError(command)
    monkeypatch.setattr("immutavault.v2v_cert.run", fake_run)

    manager._preflight_proxmox_storage(target, {})
    assert manager._preflight_proxmox_bridges(_bundle(tmp_path), target, {}) == ["vmbr20"]
    assert any("--content images --enabled 1" in item for item in seen)
    assert any("ip -o link show dev vmbr20" in item for item in seen)


def test_disabled_or_missing_image_storage_fails_before_conversion(monkeypatch):
    cfg = load_v10_config(EXAMPLE)
    manager = CertifiedV2VManager(cfg)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    monkeypatch.setattr("immutavault.v2v_cert.ProxmoxAdapter", FakeAdapter)
    monkeypatch.setattr(
        "immutavault.v2v_cert.run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="Name Type Status Total Used Available %\n", stderr=""),
    )
    with pytest.raises(RuntimeError, match="not enabled/available for VM image content"):
        manager._preflight_proxmox_storage(target, {})


def test_missing_target_bridge_fails_before_vm_creation(monkeypatch, tmp_path):
    cfg = load_v10_config(EXAMPLE)
    manager = CertifiedV2VManager(cfg)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    monkeypatch.setattr("immutavault.v2v_cert.ProxmoxAdapter", FakeAdapter)
    monkeypatch.setattr(
        "immutavault.v2v_cert.run",
        lambda command, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Device does not exist"),
    )
    with pytest.raises(RuntimeError, match="target bridge 'vmbr20' does not exist"):
        manager._preflight_proxmox_bridges(_bundle(tmp_path), target, {})
