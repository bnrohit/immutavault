from __future__ import annotations

from typing import Any

from . import setup_wizard as base


def _ui(source: str) -> str:
    if "VMware backup transport" not in source:
        source = source.replace(
            '<label>Resource pool<input id="prp"></label>',
            '<label>Resource pool<input id="prp"></label><label>VMware backup transport<select id="pbt"><option value="auto">Automatic incremental + safe full fallback</option><option value="hot-clone-export">Full hot-clone export only</option><option value="vddk-cbt-strict">Strict CBT/VDDK (no fallback)</option></select></label><label>VDDK directory<input id="pvddk" value="/opt/vmware-vix-disklib-distrib"></label><label>vCenter VDDK TLS thumbprint<input id="pthumb" placeholder="AA:BB:CC:..."></label><label>VDDK transport order<input id="ptrans" value="san:hotadd:nbdssl:nbd"></label><label><input id="pcbt" type="checkbox" style="width:auto"> Enable CBT automatically when supported</label><label><input id="pfallback" type="checkbox" checked style="width:auto"> Automatic full fallback if CBT/VDDK is unavailable</label>'
        )
        old = "restore_sr_uuid:$('psr').value}"
        new = "restore_sr_uuid:$('psr').value,backup_transport:$('pbt').value,vddk_libdir:$('pvddk').value,vddk_thumbprint:$('pthumb').value,vddk_transports:$('ptrans').value,cbt_auto_enable:$('pcbt').checked,incremental_fallback:$('pfallback').checked}"
        source = source.replace(old, new)
    return source


class SetupManager(base.SetupManager):
    def _platform(self, body: dict[str, Any]):
        platform, env = super()._platform(body)
        if platform.get("type") != "vmware":
            return platform, env
        requested = str(body.get("backup_transport") or "auto").strip().lower()
        allowed = {"auto", "hot-clone-export", "vddk-cbt-strict"}
        if requested not in allowed:
            raise ValueError("unsupported VMware backup transport")
        platform["mode"] = requested
        options = dict(platform.get("options") or {})
        options.update({
            "cbt_auto_enable": bool(body.get("cbt_auto_enable", False)),
            "incremental_fallback": bool(body.get("incremental_fallback", True)),
            "cbt_max_chain_length": 32,
            "vddk_libdir": str(body.get("vddk_libdir") or "/opt/vmware-vix-disklib-distrib").strip(),
            "vddk_transports": str(body.get("vddk_transports") or "san:hotadd:nbdssl:nbd").strip(),
        })
        thumbprint = str(body.get("vddk_thumbprint") or "").strip()
        if thumbprint:
            options["vddk_thumbprint"] = thumbprint
        platform["options"] = options
        return platform, env


UI = _ui(base.UI)
base.UI = UI
base.SetupManager = SetupManager


def main(argv: list[str] | None = None) -> int:
    return base.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
