from __future__ import annotations

import json
from typing import Any

from .config import PlatformConfig
from .v2v import BUILTIN_PAIR, V2VManager, V2VPlan


OVF_EXPORT_TRANSPORTS = {
    "export",
    "cold-export",
    "hot-clone-export",
    "snapshot-clone-export",
    "hot",
}
NATIVE_INCREMENTAL_TRANSPORTS = {"auto", "cbt", "vddk", "vddk-cbt"}


class CertifiedV2VManager(V2VManager):
    """V2V manager with recovery-point format attestation.

    The built-in converter consumes an OVF/OVA-style VMware export. Native
    VDDK/CBT recovery layouts are valid recovery points, but they are not
    silently treated as OVF. Those points remain blocked for the built-in path
    until a separately certified provider declares support for that layout.
    """

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
