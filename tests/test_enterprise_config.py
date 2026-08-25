from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from immutavault.enterprise_config import load_enterprise_config


EXAMPLE = Path("config/enterprise-v0.9.example.yml")


def _write(tmp_path, mutate):
    value = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(value)
    path = tmp_path / "enterprise.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_enterprise_example_validates():
    cfg = load_enterprise_config(EXAMPLE)
    assert cfg.tenant_for_platform("vc-campus-a") == "campus-a"
    assert cfg.tenant_for_platform("vc-campus-b") == "campus-b"
    assert cfg.oidc.enabled is True
    assert cfg.oidc.require_mfa is True
    assert cfg.oidc.allow_local_tokens is False
    assert cfg.observability.metrics_enabled is True
    assert cfg.observability.websocket_enabled is True
    assert cfg.observability.websocket_public_url == "wss://backup.example.com:8788"


def test_overlapping_tenant_platform_patterns_fail_closed(tmp_path):
    path = _write(tmp_path, lambda raw: raw.update({"tenants": [
        {"id": "all", "platforms": ["*"]},
        {"id": "vmware", "platforms": ["vc-*"]},
    ]}))
    with pytest.raises(ValueError, match="must match exactly one tenant"):
        load_enterprise_config(path)


def test_unassigned_platform_fails_closed(tmp_path):
    path = _write(tmp_path, lambda raw: raw.update({"tenants": [
        {"id": "only-a", "platforms": ["vc-campus-a"]},
    ]}))
    with pytest.raises(ValueError, match="must match exactly one tenant"):
        load_enterprise_config(path)


def test_portal_user_unknown_tenant_is_rejected(tmp_path):
    def mutate(raw):
        raw["identity"]["oidc"]["enabled"] = False
        raw["portal"]["users"][0]["tenants"] = ["does-not-exist"]
    path = _write(tmp_path, mutate)
    with pytest.raises(ValueError, match="unknown tenant"):
        load_enterprise_config(path)


def test_websocket_port_cannot_overlap_portal(tmp_path):
    def mutate(raw):
        raw["observability"]["websocket_port"] = raw["portal"]["port"]
    path = _write(tmp_path, mutate)
    with pytest.raises(ValueError, match="must differ from portal.port"):
        load_enterprise_config(path)
