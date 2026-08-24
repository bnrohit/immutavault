from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any

from .base import Adapter, VM
from immutavault.runner import run
from immutavault.util import safe_component


class VMwareAdapter(Adapter):
    """VMware/vCenter adapter using govc with per-platform credentials.

    Credentials are resolved independently for every configured vCenter so a
    primary and DR vCenter can safely coexist in the same Immutavault process.
    """

    def _govc_env(self) -> dict[str, str]:
        opts = self.cfg.options
        username_env = str(opts.get("username_env", "GOVC_USERNAME"))
        password_env = str(opts.get("password_env", "GOVC_PASSWORD"))
        username = os.getenv(username_env)
        password = os.getenv(password_env)
        if not username:
            raise RuntimeError(f"{username_env} is not set for VMware platform {self.cfg.name}")
        if not password:
            raise RuntimeError(f"{password_env} is not set for VMware platform {self.cfg.name}")
        env = {
            "GOVC_URL": self.cfg.endpoint,
            "GOVC_USERNAME": username,
            "GOVC_PASSWORD": password,
            "GOVC_INSECURE": "true" if bool(opts.get("insecure", False)) else "false",
        }
        mapping = {
            "tls_ca_certs": "GOVC_TLS_CA_CERTS",
            "tls_known_hosts": "GOVC_TLS_KNOWN_HOSTS",
            "datacenter": "GOVC_DATACENTER",
            "datastore": "GOVC_DATASTORE",
            "network": "GOVC_NETWORK",
            "resource_pool": "GOVC_RESOURCE_POOL",
            "host": "GOVC_HOST",
        }
        for key, env_name in mapping.items():
            value = opts.get(key)
            if value not in (None, ""):
                env[env_name] = str(value)
        return env

    def doctor(self) -> list[str]:
        problems: list[str] = []
        if shutil.which("govc") is None:
            problems.append("govc is not installed")
            return problems
        try:
            env = self._govc_env()
        except RuntimeError as exc:
            problems.append(str(exc))
            return problems
        if bool(self.cfg.options.get("insecure", False)):
            problems.append("VMware TLS verification is disabled (options.insecure=true); use a trusted CA/known-hosts file for production")
        mode = self.cfg.mode.lower()
        if mode in {"cbt", "vddk"}:
            problems.append("VDDK/CBT transport is selected but no tested VDDK transport plugin is installed")
        elif mode in {"export", "cold-export"}:
            problems.append("VMware cold OVF export requires protected VMs to be powered off; use mode=hot-clone-export for no planned VM power-off")
        elif mode not in {"hot-clone-export", "snapshot-clone-export", "hot"}:
            problems.append(f"unsupported VMware backup mode: {self.cfg.mode}")
        probe = run(["govc", "about", "-json"], timeout=60, env=env, check=False)
        if probe.returncode != 0:
            problems.append(f"cannot reach VMware platform {self.cfg.name}: {probe.stderr.strip()}")
        return problems

    def platform_info(self) -> dict[str, Any]:
        result = run(["govc", "about", "-json"], timeout=60, env=self._govc_env())
        data = json.loads(result.stdout or "{}")
        return {"name": self.cfg.name, "type": "vmware", "endpoint": self.cfg.endpoint, "about": data}

    def inventory(self) -> list[VM]:
        result = run(["govc", "find", "/", "-type", "m", "-json"], timeout=self.timeout, env=self._govc_env())
        data = json.loads(result.stdout or "{}")
        paths = data.get("elements") or []
        vms: list[VM] = []
        for item in paths:
            path = item.get("path") if isinstance(item, dict) else str(item)
            if not path:
                continue
            name = path.rsplit("/", 1)[-1]
            if self._selected(name):
                vms.append(VM(id=path, name=name, kind="vmware"))
        return vms

    def _selected(self, name: str) -> bool:
        included = any(fnmatch.fnmatch(name, pat) for pat in self.cfg.include)
        excluded = any(fnmatch.fnmatch(name, pat) for pat in self.cfg.exclude)
        return included and not excluded

    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        """Export a recoverable VM package.

        `hot-clone-export` keeps the protected VM running: create a short-lived
        snapshot, clone that point-in-time state to a powered-off temporary VM,
        export the clone, then remove the clone and consolidate the snapshot.
        The legacy `export` mode is retained only for already-powered-off VMs.
        """
        out = destination / safe_component(vm.name)
        if dry_run:
            return out
        mode = self.cfg.mode.lower()
        if mode in {"cbt", "vddk"}:
            raise RuntimeError("VDDK/CBT mode requires a tested transport plugin; this build refuses to pretend OVF export is CBT")
        if mode in {"export", "cold-export"}:
            out.mkdir(parents=True, exist_ok=True)
            run(["govc", "export.ovf", "-vm", vm.id, str(out)], timeout=self.timeout, env=self._govc_env())
            return out
        if mode not in {"hot-clone-export", "snapshot-clone-export", "hot"}:
            raise ValueError(f"unsupported VMware backup mode: {self.cfg.mode}")

        out.mkdir(parents=True, exist_ok=True)
        env = self._govc_env()
        nonce = uuid.uuid4().hex[:12]
        snap = f"immutavault-{nonce}"
        clone = f"immutavault-{safe_component(vm.name)[:48]}-{nonce}"
        quiesce = bool(self.cfg.options.get("quiesce", True))
        fallback = bool(self.cfg.options.get("quiesce_fallback_crash_consistent", False))
        snapshot_created = False
        clone_created = False
        try:
            snap_cmd = ["govc", "snapshot.create", "-vm", vm.id, "-m=false", f"-q={'true' if quiesce else 'false'}", "-d", "Immutavault point-in-time backup", snap]
            result = run(snap_cmd, timeout=self.timeout, env=env, check=False)
            if result.returncode != 0 and quiesce and fallback:
                snap_cmd = ["govc", "snapshot.create", "-vm", vm.id, "-m=false", "-q=false", "-d", "Immutavault crash-consistent point-in-time backup", snap]
                result = run(snap_cmd, timeout=self.timeout, env=env, check=False)
            if result.returncode != 0:
                raise RuntimeError(f"VMware snapshot failed for {vm.name}: {result.stderr.strip()}")
            snapshot_created = True

            clone_cmd = ["govc", "vm.clone", "-vm", vm.id, "-snapshot", snap, "-on=false", clone]
            run(clone_cmd, timeout=self.timeout, env=env)
            clone_created = True
            run(["govc", "export.ovf", "-vm", clone, str(out)], timeout=self.timeout, env=env)
            return out
        finally:
            active_error = sys.exc_info()[0] is not None
            cleanup_errors: list[str] = []
            if clone_created:
                cleanup = run(["govc", "vm.destroy", clone], timeout=self.timeout, env=env, check=False)
                if cleanup.returncode != 0:
                    cleanup_errors.append(f"temporary clone cleanup failed: {cleanup.stderr.strip()}")
            if snapshot_created:
                # Consolidate the source disks after the temporary point-in-time
                # snapshot is no longer needed. Cleanup is attempted even when export fails.
                cleanup = run(["govc", "snapshot.remove", "-vm", vm.id, "-c=true", snap], timeout=self.timeout, env=env, check=False)
                if cleanup.returncode != 0:
                    cleanup_errors.append(f"source snapshot cleanup/consolidation failed: {cleanup.stderr.strip()}")
            if cleanup_errors and not active_error:
                raise RuntimeError("VMware backup exported data but cleanup requires operator attention: " + "; ".join(cleanup_errors))

    def restore(self, source: Path, *, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        ovfs = sorted(source.rglob("*.ovf"))
        if not ovfs:
            raise RuntimeError("no OVF descriptor found in VMware recovery point")
        ovf = ovfs[0]
        cmd = ["govc", "import.ovf", "-name", target_name]
        if options.get("options_json"):
            cmd += ["-options", str(options["options_json"])]
        cmd.append(str(ovf))
        if dry_run:
            return {"platform": self.cfg.name, "name": target_name, "ovf": str(ovf), "command": cmd}
        env = self._govc_env()
        # Do not overwrite: verify the requested inventory name does not already exist.
        found = run(["govc", "find", "/", "-type", "m", "-name", target_name], timeout=60, env=env, check=False)
        if found.returncode == 0 and found.stdout.strip():
            raise RuntimeError(f"VM {target_name!r} already exists; Immutavault refuses overwrite")
        run(cmd, timeout=self.timeout, env=env)
        return {"platform": self.cfg.name, "name": target_name, "source": str(ovf)}

    def power_on(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        name = str(restored.get("name") or "")
        if not name:
            raise ValueError("VMware restored result is missing name")
        cmd = ["govc", "vm.power", "-on", name]
        if not dry_run:
            run(cmd, timeout=self.timeout, env=self._govc_env())
        return {"platform": self.cfg.name, "name": name, "power": "on", "command": cmd if dry_run else None}

    def power_off(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        name = str(restored.get("name") or "")
        if not name:
            raise ValueError("VMware restored result is missing name")
        cmd = ["govc", "vm.power", "-off", name]
        if not dry_run:
            run(cmd, timeout=self.timeout, env=self._govc_env(), check=False)
        return {"platform": self.cfg.name, "name": name, "power": "off", "command": cmd if dry_run else None}
