from immutavault import setup_v07


def test_v07_setup_defaults_to_incremental_with_safe_fallback(tmp_path):
    manager = object.__new__(setup_v07.SetupManager)
    platform, env = manager._platform({
        "name": "vc-main", "type": "vmware", "endpoint": "https://vc/sdk",
        "username": "svc", "password": "secret", "backup_transport": "auto",
        "vddk_libdir": "/opt/vddk", "vddk_thumbprint": "AA:BB",
        "vddk_transports": "hotadd:nbdssl", "cbt_auto_enable": True,
        "incremental_fallback": True,
    })
    assert platform["mode"] == "auto"
    assert platform["options"]["cbt_auto_enable"] is True
    assert platform["options"]["incremental_fallback"] is True
    assert platform["options"]["vddk_libdir"] == "/opt/vddk"
    assert platform["options"]["vddk_thumbprint"] == "AA:BB"
    assert "secret" not in str(platform)
    assert env["IMMUTAVAULT_VC_MAIN_PASSWORD"] == "secret"


def test_v07_ui_exposes_incremental_controls():
    assert "VMware backup transport" in setup_v07.UI
    assert "Automatic incremental + safe full fallback" in setup_v07.UI
    assert "Enable CBT automatically" in setup_v07.UI
    assert "Automatic full fallback" in setup_v07.UI
