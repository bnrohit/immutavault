"""Readable, tamper-evident migration report for change-control operators."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any


REMEDIATION = {
    "source_snapshot_fresh": "Re-run source read-only discovery.",
    "destination_snapshot_fresh": "Re-run destination read-only discovery.",
    "target_hosts_healthy": "Inspect all destination hosts before scheduling.",
    "switch_network_verified": "Review switch LACP, VLAN and physical port evidence.",
    "storage_multipath_verified": "Verify every SAN LUN, path and multipath policy.",
    "iscsi_paths_verified": "Check both iSCSI fabrics and target sessions.",
    "cluster_access_verified": "Check pool identity and host/SR accessibility.",
    "transport_supported": "Select a certified migration transport; do not assume V2V.",
    "unique_target_name": "Choose a unique powered-off destination VM name.",
    "host_memory_capacity": "Free destination host memory or choose another host.",
    "protection_evidence": "Verify immutable backup and isolated restore evidence.",
    "firmware_compatible": "Check BIOS/UEFI and guest boot compatibility.",
    "special_devices_reviewed": "Manually handle passthrough, SPAN, TPM and multi-NIC VMs.",
    "destination_not_management_appliance": "Move migration management separately.",
    "exact_network_mapping": "Map all NICs to validated target VLANs and reachable hosts.",
    "shared_storage_capacity": "Present new shared SR on every host and confirm capacity.",
    "guest_consistency_review": "Obtain app owner signoff and test recovery consistency.",
}


class InvalidMigrationPlan(ValueError):
    pass


def summarize_migration_plan(report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise InvalidMigrationPlan("expected version-1 migration plan")
    supplied = report.get("plan_sha256")
    raw = {k: v for k, v in report.items() if k != "plan_sha256"}
    digest = sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if not isinstance(supplied, str) or supplied != digest:
        raise InvalidMigrationPlan("migration plan integrity check failed")
    global_checks = report.get("global_checks")
    vms = report.get("vms")
    if not isinstance(global_checks, list) or not isinstance(vms, list) or not vms:
        raise InvalidMigrationPlan("migration plan has no valid checks or VMs")
    blocked = []
    for check in global_checks:
        if not isinstance(check, dict) or not isinstance(check.get("passed"), bool):
            raise InvalidMigrationPlan("malformed global migration check")
        if not check["passed"]:
            blocked.append(check.get("code", "unknown"))
    rows = []
    for vm in vms:
        if not isinstance(vm, dict) or not isinstance(vm.get("checks"), list):
            raise InvalidMigrationPlan("malformed VM migration row")
        failed = []
        for check in vm["checks"]:
            if not isinstance(check, dict) or not isinstance(check.get("passed"), bool):
                raise InvalidMigrationPlan("malformed VM check")
            if not check["passed"]:
                failed.append(check.get("code", "unknown"))
        rows.append({
            "vm": vm.get("source_name"), "source_id": vm.get("source_vm_id"),
            "destination_host": vm.get("destination_host"),
            "status": "BLOCKED" if failed or blocked else "READY_FOR_OPERATOR_REVIEW",
            "blockers": failed,
            "next_actions": list(dict.fromkeys(REMEDIATION.get(code, "Investigate " + str(code))
                                               for code in failed)),
        })
    all_codes = list(dict.fromkeys(blocked + [code for vm in rows for code in vm["blockers"]]))
    ready = bool(report.get("ready_for_operator_review")) and not all_codes
    return {
        "status": "READY_FOR_OPERATOR_REVIEW" if ready else "BLOCKED",
        "approval_to_execute": False,
        "source": report.get("source_platform"),
        "destination": report.get("destination_platform"),
        "total_vms": len(rows),
        "blocked_vms": sum(row["status"] == "BLOCKED" for row in rows),
        "global_blockers": blocked,
        "next_actions": list(dict.fromkeys(REMEDIATION.get(code, "Investigate " + str(code))
                                           for code in all_codes)),
        "vms": rows,
        "warning": "No VM changes performed; checks are historical snapshots, not live cutover approval",
    }
