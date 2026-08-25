from __future__ import annotations

import json
from pathlib import Path

import pytest

from immutavault.runner import CommandResult
from immutavault.v2v import V2VInspection
from immutavault.v2v_cert import CertifiedV2VManager
from immutavault.v2v_config import load_v10_config


CFG = load_v10_config("config/enterprise-v1.0.example.yml")


def _manager():
    return CertifiedV2VManager(CFG)


def _inspector_xml() -> str:
    return """<v2v-inspection>
<operatingsystem><name>linux</name><distro>ubuntu</distro><osinfo>ubuntu24.04</osinfo><arch>x86_64</arch></operatingsystem>
<disks><disk index='0'><virtual-size>2147483648</virtual-size></disk></disks>
</v2v-inspection>"""


def test_certified_prepare_ova_uses_per_nic_connection_and_uefi_hint(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "disk.vmdk").write_bytes(b"disk")
    (source / "guest.ovf").write_text(
        """<Envelope xmlns:ovf='http://schemas.dmtf.org/ovf/envelope/1' xmlns:rasd='http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData'>
<References><File ovf:id='f1' ovf:href='disk.vmdk'/></References>
<VirtualSystem><VirtualHardwareSection>
<Item><rasd:ResourceType>10</rasd:ResourceType><rasd:Connection>Servers</rasd:Connection></Item>
</VirtualHardwareSection><Extra>firmware=efi</Extra></VirtualSystem>
</Envelope>""",
        encoding="utf-8",
    )
    work = tmp_path / "work"; work.mkdir()
    _, flags = _manager()._prepare_ova(source, work)
    assert flags["source_networks"] == ["Servers"]
    assert flags["firmware_hint"] == "uefi"


def test_unknown_firmware_is_blocked_instead_of_assumed_bios(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "immutavault.v2v.run",
        lambda command, **kwargs: CommandResult([str(x) for x in command], 0, _inspector_xml(), ""),
    )
    with pytest.raises(RuntimeError, match="firmware could not be attested"):
        _manager()._inspection(tmp_path / "source.ova", {
            "secure_boot": False, "source_tpm": False,
            "firmware_hint": "unknown", "source_networks": ["Servers"],
        })


def test_bundle_rejects_nic_count_that_cannot_be_correlated(monkeypatch, tmp_path):
    disk = tmp_path / "disk.qcow2"; disk.write_bytes(b"qcow")
    xml = tmp_path / "guest.xml"
    xml.write_text(
        f"""<domain><memory unit='MiB'>2048</memory><vcpu>2</vcpu><devices>
<disk device='disk'><source file='{disk}'/></disk>
<interface><mac address='52:54:00:11:22:33'/><source network='default'/></interface>
</devices></domain>""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "immutavault.v2v.run",
        lambda command, **kwargs: CommandResult([str(x) for x in command], 0, json.dumps({"format":"qcow2","virtual-size":2147483648}), ""),
    )
    inspection = V2VInspection(
        os_family="linux", distro="ubuntu", osinfo="ubuntu24.04", arch="x86_64",
        firmware="uefi", disk_count=1, virtual_bytes=2147483648,
        secure_boot=False, source_tpm=False, source_networks=("Servers", "Backup"),
    )
    with pytest.raises(RuntimeError, match="NIC count does not match"):
        _manager()._parse_bundle(tmp_path, inspection)
