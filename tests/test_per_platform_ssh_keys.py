from immutavault.config import PlatformConfig
from immutavault.adapters.proxmox import ProxmoxAdapter
from immutavault.adapters.xcpng import XCPNGAdapter


def _cfg(ptype: str, env_name: str) -> PlatformConfig:
    return PlatformConfig(name="site", type=ptype, endpoint="host", ssh_user="backupsvc", options={"ssh_key_env": env_name})


def test_proxmox_uses_platform_specific_ssh_key(monkeypatch):
    monkeypatch.setenv("SITE_PVE_KEY", "/keys/pve")
    monkeypatch.setenv("IMMUTAVAULT_SSH_KEY", "/keys/global")
    adapter = ProxmoxAdapter(_cfg("proxmox", "SITE_PVE_KEY"), 60)
    assert adapter._key() == "/keys/pve"
    assert adapter._ssh()[-3:-1] == ["-i", "/keys/pve"]


def test_xcpng_uses_platform_specific_ssh_key(monkeypatch):
    monkeypatch.setenv("SITE_XCP_KEY", "/keys/xcp")
    monkeypatch.setenv("IMMUTAVAULT_SSH_KEY", "/keys/global")
    adapter = XCPNGAdapter(_cfg("xcpng", "SITE_XCP_KEY"), 60)
    assert adapter._key() == "/keys/xcp"
    assert adapter._ssh()[-3:-1] == ["-i", "/keys/xcp"]


def test_legacy_global_key_remains_supported(monkeypatch):
    monkeypatch.delenv("MISSING_PLATFORM_KEY", raising=False)
    monkeypatch.setenv("IMMUTAVAULT_SSH_KEY", "/keys/global")
    cfg = PlatformConfig(name="legacy", type="proxmox", endpoint="host", ssh_user="root", options={})
    assert ProxmoxAdapter(cfg, 60)._key() == "/keys/global"
