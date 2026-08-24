from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from immutavault.config import PlatformConfig
from immutavault.util import safe_component


@dataclass(frozen=True)
class VM:
    id: str
    name: str
    power_state: str = "unknown"
    kind: str = "vm"


class Adapter(ABC):
    def __init__(self, cfg: PlatformConfig, timeout: int) -> None:
        self.cfg = cfg
        self.timeout = timeout

    def backup_root(self, base: Path, vm: VM, stamp: str) -> Path:
        """Return the staging root used for one backup operation.

        Export-style adapters use timestamped roots. Native incremental
        transports may override this with a stable per-VM cache so unchanged
        block files keep stable paths/inodes across restic snapshots.
        """
        return base / safe_component(self.cfg.name) / safe_component(vm.name) / stamp

    def retain_backup_root(self, vm: VM) -> bool:
        """Whether a successful backup root is a persistent transport cache."""
        return False

    @abstractmethod
    def doctor(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def platform_info(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def inventory(self) -> list[VM]:
        raise NotImplementedError

    @abstractmethod
    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        raise NotImplementedError

    @abstractmethod
    def restore(self, source: Path, *, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Restore as a NEW workload by default. Implementations must not overwrite production implicitly."""
        raise NotImplementedError

    @abstractmethod
    def power_on(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def power_off(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        raise NotImplementedError
