from pathlib import Path
import pytest

from immutavault.config import load_config
from immutavault.dr_config import parse_dr


def _base() -> str:
    return """
repository:
  url: rest:https://vault.example:8000/
  local_path: /srv/immutavault/repository
  staging_path: /srv/immutavault/staging
platforms:
  - name: vc-primary
    type: vmware
    endpoint: https://vc-primary.example/sdk
  - name: vc-dr
    type: vmware
    endpoint: https://vc-dr.example/sdk
replicas:
  - name: dr-vault
    backend: rest
    url: rest:https://dr.example:8000/
disaster_recovery:
  enabled: true
  primary_site: main
  dr_site: dr
  replica: dr-vault
  sites:
    - name: main
      gateway:
        host: main-gw.example
        underlay_interface: bond0
        trunk_interface: bond1
        vtep_ip: 10.255.0.1
        router_id: 10.255.0.1
    - name: dr
      gateway:
        host: dr-gw.example
        underlay_interface: bond0
        trunk_interface: bond1
        vtep_ip: 10.255.0.2
        router_id: 10.255.0.2
  networks:
    - name: servers
      vlan_id: 48
      vni: 10048
      subnet: 10.14.48.0/24
      gateway_cidr: 10.14.48.1/24
  workloads:
    - name: dns01
      source_platform: vc-primary
      target_platform: vc-dr
"""


def test_complete_dr_config_loads(tmp_path: Path):
    path = tmp_path / "cfg.yml"
    path.write_text(_base(), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.dr.enabled
    assert cfg.dr.sites[1].gateway.vtep_ip == "10.255.0.2"
    assert cfg.dr.networks[0].gateway_cidr == "10.14.48.1/24"


def test_dr_rejects_gateway_outside_subnet():
    raw = {
        "enabled": True,
        "primary_site": "a",
        "dr_site": "b",
        "replica": "r",
        "sites": [
            {"name": "a", "gateway": {"host": "a", "vtep_ip": "10.0.0.1", "router_id": "10.0.0.1"}},
            {"name": "b", "gateway": {"host": "b", "vtep_ip": "10.0.0.2", "router_id": "10.0.0.2"}},
        ],
        "networks": [{"vlan_id": 10, "vni": 10010, "subnet": "192.0.2.0/24", "gateway_cidr": "198.51.100.1/24"}],
        "workloads": [{"name": "vm", "source_platform": "p1", "target_platform": "p2"}],
    }
    with pytest.raises(ValueError, match="gateway_cidr"):
        parse_dr(raw)


def test_auto_failover_requires_verified_fencing_and_probe(tmp_path: Path):
    path = tmp_path / "cfg.yml"
    text = _base().replace("  sites:\n", "  auto_failover: true\n  control_plane_site: dr\n  sites:\n")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="primary_probe|fence.mode"):
        load_config(path)


def test_dr_references_must_exist(tmp_path: Path):
    path = tmp_path / "cfg.yml"
    path.write_text(_base().replace("target_platform: vc-dr", "target_platform: missing-dr"), encoding="utf-8")
    with pytest.raises(ValueError, match="target platform"):
        load_config(path)


def test_failure_quorum_cannot_exceed_probes():
    raw = {
        "enabled": False,
        "primary_failure_quorum": 2,
        "primary_probes": [{"type": "tcp", "host": "a", "port": 443}],
    }
    with pytest.raises(ValueError, match="cannot exceed"):
        parse_dr(raw)


def test_auto_failover_requires_surviving_control_plane():
    import yaml

    raw = yaml.safe_load(_base())["disaster_recovery"]
    raw["auto_failover"] = True
    raw["fence"] = {"mode": "command", "command_env": "F", "verify_command_env": "V"}
    raw["primary_probes"] = [{"type": "tcp", "host": "core", "port": 22}]
    with pytest.raises(ValueError, match="control_plane_site"):
        parse_dr(raw)
    raw["control_plane_site"] = "main"
    with pytest.raises(ValueError, match="cannot reside only at the primary"):
        parse_dr(raw)
    raw["control_plane_site"] = "dr"
    assert parse_dr(raw).control_plane_site == "dr"
