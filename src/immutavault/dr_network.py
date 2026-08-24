from __future__ import annotations

import ipaddress
import os
import shlex
from typing import Any

from .dr_config import DRConfig, DRSiteConfig
from .runner import run


class DRNetworkManager:
    """Builds a two-site VXLAN stretch and FRR OSPF handoff on dedicated Linux gateways."""

    def __init__(self, cfg: DRConfig, timeout: int = 300) -> None:
        self.cfg = cfg
        self.timeout = timeout

    def site(self, name: str) -> DRSiteConfig:
        for site in self.cfg.sites:
            if site.name == name:
                return site
        raise ValueError(f"unknown DR site: {name}")

    def peer(self, name: str) -> DRSiteConfig:
        if name == self.cfg.primary_site:
            return self.site(self.cfg.dr_site)
        if name == self.cfg.dr_site:
            return self.site(self.cfg.primary_site)
        raise ValueError(f"site {name} is not part of the configured DR pair")

    @staticmethod
    def bridge_name(vlan: int) -> str:
        return f"imvb{vlan}"

    @staticmethod
    def vxlan_name(vni: int) -> str:
        return f"imvx{vni}"

    @staticmethod
    def vlan_name(vlan: int) -> str:
        return f"imva{vlan}"

    def _wrap(self, site: DRSiteConfig) -> list[str]:
        g = site.gateway
        if g.local:
            return ["bash", "-s"]
        target = f"{g.ssh_user}@{g.host}" if g.ssh_user else g.host
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
        key = os.getenv("IMMUTAVAULT_SSH_KEY")
        if key:
            cmd += ["-i", key]
        # Send gateway commands on stdin rather than in argv. This prevents OSPF
        # authentication material from appearing in local process listings.
        return cmd + [target, "bash", "-s"]

    def _run(self, site: DRSiteConfig, script: str, *, check: bool = True) -> Any:
        return run(self._wrap(site), timeout=self.timeout, check=check, input_text=script + "\n")

    @staticmethod
    def _vtysh(commands: list[str]) -> str:
        return "vtysh " + " ".join(f"-c {shlex.quote(cmd)}" for cmd in commands)

    def preflight(self, site_name: str) -> dict[str, Any]:
        """Read-only DR gateway validation used before any fencing or failover."""
        site = self.site(site_name)
        peer = self.peer(site_name)
        g = site.gateway
        commands = [
            "command -v ip",
            "command -v bridge",
            "command -v vtysh",
            f"ip link show {shlex.quote(g.underlay_interface)} >/dev/null",
            f"ip link show {shlex.quote(g.trunk_interface)} >/dev/null",
            f"ip route get {shlex.quote(peer.gateway.vtep_ip)} >/dev/null",
            "vtysh -c 'show version' >/dev/null",
        ]
        result = self._run(site, "set -euo pipefail; " + "; ".join(commands), check=False)
        return {
            "site": site_name,
            "peer": peer.name,
            "ok": result.returncode == 0,
            "error": result.stderr.strip()[-2000:],
        }

    def plan(self, site_name: str) -> dict[str, Any]:
        site = self.site(site_name); peer = self.peer(site_name); g = site.gateway
        commands: list[str] = []
        for net in self.cfg.networks:
            if not net.enabled:
                continue
            br = self.bridge_name(net.vlan_id); vx = self.vxlan_name(net.vni); vlanif = self.vlan_name(net.vlan_id)
            commands += [
                f"ip link show {br} >/dev/null 2>&1 || ip link add {br} type bridge",
                f"ip link show {vlanif} >/dev/null 2>&1 || ip link add link {g.trunk_interface} name {vlanif} type vlan id {net.vlan_id}",
                f"ip link show {vx} >/dev/null 2>&1 || ip link add {vx} type vxlan id {net.vni} local {g.vtep_ip} remote {peer.gateway.vtep_ip} dev {g.underlay_interface} dstport 4789 nolearning",
                f"ip link set {vlanif} master {br}", f"ip link set {vx} master {br}",
                f"ip link set {vlanif} mtu {net.mtu}", f"ip link set {vx} mtu {net.mtu}", f"ip link set {br} mtu {net.mtu}",
                f"ip link set {vlanif} up", f"ip link set {vx} up", f"ip link set {br} up",
            ]
        return {"site": site.name, "peer": peer.name, "vtep": g.vtep_ip, "peer_vtep": peer.gateway.vtep_ip, "commands": commands}

    def prepare(self, site_name: str, *, dry_run: bool = False) -> dict[str, Any]:
        site = self.site(site_name); peer = self.peer(site_name); g = site.gateway
        if dry_run:
            return self.plan(site_name)
        checks = self._run(site, "command -v ip && command -v vtysh && command -v bridge", check=False)
        if checks.returncode != 0:
            raise RuntimeError(f"DR gateway {site.name} requires iproute2, bridge and FRR/vtysh")
        script = ["set -euo pipefail"] + self.plan(site_name)["commands"]
        self._run(site, "; ".join(script))
        auth: list[str] = []
        if g.ospf_auth_key_env:
            secret = os.getenv(g.ospf_auth_key_env)
            if not secret:
                raise RuntimeError(f"missing OSPF auth environment variable {g.ospf_auth_key_env}")
            if len(secret.encode("utf-8")) > 16 or any(ch.isspace() for ch in secret):
                raise RuntimeError("OSPF message-digest key must be a non-whitespace value of at most 16 bytes")
            auth = ["ip ospf authentication message-digest", f"ip ospf message-digest-key {g.ospf_auth_key_id} md5 {secret}"]
        vty = [
            "configure terminal", f"router ospf", f"ospf router-id {g.router_id}", "exit",
            f"interface {g.underlay_interface}", f"ip ospf area {g.ospf_area}", f"ip ospf cost {g.ospf_cost}", *auth, "exit", "end", "write memory",
        ]
        self._run(site, self._vtysh(vty))
        return {"status": "prepared", "site": site.name, "peer": peer.name}

    def activate(self, site_name: str, *, dry_run: bool = False) -> dict[str, Any]:
        site = self.site(site_name); g = site.gateway
        actions: list[str] = []
        for net in self.cfg.networks:
            if not net.enabled:
                continue
            br = self.bridge_name(net.vlan_id)
            ipif = ipaddress.ip_interface(net.gateway_cidr)
            vip = str(ipif.ip)
            actions.append(f"ip address show dev {br} | grep -Fq {shlex.quote(net.gateway_cidr)} || ip address add {shlex.quote(net.gateway_cidr)} dev {br}")
            actions.append(f"command -v arping >/dev/null 2>&1 && arping -U -c 3 -I {br} {vip} >/dev/null 2>&1 || true")
            vty = ["configure terminal", f"interface {br}", f"ip ospf area {g.ospf_area}", "ip ospf passive", "exit", "end", "write memory"]
            actions.append(self._vtysh(vty))
        if dry_run:
            return {"site": site_name, "action": "activate", "commands": actions}
        self._run(site, "set -euo pipefail; " + "; ".join(actions))
        return {"status": "active", "site": site_name}

    def deactivate(self, site_name: str, *, dry_run: bool = False, tolerate_unreachable: bool = False) -> dict[str, Any]:
        site = self.site(site_name); g = site.gateway; actions: list[str] = []
        for net in self.cfg.networks:
            if not net.enabled:
                continue
            br = self.bridge_name(net.vlan_id)
            vty = ["configure terminal", f"interface {br}", f"no ip ospf area {g.ospf_area}", "exit", "end", "write memory"]
            actions.append(self._vtysh(vty))
            actions.append(f"ip address del {shlex.quote(net.gateway_cidr)} dev {br} 2>/dev/null || true")
        if dry_run:
            return {"site": site_name, "action": "deactivate", "commands": actions}
        result = self._run(site, "set -u; " + "; ".join(actions), check=not tolerate_unreachable)
        return {"status": "inactive" if result.returncode == 0 else "unreachable", "site": site_name}

    def status(self, site_name: str) -> dict[str, Any]:
        site = self.site(site_name)
        result = self._run(site, "ip -j address show; echo __OSPF__; vtysh -c 'show ip ospf neighbor json'", check=False)
        return {"site": site_name, "reachable": result.returncode == 0, "output": result.stdout[-20000:], "error": result.stderr[-2000:]}
