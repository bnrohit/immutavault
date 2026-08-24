from __future__ import annotations

import fnmatch
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import mimetypes
import os
import ssl
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config, PortalUserConfig
from .consistency import point_consistency
from .engine import BackupEngine
from .flr import FLRManager


UI = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Immutavault Recovery Portal</title>
<style>
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#0c1117;color:#e6edf3}.wrap{max-width:1320px;margin:auto;padding:24px}
.card{background:#151b23;border:1px solid #30363d;border-radius:12px;padding:18px;margin:14px 0}input,select,button{padding:9px;border-radius:8px;border:1px solid #444;background:#0d1117;color:#e6edf3}button{cursor:pointer}.danger{border-color:#da3633}.good{border-color:#238636}table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid #30363d;text-align:left;font-size:14px}.bad{color:#ff7b72}.ok{color:#7ee787}.warn{color:#d29922}.muted{color:#8b949e}.pill{border:1px solid #444;border-radius:999px;padding:3px 8px;font-size:12px}.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}</style></head>
<body><div class="wrap"><h1>Immutavault Recovery Portal</h1><p class="muted">Browse immutable recovery points, recover individual files through short-lived read-only mounts, or restore a complete VM as a new workload. Existing production workloads are never overwritten automatically.</p>
<div class="card"><label>Access token <input id="token" type="password" size="48" autocomplete="off"></label> <button onclick="loadAll()">Connect</button> <span id="who" class="pill"></span> <span id="flrState" class="pill"></span></div>
<div class="card"><h2>Protected VMs</h2><table><thead><tr><th>Source</th><th>Hypervisor</th><th>VM</th><th>Latest point</th><th>Points</th><th></th></tr></thead><tbody id="vms"></tbody></table></div>
<div class="card"><h2>Recovery points</h2><div id="points" class="muted">Select a protected VM.</div></div>
<div class="card"><h2>File-level recovery</h2><p class="muted">FLR mounts the selected immutable recovery point read-only and exposes the guest filesystem only for this authenticated session. Symlinks and special files are not downloadable.</p><div class="row"><button id="flrUp" onclick="flrUp()" disabled>Up</button><button id="flrClose" onclick="closeFlr()" disabled>Close session</button><span id="flrPath" class="path muted">No FLR session.</span></div><div id="files"></div></div>
<div class="card"><h2>Restore workflow</h2><div id="restores"></div></div>
<script>
let role='', platforms=[], flrSession=null, flrPath='/'; const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const authHeaders=()=>({'Authorization':'Bearer '+document.getElementById('token').value});
const api=async(path,opts={})=>{opts.headers=Object.assign(authHeaders(),{'Content-Type':'application/json'},opts.headers||{});const r=await fetch(path,opts);const t=await r.text();let j;try{j=JSON.parse(t)}catch{j={error:t}}if(!r.ok)throw new Error(j.error||r.statusText);return j};
async function loadAll(){try{const h=await api('/api/v1/health');role=h.role;who.textContent=h.user+' · '+h.role;flrState.textContent=h.flr&&h.flr.enabled?'FLR enabled':'FLR disabled';platforms=await api('/api/v1/platforms');const v=await api('/api/v1/vms');vms.innerHTML=v.map(x=>`<tr><td>${esc(x.platform)}</td><td>${esc(x.platform_type)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.latest_point)}</td><td>${x.restore_points}</td><td><button onclick='pointsFor(${JSON.stringify(x.platform)},${JSON.stringify(x.vm_id)})'>Recovery points</button></td></tr>`).join('');await loadRestores()}catch(e){alert(e.message)}}
function consistency(x){const c=x.application_consistency||{};const state=c.state||'unknown';const cls=c.application_consistent?'ok':(state==='crash-consistent'?'warn':'muted');return `<span class="${cls}" title="${esc(c.detail||c.method||'')}">${esc(state)}</span>`}
async function pointsFor(p,id){const rows=await api('/api/v1/recovery-points?platform='+encodeURIComponent(p)+'&vm_id='+encodeURIComponent(id));points.innerHTML='<table><tr><th>Captured</th><th>Snapshot</th><th>Consistency</th><th>Verified</th><th>Immutable until</th><th>Recovery score</th><th>Change risk</th><th>Copies</th><th>Actions</th></tr>'+rows.map(x=>{let a='';if(['restore_operator','admin'].includes(role)){a+=`<button onclick='openFlr(${JSON.stringify(x.snapshot_id)})'>Files</button> `;a+=`<button onclick='requestRestore(${JSON.stringify(x.snapshot_id)},${JSON.stringify(x.vm_name)},${JSON.stringify(x.platform_type)},${JSON.stringify(x.available_restore_sources||['primary'])})'>Full VM</button>`}return `<tr><td>${esc(x.created_at)}</td><td>${esc(x.snapshot_id.slice(0,12))}</td><td>${consistency(x)}</td><td class="${x.verified?'ok':'muted'}">${x.verified?'verified':'not full-restored yet'}</td><td>${esc(x.immutable_until)}</td><td class="${x.recovery_score>=75?'ok':'bad'}">${esc(x.recovery_score)} / 100 · ${esc(x.recovery_status)}</td><td class="${x.suspicious?'bad':'ok'}">${esc(x.suspicious?(x.suspicious_reason||'suspicious'):'normal')}</td><td>${esc((x.available_restore_sources||['primary']).join(', '))}</td><td>${a}</td></tr>`}).join('')+'</table>'}
async function openFlr(snapshot){try{if(flrSession)await closeFlr(false);const s=await api('/api/v1/flr/sessions',{method:'POST',body:JSON.stringify({snapshot_id:snapshot})});flrSession=s.session_id;flrPath='/';flrClose.disabled=false;await browseFlr('/')}catch(e){alert(e.message)}}
async function browseFlr(path){if(!flrSession)return;try{const d=await api('/api/v1/flr/sessions/'+encodeURIComponent(flrSession)+'/browse?path='+encodeURIComponent(path));flrPath=d.path;flrPathEl();files.innerHTML='<table><tr><th>Name</th><th>Type</th><th>Size</th><th>Modified</th><th></th></tr>'+d.entries.map(x=>{const next=(flrPath==='/'?'':flrPath)+'/'+x.name;let a='';if(x.type==='directory')a=`<button onclick='browseFlr(${JSON.stringify(next)})'>Open</button>`;else if(x.downloadable)a=`<button onclick='downloadFlr(${JSON.stringify(next)},${JSON.stringify(x.name)})'>Download</button>`;return `<tr><td>${esc(x.name)}</td><td>${esc(x.type)}</td><td>${x.size==null?'':esc(x.size)}</td><td>${esc(x.modified)}</td><td>${a}</td></tr>`}).join('')+'</table>'}catch(e){files.textContent=e.message}}
function flrPathEl(){document.getElementById('flrPath').textContent=flrSession?flrPath:'No FLR session.';flrUp.disabled=!flrSession||flrPath==='/'}
function flrUp(){if(!flrSession||flrPath==='/')return;const p=flrPath.split('/').filter(Boolean);p.pop();browseFlr('/'+p.join('/'))}
async function downloadFlr(path,name){if(!flrSession)return;try{const r=await fetch('/api/v1/flr/sessions/'+encodeURIComponent(flrSession)+'/download?path='+encodeURIComponent(path),{headers:authHeaders()});if(!r.ok){let msg=await r.text();try{msg=JSON.parse(msg).error||msg}catch{}throw new Error(msg)}const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name||'recovered-file';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}catch(e){alert(e.message)}}
async function closeFlr(show=true){if(!flrSession)return;const sid=flrSession;try{await api('/api/v1/flr/sessions/'+encodeURIComponent(sid),{method:'DELETE'})}catch(e){if(show)alert(e.message)}finally{flrSession=null;flrPath='/';files.innerHTML='';flrClose.disabled=true;flrPathEl()}}
async function requestRestore(snapshot,name,type,sources){const allowed=platforms.filter(p=>p.type===type&&p.enabled);if(!allowed.length){alert('No enabled '+type+' restore target is configured');return}const choices=allowed.map(x=>x.name).join(', ');const target=prompt('Target platform ('+choices+'):',allowed[0].name);if(!target||!allowed.some(x=>x.name===target)){if(target)alert('Choose one of: '+choices);return}sources=(sources&&sources.length)?sources:['primary'];const source=prompt('Backup copy to restore from ('+sources.join(', ')+'):',sources[0]);if(!source||!sources.includes(source)){if(source)alert('Choose one of: '+sources.join(', '));return}const newName=prompt('New VM name:',name+'-restore');if(!newName)return;try{const r=await api('/api/v1/restores',{method:'POST',body:JSON.stringify({snapshot_id:snapshot,target_platform:target,target_name:newName,options:{source_repository:source}})});alert('Restore request #'+r.request_id+' created: '+r.status);await loadRestores()}catch(e){alert(e.message)}}
async function approve(id){try{await api('/api/v1/restores/'+id+'/approve',{method:'POST',body:'{}'});await loadRestores()}catch(e){alert(e.message)}}
async function execute(id){if(!confirm('Execute approved restore #'+id+' as a NEW VM?'))return;try{const r=await api('/api/v1/restores/'+id+'/execute',{method:'POST',body:'{}'});alert('Restore completed: '+JSON.stringify(r));await loadRestores()}catch(e){alert(e.message)}}
async function loadRestores(){try{const r=await api('/api/v1/restores');restores.innerHTML='<table><tr><th>ID</th><th>VM</th><th>Target</th><th>Status</th><th>Requester</th><th>Approver</th><th>Actions</th></tr>'+r.map(x=>{let a='';if(x.status==='pending_approval'&&['approver','admin'].includes(role))a+=`<button class="good" onclick="approve(${x.id})">Approve</button> `;if(['approved','ready'].includes(x.status)&&['restore_operator','admin'].includes(role))a+=`<button class="danger" onclick="execute(${x.id})">Execute</button>`;return `<tr><td>${x.id}</td><td>${esc(x.vm_name)}</td><td>${esc(x.target_platform)} / ${esc(x.target_name)}</td><td>${esc(x.status)}</td><td>${esc(x.requester)}</td><td>${esc(x.approved_by||'')}</td><td>${a}</td></tr>`}).join('')+'</table>'}catch(e){restores.textContent=e.message}}
window.addEventListener('beforeunload',()=>{if(flrSession)navigator.sendBeacon('/api/v1/flr/sessions/'+encodeURIComponent(flrSession))});
</script></div></body></html>'''


class Portal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = BackupEngine(cfg)
        self.flr = FLRManager(cfg, self.engine.repo)

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
        listen = self.cfg.portal.listen.strip()
        loopback = listen in {"127.0.0.1", "::1", "localhost"}
        if not loopback and not (self.cfg.portal.tls_cert and self.cfg.portal.tls_key):
            raise RuntimeError("portal refuses non-loopback plaintext exposure; configure tls_cert/tls_key or bind to loopback")
        portal = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ImmutavaultPortal/0.8"

            def log_message(self, fmt: str, *args: Any) -> None:
                print(f"portal {self.client_address[0]} {fmt % args}")

            def _security_headers(self) -> None:
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

            def _json(self, code: int, value: Any) -> None:
                payload = json.dumps(value, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self._security_headers()
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
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid or missing bearer token"})
                    return None
                if user.role not in roles:
                    self._json(HTTPStatus.FORBIDDEN, {"error": f"role {user.role} is not permitted"})
                    return None
                return user

            def _point(self, snapshot_id: str, user: PortalUserConfig) -> dict[str, Any] | None:
                point = portal.engine.state.get_point(snapshot_id)
                if not point:
                    self._json(404, {"error": "recovery point not found"})
                    return None
                if not self._allowed_point(user, point):
                    self._json(403, {"error": "recovery point outside your scope"})
                    return None
                return point

            def _send_file(self, file) -> None:
                mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                safe_name = file.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(file.size))
                self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
                self._security_headers()
                self.end_headers()
                portal.flr.stream_file(file, self.wfile)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    payload = UI.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline' blob:; object-src 'none'; frame-ancestors 'none'")
                    self._security_headers()
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                user = self._require({"viewer", "restore_operator", "approver", "admin"})
                if not user:
                    return
                try:
                    if parsed.path == "/api/v1/health":
                        self._json(200, {"status": "ok", "user": user.name, "role": user.role, "flr": portal.flr.status()})
                        return
                    if parsed.path == "/api/v1/system-health":
                        if user.role != "admin":
                            self._json(403, {"error": "admin role required"}); return
                        status = portal.engine.status()
                        status["flr"] = portal.flr.status()
                        self._json(200 if status.get("healthy") else 503, status); return
                    if parsed.path == "/api/v1/platforms":
                        self._json(200, [{"name": p.name, "type": p.type, "enabled": p.enabled} for p in portal.cfg.platforms]); return
                    if parsed.path == "/api/v1/storage-targets":
                        self._json(200, portal.engine.storage_targets()); return
                    if parsed.path == "/api/v1/vms":
                        rows = []
                        for vm in portal.engine.state.list_vms():
                            if self._allowed_point(user, {"platform": vm["platform"], "vm_name": vm["vm_name"]}):
                                rows.append(vm)
                        self._json(200, rows); return
                    if parsed.path == "/api/v1/recovery-points":
                        q = parse_qs(parsed.query)
                        rows = portal.engine.list_recovery_points(platform=(q.get("platform") or [None])[0], vm_id=(q.get("vm_id") or [None])[0])
                        allowed = []
                        for row in rows:
                            if self._allowed_point(user, row):
                                row["application_consistency"] = point_consistency(row)
                                allowed.append(row)
                        self._json(200, allowed); return
                    if parsed.path == "/api/v1/restores":
                        rows = portal.engine.state.list_restore_requests()
                        if user.role not in {"admin", "approver"}:
                            rows = [r for r in rows if r["requester"] == user.name]
                        self._json(200, rows); return
                    if parsed.path.startswith("/api/v1/flr/sessions/") and parsed.path.endswith("/browse"):
                        if user.role not in {"restore_operator", "admin"}:
                            self._json(403, {"error": "restore_operator or admin role required"}); return
                        sid = parsed.path.split("/")[5]
                        q = parse_qs(parsed.query)
                        value = portal.flr.list_directory(
                            sid,
                            (q.get("path") or ["/"])[0],
                            actor=user.name,
                            admin=user.role == "admin",
                        )
                        self._json(200, value); return
                    if parsed.path.startswith("/api/v1/flr/sessions/") and parsed.path.endswith("/download"):
                        if user.role not in {"restore_operator", "admin"}:
                            self._json(403, {"error": "restore_operator or admin role required"}); return
                        sid = parsed.path.split("/")[5]
                        q = parse_qs(parsed.query)
                        requested_path = (q.get("path") or [""])[0]
                        file = portal.flr.open_file(sid, requested_path, actor=user.name, admin=user.role == "admin")
                        portal.engine.state.audit(user.name, "flr.file.download", "flr_session", sid, {
                            "path": requested_path, "size": file.size,
                        })
                        self._send_file(file); return
                    if parsed.path == "/api/v1/audit":
                        if user.role != "admin":
                            self._json(403, {"error": "admin role required"}); return
                        self._json(200, portal.engine.state.list_audit()); return
                    if parsed.path == "/api/v1/audit/verify":
                        if user.role != "admin":
                            self._json(403, {"error": "admin role required"}); return
                        ok, errors = portal.engine.state.verify_audit_chain()
                        self._json(200 if ok else 409, {"valid": ok, "errors": errors}); return
                    self._json(404, {"error": "not found"})
                except (ValueError, PermissionError) as exc:
                    self._json(400 if isinstance(exc, ValueError) else 403, {"error": str(exc)})
                except Exception as exc:
                    print(f"portal internal GET error: {type(exc).__name__}: {exc}")
                    self._json(500, {"error": "internal server error"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path == "/api/v1/flr/sessions":
                        user = self._require({"restore_operator", "admin"})
                        if not user: return
                        body = self._body()
                        point = self._point(str(body.get("snapshot_id", "")), user)
                        if not point: return
                        session = portal.flr.open_session(point, actor=user.name)
                        portal.engine.state.audit(user.name, "flr.session.open", "recovery_point", point["snapshot_id"], {
                            "session_id": session["session_id"], "read_only": True,
                        })
                        self._json(201, session); return
                    if parsed.path == "/api/v1/restores":
                        user = self._require({"restore_operator", "admin"})
                        if not user: return
                        body = self._body()
                        point = self._point(str(body.get("snapshot_id", "")), user)
                        if not point: return
                        rid = portal.engine.request_restore(
                            snapshot_id=point["snapshot_id"], requester=user.name,
                            target_platform=str(body.get("target_platform", "")), target_name=body.get("target_name"),
                            options=dict(body.get("options") or {}),
                        )
                        req = portal.engine.state.get_restore_request(rid) or {}
                        self._json(201, {"request_id": rid, "status": req.get("status")}); return
                    if parsed.path.startswith("/api/v1/restores/") and parsed.path.endswith("/approve"):
                        user = self._require({"approver", "admin"})
                        if not user: return
                        rid = int(parsed.path.split("/")[4])
                        portal.engine.approve_restore(rid, user.name)
                        self._json(200, portal.engine.state.get_restore_request(rid)); return
                    if parsed.path.startswith("/api/v1/restores/") and parsed.path.endswith("/execute"):
                        user = self._require({"restore_operator", "admin"})
                        if not user: return
                        rid = int(parsed.path.split("/")[4])
                        req = portal.engine.state.get_restore_request(rid)
                        if not req:
                            self._json(404, {"error": "restore request not found"}); return
                        if user.role != "admin" and req["requester"] != user.name:
                            self._json(403, {"error": "only an admin can execute another user's restore"}); return
                        self._json(200, portal.engine.execute_restore(rid, actor=user.name)); return
                    if parsed.path.startswith("/api/v1/recovery-points/") and parsed.path.endswith("/verify"):
                        user = self._require({"admin"})
                        if not user: return
                        snapshot = parsed.path.split("/")[4]
                        self._json(200, {"snapshot_id": snapshot, "verified": portal.engine.verify_recovery_point(snapshot, actor=user.name)}); return
                    if parsed.path.startswith("/api/v1/recovery-points/") and parsed.path.endswith("/hold"):
                        user = self._require({"admin"})
                        if not user: return
                        snapshot = parsed.path.split("/")[4]
                        body = self._body()
                        until = portal.engine.hold_recovery_point(
                            snapshot, actor=user.name, days=int(body.get("days", 30)), reason=str(body.get("reason", "manual hold"))
                        )
                        self._json(200, {"snapshot_id": snapshot, "immutable_until": until}); return
                    self._json(404, {"error": "not found"})
                except (ValueError, PermissionError) as exc:
                    self._json(400 if isinstance(exc, ValueError) else 403, {"error": str(exc)})
                except Exception as exc:
                    print(f"portal internal POST error: {type(exc).__name__}: {exc}")
                    self._json(500, {"error": "internal server error"})

            def do_DELETE(self) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path.startswith("/api/v1/flr/sessions/"):
                        user = self._require({"restore_operator", "admin"})
                        if not user: return
                        parts = parsed.path.strip("/").split("/")
                        if len(parts) != 5:
                            self._json(404, {"error": "not found"}); return
                        sid = parts[4]
                        portal.flr.close_session(sid, actor=user.name if user.role != "admin" else None, force=user.role == "admin")
                        portal.engine.state.audit(user.name, "flr.session.close", "flr_session", sid, {})
                        self._json(200, {"session_id": sid, "status": "closed"}); return
                    self._json(404, {"error": "not found"})
                except (ValueError, PermissionError) as exc:
                    self._json(400 if isinstance(exc, ValueError) else 403, {"error": str(exc)})
                except Exception as exc:
                    print(f"portal internal DELETE error: {type(exc).__name__}: {exc}")
                    self._json(500, {"error": "internal server error"})

        httpd = ThreadingHTTPServer((self.cfg.portal.listen, self.cfg.portal.port), Handler)
        if self.cfg.portal.tls_cert and self.cfg.portal.tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(self.cfg.portal.tls_cert, self.cfg.portal.tls_key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print(f"Immutavault portal listening on {self.cfg.portal.listen}:{self.cfg.portal.port}")
        httpd.serve_forever()
