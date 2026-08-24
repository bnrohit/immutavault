from dataclasses import replace
import pytest

from immutavault.config import PortalConfig, PortalUserConfig, load_config
from immutavault.portal import Portal


def test_portal_refuses_remote_plaintext_listener(monkeypatch, tmp_path):
    base = load_config("config/immutavault.example.yml")
    cfg = replace(
        base,
        runtime=replace(base.runtime, state_db=str(tmp_path / "state.db")),
        portal=PortalConfig(
            enabled=True,
            listen="0.0.0.0",
            port=8787,
            tls_cert=None,
            tls_key=None,
            users=[PortalUserConfig(name="admin", role="admin", token_env="TEST_PORTAL_TOKEN")],
        ),
    )
    monkeypatch.setenv("TEST_PORTAL_TOKEN", "x" * 32)
    with pytest.raises(RuntimeError, match="refuses non-loopback plaintext exposure"):
        Portal(cfg).serve()
