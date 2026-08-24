from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
from typing import Any

from immutavault.incremental import (
    LAYOUT_FILE,
    TRANSPORT_FILE,
    IncrementalTransportError,
    VDDKProvider,
)
from immutavault.runner import run
from immutavault.util import safe_component

from .base import VM
from .vmware import VMwareAdapter


INCREMENTAL_MODES = {"auto", "cbt", "vddk", "vddk-cbt"}


class VMwareIncrementalAdapter(VMwareAdapter):
    """VMware adapter that prefers an authorized VDDK/CBT provider.

    The existing hot-clone-export implementation remains the safe fallback.
    The provider cache is outside the one-shot staging tree. Each backup gets a
    hard-linked snapshot view so restic keeps a self-contained recovery point
    while unchanged block files are not recopied locally.
    """

    def _incremental_mode(self) -> bool:
        return self.cfg.mode.lower() in INCREMENTAL_MODES

    def _fallback_allowed(self) -> bool:
        if bool(self.cfg.options.get("incremental_strict", False)):
            return False
        return bool(self.cfg.options.get("incremental_fallback", True))

    def _provider(self) -> VDDKProvider:
        return VDDKProvider(self.cfg.options, self.timeout)

    def _fallback_adapter(self) -> VMwareAdapter:
        return VMwareAdapter(replace(self.cfg, mode="hot-clone-export"), self.timeout)

    def _cache_root(self, vm: VM) -> Path:
        base = Path(str(self.cfg.options.get("incremental_cache_root") or "/var/cache/immutavault/vddk"))
        return base / safe_component(self.cfg.name) / safe_component(vm.name)

    @staticmethod
    def _secure_parent(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    @staticmethod
    def _snapshot_view(cache: Path, target: Path) -> None:
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        for src in sorted(cache.rglob("*")):
            rel = src.relative_to(cache)
            dst = target / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                os.chmod(dst, 0o700)
                continue
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

    def doctor(self) -> list[str]:
        if not self._incremental_mode():
            return super().doctor()
        # Validate normal VMware snapshot/export capabilities using the proven
        # fallback mode; these are required if CBT must fall back.
        problems = self._fallback_adapter().doctor()
        try:
            caps = self._provider().capabilities(env=self._govc_env())
        except RuntimeError as exc:
            problems.append(str(exc))
            return problems
        if not caps.get("available") and not self._fallback_allowed():
            problems.append(
                "native VMware incremental transport is required but unavailable: "
                + str(caps.get("reason") or "provider unavailable")
            )
        return problems

    def platform_info(self) -> dict[str, Any]:
        info = super().platform_info()
        if self._incremental_mode():
            try:
                caps = self._provider().capabilities(env=self._govc_env())
            except Exception as exc:
                caps = {"available": False, "reason": str(exc)}
            info["backup_transport"] = {
                "requested": self.cfg.mode,
                "native_incremental": caps,
                "fallback": "hot-clone-export" if self._fallback_allowed() else None,
            }
        else:
            info["backup_transport"] = {"requested": self.cfg.mode, "native_incremental": {"available": False}}
        return info

    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        if not self._incremental_mode():
            return super().export(vm, destination, dry_run=dry_run)

        target = destination / safe_component(vm.name)
        env = self._govc_env()
        provider = self._provider()
        caps = provider.capabilities(env=env)
        if dry_run:
            if caps.get("available"):
                return target
            if self._fallback_allowed():
                return self._fallback_adapter().export(vm, destination, dry_run=True)
            raise RuntimeError(
                "native VMware incremental transport is unavailable and fallback is disabled: "
                + str(caps.get("reason") or "provider unavailable")
            )

        cache = self._cache_root(vm)
        self._secure_parent(cache.parent)
        try:
            result = provider.backup(
                platform_name=self.cfg.name,
                endpoint=self.cfg.endpoint,
                vm_id=vm.id,
                vm_name=vm.name,
                destination=cache,
                env=env,
                quiesce=bool(self.cfg.options.get("quiesce", True)),
            )
            self._snapshot_view(result.path, target)
            return target
        except IncrementalTransportError as exc:
            # A provider error can occur after it has already touched one or
            # more cached blocks. Unless the provider completed successfully we
            # cannot prove that cache represents one VMware point in time, so
            # discard it for *every* provider failure. The next native attempt
            # starts from a fresh baseline rather than trusting partial state.
            shutil.rmtree(cache, ignore_errors=True)
            if not self._fallback_allowed() or not exc.fallback_safe:
                raise RuntimeError(
                    f"native VMware incremental backup failed ({exc.reason}) and cannot safely fall back: {exc}"
                ) from exc
            fallback = self._fallback_adapter().export(vm, destination, dry_run=False)
            marker = {
                "version": 1,
                "provider": "hot-clone-export",
                "mode": "fallback-full",
                "fallback_reason": exc.reason,
                "fallback_error": str(exc),
                "native_cache_invalidated": True,
            }
            (fallback / TRANSPORT_FILE).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(fallback / TRANSPORT_FILE, 0o600)
            return fallback

    def restore(self, source: Path, *, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        layouts = sorted(source.rglob(LAYOUT_FILE))
        if not layouts:
            return super().restore(source, target_name=target_name, options=options, dry_run=dry_run)

        layout_root = layouts[0].parent
        env = self._govc_env()
        found = run(["govc", "find", "/", "-type", "m", "-name", target_name], timeout=60, env=env, check=False)
        if found.returncode == 0 and found.stdout.strip():
            raise RuntimeError(f"VM {target_name!r} already exists; Immutavault refuses overwrite")
        if dry_run:
            return {
                "platform": self.cfg.name,
                "name": target_name,
                "source": str(layout_root),
                "transport": "vddk-cbt",
                "dry_run": True,
            }
        result = self._provider().restore(source=layout_root, target_name=target_name, options=options, env=env)
        result.setdefault("platform", self.cfg.name)
        result.setdefault("source", str(layout_root))
        return result
