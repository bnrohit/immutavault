from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
from typing import Any


CONSISTENCY_FILE = ".immutavault-consistency.json"
APPLICATION_STATES = {"application-consistent", "guest-quiesced"}


@dataclass(frozen=True)
class ApplicationConsistency:
    state: str
    method: str
    requested: bool
    strict: bool
    provider_attested: bool = False
    detail: str | None = None

    @property
    def application_consistent(self) -> bool:
        return self.state in APPLICATION_STATES

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["application_consistent"] = self.application_consistent
        return value


def write_consistency(path: Path, value: ApplicationConsistency | dict[str, Any]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    payload = value.as_dict() if isinstance(value, ApplicationConsistency) else dict(value)
    target = path / CONSISTENCY_FILE
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return target


def read_consistency(path: Path) -> dict[str, Any] | None:
    target = path / CONSISTENCY_FILE
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def normalize_provider_consistency(value: Any, *, requested: bool, strict: bool) -> ApplicationConsistency:
    if not isinstance(value, dict):
        return ApplicationConsistency(
            state="unattested",
            method="provider",
            requested=requested,
            strict=strict,
            provider_attested=False,
            detail="provider did not return a consistency attestation",
        )
    state = str(value.get("state") or "unattested").strip().lower()
    method = str(value.get("method") or "provider").strip()
    detail = str(value.get("detail")) if value.get("detail") not in (None, "") else None
    return ApplicationConsistency(
        state=state,
        method=method,
        requested=requested,
        strict=strict,
        provider_attested=True,
        detail=detail,
    )


def point_consistency(point: dict[str, Any]) -> dict[str, Any]:
    """Return consistency metadata for portal/API display, including legacy points."""
    try:
        metadata = json.loads(point.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    value = metadata.get("application_consistency") if isinstance(metadata, dict) else None
    if isinstance(value, dict):
        result = dict(value)
        result.setdefault("state", "unknown")
        result.setdefault("method", "recorded")
        result.setdefault("application_consistent", str(result["state"]).lower() in APPLICATION_STATES)
        return result
    return {
        "state": "unknown",
        "method": "legacy-point",
        "requested": False,
        "strict": False,
        "provider_attested": False,
        "application_consistent": False,
        "detail": "recovery point predates v0.8 consistency metadata",
    }
