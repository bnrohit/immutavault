from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .consistency import normalize_provider_consistency, write_consistency
from .runner import run


PROTOCOL_VERSION = 1
DEFAULT_HELPER = "immutavault-vddk"
DEFAULT_BLOCK_SIZE = 128 * 1024 * 1024
CHECKPOINT_FILE = ".immutavault-cbt-checkpoint.json"
TRANSPORT_FILE = ".immutavault-transport.json"
LAYOUT_FILE = "immutavault-vddk-layout.json"


class IncrementalTransportError(RuntimeError):
    """A native incremental provider could not safely complete the request."""

    def __init__(self, message: str, *, reason: str = "provider_error", fallback_safe: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.fallback_safe = fallback_safe


@dataclass(frozen=True)
class IncrementalResult:
    path: Path
    mode: str
    changed_bytes: int = 0
    source_bytes_read: int = 0
    checkpoint: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


class VDDKProvider:
    """Capability-gated bridge to an externally installed VDDK/CBT provider.

    Immutavault does not redistribute Broadcom VDDK libraries. Authorized
    installations provide a helper implementing protocol v1. The helper reads
    secrets from the inherited GOVC_* environment and exchanges JSON over
    stdin/stdout so credentials never appear in process arguments.
    """

    def __init__(self, options: dict[str, Any], timeout: int) -> None:
        self.options = options
        self.timeout = timeout
        self.helper = str(options.get("vddk_helper") or DEFAULT_HELPER)

    def _resolved_helper(self) -> str | None:
        candidate = Path(self.helper)
        if candidate.is_absolute() or "/" in self.helper:
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        return shutil.which(self.helper)

    @staticmethod
    def _secure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    def capabilities(self, *, env: dict[str, str]) -> dict[str, Any]:
        helper = self._resolved_helper()
        if not helper:
            return {"available": False, "reason": "helper_missing", "helper": self.helper}
        result = run([helper, "capabilities", "--json"], timeout=min(self.timeout, 60), env=env, check=False)
        if result.returncode != 0:
            return {
                "available": False,
                "reason": "capability_probe_failed",
                "helper": helper,
                "error": result.stderr.strip() or result.stdout.strip(),
            }
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {"available": False, "reason": "invalid_capability_json", "helper": helper}
        features = set(payload.get("features") or [])
        protocol = int(payload.get("protocol_version") or 0)
        required = {"cbt", "backup", "restore"}
        available = protocol == PROTOCOL_VERSION and required.issubset(features)
        return {
            "available": available,
            "reason": None if available else "missing_required_capability",
            "helper": helper,
            "protocol_version": protocol,
            "features": sorted(features),
            "provider": payload.get("provider", "vddk"),
            "provider_version": payload.get("provider_version"),
            "transport_modes": payload.get("transport_modes") or [],
        }

    @staticmethod
    def _read_checkpoint(path: Path) -> dict[str, Any] | None:
        file = path / CHECKPOINT_FILE
        if not file.is_file():
            return None
        try:
            value = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        VDDKProvider._secure_dir(path.parent)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def backup(
        self,
        *,
        platform_name: str,
        endpoint: str,
        vm_id: str,
        vm_name: str,
        destination: Path,
        env: dict[str, str],
        quiesce: bool,
    ) -> IncrementalResult:
        caps = self.capabilities(env=env)
        if not caps.get("available"):
            raise IncrementalTransportError(
                f"VDDK/CBT provider unavailable: {caps.get('reason')}",
                reason=str(caps.get("reason") or "provider_unavailable"),
                fallback_safe=False,
            )
        helper = str(caps["helper"])
        app_strict = bool(self.options.get("application_consistency_strict", False))
        self._secure_dir(destination)
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "backup",
            "platform": platform_name,
            "endpoint": endpoint,
            "vm_id": vm_id,
            "vm_name": vm_name,
            "destination": str(destination),
            "previous_checkpoint": self._read_checkpoint(destination),
            "block_size": int(self.options.get("incremental_block_size") or DEFAULT_BLOCK_SIZE),
            "transport_order": list(self.options.get("vddk_transport_order") or ["san", "hotadd", "nbdssl"]),
            "quiesce": quiesce,
            "application_consistency_strict": app_strict,
        }
        result = run(
            [helper, "backup", "--json"],
            timeout=self.timeout,
            env=env,
            check=False,
            input_text=json.dumps(request),
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        if result.returncode != 0 or payload.get("status") != "success":
            reason = str(payload.get("reason") or "provider_error")
            message = str(payload.get("error") or result.stderr.strip() or result.stdout.strip() or reason)
            fallback_safe = bool(payload.get("fallback_safe", False))
            raise IncrementalTransportError(message, reason=reason, fallback_safe=fallback_safe)

        consistency = normalize_provider_consistency(
            payload.get("consistency"),
            requested=quiesce,
            strict=app_strict,
        )
        if app_strict and quiesce and not consistency.application_consistent:
            raise IncrementalTransportError(
                "VDDK provider reported backup success without proving application/guest quiescence; strict application consistency fails closed",
                reason="application_consistency_unproven",
                fallback_safe=False,
            )

        layout = destination / LAYOUT_FILE
        if not layout.is_file():
            raise IncrementalTransportError(
                f"VDDK provider reported success but did not create {LAYOUT_FILE}",
                reason="missing_layout",
                fallback_safe=False,
            )
        checkpoint = payload.get("checkpoint")
        if not isinstance(checkpoint, dict) or not checkpoint:
            raise IncrementalTransportError(
                "VDDK provider did not return a valid per-disk CBT checkpoint",
                reason="missing_checkpoint",
                fallback_safe=False,
            )

        # Only advance the checkpoint after all strict transport and consistency
        # gates have passed. A rejected point therefore cannot poison the next CBT run.
        self._write_json_atomic(destination / CHECKPOINT_FILE, checkpoint)
        write_consistency(destination, consistency)
        transport = {
            "version": 1,
            "provider": "vddk-cbt",
            "mode": str(payload.get("mode") or "incremental"),
            "changed_bytes": int(payload.get("changed_bytes") or 0),
            "source_bytes_read": int(payload.get("source_bytes_read") or 0),
            "transport": payload.get("transport"),
            "checkpoint": checkpoint,
            "consistency": consistency.as_dict(),
        }
        self._write_json_atomic(destination / TRANSPORT_FILE, transport)
        return IncrementalResult(
            path=destination,
            mode=str(transport["mode"]),
            changed_bytes=int(transport["changed_bytes"]),
            source_bytes_read=int(transport["source_bytes_read"]),
            checkpoint=checkpoint,
            detail=transport,
        )

    def restore(
        self,
        *,
        source: Path,
        target_name: str,
        options: dict[str, Any],
        env: dict[str, str],
    ) -> dict[str, Any]:
        caps = self.capabilities(env=env)
        if not caps.get("available"):
            raise RuntimeError(
                "this recovery point uses VDDK/CBT block layout, but the authorized VDDK provider is unavailable"
            )
        layout = source / LAYOUT_FILE
        if not layout.is_file():
            raise RuntimeError(f"incremental recovery layout is missing: {layout}")
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "restore",
            "source": str(source),
            "target_name": target_name,
            "options": options,
        }
        result = run(
            [str(caps["helper"]), "restore", "--json"],
            timeout=self.timeout,
            env=env,
            check=False,
            input_text=json.dumps(request),
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        if result.returncode != 0 or payload.get("status") != "success":
            raise RuntimeError(
                str(payload.get("error") or result.stderr.strip() or result.stdout.strip() or "VDDK restore failed")
            )
        restored = dict(payload.get("result") or {})
        restored.setdefault("name", target_name)
        restored.setdefault("transport", "vddk-cbt")
        return restored
