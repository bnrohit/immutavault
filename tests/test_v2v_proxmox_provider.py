from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
import yaml

from immutavault.config import PlatformConfig
from immutavault.v2v import ConvertedBundle, ConvertedDisk, ConvertedNIC, V2VInspection, V2VManager
from immutavault.v2v_config import load_v10_config


EXAMPLE = Path("config/enterprise-v1.0.example.yml")


def _bundle(tmp_path, *, firmware="uefi"):
    inspection = V2VInspection(
        os_family="linux", distro="ubuntu", osinfo="ubuntu24.04", arch="x86_64",
        firmware=firmware, disk_count=1, virtual_bytes=2 * 1024**3,
        secure_boot=False, source_tpm=False, source_networks=("Servers",),
    )
    return ConvertedBundle(
        domain_xml=tmp_path / "guest.xml",
        disks=(ConvertedDisk(tmp_path / "disk.qcow2", "qcow2", 2 * 1024**3),),
        nics=(ConvertedNIC("52:54:00:12:34:56", "Servers"),),
        memory_mib=4096, vcpus=4, firmware=firmware, inspection=inspection,
    )


def test_proxmox_dry_run_preserves_firmware_resources_and_nic_map(tmp_path):
    cfg = load_v10_config(EXAMPLE)
    manager = V2VManager(cfg)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    result = manager._restore_proxmox(_bundle(tmp_path), target, "converted-vm", {}, dry_run=True)
    command = result["create_command"]
    assert ["--memory", "4096"] == command[command.index("--memory"):command.index("--memory") + 2]
    assert ["--cores", "4"] == command[command.index("--cores"):command.index("--cores") + 2]
    assert ["--bios", "ovmf"] == command[command.index("--bios"):command.index("--bios") + 2]
    assert "virtio=52:54:00:12:34:56,bridge=vmbr20" in command
    assert result["network_bridges"] == ["vmbr20"]
    assert result["power"] == "off"


def test_missing_nic_mapping_fails_closed(tmp_path):
    cfg = load_v10_config(EXAMPLE)
    manager = V2VManager(cfg)
    target = PlatformConfig(
        name="pve", type="proxmox", endpoint="pve.example", enabled=True,
        mode="vzdump", ssh_user="svc", options={"v2v_storage": "local-lvm"},
    )
    with pytest.raises(RuntimeError, match="no certified network mapping"):
        manager._restore_proxmox(_bundle(tmp_path), target, "converted-vm", {}, dry_run=True)


def test_unused_volume_parser_uses_latest_unused_slot():
    cfg = load_v10_config(EXAMPLE)
    manager = V2VManager(cfg)
    key, volume = manager._unused_volume("unused0: local-lvm:vm-200-disk-0\nunused2: fast:vm-200-disk-2\n")
    assert key == "unused2"
    assert volume == "fast:vm-200-disk-2"


def test_provider_binary_is_sha_pinned_and_capability_attested(tmp_path):
    helper = tmp_path / "provider.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "if sys.argv[1]=='capabilities':\n"
        " print(json.dumps({'protocol':1,'certification_id':'lab-xcpng-1','pairs':['vmware:xcpng'],'features':['inspect','convert','validate','rollback']}))\n"
        "else:\n"
        " print(json.dumps({'status':'success','certification_id':'lab-xcpng-1','result':{'target':'new-vm'},'validation':{'source_read_only':True,'target_new_vm':True,'network_mapped':True,'rollback_available':True}}))\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    digest = sha256(helper.read_bytes()).hexdigest()
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["identity"]["oidc"]["enabled"] = False
    raw["observability"]["websocket_enabled"] = False
    raw["v2v"]["enabled"] = True
    raw["v2v"]["providers"] = [{
        "name": "lab-xcpng", "helper": str(helper), "sha256": digest,
        "certification_id": "lab-xcpng-1", "pairs": ["vmware:xcpng"], "enabled": True,
    }]
    path = tmp_path / "cfg.yml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_v10_config(path)
    manager = V2VManager(cfg)
    provider = cfg.v2v.providers[0]
    assert manager._provider_doctor(provider) == []
    target = next(p for p in cfg.platforms if p.name == "xcp-campus-a")
    result = manager._execute_provider(
        provider,
        source=tmp_path,
        point={"snapshot_id":"s1","platform":"vc-campus-a","platform_type":"vmware","vm_name":"src"},
        target=target, target_name="new-vm", options={}, dry_run=False,
    )
    assert result["certification_id"] == "lab-xcpng-1"
    assert result["validation"]["target_new_vm"] is True


def test_provider_hash_change_is_refused(tmp_path):
    helper = tmp_path / "provider"
    helper.write_text("#!/bin/sh\necho '{}'\n", encoding="utf-8")
    helper.chmod(0o755)
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["identity"]["oidc"]["enabled"] = False
    raw["observability"]["websocket_enabled"] = False
    raw["v2v"]["providers"] = [{
        "name":"x", "helper":str(helper), "sha256":"0"*64,
        "certification_id":"x1", "pairs":["vmware:xcpng"],
    }]
    path = tmp_path / "cfg.yml"; path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_v10_config(path)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        V2VManager(cfg)._provider_payload(cfg.v2v.providers[0], "capabilities")
