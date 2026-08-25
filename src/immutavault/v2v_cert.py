from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .config import PlatformConfig
from .v2v import (
    BUILTIN_PAIR,
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
