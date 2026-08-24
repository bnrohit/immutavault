from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import tempfile
from typing import Any

import yaml

from . import setup_console as base
from .adapters import build_adapter
from .config import load_config
from .engine import BackupEngine
from .runner import run
from .storage import restic_options, target_env, s3_preflight


def _metadata(path: Path, default_mode: int) -> tuple[int, int, int]:
    if path.exists():
        st = path.stat()
        return st.st_uid, st.st_gid, st.st_mode & 0o777
    return os.geteuid(), os.getegid(), default_mode


def _atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    uid, gid, mode = _metadata(path, 0o640)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".setup-backup"))
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
            fh.flush(); os.fsync(fh.fileno())
        load_config(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _env_write(path: Path, updates: dict[str, str]) -> None:
    data = base._env_read(path)
    data.update({key: value for key, value in updates.items() if value != ""})
    path.parent.mkdir(parents=True, exist_ok=True)
    uid, gid, mode = _metadata(path, 0o640)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"{k}={shlex.quote(v)}" for k, v in sorted(data.items())) + "\n")
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# Existing setup-console methods resolve these helpers at call time, so replacing
# them upgrades all platform/storage writes without duplicating the HTTP server.
if hasattr(base, "_atomic"):
    base._atomic = _atomic
if hasattr(base, "_atomic_yaml"):
    base._atomic_yaml = _atomic
base._env_write = _env_write


def _enhance_ui(ui: str) -> str:
    if "Immutable-Copy Verification" not in ui:
        auth = '<div class="card"><label>One-time setup token <input id="token" type="password"></label> <button onclick="loadAll()">Connect</button> <span id="who" class="pill"></span></div>'
        if auth not in ui:
            auth = '<div class="card"><label>One-time setup token <input id="token" type="password"></label><button onclick="status()">Connect</button> <span id="st"></span></div>'
        dashboard = auth + '<div class="grid"><div class="card"><h2>RPO Status</h2><div id="rpo" class="muted">Connect to load.</div><label>RPO target (minutes)<input id="rpot" type="number" min="1" max="10080" value="1440"></label><button onclick="saveRpo()">Save RPO target</button></div><div class="card"><h2>Immutable-Copy Verification</h2><div id="imm" class="muted">Connect to load.</div><button onclick="verifyCopies()">Verify immutable copies now</button></div></div>'
        ui = ui.replace(auth, dashboard)
        status_old = "async function status(){try{let s=await api('/api/v1/setup/status');$('st').textContent=`Connected: ${s.platforms} hypervisor(s), ${s.replicas} replica(s)`;await plats()}catch(e){$('st').textContent=e.message}}"
        status_new = "async function status(){try{let s=await api('/api/v1/setup/status');$('st').textContent=`Connected: ${s.platforms} hypervisor(s), ${s.replicas} replica(s)`;await plats();await dashboard()}catch(e){$('st').textContent=e.message}}"
        ui = ui.replace(status_old, status_new)
        funcs = "async function dashboard(){let d=await api('/api/v1/setup/dashboard');$('rpot').value=d.rpo.target_minutes;$('rpo').innerHTML=`<b class=\"${d.rpo.overdue||d.rpo.never_backed_up?'warn':'ok'}\">${d.rpo.within_target}/${d.rpo.total} within target</b><br>Overdue: ${d.rpo.overdue} · Never backed up: ${d.rpo.never_backed_up}`;$('imm').innerHTML=`<b class=\"${d.immutable.unverified?'warn':'ok'}\">${d.immutable.verified}/${d.immutable.total} verified</b><br>Active immutable: ${d.immutable.active} · Unverified: ${d.immutable.unverified}`;}async function saveRpo(){try{await api('/api/v1/setup/rpo/save',{method:'POST',body:JSON.stringify({minutes:Number($('rpot').value)})});await dashboard()}catch(e){alert(e.message)}}async function verifyCopies(){try{out('fo',await api('/api/v1/setup/immutable/verify',{method:'POST',body:'{}'}));await dashboard()}catch(e){out('fo',{error:e.message})}}"
        ui = ui.replace('</script>', funcs + '</script>')
    if 'id="dsrc"' not in ui:
        ui = ui.replace(
            '<label>MTU<input id="dm" value="1450"></label>',
            '<label>MTU<input id="dm" value="1450"></label><label>DR gateway SSH key path<input id="dkey" placeholder="/etc/immutavault/keys/dr-gateway.key"></label><label>OSPF MD5 key (optional; must match upstream router)<input id="dok" type="password" maxlength="16"></label><label>Source hypervisor<select id="dsrc"></select></label><label>DR hypervisor<select id="ddst"></select></label>',
        )
    ui = ui.replace('>Save DR</button>', '>Save DR + map selected VMs</button>')
    original_plats = "async function plats(){let p=await api('/api/v1/setup/platforms');$('vp').innerHTML=p.map(x=>`<option>${x.name}</option>`).join('')}"
    enhanced_plats = "async function plats(){let p=await api('/api/v1/setup/platforms'),o=p.map(x=>`<option value=\"${esc(x.name)}\">${esc(x.name)} (${esc(x.type)})</option>`).join('');$('vp').innerHTML=o;$('dsrc').innerHTML=o;$('ddst').innerHTML=o}"
    ui = ui.replace(original_plats, enhanced_plats)
    if "const esc=" not in ui:
        ui = ui.replace(
            "const $=x=>document.getElementById(x);",
            "const $=x=>document.getElementById(x);const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\\\"','&quot;').replaceAll(\"'\",'&#39;');",
        )
    original_render = "function render(v){$('vms').innerHTML=v.map(x=>`<label class=\"vm\"><input class=\"vc\" type=\"checkbox\" value=\"${x.name}\" checked style=\"width:auto\"> ${x.name} (${x.power_state})</label>`).join('')||'No VMs discovered'}"
    enhanced_render = "function render(v){$('vms').innerHTML=v.map(x=>`<label class=\"vm\"><input class=\"vc\" type=\"checkbox\" value=\"${esc(x.name)}\" checked style=\"width:auto\"> ${esc(x.name)} (${esc(x.power_state)})</label>`).join('')||'No VMs discovered'}"
    ui = ui.replace(original_render, enhanced_render)
    if "function selectAll" not in ui:
        ui = ui.replace('<button onclick="discover()">Refresh VMs</button>', '<button onclick="discover()">Refresh VMs</button> <button onclick="selectAll(true)">Select all</button> <button onclick="selectAll(false)">Clear</button>')
        ui = ui.replace(enhanced_render, enhanced_render + "function selectAll(v){document.querySelectorAll('.vc').forEach(x=>x.checked=v)}")
    old_db = "function db(){return{primary_site:$('dp').value,dr_site:$('dd').value,replica:$('dr').value,primary_gateway_host:$('dph').value,dr_gateway_host:$('ddh').value,primary_vtep:$('dpv').value,dr_vtep:$('ddv').value,underlay_interface:$('du').value,trunk_interface:$('dt').value,vlan_id:Number($('dv').value),vni:Number($('dni').value),subnet:$('dsn').value,gateway_cidr:$('dgw').value,mtu:Number($('dm').value)}}"
    new_db = "function db(){return{primary_site:$('dp').value,dr_site:$('dd').value,replica:$('dr').value,primary_gateway_host:$('dph').value,dr_gateway_host:$('ddh').value,primary_vtep:$('dpv').value,dr_vtep:$('ddv').value,underlay_interface:$('du').value,trunk_interface:$('dt').value,vlan_id:Number($('dv').value),vni:Number($('dni').value),subnet:$('dsn').value,gateway_cidr:$('dgw').value,mtu:Number($('dm').value),dr_ssh_key_path:$('dkey').value,ospf_key:$('dok').value,source_platform:$('dsrc').value,target_platform:$('ddst').value}}"
    ui = ui.replace(old_db, new_db)
    ui = ui.replace(
        'Immutavault only PREPARES VXLAN/FRR/OSPF after explicit confirmation. It does not promote DR here.',
        'Choose a source and same-family DR hypervisor. Immutavault maps the exact VMs selected in step 2, then PREPARES VXLAN/FRR/OSPF only after explicit confirmation. It never promotes DR here.',
    )
    return ui


UI = _enhance_ui(base.UI)
base.UI = UI


class SetupManager(base.SetupManager):
    def status(self) -> dict[str, Any]:
        result = super().status()
        dr = base._load(self.config_path).get("disaster_recovery") or {}
        result["dr_workloads"] = len(dr.get("workloads") or [])
        return result

    def save_rpo_target(self, minutes: int) -> dict[str, Any]:
        minutes = int(minutes)
        if not 1 <= minutes <= 10080:
            raise ValueError("RPO target must be between 1 minute and 7 days")
        data = base._load(self.config_path)
        protection = dict(data.get("protection") or {})
        protection["rpo_target_minutes"] = minutes
        data["protection"] = protection
        _atomic(self.config_path, data)
        return {"rpo_target_minutes": minutes}

    def dashboard(self) -> dict[str, Any]:
        self._reload_env(); cfg = load_config(str(self.config_path)); engine = BackupEngine(cfg)
        now = datetime.now(timezone.utc); target = cfg.protection.rpo_target_minutes
        catalog = engine.state.list_vms(); by_name = {(str(r["platform"]), str(r["vm_name"])): r for r in catalog}
        protected: set[tuple[str, str]] = set()
        for platform in cfg.platforms:
            if not platform.enabled: continue
            for row in catalog:
                if str(row["platform"]) != platform.name: continue
                name = str(row["vm_name"])
                if any(fnmatch.fnmatch(name, pat) for pat in platform.include) and not any(fnmatch.fnmatch(name, pat) for pat in platform.exclude): protected.add((platform.name, name))
            for name in platform.include:
                if name != "*" and not any(ch in name for ch in "?[]"): protected.add((platform.name, name))
        within = overdue = never = 0; snapshots: set[str] = set()
        for key in protected:
            row = by_name.get(key)
            if not row or not row.get("latest_point"):
                never += 1; continue
            try:
                created = datetime.fromisoformat(str(row["latest_point"])); created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                if (now-created).total_seconds()/60 <= target: within += 1
                else: overdue += 1
            except (TypeError, ValueError): overdue += 1
            point = engine.state.latest_point(str(row["platform"]), str(row["vm_id"]))
            if point: snapshots.add(str(point["snapshot_id"]))
        total = active = verified = 0
        for sid in snapshots:
            point = engine.state.get_point(sid) or {}
            for copy in engine.state.list_recovery_copies(sid):
                name = str(copy.get("target_name")); lock = dict(copy.get("object_lock") or {})
                candidate = name == "primary" or bool(lock.get("enabled")) or bool(lock.get("logical_immutability"))
                if not candidate:
                    try:
                        r = engine._replica(name); candidate = r.object_lock_enabled or r.r2_bucket_lock_enabled
                    except ValueError: pass
                if not candidate: continue
                total += 1
                expiry = copy.get("immutable_until") or point.get("immutable_until")
                try:
                    dt = datetime.fromisoformat(str(expiry)); dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    active += int(dt > now and copy.get("status") == "success")
                except (TypeError, ValueError): pass
                verified += int(bool(copy.get("verified")))
        return {"rpo":{"target_minutes":target,"total":len(protected),"within_target":within,"overdue":overdue,"never_backed_up":never},"immutable":{"total":total,"active":active,"verified":verified,"unverified":max(0,total-verified)}}

    def _snapshot_exists(self, engine: BackupEngine, snapshot_id: str, target_name: str) -> bool:
        if target_name == "primary":
            result = run(["restic", "snapshots", snapshot_id, "--json"], timeout=300, env=engine.repo._env(local=False), check=False)
        else:
            replica = engine._replica(target_name)
            result = run(["restic", *restic_options(replica), "snapshots", snapshot_id, "--json"], timeout=300, env=target_env(replica), check=False)
        if result.returncode != 0: return False
        try: return bool(json.loads(result.stdout or "[]"))
        except json.JSONDecodeError: return False

    def verify_immutable_copies(self) -> list[dict[str, Any]]:
        self._reload_env(); engine = BackupEngine(load_config(str(self.config_path))); now = datetime.now(timezone.utc); results=[]; seen=set()
        for vm in engine.state.list_vms():
            point = engine.state.latest_point(str(vm["platform"]), str(vm["vm_id"]))
            if not point: continue
            sid = str(point["snapshot_id"])
            if sid in seen: continue
            seen.add(sid)
            for copy in engine.state.list_recovery_copies(sid):
                name = str(copy.get("target_name")); lock = dict(copy.get("object_lock") or {})
                candidate = name == "primary" or bool(lock.get("enabled")) or bool(lock.get("logical_immutability"))
                replica = None
                if name != "primary":
                    try:
                        replica = engine._replica(name); candidate = candidate or replica.object_lock_enabled or replica.r2_bucket_lock_enabled
                    except ValueError: pass
                if not candidate: continue
                detail={"snapshot_id":sid,"target":name}
                try:
                    exists=self._snapshot_exists(engine,sid,name); immutable=False
                    if name == "primary": immutable=bool(lock.get("logical_immutability"))
                    elif replica and replica.provider == "cloudflare_r2" and replica.r2_bucket_lock_enabled: immutable=bool(engine.replica_lock_status(name).get("enabled"))
                    elif replica and replica.backend == "s3" and replica.object_lock_enabled:
                        live=s3_preflight(replica); immutable=bool(live.get("object_lock_enabled")) and bool(lock.get("enabled"))
                    expiry=copy.get("immutable_until") or point.get("immutable_until")
                    try:
                        dt=datetime.fromisoformat(str(expiry)); dt=dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc); immutable=immutable and dt>now
                    except (TypeError,ValueError): immutable=False
                    ok=bool(exists and immutable); engine.state.mark_copy_verified(sid,name,ok,None if ok else "snapshot or immutability verification failed")
                    detail.update({"snapshot_present":exists,"immutability_active":immutable,"verified":ok})
                except Exception as exc:
                    engine.state.mark_copy_verified(sid,name,False,str(exc)); detail.update({"verified":False,"error":str(exc)})
                engine.state.audit("setup-admin","recovery.copy.verify","recovery_copy",f"{sid}:{name}",detail); results.append(detail)
        return results

    def save_platform(self, body: dict[str, Any]) -> dict[str, Any]:
        builder = getattr(self, "_platform", None) or getattr(self, "_platform_payload")
        platform, env = builder(body)
        data = base._load(self.config_path)
        previous = next((p for p in (data.get("platforms") or []) if p.get("name") == platform["name"]), None)
        if previous and previous.get("include") and previous.get("include") != ["*"]:
            platform["include"] = list(previous["include"])
        data["platforms"] = [p for p in (data.get("platforms") or []) if p.get("name") != platform["name"]] + [platform]
        _atomic(self.config_path, data); _env_write(self.env_path, env); self._reload_env()
        return {"saved": platform["name"], "type": platform["type"], "credential_envs": sorted(k for k, v in env.items() if v)}

    def discover(self, name: str) -> dict[str, Any]:
        self._reload_env(); cfg = load_config(str(self.config_path)); platform = next((p for p in cfg.platforms if p.name == name), None)
        if not platform:
            raise ValueError(f"unknown platform {name}")
        broad = replace(platform, include=["*"], exclude=[])
        adapter = build_adapter(broad, cfg.runtime.command_timeout_seconds)
        problems = adapter.doctor()
        if problems:
            return {"ok": False, "problems": problems, "inventory": []}
        return {"ok": True, "platform_info": adapter.platform_info(), "inventory": [vm.__dict__ for vm in adapter.inventory()]}

    def save_selection(self, platform: str, vms: list[str]) -> dict[str, Any]:
        selected = list(dict.fromkeys(str(vm).strip() for vm in vms if str(vm).strip()))
        if not selected:
            raise ValueError("select at least one VM")
        data = base._load(self.config_path)
        for item in data.get("platforms") or []:
            if item.get("name") == platform:
                item["include"], item["exclude"] = selected, []
                _atomic(self.config_path, data)
                return {"platform": platform, "selected": len(selected), "vms": selected}
        raise ValueError(f"unknown platform {platform}")

    def save_dr(self, body: dict[str, Any]) -> dict[str, Any]:
        required = ["primary_site", "dr_site", "replica", "primary_gateway_host", "dr_gateway_host", "primary_vtep", "dr_vtep", "subnet", "gateway_cidr", "source_platform", "target_platform"]
        missing = [key for key in required if not str(body.get(key) or "").strip()]
        if missing:
            raise ValueError("missing DR fields: " + ", ".join(missing))
        vlan, vni = int(body.get("vlan_id") or 0), int(body.get("vni") or 0)
        if not 1 <= vlan <= 4094 or not 1 <= vni <= 16777215:
            raise ValueError("invalid VLAN or VNI")
        data = base._load(self.config_path)
        replicas = {r.get("name") for r in (data.get("replicas") or []) if r.get("enabled", True)}
        if str(body["replica"]) not in replicas:
            raise ValueError("DR replica must first be added and enabled in Storage / Cloud")
        platforms = {p.get("name"): p for p in (data.get("platforms") or [])}
        source_name, target_name = str(body["source_platform"]), str(body["target_platform"])
        source, target = platforms.get(source_name), platforms.get(target_name)
        if not source or not target:
            raise ValueError("source and DR hypervisors must already be added")
        if source_name == target_name:
            raise ValueError("source and DR hypervisors must be different")
        if source.get("type") != target.get("type"):
            raise ValueError(f"cross-hypervisor automatic DR is blocked ({source.get('type')} -> {target.get('type')})")
        selected = list(dict.fromkeys(str(x).strip() for x in (source.get("include") or []) if str(x).strip()))
        if not selected or any(x == "*" or any(ch in x for ch in "?[]") for x in selected):
            raise ValueError("select exact VMs in step 2 before creating the DR map")
        primary, dr = str(body["primary_site"]).strip(), str(body["dr_site"]).strip()
        if primary == dr:
            raise ValueError("primary site and DR site must be different")
        ospf_key = str(body.get("ospf_key") or "").strip()
        if ospf_key and (len(ospf_key.encode()) > 16 or any(ch.isspace() for ch in ospf_key)):
            raise ValueError("OSPF MD5 key must be at most 16 bytes and contain no whitespace")
        env_updates: dict[str, str] = {}
        if ospf_key:
            env_updates["IMMUTAVAULT_OSPF_KEY"] = ospf_key
        dr_key = str(body.get("dr_ssh_key_path") or "").strip()
        if dr_key:
            env_updates["IMMUTAVAULT_SSH_KEY"] = dr_key
        underlay, trunk = str(body.get("underlay_interface") or "bond0"), str(body.get("trunk_interface") or "bond1")
        def site(name: str, host: str, vtep: str) -> dict[str, Any]:
            gateway: dict[str, Any] = {"host": host, "ssh_user": "root", "underlay_interface": underlay, "trunk_interface": trunk, "vtep_ip": vtep, "router_id": vtep, "ospf_area": "0.0.0.0", "ospf_cost": 10}
            if ospf_key:
                gateway["ospf_auth_key_env"] = "IMMUTAVAULT_OSPF_KEY"
            return {"name": name, "gateway": gateway}
        drcfg = {
            "enabled": True, "primary_site": primary, "dr_site": dr, "replica": str(body["replica"]),
            "rpo_max_minutes": 1440, "auto_failover": False, "control_plane_site": dr,
            "failure_threshold": 5, "check_interval_seconds": 60, "primary_failure_quorum": 0,
            "maintenance_file": "/var/lib/immutavault/dr-maintenance", "primary_probes": [],
            "fence": {"mode": "manual", "command_env": "IMMUTAVAULT_DR_FENCE_COMMAND", "verify_command_env": "IMMUTAVAULT_DR_FENCE_VERIFY_COMMAND"},
            "sites": [site(primary, str(body["primary_gateway_host"]), str(body["primary_vtep"])), site(dr, str(body["dr_gateway_host"]), str(body["dr_vtep"]))],
            "networks": [{"name": f"vlan-{vlan}", "vlan_id": vlan, "vni": vni, "subnet": str(body["subnet"]), "gateway_cidr": str(body["gateway_cidr"]), "mtu": int(body.get("mtu") or 1450)}],
            "workloads": [{"name": vm, "source_platform": source_name, "target_platform": target_name, "boot_order": (index + 1) * 10, "health_checks": [], "restore_options": {}} for index, vm in enumerate(selected)],
        }
        data["disaster_recovery"] = drcfg
        _atomic(self.config_path, data)
        if env_updates:
            _env_write(self.env_path, env_updates); self._reload_env()
        load_config(str(self.config_path))
        return {"saved": True, "enabled": True, "auto_failover": False, "source": source_name, "target": target_name, "workloads": drcfg["workloads"], "network": drcfg["networks"][0]}

    def dr_prepare(self, site: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "APPLY DR NETWORK":
            raise ValueError("confirmation phrase did not match")
        self._reload_env(); engine = BackupEngine(load_config(str(self.config_path))); dr = engine.dr_orchestrator()
        preflight = dr.net.preflight(site)
        if not preflight.get("ok"):
            raise RuntimeError(f"DR gateway preflight failed: {preflight}")
        return {"site": site, "preflight": preflight, "result": dr.net.prepare(site), "activated": False}


base.SetupManager = SetupManager

def serve(config: str, env: str, listen: str, port: int, cert: str | None, key: str | None, token: str) -> None:
    if listen not in {"127.0.0.1", "::1", "localhost"} and not (cert and key):
        raise RuntimeError("non-loopback setup console requires TLS")
    manager = SetupManager(config, env)
    class Handler(base.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None: print(f"setup {self.client_address[0]} {fmt % args}")
        def _send(self, code: int, value: Any) -> None:
            payload=json.dumps(value,default=str).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(payload))); self.send_header("Cache-Control","no-store"); self.send_header("X-Frame-Options","DENY"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Referrer-Policy","no-referrer"); self.end_headers(); self.wfile.write(payload)
        def _auth(self) -> bool:
            header=self.headers.get("Authorization",""); return header.startswith("Bearer ") and base.hmac.compare_digest(header[7:],token)
        def _body(self) -> dict[str,Any]:
            length=int(self.headers.get("Content-Length","0") or 0);
            if length>1024*1024: raise ValueError("request body too large")
            return json.loads(self.rfile.read(length) or b"{}")
        def do_GET(self) -> None:
            if self.path=="/":
                payload=UI.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.send_header("Cache-Control","no-store"); self.send_header("Content-Security-Policy","default-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","no-referrer"); self.send_header("X-Content-Type-Options","nosniff"); self.end_headers(); self.wfile.write(payload); return
            if not self._auth(): self._send(401,{"error":"invalid or missing setup token"}); return
            try:
                if self.path=="/api/v1/setup/status": self._send(200,manager.status())
                elif self.path=="/api/v1/setup/dashboard": self._send(200,manager.dashboard())
                elif self.path=="/api/v1/setup/platforms": self._send(200,manager.platforms())
                else: self._send(404,{"error":"not found"})
            except Exception as exc: print(f"setup GET error: {type(exc).__name__}: {exc}"); self._send(500,{"error":"internal server error"})
        def do_POST(self) -> None:
            if not self._auth(): self._send(401,{"error":"invalid or missing setup token"}); return
            try:
                body,path=self._body(),self.path
                if path=="/api/v1/setup/platform/test": result=manager.test_platform(body)
                elif path=="/api/v1/setup/platform/save": result=manager.save_platform(body)
                elif path=="/api/v1/setup/platform/discover": result=manager.discover(str(body.get("name") or ""))
                elif path=="/api/v1/setup/protection/save": result=manager.save_selection(str(body.get("platform") or ""),list(body.get("vms") or []))
                elif path=="/api/v1/setup/rpo/save": result=manager.save_rpo_target(int(body.get("minutes") or 0))
                elif path=="/api/v1/setup/immutable/verify": result=manager.verify_immutable_copies()
                elif path=="/api/v1/setup/storage/test": result=manager.test_storage(body)
                elif path=="/api/v1/setup/storage/save": result=manager.save_storage(body)
                elif path=="/api/v1/setup/storage/init": result=manager.init_storage(str(body.get("name") or ""))
                elif path=="/api/v1/setup/dr/save": result=manager.save_dr(body)
                elif path=="/api/v1/setup/dr/plan": result=manager.dr_plan(str(body.get("site") or ""))
                elif path=="/api/v1/setup/dr/prepare": result=manager.dr_prepare(str(body.get("site") or ""),str(body.get("confirmation") or ""))
                elif path=="/api/v1/setup/doctor": result=manager.doctor()
                elif path=="/api/v1/setup/backup/dry-run": result=manager.backup(True)
                elif path=="/api/v1/setup/backup/run": result=manager.backup(False)
                elif path=="/api/v1/setup/timers/enable": result=manager.enable_timers(str(body.get("confirmation") or ""))
                else: self._send(404,{"error":"not found"}); return
                self._send(200,result)
            except ValueError as exc: self._send(400,{"error":str(exc)})
            except Exception as exc: print(f"setup POST error: {type(exc).__name__}: {exc}"); self._send(500,{"error":"internal server error"})
    server=base.ThreadingHTTPServer((listen,port),Handler)
    if cert and key:
        context=base.ssl.SSLContext(base.ssl.PROTOCOL_TLS_SERVER); context.minimum_version=base.ssl.TLSVersion.TLSv1_2; context.load_cert_chain(cert,key); server.socket=context.wrap_socket(server.socket,server_side=True)
    print(f"Setup URL: {'https' if cert else 'http'}://{listen}:{port}/"); server.serve_forever()

def main(argv: list[str] | None = None) -> int:
    parser=base.argparse.ArgumentParser(description="Guided Immutavault setup console")
    parser.add_argument("--config",default="/etc/immutavault/immutavault.yml"); parser.add_argument("--env",default="/etc/immutavault/immutavault.env"); parser.add_argument("--listen",default="127.0.0.1"); parser.add_argument("--port",default=8788,type=int); parser.add_argument("--tls-cert"); parser.add_argument("--tls-key")
    args=parser.parse_args(argv); token=os.getenv("IMMUTAVAULT_SETUP_TOKEN") or secrets.token_urlsafe(24)
    if "IMMUTAVAULT_SETUP_TOKEN" not in os.environ: print(f"One-time setup token: {token}")
    serve(args.config,args.env,args.listen,args.port,args.tls_cert,args.tls_key,token); return 0

if __name__ == "__main__": raise SystemExit(main())
