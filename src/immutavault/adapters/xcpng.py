from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import shlex
import shutil
from typing import Any

from .base import Adapter, VM
from immutavault.runner import run
from immutavault.util import safe_component


class XCPNGAdapter(Adapter):
    def _ssh(self) -> list[str]:
        target = f"{self.cfg.ssh_user}@{self.cfg.endpoint}" if self.cfg.ssh_user else self.cfg.endpoint
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        key = os.getenv("IMMUTAVAULT_SSH_KEY")
        if key:
            cmd += ["-i", key]
        return cmd + [target]

    def _scp(self) -> list[str]:
        cmd = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        key = os.getenv("IMMUTAVAULT_SSH_KEY")
        if key:
            cmd += ["-i", key]
        return cmd

    def doctor(self) -> list[str]:
        if shutil.which("ssh") is None or shutil.which("scp") is None:
            return ["ssh/scp is not installed"]
        result = run(self._ssh() + ["xe host-list params=uuid --minimal"], timeout=30, check=False)
        problems = [] if result.returncode == 0 else [f"cannot reach XCP-ng xe CLI: {result.stderr.strip()}"]
        if result.returncode == 0:
            capabilities = run(
                self._ssh() + [
                    "for c in vm-snapshot snapshot-export-to-template snapshot-uninstall vm-import vm-install template-uninstall; "
                    "do xe help \"$c\" >/dev/null || exit 42; done"
                ],
                timeout=60, check=False,
            )
            if capabilities.returncode != 0:
                problems.append("XCP-ng xe CLI is missing one or more required snapshot/export/import/template commands")
        if self.cfg.mode.lower() in {"xe-export", "cold-export"}:
            problems.append("XCP-ng direct xe vm-export is configured; use mode=snapshot-export for running production VMs")
        elif self.cfg.mode.lower() not in {"snapshot-export", "xe-snapshot-export", "snapshot"}:
            problems.append(f"unsupported XCP-ng backup mode: {self.cfg.mode}")
        return problems

    def platform_info(self) -> dict[str, Any]:
        host = run(self._ssh() + ["xe host-list params=uuid --minimal"], timeout=60).stdout.strip().split(",")[0]
        version = run(self._ssh() + [f"xe host-param-get uuid={shlex.quote(host)} param-name=software-version param-key=product_version"], timeout=60, check=False).stdout.strip()
        build = run(self._ssh() + [f"xe host-param-get uuid={shlex.quote(host)} param-name=software-version param-key=build_number"], timeout=60, check=False).stdout.strip()
        return {"name": self.cfg.name, "type": "xcpng", "endpoint": self.cfg.endpoint, "host_uuid": host, "version": version, "build": build}

    def inventory(self) -> list[VM]:
        result = run(self._ssh() + ["xe vm-list is-control-domain=false params=uuid --minimal"], timeout=self.timeout)
        uuids = [x.strip() for x in result.stdout.strip().split(",") if x.strip()]
        vms: list[VM] = []
        for uuid in uuids:
            name = run(self._ssh() + [f"xe vm-param-get uuid={shlex.quote(uuid)} param-name=name-label"], timeout=60).stdout.strip()
            state = run(self._ssh() + [f"xe vm-param-get uuid={shlex.quote(uuid)} param-name=power-state"], timeout=60).stdout.strip()
            if self._selected(name):
                vms.append(VM(id=uuid, name=name, power_state=state, kind="xcpng"))
        return vms

    def _selected(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in self.cfg.include) and not any(fnmatch.fnmatch(name, p) for p in self.cfg.exclude)

    def export(self, vm: VM, destination: Path, *, dry_run: bool = False) -> Path:
        """Export from a storage snapshot so a running VM is not powered off."""
        out = destination / f"xcpng-{safe_component(vm.id)}-{safe_component(vm.name)}.xva"
        if dry_run:
            return out
        remote = f"/var/tmp/immutavault-{safe_component(vm.id)}.xva"
        target = self._ssh()[-1]
        mode = self.cfg.mode.lower()
        snapshot_uuid = ""
        try:
            if mode in {"snapshot-export", "xe-snapshot-export", "snapshot"}:
                result = run(
                    self._ssh() + [
                        f"xe vm-snapshot uuid={shlex.quote(vm.id)} "
                        f"new-name-label={shlex.quote('immutavault-' + safe_component(vm.name))}"
                    ],
                    timeout=self.timeout,
                )
                snapshot_uuid = result.stdout.strip()
                if not snapshot_uuid:
                    raise RuntimeError(f"XCP-ng snapshot returned no UUID for {vm.name}")
                run(
                    self._ssh() + [
                        f"xe snapshot-export-to-template snapshot-uuid={shlex.quote(snapshot_uuid)} "
                        f"filename={shlex.quote(remote)} preserve-power-state=false"
                    ],
                    timeout=self.timeout,
                )
            elif mode in {"xe-export", "cold-export"}:
                run(self._ssh() + [f"xe vm-export uuid={shlex.quote(vm.id)} filename={shlex.quote(remote)}"], timeout=self.timeout)
            else:
                raise ValueError(f"unsupported XCP-ng backup mode: {self.cfg.mode}")
            run(self._scp() + [f"{target}:{remote}", str(out)], timeout=self.timeout)
            return out
        finally:
            run(self._ssh() + [f"rm -f {shlex.quote(remote)}"], timeout=300, check=False)
            if snapshot_uuid:
                run(
                    self._ssh() + [f"xe snapshot-uninstall snapshot-uuid={shlex.quote(snapshot_uuid)} --force"],
                    timeout=self.timeout, check=False,
                )

    def restore(self, source: Path, *, target_name: str, options: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        """Restore an XVA as a new VM without overwriting an existing workload.

        Snapshot-mode backups are exported with XCP-ng's supported
        ``snapshot-export-to-template`` command. Importing that XVA therefore yields a
        template, not a directly bootable VM. We detect that case, instantiate a new VM
        with ``vm-install``, then remove the temporary imported template. Historical XVA
        backups that import directly as VMs remain supported as well.
        """
        xvas = sorted(source.rglob("*.xva"))
        if not xvas:
            raise RuntimeError("no XVA archive found in XCP-ng recovery point")
        xva = xvas[0]
        remote = f"/var/tmp/immutavault-restore-{safe_component(xva.name)}"
        target = self._ssh()[-1]
        sr_uuid = str(options.get("sr_uuid") or self.cfg.options.get("restore_sr_uuid") or "").strip()
        import_cmd = f"xe vm-import filename={shlex.quote(remote)} preserve=false"
        if sr_uuid:
            import_cmd += f" sr-uuid={shlex.quote(sr_uuid)}"
        if dry_run:
            install_cmd = f"xe vm-install template-uuid=<imported-template-uuid> new-name-label={shlex.quote(target_name)}"
            if sr_uuid:
                install_cmd += f" sr-uuid={shlex.quote(sr_uuid)}"
            return {
                "platform": self.cfg.name,
                "name": target_name,
                "xva": str(xva),
                "import_command": import_cmd,
                "template_install_command": install_cmd,
            }
        existing = run(self._ssh() + [f"xe vm-list name-label={shlex.quote(target_name)} --minimal"], timeout=60, check=False)
        if existing.returncode == 0 and existing.stdout.strip():
            raise RuntimeError(f"VM {target_name!r} already exists; Immutavault refuses overwrite")
        run(self._scp() + [str(xva), f"{target}:{remote}"], timeout=self.timeout)
        imported_uuid = ""
        imported_template = False
        uuid = ""
        try:
            result = run(self._ssh() + [import_cmd], timeout=self.timeout)
            imported_uuid = result.stdout.strip()
            if not imported_uuid:
                raise RuntimeError("XCP-ng vm-import returned no UUID")
            kind = run(
                self._ssh() + [f"xe vm-param-get uuid={shlex.quote(imported_uuid)} param-name=is-a-template"],
                timeout=60,
            ).stdout.strip().lower()
            imported_template = kind == "true"
            if imported_template:
                install_cmd = (
                    f"xe vm-install template-uuid={shlex.quote(imported_uuid)} "
                    f"new-name-label={shlex.quote(target_name)}"
                )
                if sr_uuid:
                    install_cmd += f" sr-uuid={shlex.quote(sr_uuid)}"
                installed = run(self._ssh() + [install_cmd], timeout=self.timeout)
                uuid = installed.stdout.strip()
                if not uuid:
                    raise RuntimeError("XCP-ng vm-install returned no VM UUID")
            else:
                uuid = imported_uuid
                run(
                    self._ssh() + [f"xe vm-param-set uuid={shlex.quote(uuid)} name-label={shlex.quote(target_name)}"],
                    timeout=60,
                )
        finally:
            run(self._ssh() + [f"rm -f {shlex.quote(remote)}"], timeout=300, check=False)
            if imported_template and imported_uuid:
                run(
                    self._ssh() + [f"xe template-uninstall template-uuid={shlex.quote(imported_uuid)} --force"],
                    timeout=self.timeout,
                    check=False,
                )
        return {"platform": self.cfg.name, "uuid": uuid, "name": target_name}

    def power_on(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        uuid = str(restored.get("uuid") or "")
        if not uuid:
            raise ValueError("XCP-ng restored result is missing uuid")
        remote = f"xe vm-start uuid={shlex.quote(uuid)}"
        if not dry_run:
            run(self._ssh() + [remote], timeout=self.timeout)
        return {"platform": self.cfg.name, "uuid": uuid, "power": "on", "command": remote if dry_run else None}

    def power_off(self, restored: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        uuid = str(restored.get("uuid") or "")
        if not uuid:
            raise ValueError("XCP-ng restored result is missing uuid")
        remote = f"xe vm-shutdown uuid={shlex.quote(uuid)} force=true"
        if not dry_run:
            run(self._ssh() + [remote], timeout=self.timeout, check=False)
        return {"platform": self.cfg.name, "uuid": uuid, "power": "off", "command": remote if dry_run else None}
