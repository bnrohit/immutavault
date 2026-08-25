from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re

import yaml

from .enterprise_config import EnterpriseConfig, load_enterprise_config


PAIR_RE = re.compile(r"^(vmware|proxmox|xcpng):(vmware|proxmox|xcpng)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class V2VProviderConfig:
    name: str
    helper: str
    sha256: str
    pairs: list[str]
    certification_id: str
    enabled: bool = True


@dataclass(frozen=True)
class V2VConfig:
    enabled: bool = False
    builtin_vmware_to_proxmox: bool = True
    require_verified_point: bool = True
    allow_suspicious_points: bool = False
    virt_v2v_min_version: str = "2.12.0"
    max_disks: int = 16
    max_virtual_bytes: int = 64 * 1024 * 1024 * 1024 * 1024
    require_network_mapping: bool = True
    allow_uefi: bool = True
    allow_secure_boot: bool = False
    provider_timeout_seconds: int = 14400
    providers: list[V2VProviderConfig] = field(default_factory=list)


@dataclass(frozen=True)
class V10Config:
    enterprise: EnterpriseConfig
    v2v: V2VConfig

    def __getattr__(self, name: str) -> Any:
        return getattr(self.enterprise, name)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _pairs(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    result: list[str] = []
    for item in value:
        pair = str(item).strip().lower()
        match = PAIR_RE.fullmatch(pair)
        if not match:
            raise ValueError(f"{name} contains invalid pair {pair!r}")
        if match.group(1) == match.group(2):
            raise ValueError(f"{name} must contain cross-hypervisor pairs only")
        result.append(pair)
    return result


def load_v10_config(path: str | Path) -> V10Config:
    enterprise = load_enterprise_config(path)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    row = raw.get("v2v") or {}
    providers: list[V2VProviderConfig] = []
    names: set[str] = set()
    pair_owners: dict[str, str] = {}
    for index, item in enumerate(row.get("providers", []) or []):
        if not isinstance(item, dict):
            raise ValueError("v2v.providers entries must be mappings")
        name = str(item.get("name") or "").strip()
        helper = str(item.get("helper") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        certification_id = str(item.get("certification_id") or "").strip()
        if not name:
            raise ValueError(f"v2v.providers[{index}].name is required")
        if name in names:
            raise ValueError(f"duplicate V2V provider name: {name}")
        names.add(name)
        if not helper.startswith("/"):
            raise ValueError(f"v2v provider {name}: helper must be an absolute path")
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"v2v provider {name}: sha256 must be a lowercase 64-character digest")
        if not certification_id:
            raise ValueError(f"v2v provider {name}: certification_id is required")
        pairs = _pairs(item.get("pairs"), f"v2v provider {name}.pairs")
        for pair in pairs:
            if pair in pair_owners:
                raise ValueError(f"V2V pair {pair} is claimed by both {pair_owners[pair]} and {name}")
            pair_owners[pair] = name
        providers.append(V2VProviderConfig(
            name=name,
            helper=helper,
            sha256=digest,
            pairs=pairs,
            certification_id=certification_id,
            enabled=bool(item.get("enabled", True)),
        ))

    max_disks = _bounded_int(row.get("max_disks", 16), "v2v.max_disks", 1, 64)
    max_virtual_bytes = _bounded_int(
        row.get("max_virtual_bytes", 64 * 1024 * 1024 * 1024 * 1024),
        "v2v.max_virtual_bytes", 1024 * 1024 * 1024, 1024 * 1024 * 1024 * 1024 * 1024,
    )
    timeout = _bounded_int(row.get("provider_timeout_seconds", 14400), "v2v.provider_timeout_seconds", 60, 172800)
    min_version = str(row.get("virt_v2v_min_version", "2.12.0")).strip()
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", min_version):
        raise ValueError("v2v.virt_v2v_min_version must be a numeric version such as 2.12.0")

    cfg = V2VConfig(
        enabled=bool(row.get("enabled", False)),
        builtin_vmware_to_proxmox=bool(row.get("builtin_vmware_to_proxmox", True)),
        require_verified_point=bool(row.get("require_verified_point", True)),
        allow_suspicious_points=bool(row.get("allow_suspicious_points", False)),
        virt_v2v_min_version=min_version,
        max_disks=max_disks,
        max_virtual_bytes=max_virtual_bytes,
        require_network_mapping=bool(row.get("require_network_mapping", True)),
        allow_uefi=bool(row.get("allow_uefi", True)),
        allow_secure_boot=bool(row.get("allow_secure_boot", False)),
        provider_timeout_seconds=timeout,
        providers=providers,
    )
    return V10Config(enterprise=enterprise, v2v=cfg)
