from __future__ import annotations

from immutavault.config import PlatformConfig
from .base import Adapter
from .vmware_incremental import VMwareIncrementalAdapter
from .proxmox import ProxmoxAdapter
from .xcpng import XCPNGAdapter


def build_adapter(cfg: PlatformConfig, timeout: int) -> Adapter:
    if cfg.type == "vmware":
        return VMwareIncrementalAdapter(cfg, timeout)
    if cfg.type == "proxmox":
        return ProxmoxAdapter(cfg, timeout)
    if cfg.type == "xcpng":
        return XCPNGAdapter(cfg, timeout)
    raise ValueError(f"unsupported platform: {cfg.type}")
