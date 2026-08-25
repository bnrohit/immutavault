from pathlib import Path

from immutavault.v2v_config import load_v10_config


def test_v10_release_contract_is_present():
    assert Path("VERSION").read_text(encoding="utf-8").strip() == "1.0.0"
    readme = Path("README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Immutavault v1.0.0\n")
    for token in (
        "immutavault-vmware-proxmox-v1",
        "virt-v2v",
        "VirtIO",
        "Secure Boot",
        "vTPM",
        "powered off",
        "certified provider",
    ):
        assert token in readme

    cfg = load_v10_config("config/enterprise-v1.0.example.yml")
    assert cfg.v2v.enabled is False
    assert cfg.v2v.builtin_vmware_to_proxmox is True
    assert cfg.v2v.require_verified_point is True
    assert cfg.v2v.allow_suspicious_points is False
    assert cfg.v2v.allow_secure_boot is False
    assert cfg.v2v.virt_v2v_min_version == "2.12.0"

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'immutavault = "immutavault.cli_v10:main"' in pyproject

    code = Path("src/immutavault/v2v.py").read_text(encoding="utf-8")
    for token in ("input:ova", "output:local", "source_read_only", "target_new_vm", "rollback_available"):
        assert token in code

    cert = Path("src/immutavault/v2v_cert.py").read_text(encoding="utf-8")
    assert "OVF_EXPORT_TRANSPORTS" in cert
    assert "NATIVE_INCREMENTAL_TRANSPORTS" in cert

    runbook = Path("docs/CERTIFIED_V2V.md").read_text(encoding="utf-8")
    assert "VMware/vCenter -> Proxmox VE" in runbook
    assert "VMware/Proxmox -> XCP-ng" in runbook
    assert "isolated recovery network" in runbook

    gate = Path("scripts/check_v2v.sh").read_text(encoding="utf-8")
    assert "2.12.0" in gate
    assert "--machine-readable" in gate
