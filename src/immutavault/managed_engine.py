from __future__ import annotations

import json
import time
from typing import Any

from .adapters import build_adapter
from .recovery_test import RecoveryTestTarget
from .v2v_engine import CertifiedBackupEngine


class ManagedBackupEngine(CertifiedBackupEngine):
    """Certified engine plus fail-closed isolated recovery testing."""

    def _dr_test_marker(self, options: dict[str, Any]) -> dict[str, Any] | None:
        marker = options.get("dr_test")
        return marker if isinstance(marker, dict) and marker.get("network") else None

    def _validate_dr_test(self, point: dict[str, Any], target_platform: str, options: dict[str, Any]) -> dict[str, Any] | None:
        marker = self._dr_test_marker(options)
        if not marker:
            return None
        if not point.get("verified"):
            raise RuntimeError("isolated recovery test requires a verified recovery point")
        if point.get("suspicious"):
            raise RuntimeError("isolated recovery test refuses an anomaly-flagged recovery point")
        target = self._platform(target_platform)
        network = str(marker.get("network") or "").strip()
        allowed = tuple(self.cfg.management.dr_test_networks.get(target.name, ()))
        if network not in allowed:
            raise PermissionError(
                f"network {network!r} is not registered as an isolated recovery-test network for {target.name!r}"
            )
        check = RecoveryTestTarget(self.cfg, target).validate_network(network)
        if target.type == "vmware":
            # VMware can import directly onto the isolated port group; the
            # post-restore pass still remaps every NIC before power-on.
            options["network"] = network
        return check

    def request_restore(
        self, *, snapshot_id: str, requester: str, target_platform: str,
        target_name: str | None = None, options: dict[str, Any] | None = None,
    ) -> int:
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        requested = dict(options or {})
        check = self._validate_dr_test(point, target_platform, requested)
        request_id = super().request_restore(
            snapshot_id=snapshot_id,
            requester=requester,
            target_platform=target_platform,
            target_name=target_name,
            options=requested,
        )
        if check:
            self.state.audit(requester, "dr.test.request", "restore_request", str(request_id), {
                "snapshot_id": snapshot_id,
                "target_platform": target_platform,
                "isolated_network": check["network"],
            })
        return request_id

    def execute_restore(self, request_id: int, *, actor: str, dry_run: bool = False) -> dict[str, Any]:
        req = self.state.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        try:
            options = json.loads(req.get("options_json") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("restore request options are invalid") from exc
        marker = self._dr_test_marker(options)
        if not marker:
            return super().execute_restore(request_id, actor=actor, dry_run=dry_run)
        point = self.state.get_point(str(req["snapshot_id"]))
        if not point:
            raise RuntimeError("recovery point metadata missing")
        target = self._platform(str(req["target_platform"]))
        check = self._validate_dr_test(point, target.name, options)
        if dry_run:
            plan = super().execute_restore(request_id, actor=actor, dry_run=True)
            plan["dr_test"] = {
                "isolated_network": check["network"] if check else marker["network"],
                "boot_seconds": self.cfg.management.dr_test_boot_seconds,
                "auto_cleanup": self.cfg.management.dr_test_auto_cleanup,
            }
            return plan

        helper = RecoveryTestTarget(self.cfg, target)
        adapter = build_adapter(target, self.cfg.runtime.command_timeout_seconds)
        restored: dict[str, Any] | None = None
        powered_on = False
        boot: dict[str, Any] | None = None
        isolation: dict[str, Any] | None = None
        cleanup: dict[str, Any] = {"attempted": False, "destroyed": False, "retained_powered_off": False}
        failure: Exception | None = None
        try:
            # Standard engine execution enforces approval state, immutable-source
            # verification, same-family/V2V policy, and creates a NEW VM powered off.
            restored = super().execute_restore(request_id, actor=actor, dry_run=False)
            isolation = helper.isolate(restored, str(marker["network"]))
            self.state.audit(actor, "dr.test.isolated", "restore_request", str(request_id), isolation)
            adapter.power_on(restored)
            powered_on = True
            time.sleep(self.cfg.management.dr_test_boot_seconds)
            boot = helper.running(restored)
            if not boot.get("running"):
                raise RuntimeError("isolated recovery-test VM did not remain powered on during boot validation")
        except Exception as exc:
            failure = exc
        finally:
            if restored is not None:
                try:
                    adapter.power_off(restored)
                    powered_on = False
                except Exception as exc:
                    if failure is None:
                        failure = RuntimeError(f"isolated recovery test could not power off disposable VM: {exc}")
                    self.state.audit(actor, "dr.test.poweroff_failed", "restore_request", str(request_id), {"error": str(exc)})
                if self.cfg.management.dr_test_auto_cleanup:
                    cleanup["attempted"] = True
                    try:
                        helper.destroy(restored)
                        cleanup["destroyed"] = True
                    except Exception as exc:
                        cleanup["error"] = str(exc)
                        if failure is None:
                            failure = RuntimeError(f"isolated recovery test cleanup failed: {exc}")
                else:
                    cleanup["retained_powered_off"] = not powered_on

        detail = {
            "request_id": request_id,
            "isolated_network": str(marker["network"]),
            "target_platform": target.name,
            "isolation": isolation,
            "boot_validation": boot,
            "cleanup": cleanup,
            "restored": restored,
        }
        if failure is not None:
            self.state.finish_restore(request_id, success=False, result=detail, error=str(failure))
            self.state.audit(actor, "dr.test.failed", "restore_request", str(request_id), {
                "error": str(failure), "isolated_network": str(marker["network"]), "cleanup": cleanup,
            })
            raise failure

        detail["status"] = "success"
        self.state.finish_restore(request_id, success=True, result=detail)
        self.state.audit(actor, "dr.test.complete", "restore_request", str(request_id), {
            "isolated_network": str(marker["network"]), "boot_validation": boot, "cleanup": cleanup,
        })
        return detail
