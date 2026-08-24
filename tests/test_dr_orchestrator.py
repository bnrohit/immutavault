from pathlib import Path
from types import SimpleNamespace
import pytest

from immutavault.dr import DROrchestrator
from immutavault.dr_config import parse_dr
from immutavault.state import StateDB


def _dr(tmp_path: Path, *, auto=False, quorum=0, interval=60):
    raw = {
        "enabled": True,
        "primary_site": "main", "dr_site": "dr", "replica": "r",
        "auto_failover": auto, "primary_failure_quorum": quorum, "check_interval_seconds": interval,
        "control_plane_site": "dr" if auto else "",
        "fence": {"mode": "command", "command_env": "FENCE", "verify_command_env": "VERIFY"} if auto else {"mode": "manual"},
        "primary_probes": [
            {"type": "tcp", "host": "a", "port": 443},
            {"type": "tcp", "host": "b", "port": 443},
            {"type": "tcp", "host": "c", "port": 443},
        ],
        "maintenance_file": str(tmp_path / "maintenance"),
        "sites": [
            {"name": "main", "gateway": {"host": "main", "vtep_ip": "10.0.0.1", "router_id": "10.0.0.1"}},
            {"name": "dr", "gateway": {"host": "dr", "vtep_ip": "10.0.0.2", "router_id": "10.0.0.2"}},
        ],
        "networks": [{"vlan_id": 48, "vni": 10048, "subnet": "10.14.48.0/24", "gateway_cidr": "10.14.48.1/24"}],
        "workloads": [{"name": "vm", "source_platform": "p1", "target_platform": "p2"}],
    }
    return parse_dr(raw)


def _engine(tmp_path: Path, dr):
    return SimpleNamespace(
        cfg=SimpleNamespace(dr=dr, runtime=SimpleNamespace(command_timeout_seconds=300)),
        state=StateDB(str(tmp_path / "state.db")),
    )


def test_watch_uses_failure_quorum(monkeypatch, tmp_path):
    dr = _dr(tmp_path, auto=False, quorum=2)
    orch = DROrchestrator(_engine(tmp_path, dr))
    monkeypatch.setattr("immutavault.dr.run_health_checks", lambda checks: {
        "ok": False,
        "checks": [{"ok": False}, {"ok": True}, {"ok": True}],
    })
    result = orch.watch()
    assert result["probes"]["failed_probes"] == 1
    assert result["health"]["healthy"] is True
    monkeypatch.setattr("immutavault.dr.run_health_checks", lambda checks: {
        "ok": False,
        "checks": [{"ok": False}, {"ok": False}, {"ok": True}],
    })
    result = orch.watch()
    assert result["health"]["healthy"] is False
    assert result["health"]["consecutive_failures"] == 1


def test_maintenance_suppresses_watch(monkeypatch, tmp_path):
    dr = _dr(tmp_path)
    engine = _engine(tmp_path, dr)
    orch = DROrchestrator(engine)
    orch.maintenance("on", actor="test")
    monkeypatch.setattr("immutavault.dr.run_health_checks", lambda checks: (_ for _ in ()).throw(AssertionError("should not probe")))
    result = orch.watch()
    assert result["action"] == "suppressed-maintenance"
    orch.maintenance("off", actor="test")
    assert not Path(dr.maintenance_file).exists()


def test_promotion_preflight_happens_before_fencing(monkeypatch, tmp_path):
    dr = _dr(tmp_path)
    orch = DROrchestrator(_engine(tmp_path, dr))
    monkeypatch.setattr(orch, "plan", lambda: {"ready": True, "errors": [], "workloads": []})
    monkeypatch.setattr(orch, "_already_failed_over", lambda: False)
    monkeypatch.setattr(orch, "preflight", lambda plan=None: {"ok": False, "problems": ["DR target down"]})
    fenced = []
    monkeypatch.setattr(orch, "_fence_primary", lambda **kwargs: fenced.append(True))
    with pytest.raises(RuntimeError, match="before fencing primary"):
        orch.promote(execute=True, confirm_primary_fenced=True)
    assert fenced == []


def test_watch_respects_configured_interval(monkeypatch, tmp_path):
    dr = _dr(tmp_path, interval=300)
    engine = _engine(tmp_path, dr)
    orch = DROrchestrator(engine)
    monkeypatch.setattr("immutavault.dr.run_health_checks", lambda checks: {
        "ok": True,
        "checks": [{"ok": True}, {"ok": True}, {"ok": True}],
    })
    first = orch.watch()
    assert first["health"]["healthy"] is True

    monkeypatch.setattr(
        "immutavault.dr.run_health_checks",
        lambda checks: (_ for _ in ()).throw(AssertionError("interval should suppress probe")),
    )
    second = orch.watch()
    assert second["action"] == "interval-not-due"
    assert 0 < second["next_check_in_seconds"] <= 300



def test_command_fence_requires_and_runs_independent_verification(monkeypatch, tmp_path):
    dr = _dr(tmp_path, auto=True, quorum=2)
    engine = _engine(tmp_path, dr)
    orch = DROrchestrator(engine)
    monkeypatch.setenv("FENCE", "fence-tool isolate primary")
    monkeypatch.setenv("VERIFY", "fence-tool verify primary")
    calls = []
    from immutavault.runner import CommandResult

    def fake_run(command, **kwargs):
        calls.append(command)
        return CommandResult(command, 0, "ok", "")

    monkeypatch.setattr("immutavault.dr.run", fake_run)
    orch._fence_primary(manual_confirmed=False, automatic=True)
    assert calls == [
        ["fence-tool", "isolate", "primary"],
        ["fence-tool", "verify", "primary"],
    ]


def test_successful_promotion_orders_network_restore_route_and_power(monkeypatch, tmp_path):
    dr = _dr(tmp_path)
    engine = _engine(tmp_path, dr)
    engine._platform = lambda name: SimpleNamespace(name=name, type="vmware")
    engine.execute_restore = lambda rid, actor: {"platform": "p2", "name": "vm-dr"}
    engine.state.upsert_recovery_point({
        "snapshot_id": "snap1",
        "created_at": "2099-01-01T00:00:00+00:00",
        "platform": "p1",
        "platform_type": "vmware",
        "vm_id": "vm-1",
        "vm_name": "vm",
        "power_state": "poweredOn",
        "source_path": "/staging/vm",
        "backup_bytes": 1,
        "data_added": 1,
        "file_count": 1,
        "manifest_sha256": "abc",
        "immutable_until": "2099-02-01T00:00:00+00:00",
        "suspicious": 0,
        "suspicious_reason": None,
        "verified": 1,
        "verified_at": "2099-01-01T00:00:00+00:00",
        "metadata_json": "{}",
    })
    orch = DROrchestrator(engine)
    monkeypatch.setattr(orch, "plan", lambda: {
        "ready": True,
        "errors": [],
        "workloads": [{"snapshot_id": "snap1"}],
    })
    monkeypatch.setattr(orch, "preflight", lambda plan=None: {"ok": True, "problems": []})
    monkeypatch.setattr(orch, "_already_failed_over", lambda: False)
    events = []
    monkeypatch.setattr(orch, "_fence_primary", lambda **kwargs: events.append("fence"))
    monkeypatch.setattr(orch.net, "prepare", lambda site: events.append(f"prepare:{site}") or {"ok": True})
    monkeypatch.setattr(orch.net, "deactivate", lambda site, **kwargs: events.append(f"deactivate:{site}") or {"ok": True})
    monkeypatch.setattr(orch.net, "activate", lambda site, **kwargs: events.append(f"activate:{site}") or {"ok": True})

    class FakeAdapter:
        def power_on(self, result):
            events.append("power:on")
        def power_off(self, result):
            events.append("power:off")

    monkeypatch.setattr("immutavault.dr.build_adapter", lambda *args, **kwargs: FakeAdapter())
    monkeypatch.setattr(orch, "_wait_workload_health", lambda workload: {"ok": True, "checks": []})
    result = orch.promote(execute=True, confirm_primary_fenced=True)
    assert result["status"] == "success"
    assert events[:4] == ["fence", "prepare:dr", "deactivate:main", "activate:dr"]
    assert events[-1] == "power:on"
    latest = engine.state.latest_dr_run(direction="failover", status="success")
    assert latest is not None
