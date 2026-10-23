"""Safety-first, offline-verifiable cluster migration plans.

Every discovery input is a snapshot, not proof that an infrastructure endpoint is
currently healthy. This module NEVER opens connections or changes hypervisors.
Execution requires the independent, existing Immutavault restore approval path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


class MigrationInputError(ValueError):
    """Malformed or incomplete discovery or mapping document."""


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MigrationInputError(f"{where} must be an object")
    return value


def _array(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise MigrationInputError(f"{where} must be an array")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationInputError(f"{where} must be non-empty text")
    return value.strip()


def _size(value: Any, where: str) -> int:
    if type(value) is not int or value < 0:
        raise MigrationInputError(f"{where} must be a nonnegative integer byte count")
    return value


def _timestamp(value: Any, where: str) -> datetime:
    raw = _text(value, where)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationInputError(f"{where} must be an ISO8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise MigrationInputError(f"{where} requires timezone information")
    return parsed.astimezone(timezone.utc)


def _unique(rows: list[Any], where: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(rows):
        obj = _object(row, f"{where}[{i}]")
        key = _text(obj.get("id"), f"{where}[{i}].id")
        if key in found:
            raise MigrationInputError(f"{where} contains duplicate ID {key!r}")
        found[key] = obj
    return found


def read_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if p.stat().st_size > 8 * 1024 * 1024:
        raise MigrationInputError("migration JSON exceeds 8 MiB limit")
    return _object(json.loads(p.read_text(encoding="utf-8")), str(path))


def validate_snapshot(snapshot: dict[str, Any], *, now: datetime | None = None,
                      maximum_age_minutes: int = 120) -> dict[str, Any]:
    snap = _object(snapshot, "snapshot")
    if type(snap.get("schema_version")) is not int or snap["schema_version"] != 1:
        raise MigrationInputError("snapshot.schema_version must be 1")
    family = _text(snap.get("family"), "snapshot.family")
    if family not in {"xcpng", "vmware", "proxmox"}:
        raise MigrationInputError("unsupported snapshot family")
    _text(snap.get("platform_name"), "snapshot.platform_name")
    _text(snap.get("cluster_id"), "snapshot.cluster_id")
    captured = _timestamp(snap.get("captured_at"), "snapshot.captured_at")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise MigrationInputError("now must be timezone aware")
    age = (current.astimezone(timezone.utc) - captured).total_seconds()
    if age < -300:
        raise MigrationInputError("snapshot capture time is in the future")
    # Staleness is a blocking readiness check rather than a parsing error.
    hosts = _unique(_array(snap.get("hosts"), "snapshot.hosts"), "snapshot.hosts")
    networks = _unique(_array(snap.get("networks"), "snapshot.networks"), "snapshot.networks")
    storage = _unique(_array(snap.get("storage"), "snapshot.storage"), "snapshot.storage")
    vms = _unique(_array(snap.get("vms"), "snapshot.vms"), "snapshot.vms")
    if not hosts:
        raise MigrationInputError("snapshot.hosts must include hosts")
    for key, host in hosts.items():
        _text(host.get("name"), f"host {key}.name")
        _size(host.get("free_memory_bytes"), f"host {key}.free_memory_bytes")
        if not isinstance(host.get("healthy"), bool):
            raise MigrationInputError(f"host {key}.healthy must be true/false")
    for key, net in networks.items():
        _text(net.get("name"), f"network {key}.name")
        if type(net.get("vlan")) is not int or not 0 <= net["vlan"] <= 4094:
            raise MigrationInputError(f"network {key}.vlan must be 0..4094 (0 = native)")
        _array(net.get("accessible_hosts"), f"network {key}.accessible_hosts")
    for key, sr in storage.items():
        _text(sr.get("name"), f"storage {key}.name")
        _size(sr.get("free_bytes"), f"storage {key}.free_bytes")
        if not isinstance(sr.get("shared"), bool):
            raise MigrationInputError(f"storage {key}.shared must be true/false")
        _array(sr.get("accessible_hosts"), f"storage {key}.accessible_hosts")
    for key, vm in vms.items():
        _text(vm.get("name"), f"VM {key}.name")
        _size(vm.get("memory_bytes"), f"VM {key}.memory_bytes")
        _array(vm.get("nics"), f"VM {key}.nics")
        _array(vm.get("disks"), f"VM {key}.disks")
        for disk in vm["disks"]:
            d = _object(disk, f"VM {key} disk")
            _size(d.get("size_bytes"), f"VM {key} disk.size_bytes")
            _text(d.get("sr_id"), f"VM {key} disk.sr_id")
    return {"family": family, "hosts": hosts, "networks": networks, "storage": storage,
            "vms": vms, "captured_at": captured, "age_seconds": age,
            "fresh": age <= maximum_age_minutes * 60}


def _check(target: list[dict[str, Any]], code: str, passed: bool, detail: str) -> None:
    target.append({"code": code, "passed": bool(passed), "detail": detail})


def plan_migration(source: dict[str, Any], destination: dict[str, Any],
                   selection: dict[str, Any], *, now: datetime | None = None,
                   maximum_age_minutes: int = 120) -> dict[str, Any]:
    """Create deterministic fail-closed migration plan for exact VM IDs.

    Map source network IDs to exact destination IDs, and source SR IDs to target
    SR IDs. Evidence and topology snapshots cannot authorize VM execution.
    """
    now = now or datetime.now(timezone.utc)
    src = validate_snapshot(source, now=now, maximum_age_minutes=maximum_age_minutes)
    dst = validate_snapshot(destination, now=now, maximum_age_minutes=maximum_age_minutes)
    selection = _object(selection, "selection")
    vm_ids = _array(selection.get("vm_ids"), "selection.vm_ids")
    if not vm_ids or len(set(vm_ids)) != len(vm_ids) or any(not isinstance(x, str) or not x for x in vm_ids):
        raise MigrationInputError("selection.vm_ids must contain unique nonempty VM IDs")
    mapping = _object(selection.get("network_map"), "selection.network_map")
    storage_map = _object(selection.get("storage_map"), "selection.storage_map")
    evidence = _object(selection.get("evidence", {}), "selection.evidence")
    recovery_points = _object(selection.get("recovery_points", {}), "selection.recovery_points")
    required_evidence = ("switch_network_verified", "storage_multipath_verified",
                         "iscsi_paths_verified", "cluster_access_verified")
    reserve_pct = selection.get("reserve_percent", 20)
    if type(reserve_pct) is not int or not 0 <= reserve_pct <= 80:
        raise MigrationInputError("selection.reserve_percent must be integer from 0 to 80")
    if src["family"] != dst["family"] and not selection.get("certified_conversion_id"):
        # Plan may be examined, but conversion will be blocked.
        pass
    if source["platform_name"] == destination["platform_name"]:
        raise MigrationInputError("source and destination must be separate configured platforms")
    if source["cluster_id"] == destination["cluster_id"] and src["family"] == dst["family"]:
        raise MigrationInputError("source and destination may not be the same cluster")
    unknown = set(vm_ids) - set(src["vms"])
    if unknown:
        raise MigrationInputError("selected source VM IDs not found: " + ", ".join(sorted(unknown)))
    if any(v.get("is_control_domain") or v.get("is_template") or v.get("is_snapshot")
           for k, v in src["vms"].items() if k in vm_ids):
        raise MigrationInputError("cannot migrate control domains, templates or snapshots")
    dest_names = {v["name"].casefold() for v in dst["vms"].values()}
    # Never assume total free RAM is sufficient: place each VM on an actual host.
    host_remaining = {
        hid: h["free_memory_bytes"] * (100 - reserve_pct) // 100
        for hid, h in dst["hosts"].items() if h["healthy"] is True
    }
    storage_remaining = {
        sid: s["free_bytes"] * (100 - reserve_pct) // 100
        for sid, s in dst["storage"].items()
    }
    checks: list[dict[str, Any]] = []
    _check(checks, "source_snapshot_fresh", src["fresh"], "source discovery must be recent")
    _check(checks, "destination_snapshot_fresh", dst["fresh"], "destination discovery must be recent")
    _check(checks, "target_hosts_healthy", all(h["healthy"] for h in dst["hosts"].values()),
           "all target pool hosts must be healthy")
    for code in required_evidence:
        _check(checks, code, evidence.get(code) is True,
               "operator evidence required and must be revalidated live before migration")
    if src["family"] == dst["family"]:
        conversion_ok = True
    else:
        conversion_ok = (isinstance(selection.get("certified_conversion_id"), str)
                         and bool(selection["certified_conversion_id"].strip())
                         and evidence.get("certification_validated") is True)
    _check(checks, "transport_supported", conversion_ok,
           "cross-family migration requires separately certified provider and live verification")
    results: list[dict[str, Any]] = []
    # Largest-first minimizes accidental placement failures and is deterministic.
    ordered_ids = sorted(vm_ids, key=lambda key: (-src["vms"][key]["memory_bytes"], key))
    for vm_id in ordered_ids:
        vm = src["vms"][vm_id]
        row_checks: list[dict[str, Any]] = []
        prefix = _text(selection.get("target_prefix", "migrated-"), "selection.target_prefix")
        target_name = prefix + vm["name"]
        _check(row_checks, "unique_target_name", target_name.casefold() not in dest_names,
               "target must be a new VM, never an overwrite")
        # Reserve planned names too: different source VMs may have identical names.
        dest_names.add(target_name.casefold())
        mem = vm["memory_bytes"]
        eligible = sorted(host_remaining, key=lambda hid: (-host_remaining[hid], hid))
        chosen_host = next((hid for hid in eligible if mem > 0 and host_remaining[hid] >= mem), None)
        _check(row_checks, "host_memory_capacity", chosen_host is not None,
               "unreserved memory must fit a healthy individual destination host")
        if chosen_host:
            host_remaining[chosen_host] -= mem
        requested_recovery = _object(recovery_points.get(vm_id, {}), f"recovery point {vm_id}")
        backup_id = requested_recovery.get("snapshot_id")
        backup_good = (isinstance(backup_id, str) and bool(backup_id)
                       and requested_recovery.get("verified") is True
                       and requested_recovery.get("suspicious") is False
                       and requested_recovery.get("isolated_restore_test_passed") is True)
        _check(row_checks, "protection_evidence", backup_good,
               "verified non-suspicious backup and isolated restore-test record required")
        guest = _text(vm.get("firmware", "unknown"), f"VM {vm_id}.firmware")
        supported = destination.get("supported_firmware", [])
        _check(row_checks, "firmware_compatible", isinstance(supported, list) and guest in supported,
               f"guest firmware {guest!r} must be supported explicitly")
        specials = vm.get("special_devices", [])
        _check(row_checks, "special_devices_reviewed", specials == [],
               "passthrough, vTPM, SPAN, multi-homed or other special NICs require manual handling")
        # Each NIC is explicitly mapped, source and target VLAN tags must match.
        mapped_nics = []
        nic_pass = True
        for nic in vm["nics"]:
            nic = _object(nic, f"VM {vm_id} NIC")
            src_net = str(nic.get("network_id") or "")
            to = mapping.get(src_net)
            s_net = src["networks"].get(src_net)
            t_net = dst["networks"].get(to) if isinstance(to, str) else None
            exists = (s_net is not None and t_net is not None
                      and s_net["vlan"] == t_net["vlan"]
                      and chosen_host is not None
                      and chosen_host in t_net["accessible_hosts"])
            nic_pass &= exists
            mapped_nics.append({"source_network": src_net, "target_network": to,
                                "vlan": s_net["vlan"] if s_net else None, "valid": bool(exists)})
        _check(row_checks, "exact_network_mapping", nic_pass and bool(mapped_nics),
               "all NICs require same-VLAN, host-accessible destination network mapping")
        mapped_disks = []
        disk_pass = True
        for disk in vm["disks"]:
            source_sr = disk["sr_id"]
            to = storage_map.get(source_sr)
            target_sr = dst["storage"].get(to) if isinstance(to, str) else None
            capacity = disk["size_bytes"]
            exists = (
                target_sr is not None and target_sr["shared"] is True
                and chosen_host is not None and chosen_host in target_sr["accessible_hosts"]
                and capacity > 0 and storage_remaining.get(to, 0) >= capacity
            )
            disk_pass &= exists
            if exists:
                storage_remaining[to] -= capacity
            mapped_disks.append({"source_sr": source_sr, "target_sr": to,
                                 "size_bytes": capacity, "valid": bool(exists)})
        _check(row_checks, "shared_storage_capacity", disk_pass and bool(mapped_disks),
               "all disks require sufficient free space on shared, visible storage")
        _check(row_checks, "guest_consistency_review",
               requested_recovery.get("guest_consistency_approved") is True,
               "application-consistency requirements need explicit validation")
        results.append({
            "source_vm_id": vm_id, "source_name": vm["name"],
            "source_power_state": vm.get("power_state", "unknown"),
            "target_name": target_name, "destination_host": chosen_host,
            "nics": mapped_nics, "disks": mapped_disks,
            "snapshot_id": backup_id,
            "checks": row_checks, "ready_for_operator_review": all(c["passed"] for c in row_checks),
        })
    ready = all(c["passed"] for c in checks) and all(r["ready_for_operator_review"] for r in results)
    report = {
        "schema_version": 1, "source_platform": source["platform_name"],
        "destination_platform": destination["platform_name"],
        "source_cluster": source["cluster_id"], "destination_cluster": destination["cluster_id"],
        "source_family": src["family"], "destination_family": dst["family"],
        "source_captured_at": source["captured_at"],
        "destination_captured_at": destination["captured_at"],
        "global_checks": checks, "vms": results,
        "ready_for_operator_review": ready,
        "approved_to_execute": False,
        "execution_mode": "disabled: independent four-eyes restore authorization required",
        "source_fencing": "mandatory before production target power-on",
        "operator_warning": "Snapshots and attestations are NOT live validation. No VM changes were made.",
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["plan_sha256"] = sha256(canonical).hexdigest()
    return report


def create_restore_request(plan: dict[str, Any], vm_id: str, *, engine: Any,
                           requester: str) -> dict[str, Any]:
    """Request a powered-off restore through existing authorization; DO NOT execute it.

    The engine must independently validate snapshot, tenant and V2V policy.
    This method additionally enforces the migration planner's immutable VM IDs.
    """
    if not plan.get("ready_for_operator_review"):
        raise PermissionError("migration plan blocked; fix every preflight check first")
    if not requester or requester.strip() == "":
        raise MigrationInputError("named requester is required")
    rows = [r for r in plan.get("vms", []) if r.get("source_vm_id") == vm_id]
    if len(rows) != 1:
        raise MigrationInputError("source VM ID not uniquely present in plan")
    row = rows[0]
    if not row["ready_for_operator_review"]:
        raise PermissionError("selected VM failed preflight")
    point = engine.state.get_point(row["snapshot_id"])
    if not point or point.get("vm_id") != vm_id or point.get("platform") != plan["source_platform"]:
        raise PermissionError("recovery point does not belong to requested source VM/platform")
    if not point.get("verified") or point.get("suspicious"):
        raise PermissionError("source recovery point is not verified and clean")
    if not getattr(engine.cfg.protection, "require_four_eyes_restore", False):
        raise PermissionError("migration requires four-eyes restore approval to be enabled")
    request_id = engine.request_restore(
        snapshot_id=row["snapshot_id"],
        requester=requester,
        target_platform=plan["destination_platform"],
        target_name=row["target_name"],
        options={"migration_plan_sha256": plan["plan_sha256"]},
    )
    engine.state.audit(requester, "migration.restore.requested", "restore_request", str(request_id),
                       {"plan_sha256": plan["plan_sha256"], "source_vm_id": vm_id,
                        "target_platform": plan["destination_platform"]})
    return {"request_id": request_id, "state": "awaiting separate restore approval",
            "execution_performed": False,
            "warning": "Do not execute restore until network/SR placement, powered-off staging, and live checks are reviewed."}
