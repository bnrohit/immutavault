from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
from typing import Any
import xml.etree.ElementTree as ET

from .adapters.proxmox import ProxmoxAdapter
from .config import PlatformConfig
from .runner import run
from .v2v import (
    BRIDGE_RE,
    BUILTIN_PAIR,
    STORAGE_RE,
    ConvertedBundle,
    ConvertedNIC,
    V2VInspection,
    V2VManager,
    V2VPlan,
)


OVF_EXPORT_TRANSPORTS = {
    "export",
    "cold-export",
    "hot-clone-export",
    "snapshot-clone-export",
    "hot",
}
NATIVE_INCREMENTAL_TRANSPORTS = {"auto", "cbt", "vddk", "vddk-cbt"}


class CertifiedV2VManager(V2VManager):
    """V2V manager with recovery-point and guest-layout certification guards."""

    @staticmethod
    def _vmware_transport(point: dict[str, Any]) -> str | None:
        try:
            metadata = json.loads(point.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        info = metadata.get("platform_info") or {}
        if not isinstance(info, dict):
            return None
        transport = info.get("backup_transport") or {}
        if not isinstance(transport, dict):
            return None
        requested = str(transport.get("requested") or "").strip().lower()
        return requested or None

    @staticmethod
    def _child_text(node: ET.Element, suffix: str) -> str | None:
        for child in list(node):
            if child.tag == suffix or child.tag.endswith("}" + suffix):
                value = (child.text or "").strip()
                return value or None
        return None

    def _prepare_ova(self, source: Path, work: Path) -> tuple[Path, dict[str, Any]]:
        ova, flags = super()._prepare_ova(source, work)
        ovfs = sorted(source.rglob("*.ovf"))
        if len(ovfs) != 1:
            return ova, flags
        try:
            root = ET.parse(ovfs[0]).getroot()
        except ET.ParseError:
            return ova, flags

        # Use the network attached to each virtual NIC, in Item order. A list of
        # NetworkSection definitions is not sufficient because a guest can have
        # multiple NICs attached to the same or different networks.
        connections: list[str] = []
        for item in root.findall(".//{*}Item"):
            resource_type = self._child_text(item, "ResourceType")
            if resource_type != "10":
                continue
            connection = self._child_text(item, "Connection")
            if not connection:
                raise RuntimeError("OVF NIC is missing an explicit Connection; refusing ambiguous network remap")
            connections.append(connection)
        if connections:
            flags["source_networks"] = connections

        # Do not default an un-attested firmware type to BIOS. The inspector may
        # report firmware itself; otherwise require explicit OVF evidence.
        text = ovfs[0].read_text(encoding="utf-8", errors="replace").lower()
        if "uefi" in text or "efi" in text:
            flags["firmware_hint"] = "uefi"
        elif "bios" in text or "legacy" in text:
            flags["firmware_hint"] = "bios"
        else:
            flags["firmware_hint"] = "unknown"
        return ova, flags

    def _inspection(self, ova: Path, flags: dict[str, Any]) -> V2VInspection:
        inspection = super()._inspection(ova, flags)
        if inspection.firmware not in {"bios", "uefi"}:
            raise RuntimeError("guest firmware could not be attested as BIOS or UEFI; built-in V2V refuses to guess")
        return inspection

    def _parse_bundle(self, output: Path, inspection: V2VInspection) -> ConvertedBundle:
        bundle = super()._parse_bundle(output, inspection)
        if inspection.source_networks:
            if len(inspection.source_networks) != len(bundle.nics):
                raise RuntimeError(
                    "converted NIC count does not match OVF NIC Connection metadata; refusing ambiguous network remap"
                )
            nics = tuple(
                ConvertedNIC(mac=nic.mac, source_network=inspection.source_networks[index])
                for index, nic in enumerate(bundle.nics)
            )
            bundle = ConvertedBundle(
                domain_xml=bundle.domain_xml,
                disks=bundle.disks,
                nics=nics,
                memory_mib=bundle.memory_mib,
                vcpus=bundle.vcpus,
                firmware=bundle.firmware,
                inspection=bundle.inspection,
            )
        elif bundle.nics and self.cfg.v2v.require_network_mapping:
            raise RuntimeError("source NIC network identity is unavailable; refusing to guess target bridge mapping")
        return bundle

    def _proxmox_storage_ids(self, target: PlatformConfig, options: dict[str, Any]) -> tuple[str, ...]:
        storage = str(
            options.get("storage")
            or target.options.get("v2v_storage")
            or target.options.get("restore_storage")
            or ""
        ).strip()
        if not storage or not STORAGE_RE.fullmatch(storage):
            raise RuntimeError("Proxmox V2V target storage is missing or invalid")
        values = [storage]
        efi_storage = str(options.get("efi_storage") or target.options.get("v2v_efi_storage") or storage).strip()
        if efi_storage:
            if not STORAGE_RE.fullmatch(efi_storage):
                raise RuntimeError("Proxmox V2V EFI storage id is invalid")
            if efi_storage not in values:
                values.append(efi_storage)
        return tuple(values)

    def _preflight_proxmox_storage(self, target: PlatformConfig, options: dict[str, Any]) -> None:
        """Fail before conversion when the target image storage is unavailable.

        `pvesm status --content images --enabled 1` is authoritative for whether
        a configured Proxmox storage can currently accept QEMU VM images.
        """
        adapter = ProxmoxAdapter(target, self.cfg.runtime.command_timeout_seconds)
        for storage in self._proxmox_storage_ids(target, options):
            command = (
                "pvesm status --storage " + shlex.quote(storage)
                + " --content images --enabled 1"
            )
            result = run(adapter._ssh() + [command], timeout=60, check=False)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "pvesm status failed").strip()
                raise RuntimeError(f"Proxmox V2V storage preflight failed for {storage!r}: {detail}")
            rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
            matched = [row for row in rows if row and row[0] == storage]
            if not matched:
                raise RuntimeError(
                    f"Proxmox V2V storage {storage!r} is not enabled/available for VM image content"
                )
            if not any(any(value.lower() == "active" for value in row[1:4]) for row in matched):
                raise RuntimeError(f"Proxmox V2V storage {storage!r} is not active")

    def _preflight_proxmox_bridges(self, bundle: ConvertedBundle, target: PlatformConfig, options: dict[str, Any]) -> list[str]:
        adapter = ProxmoxAdapter(target, self.cfg.runtime.command_timeout_seconds)
        bridges = self._mapped_bridges(bundle, target, options)
        for bridge in sorted(set(bridges)):
            if not BRIDGE_RE.fullmatch(bridge):
                raise RuntimeError(f"unsafe or invalid Proxmox bridge name: {bridge!r}")
            result = run(
                adapter._ssh() + ["ip -o link show dev " + shlex.quote(bridge)],
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Proxmox V2V target bridge {bridge!r} does not exist on the selected node")
        return bridges

    def _restore_proxmox(
        self,
        bundle: ConvertedBundle,
        target: PlatformConfig,
        target_name: str,
        options: dict[str, Any],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        if not dry_run:
            # Storage was checked before conversion; repeat it immediately before
            # target creation and validate every mapped bridge to close the race.
            self._preflight_proxmox_storage(target, options)
            self._preflight_proxmox_bridges(bundle, target, options)
        return super()._restore_proxmox(bundle, target, target_name, options, dry_run=dry_run)

    def execute(
        self,
        *,
        source: Path,
        point: dict[str, Any],
        target: PlatformConfig,
        target_name: str,
        options: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        plan = self.plan(point, target, options)
        if plan.allowed and plan.mode == "builtin" and self._pair(str(point.get("platform_type") or ""), target.type) == BUILTIN_PAIR:
            # A read-only target storage check happens before a potentially long
            # virt-v2v conversion so an offline/disabled datastore fails fast.
            if not dry_run:
                self._preflight_proxmox_storage(target, options)
        return super().execute(
            source=source,
            point=point,
            target=target,
            target_name=target_name,
            options=options,
            dry_run=dry_run,
        )

    def plan(self, point: dict[str, Any], target: PlatformConfig, options: dict[str, Any] | None = None) -> V2VPlan:
        plan = super().plan(point, target, options)
        pair = self._pair(str(point.get("platform_type") or ""), target.type)
        if pair != BUILTIN_PAIR or plan.mode != "builtin" or not plan.allowed:
            return plan
        transport = self._vmware_transport(point)
        if transport in OVF_EXPORT_TRANSPORTS:
            return plan
        if transport in NATIVE_INCREMENTAL_TRANSPORTS:
            reason = (
                f"VMware recovery point transport {transport!r} is a native CBT/VDDK layout, not an attested OVF input; "
                "capture an export-format recovery point or configure a certified provider for native-layout conversion"
            )
        else:
            reason = (
                "VMware recovery point does not attest an OVF/export transport; built-in V2V refuses to guess the source layout"
            )
        return V2VPlan(
            allowed=False,
            source_type=plan.source_type,
            target_type=plan.target_type,
            mode=plan.mode,
            certification_id=plan.certification_id,
            provider=plan.provider,
            reasons=tuple(list(plan.reasons) + [reason]),
        )
