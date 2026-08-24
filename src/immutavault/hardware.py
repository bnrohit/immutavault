from __future__ import annotations

import json
import os
import platform
from pathlib import Path
import shutil
from .runner import run


def _read(path: str) -> str | None:
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _memory_gib() -> float | None:
    value = _read("/proc/meminfo")
    if not value:
        return None
    for line in value.splitlines():
        if line.startswith("MemTotal:"):
            try:
                kib = int(line.split()[1])
                return round(kib / 1024 / 1024, 2)
            except (ValueError, IndexError):
                return None
    return None


def hardware_report() -> dict:
    arch = platform.machine()
    system = platform.system()
    cpu_count = os.cpu_count() or 0
    memory_gib = _memory_gib()
    report = {
        "architecture": arch,
        "os": platform.platform(),
        "vendor": _read("/sys/class/dmi/id/sys_vendor"),
        "model": _read("/sys/class/dmi/id/product_name"),
        "serial": _read("/sys/class/dmi/id/product_serial"),
        "cpu_logical_count": cpu_count,
        "memory_gib": memory_gib,
        "supported_design": system == "Linux" and arch in {"x86_64", "amd64", "aarch64"},
        "block_devices": [],
        "network_interfaces": [],
        "warnings": [],
        "assessment": {},
    }
    if shutil.which("lsblk"):
        result = run(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,ROTA"], timeout=30, check=False)
        if result.returncode == 0:
            report["block_devices"] = json.loads(result.stdout or "{}").get("blockdevices", [])
    else:
        report["warnings"].append("lsblk not installed")
    if shutil.which("ip"):
        result = run(["ip", "-j", "link", "show"], timeout=30, check=False)
        if result.returncode == 0:
            try:
                report["network_interfaces"] = [
                    {"name": x.get("ifname"), "state": x.get("operstate"), "mtu": x.get("mtu")}
                    for x in json.loads(result.stdout or "[]") if x.get("ifname") != "lo"
                ]
            except json.JSONDecodeError:
                pass
    if not shutil.which("smartctl"):
        report["warnings"].append("smartmontools not installed; disk SMART health unavailable")

    reasons: list[str] = []
    if system != "Linux":
        reasons.append("vault appliance design expects Linux")
    if arch not in {"x86_64", "amd64", "aarch64"}:
        reasons.append(f"architecture {arch} is outside the supported design set")
    if cpu_count and cpu_count < 4:
        reasons.append("fewer than 4 logical CPUs; suitable only for a small lab")
    if memory_gib is not None and memory_gib < 8:
        reasons.append("less than 8 GiB RAM; increase memory before production use")
    report["assessment"] = {
        "control_plane_suitable": not reasons,
        "minimum_guidance": {"cpu": "4 logical CPUs", "memory": "8 GiB", "production_memory": "32+ GiB"},
        "notes": reasons or [
            "CPU/RAM are adequate for the Immutavault control plane; backup capacity and throughput depend primarily on attached storage and network bandwidth."
        ],
    }
    return report
