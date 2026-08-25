from pathlib import Path

from immutavault.enterprise_config import load_enterprise_config


def test_v09_enterprise_guarantees_are_retained():
    readme = Path("README.md").read_text(encoding="utf-8")
    for token in (
        "Microsoft Entra ID",
        "Prometheus",
        "WebSocket",
        "incremental_strict: true",
        "incremental_fallback: false",
        "application_consistency_strict: true",
    ):
        assert token in readme

    cfg = load_enterprise_config("config/enterprise-v0.9.example.yml")
    assert cfg.oidc.enabled is True
    assert cfg.oidc.require_mfa is True
    assert cfg.oidc.allow_local_tokens is False
    assert cfg.observability.metrics_enabled is True
    assert cfg.observability.websocket_enabled is True

    auth = Path("src/immutavault/enterprise_auth.py").read_text(encoding="utf-8")
    for token in ("RS256", "amr", "acrs", "code_challenge", "openid-configuration"):
        assert token in auth

    portal = Path("src/immutavault/portal_v09.py").read_text(encoding="utf-8")
    for token in ("/auth/callback", "/api/v1/ws-ticket", "metrics_token_env", "cross-tenant restore is prohibited"):
        assert token in portal

    isolation = Path("src/immutavault/enterprise_flr.py").read_text(encoding="utf-8")
    assert "owner-only" in isolation
    assert "admin=False" in isolation

    installer = Path("scripts/install_controller.sh").read_text(encoding="utf-8")
    assert "IMMUTAVAULT_OIDC_SESSION_SECRET" in installer
    assert "IMMUTAVAULT_METRICS_TOKEN" in installer

    alerts = Path("ops/prometheus/immutavault-alerts.yml").read_text(encoding="utf-8")
    assert "ImmutavaultRPOBreach" in alerts
    assert "ImmutavaultAuditChainInvalid" in alerts
