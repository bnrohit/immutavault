from __future__ import annotations

import json
import time
from typing import Any

from . import portal_v11 as base
from .adapters import build_adapter
from .recovery_test import RecoveryTestTarget
from .v2v_engine import CertifiedBackupEngine


class ManagedRecoveryEngine(CertifiedBackupEngine):
    """Certified engine plus fail-closed isolated DR-test execution.

    DR tests intentionally reuse the normal restore request and approval table so
    tenant authorization and four-eyes controls cannot be bypassed by a separate
    execution surface. A ``dr_test`` option is only honored after the recovery
    point, allow-listed target network and live network existence are validated.
    """

    def _dr_test_marker(self, options: dict[str, Any]) -> dict[str, Any] | None:
        marker = options.get("dr_test")
        if marker is None:
            return None
        if not isinstance(marker, dict):
            raise ValueError("dr_test must be an object")
        network = str(marker.get("network") or "").strip()
        if not network:
            raise ValueError("dr_test.network is required")
        marker = dict(marker)
        marker["network"] = network
        return marker

    def _validate_dr_test(self, snapshot_id: str, target_platform: str, options: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        marker = self._dr_test_marker(options)
        if marker is None:
            raise ValueError("restore is not an isolated DR test")
        point = self.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        if not point.get("verified"):
            raise RuntimeError("isolated recovery test requires a verified recovery point")
        if point.get("suspicious"):
            raise RuntimeError("isolated recovery test refuses an anomaly-flagged recovery point")
        target = self._platform(target_platform)
        network = str(marker["network"])
        allowed = tuple(self.cfg.management.dr_test_networks.get(target.name, ()))
        if network not in allowed:
            raise PermissionError(
                f"network {network!r} is not registered as an isolated recovery-test network for {target.name!r}"
            )
        RecoveryTestTarget(self.cfg, target).validate_network(network)
        return target, marker

    def request_restore(
        self, *, snapshot_id: str, requester: str, target_platform: str, target_name: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> int:
        requested = dict(options or {})
        marker = self._dr_test_marker(requested)
        if marker is not None:
            target, marker = self._validate_dr_test(snapshot_id, target_platform, requested)
            requested["dr_test"] = {**marker, "requested_by": requester}
            # Create the target already isolated whenever the native/certified
            # importer exposes a placement option. The post-restore isolation
            # pass still validates and rewrites every NIC before power-on.
            if target.type == "vmware":
                requested["network"] = marker["network"]
            elif target.type == "proxmox":
                requested["network_map"] = {"*": marker["network"]}
                requested["default_bridge"] = marker["network"]
        request_id = super().request_restore(
            snapshot_id=snapshot_id,
            requester=requester,
            target_platform=target_platform,
            target_name=target_name,
            options=requested,
        )
        if marker is not None:
            self.state.audit(requester, "dr.test.request", "restore_request", str(request_id), {
                "snapshot_id": snapshot_id,
                "target_platform": target_platform,
                "isolated_network": marker["network"],
                "source_repository": requested.get("source_repository", "primary"),
            })
        return request_id

    def execute_restore(self, request_id: int, *, actor: str, dry_run: bool = False) -> dict[str, Any]:
        req = self.state.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        try:
            options = json.loads(req.get("options_json") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("restore request contains invalid options JSON") from exc
        marker = self._dr_test_marker(options)
        if marker is None:
            return super().execute_restore(request_id, actor=actor, dry_run=dry_run)

        target, marker = self._validate_dr_test(str(req["snapshot_id"]), str(req["target_platform"]), options)
        network = str(marker["network"])
        helper = RecoveryTestTarget(self.cfg, target)
        if dry_run:
            planned = super().execute_restore(request_id, actor=actor, dry_run=True)
            return {
                **planned,
                "mode": "isolated-dr-test",
                "isolated_network": network,
                "boot_validation_seconds": self.cfg.management.dr_test_boot_seconds,
                "auto_cleanup": self.cfg.management.dr_test_auto_cleanup,
            }

        restored: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        primary_error: Exception | None = None
        cleanup_errors: list[str] = []
        adapter = build_adapter(target, self.cfg.runtime.command_timeout_seconds)
        try:
            restored = super().execute_restore(request_id, actor=actor, dry_run=False)
            isolation = helper.isolate(restored, network)
            self.state.audit(actor, "dr.test.isolated", "restore_request", str(request_id), isolation)
            adapter.power_on(restored)
            time.sleep(self.cfg.management.dr_test_boot_seconds)
            boot = helper.running(restored)
            if not boot.get("running"):
                raise RuntimeError("isolated recovery-test VM did not remain powered on during boot validation")
            result = {
                "status": "success",
                "mode": "isolated-dr-test",
                "request_id": request_id,
                "target_platform": target.name,
                "isolated_network": network,
                "boot_validation": boot,
                "restored": restored,
            }
            self.state.audit(actor, "dr.test.boot_verified", "restore_request", str(request_id), {
                "target_platform": target.name,
                "isolated_network": network,
                "boot_validation": boot,
            })
        except Exception as exc:
            primary_error = exc
        finally:
            if restored is not None:
                try:
                    adapter.power_off(restored)
                except Exception as exc:
                    cleanup_errors.append("power-off: " + str(exc))
                if self.cfg.management.dr_test_auto_cleanup:
                    try:
                        helper.destroy(restored)
                    except Exception as exc:
                        cleanup_errors.append("destroy: " + str(exc))

        if primary_error is not None or cleanup_errors:
            pieces = ([str(primary_error)] if primary_error is not None else []) + cleanup_errors
            message = "; ".join(pieces)
            # super().execute_restore may already have marked the import itself
            # successful. An isolated test is only successful if boot validation
            # and required cleanup both succeed, so correct the request outcome.
            self.state.finish_restore(request_id, success=False, error=message)
            self.state.audit(actor, "dr.test.failed", "restore_request", str(request_id), {
                "error": message,
                "isolated_network": network,
                "cleanup_errors": cleanup_errors,
            })
            if cleanup_errors:
                raise RuntimeError("isolated recovery test cleanup failed: " + message) from primary_error
            assert primary_error is not None
            raise primary_error

        assert result is not None
        self.state.audit(actor, "dr.test.cleanup", "restore_request", str(request_id), {
            "auto_cleanup": self.cfg.management.dr_test_auto_cleanup,
            "destroyed": self.cfg.management.dr_test_auto_cleanup,
            "isolated_network": network,
        })
        return result


def _enhance_ui(ui: str) -> str:
    # Replica target checkboxes are populated from the authenticated storage
    # target endpoint. The primary repository is implicit and always receives the
    # backup; only optional replicas are displayed here.
    ui = ui.replace(
        '<div id="policyVMs" class="card muted">Choose a platform and discover.</div>',
        '<div id="policyReplicas" class="card muted">Replica targets: primary repository only.</div><div id="policyVMs" class="card muted">Choose a platform and discover.</div>',
    )
    ui = ui.replace(
        '<label>Mounted path<input id="mgSpath"></label>',
        '<label>Mounted / target path<input id="mgSpath" placeholder="/srv/immutavault/storage/nas-dr"></label><label>Direct NAS type<select id="mgSmt"><option value="">Existing mounted path</option><option value="nfs">NFS</option><option value="cifs">SMB 3.1.1</option></select></label><label>NFS/SMB source<input id="mgSms" placeholder="server:/export or //server/share"></label><label>SMB username<input id="mgSmu"></label><label>SMB password<input id="mgSmp" type="password"></label>',
    )
    ui = ui.replace(
        "let role='',tenants=[],platforms=[],flrSession=null",
        "let role='',tenants=[],platforms=[],storageTargets=[],flrSession=null",
    )
    ui = ui.replace(
        "platforms=await api('/api/v1/platforms');const o=await api('/api/v1/ops/snapshot');",
        "platforms=await api('/api/v1/platforms');storageTargets=await api('/api/v1/storage-targets');const o=await api('/api/v1/ops/snapshot');",
    )
    old_fill = "function fillPlatformSelectors(){const opts=platforms.map(p=>`<option value=\"${esc(p.name)}\">${esc(p.name)} (${esc(p.type)})</option>`).join('');$('polPlatform').innerHTML=opts;$('mgPlatform').innerHTML=opts;$('drNetPlatform').innerHTML=opts}"
    new_fill = old_fill[:-1] + ";renderReplicaTargets()}function renderReplicaTargets(){const rows=storageTargets.filter(x=>x.name!=='primary'&&x.enabled);$('policyReplicas').innerHTML=rows.length?'<b>Optional replica targets</b><br>'+rows.map(x=>`<label class=\"vm\"><input class=\"pri\" type=\"checkbox\" value=\"${esc(x.name)}\"> ${esc(x.name)} (${esc(x.backend||x.provider||'replica')})</label>`).join(''):'Replica targets: primary repository only.'}"
    ui = ui.replace(old_fill, new_fill)
    ui = ui.replace(
        "replica_targets:[]",
        "replica_targets:[...document.querySelectorAll('.pri:checked')].map(x=>x.value)",
    )
    old_storage = "function storageBody(){return{name:$('mgSn').value,backend:$('mgSb').value,provider:$('mgSp').value,endpoint:$('mgSe').value,url:$('mgSe').value,region:$('mgSr').value,bucket:$('mgSbu').value,prefix:$('mgSpre').value,path:$('mgSpath').value,access_key:$('mgSak').value,secret_key:$('mgSsk').value,password:$('mgSrp').value,immutable:$('mgSil').checked,lock_days:Number($('mgSid').value||30)}}"
    new_storage = "function storageBody(){return{name:$('mgSn').value,backend:$('mgSb').value,provider:$('mgSp').value,endpoint:$('mgSe').value,url:$('mgSe').value,region:$('mgSr').value,bucket:$('mgSbu').value,prefix:$('mgSpre').value,path:$('mgSpath').value,mount_type:$('mgSmt').value,mount_source:$('mgSms').value,mount_username:$('mgSmu').value,mount_password:$('mgSmp').value,access_key:$('mgSak').value,secret_key:$('mgSsk').value,password:$('mgSrp').value,immutable:$('mgSil').checked,lock_days:Number($('mgSid').value||30)}}"
    ui = ui.replace(old_storage, new_storage)

    # Put DR Test next to the normal full-VM action. The request still lands in
    # the normal restore approval table; ManagedRecoveryEngine recognizes the
    # marker only after server-side allow-list/network validation.
    ui = ui.replace(
        ">Full VM</button>`}",
        ">Full VM</button> <button class=\"good\" onclick='requestDrTest(${JSON.stringify(x.snapshot_id)},${JSON.stringify(x.vm_name)},${JSON.stringify(x.available_restore_sources||['primary'])})'>Run DR Test</button>`}",
    )
    dr_function = """async function requestDrTest(snapshot,name,sources){const target=prompt('DR-test target platform:',platforms[0]?.name||'');if(!target)return;const source=prompt('Backup copy:',(sources||['primary'])[0]);if(!source)return;const network=prompt('Allow-listed isolated recovery network:','');if(!network)return;const newName=prompt('Disposable DR-test VM name:',name+'-drtest');if(!newName)return;try{const r=await api('/api/v1/restores',{method:'POST',body:JSON.stringify({snapshot_id:snapshot,target_platform:target,target_name:newName,options:{source_repository:source,dr_test:{network}}})});alert('DR test request #'+r.request_id+' created: '+r.status+'. Normal four-eyes approval still applies.');await loadRestores()}catch(e){alert(e.message)}}
"""
    ui = ui.replace("async function requestRestore(", dr_function + "async function requestRestore(")
    return ui


# portal_v11 resolves these module globals when a Portal instance is created.
# Patch only the v1.1 extension points; the existing OIDC/tenant/FLR/WebSocket
# handler remains the established implementation.
base.CertifiedBackupEngine = ManagedRecoveryEngine
base.UI = _enhance_ui(base.UI)

Portal = base.EnterprisePortal
EnterprisePortal = base.EnterprisePortal
UI = base.UI
