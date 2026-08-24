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

# Capability failures happen before the provider is allowed to touch the CBT
# cache. Only these explicit capability states may degrade to the proven full
# hot-clone path when strict mode is off.
SAFE_CAPABILITY_FALLBACK_REASONS = {
    "helper_missing",
    "missing_required_capability",
}

# Once the provider has entered backup, fallback requires BOTH an explicit
# provider assertion (fallback_safe=true) and one of these known reasons. An
# unknown reason is ambiguous and therefore fails closed.
SAFE_PROVIDER_FALLBACK_REASONS = {
    "cbt_disabled",
    "cbt_uninitialized",
    "cbt_not_supported",
    "cbt_invalid",
    "change_id_reset",
    "invalid_change_id",
    "unsupported_disk",
    "baseline_required",
}


def incremental_fallback_allowed(
    *,
    strict: bool,
    configured: bool,
    reason: str,
    fallback_safe: bool,
    capability_stage: bool = False,
) -> bool:
    """Return True only when a fallback is explicitly safe and policy allows it.

    `incremental_strict: true` is absolute: no native transport failure is
    converted into a hot-clone backup. In non-strict mode, unknown/ambiguous
    reasons are also denied even if a buggy provider claims `fallback_safe`.
    """
    if strict or not configured or not fallback_safe:
        return False
    allowed = SAFE_CAPABILITY_FALLBACK_REASONS if capability_stage else SAFE_PROVIDER_FALLBACK_REASONS
    return reason in allowed


class VMwareIncrementalAdapter(VMwareAdapter):
    """VMware adapter that prefers an authorized VDDK/CBT provider.

    The existing hot-clone-export implementation remains a controlled fallback.
    `incremental_strict: true` disables fallback entirely. When strict mode is
    off, only explicitly allow-listed, provider-declared safe states may fall
    back. Unsafe, malformed, unknown, or ambiguous provider states fail closed.

    The provider cache is outside the one-shot staging tree. Each backup gets a
    hard-linked snapshot view so restic keeps a self-contained recovery point
    while unchanged block files are not recopied locally.
    """

    def _incremental_mode(self) -> bool:
        return self.cfg.mode.lower() in INCREMENTAL_MODES

    def _strict(self) -> bool:
        return bool(self.cfg.options.get("incremental_strict", False))

    def _fallback_configured(self) -> bool:
        return bool(self.cfg.options.get("incremental_fallback", True))

    def _capability_fallback_allowed(self, reason: str) -> bool:
        return incremental_fallback_allowed(
            strict=self._strict(),
            configured=self._fallback_configured(),
            reason=reason,
            fallback_safe=True,
            capability_stage=True,
        )

    def _provider_fallback_allowed(self, exc: IncrementalTransportError) -> bool:
        return incremental_fallback_allowed(
            strict=self._strict(),
            configured=self._fallback_configured(),
            reason=str(exc.reason or "provider_state_ambiguous"),
            fallback_safe=bool(exc.fallback_safe),
            capability_stage=False,
        )

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

    def _full_fallback(self, vm: VM, destination: Path, *, reason: str, error: str, cache_invalidated: bool) -> Path:
        fallback = self._fallback_adapter().export(vm, destination, dry_run=False)
        marker = {
            "version": 1,
            "provider": "hot-clone-export",
            "mode": "fallback-full",
            "fallback_reason": reason,
            "fallback_error": error,
            "native_cache_invalidated": cache_invalidated,
            "incremental_strict": False,
        }
        marker_path = fallback / TRANSPORT_FILE
        marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(marker_path, 0o600)
        return fallback

    def doctor(self) -> list[str]:
        if not self._incremental_mode():
            return super().doctor()
        # Validate normal VMware snapshot/export capabilities using the proven
        # fallback mode. These remain useful even when strict native mode is
        # selected because operators can explicitly switch modes during repair.
        problems = self._fallback_adapter().doctor()
        try:
            caps = self._provider().capabilities(env=self._govc_env())
        except RuntimeError as exc:
            problems.append(str(exc))
            return problems
        if caps.get("available"):
            return problems

        reason = str(caps.get("reason") or "provider_state_ambiguous")
        if not self._capability_fallback_allowed(reason):
            if self._strict():
                problems.append(
                    "native VMware incremental strict mode is enabled; provider is unavailable and no fallback is permitted: "
                    + reason
                )
            else:
                problems.append(
                    "native VMware incremental provider state is unsafe or ambiguous; automatic fallback is denied: "
                    + reason
                )
        return problems

    def platform_info(self) -> dict[str, Any]:
        info = super().platform_info()
        if self._incremental_mode():
            try:
                caps = self._provider().capabilities(env=self._govc_env())
            except Exception as exc:
                caps = {"available": False, "reason": str(exc)}
            reason = str(caps.get("reason") or "provider_state_ambiguous")
            fallback = None
            if not caps.get("available") and self._capability_fallback_allowed(reason):
                fallback = "hot-clone-export"
            info["backup_transport"] = {
                "requested": self.cfg.mode,
                "incremental_strict": self._strict(),
                "native_incremental": caps,
                "fallback": fallback,
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
        if not caps.get("available"):
            reason = str(caps.get("reason") or "provider_state_ambiguous")
            if dry_run and self._capability_fallback_allowed(reason):
                return self._fallback_adapter().export(vm, destination, dry_run=True)
            if dry_run:
                policy = "incremental_strict=true" if self._strict() else "unsafe/ambiguous provider capability state"
                raise RuntimeError(
                    f"native VMware incremental backup would fail closed ({policy}); reason={reason}; no fallback permitted"
                )
            if not self._capability_fallback_allowed(reason):
                policy = "incremental_strict=true" if self._strict() else "unsafe/ambiguous provider capability state"
                raise RuntimeError(
                    f"native VMware incremental backup failed closed ({policy}); reason={reason}; no fallback was attempted"
                )
            return self._full_fallback(
                vm,
                destination,
                reason=reason,
                error=str(caps.get("error") or reason),
                cache_invalidated=False,
            )

        if dry_run:
            return target

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
            # Once the provider has entered backup it may already have touched
            # cached blocks. A non-success result cannot prove the cache is one
            # consistent VMware point in time, so always invalidate it.
            shutil.rmtree(cache, ignore_errors=True)
            if not self._provider_fallback_allowed(exc):
                reason = str(exc.reason or "provider_state_ambiguous")
                policy = "incremental_strict=true" if self._strict() else "unsafe/ambiguous provider state"
                raise RuntimeError(
                    f"native VMware incremental backup failed closed ({policy}); reason={reason}; no fallback was attempted: {exc}"
                ) from exc
            return self._full_fallback(
                vm,
                destination,
                reason=str(exc.reason),
                error=str(exc),
                cache_invalidated=True,
            )
        except Exception as exc:
            # Unknown exceptions are ambiguous by definition and must never be
            # silently converted into a full backup.
            shutil.rmtree(cache, ignore_errors=True)
            raise RuntimeError(
                f"native VMware incremental provider failed ambiguously; backup failed closed and no fallback was attempted: {exc}"
            ) from exc

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
