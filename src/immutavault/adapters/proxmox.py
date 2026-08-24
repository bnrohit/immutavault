from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import shlex
import shutil
from typing import Any

from .base import Adapter, VM
from immutavault.runner import run
from immutavault.util import safe_component


class ProxmoxAdapter(Adapter):
    def _key(self) -> str | None:
        env_name = str(self.cfg.options.get("ssh_key_env") or "IMMUTAVAULT_SSH_KEY")
        return os.getenv(env_name) or None

    def _ssh(self) -> list[str]:
        target = f"{self.cfg.ssh_user}@{self.cfg.endpoint}" if self.cfg.ssh_user else self.cfg.endpoint
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        key = self._key()
        if key:
            cmd += ["-i", key]
        return cmd + [target]

    def _scp(self) -> list[str]:
        cmd = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        key = self._key()
        if key:
            cmd += ["-i", key]
        return cmd

    def doctor(self) -> list[str]:
        problems: list[str] = []
        if shutil.which("ssh") is None or shutil.which("scp") is None:
            problems.append("ssh/scp is not installed")
            return problems
        result = run(self._ssh() + ["pvesh get /version --output-format json"], timeout=30, check=False)
        if result.returncode != 0:
            problems.append(f"cannot reach Proxmox API over SSH: {result.stderr.strip()}")
            return problems
        capabilities = run(
            self._ssh() + ["for c in pvesh vzdump qmrestore pct qm; do command -v \"$c\" >/dev/null || exit 41; done"],
            timeout=30, check=False,
        )
        if capabilities.returncode != 0:
            problems.append("Proxmox node is missing one or more required commands: pvesh, vzdump, qmrestore, pct, qm")
        return problems

    def platform_info(self) -> dict[str, Any]:
        result = run(self._ssh() + ["pvesh get /version --output-format json"], timeout=60)
        data = json.loads(result.stdout or "{}")
        return {"name": self.cfg.name, "type": "proxmox", "endpoint": self.cfg.endpoint, "version": data}

    def inventory(self) -> list[VM]:
        result = run(self._ssh() + ["pvesh get /cluster/resources --type vm --output-format json"], timeout=self.timeout)
        rows = json.loads(result.stdout or "[]")
        vms: list[VM] = []
        for row in rows:
            name = str(row.get("name") or row.get("vmid"))
            if self._selected(name):
                vms.append(VM(
                    id=str(row["vmid"]), name=name, power_state=str(row.get("status", "unknown")),
                    kind=str(row.get("type", "qemu")),
                ))
        return vms

    def _selected(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in self.cfg.include) and not any(
            fnmatch.fnmatch(name, p) for p in self.cfg.exclude
        )

    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        out = destination / f"proxmox-{safe_component(vm.id)}-{safe_component(vm.name)}"
        if dry_run:
            return out
        out.mkdir(parents=True, exist_ok=True)
        remote_dir = f"/var/tmp/immutavault-{safe_component(vm.id)}"
        # Important: leave the VMA stream uncompressed by default so the content-addressed
        # repository can deduplicate VM blocks across recovery points. Set options.compress
        # explicitly if network bandwidth is more important than repository dedupe.
        compress = str(self.cfg.options.get("compress", "0"))
        remote_cmd = (
            f"set -euo pipefail; mkdir -p {shlex.quote(remote_dir)}; "
            f"vzdump {shlex.quote(vm.id)} --mode snapshot --compress {shlex.quote(compress)} "
            f"--dumpdir {shlex.quote(remote_dir)}; "
            f"find {shlex.quote(remote_dir)} -maxdepth 1 -type f -printf '%f\\n'"
        )
        target = self._ssh()[-1]
        try:
            result = run(self._ssh() + [remote_cmd], timeout=self.timeout)
            files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not files:
                raise RuntimeError(f"vzdump produced no files for VM {vm.name}")
            for name in files:
                if Path(name).name != name or name in {".", ".."}:
                    raise RuntimeError(f"vzdump returned an unsafe filename: {name!r}")
                run(self._scp() + [f"{target}:{remote_dir}/{name}", str(out / name)], timeout=self.timeout)
            return out
        finally:
            # Never leave large temporary vzdump archives behind after a failed transfer.
            run(self._ssh() + [f"rm -rf {shlex.quote(remote_dir)}"], timeout=300, check=False)

    def restore(self, source: Path, *, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        files = sorted(p for p in source.rglob("*") if p.is_file() and p.name.startswith("vzdump-"))
        if not files:
            raise RuntimeError("no Proxmox vzdump archive found in recovery point")
        archive = files[0]
        kind = "lxc" if "vzdump-lxc-" in archive.name else "qemu"
        vmid = str(options.get("vmid") or "").strip()
        if not vmid and not dry_run:
            vmid = run(self._ssh() + ["pvesh get /cluster/nextid"], timeout=60).stdout.strip()
        if not vmid:
            vmid = "NEXTID"
        if vmid != "NEXTID" and not vmid.isdigit():
            raise ValueError("Proxmox restore VMID must be numeric")
        remote = f"/var/tmp/immutavault-restore-{safe_component(vmid)}-{safe_component(archive.name)}"
        target = self._ssh()[-1]
        storage = str(options.get("storage") or self.cfg.options.get("restore_storage") or "").strip()
        if kind == "qemu":
            cmd = f"qmrestore {shlex.quote(remote)} {shlex.quote(vmid)}"
            if storage:
                cmd += f" --storage {shlex.quote(storage)}"
        else:
            cmd = f"pct restore {shlex.quote(vmid)} {shlex.quote(remote)}"
            if storage:
                cmd += f" --storage {shlex.quote(storage)}"
        # Rename only after successful import. Never replace an existing VM id automatically.
        rename = (
            f"qm set {shlex.quote(vmid)} --name {shlex.quote(target_name)}" if kind == "qemu"
            else f"pct set {shlex.quote(vmid)} --hostname {shlex.quote(target_name)}"
        )
        if dry_run:
            return {"platform": self.cfg.name, "kind": kind, "vmid": vmid, "archive": str(archive), "command": cmd}
        vmid_re = shlex.quote(vmid)
        exists_cmd = (
            "pvesh get /cluster/resources --type vm --output-format json | "
            f"grep -Eq '\"vmid\"[[:space:]]*:[[:space:]]*{vmid_re}([,}}])'"
        )
        exists = run(self._ssh() + [exists_cmd], timeout=30, check=False)
        if exists.returncode == 0:
            raise RuntimeError(f"target VMID {vmid} already exists; Immutavault refuses overwrite")
        run(self._scp() + [str(archive), f"{target}:{remote}"], timeout=self.timeout)
        try:
            run(self._ssh() + [f"set -euo pipefail; {cmd}; {rename}"], timeout=self.timeout)
        finally:
            run(self._ssh() + [f"rm -f {shlex.quote(remote)}"], timeout=300, check=False)
        return {"platform": self.cfg.name, "kind": kind, "vmid": vmid, "name": target_name}

    def power_on(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        vmid = str(restored.get("vmid") or "")
        kind = str(restored.get("kind") or "qemu")
        if not vmid.isdigit():
            raise ValueError("Proxmox restored result is missing numeric vmid")
        remote = f"{'pct' if kind == 'lxc' else 'qm'} start {shlex.quote(vmid)}"
        if not dry_run:
            run(self._ssh() + [remote], timeout=self.timeout)
        return {"platform": self.cfg.name, "vmid": vmid, "kind": kind, "power": "on", "command": remote if dry_run else None}

    def power_off(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        vmid = str(restored.get("vmid") or "")
        kind = str(restored.get("kind") or "qemu")
        if not vmid.isdigit():
            raise ValueError("Proxmox restored result is missing numeric vmid")
        remote = f"{'pct' if kind == 'lxc' else 'qm'} stop {shlex.quote(vmid)}"
        if not dry_run:
            run(self._ssh() + [remote], timeout=self.timeout, check=False)
        return {"platform": self.cfg.name, "vmid": vmid, "kind": kind, "power": "off", "command": remote if dry_run else None}
