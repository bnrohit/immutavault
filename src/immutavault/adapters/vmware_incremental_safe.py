from __future__ import annotations

from pathlib import Path

from .base import VM
from .vmware_incremental import IncrementalUnavailable, VMwareIncrementalAdapter as _IncrementalAdapter
from immutavault.transport_state import read_json, state_path
from immutavault.util import safe_component


class VMwareIncrementalAdapter(_IncrementalAdapter):
    """Production wrapper: fallback only for known incremental-invalid states.

    Unexpected runtime and cleanup/consolidation errors are intentionally allowed
    to fail the job instead of being hidden behind a second full snapshot cycle.
    """

    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        mode = self.cfg.mode.lower()
        if mode in self.LEGACY:
            return super(_IncrementalAdapter, self).export(vm, destination, dry_run=dry_run)
        if mode not in self.INCREMENTAL | self.STRICT:
            raise ValueError(f"unsupported VMware backup mode: {self.cfg.mode}")
        if dry_run:
            return destination / safe_component(vm.name)

        state = read_json(state_path(self.cfg.name, vm.id))
        if not state:
            return self._baseline(vm, destination, "initial CBT baseline")
        max_chain = max(2, int(self.cfg.options.get("cbt_max_chain_length", 32)))
        if len(state.get("chain_snapshot_ids") or []) >= max_chain:
            return self._baseline(vm, destination, f"scheduled full after {max_chain} points")

        try:
            problems = self._vddk_problems()
            if problems:
                raise IncrementalUnavailable("; ".join(problems))
            return self._delta(vm, destination, state)
        except IncrementalUnavailable as exc:
            if not self._fallback_allowed():
                raise
            return self._baseline(vm, destination, str(exc))
