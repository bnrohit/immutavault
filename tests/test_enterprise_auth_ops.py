from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import pytest

from immutavault.enterprise_auth import Identity, OIDCClient, SignedToken
from immutavault.enterprise_config import load_enterprise_config
from immutavault.enterprise_ops import EnterpriseOps, WebSocketTelemetryServer
from immutavault.state import StateDB


EXAMPLE = Path("config/enterprise-v0.9.example.yml")


def _cfg(tmp_path):
    cfg = load_enterprise_config(EXAMPLE)
    core = replace(cfg.core, runtime=replace(cfg.core.runtime, state_db=str(tmp_path / "state.db")))
    return replace(cfg, core=core)


def test_signed_session_roundtrip_and_expiry():
    signer = SignedToken("s" * 48)
    identity = Identity(
        subject="oidc:tenant:user", name="Alice", role="restore_operator",
        tenants=("campus-a",), source="oidc", mfa=True, tenant_id="tenant",
    )
    token = signer.identity_token(identity, minutes=5)
    restored = signer.identity_from_token(token)
    assert restored.subject == identity.subject
    assert restored.tenants == ("campus-a",)
    assert restored.mfa is True

    expired = signer.encode({"typ": "session", "exp": int(time.time()) - 1})
    with pytest.raises(PermissionError, match="expired"):
        signer.decode(expired, expected_type="session")


def test_entra_claim_mapping_requires_mfa(tmp_path):
    cfg = _cfg(tmp_path)
    client = OIDCClient(cfg, SignedToken("x" * 48))
    claims = {
        "tid": "11111111-2222-3333-4444-555555555555",
        "oid": "user-1",
        "name": "Campus A Operator",
        "groups": [
            "10000000-0000-0000-0000-000000000003",
            "20000000-0000-0000-0000-000000000001",
        ],
        "amr": ["pwd"],
    }
    with pytest.raises(PermissionError, match="MFA evidence is required"):
        client.identity_from_claims(claims)

    claims["amr"] = ["pwd", "mfa"]
    identity = client.identity_from_claims(claims)
    assert identity.role == "restore_operator"
    assert identity.tenants == ("campus-a",)
    assert identity.mfa is True


def test_entra_app_roles_are_supported(tmp_path):
    cfg = _cfg(tmp_path)
    cfg = replace(
        cfg,
        oidc=replace(
            cfg.oidc,
            default_tenants=["campus-b"],
        ),
    )
    client = OIDCClient(cfg, SignedToken("y" * 48))
    identity = client.identity_from_claims({
        "tid": "11111111-2222-3333-4444-555555555555",
        "sub": "subject-2",
        "roles": ["Immutavault.Approver"],
        "amr": ["ngcmfa"],
    })
    assert identity.role == "approver"
    assert identity.tenants == ("campus-b",)


def _point(snapshot_id, platform, vm_id, vm_name):
    return {
        "snapshot_id": snapshot_id,
        "created_at": "2026-08-24T20:00:00+00:00",
        "platform": platform,
        "platform_type": "vmware",
        "vm_id": vm_id,
        "vm_name": vm_name,
        "power_state": "poweredOn",
        "source_path": f"/staging/{vm_name}",
        "backup_bytes": 1000,
        "data_added": 100,
        "file_count": 10,
        "manifest_sha256": "a" * 64,
        "immutable_until": "2026-09-24T20:00:00+00:00",
        "suspicious": 0,
        "suspicious_reason": None,
        "verified": 1,
        "verified_at": "2026-08-24T21:00:00+00:00",
        "metadata_json": "{}",
    }


def test_prometheus_metrics_are_tenant_labeled_without_vm_names(tmp_path):
    cfg = _cfg(tmp_path)
    state = StateDB(cfg.runtime.state_db)
    state.upsert_recovery_point(_point("snap-a", "vc-campus-a", "vm-a", "secret-campus-a-db"))
    state.upsert_recovery_point(_point("snap-b", "vc-campus-b", "vm-b", "secret-campus-b-db"))
    state.upsert_recovery_copy(
        snapshot_id="snap-a", target_name="primary", backend="rest",
        repository="rest:https://vault/", status="success", verified=True,
    )
    job = state.start_job("vc-campus-a", "vm-a", "secret-campus-a-db")
    state.finish_job(job, status="success", snapshot_id="snap-a")

    metrics = EnterpriseOps(cfg, state).render_prometheus()
    assert 'tenant="campus-a"' in metrics
    assert 'tenant="campus-b"' in metrics
    assert "immutavault_rpo_compliant" in metrics
    assert "immutavault_audit_chain_valid" in metrics
    assert "secret-campus-a-db" not in metrics
    assert "secret-campus-b-db" not in metrics


def test_live_job_snapshot_is_tenant_scoped(tmp_path):
    cfg = _cfg(tmp_path)
    state = StateDB(cfg.runtime.state_db)
    a = state.start_job("vc-campus-a", "a", "vm-a")
    b = state.start_job("vc-campus-b", "b", "vm-b")
    state.finish_job(b, status="failed", error="test")
    ops = EnterpriseOps(cfg, state)
    rows = ops.jobs(("campus-a",))
    assert [row["vm_name"] for row in rows] == ["vm-a"]
    assert rows[0]["tenant"] == "campus-a"
    assert rows[0]["progress_estimated"] is True
    state.finish_job(a, status="success", snapshot_id="snap-a")


def test_websocket_ticket_cannot_expand_tenant_scope(tmp_path):
    cfg = _cfg(tmp_path)
    signer = SignedToken("z" * 48)
    state = StateDB(cfg.runtime.state_db)
    ops = EnterpriseOps(cfg, state)
    identity = Identity(
        subject="oidc:t:u", name="Alice", role="viewer",
        tenants=("campus-a",), source="oidc", mfa=True,
    )
    ticket = ops.issue_ws_ticket(identity, signer)
    restored = ops.identity_from_ws_ticket(ticket, signer)
    assert restored.tenants == ("campus-a",)
    assert restored.role == "viewer"


def test_websocket_server_frame_supports_large_payloads():
    payload = b"x" * 70000
    frame = WebSocketTelemetryServer._frame(payload)
    assert frame[0] == 0x81
    assert frame[1] == 127
    assert int.from_bytes(frame[2:10], "big") == len(payload)
    assert frame[10:] == payload
