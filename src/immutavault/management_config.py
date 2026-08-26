from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml

from .v2v_config import V10Config, load_v10_config


POLICY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class PolicySelection:
    platform: str
    vms: tuple[str, ...]


@dataclass(frozen=True)
class PolicySchedule:
    frequency: str = "daily"
    time: str = "22:00"
    weekdays: tuple[str, ...] = field(default_factory=tuple)
    every_hours: int = 1

    def on_calendar(self) -> str | None:
        if self.frequency == "manual":
            return None
        if self.frequency == "daily":
            return f"*-*-* {self.time}:00"
        if self.frequency == "weekly":
            days = ",".join(day.title() for day in self.weekdays)
            return f"{days} *-*-* {self.time}:00"
        if self.frequency == "hourly":
            return f"*-*-* 00/{self.every_hours}:00:00"
        raise ValueError(f"unsupported policy frequency: {self.frequency}")


@dataclass(frozen=True)
class ProtectionPolicy:
    id: str
    name: str
    enabled: bool
    selections: tuple[PolicySelection, ...]
    schedule: PolicySchedule
    immutable_days: int
    replica_targets: tuple[str, ...] = field(default_factory=tuple)
    verify_after_backup: bool = True


@dataclass(frozen=True)
class ManagementConfig:
    enabled: bool = True
    broker_socket: str = "/run/immutavault/manage.sock"
    policies: tuple[ProtectionPolicy, ...] = field(default_factory=tuple)
    dr_test_networks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dr_test_boot_seconds: int = 30
    dr_test_auto_cleanup: bool = True


@dataclass(frozen=True)
class V11Config:
    v10: V10Config
    management: ManagementConfig

    def __getattr__(self, name: str) -> Any:
        return getattr(self.v10, name)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _schedule(raw: Any, name: str) -> PolicySchedule:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be a mapping")
    frequency = str(raw.get("frequency", "daily")).strip().lower()
    if frequency not in {"manual", "daily", "weekly", "hourly"}:
        raise ValueError(f"{name}.frequency must be manual, daily, weekly, or hourly")
    clock = str(raw.get("time", "22:00")).strip()
    if frequency in {"daily", "weekly"} and not TIME_RE.fullmatch(clock):
        raise ValueError(f"{name}.time must be HH:MM in 24-hour format")
    weekdays_raw = raw.get("weekdays") or []
    if isinstance(weekdays_raw, str):
        weekdays_raw = [weekdays_raw]
    if not isinstance(weekdays_raw, list):
        raise ValueError(f"{name}.weekdays must be a list")
    weekdays = tuple(str(day).strip().lower()[:3] for day in weekdays_raw)
    if frequency == "weekly":
        if not weekdays:
            raise ValueError(f"{name}.weekdays is required for weekly policies")
        invalid = [day for day in weekdays if day not in WEEKDAYS]
        if invalid:
            raise ValueError(f"{name}.weekdays contains invalid values: {invalid}")
    every_hours = _bounded_int(raw.get("every_hours", 1), f"{name}.every_hours", 1, 24)
    return PolicySchedule(
        frequency=frequency,
        time=clock,
        weekdays=weekdays,
        every_hours=every_hours,
    )


def load_v11_config(path: str | Path) -> V11Config:
    v10 = load_v10_config(path)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    management_raw = raw.get("management") or {}
    if not isinstance(management_raw, dict):
        raise ValueError("management must be a mapping")

    platform_names = {platform.name for platform in v10.platforms}
    replica_names = {replica.name for replica in v10.replicas if replica.enabled}
    policies: list[ProtectionPolicy] = []
    ids: set[str] = set()
    for index, item in enumerate(management_raw.get("policies") or []):
        if not isinstance(item, dict):
            raise ValueError("management.policies entries must be mappings")
        policy_id = str(item.get("id") or "").strip().lower()
        if not POLICY_ID_RE.fullmatch(policy_id):
            raise ValueError(
                f"management.policies[{index}].id must match {POLICY_ID_RE.pattern}"
            )
        if policy_id in ids:
            raise ValueError(f"duplicate management policy id: {policy_id}")
        ids.add(policy_id)
        display_name = str(item.get("name") or policy_id).strip()
        if not display_name:
            raise ValueError(f"management policy {policy_id}: name is required")

        selections: list[PolicySelection] = []
        seen_platforms: set[str] = set()
        for selection in item.get("selections") or []:
            if not isinstance(selection, dict):
                raise ValueError(f"management policy {policy_id}: selections must be mappings")
            platform = str(selection.get("platform") or "").strip()
            if platform not in platform_names:
                raise ValueError(f"management policy {policy_id}: unknown platform {platform!r}")
            if platform in seen_platforms:
                raise ValueError(f"management policy {policy_id}: duplicate selection for {platform}")
            seen_platforms.add(platform)
            vm_rows = selection.get("vms") or []
            if isinstance(vm_rows, str):
                vm_rows = [vm_rows]
            if not isinstance(vm_rows, list) or not vm_rows:
                raise ValueError(f"management policy {policy_id}: {platform} requires at least one VM")
            vms = tuple(str(vm).strip() for vm in vm_rows if str(vm).strip())
            if not vms:
                raise ValueError(f"management policy {policy_id}: {platform} requires at least one VM")
            if any(any(ch in vm for ch in "*?[]") for vm in vms):
                raise ValueError(
                    f"management policy {policy_id}: checkbox policies require exact VM names, not wildcard patterns"
                )
            if len(vms) != len(set(vms)):
                raise ValueError(f"management policy {policy_id}: duplicate VM name in {platform}")
            selections.append(PolicySelection(platform=platform, vms=vms))
        if not selections:
            raise ValueError(f"management policy {policy_id}: at least one platform/VM selection is required")

        replicas_raw = item.get("replica_targets") or []
        if isinstance(replicas_raw, str):
            replicas_raw = [replicas_raw]
        if not isinstance(replicas_raw, list):
            raise ValueError(f"management policy {policy_id}: replica_targets must be a list")
        replicas = tuple(str(name).strip() for name in replicas_raw if str(name).strip())
        unknown_replicas = sorted(set(replicas) - replica_names)
        if unknown_replicas:
            raise ValueError(
                f"management policy {policy_id}: unknown or disabled replicas: {unknown_replicas}"
            )

        policies.append(ProtectionPolicy(
            id=policy_id,
            name=display_name,
            enabled=bool(item.get("enabled", True)),
            selections=tuple(selections),
            schedule=_schedule(item.get("schedule"), f"management policy {policy_id}.schedule"),
            immutable_days=_bounded_int(
                item.get("immutable_days", v10.repository.retention.keep_within_days),
                f"management policy {policy_id}.immutable_days", 1, 3650,
            ),
            replica_targets=replicas,
            verify_after_backup=bool(item.get("verify_after_backup", True)),
        ))

    networks_raw = management_raw.get("dr_test_networks") or {}
    if not isinstance(networks_raw, dict):
        raise ValueError("management.dr_test_networks must be a mapping of platform to isolated network names")
    networks: dict[str, tuple[str, ...]] = {}
    for platform, value in networks_raw.items():
        platform_name = str(platform)
        if platform_name not in platform_names:
            raise ValueError(f"management.dr_test_networks references unknown platform {platform_name!r}")
        rows = [value] if isinstance(value, str) else value
        if not isinstance(rows, list):
            raise ValueError(f"management.dr_test_networks.{platform_name} must be a string or list")
        names = tuple(str(name).strip() for name in rows if str(name).strip())
        if not names:
            raise ValueError(f"management.dr_test_networks.{platform_name} cannot be empty")
        networks[platform_name] = names

    broker_socket = str(management_raw.get("broker_socket", "/run/immutavault/manage.sock")).strip()
    if not broker_socket.startswith("/"):
        raise ValueError("management.broker_socket must be an absolute path")

    management = ManagementConfig(
        enabled=bool(management_raw.get("enabled", True)),
        broker_socket=broker_socket,
        policies=tuple(policies),
        dr_test_networks=networks,
        dr_test_boot_seconds=_bounded_int(
            management_raw.get("dr_test_boot_seconds", 30), "management.dr_test_boot_seconds", 5, 900
        ),
        dr_test_auto_cleanup=bool(management_raw.get("dr_test_auto_cleanup", True)),
    )
    return V11Config(v10=v10, management=management)
