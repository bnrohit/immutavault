from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
from typing import Any


_IFNAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


@dataclass(frozen=True)
class HealthCheckConfig:
    type: str = "tcp"  # tcp, http, https, icmp
    host: str | None = None
    port: int | None = None
    url: str | None = None
    timeout_seconds: int = 5
    expected_status: int = 200
    attempts: int = 12
    interval_seconds: int = 10


@dataclass(frozen=True)
class DRGatewayConfig:
    host: str
    ssh_user: str | None = None
    local: bool = False
    underlay_interface: str = "eth0"
    trunk_interface: str = "eth1"
    vtep_ip: str = ""
    router_id: str = ""
    ospf_area: str = "0.0.0.0"
    ospf_cost: int = 10
    ospf_auth_key_env: str | None = None
    ospf_auth_key_id: int = 1


@dataclass(frozen=True)
class DRSiteConfig:
    name: str
    gateway: DRGatewayConfig


@dataclass(frozen=True)
class DRNetworkConfig:
    name: str
    vlan_id: int
    vni: int
    subnet: str
    gateway_cidr: str
    mtu: int = 1450
    enabled: bool = True


@dataclass(frozen=True)
class DRVMConfig:
    name: str
    source_platform: str
    target_platform: str
    boot_order: int = 100
    target_name: str | None = None
    health_checks: list[HealthCheckConfig] = field(default_factory=list)
    restore_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FenceConfig:
    mode: str = "manual"  # manual, command
    command_env: str | None = None
    verify_command_env: str | None = None


@dataclass(frozen=True)
class DRConfig:
    enabled: bool = False
    primary_site: str = "primary"
    dr_site: str = "dr"
    replica: str = ""
    rpo_max_minutes: int = 1440
    auto_failover: bool = False
    failure_threshold: int = 5
    check_interval_seconds: int = 60
    # For unattended failover the controller must survive loss of the primary site.
    # Set this to the DR site name or an external/third-site identifier.
    control_plane_site: str = ""
    primary_failure_quorum: int = 0  # 0 means all configured probes must fail
    maintenance_file: str = "/var/lib/immutavault/dr-maintenance"
    primary_probes: list[HealthCheckConfig] = field(default_factory=list)
    fence: FenceConfig = field(default_factory=FenceConfig)
    sites: list[DRSiteConfig] = field(default_factory=list)
    networks: list[DRNetworkConfig] = field(default_factory=list)
    workloads: list[DRVMConfig] = field(default_factory=list)


def _check(item: dict[str, Any]) -> HealthCheckConfig:
    kind = str(item.get("type", "tcp")).lower()
    if kind not in {"tcp", "http", "https", "icmp"}:
        raise ValueError(f"unsupported DR health check type: {kind}")
    port = item.get("port")
    parsed_port = int(port) if port is not None else None
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ValueError("DR health check port must be between 1 and 65535")
    expected_status = int(item.get("expected_status", 200))
    if not 100 <= expected_status <= 599:
        raise ValueError("DR health check expected_status must be between 100 and 599")
    host = str(item.get("host")) if item.get("host") else None
    url = str(item.get("url")) if item.get("url") else None
    if kind in {"tcp", "icmp"} and not host:
        raise ValueError(f"{kind} DR health check requires host")
    if kind == "tcp" and parsed_port is None:
        raise ValueError("tcp DR health check requires port")
    if kind in {"http", "https"} and not (url or host):
        raise ValueError(f"{kind} DR health check requires url or host")
    return HealthCheckConfig(
        type=kind,
        host=host,
        port=parsed_port,
        url=url,
        timeout_seconds=max(1, int(item.get("timeout_seconds", 5))),
        expected_status=expected_status,
        attempts=max(1, int(item.get("attempts", 12))),
        interval_seconds=max(1, int(item.get("interval_seconds", 10))),
    )


def _validate_ip(value: str, label: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid IP address") from exc


def _validate_router_id(value: str, label: str) -> str:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid IPv4 router ID") from exc
    if addr.version != 4:
        raise ValueError(f"{label} must be an IPv4 address")
    return str(addr)


def _validate_ifname(value: str, label: str) -> str:
    if not _IFNAME_RE.fullmatch(value):
        raise ValueError(f"{label} must be a valid Linux interface name (1-15 safe characters)")
    return value


def parse_dr(raw: dict[str, Any] | None) -> DRConfig:
    raw = raw or {}
    enabled = bool(raw.get("enabled", False))
    sites: list[DRSiteConfig] = []
    seen_sites: set[str] = set()
    seen_vteps: set[str] = set()
    seen_router_ids: set[str] = set()
    for item in raw.get("sites", []) or []:
        gateway_raw = item.get("gateway") or {}
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("DR site name is required")
        if name in seen_sites:
            raise ValueError(f"duplicate DR site name: {name}")
        seen_sites.add(name)
        host = str(gateway_raw.get("host") or "").strip()
        if enabled and not host:
            raise ValueError(f"DR site {name} is missing gateway.host")
        vtep_raw = str(gateway_raw.get("vtep_ip", "")).strip()
        router_raw = str(gateway_raw.get("router_id", vtep_raw)).strip()
        if enabled and not vtep_raw:
            raise ValueError(f"DR site {name} is missing gateway.vtep_ip")
        if enabled and not router_raw:
            raise ValueError(f"DR site {name} is missing gateway.router_id")
        vtep = _validate_ip(vtep_raw, f"DR site {name} gateway.vtep_ip") if vtep_raw else ""
        router_id = _validate_router_id(router_raw, f"DR site {name} gateway.router_id") if router_raw else ""
        if vtep:
            if vtep in seen_vteps:
                raise ValueError(f"duplicate DR VTEP address: {vtep}")
            seen_vteps.add(vtep)
        if router_id:
            if router_id in seen_router_ids:
                raise ValueError(f"duplicate DR OSPF router ID: {router_id}")
            seen_router_ids.add(router_id)
        underlay = _validate_ifname(str(gateway_raw.get("underlay_interface", "eth0")), f"DR site {name} underlay_interface")
        trunk = _validate_ifname(str(gateway_raw.get("trunk_interface", "eth1")), f"DR site {name} trunk_interface")
        key_id = int(gateway_raw.get("ospf_auth_key_id", 1))
        if not 1 <= key_id <= 255:
            raise ValueError("DR ospf_auth_key_id must be between 1 and 255")
        ospf_cost = int(gateway_raw.get("ospf_cost", 10))
        if not 1 <= ospf_cost <= 65535:
            raise ValueError("DR ospf_cost must be between 1 and 65535")
        ospf_area = str(gateway_raw.get("ospf_area", "0.0.0.0"))
        # FRR accepts dotted IPv4 or decimal area IDs; validate common dotted form when used.
        if "." in ospf_area:
            _validate_router_id(ospf_area, f"DR site {name} ospf_area")
        else:
            try:
                area_num = int(ospf_area)
            except ValueError as exc:
                raise ValueError(f"DR site {name} ospf_area must be dotted IPv4 or 0-4294967295") from exc
            if not 0 <= area_num <= 4294967295:
                raise ValueError(f"DR site {name} ospf_area must be dotted IPv4 or 0-4294967295")
        sites.append(DRSiteConfig(
            name=name,
            gateway=DRGatewayConfig(
                host=host,
                ssh_user=str(gateway_raw.get("ssh_user")) if gateway_raw.get("ssh_user") else None,
                local=bool(gateway_raw.get("local", False)),
                underlay_interface=underlay,
                trunk_interface=trunk,
                vtep_ip=vtep,
                router_id=router_id,
                ospf_area=ospf_area,
                ospf_cost=ospf_cost,
                ospf_auth_key_env=str(gateway_raw.get("ospf_auth_key_env")) if gateway_raw.get("ospf_auth_key_env") else None,
                ospf_auth_key_id=key_id,
            ),
        ))

    networks: list[DRNetworkConfig] = []
    seen_vni: set[int] = set()
    seen_vlan: set[int] = set()
    seen_network_names: set[str] = set()
    seen_subnets: list[ipaddress._BaseNetwork] = []
    for item in raw.get("networks", []) or []:
        vni = int(item.get("vni", 0))
        vlan = int(item.get("vlan_id", 0))
        if not 1 <= vni <= 16777215:
            raise ValueError("DR VXLAN VNI must be between 1 and 16777215")
        if not 1 <= vlan <= 4094:
            raise ValueError("DR VLAN ID must be between 1 and 4094")
        if vni in seen_vni:
            raise ValueError(f"duplicate DR VXLAN VNI: {vni}")
        if vlan in seen_vlan:
            raise ValueError(f"duplicate DR VLAN ID: {vlan}")
        seen_vni.add(vni)
        seen_vlan.add(vlan)
        name = str(item.get("name") or f"vlan-{vlan}").strip()
        if name in seen_network_names:
            raise ValueError(f"duplicate DR network name: {name}")
        seen_network_names.add(name)
        subnet_raw = str(item.get("subnet") or "").strip()
        gateway_raw = str(item.get("gateway_cidr") or "").strip()
        if not subnet_raw or not gateway_raw:
            raise ValueError(f"DR network {name} requires subnet and gateway_cidr")
        try:
            subnet = ipaddress.ip_network(subnet_raw, strict=True)
            gateway = ipaddress.ip_interface(gateway_raw)
        except ValueError as exc:
            raise ValueError(f"DR network {name} has invalid subnet/gateway_cidr: {exc}") from exc
        if gateway.version != subnet.version or gateway.ip not in subnet:
            raise ValueError(f"DR network {name} gateway_cidr must belong to subnet {subnet}")
        if gateway.network != subnet:
            raise ValueError(f"DR network {name} gateway_cidr prefix must match subnet {subnet}")
        for other in seen_subnets:
            if subnet.overlaps(other):
                raise ValueError(f"DR network {name} subnet {subnet} overlaps configured subnet {other}")
        seen_subnets.append(subnet)
        mtu = int(item.get("mtu", 1450))
        if not 576 <= mtu <= 9216:
            raise ValueError("DR MTU must be between 576 and 9216")
        networks.append(DRNetworkConfig(
            name=name,
            vlan_id=vlan,
            vni=vni,
            subnet=str(subnet),
            gateway_cidr=str(gateway),
            mtu=mtu,
            enabled=bool(item.get("enabled", True)),
        ))

    workloads: list[DRVMConfig] = []
    seen_workloads: set[str] = set()
    for item in raw.get("workloads", []) or []:
        name = str(item.get("name") or "").strip()
        source = str(item.get("source_platform") or "").strip()
        target = str(item.get("target_platform") or "").strip()
        if not name or not source or not target:
            raise ValueError("each DR workload requires name, source_platform, and target_platform")
        if name in seen_workloads:
            raise ValueError(f"duplicate DR workload name: {name}")
        seen_workloads.add(name)
        if source == target:
            raise ValueError(f"DR workload {name} source_platform and target_platform must be different")
        workloads.append(DRVMConfig(
            name=name,
            source_platform=source,
            target_platform=target,
            boot_order=int(item.get("boot_order", 100)),
            target_name=str(item.get("target_name")) if item.get("target_name") else None,
            health_checks=[_check(x) for x in item.get("health_checks", []) or []],
            restore_options=dict(item.get("restore_options") or {}),
        ))

    fence_raw = raw.get("fence") or {}
    fence = FenceConfig(
        mode=str(fence_raw.get("mode", "manual")).lower(),
        command_env=str(fence_raw.get("command_env")) if fence_raw.get("command_env") else None,
        verify_command_env=str(fence_raw.get("verify_command_env")) if fence_raw.get("verify_command_env") else None,
    )
    if fence.mode not in {"manual", "command"}:
        raise ValueError("DR fence.mode must be manual or command")
    if fence.mode == "command" and not fence.command_env:
        raise ValueError("DR fence.command_env is required when fence.mode=command")

    probes = [_check(x) for x in raw.get("primary_probes", []) or []]
    quorum = int(raw.get("primary_failure_quorum", 0))
    if quorum < 0:
        raise ValueError("DR primary_failure_quorum must be >= 0")
    if probes and quorum > len(probes):
        raise ValueError("DR primary_failure_quorum cannot exceed the number of primary_probes")

    cfg = DRConfig(
        enabled=enabled,
        primary_site=str(raw.get("primary_site", "primary")),
        dr_site=str(raw.get("dr_site", "dr")),
        replica=str(raw.get("replica", "")),
        rpo_max_minutes=max(1, int(raw.get("rpo_max_minutes", 1440))),
        auto_failover=bool(raw.get("auto_failover", False)),
        failure_threshold=max(2, int(raw.get("failure_threshold", 5))),
        check_interval_seconds=max(30, int(raw.get("check_interval_seconds", 60))),
        control_plane_site=str(raw.get("control_plane_site", "")).strip(),
        primary_failure_quorum=quorum,
        maintenance_file=str(raw.get("maintenance_file", "/var/lib/immutavault/dr-maintenance")),
        primary_probes=probes,
        fence=fence,
        sites=sites,
        networks=networks,
        workloads=workloads,
    )
    if enabled:
        names = {x.name for x in sites}
        if cfg.primary_site not in names or cfg.dr_site not in names:
            raise ValueError("DR primary_site and dr_site must reference configured sites")
        if cfg.primary_site == cfg.dr_site:
            raise ValueError("DR primary_site and dr_site must be different")
        if not cfg.replica:
            raise ValueError("DR replica is required when disaster_recovery.enabled=true")
        if not cfg.networks:
            raise ValueError("at least one DR network is required when disaster_recovery.enabled=true")
        if not cfg.workloads:
            raise ValueError("at least one DR workload is required when disaster_recovery.enabled=true")
        if cfg.auto_failover:
            if not cfg.control_plane_site:
                raise ValueError("automatic DR failover requires control_plane_site to document where the surviving controller runs")
            if cfg.control_plane_site == cfg.primary_site:
                raise ValueError("automatic DR failover controller cannot reside only at the primary site")
            if not cfg.primary_probes:
                raise ValueError("automatic DR failover requires at least one primary_probe")
            if cfg.fence.mode != "command":
                raise ValueError("automatic DR failover requires fence.mode=command")
            if not cfg.fence.verify_command_env:
                raise ValueError("automatic DR failover requires fence.verify_command_env")
    return cfg
