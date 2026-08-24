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
    # Translate the developer-oriented base headings into an operator workflow.
    # The underlying endpoints stay shared with the CLI/config schema.
    headings = {
        "<h2>1. Hypervisor</h2>": "<h2>1. Add a hypervisor</h2>",
        "<h2>2. Select VMs</h2>": "<h2>2. Choose VMs to protect</h2>",
        "<h2>3. Storage / Cloud</h2>": "<h2>3. Add storage or cloud</h2>",
        "<h2>4. DR site and network</h2>": "<h2>4. Configure disaster-recovery site</h2>",
        "<h2>5. Test and start</h2>": "<h2>5. Test and start protection</h2>",
    }
    for old, new in headings.items():
        ui = ui.replace(old, new)
    if "Immutable-Copy Verification" not in ui:
        auth = '<div class="card"><label>One-time setup token <input id="token" type="password"></label> <button onclick="loadAll()">Connect</button> <span id="who" class="pill"></span></div>'
        if auth not in ui:
            auth = '<div class="card"><label>One-time setup token <input id="token" type="password"></label><button onclick="status()">Connect</button> <span id="st"></span></div>'
        dashboard = auth + '<div class="grid"><div class="card"><h2>RPO Status</h2><div id="rpo" class="muted">Connect to load.</div><label>RPO target (minutes)<input id="rpot" type="number" min="1" max="10080" value="1440"></label><button onclick="saveRpo()">Save RPO target</button></div><div class="card"><h2>Immutable-Copy Verification</h2><div id="imm" class="muted">Connect to load.</div><button onclick="verifyCopies()">Verify immutable copies now</button></div></div>'
        ui = ui.replace(auth, dashboard)
        status_old = "async function status(){try{let s=await api('/api/v1/setup/status');$('st').textContent=`Connected: ${s.platforms} hypervisor(s), ${s.replicas} replica(s)`;await plats()}catch(e){$('st').textContent=e.message}}"
        status_new = "async function status(){try{let s=await api('/api/v1/setup/status');$('st').textContent=`Connected: ${s.platforms} hypervisor(s), ${s.replicas} replica(s)`;await plats();await dashboard()}catch(e){$('st').textContent=e.message}}"
        ui = ui.replace(status_old, status_new)
        funcs = "async function dashboard(){let d=await api('/api/v1/setup/dashboard');$('rpot').value=d.rpo.target_minutes;$('rpo').innerHTML=`<b class=\"${d.rpo.overdue||d.rpo.never_backed_up?'warn':'ok'}\">${d.rpo.within_target}/${d.rpo.total} within target</b><br>Overdue: ${d.rpo.overdue} Â· Never backed up: ${d.rpo.never_backed_up}`;$('imm').innerHTML=`<b class=\"${d.immutable.unverified?'warn':'ok'}\">${d.immutable.verified}/${d.immutable.total} verified</b><br>Active immutable: ${d.immutable.active} Â· Unverified: ${d.immutable.unverified}`;}async function saveRpo(){try{await api('/api/v1/setup/rpo/save',{method:'POST',body:JSON.stringify({minutes:Number($('rpot').value)})});await dashboard()}catch(e){alert(e.message)}}async function verifyCopies(){try{out('fo',await api('/api/v1/setup/immutable/verify',{method:'POST',body:'{}'}));await dashboard()}catch(e){out('fo',{error:e.message})}}"
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
    original_db = "function db(){return{name:$('dp').value,dr_site:$('dd').value,replica:$('dr').value,primary_gateway_host:$('dph').value,dr_gateway_host:$('ddh').value,{primary_vtep:$('dpv').value,dr_vtep:$('ddv').value,underlay_interface:$('ud').value,trunk_interface:$('dt').value,vlan_id:Number($('dv').value),vni:Number($('dni').value),subnet:$('dsn').value,gateway_cidr:$('dgw').value,mtu:Number($('dm').value)}}"
    enhanced_db = "function db(){return{name:$('dp').value,dr_site:$('dd').value,replica:$('dr').value,primary_gateway_host:$('dph').value,dr_gateway_host:$('ddh').value,primary_vtep:$('dpv').value,dr_vtep:$('ddv').value,underlay_interface:$('ud').value,trunk_interface:$('dt').value,vlan_id:Number($('dv').value),vni:Number($('dni').value),subnet:$('dsn').value,gateway_cidr:$('dgw').value,mtu:Number($('dm').value),dr_ssh_key_path:$('dkey').value,ospf_key:$('dok').value,source_platform:$('dsrc').value,target_platform:$('ddst').value}}"
    ui = ui.replace(original_db, enhanced_db)
    return ui



UI = _enhance_ui(base.UI)


class SetupManager(base.SetupManager):
    def status(self) -> dict[str, Any]:
        result = super().status()
        try:
            cfg = load_config(str(self.config_path))
            result["rpo_target_minutes"] = cfg.protection.rpo_target_minutes
        except Exception:
            result["rpo_target_minutes"] = None
        return result

    def save_rpo_target(self, minutes: int) -> dict[str, Any]:
        if not 1 <= int(minutes) <= 10080:
            raise ValueError("RPO target must be between 1 and 10080 minutes")
        data = base._load(self.config_path)
        protection = dict(data.get("protection") or {}); protection["rpo_target_minutes"] = int(minutes); data["protection"] = protection
        _atomic(self.config_path, data)
        return {"rpo_target_minutes": int(minutes)}

    def dashboard(self) -> dict[str, Any]:
        self._reload_env()
        cfg = load_config(str(self.config_path)); engine = BackupEngine(cfg); now = datetime.now(timezone.utc)
        selected: list[tuple[str, str, str]] = []
        for platform in cfg.platforms:
            if not platform.enabled: continue
            for vm in build_adapter(platform, cfg.runtime.command_timeout_seconds).inventory(): selected.append((platform.name, vm.id, vm.name))
        rpo_rows, overdue, never = [], 0, 0
        for platform, vm_id, vm_name in selected:
            point = engine.state.latest_point(platform, vm_id); row = {"platform": platform, "vm_id": vm_id, "vm_name": vm_name, "latest_point": None, "age_minutes": None, "within_target": False}
            if point:
                try: created = datetime.fromisoformat(str(point["created_at"])); age = max(0.0, (now - created).¶‹Z–Çœ¢wlÿ­›İË\]JÈ›]\İÜÚ[ˆÚ[È˜Ü™X]YØ]—K˜YÙWÛZ[]\Èˆ›İ[™
YÙKJKÚ][—İ\™Ù]ˆYÙHHÙ™Ëœ›İXİ[Û‹œœ×İ\™Ù]ÛZ[]\ßJNÈİ™\™YH
ÏHYˆ›İÖÈÚ][—İ\™Ù]—H[ÙHBˆ^Ù\^Ù\[Ûˆİ™\™YH
ÏHBˆ[ÙNˆ™]™\ˆ
ÏHBˆœ×Ü›İÜË˜\[™
›İÊBˆ[WÜ›İÜÎˆ\İÙXİÜİ‹[WWHH×Bˆ›Üˆ]›Ü›K›WÚY›WÛ˜[YH[ˆÙ[XİY‚ˆÚ[H[™Ú[™Kœİ]K›]\İÜÚ[
]›Ü›K›WÚY
BˆYˆ›İÚ[ˆÛÛ[YBˆÛÜY\ÈH[™Ú[™Kœİ]K›\İÜ™XÛİ™\WØÛÜY\ÊİŠÚ[ÈœÛ˜\ÚİÚY—JJNÈXİ]™HH×Bˆ›ÜˆÛÜH[ˆÛÜY\Î‚ˆYˆÛÜK™Ù]
œİ]\ÈŠHOHœİXØÙ\ÜÈˆÛÛ[YBˆNˆ[[H]][YK™œ›ÛZ\ÛÙ›Ü›X]
İŠÛÜK™Ù]
š[[]]X›Wİ[[ˆÜˆˆŠJNÈØÚÙYH[[ˆ›İÂˆ^Ù\^Ù\[ÛˆØÚÙYH˜[ÙBˆÙÚXØ[H›ÛÛ

ÛÜK™Ù]
›Øš™XİÛØÚÈŠHÜˆßJK™Ù]
›ÙÚXØ[Ú[[]]Xš[]HŠJBˆYˆØÚÙYÜˆÙÚXØ[ˆXİ]™K˜\[™
ÛÜJBˆ[WÜ›İÜË˜\[™
Èœ]›Ü›Hˆ]›Ü›K›WÛ˜[YHˆ›WÛ˜[YKœÛ˜\ÚİÚYˆÚ[ÈœÛ˜\ÚİÚY—K˜Xİ]™WØÛÜY\Èˆ[ŠXİ]™JK™\šYšYYØÛÜY\Èˆİ[JH›ÜˆÈ[ˆXİ]™HYˆË™Ù]
™\šYšYYŠJ_JBˆ™]\›ˆÈœœÈˆÈ\™Ù]ÛZ[]\ÈˆÙ™Ëœ›İXİ[Û‹œœ×İ\™Ù]ÛZ[]\Ëİ[ˆ[Šœ×Ü›İÜÊKÚ][—İ\™Ù]ˆİ[JH›Üˆˆ[ˆœ×Ü›İÜÈYˆ–ÈÚ][—İ\™Ù]—JK›İ™\™YHˆİ™\™YK›™]™\—Ø˜XÚÙYİ\ˆ™]™\‹ÛÜšÛØYÈˆœ×Ü›İÜßKš[[]]X›HˆÈİ[ˆ[Š[WÜ›İÜÊK˜Xİ]™Hˆİ[JH›Üˆˆ[ˆ[WÜ›İÜÈYˆ–È˜Xİ]™WØÛÜY\È—JK™\šYšYYˆİ[JH›Üˆˆ[ˆ[WÜ›İÜÈYˆ–È™\šYšYYØÛÜY\È—JK[™\šYšYYˆİ[JH›Üˆˆ[ˆ[WÜ›İÜÈYˆ–È˜Xİ]™WØÛÜY\È—H[™›İ–È™\šYšYYØÛÜY\È—JKÛÜšÛØYÈˆ[WÜ›İÜß_B‚ˆYˆÜÛ˜\ÚİÙ^\İÊÙ[‹[™Ú[™Nˆ˜XÚİ\[™Ú[™KÛ˜\ÚİÚYˆİ‹™\XØOS›Û™JHOˆ›ÛÛ‚ˆ\™ÜÈH™\İX×ÛÜ[ÛœÊœ™\İXÈKZœÛÛˆÛ˜\ÚİÈŠBˆ™\ÈH[™Ú[™Kœ™\Ë—Ø˜\ÙJ
H
È\™ÜÈ
ÈÈ‹KZœÛÛˆ‹œÛ˜\ÚİÈ—Bˆ[ˆH[™Ú[™Kœ™\Ë—Ù[Š
BˆYˆ™\XØH\È›İ›Û™N‚ˆ™\ÈH[™Ú[™Kœ™\Ë—Ø˜\ÙJ
^È
È\™ÜÈ
ÈÈ‹\ˆ‹˜\ÙKœ™\İX×İ\™Ù]İ\›
™\XØJK‹KZœÛÛˆ‹œÛ˜\ÚİÈ—Bˆ[‹\]J\™Ù]Ù[Š™\XØJJBˆ™\İ[H[Š™\Ë[Y[‹[Y[İ]MŒÚXÚÏQ˜[ÙJBˆYˆ™\İ[œ™]\›˜ÛÙHOHˆ™]\›ˆ˜[ÙBˆNˆ›İÜÈHœÛÛ‹›ØYÊ™\İ[œİİ]Üˆ–×HŠBˆ^Ù\œÛÛ‹’”ÓÓ‘XÛÙQ\œ›Üˆ™]\›ˆ˜[ÙBˆ™]\›ˆ[JİŠ‹™Ù]
œÚÜÚYˆÜˆ‹™Ù]
šYŠHÜˆˆŠKœİ\İÚ]
Û˜\ÚİÚY
H›Üˆˆ[ˆ›İÜÊB‚ˆYˆ™\šYWÚ[[]]X›WØÛÜY\ÊÙ[ŠHOˆXİÜİ‹[WN‚ˆÙ[‹—Ü™[ØYÙ[Š
NÈÙ™ÈHØYØÛÛ™šYÊİŠÙ[‹˜ÛÛ™šY×Ü]
JNÈ[™Ú[™HH˜XÚİ\[™Ú[™JÙ™ÊNÈ›İÈH]][YK››İÊ[Y^›Û™K]ÊNÈ™\İ[ÈH×Bˆ›Üˆ]›Ü›H[ˆÙ™Ëœ]›Ü›\Î‚ˆYˆ›İ]›Ü›K™[˜X›YˆÛÛ[YBˆ›Üˆ›H[ˆZ[ØY\\Š]›Ü›KÙ™Ëœ[[YK˜ÛÛ[X[™İ[Y[İ]ÜÙXÛÛ™ÊKš[™[ÜJ
N‚ˆÚ[H[™Ú[™Kœİ]K›]\İÜÚ[
]›Ü›K›˜[YK›KšY
BˆYˆ›İÚ[ˆÛÛ[YBˆÛ˜\ÚİHİŠÚ[ÈœÛ˜\ÚİÚY—JBˆ›ÜˆÛÜH[ˆ[™Ú[™Kœİ]K›\İÜ™XÛİ™\WØÛÜY\ÊÛ˜\Úİ
N‚ˆYˆÛÜK™Ù]
œİ]\ÈŠHOHœİXØÙ\ÜÈˆÛÛ[YBˆ\™Ù]HİŠÛÜVÈ\™Ù]Û˜[YH—JNÈÚË]Z[H˜[ÙKˆ‚ˆN‚ˆYˆ\™Ù]OHœš[X\H‚ˆÚÈHÙ[‹—ÜÛ˜\ÚİÙ^\İÊ[™Ú[™KÛ˜\Úİ
BˆØÚÈHÛÜK™Ù]
›Øš™XİÛØÚÈˆÜˆßNÈÙÚXØ[H›ÛÛ
ØÚË™Ù]
›ÙÚXØ[Ú[[]]Xš[]HŠJBˆNˆ[[H]][YK™œ›ÛZ\ÛÙ›Ü›JİŠÛÜK™Ù]
š[[]]X›Wİ[[ŠHÜˆˆŠJNÈØÚÙYH[[ˆ›İÂˆ^Ù\^Ù\[ÛˆØÚÙYH˜[ÙBˆÚÈHÚÈ[™
ÙÚXØ[ÜˆØÚÙY
NÈ]Z[H˜\[™[Û›H˜][‚ˆ[ÙN‚ˆ™\XØHH™^

ˆ›Üˆˆ[ˆÙ™Ëœ™\XØ\ÈYˆ‹›˜[YHOH\™Ù]
K›Û™JBˆYˆ›İ™\XØNˆ˜Z\ÙH[[YQ\œ›ÜŠ˜ÛÛ™šYİ\™Y™\XØH›İ›İ[™ŠBˆÚÈHÙ[‹—ÜÛ˜\ÚİÙ^\İÊ[™Ú[™KÛ˜\Úİ™\XØJBˆYˆ™\XØKœ›İšY\ˆOH˜ÛİY›\™WÜŒˆˆ[™™\XØKœŒ—ØXÚÙ]ÛØÚ×Ù[˜X›Y‚ˆİ]\ÈH˜\ÙKœ—ØXÚÙ]ÛØÚ×Üİ]\Ê™\XØJNÈÚÈHÚÈ[™›ÛÛ
İ]\Ë™Ù]
›ØÚÙYİ[[ŠHÜˆİ]\Ë™Ù]
œ[\ÈŠJNÈ]Z[HœŒˆXÚÙ]ØÚÈ‚ˆ[Yˆ™\XØK˜˜XÚÙ[™OHœÌÈˆ[™™\XØK›Øš™XİÛØÚ×Ù[˜X›Y‚ˆ™Y›YÚHÌ×Ü™Y›YÚ
™\XØJNÈÚÈHÚÈ[™›ÛÛ
™Y›YÚ™Ù]
›Øš™XİÛØÚ×Ù[˜X›YŠJNÈ]Z[HœÌÈØš™XİØÚÈ‚ˆ[ÙN‚ˆNˆ[[H]][YK™œ›ÛZ\ÛÙ›Ü›X]
İŠÛÜK™Ù]
š[[]]X›Wİ[[ˆÜˆˆŠJNÈÚÈHÚÈ[™[[ˆ›İÂˆ^Ù\^Ù\[ÛˆÚÈH˜[ÙBˆ]Z[H˜Ø][ÙÈ™][[Ûˆ‚ˆ^Ù\^Ù\[Ûˆ\È^Îˆ]Z[HİŠ^ÊNÈÚÈH˜[ÙBˆ[™Ú[™Kœİ]K›X\š×ØÛÜWİ™\šYšYY
Û˜\Úİ\™Ù]ÚË]Z[
NÈ™\İ[Ë˜\[™
ÈœÛ˜\ÚİÚYˆÛ˜\Úİ\™Ù]ˆ\™Ù]™\šYšYYˆÚË™]Z[ˆ]Z[JBˆ™]\›ˆÈ›ÚÈˆ[
‹™Ù]
™\šYšYYŠH›Üˆˆ[ˆ™\İ[ÊHYˆ™\İ[È[ÙH˜[ÙK˜ÛÜY\Èˆ™\İ[ßB‚ˆYˆØ]™WÜ]›Ü›JÙ[‹›ÙNˆXİÜİ‹[WJHOˆXİÜİ‹[WN‚ˆ˜[YHHİŠ›ÙK™Ù]
›˜[YHŠHÜˆˆŠKœİš\

NÈ™]š[İ\×Ù]HH˜\ÙK—ÛØY
Ù[‹˜ÛÛ™šY×Ü]
NÈ™]š[İ\ÈH™^

›Üˆ[ˆ™]š[İ\×Ù]K™Ù]
œ]›Ü›\È‹×JHYˆ™Ù]
›˜[YHŠHOH˜[YJK›Û™JNÈ™]š[İ\×ÜÙ[Xİ[ÛˆH\İ

™]š[İ\ÈÜˆßJK™Ù]
š[˜ÛYHŠHÜˆ×JBˆ™\İ[Hİ\\Š
KœØ]™WÜ]›Ü›J›ÙJBˆYˆ™]š[İ\×ÜÙ[Xİ[Ûˆ[™™]š[İ\×ÜÙ[Xİ[ÛˆOHÈŠˆ—N‚ˆ]HH˜\ÙK—ÛØY
Ù[‹˜ÛÛ™šY×Ü]
Bˆ›Üˆ]›Ü›H[ˆ]K™Ù]
œ]›Ü›\È‹×JN‚ˆYˆ]›Ü›K™Ù]
›˜[YHŠHOH˜[YNˆ]›Ü›VÈš[˜ÛYH—HH™]š[İ\×ÜÙ[Xİ[ÛÈ]›Ü›VÈ™^ÛYH—HH×NÈœ™XZÂˆØ]ÛZXÊÙ[‹˜ÛÛ™šY×Ü]]JBˆ™]\›ˆ™\İ[‚ˆYˆ\ØÛİ™\ŠÙ[‹˜[YNˆİŠHOˆXİÜİ‹[WN‚ˆÙ[‹—Ü™[ØYÙ[Š
NÈÙ™ÈHØYØÛÛ™šYÊİŠÙ[‹˜ÛÛ™šY×Ü]
JNÈ]›Ü›HH™^

›Üˆ[ˆÙ™Ëœ]›Ü›\ÈYˆ›˜[YHOH˜[YJK›Û™JBˆYˆ›İ]›Ü›Nˆ˜Z\ÙH˜[YQ\œ›ÜŠˆ[šÛ›İÛˆ]›Ü›HÛ˜[Y_HŠBˆœ›ØYH™\XÙJ]›Ü›K[˜ÛYOVÈŠˆ—K^ÛYOV×JNÈ[\HßBˆÙ^WÙ[ˆHİŠœ›ØY›Ü[ÛœË™Ù]
œÜÚÚÙ^WÙ[ˆŠHÜˆˆŠBˆYˆÙ^WÙ[ˆ[™ÜË™Ù][ŠÙ^WÙ[ŠNˆ[\È’SSUUUUSÔÔÒÒÑVH—HHÜË™[š\›Û–ÚÙ^WÙ[—BˆÚ]˜\ÙK—İ[\Ü˜\WÙ[Š[\
N‚ˆY\\ˆHZ[ØY\\Šœ›ØYÙ™Ëœ[[YK˜ÛÛ[X[™İ[Y[İ]ÜÙXÛÛ™ÊNÈ›Ø›[\ÈHY\\‹™ØİÜŠ
BˆYˆ›Ø›[\Îˆ™]\›ˆÈ›ÚÈˆ˜[ÙKœ›Ø›[\Èˆ›Ø›[\Ëš[™[ÜHˆ×_Bˆ™]\›ˆÈ›ÚÈˆYKœ]›Ü›WÚ[™›ÈˆY\\‹œ]›Ü›WÚ[™›Ê
Kš[™[ÜHˆİ—×ÙXİ×È›Üˆˆ[ˆY\\‹š[™[ÜJ
W_B‚ˆYˆØ]™WÜÙ[Xİ[ÛŠÙ[‹]›Ü›Nˆİ‹›\Îˆ\İÜİ—JHOˆXİÜİ‹[WN‚ˆ˜[Y]YHÙ[‹™\ØÛİ™\Š]›Ü›JBˆYˆ›İ˜[Y]Y™Ù]
›ÚÈŠNˆ˜Z\ÙH[[YQ\œ›ÜŠˆ˜Ø[››İ˜[Y]H“H[™[ÜNˆİ˜[Y]Y™Ù]
	Ü›Ø›[\ÉÊ_HŠBˆ\ØÛİ™\™YHÜİŠÈ›˜[YH—JH›Üˆ[ˆ˜[Y]YÚ[™[ÜH—_NÈÙ[XİYHÜİŠ
Kœİš\

H›Üˆ[ˆ›\ÈYˆİŠ
Kœİš\

WBˆYˆ›İÙ[XİYˆ˜Z\ÙH˜[YQ\œ›ÜŠœÙ[Xİ]X\İÛ™H“HŠBˆ[šÛ›İÛˆHÛÜY
Ù]
Ù[XİY
HH\ØÛİ™\™Y
BˆYˆ[šÛ›İÛˆ˜Z\ÙH˜[YQ\œ›ÜŠˆ•“\È›ÈÛ™Ù\ˆ™\Ù[[ˆ[™[ÜNˆÉİ	Ë	Ëš›Ú[Š[šÛ›İÛŠ_HŠBˆ™]\›ˆİ\\Š
KœØ]™WÜÙ[Xİ[ÛŠ]›Ü›KÙ[XİY
B‚ˆYˆØ]™WÙŠÙ[‹›ÙNˆXİÜİ‹[WJHOˆXİÜİ‹[WN‚ˆ]HH˜\ÙK—ÛØY
Ù[‹˜ÛÛ™šY×Ü]
NÈ]›Ü›\ÈHÜİŠ™Ù]
›˜[YHŠJNˆè®˜§u«Zëi•«_¢¹¬ source_name, target_name = str(body.get("source_platform") or ""), str(body.get("target_platform") or "")
        if source_name not in platforms or target_name not in platforms: raise ValueError("choose a valid source and DR hypervisor")
        if platforms[source_name].get("type") != platforms[target_name].get("type"): raise ValueError("automatic DR mapping requires the same hypervisor family")
        selected = list(platforms[source_name].get("include") or [])
        if not selected or selected == ["*"]: raise ValueError("select exact VMs to protect before configuring DR")
        primary, dr, replica = str(body.get("primary_site") or "main"), str(body.get("dr_site") or "dr-site"), str(body.get("replica") or "").trim()
        if not replica or not any(r.get("name" ) == replica and bool(r.get("enabled")) for r in data.get("replicas", [])): raise ValueError("choose an enabled DR storage replica")
        vlan, vni = int(body.get("vlan_id") or 0), int(body.get("vni") or 0)
        if not 1 <= vlan <= 4094 or not 1 <= vni <= 16777215: raise ValueError("enter a valid VLAN (1-4094) and VNI")
        tag, env_updates = "DE_GATEWAY", {}
        if str(body.get("dr_ssh_key_path") or "").strip(): env_updates["IMMUTAVAULT_SSH_KEY"] = str(body["dr_ssh_key_path"]).strip()
        if str(body.get("ospf_key") or "").strip(): env_updates["IMMUTAVAULT_OSPF_KEY"] = str(body["ospf_key"]).strip()
        def site(name: str, host: str, vtep: str) -> dict[str, Any]:
            return {"name": name, "gateway": {"host": host, "ssh_user": str(body.get("ssh_user") or "root"), "underlay_interface": str(body.get("underlay_interface") or "bond0"), "trunk_interface": str(body.get("trunk_interface") or "bond1"), "vtep_ip": vtep, "router_id": vtep, "ospf_area": "0.0.0.0", "ospf_cost": 10, "ospf_auth_key_env": "IMMUTAVAULT_OSPF_KEY" if "IMMUTAVAULT_OSPF_KEY" in env_updates else None}}
        drcfg = {
            "enabled": True, "primary_site": primary, "dr_site": dr, "replica": replica, "rpo_max_minutes": int(body.get("rpo_max_minutes") or 1440), "auto_failover": False, "failure_threshold": 5, "check_interval_seconds": 60, "control_plane_site": dr, "primary_failure_quorum": 0, "maintenance_file": "/var/lib/immutavault/dr-maintenance",
            "fence": {"mode": "manual", "command_env": "IMMUTAVAULT_DR_FENCE_COMMAND", "verify_command_env": "IMMUTAVAULT_DR_FENCE_VERIFY_COMMAND"}, "primary_probes": [],
            "sites": [site(primary, str(body["primary_gateway_host"]), str(body["primary_vtep"])), site(dr, str(body["dr_gateway_host"]), str(body["dr_vtep"]))],
            "networks": [{"name": f"vlan-{vlan}", "vlan_id": vlan, "vni": vni, "subnet": str(body["subnet"]), "gateway_cidr": str(body["gateway_cidr"]), "mtu": int(body.get("mtu") or 1450)}],
            "workloads": [{
"name": vm, "source_platform": source_name, "target_platform": target_name, "boot_order": (index + 1) * 10, "health_checks": [], "restore_options": {}} for index, vm in enumerate(selected)],
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
        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 1024 * 1024:
                raise ValueError("request body too large")
            return json.loads(self.rfile.read(length) or b"{}")
        def _auth(self) -> bool:
            header=self.headers.get("Authorization",""); return header.startswith("Bearer ") and base.hmac.compare_digest(header[7:],token)
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
                elif path=="/api/v1/setup/dr/plan": result=manager.dr_plan( str(body.get("site") or ""))
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
