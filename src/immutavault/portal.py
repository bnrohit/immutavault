from __future__ import annotations

import fnmatch
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import ssl
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config, PortalUserConfig
from .engine import BackupEngine


UI = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Immutavault Recovery Portal</title>
<style>
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#0c1117;color:#e6edf3}.wrap{max-width:1250px;margin:auto;padding:24px}
.card{background:#151b23;border:1px solid #30363d;border-radius:12px;padding:18px;margin:14px 0}input,select,button{padding:9px;border-radius:8px;border:1px solid #444;background:#0d1117;color:#e6edf3}button{cursor:pointer}.danger{border-color:#da3633}.good{border-color:#238636}table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid #30363d;text-align:left;font-size:14px}.bad{color:#ff7b72}.ok{color:#7ee787}.muted{color:#8b949e}.pill{border:1px solid #444;border-radius:999px;padding:3px 8px;font-size:12px}</style></head>
<body><div class="wrap"><h1>Immutavault Recovery Portal</h1><p class="muted">Browse immutable recovery points, request recovery, approve with a second identity, and restore as a new VM. Existing production workloads are not overwritten automatically.</p>
<div class="card"><label>Access token <input id="token" type="password" size="48" autocomplete="off"></label> <button onclick="loadAll()">Connect</button> <span id="who" class="pill"></span></div>
<div class="card"><h2>Protected VMs</h2><table><thead><tr><th>Source</th><th>Hypervisor</th><th>VM</th><th>Latest point</th><th>Points</th><th></th></tr></thead><tbody id="vms"></tbody></table></div>
<div class="card"><h2>Recovery points</h2><div id="points" class="muted">Select a protected VM.</div></div>
<div class="card"><h2>Restore workflow</h2><div id="restores"></div></div>
<script>
let role='', platforms=[]; const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=async(path,opts={})=>{opts.headers=Object.assign({'Authorization':'Bearer '+document.getElementById('token').value,'Content-Type':'application/json'},opts.headers||{});const r=await fetch(path,opts);const t=await r.text();let j;try{j=JSON.parse(t)}catch{j={error:t}}if(!r.ok)throw new Error(j.error||r.statusText);return j};
async function loadAll(){try{const h=await api('/api/v1/health');role=h.role;who.textContent=h.user+' · '+h.role;platforms=await api('/api/v1/platforms');const v=await api('/api/v1/vms');vms.innerHTML=v.map(x=>`<tr><td>${esc(x.platform)}</td><td>${esc(x.platform_type)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.latest_point)}</td><td>${x.restore_points}</td><td><button onclick='pointsFor(${JSON.stringify(x.platform)},${JSON.stringify(x.vm_id)})'>Recovery points</button></td></tr>`).join('');await loadRestores()}catch(e){alert(e.message)}}
async function pointsFor(p,id){const rows=await api('/api/v1/recovery-points?platform='+encodeURIComponent(p)+'&vm_id='+encodeURIComponent(id));points.innerHTML='<table><tr><th>Captured</th><th>Snapshot</th><th>Verified</th><th>Immutable until</th><th>Recovery score</th><th>Change risk</th><th>Copies</th><th></th></tr>'+rows.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${esc(x.snapshot_id.slice(0,12))}</td><td class="${x.verified?'ok':'muted'}">${x.verified?'verified':'not full-restored yet'}</td><td>${esc(x.immutable_until)}</td><td class="${x.recovery_score>=75?'ok':'bad'}">${esc(x.recovery_score)} / 100 · ${esc(x.recovery_status)}</td><td class="${x.suspicious?'bad':'ok'}">${esc(x.suspicious?(x.suspicious_reason||'suspicious'):'normal')}</td><td>${esc((x.available_restore_sources||['primary']).join(', '))}</td><td>${['restore_operator','admin'].includes(role)?`<button onclick='requestRestore(${JSON.stringify(x.snapshot_id)},${JSON.stringify(x.vm_name)},${JSON.stringify(x.platform_type)},${JSON.stringify(x.available_restore_sources||['primary'])})'>Choose</button>`:''}</td></tr>`).join('')+'</table>'}
async function requestRestore(snapshot,name,type,sources){const allowed=platforms.filter(p=>p.type===type&&p.enabled);if(!allowed.length){alert('No enabled '+type+' restore target is configured');return}const choices=allowed.map(x=>x.name).join(', ');const target=prompt('Target platform ('+choices+'):',allowed[0].name);if(!target||!allowed.some(x=>x.name===target)){if(target)alert('Choose one of: '+choices);return}sources=(sources&&sources.length)?sources:['primary'];const source=prompt('Backup copy to restore from ('+sources.join(', ')+'):',sources[0]);if(!source||!sources.includes(source)){if(source)alert('Choose one of: '+sources.join(', '));return}const newName=prompt('New VM name:',name+'-restore');if(!newName)return;try{const r=await api('/api/v1/restores',{method:'POST',body:JSON.stringify({snapshot_id:snapshot,target_platform:target,target_name:newName,options:{source_repository:source}})});alert('Restore request #'+r.request_id+' created: '+r.status);await loadRestores()}catch(e){alert(e.message)}}
async function approve(id){try{await api('/api/v1/restores/'+id+'/approve',{method:'POST',body:'{}'});await loadRestores()}catch(e){alert(e.message)}}
async function execute(id){if(!confirm('Execute approved restore #'+id+' as a NEW VM?'))return;try{const r=await api('/api/v1/restores/'+id+'/execute',{method:'POST',body:'{}'});alert('Restore completed: '+JSON.stringify(r));await loadRestores()}catch(e){alert(e.message)}}
async function loadRestores(){try{const r=await api('/api/v1/restores');restores.innerHTML='<table><tr><th>ID</th><th>VM</th><th>Target</th><th>Status</th><th>Requester</th><th>Approver</th><th>Actions</th></tr>'+r.map(x=>{let a='';if(x.status==='pending_approval'&&['approver','admin'].includes(role))a+=`<button class="good" onclick="approve(${x.id})">Approve</button> `;if(['approved','ready'].includes(x.status)&&['restore_operator','admin'].includes(role))a+=`<button class="danger" onclick="execute(${x.id})">Execute</button>`;return `<tr><td>${x.id}</td><td>${esc(x.vm_name)}</td><td>${esc(x.target_platform)} / ${esc(x.target_name)}</td><td>${esc(x.status)}</td><td>${esc(x.requester)}</td><td>${esc(x.approved_by||'')}</td><td>${a}</td></tr>`}).join('')+'</table>'}catch(e){restores.textContent=e.message}}
</script></div></body></html>'''


class Portal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = BackupEngine(cfg)

    def users(self) -> list[tuple[PortalUserConfig, str]]:
        resolved: list[tuple[PortalUserConfig, str]] = []
        for user in self.cfg.portal.users:
            token = os.getenv(user.token_env)
            if token:
                resolved.append((user, token))
        return resolved

    def serve(self) -> None:
        users = self.users()
        if not users:
            raise RuntimeError("portal has no active users: configure portal.users and set their token_env variables")
        portal = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ImmutavaultPortal/0.3"

            def log_message(self, fmt: str, *args: Any) -> None:
                print(f"portal {self.client_address[0]} {fmt % args}")

            def _json(self, code: int, value: Any) -> None:
                payload = json.dumps(value, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 1024 * 1024:
                    raise ValueError("request body too large")
                return json.loads(self.rfile.read(length) or b"{}")

            def _auth(self) -> PortalUserConfig | None:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return None
                token = header[7:]
                for user, expected in users:
                    if hmac.compare_digest(token, expected):
                        return user
                return None

            def _allowed_point(self, user: PortalUserConfig, point: dict[str, Any]) -> bool:
                return any(fnmatch.fnmatch(point["platform"], p) for p in user.sources) and any(
                    fnmatch.fnmatch(point["vm_name"], p) for p in user.vm_patterns
                )

            def _require(self, roles: set[str]) -> PortalUserConfig | None:
                user = self._auth()
                if not user:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid or missing bearer token"}); return None
                if user.role not in roles:
                    self._json(HTTPStatus.FORBIDDEN, {"error": f"role {user.role} is not permitted"}); return None
                return user

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    payload = UI.encode(); self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store"); self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'")
                    self.end_headers(); self.wfile.write(payload); return
                user = self._require({"viewer", "restore_operator", "approver", "admin"})
                if not user: return
                try:
                    if parsed.path == "/api/v1/health":
                        self._json(200, {"status": "ok", "user": user.name, "role": user.role}); return
                    if parsed.path == "/api/v1/system-health":
                        if user.role != "admin": self._json(403, {"error":"admin role required"}); return
                        status = portal.engine.status(); self._json(200 if status.get("healthy") else 503, status); return
                    if parsed.path == "/api/v1/platforms":
                        self._json(200, [{"name": p.name, "type": p.type, "enabled": p.enabled} for p in portal.cfg.platforms]); return
                    if parsed.path == "/api/v1/storage-targets":
                        self._json(200, portal.engine.storage_targets()); return
                    if parsed.path == "/api/v1/vms":
                        rows = []
                        for vm in portal.engine.state.list_vms():
                            if self._allowed_point(user, {"platform": vm["platform"], "vm_name": vm["vm_name"]}): rows.append(vm)
                        self._json(200, rows); return
                    if parsed.path == "/api/v1/recovery-points":
                        q = parse_qs(parsed.query)
                        rows = portal.engine.list_recovery_points(platform=(q.get("platform") or [None])[0], vm_id=(q.get("vm_id") or [None])[0])
                        self._json(200, [r for r in rows if self._allowed_point(user, r)]); return
                    if parsed.path == "/api/v1/restores":
                        rows = portal.engine.state.list_restore_requests()
                        if user.role not in {"admin", "approver"}: rows = [r for r in rows if r["requester"] == user.name]
                        self._json(200, rows); return
                    if parsed.path == "/api/v1/audit":
                        if user.role != "admin": self._json(403, {"error":"admin role required"}); return
                        self._json(200, portal.engine.state.list_audit()); return
                    if parsed.path == "/api/v1/audit/verify":
                        if user.role != "admin": self._json(403, {"error":"admin role required"}); return
                        ok, errors = portal.engine.state.verify_audit_chain()
                        self._json(200 if ok else 409, {"valid": ok, "errors": errors}); return
                    self._json(404, {"error": "not found"})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path == "/api/v1/restores":
                        user = self._require({"restore_operator", "admin"})
                        if not user: return
                        body = self._body(); point = portal.engine.state.get_point(str(body.get("snapshot_id", "")))
                        if not point: self._json(404,{"error":"recovery point not found"}); return
                        if not self._allowed_point(user, point): self._json(403,{"error":"recovery point outside your scope"}); return
                        rid = portal.engine.request_restore(snapshot_id=point["snapshot_id"], requester=user.name,
                            target_platform=str(body.get("target_platform", "")), target_name=body.get("target_name"), options=dict(body.get("options") or {}))
                        req = portal.engine.state.get_restore_request(rid) or {}; self._json(201, {"request_id": rid, "status": req.get("status")}); return
                    if parsed.path.startswith("/api/v1/restores/") and parsed.path.endswith("/approve"):
                        user = self._require({"approver", "admin"})
                        if not user: return
                        rid = int(parsed.path.split("/")[4]); portal.engine.approve_restore(rid, user.name)
                        self._json(200, portal.engine.state.get_restore_request(rid)); return
                    if parsed.path.startswith("/api/v1/restores/") and parsed.path.endswith("/execute"):
                        user = self._require({"restore_operator", "admin"})
                        if not user: return
                        rid = int(parsed.path.split("/")[4]); req = portal.engine.state.get_restore_request(rid)
                        if not req: self._json(404,{"error":"restore request not found"}); return
                        if user.role != "admin" and req["requester"] != user.name: self._json(403,{"error":"only an admin can execute another user's restore"}); return
                        self._json(200, portal.engine.execute_restore(rid, actor=user.name)); return
                    if parsed.path.startswith("/api/v1/recovery-points/") and parsed.path.endswith("/verify"):
                        user = self._require({"admin"})
                        if not user: return
                        snapshot = parsed.path.split("/")[4]
                        self._json(200, {"snapshot_id": snapshot, "verified": portal.engine.verify_recovery_point(snapshot, actor=user.name)}); return
                    if parsed.path.startswith("/api/v1/recovery-points/") and parsed.path.endswith("/hold"):
                        user = self._require({"admin"})
                        if not user: return
                        snapshot = parsed.path.split("/")[4]; body = self._body()
                        until = portal.engine.hold_recovery_point(snapshot, actor=user.name, days=int(body.get("days", 30)), reason=str(body.get("reason", "manual hold")))
                        self._json(200, {"snapshot_id": snapshot, "immutable_until": until}); return
                    self._json(404, {"error": "not found"})
                except (ValueError, PermissionError) as exc:
                    self._json(400 if isinstance(exc, ValueError) else 403, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})

        httpd = ThreadingHTTPServer((self.cfg.portal.listen, self.cfg.portal.port), Handler)
        if self.cfg.portal.tls_cert and self.cfg.portal.tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(self.cfg.portal.tls_cert, self.cfg.portal.tls_key); httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print(f"Immutavault portal listening on {self.cfg.portal.listen}:{self.cfg.portal.port}")
        httpd.serve_forever()
