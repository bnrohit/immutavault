from __future__ import annotations

from dataclasses import replace
from typing import Any

from .management_config import ProtectionPolicy, V11Config
from .v2v_engine import CertifiedBackupEngine


class _PolicyConfigView:
    """Read-only config overlay used for one named policy execution.

    It narrows platform include scopes to exact checkbox-selected VM names,
    narrows replica targets, and applies the policy immutable window to that
    run. An empty replica list means primary repository only.
    """

    def __init__(self, cfg: V11Config, policy: ProtectionPolicy) -> None:
        self._cfg = cfg
        selections = {row.platform: row.vms for row in policy.selections}
        self.platforms = [
            replace(
                platform,
                enabled=platform.enabled and platform.name in selections,
                include=list(selections.get(platform.name, ())),
                exclude=[],
            )
            for platform in cfg.platforms
        ]
        selected = set(policy.replica_targets)
        self.replicas = [replica for replica in cfg.replicas if replica.name in selected]
        self.protection = replace(cfg.protection, verify_after_backup=policy.verify_after_backup)
        self.repository = replace(
            cfg.repository,
            retention=replace(cfg.repository.retention, keep_within_days=policy.immutable_days),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cfg, name)


class ProtectionPolicyRunner:
    def __init__(self, cfg: V11Config) -> None:
        self.cfg = cfg

    def policy(self, policy_id: str) -> ProtectionPolicy:
        for policy in self.cfg.management.policies:
            if policy.id == policy_id:
                return policy
        raise ValueError(f"unknown protection policy: {policy_id}")

    def run(self, policy_id: str, *, dry_run: bool = False, actor: str = "policy-scheduler") -> dict[str, Any]:
        policy = self.policy(policy_id)
        if not policy.enabled:
            raise RuntimeError(f"protection policy {policy.id} is disabled")
        engine = CertifiedBackupEngine(_PolicyConfigView(self.cfg, policy))
        results = engine.backup_all(dry_run=dry_run)
        failed = [row for row in results if row.get("status") == "failed"]
        detail = {
            "policy_id": policy.id,
            "policy_name": policy.name,
            "dry_run": dry_run,
            "immutable_days": policy.immutable_days,
            "replica_targets": list(policy.replica_targets),
            "results": results,
            "failed": len(failed),
        }
        if not dry_run:
            engine.state.audit(
                actor,
                "policy.backup.complete" if not failed else "policy.backup.partial_failure",
                "protection_policy",
                policy.id,
                {key: value for key, value in detail.items() if key != "results"},
            )
        return detail
