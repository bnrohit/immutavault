from __future__ import annotations

import json
import re
import shlex
import time
from typing import Any

from .adapters import build_adapter
from .runner import run
from .util import safe_component
from .v2v_engine import CertifiedBackupEngine


UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")
PROXMOX_BRIDGE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class RecoveryTestTarget:
    """Hypervisor-specific post-restore isolation and cleanup.

    The recovery engine creates a new powered-off workload first. This helper
    then verifies and applies an explicitly allow-listed isolated network before
    any power-on occurs.
    """

    def __init__(self, cfg: Any, platform: Any) -> None:
        self.cfg = cfg
        self.platform = platform
        self.adapter = build_adapter(platform, cfg.runtime.command_timeout_seconds)
        self.timeout = cfg.runtime.command_timeout_seconds

    def _vmware_devices(self, name: str) -> list[str]:
        result = run(["govc", "device.ls", "-vm", name], timeout=60, env=self.adapter._govc_env())  # type: ignore[attr-defined]
        devices = []
        for line in result.stdout.splitlines():
            first = line.split(None, 1)[0] if line.strip() else ""
            if first.startswith("ethernet-"):
                devices.append(first)
        if not devices:
            raise RuntimeError("restored VMware VM has no virtual NICs to isolate")
        return devices

    def _xcp_network_uuid(self, network: str) -> str:
        selector = f"uuid={shlex.quote(network)}" if UUID_RE.fullmatch(network) else f"name-label={shlex.quote(network)}"
        result = run(
            self.adapter._ssh() + [f"xe network-list {selector} params=uuid --minimal"],  # type: ignore[attr-defined]
            timeout=60,
        )
        rows = [item.strip() for item in result.stdout.replace("\n", ",").split(",") if item.strip()]
        rows = list(dict.fromkeys(rows))
        if len(rows) != 1:
            raise RuntimeError(f"XCP-ng isolated network {network!r} must resolve to exactly one network UUID")
        return rows[0]

    def validate_network(self, network: str) -> dict[str, Any]:
        network = str(network).strip()
        if not network:
            raise ValueError("isolated recovery network is required")
        if self.platform.type == "vmware":
            env = self.adapter._govc_env()  # type: ignore[attr-defined]
            result = run(["govc", "find", "/", "-type", "n", "-name", network], timeout=60, env=env, check=False)
            if result.returncode != 0 or not result.stdout.strip():
                # Distributed port groups may not appear under the basic Network type.
                result = run(["govc", "ls", "-t", "DistributedVirtualPortgroup", "/*/network/*"], timeout=60, env=env, check=False)
                if result.returncode != 0 or network not in result.stdout:
                    raise RuntimeError(f"VMware isolated network/port group {network!r} was not found")
            return {"platform": self.platform.name, "type": "vmware", "network": network, "exists": True}
        if self.platform.type == "proxmox":
            if not PROXMOX_BRIDGE_RE.fullmatch(network):
                raise ValueError("Proxmox isolated bridge name is invalid")
            result = run(
                self.adapter._ssh() + ["ip -o link show dev " + shlex.quote(network)],  # type: ignore[attr-defined]
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Proxmox isolated bridge {network!r} does not exist on the selected node")
            return {"platform": self.platform.name, "type": "proxmox", "network": network, "exists": True}
        if self.platform.type == "xcpng":
            uuid = self._xcp_network_uuid(network)
            return {"platform": self.platform.name, "type": "xcpng", "network": network, "network_uuid": uuid, "exists": True}
        raise ValueError(f"unsupported recovery-test platform: {self.platform.type}")

    def isolate(self, restored: dict[str, Any], network: str) -> dict[str, Any]:
        validated = self.validate_network(network)
        if self.platform.type == "vmware":
            name = str(restored.get("name") or "")
            if not name:
                raise ValueError("restored VMware result is missing VM name")
            devices = self._vmware_devices(name)
            env = self.adapter._govc_env()  # type: ignore[attr-defined]
            for device in devices:
                run(["govc", "vm.network.change", "-vm", name, "-net", network, device], timeout=120, env=env)
            return {**validated, "nics_remapped": devices}
        if self.platform.type == "proxmox":
            vmid = str(restored.get("vmid") or "")
            kind = str(restored.get("kind") or "qemu")
            if not vmid.isdigit():
                raise ValueError("restored Proxmox result is missing numeric VMID")
            if kind != "qemu":
                raise RuntimeError("v1.1 isolated recovery tests support Proxmox QEMU VMs; LXC test remapping is blocked")
            text = run(self.adapter._ssh() + [f"qm config {shlex.quote(vmid)}"], timeout=60).stdout  # type: ignore[attr-defined]
            changed: list[str] = []
            for line in text.splitlines():
                match = re.match(r"^(net\d+):\s*(.+)$", line.strip())
                if not match:
                    continue
                key, value = match.group(1), match.group(2)
                parts = [part for part in value.split(",") if part]
                parts = [part for part in parts if not part.startswith("bridge=")]
                parts.append("bridge=" + network)
                new_value = ",".join(parts)
                run(self.adapter._ssh() + [f"qm set {shlex.quote(vmid)} --{key} {shlex.quote(new_value)}"], timeout=60)  # type: ignore[attr-defined]
                changed.append(key)
            if not changed:
                raise RuntimeError("restored Proxmox VM has no virtual NICs to isolate")
            return {**validated, "nics_remapped": changed}
        if self.platform.type == "xcpng":
            uuid = str(restored.get("uuid") or "")
            if not UUID_RE.fullmatch(uuid):
                raise ValueError("restored XCP-ng result is missing VM UUID")
            network_uuid = str(validated["network_uuid"])
            result = run(
                self.adapter._ssh() + [f"xe vif-list vm-uuid={shlex.quote(uuid)} params=uuid --minimal"],  # type: ignore[attr-defined]
                timeout=60,
            )
            vifs = [item.strip() for item in result.stdout.replace("\n", ",").split(",") if item.strip()]
            if not vifs:
                raise RuntimeError("restored XCP-ng VM has no VIFs to isolate")
            for vif in vifs:
                run(
                    self.adapter._ssh() + [f"xe vif-move uuid={shlex.quote(vif)} network-uuid={shlex.quote(network_uuid)}"],  # type: ignore[attr-defined]
                    timeout=60,
                )
            return {**validated, "nics_remapped": vifs}
        raise ValueError(f"unsupported recovery-test platform: {self.platform.type}")

    def running(self, restored: dict[str, Any]) -> dict[str, Any]:
        if self.platform.type == "vmware":
            name = str(restored.get("name") or "")
            result = run(["govc", "vm.info", name], timeout=60, env=self.adapter._govc_env(), check=False)  # type: ignore[attr-defined]
            running = result.returncode == 0 and "poweredOn" in result.stdout
            return {"running": running, "detail": result.stdout[-2000:]}
        if self.platform.type == "proxmox":
            vmid = str(restored.get("vmid") or "")
            result = run(self.adapter._ssh() + [f"qm status {shlex.quote(vmid)}"], timeout=60, check=False)  # type: ignore[attr-defined]
            return {"running": result.returncode == 0 and "running" in result.stdout.lower(), "detail": result.stdout.strip()}
        if self.platform.type == "xcpng":
            uuid = str(restored.get("uuid") or "")
            result = run(
                self.adapter._ssh() + [f"xe vm-param-get uuid={shlex.quote(uuid)} param-name=power-state"],  # type: ignore[attr-defined]
                timeout=60,
                check=False,
            )
            return {"running": result.returncode == 0 and result.stdout.strip().lower() == "running", "detail": result.stdout.strip()}
        raise ValueError(f"unsupported recovery-test platform: {self.platform.type}")

    def destroy(self, restored: dict[str, Any]) -> dict[str, Any]:
        if self.platform.type == "vmware":
            name = str(restored.get("name") or "")
            result = run(["govc", "vm.destroy", name], timeout=self.timeout, env=self.adapter._govc_env(), check=False)  # type: ignore[attr-defined]
        elif self.platform.type == "proxmox":
            vmid = str(restored.get("vmid") or "")
            kind = str(restored.get("kind") or "qemu")
            command = f"{'pct' if kind == 'lxc' else 'qm'} destroy {shlex.quote(vmid)} --purge 1"
            result = run(self.adapter._ssh() + [command], timeout=self.timeout, check=False)  # type: ignore[attr-defined]
        elif self.platform.type == "xcpng":
            uuid = str(restored.get("uuid") or "")
            result = run(self.adapter._ssh() + [f"xe vm-uninstall uuid={shlex.quote(uuid)} force=true"], timeout=self.timeout, check=False)  # type: ignore[attr-defined]
        else:
            raise ValueError(f"unsupported recovery-test platform: {self.platform.type}")
        if result.returncode != 0:
            raise RuntimeError("recovery-test cleanup failed: " + (result.stderr or result.stdout).strip())
        return {"destroyed": True, "platform": self.platform.name}


class RecoveryTestManager:
    def __init__(self, cfg: Any, engine: CertifiedBackupEngine | None = None) -> None:
        self.cfg = cfg
        self.engine = engine or CertifiedBackupEngine(cfg)

    def _platform(self, name: str) -> Any:
        return self.engine._platform(name)

    def _allowed_network(self, platform: str, network: str) -> None:
        allowed = tuple(self.cfg.management.dr_test_networks.get(platform, ()))
        if network not in allowed:
            raise PermissionError(
                f"network {network!r} is not registered as an isolated recovery-test network for {platform!r}"
            )

    def request(
        self,
        *,
        snapshot_id: str,
        requester: str,
        target_platform: str,
        network: str,
        source_repository: str = "primary",
        target_name: str | None = None,
    ) -> dict[str, Any]:
        point = self.engine.state.get_point(snapshot_id)
        if not point:
            raise ValueError("unknown recovery point")
        if not point.get("verified"):
            raise RuntimeError("isolated recovery test requires a verified recovery point")
        if point.get("suspicious"):
            raise RuntimeError("isolated recovery test refuses an anomaly-flagged recovery point")
        target = self._platform(target_platform)
        self._allowed_network(target.name, network)
        RecoveryTestTarget(self.cfg, target).validate_network(network)
        options: dict[str, Any] = {
            "source_repository": source_repository,
            "dr_test": {"network": network, "requested_by": requester},
        }
        # VMware can place imported adapters on the isolated port group from the
        # first import operation. The post-restore isolation pass still remaps all NICs.
        if target.type == "vmware":
            options["network"] = network
        request_id = self.engine.request_restore(
            snapshot_id=snapshot_id,
            requester=requester,
            target_platform=target.name,
            target_name=target_name or f"{point['vm_name']}-drtest-{snapshot_id[:8]}",
            options=options,
        )
        self.engine.state.audit(requester, "dr.test.request", "restore_request", str(request_id), {
            "snapshot_id": snapshot_id,
            "target_platform": target.name,
            "isolated_network": network,
            "source_repository": source_repository,
        })
        req = self.engine.state.get_restore_request(request_id) or {}
        return {"request_id": request_id, "status": req.get("status"), "isolated_network": network}

    def execute(self, request_id: int, *, actor: str) -> dict[str, Any]:
        req = self.engine.state.get_restore_request(request_id)
        if not req:
            raise ValueError("restore request not found")
        try:
            options = json.loads(req.get("options_json") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("restore request has invalid options") from exc
        marker = options.get("dr_test") or {}
        if not isinstance(marker, dict) or not marker.get("network"):
            raise ValueError("restore request is not an isolated recovery test")
        network = str(marker["network"])
        target_platform = str(req["target_platform"])
        self._allowed_network(target_platform, network)
        target = self._platform(target_platform)
        helper = RecoveryTestTarget(self.cfg, target)
        helper.validate_network(network)
        restored: dict[str, Any] | None = None
        powered = False
        cleanup: dict[str, Any] = {"attempted": False, "destroyed": False}
        try:
            restored = self.engine.execute_restore(request_id, actor=actor)
            isolation = helper.isolate(restored, network)
            self.engine.state.audit(actor, "dr.test.isolated", "restore_request", str(request_id), isolation)
            self.engine_adapter(target).power_on(restored)
            powered = True
            time.sleep(self.cfg.management.dr_test_boot_seconds)
            boot = helper.running(restored)
            if not boot.get("running"):
                raise RuntimeError("isolated recovery-test VM did not remain powered on during boot validation")
            result = {
                "status": "success",
                "request_id": request_id,
                "target_platform": target_platform,
                "isolated_network": network,
                "boot_validation": boot,
                "restored": restored,
            }
            self.engine.state.audit(actor, "dr.test.boot_verified", "restore_request", str(request_id), {
                "target_platform": target_platform, "isolated_network": network, "boot_validation": boot,
            })
            return result
        except Exception as exc:
            self.engine.state.audit(actor, "dr.test.failed", "restore_request", str(request_id), {"error": str(exc), "isolated_network": network})
            raise
        finally:
            if restored is not None:
                try:
                    self.engine_adapter(target).power_off(restored)
                    powered = False
                except Exception as exc:
                    self.engine.state.audit(actor, "dr.test.poweroff_failed", "restore_request", str(request_id), {"error": str(exc)})
                if self.cfg.management.dr_test_auto_cleanup:
                    cleanup["attempted"] = True
                    try:
                        helper.destroy(restored); cleanup["destroyed"] = True
                        self.engine.state.audit(actor, "dr.test.cleanup", "restore_request", str(request_id), cleanup)
                    except Exception as exc:
                        cleanup["error"] = str(exc)
                        self.engine.state.audit(actor, "dr.test.cleanup_failed", "restore_request", str(request_id), cleanup)

    def engine_adapter(self, platform: Any):
        return build_adapter(platform, self.cfg.runtime.command_timeout_seconds)
