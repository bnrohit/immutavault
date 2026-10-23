"""Read-only discovery for Immutavault migration manifests.

XCP-ng discovery intentionally does NOT certify switch, iSCSI, multipath,
application consistency, or firmware. Ambiguous facts remain empty/unknown and
fail preflight. No xe write operation is executed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import shlex
from typing import Any

from .adapters import build_adapter
from .runner import run


UUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def _csv(text: str) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in text.replace("\n", ",").split(",") if v.strip()))


def discover_xcpng(platform: Any, timeout: int = 120) -> dict[str, Any]:
    if platform.type != "xcpng":
        raise ValueError("read-only XCP-ng discovery requires an XCP-ng platform")
    adapter = build_adapter(platform, timeout)
    warnings: list[str] = []

    def xe(cmd: str, *, required: bool = False, default: str = "") -> str:
        if not cmd.startswith("xe "):
            raise ValueError("discovery allows xe read commands only")
        verb = cmd.split()[1]
        if not (verb.endswith("-list") or verb.endswith("-param-get") or
                verb in {"host-compute-free-memory"}):
            raise ValueError("discovery refuses non-read-only xe command " + verb)
        result = run(adapter._ssh() + [cmd], timeout=timeout, check=False)
        if result.returncode != 0:
            if required:
                raise RuntimeError("XCP-ng discovery read failed (" + verb + "): " + result.stderr.strip())
            warnings.append("XCP-ng read unavailable: " + verb)
            return default
        return result.stdout.strip()

    def item(kind: str, uid: str, key: str, *, required: bool = False) -> str:
        if not UUID.fullmatch(uid):
            raise ValueError("unexpected XCP-ng object UUID")
        return xe("xe " + kind + "-param-get uuid=" + shlex.quote(uid) +
                  " param-name=" + shlex.quote(key), required=required)

    host_ids = _csv(xe("xe host-list params=uuid --minimal", required=True))
    if not host_ids:
        raise RuntimeError("XCP-ng discovery returned no hosts")
    hosts = []
    for host in host_ids:
        memory = item("host", host, "memory-free")
        enabled = item("host", host, "enabled").lower()
        hosts.append({
            "id": host, "name": item("host", host, "name-label", required=True),
            "free_memory_bytes": int(memory) if memory.isdigit() else 0,
            "healthy": enabled == "true",
            "discovery_note": "enabled=true is NOT a host health certificate",
        })
    pool_ids = _csv(xe("xe pool-list params=uuid --minimal", required=True))
    if len(pool_ids) != 1:
        raise RuntimeError("expected exactly one XCP-ng pool UUID")
    nets = []
    for network in _csv(xe("xe network-list params=uuid --minimal", required=True)):
        name = item("network", network, "name-label") or network
        pif_ids = _csv(xe("xe pif-list network-uuid=" + shlex.quote(network) + " params=uuid --minimal"))
        vlans: set[int] = set()
        reachable: set[str] = set()
        for pif in pif_ids:
            raw_vlan = item("pif", pif, "VLAN")
            raw_host = item("pif", pif, "host-uuid")
            if raw_vlan.lstrip("-").isdigit():
                vlan = int(raw_vlan)
                if vlan == -1:
                    vlan = 0
                if 0 <= vlan <= 4094:
                    vlans.add(vlan)
            if raw_host in host_ids:
                reachable.add(raw_host)
        if len(vlans) != 1:
            warnings.append("network VLAN cannot be uniquely verified: " + name)
        # -1 forces manual review instead of inventing an access VLAN.
        nets.append({"id": network, "name": name,
                     "vlan": next(iter(vlans)) if len(vlans) == 1 else -1,
                     "accessible_hosts": sorted(reachable)})
    srs = []
    for sr in _csv(xe("xe sr-list params=uuid --minimal", required=True)):
        size = item("sr", sr, "physical-size")
        used = item("sr", sr, "physical-utilisation")
        shared = item("sr", sr, "shared").lower()
        pbd_ids = _csv(xe("xe pbd-list sr-uuid=" + shlex.quote(sr) + " params=uuid --minimal"))
        reach = set()
        for pbd in pbd_ids:
            h = item("pbd", pbd, "host-uuid")
            attached = item("pbd", pbd, "currently-attached").lower()
            if h in host_ids and attached == "true":
                reach.add(h)
        free = max(0, int(size) - int(used)) if size.isdigit() and used.isdigit() else 0
        srs.append({"id": sr, "name": item("sr", sr, "name-label") or sr,
                    "free_bytes": free, "shared": shared == "true",
                    "accessible_hosts": sorted(reach),
                    "discovery_note": "attached SR is not proof of healthy redundant multipath"})
    vms = []
    for vm in _csv(xe("xe vm-list is-control-domain=false params=uuid --minimal", required=True)):
        if item("vm", vm, "is-a-snapshot").lower() == "true":
            continue
        if item("vm", vm, "is-a-template").lower() == "true":
            continue
        mem = item("vm", vm, "memory-static-max")
        nics = []
        for vif in _csv(xe("xe vif-list vm-uuid=" + shlex.quote(vm) + " params=uuid --minimal")):
            nics.append({"id": vif, "network_id": item("vif", vif, "network-uuid"),
                         "device": item("vif", vif, "device"), "mac": item("vif", vif, "MAC")})
        disks = []
        for vbd in _csv(xe("xe vbd-list vm-uuid=" + shlex.quote(vm) +
                           " type=Disk params=uuid --minimal")):
            vdi = item("vbd", vbd, "vdi-uuid")
            if not UUID.fullmatch(vdi):
                continue
            size = item("vdi", vdi, "virtual-size")
            disks.append({"id": vdi, "sr_id": item("vdi", vdi, "sr-uuid"),
                          "size_bytes": int(size) if size.isdigit() else 0})
        vms.append({
            "id": vm, "name": item("vm", vm, "name-label", required=True),
            "memory_bytes": int(mem) if mem.isdigit() else 0,
            "power_state": item("vm", vm, "power-state"),
            "firmware": "unknown", "nics": nics, "disks": disks,
            "special_devices": ["hardware_and_nic_profile_unreviewed"],
        })
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "family": "xcpng", "platform_name": platform.name,
        "cluster_id": pool_ids[0],
        "hosts": hosts, "networks": nets, "storage": srs, "vms": vms,
        "discovery_warnings": warnings,
        "discovery_status": "READ_ONLY_UNATTESTED",
        "discovery_guidance": "Explicitly verify VLANs, host/SR access, storage, firmware and special NICs before plan approval.",
    }


def discover_vmware(platform: Any, timeout: int = 120) -> dict[str, Any]:
    """Capture VM identities without guessing VMware resources or port groups.

    vSphere inventory shapes vary by vCenter/govc version; a full VMware
    topology provider is intentionally deferred rather than returning false
    positive resource/network maps.
    """
    if platform.type != "vmware":
        raise ValueError("VMware discovery requires a VMware platform")
    adapter = build_adapter(platform, timeout)
    vms = adapter.inventory()
    return {
        "schema_version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
        "family": "vmware", "platform_name": platform.name,
        "cluster_id": "UNVERIFIED-" + platform.name,
        "hosts": [{"id": "unverified-host", "name": "UNVERIFIED", "healthy": False, "free_memory_bytes": 0}],
        "networks": [], "storage": [],
        "vms": [{"id": vm.id, "name": vm.name, "memory_bytes": 0, "nics": [],
                 "disks": [], "firmware": "unknown", "special_devices": ["unverified-vsphere-topology"]}
                for vm in vms],
        "discovery_status": "IDENTITIES_ONLY_BLOCKED",
        "discovery_warnings": ["VMware RAM, datastore, distributed port groups, lag1 and iSCSI are NOT certified by identity discovery."],
    }


def discover(platform: Any, timeout: int = 120) -> dict[str, Any]:
    if platform.type == "xcpng":
        return discover_xcpng(platform, timeout)
    if platform.type == "vmware":
        return discover_vmware(platform, timeout)
    raise ValueError("migration discovery supports VMware and XCP-ng only")
