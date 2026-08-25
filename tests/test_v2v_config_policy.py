from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from immutavault.v2v import V2VManager
from immutavault.v2v_config import load_v10_config


EXAMPLE = Path("config/enterprise-v1.0.example.yml")


def _write(tmp_path, mutate):
    value = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(value)
    path = tmp_path / "v10.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def _enabled_cfg(tmp_path):
    def mutate(raw):
        raw["identity"]["oidc"]["enabled"] = False
        raw["observability"]["websocket_enabled"] = False
        raw["v2v"]["enabled"] = True
        for platform in raw["platforms"]:
            platform["enabled"] = True
    return load_v10_config(_write(tmp_path, mutate))


def _point(**overrides):
    value = {
        "snapshot_id": "snap-1",
        "platform": "vc-campus-a",
        "platform_type": "vmware",
        "vm_name": "fileserver01",
        "verified": 1,
        "suspicious": 0,
    }
    value.update(overrides)
    return value


def test_v10_example_is_backward_safe_and_v2v_disabled():
    cfg = load_v10_config(EXAMPLE)
    assert cfg.v2v.enabled is False
    assert cfg.v2v.require_verified_point is True
    assert cfg.v2v.allow_suspicious_points is False
    assert cfg.v2v.allow_secure_boot is False
    assert cfg.v2v.virt_v2v_min_version == "2.12.0"


def test_builtin_vmware_to_proxmox_requires_explicit_enable(tmp_path):
    cfg = load_v10_config(EXAMPLE)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    plan = V2VManager(cfg).plan(_point(), target)
    assert plan.allowed is False
    assert "v2v.enabled is false" in plan.reasons


def test_verified_vmware_to_proxmox_is_certified_when_enabled(tmp_path):
    cfg = _enabled_cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    plan = V2VManager(cfg).plan(_point(), target)
    assert plan.allowed is True
    assert plan.mode == "builtin"
    assert plan.certification_id == "immutavault-vmware-proxmox-v1"


def test_unverified_and_suspicious_points_fail_closed(tmp_path):
    cfg = _enabled_cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "pve-campus-a")
    unverified = V2VManager(cfg).plan(_point(verified=0), target)
    suspicious = V2VManager(cfg).plan(_point(suspicious=1), target)
    assert unverified.allowed is False
    assert any("verification" in reason for reason in unverified.reasons)
    assert suspicious.allowed is False
    assert any("suspicious" in reason for reason in suspicious.reasons)


def test_xcpng_stays_blocked_without_certified_provider(tmp_path):
    cfg = _enabled_cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "xcp-campus-a")
    plan = V2VManager(cfg).plan(_point(), target)
    assert plan.allowed is False
    assert plan.mode == "blocked"
    assert any("no certified conversion provider" in reason for reason in plan.reasons)


def test_same_family_remains_native(tmp_path):
    cfg = _enabled_cfg(tmp_path)
    target = next(p for p in cfg.platforms if p.name == "vc-campus-a")
    plan = V2VManager(cfg).plan(_point(), target)
    assert plan.allowed is True
    assert plan.mode == "native"


def test_invalid_provider_digest_is_rejected(tmp_path):
    def mutate(raw):
        raw["v2v"]["providers"] = [{
            "name": "bad",
            "helper": "/usr/local/libexec/bad",
            "sha256": "not-a-digest",
            "certification_id": "test",
            "pairs": ["vmware:xcpng"],
        }]
    with pytest.raises(ValueError, match="sha256"):
        load_v10_config(_write(tmp_path, mutate))


def test_duplicate_provider_pair_ownership_is_rejected(tmp_path):
    digest = "a" * 64
    def mutate(raw):
        raw["v2v"]["providers"] = [
            {"name": "one", "helper": "/one", "sha256": digest, "certification_id": "c1", "pairs": ["vmware:xcpng"]},
            {"name": "two", "helper": "/two", "sha256": digest, "certification_id": "c2", "pairs": ["vmware:xcpng"]},
        ]
    with pytest.raises(ValueError, match="claimed by both"):
        load_v10_config(_write(tmp_path, mutate))
