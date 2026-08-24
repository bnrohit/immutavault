from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import tempfile
from typing import Any

import yaml

from .adapters import build_adapter
from .config import load_config
from .engine import BackupEngine
from .storage import s3_preflight


UI = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Immutavault Setup</title><style>
body{font-family:system-ui;margin:auto;max-width:1200px;padding:20px;background:#0b1117;color:#e6edf3}.card{background:#151b23;border:1px solid #30363d;border-radius:12px;padding:16px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px}input,select,button{box-sizing:border-box;padding:9px;border-radius:7px;border:1px solid #444;background:#0d1117;color:#e6edf3}input,select{width:100%}button{cursor:pointer}.ok{color:#7ee787}.muted{color:#8b949e}.danger{border-color:#da3633}pre{white-space:pre-wrap;background:#0d1117;padding:10px;border-radius:8px}.vm{display:block;padding:4px}</style></head><body>
<h1>Immutavault Guided Setup</h1><p class="muted">Add systems, discover VMs, select protection, add storage, plan DR, then test and start.</p>
<div class="card"><label>One-time setup token <input id="token" type="password"></label><button onclick="status()">Connect</button> <span id="st"></span></div>
<div class="card"><h2>1. Hypervisor</h2><div class="grid"><label>Name<input id="pn" placeholder="vc-primary"></label><label>Type<select id="pt"><option value="vmware">VMware/vCenter</option><option value="proxmox">Proxmox</option><option value="xcpng">XCP-ng</option></select></label><label>Endpoint<input id="pe"></label><label>Username<input id="pu"></label><label>Password<input id="pp" type="password"></label><label>SSH user<input id="psu" value="backupsvc"></label><label>SSH key path<input id="pk"></label><label>Datacenter<input id="pdc"></label><label>Datastore / restore storage<input id="pds"></label><label>VMware network<input id="pnet"></label><label>Resource pool<input id="prp"></label><label>XCP-ng restore SR UUID<input id="psr"></label></div><button onclick="ptest()">Test + discover</button> <button onclick="psave()">Save</button><pre id="po"></pre></div>
<div class="card"><h2>2. Select VMs</h2><select id="vp"></select> <button onclick="discover()">Refresh VMs</button><div id="vms"></div><button onclick="vsave()">Save selected VMs</button></div>
<div class="card"><h2>3. Storage / Cloud</h2><div class="grid"><label>Name<input id="sn" placeholder="wasabi-dr"></label><label>Backend<select id="sb"><option value="s3">S3/cloud</option><option value="filesystem">NFS/SMB mounted path</option><option value="rest">Second Immutavault vault</option></select></label><label>Provider<select id="sp"><option>wasabi</option><option>idrive_e2</option><option>backblaze_b2</option><option>cloudflare_r2</option><option>aws</option><option>minio</option><option>ceph</option><option>custom</option></select></label><label>Endpoint / REST URL<input id="se"></label><label>Region<input id="sr"></label><label>Bucket<input id="sbu"></label><label>Prefix<input id="spre" value="prod"></label><label>Mounted path<input id="spath"></label><label>Mount source<input id="sms"></label><label>Access key<input id="sak"></label><label>Secret key<input id="ssk" type="password"></label><label>Repository password<input id="srp" type="password"></label><label>Immutable days<input id="sid" type="number" value="30"></label></div><label><input id="sil" type="checkbox" checked style="width:auto"> provider-side immutability</label><br><button onclick="stest()">Test</button> <button onclick="ssave()">Save</button> <button onclick="sinit()">Initialize</button><pre id="so"></pre></div>
<div class="card"><h2>4. DR site and network</h2><p class="muted">Immutavault only PREPARES VXLAN/FRR/OSPF after explicit confirmation. It does not promote DR here.</p><div class="grid"><label>Primary site<input id="dp" value="main"></label><label>DR site<input id="dd" value="dr-site"></label><label>DR replica<input id="dr"></label><label>Primary gateway host<input id="dph"></label><label>DR gateway host<input id="ddh"></label><label>Primary VTEP IP<input id="dpv"></label><label>DR VTEP IP<input id="ddv"></label><label>Underlay interface<input id="du" value="bond0"></label><label>Trunk interface<input id="dt" value="bond1"></label><label>VLAN<input id="dv" type="number"></label><label>VNI<input id="dni" type="number"></label><label>Subnet<input id="dsn" placeholder="10.14.48.0/21"></label><label>Gateway CIDR<input id="dgw" placeholder="10.14.48.1/21"></label><label>MTU<input id="dm" value="1450"></label></div><button onclick="dsave()">Save DR</button> <button onclick="dplan()">Plan / preflight</button> <button class="danger" onclick="dprepare()">Prepare network</button><pre id="do"></pre></div>
<div class="card"><h2>5. Test and start</h2><button onclick="doctor()">Health check</button> <button onclick="backup(true)">Backup dry-run</button> <button onclick="backup(false)">First real backup</button> <button onclick="timers()">Enable normal schedules</button><pre id="fo"></pre><p class="muted">Automatic DR failover stays OFF until a controlled failover/failback drill is completed.</p></div>
<script>
const $=x=>document.getElementById(x);async function api(p,o={}){o.headers={'Authorization':'Bearer '+$('token').value,'Content-Type':'application/json'};let r=await fetch(p,o),t=await r.text(),j;try{j=JSON.parse(t)}catch{j={error:t}}if(!r.ok)throw Error(j.error||r.statusText);return j}const out=(id,x)=>$(id).textContent=JSON.stringify(x,null,2);
function pb(){return{name:$('pn').value,type:$('pt').value,endpoint:$('pe').value,username:$('pu').value,password:$('pp').value,ssh_user:$('psu').value,ssh_key_path:$('pk').value,datacenter:$('pdc').value,datastore:$('pds').value,network:$('pnet').value,resource_pool:$('prp').value,restore_sr_uuid:$('psr').value}}
async function status(){try{let s=await api('/api/v1/setup/status');$('st').textContent=`Connected: ${s.platforms} hypervisor(s), ${s.replicas} replica(s)`;await plats()}catch(e){$('st').textContent=e.message}}async function plats(){let p=await api('/api/v1/setup/platforms');$('vp').innerHTML=p.map(x=>`<option>${x.name}</option>`).join('')}
function render(v){$('vms').innerHTML=v.map(x=>`<label class="vm"><input class="vc" type="checkbox" value="${x.name}" checked style="width:auto"> ${x.name} (${x.power_state})</label>`).join('')||'No VMs discovered'}
async function ptest(){try{let r=await api('/api/v1/setup/platform/test',{method:'POST',body:JSON.stringify(pb())});out('po',r);render(r.inventory||[])}catch(e){out('po',{error:e.message})}}async function psave(){try{out('po',await api('/api/v1/setup/platform/save',{method:'POST',body:JSON.stringify(pb())}));await plats()}catch(e){out('po',{error:e.message})}}async function discover(){try{let r=await api('/api/v1/setup/platform/discover',{method:'POST',body:JSON.stringify({name:$('vp').value})});render(r.inventory||[])}catch(e){$('vms').textContent=e.message}}async function vsave(){try{let v=[...document.querySelectorAll('.vc:checked')].map(x=>x.value);alert(JSON.stringify(await api('/api/v1/setup/protection/save',{method:'POST',body:JSON.stringify({platform:$('vp').value,vms:v})})))}catch(e){alert(e.message)}}
function sb(){return{name:$('sn').value,backend:$('sb').value,provider:$('sp').value,endpoint:$('se').value,url:$('se').value,region:$('sr').value,bucket:$('sbu').value,prefix:$('spre').value,path:$('spath').value,mount_source:$('sms').value,access_key:$('sak').value,secret_key:$('ssk').value,password:$('srp').value,immutable:$('sil').checked,lock_days:Number($('sid').value||30)}}async function stest(){try{out('so',await api('/api/v1/setup/storage/test',{method:'POST',body:JSON.stringify(sb())}))}catch(e){out('so',{error:e.message})}}async function ssave(){try{out('so',await api('/api/v1/setup/storage/save',{method:'POST',body:JSON.stringify(sb())}))}catch(e){out('so',{error:e.message})}}async function sinit(){try{out('so',await api('/api/v1/setup/storage/init',{method:'POST',body:JSON.stringify({name:$('sn').value})}))}catch(e){out('so',{error:e.message})}}
function db(){return{primary_site:$('dp').value,dr_site:$('dd').value,replica:$('dr').value,primary_gateway_host:$('dph').value,dr_gateway_host:$('ddh').value,primary_vtep:$('dpv').value,dr_vtep:$('ddv').value,underlay_interface:$('du').value,trunk_interface:$('dt').value,vlan_id:Number($('dv').value),vni:Number($('dni').value),subnet:$('dsn').value,gateway_cidr:$('dgw').value,mtu:Number($('dm').value)}}async function dsave(){try{out('do',await api('/api/v1/setup/dr/save',{method:'POST',body:JSON.stringify(db())}))}catch(e){out('do',{error:e.message})}}async function dplan(){try{out('do',await api('/api/v1/setup/dr/plan',{method:'POST',body:JSON.stringify({site:$('dd').value})}))}catch(e){out('do',{error:e.message})}}async function dprepare(){if(prompt('Type APPLY DR NETWORK')!=='APPLY DR NETWORK')return;try{out('do',await api('/api/v1/setup/dr/prepare',{method:'POST',body:JSON.stringify({site:$('dd').value,confirmation:'APPLY DR NETWORK'})}))}catch(e){out('do',{error:e.message})}}
async function doctor(){try{out('fo',await api('/api/v1/setup/doctor',{method:'POST',body:'{}'}))}catch(e){out('fo',{error:e.message})}}async function backup(d){if(!d&&!confirm('Run a REAL backup of selected VMs?'))return;try{out('fo',await api(d?'/api/v1/setup/backup/dry-run':'/api/v1/setup/backup/run',{method:'POST',body:'{}'}))}catch(e){out('fo',{error:e.message})}}async function timers(){if(!confirm('Enable normal backup schedules? DR auto-failover remains off.'))return;try{out('fo',await api('/api/v1/setup/timers/enable',{method:'POST',body:JSON.stringify({confirmation:'ENABLE SCHEDULES'})}))}catch(e){out('fo',{error:e.message})}}
</script></body></html>'''


def _name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").upper()
    if not result:
        raise ValueError("name is required")
    return result


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a mapping")
    return data


def _atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".setup-backup"))
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _env_read(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists(): return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw: continue
        key, value = raw.split("=", 1); key = key.strip()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key): continue
        try:
            parts = shlex.split(value.strip()); result[key] = parts[0] if parts else ""
        except ValueError:
            result[key] = value.strip().strip("'\"")
    return result


def _env_write(path: Path, updates: dict[str, str]) -> None:
    data = _env_read(path); data.update(updates); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"{k}={shlex.quote(v)}" for k, v in sorted(data.items())) + "\n"); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


@contextmanager
def _temporary_env(values: dict[str, str]):
    old = {k: os.environ.get(k) for k in values}; os.environ.update(values)
    try: yield
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v


class SetupManager:
    def __init__(self, config_path: str, env_path: str):
        self.config_path, self.env_path = Path(config_path), Path(env_path)

    def _reload_env(self) -> None:
        os.environ.update(_env_read(self.env_path))

    def status(self) -> dict[str, Any]:
        d = _load(self.config_path)
        return {"platforms": len(d.get("platforms") or []), "replicas": len(d.get("replicas") or []), "dr_enabled": bool((d.get("disaster_recovery") or {}).get("enabled"))}

    def platforms(self) -> list[dict[str, Any]]:
        return [{"name": p.get("name"), "type": p.get("type"), "enabled": bool(p.get("enabled"))} for p in (_load(self.config_path).get("platforms") or [])]

    def _platform(self, b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        name, typ, endpoint = str(b.get("name") or "").strip(), str(b.get("type") or "").strip(), str(b.get("endpoint") or "").strip()
        if typ not in {"vmware", "proxmox", "xcpng"} or not name or not endpoint: raise ValueError("name, endpoint and a supported type are required")
        tag, env = _name(name), {}; p: dict[str, Any] = {"name": name, "type": typ, "enabled": True, "endpoint": endpoint, "include": ["*"], "exclude": []}; o: dict[str, Any] = {}
        if typ == "vmware":
            p["mode"] = "hot-clone-export"; ue, pe = f"IMMUTAVAULT_{tag}_USERNAME", f"IMMUTAVAULT_{tag}_PASSWORD"; o.update({"username_env": ue, "password_env": pe, "insecure": False, "quiesce": True, "quiesce_fallback_crash_consistent": False}); env[ue], env[pe] = str(b.get("username") or ""), str(b.get("password") or "")
            for k in ("datacenter", "datastore", "network", "resource_pool"):
                if str(b.get(k) or "").strip(): o[k] = str(b[k]).strip()
        else:
            p["ssh_user"] = str(b.get("ssh_user") or "backupsvc").strip(); p["mode"] = "vzdump" if typ == "proxmox" else "snapshot-export"; ke = f"IMMUTAVAULT_{tag}_SSH_KEY"; o["ssh_key_env"] = ke
            if str(b.get("ssh_key_path") or "").strip(): env[ke] = str(b["ssh_key_path"]).strip()
            if typ == "proxmox": o.update({"compress": "0", "restore_storage": str(b.get("datastore") or "local-lvm")})
            elif str(b.get("restore_sr_uuid") or "").strip(): o["restore_sr_uuid"] = str(b["restore_sr_uuid"]).strip()
        p["options"] = o; return p, env

    def _temp(self, mutate):
        d = _load(self.config_path); mutate(d); fd, n = tempfile.mkstemp(suffix=".yml"); os.close(fd); p = Path(n); p.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8"); return load_config(str(p)), p

    def test_platform(self, b: dict[str, Any]) -> dict[str, Any]:
        p, e = self._platform(b); cfg, tmp = self._temp(lambda d: d.update(platforms=[p]))
        try:
            with _temporary_env({**_env_read(self.env_path), **e}):
                a = build_adapter(cfg.platforms[0], cfg.runtime.command_timeout_seconds); problems = a.doctor()
                if problems: return {"ok": False, "problems": problems, "inventory": []}
                return {"ok": True, "platform_info": a.platform_info(), "inventory": [v.__dict__ for v in a.inventory()]}
        finally: tmp.unlink(missing_ok=True)

    def save_platform(self, b: dict[str, Any]) -> dict[str, Any]:
        p, e = self._platform(b); d = _load(self.config_path); d["platforms"] = [x for x in (d.get("platforms") or []) if x.get("name") != p["name"]] + [p]; _atomic(self.config_path, d); _env_write(self.env_path, e); self._reload_env(); return {"saved": p["name"], "type": p["type"], "credential_envs": sorted(e)}

    def discover(self, name: str) -> dict[str, Any]:
        self._reload_env(); cfg = load_config(str(self.config_path)); p = next((x for x in cfg.platforms if x.name == name), None)
        if not p: raise ValueError(f"unknown platform {name}")
        # Current SSH adapters use IMMUTAVAULT_SSH_KEY; map a per-platform wizard key into it for this operation.
        key_env = str(p.options.get("ssh_key_env") or "")
        values = {"IMMUTAVAULT_SSH_KEY": os.getenv(key_env, "")} if key_env else {}
        with _temporary_env(values):
            a = build_adapter(p, cfg.runtime.command_timeout_seconds); problems = a.doctor()
            if problems: return {"ok": False, "problems": problems, "inventory": []}
            return {"ok": True, "platform_info": a.platform_info(), "inventory": [v.__dict__ for v in a.inventory()]}

    def save_selection(self, platform: str, vms: list[str]) -> dict[str, Any]:
        names = [str(x).strip() for x in vms if str(x).strip()]
        if not names: raise ValueError("select at least one VM")
        d = _load(self.config_path)
        for p in d.get("platforms") or []:
            if p.get("name") == platform: p["include"], p["exclude"] = names, []; _atomic(self.config_path, d); return {"platform": platform, "selected": len(names), "vms": names}
        raise ValueError(f"unknown platform {platform}")

    def _replica(self, b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        name, backend = str(b.get("name") or "").strip(), str(b.get("backend") or "").strip()
        if not name or backend not in {"s3", "filesystem", "rest"}: raise ValueError("storage name and backend are required")
        tag, env = _name(name), {}; r: dict[str, Any] = {"name": name, "enabled": True, "backend": backend}; pw = f"RESTIC_{tag}_PASSWORD"; r["password_env"] = pw; env[pw] = str(b.get("password") or "")
        if backend == "s3":
            r.update({"provider": str(b.get("provider") or "custom"), "endpoint": str(b.get("endpoint") or "").strip(), "region": str(b.get("region") or "").strip(), "bucket": str(b.get("bucket") or "").strip(), "prefix": str(b.get("prefix") or "prod").strip(), "connections": 10})
            if not r["endpoint"] or not r["bucket"]: raise ValueError("S3 endpoint and bucket are required")
            ae, se = f"{tag}_ACCESS_KEY_ID", f"{tag}_SECRET_ACCESS_KEY"; r["access_key_env"], r["secret_key_env"] = ae, se; env[ae], env[se] = str(b.get("access_key") or ""), str(b.get("secret_key") or ""); days = max(1, int(b.get("lock_days") or 30)); immutable = bool(b.get("immutable", True))
            if r["provider"] == "cloudflare_r2": r.update({"region": r["region"] or "auto", "object_lock_enabled": False, "r2_bucket_lock_enabled": immutable, "r2_bucket_lock_days": days, "r2_lock_rule_id": f"immutavault-{name}-retention", "r2_account_id_env": "CLOUDFLARE_ACCOUNT_ID", "r2_api_token_env": "CLOUDFLARE_API_TOKEN"})
            else: r.update({"object_lock_enabled": immutable, "object_lock_mode": "COMPLIANCE", "object_lock_days": days})
        elif backend == "filesystem":
            r.update({"path": str(b.get("path") or "").strip(), "mount_required": True, "mount_source": str(b.get("mount_source") or "").strip()})
            if not r["path"]: raise ValueError("mounted filesystem path is required")
        else:
            r["url"] = str(b.get("url") or b.get("endpoint") or "").strip()
            if not r["url"].startswith("rest:"): raise ValueError("second-vault URL must start with rest:")
        return r, env

    def test_storage(self, b: dict[str, Any]) -> dict[str, Any]:
        r, e = self._replica(b); cfg, tmp = self._temp(lambda d: d.update(replicas=[r]))
        try:
            replica = cfg.replicas[0]
            with _temporary_env({**_env_read(self.env_path), **e}):
                if replica.backend == "filesystem":
                    p = Path(replica.path or ""); return {"ok": p.is_dir(), "path": str(p), "exists": p.exists(), "writable": os.access(p, os.W_OK) if p.exists() else False}
                if replica.backend == "s3": return {"ok": True, "preflight": s3_preflight(replica)}
                h = BackupEngine(cfg).repo.replica_health(replica); return {"ok": bool(h.get("ok")), "health": h}
        finally: tmp.unlink(missing_ok=True)

    def save_storage(self, b: dict[str, Any]) -> dict[str, Any]:
        r, e = self._replica(b); d = _load(self.config_path); d["replicas"] = [x for x in (d.get("replicas") or []) if x.get("name") != r["name"]] + [r]; _atomic(self.config_path, d); _env_write(self.env_path, e); self._reload_env(); return {"saved": r["name"], "backend": r["backend"]}

    def init_storage(self, name: str) -> dict[str, Any]:
        self._reload_env(); engine = BackupEngine(load_config(str(self.config_path))); result = {"repository": engine.init_replica(name)}; r = next(x for x in engine.cfg.replicas if x.name == name)
        if r.object_lock_enabled or r.r2_bucket_lock_enabled: result["immutability"] = engine.init_replica_lock(name)
        return result

    def save_dr(self, b: dict[str, Any]) -> dict[str, Any]:
        req = ["primary_site", "dr_site", "replica", "primary_gateway_host", "dr_gateway_host", "primary_vtep", "dr_vtep", "subnet", "gateway_cidr"]
        missing = [k for k in req if not str(b.get(k) or "").strip()]
        if missing: raise ValueError("missing DR fields: " + ", ".join(missing))
        vlan, vni = int(b.get("vlan_id") or 0), int(b.get("vni") or 0)
        if not 1 <= vlan <= 4094 or not 1 <= vni <= 16777215: raise ValueError("invalid VLAN or VNI")
        primary, dr = str(b["primary_site"]), str(b["dr_site"]); underlay, trunk = str(b.get("underlay_interface") or "bond0"), str(b.get("trunk_interface") or "bond1")
        def gw(name, host, vtep): return {"name": name, "gateway": {"host": host, "ssh_user": "root", "underlay_interface": underlay, "trunk_interface": trunk, "vtep_ip": vtep, "router_id": vtep, "ospf_area": "0.0.0.0", "ospf_cost": 10, "ospf_auth_key_env": "IMMUTAVAULT_OSPF_KEY"}}
        drcfg = {"enabled": True, "primary_site": primary, "dr_site": dr, "replica": str(b["replica"]), "rpo_max_minutes": 1440, "auto_failover": False, "control_plane_site": dr, "failure_threshold": 5, "check_interval_seconds": 60, "primary_failure_quorum": 0, "maintenance_file": "/var/lib/immutavault/dr-maintenance", "fence": {"mode": "manual", "command_env": "IMMUTAVAULT_DR_FENCE_COMMAND", "verify_command_env": "IMMUTAVAULT_DR_FENCE_VERIFY_COMMAND"}, "primary_probes": [], "sites": [gw(primary, str(b["primary_gateway_host"]), str(b["primary_vtep"])), gw(dr, str(b["dr_gateway_host"]), str(b["dr_vtep"]))], "networks": [{"name": f"vlan-{vlan}", "vlan_id": vlan, "vni": vni, "subnet": str(b["subnet"]), "gateway_cidr": str(b["gateway_cidr"]), "mtu": int(b.get("mtu") or 1450)}], "workloads": []}
        d = _load(self.config_path); d["disaster_recovery"] = drcfg; _atomic(self.config_path, d); load_config(str(self.config_path)); return {"saved": True, "auto_failover": False, "network": drcfg["networks"][0]}

    def dr_plan(self, site: str) -> dict[str, Any]:
        self._reload_env(); d = BackupEngine(load_config(str(self.config_path))).dr_orchestrator(); return {"plan": d.plan(), "preflight": d.preflight(), "network": d.net.plan(site)}

    def dr_prepare(self, site: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "APPLY DR NETWORK": raise ValueError("confirmation phrase did not match")
        self._reload_env(); d = BackupEngine(load_config(str(self.config_path))).dr_orchestrator(); return {"site": site, "result": d.net.prepare(site), "activated": False}

    def doctor(self) -> dict[str, Any]:
        self._reload_env(); r = BackupEngine(load_config(str(self.config_path))).doctor(); return {"ok": not any(r.values()), "components": r}

    def backup(self, dry_run: bool) -> dict[str, Any]:
        self._reload_env(); rows = BackupEngine(load_config(str(self.config_path))).backup_all(dry_run=dry_run); return {"ok": not any(x.get("status") == "failed" for x in rows), "dry_run": dry_run, "results": rows}

    def enable_timers(self, confirmation: str) -> dict[str, Any]:
        if confirmation != "ENABLE SCHEDULES": raise ValueError("confirmation phrase did not match")
        units = ["immutavault-rest-server.service", "immutavault-portal.service", "immutavault-backup.timer", "immutavault-state-backup.timer", "immutavault-health.timer", "immutavault-retention.timer", "immutavault-verify.timer"]
        for unit in units:
            cp = subprocess.run(["systemctl", "enable", "--now", unit], capture_output=True, text=True)
            if cp.returncode: raise RuntimeError(f"failed enabling {unit}: {cp.stderr.strip()}")
        return {"enabled": units, "dr_auto_failover_enabled": False}


def serve(config: str, env: str, listen: str, port: int, cert: str | None, key: str | None, token: str) -> None:
    if listen not in {"127.0.0.1", "::1", "localhost"} and not (cert and key): raise RuntimeError("non-loopback setup console requires TLS")
    m = SetupManager(config, env)
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): print(f"setup {self.client_address[0]} {fmt % args}")
        def sendj(self, code, value):
            p = json.dumps(value, default=str).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(p))); self.send_header("Cache-Control", "no-store"); self.send_header("X-Frame-Options", "DENY"); self.end_headers(); self.wfile.write(p)
        def auth(self):
            h = self.headers.get("Authorization", ""); return h.startswith("Bearer ") and hmac.compare_digest(h[7:], token)
        def body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 1024 * 1024: raise ValueError("request body too large")
            return json.loads(self.rfile.read(n) or b"{}")
        def do_GET(self):
            if self.path == "/":
                p = UI.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(p))); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'"); self.end_headers(); self.wfile.write(p); return
            if not self.auth(): self.sendj(401, {"error": "invalid or missing setup token"}); return
            try:
                if self.path == "/api/v1/setup/status": self.sendj(200, m.status())
                elif self.path == "/api/v1/setup/platforms": self.sendj(200, m.platforms())
                else: self.sendj(404, {"error": "not found"})
            except Exception as e: self.sendj(500, {"error": str(e)})
        def do_POST(self):
            if not self.auth(): self.sendj(401, {"error": "invalid or missing setup token"}); return
            try:
                b, p = self.body(), self.path
                if p == "/api/v1/setup/platform/test": r = m.test_platform(b)
                elif p == "/api/v1/setup/platform/save": r = m.save_platform(b)
                elif p == "/api/v1/setup/platform/discover": r = m.discover(str(b.get("name") or ""))
                elif p == "/api/v1/setup/protection/save": r = m.save_selection(str(b.get("platform") or ""), list(b.get("vms") or []))
                elif p == "/api/v1/setup/storage/test": r = m.test_storage(b)
                elif p == "/api/v1/setup/storage/save": r = m.save_storage(b)
                elif p == "/api/v1/setup/storage/init": r = m.init_storage(str(b.get("name") or ""))
                elif p == "/api/v1/setup/dr/save": r = m.save_dr(b)
                elif p == "/api/v1/setup/dr/plan": r = m.dr_plan(str(b.get("site") or ""))
                elif p == "/api/v1/setup/dr/prepare": r = m.dr_prepare(str(b.get("site") or ""), str(b.get("confirmation") or ""))
                elif p == "/api/v1/setup/doctor": r = m.doctor()
                elif p == "/api/v1/setup/backup/dry-run": r = m.backup(True)
                elif p == "/api/v1/setup/backup/run": r = m.backup(False)
                elif p == "/api/v1/setup/timers/enable": r = m.enable_timers(str(b.get("confirmation") or ""))
                else: self.sendj(404, {"error": "not found"}); return
                self.sendj(200, r)
            except ValueError as e: self.sendj(400, {"error": str(e)})
            except Exception as e: self.sendj(500, {"error": str(e)})
    server = ThreadingHTTPServer((listen, port), H)
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2; ctx.load_cert_chain(cert, key); server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"Setup URL: {'https' if cert else 'http'}://{listen}:{port}/")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Guided Immutavault setup console"); p.add_argument("--config", default="/etc/immutavault/immutavault.yml"); p.add_argument("--env", default="/etc/immutavault/immutavault.env"); p.add_argument("--listen", default="127.0.0.1"); p.add_argument("--port", default=8788, type=int); p.add_argument("--tls-cert"); p.add_argument("--tls-key"); a = p.parse_args(argv)
    token = os.getenv("IMMUTAVAULT_SETUP_TOKEN") or secrets.token_urlsafe(24)
    if "IMMUTAVAULT_SETUP_TOKEN" not in os.environ: print(f"One-time setup token: {token}")
    serve(a.config, a.env, a.listen, a.port, a.tls_cert, a.tls_key, token); return 0


if __name__ == "__main__": raise SystemExit(main())
