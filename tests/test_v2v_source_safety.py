from __future__ import annotations

from pathlib import Path
import tarfile

import pytest

from immutavault.runner import CommandResult
from immutavault.v2v import V2VManager
from immutavault.v2v_config import load_v10_config


CFG = load_v10_config("config/enterprise-v1.0.example.yml")


def _manager():
    return V2VManager(CFG)


def _ovf(root: Path, href: str = "disk.vmdk") -> Path:
    path = root / "guest.ovf"
    path.write_text(
        f'''<?xml version="1.0"?>
<Envelope xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1">
  <References><File ovf:id="file1" ovf:href="{href}"/></References>
  <NetworkSection><Network ovf:name="VM Network"/></NetworkSection>
</Envelope>''',
        encoding="utf-8",
    )
    return path


def _inspection_xml(*, firmware: str = "uefi", os_family: str = "linux", arch: str = "x86_64") -> str:
    return f'''<virt-v2v-inspection>
<operatingsystem>
  <name>{os_family}</name><distro>ubuntu</distro><osinfo>ubuntu24.04</osinfo><arch>{arch}</arch>
</operatingsystem>
<firmware type="{firmware}"/>
<disks><disk><virtual-size>2147483648</virtual-size></disk></disks>
</virt-v2v-inspection>'''


def test_prepare_ova_packages_only_safe_referenced_files(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    _ovf(source)
    (source / "disk.vmdk").write_bytes(b"fake-vmdk")
    work = tmp_path / "work"; work.mkdir()
    ova, flags = _manager()._prepare_ova(source, work)
    assert ova.is_file()
    assert flags["source_networks"] == ["VM Network"]
    with tarfile.open(ova, "r") as archive:
        assert sorted(archive.getnames()) == ["disk.vmdk", "guest.ovf"]


def test_prepare_ova_rejects_path_traversal_reference(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    _ovf(source, "../disk.vmdk")
    (tmp_path / "disk.vmdk").write_bytes(b"outside")
    work = tmp_path / "work"; work.mkdir()
    with pytest.raises(RuntimeError, match="unsafe external file reference"):
        _manager()._prepare_ova(source, work)


def test_prepare_ova_rejects_symlinks(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    _ovf(source)
    real = tmp_path / "real.vmdk"; real.write_bytes(b"disk")
    (source / "disk.vmdk").symlink_to(real)
    work = tmp_path / "work"; work.mkdir()
    with pytest.raises(RuntimeError, match="symlink"):
        _manager()._prepare_ova(source, work)


def test_inspection_accepts_linux_x86_64_uefi(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return CommandResult([str(x) for x in command], 0, _inspection_xml(), "")
    monkeypatch.setattr("immutavault.v2v.run", fake_run)
    inspection = _manager()._inspection(tmp_path / "source.ova", {
        "secure_boot": False, "source_tpm": False, "firmware_hint": "bios", "source_networks": ["Servers"]
    })
    assert inspection.os_family == "linux"
    assert inspection.firmware == "uefi"
    assert inspection.disk_count == 1
    assert inspection.source_networks == ("Servers",)


def test_inspection_blocks_virtual_tpm(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "immutavault.v2v.run",
        lambda command, **kwargs: CommandResult([str(x) for x in command], 0, _inspection_xml(), ""),
    )
    with pytest.raises(RuntimeError, match="TPM"):
        _manager()._inspection(tmp_path / "source.ova", {
            "secure_boot": False, "source_tpm": True, "firmware_hint": "uefi", "source_networks": []
        })


def test_inspection_blocks_secure_boot_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "immutavault.v2v.run",
        lambda command, **kwargs: CommandResult([str(x) for x in command], 0, _inspection_xml(), ""),
    )
    with pytest.raises(RuntimeError, match="Secure Boot"):
        _manager()._inspection(tmp_path / "source.ova", {
            "secure_boot": True, "source_tpm": False, "firmware_hint": "uefi", "source_networks": []
        })


def test_inspection_blocks_non_x86_guest(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "immutavault.v2v.run",
        lambda command, **kwargs: CommandResult([str(x) for x in command], 0, _inspection_xml(arch="aarch64"), ""),
    )
    with pytest.raises(RuntimeError, match="architecture"):
        _manager()._inspection(tmp_path / "source.ova", {
            "secure_boot": False, "source_tpm": False, "firmware_hint": "uefi", "source_networks": []
        })
