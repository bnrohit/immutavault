from immutavault.config import PlatformConfig
from immutavault.adapters.proxmox import ProxmoxAdapter


def test_include_exclude():
    cfg = PlatformConfig(
        name="pve", type="proxmox", endpoint="example", include=["prod-*"], exclude=["*-temp"]
    )
    adapter = ProxmoxAdapter(cfg, 1)
    assert adapter._selected("prod-db")
    assert not adapter._selected("prod-temp")
    assert not adapter._selected("dev-db")

from immutavault.util import safe_component


def test_safe_component_blocks_path_traversal():
    assert safe_component("../../prod vm") == "prod_vm"
