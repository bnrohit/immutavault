from types import SimpleNamespace
import pytest

from immutavault.dr_config import parse_dr
from immutavault.dr_network import DRNetworkManager
from immutavault.runner import CommandResult


def _cfg():
    return parse_dr({
        "enabled": True,
        "primary_site": "main", "dr_site": "dr", "replica": "r",
        "sites": [
            {"name": "main", "gateway": {"host": "main-gw", "underlay_interface": "bond0", "trunk_interface": "bond1", "vtep_ip": "10.255.0.1", "router_id": "10.255.0.1", "ospf_auth_key_env": "OSPF_KEY"}},
            {"name": "dr", "gateway": {"host": "dr-gw", "underlay_interface": "bond0", "trunk_interface": "bond1", "vtep_ip": "10.255.0.2", "router_id": "10.255.0.2", "ospf_auth_key_env": "OSPF_KEY"}},
        ],
        "networks": [{"name": "servers", "vlan_id": 48, "vni": 10048, "subnet": "10.14.48.0/24", "gateway_cidr": "10.14.48.1/24"}],
        "workloads": [{"name": "vm", "source_platform": "p1", "target_platform": "p2"}],
    })


def test_dr_network_plan_builds_vlan_vxlan_bridge():
    manager = DRNetworkManager(_cfg())
    plan = manager.plan("dr")
    joined = "\n".join(plan["commands"])
    assert "type vxlan id 10048" in joined
    assert "remote 10.255.0.1" in joined
    assert "type vlan id 48" in joined
    assert "imvb48" in joined


def test_dr_network_preflight_is_read_only(monkeypatch):
    manager = DRNetworkManager(_cfg())
    seen = []
    def fake_run(site, script, check=True):
        seen.append(script)
        return CommandResult([], 0, "", "")
    monkeypatch.setattr(manager, "_run", fake_run)
    result = manager.preflight("dr")
    assert result["ok"] is True
    text = seen[0]
    assert "ip route get 10.255.0.1" in text
    assert "ip link add" not in text
    assert "vtysh -c 'show version'" in text


def test_ospf_key_length_is_rejected(monkeypatch):
    manager = DRNetworkManager(_cfg())
    monkeypatch.setenv("OSPF_KEY", "this-key-is-way-too-long")
    def fake_run(site, script, check=True):
        return CommandResult([], 0, "", "")
    monkeypatch.setattr(manager, "_run", fake_run)
    with pytest.raises(RuntimeError, match="at most 16 bytes"):
        manager.prepare("dr")
