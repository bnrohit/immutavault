from __future__ import annotations

import json
from pathlib import Path

import yaml

from immutavault.v2v_cert import CertifiedV2VManager
from immutavault.v2v_config import load_v10_config


EXAMPLE = Path("config/enterprise-v1.0.example.yml")


def _cfg(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["identity"]["oidc"]["enabled"] = False
    raw["observability"]["websocket_enabled"] = False
    raw["v2v"]["enabled"] = True
    for platform in raw["platforms"]:
        platform["enabled"] = True
    path = tmp_path / "v10.yml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_v10_config(path)


def _point(transport):
    return {
        "snapshot_id": "s1",
        "platform": "vc-campus-a",
        "platform_type": "vmware",
        "vm_name": "server1",
        "verified": 1,
        "suspicious": 0,
        "metadata_json": json.dumps({
            "platform_info": {"backup_transport": {"requested": transport}},
        }),
    }


def test_hot_clone_export_is_valid_builtin_v2v_source(tmp_path):
    cfg = _cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    plan = CertifiedV2VManager(cfg).plan(_point("hot-clone-export"), target)
    assert plan.allowed is True
    assert plan.certification_id == "immutavault-vmware-proxmox-v1"


def test_native_vddk_point_is_not_misrepresented_as_ovf(tmp_path):
    cfg = _cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    plan = CertifiedV2VManager(cfg).plan(_point("vddk"), target)
    assert plan.allowed is False
    assert any("native CBT/VDDK layout" in reason for reason in plan.reasons)


def test_missing_source_format_attestation_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    point = _point("hot-clone-export")
    point["metadata_json"] = "{}"
    plan = CertifiedV2VManager(cfg).plan(point, target)
    assert plan.allowed is False
    assert any("does not attest" in reason for reason in plan.reasons)
