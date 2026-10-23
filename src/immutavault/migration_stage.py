"""Explicitly authorized, powered-off XCP-ng migration staging.

No production cutover, source shutdown, destination power-on, MAC/IP takeover,
or cleanup of old production VMs is implemented. Failures leave staging VM
halted for operator inspection. This is not a warm/live migration.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any

from .adapters import build_adapter
from .runner import run


_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class StagingBlocked(RuntimeError):
    pass


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise StagingBlocked(field + " must be a canonical UUID")
    return value


class XCPNGMigrationStager:
    """Use the existing approved restic restore and remap exact powered-off VIFs."""

    def __init__(self, engine: Any, plan: dict[str, Any]) -> None:
        self.engine = engine
        self.plan = plan
        if plan.get("source_family") != "xcpng" or plan.get("destination_family") != "xcpng":
            raise StagingBlocked("this executor is XCP-ng -> XCP-ng only")
        target = engine._platform(plan["destination_platform"])
        if target.type != "xcpng" or not target.enabled:
            raise StagingBlocked("destination platform must be an enabled XCP-ng pool")
        self.adapter = build_adapter(target, engine.cfg.runtime.command_timeout_seconds)
        self.timeout = engine.cfg.runtime.command_timeout_seconds

    def _read(self, command: str) -> str:
        if not command.startswith("xe "):
            raise StagingBlocked("unsupported XCP-ng read")
        if not (command.split()[1].endswith("-list") or command.split()[1].endswith("-param-get")):
            raise StagingBlocked("unsupported XCP-ng read command")
        return run(self.adapter._ssh() + [command], timeout=min(120, self.timeout)).stdout.strip()

    def _list(self, command: str) -> list[str]:
        return [v.strip() for v in self._read(command).replace("\n", ",").split(",") if v.strip()]

    def _param(self, kind: str, uid: str, name: str) -> str:
        return self._read(f"xe {kind}-param-get uuid={shlex.quote(_uuid(uid, 'uuid'))} "
                          f"param-name={shlex.quote(name)}")

    def _row_for_request(self, request_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        req = self.engine.state.get_restore_request(request_id)
        if not req:
            raise StagingBlocked("unknown restore request")
        if req.get("status") != "approved":
            raise StagingBlocked("migration staging requires separately approved restore request")
        if not self.engine.cfg.protection.require_four_eyes_restore:
            raise StagingBlocked("four-eyes approval must be enforced")
        options = json.loads(req.get("options_json") or "{}")
        if options.get("migration_plan_sha256") != self.plan.get("plan_sha256"):
            raise StagingBlocked("restore request is not tied to this exact immutable plan")
        rows = [r for r in self.plan.get("vms", []) if
                r.get("snapshot_id") == req.get("snapshot_id") and
                r.get("target_name") == req.get("target_name")]
        if len(rows) != 1:
            raise StagingBlocked("restore request does not uniquely match planned workload")
        row = rows[0]
        if not row.get("ready_for_operator_review") or not self.plan.get("ready_for_operator_review"):
            raise StagingBlocked("migration plan is blocked")
        if req.get("target_platform") != self.plan["destination_platform"]:
            raise StagingBlocked("restore request target platform mismatch")
        target_srs = {d["target_sr"] for d in row["disks"]}
        if len(target_srs) != 1 or options.get("sr_uuid") != next(iter(target_srs)):
            raise StagingBlocked("approved restore request SR does not match approved migration plan")
        return req, row

    def _validate_live_destination(self, row: dict[str, Any]) -> None:
        pool = self._list("xe pool-list params=uuid --minimal")
        if pool != [self.plan["destination_cluster"]]:
            raise StagingBlocked("connected XCP-ng pool identity differs from approved migration plan")
        sr = _uuid(row["disks"][0]["target_sr"], "destination SR")
        if self._param("sr", sr, "shared").lower() != "true":
            raise StagingBlocked("destination SR is no longer shared")
        sr_type = self._param("sr", sr, "type")
        if sr_type.lower() in {"iso", "udev", ""}:
            raise StagingBlocked("target SR is not an eligible VM disk repository")
        size = self._param("sr", sr, "physical-size")
        used = self._param("sr", sr, "physical-utilisation")
        needed = sum(int(d["size_bytes"]) for d in row["disks"])
        if not size.isdigit() or not used.isdigit() or int(size) - int(used) < (needed * 120 // 100):
            raise StagingBlocked("live shared SR free space is below planned disk size plus reserve")
        hosts = self._list("xe host-list params=uuid --minimal")
        if _uuid(row["destination_host"], "destination host") not in hosts:
            raise StagingBlocked("planned destination host is missing")
        attached_hosts = set()
        for pbd in self._list(f"xe pbd-list sr-uuid={shlex.quote(sr)} params=uuid --minimal"):
            host = self._param("pbd", _uuid(pbd, "PBD"), "host-uuid")
            attached = self._param("pbd", pbd, "currently-attached").lower() == "true"
            if attached:
                attached_hosts.add(host)
        if not set(hosts).issubset(attached_hosts):
            raise StagingBlocked("destination SR is not attached on every pool host")
        for nic in row["nics"]:
            network = _uuid(nic["target_network"], "destination network")
            expected_vlan = nic.get("vlan")
            if type(expected_vlan) is not int or not 0 <= expected_vlan <= 4094:
                raise StagingBlocked("NIC has no verified VLAN")
            if self._list(f"xe network-list uuid={shlex.quote(network)} params=uuid --minimal") != [network]:
                raise StagingBlocked("destination network is missing: " + network)
            seen_hosts = set()
            for pif in self._list(f"xe pif-list network-uuid={shlex.quote(network)} params=uuid --minimal"):
                host = self._param("pif", _uuid(pif, "PIF"), "host-uuid")
                attached = self._param("pif", pif, "currently-attached").lower() == "true"
                tag = self._param("pif", pif, "VLAN")
                vlan = 0 if tag == "-1" else int(tag) if tag.isdigit() else -1
                if attached and vlan == expected_vlan:
                    seen_hosts.add(host)
            if not set(hosts).issubset(seen_hosts):
                raise StagingBlocked("destination VLAN lacks verified PIF reachability on all pool hosts")
        # No destructive cleanup or automatic VM overwrite, even with same name.
        name = shlex.quote(row["target_name"])
        existing = self._list(f"xe vm-list name-label={name} params=uuid --minimal")
        if existing:
            raise StagingBlocked("destination VM name already exists")

    def _move_vifs(self, new_vm_uuid: str, row: dict[str, Any]) -> list[dict[str, str]]:
        if self._param("vm", new_vm_uuid, "power-state").lower() != "halted":
            raise StagingBlocked("staged target VM must be halted before NIC remapping")
        vifs = self._list(f"xe vif-list vm-uuid={shlex.quote(new_vm_uuid)} params=uuid --minimal")
        expected = {}
        for nic in row["nics"]:
            device = str(nic.get("device") or "")
            if not device.isdigit() or device in expected:
                raise StagingBlocked("every planned NIC requires a unique numeric device index")
            expected[device] = _uuid(nic.get("target_network"), "target network")
        if len(vifs) != len(expected) or not expected:
            raise StagingBlocked("restored NIC count differs from approved manifest")
        observed: dict[str, str] = {}
        for vif in vifs:
            device = self._param("vif", _uuid(vif, "vif"), "device")
            if device not in expected or device in observed:
                raise StagingBlocked("restored VIF devices differ from approved manifest")
            observed[device] = vif
        if set(observed) != set(expected):
            raise StagingBlocked("restored NIC device list differs from approved manifest")
        changes = []
        for device in sorted(observed, key=int):
            vif = observed[device]
            network = expected[device]
            # vif-move changes the powered-off target only, never the source.
            run(self.adapter._ssh() + [
                f"xe vif-move uuid={shlex.quote(vif)} network-uuid={shlex.quote(network)}"
            ], timeout=min(self.timeout, 120))
            actual = self._param("vif", vif, "network-uuid")
            if actual != network:
                raise StagingBlocked("network remapping was not confirmed on target VIF")
            changes.append({"device": device, "vif": vif, "network": network})
        if self._param("vm", new_vm_uuid, "power-state").lower() != "halted":
            raise StagingBlocked("target VM unexpectedly became powered on")
        return changes

    def stage(self, request_id: int, *, actor: str, execute: bool = False) -> dict[str, Any]:
        if not actor or not actor.strip():
            raise StagingBlocked("authenticated named actor is required")
        req, row = self._row_for_request(request_id)
        self._validate_live_destination(row)
        if not execute:
            return {
                "dry_run": True, "request_id": request_id,
                "target_name": row["target_name"],
                "target_sr": row["disks"][0]["target_sr"],
                "exact_nic_devices": row["nics"],
                "no_changes": True,
                "next": "explicit execution imports a NEW powered-off VM from verified backup and maps its NICs",
            }
        restored = None
        try:
            restored = self.engine.execute_restore(request_id, actor=actor, dry_run=False)
            new_uuid = _uuid(restored.get("uuid"), "restored target VM UUID")
            changes = self._move_vifs(new_uuid, row)
            self.engine.state.audit(actor, "migration.staged.powered_off", "restore_request", str(request_id),
                                    {"plan_sha256": self.plan["plan_sha256"],
                                     "target_uuid": new_uuid, "vifs": changes,
                                     "source_unchanged": True})
            return {
                "status": "STAGED_POWERED_OFF", "request_id": request_id,
                "destination_vm_uuid": new_uuid, "mapped_vifs": changes,
                "source_unchanged": True,
                "next": "Manual application-consistent FINAL SYNC, source fencing and approval needed before target boot.",
            }
        except Exception as exc:
            self.engine.state.audit(actor, "migration.stage_attention", "restore_request", str(request_id),
                                    {"error": str(exc),
                                     "restored_vm": restored,
                                     "warning": "Inspect imported target; never boot before mappings are reconciled"})
            raise
