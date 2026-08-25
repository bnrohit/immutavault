from __future__ import annotations

import fnmatch
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import mimetypes
import os
import ssl
from typing import Any
from urllib.parse import parse_qs, urlparse

from .consistency import point_consistency
from .engine import BackupEngine
from .enterprise_auth import Identity, OIDCClient, SignedToken
from .enterprise_config import EnterpriseConfig
from .enterprise_ops import EnterpriseOps, WebSocketTelemetryServer
from .flr import FLRManager


UI = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Immutavault Enterprise Operations</title>
<style>
body{font-family:system-ui,Segoe UI,sans-serif;margin:0;background:#0c1117;color:#e6edf3}.wrap{max-width:1400px;margin:auto;padding:24px}.card{background:#151b23;border:1px solid #30363d;border-radius:12px;padding:18px;margin:14px 0}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.metric{font-size:28px;font-weight:700}input,select,button,a.btn{padding:9px;border-radius:8px;border:1px solid #444;background:#0d1117;color:#e6edf3;text-decoration:none}button,a.btn{cursor:pointer}.danger{border-color:#da3633}.good{border-color:#238636}table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid #30363d;text-align:left;font-size:14px}.bad{color:#ff7b72}.ok{color:#7ee787}.warn{color:#d29922}.muted{color:#8b949e}.pill{border:1px solid #444;border-radius:999px;padding:3px 8px;font-size:12px}.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.bar{width:180px;height:10px;border:1px solid #444;border-radius:99px;overflow:hidden}.bar>span{display:block;height:100%;background:#238636}.hidden{display:none}</style></head>
<body><div class="wrap"><h1>Immutavault Enterprise Operations</h1><p class="muted">Tenant-scoped immutable recovery, live backup operations, Entra/OIDC identity and audit-first recovery.</p>
<div class="card"><div class="row"><a class="btn" id="oidcLogin" href="/auth/login">Sign in with OIDC / Entra ID</a><label id="localAuth">Break-glass token <input id="token" type="password" size="38" autocomplete="off"></label><button onclick="loadAll()">Connect</button><a class="btn" href="/auth/logout">Sign out</a><span id="who" class="pill"></span><span id="scope" class="pill"></span><span id="live" class="pill">live: disconnected</span></div></div>
<div class="grid"><div class="card"><div class="muted">Recovery points</div><div id="mPoints" class="metric">—</div></div><div class="card"><div class="muted">Verified</div><div id="mVerified" class="metric">—</div></div><div class="card"><div class="muted">Suspicious</div><div id="mSuspicious" class="metric">—</div></div><div class="card"><div class="muted">Running jobs</div><div id="mRunning" class="metric">—</div></div></div>
<div class="card"><h2>Live operations</h2><p class="muted">Running percentages are metadata-based estimates until the authoritative job commits successfully.</p><div id="jobs">Connect to view scoped jobs.</div></div>
<div class="card"><h2>Protected VMs</h2><table><thead><tr><th>Tenant</th><th>Source</th><th>Hypervisor</th><th>VM</th><th>Latest point</th><th>Points</th><th></th></tr></thead><tbody id="vms"></tbody></table></div>
<div class="card"><h2>Recovery points</h2><div id="points" class="muted">Select a protected VM.</div></div>
<div class="card"><h2>File-level recovery</h2><p class="muted">Short-lived read-only FLR session. Guest symlinks and special files are never downloadable.</p><div class="row"><button id="flrUp" onclick="flrUp()" disabled>Up</button><button id="flrClose" onclick="closeFlr()" disabled>Close session</button><span id="flrPath" class="path muted">No FLR session.</span></div><div id="files"></div></div>
<div class="card"><h2>Restore workflow</h2><div id="restores"></div></div>
<script>
let role='',platforms=[],flrSession=null,flrPath='/',ws=null,wsURL=null;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const authHeaders=()=>{const t=document.getElementById('token').value;return t?{'Authorization':'Bearer '+t}:{}};
const api=async(path,opts={})=>{opts.credentials='same-origin';opts.headers=Object.assign(authHeaders(),{'Content-Type':'application/json'},opts.headers||{});const r=await fetch(path,opts);const t=await r.text();let j;try{j=JSON.parse(t)}catch{j={error:t}}if(!r.ok)throw new Error(j.error||r.statusText);return j};
async function authConfig(){try{const c=await fetch('/api/v1/auth-config',{credentials:'same-origin'}).then(r=>r.json());oidcLogin.classList.toggle('hidden',!c.oidc_enabled);localAuth.classList.toggle('hidden',!c.local_tokens_allowed)}catch{}}
function metrics(s){mPoints.textContent=s.recovery_points??0;mVerified.textContent=s.verified??0;mSuspicious.textContent=s.suspicious??0;mRunning.textContent=s.running_jobs??0}
function jobsTable(rows){jobs.innerHTML='<table><tr><th>Tenant</th><th>VM</th><th>Source</th><th>Stage</th><th>Progress</th><th>Status</th><th>Elapsed</th></tr>'+rows.map(x=>`<tr><td>${esc(x.tenant)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.platform)}</td><td>${esc(x.stage)}</td><td><div class="row"><div class="bar"><span style="width:${Number(x.progress_percent||0)}%"></span></div>${esc(x.progress_percent)}%${x.progress_estimated?' est.':''}</div></td><td class="${x.status==='success'?'ok':x.status==='failed'?'bad':'warn'}">${esc(x.status)}</td><td>${x.elapsed_seconds==null?'':esc(x.elapsed_seconds+'s')}</td></tr>`).join('')+'</table>'}
async function loadAll(){try{const h=await api('/api/v1/health');role=h.identity.role;who.textContent=h.identity.name+' · '+role+(h.identity.mfa?' · MFA':'');scope.textContent='tenant: '+h.identity.tenants.join(', ');wsURL=h.websocket_url||null;platforms=await api('/api/v1/platforms');const o=await api('/api/v1/ops/snapshot');metrics(o.summary);jobsTable(o.jobs);const v=await api('/api/v1/vms');vms.innerHTML=v.map(x=>`<tr><td>${esc(x.tenant)}</td><td>${esc(x.platform)}</td><td>${esc(x.platform_type)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.latest_point)}</td><td>${x.restore_points}</td><td><button onclick='pointsFor(${JSON.stringify(x.platform)},${JSON.stringify(x.vm_id)})'>Recovery points</button></td></tr>`).join('');await loadRestores();await connectWS()}catch(e){alert(e.message)}}
async function connectWS(){if(!wsURL)return;try{if(ws)ws.close();const t=await api('/api/v1/ws-ticket',{method:'POST',body:'{}'});ws=new WebSocket(wsURL+(wsURL.includes('?')?'&':'?')+'ticket='+encodeURIComponent(t.ticket));ws.onopen=()=>live.textContent='live: connected';ws.onclose=()=>live.textContent='live: disconnected';ws.onerror=()=>live.textContent='live: error';ws.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='operations'){metrics(d.summary);jobsTable(d.jobs)}}}catch(e){live.textContent='live: '+e.message}}
function consistency(x){const c=x.application_consistency||{};const state=c.state||'unknown';const cls=c.application_consistent?'ok':(state==='crash-consistent'?'warn':'muted');return `<span class="${cls}" title="${esc(c.detail||c.method||'')}">${esc(state)}</span>`}
async function pointsFor(p,id){const rows=await api('/api/v1/recovery-points?platform='+encodeURIComponent(p)+'&vm_id='+encodeURIComponent(id));points.innerHTML='<table><tr><th>Captured</th><th>Snapshot</th><th>Consistency</th><th>Verified</th><th>Immutable until</th><th>Score</th><th>Change risk</th><th>Copies</th><th>Actions</th></tr>'+rows.map(x=>{let a='';if(['restore_operator','admin'].includes(role)){a+=`<button onclick='openFlr(${JSON.stringify(x.snapshot_id)})'>Files</button> `;a+=`<button onclick='requestRestore(${JSON.stringify(x.snapshot_id)},${JSON.stringify(x.vm_name)},${JSON.stringify(x.platform_type)},${JSON.stringify(x.available_restore_sources||['primary'])})'>Full VM</button>`}return `<tr><td>${esc(x.created_at)}</td><td>${esc(x.snapshot_id.slice(0,12))}</td><td>${consistency(x)}</td><td class="${x.verified?'ok':'muted'}">${x.verified?'verified':'not full-restored yet'}</td><td>${esc(x.immutable_until)}</td><td>${esc(x.recovery_score)} · ${esc(x.recovery_status)}</td><td class="${x.suspicious?'bad':'ok'}">${esc(x.suspicious?(x.suspicious_reason||'suspicious'):'normal')}</td><td>${esc((x.available_restore_sources||['primary']).join(', '))}</td><td>${a}</td></tr>`}).join('')+'</table>'}
async function openFlr(snapshot){try{if(flrSession)await closeFlr(false);const s=await api('/api/v1/flr/sessions',{method:'POST',body:JSON.stringify({snapshot_id:snapshot})});flrSession=s.session_id;flrPath='/';flrClose.disabled=false;await browseFlr('/')}catch(e){alert(e.message)}}
async function browseFlr(path){if(!flrSession)return;try{const d=await api('/api/v1/flr/sessions/'+encodeURIComponent(flrSession)+'/browse?path='+encodeURIComponent(path));flrPath=d.path;flrPathEl();files.innerHTML='<table><tr><th>Name</th><th>Type</th><th>Size</th><th>Modified</th><th></th></tr>'+d.entries.map(x=>{const next=(flrPath==='/'?'':flrPath)+'/'+x.name;let a='';if(x.type==='directory')a=`<button onclick='browseFlr(${JSON.stringify(next)})'>Open</button>`;else if(x.downloadable)a=`<button onclick='downloadFlr(${JSON.stringify(next)},${JSON.stringify(x.name)})'>Download</button>`;return `<tr><td>${esc(x.name)}</td><td>${esc(x.type)}</td><td>${x.size==null?'':esc(x.size)}</td><td>${esc(x.modified)}</td><td>${a}</td></tr>`}).join('')+'</table>'}catch(e){files.textContent=e.message}}
function flrPathEl(){document.getElementById('flrPath').textContent=flrSession?flrPath:'No FLR session.';flrUp.disabled=!flrSession||flrPath==='/'}
function flrUp(){if(!flrSession||flrPath==='/')return;const p=flrPath.split('/').filter(Boolean);p.pop();browseFlr('/'+p.join('/'))}
async function downloadFlr(path,name){if(!flrSession)return;try{const r=await fetch('/api/v1/flr/sessions/'+encodeURIComponent(flrSession)+'/download?path='+encodeURIComponent(path),{headers:authHeaders(),credentials:'same-origin'});if(!r.ok){let msg=await r.text();try{msg=JSON.parse(msg).error||msg}catch{}throw new Error(msg)}const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name||'recovered-file';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}catch(e){alert(e.message)}}
async function closeFlr(show=true){if(!flrSession)return;const sid=flrSession;try{await api('/api/v1/flr/sessions/'+encodeURIComponent(sid),{method:'DELETE'})}catch(e){if(show)alert(e.message)}finally{flrSession=null;flrPath='/';files.innerHTML='';flrClose.disabled=true;flrPathEl()}}
async function requestRestore(snapshot,name,type,sources){const allowed=platforms.filter(p=>p.type===type&&p.enabled);if(!allowed.length){alert('No enabled '+type+' restore target is configured in your tenant scope');return}const choices=allowed.map(x=>x.name).join(', ');const target=prompt('Target platform ('+choices+'):',allowed[0].name);if(!target||!allowed.some(x=>x.name===target)){if(target)alert('Choose one of: '+choices);return}sources=(sources&&sources.length)?sources:['primary'];const source=prompt('Backup copy to restore from ('+sources.join(', ')+'):',sources[0]);if(!source||!sources.includes(source)){if(source)alert('Choose one of: '+sources.join(', '));return}const newName=prompt('New VM name:',name+'-restore');if(!newName)return;try{const r=await api('/api/v1/restores',{method:'POST',body:JSON.stringify({snapshot_id:snapshot,target_platform:target,target_name:newName,options:{source_repository:source}})});alert('Restore request #'+r.request_id+' created: '+r.status);await loadRestores()}catch(e){alert(e.message)}}
async function approve(id){try{await api('/api/v1/restores/'+id+'/approve',{method:'POST',body:'{}'});await loadRestores()}catch(e){alert(e.message)}}
async function execute(id){if(!confirm('Execute approved restore #'+id+' as a NEW VM?'))return;try{const r=await api('/api/v1/restores/'+id+'/execute',{method:'POST',body:'{}'});alert('Restore completed: '+JSON.stringify(r));await loadRestores()}catch(e){alert(e.message)}}
async function loadRestores(){try{const r=await api('/api/v1/restores');restores.innerHTML='<table><tr><th>ID</th><th>Tenant</th><th>VM</th><th>Target</th><th>Status</th><th>Requester</th><th>Approver</th><th>Actions</th></tr>'+r.map(x=>{let a='';if(x.status==='pending_approval'&&['approver','admin'].includes(role))a+=`<button class="good" onclick="approve(${x.id})">Approve</button> `;if(['approved','ready'].includes(x.status)&&['restore_operator','admin'].includes(role))a+=`<button class="danger" onclick="execute(${x.id})">Execute</button>`;return `<tr><td>${x.id}</td><td>${esc(x.tenant)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.target_platform)} / ${esc(x.target_name)}</td><td>${esc(x.status)}</td><td>${esc(x.requester)}</td><td>${esc(x.approved_by||'')}</td><td>${a}</td></tr>`}).join('')+'</table>'}catch(e){restores.textContent=e.message}}
authConfig();
</script></div></body></html>'''


class EnterprisePortal:
    def __init__(self, cfg: EnterpriseConfig):
        self.cfg = cfg
        self.engine = BackupEngine(cfg)
        self.flr = FLRManager(cfg, self.engine.repo)
        self.ops = EnterpriseOps(cfg, self.engine.state)
        self.signer: SignedToken | None = None
        self.oidc: OIDCClient | None = None
        self.ws: WebSocketTelemetryServer | None = None
        if cfg.oidc.enabled or cfg.observability.websocket_enabled:
            self.signer = SignedToken.from_env(cfg.oidc.session_secret_env)
        if cfg.oidc.enabled:
            assert self.signer is not None
            self.oidc = OIDCClient(cfg, self.signer)
        if cfg.observability.websocket_enabled:
            assert self.signer is not None
            self.ws = WebSocketTelemetryServer(cfg, self.ops, self.signer)

    def _static_users(self) -> list[tuple[Any, str]]:
        if self.cfg.oidc.enabled and not self.cfg.oidc.allow_local_tokens:
            return []
        resolved = []
        for user in self.cfg.portal.users:
            token = os.getenv(user.token_env)
            if token:
                resolved.append((user, token))
        return resolved

    def _static_identity(self, token: str) -> Identity | None:
        for user, expected in self._static_users():
            if hmac.compare_digest(token, expected):
                tenants = tuple(self.cfg.access.user_tenants.get(user.name, ["*"]))
                return Identity(
                    subject=f"local:{user.name}", name=user.name, role=user.role,
                    tenants=tenants, source="local-token", mfa=False,
                )
        return None

    def _websocket_url(self, host_header: str) -> str | None:
        if not self.cfg.observability.websocket_enabled:
            return None
        public = getattr(self.cfg.observability, "websocket_public_url", None)
        if public:
            return str(public).rstrip("/") + "/events"
        hostname = host_header.split(":", 1)[0].strip("[]") or "localhost"
        scheme = "wss" if self.cfg.portal.tls_cert and self.cfg.portal.tls_key else "ws"
        host = f"[{hostname}]" if ":" in hostname else hostname
        return f"{scheme}://{host}:{self.cfg.observability.websocket_port}/events"

    def serve(self) -> None:
        if not self.cfg.oidc.enabled and not self._static_users():
            raise RuntimeError("portal has no active authentication method")
        listen = self.cfg.portal.listen.strip()
        loopback = listen in {"127.0.0.1", "::1", "localhost"}
        if not loopback and not (self.cfg.portal.tls_cert and self.cfg.portal.tls_key):
            raise RuntimeError("portal refuses non-loopback plaintext exposure; configure TLS or bind to loopback")
        portal = self
        if self.ws:
            self.ws.start(tls_cert=self.cfg.portal.tls_cert, tls_key=self.cfg.portal.tls_key)

        class Handler(BaseHTTPRequestHandler):
            server_version = "ImmutavaultEnterprisePortal/0.9"

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
                self._security_headers(); self.end_headers(); self.wfile.write(payload)

            def _redirect(self, location: str, *, cookies: list[str] | None = None) -> None:
                self.send_response(302); self.send_header("Location", location)
                for cookie in cookies or []: self.send_header("Set-Cookie", cookie)
                self._security_headers(); self.end_headers()

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 1024 * 1024: raise ValueError("request body too large")
                return json.loads(self.rfile.read(length) or b"{}")

            def _cookies(self) -> SimpleCookie:
                cookie = SimpleCookie(); cookie.load(self.headers.get("Cookie", "")); return cookie

            def _auth(self) -> Identity | None:
                header = self.headers.get("Authorization", "")
                if header.startswith("Bearer "):
                    identity = portal._static_identity(header[7:])
                    if identity: return identity
                if portal.signer:
                    morsel = self._cookies().get("immutavault_session")
                    if morsel:
                        try: return portal.signer.identity_from_token(morsel.value)
                        except PermissionError: pass
                return None

            def _require(self, roles: set[str]) -> Identity | None:
                identity = self._auth()
                if not identity:
                    self._json(401, {"error": "authentication required"}); return None
                if identity.role not in roles:
                    self._json(403, {"error": f"role {identity.role} is not permitted"}); return None
                return identity

            def _global_admin(self, identity: Identity) -> bool:
                return identity.role == "admin" and "*" in identity.tenants

            def _local_policy(self, identity: Identity):
                if identity.source != "local-token": return None
                for user in portal.cfg.portal.users:
                    if user.name == identity.name: return user
                return None

            def _allowed_platform(self, identity: Identity, platform: str) -> bool:
                try: tenant = portal.cfg.tenant_for_platform(platform)
                except ValueError: return False
                return identity.allows_tenant(tenant)

            def _allowed_point(self, identity: Identity, point: dict[str, Any]) -> bool:
                if not self._allowed_platform(identity, str(point["platform"])): return False
                local = self._local_policy(identity)
                if local is None: return True
                return any(fnmatch.fnmatch(str(point["platform"]), p) for p in local.sources) and any(
                    fnmatch.fnmatch(str(point["vm_name"]), p) for p in local.vm_patterns
                )

            def _point(self, snapshot_id: str, identity: Identity) -> dict[str, Any] | None:
                point = portal.engine.state.get_point(snapshot_id)
                if not point: self._json(404, {"error": "recovery point not found"}); return None
                if not self._allowed_point(identity, point): self._json(403, {"error": "recovery point outside your tenant/scope"}); return None
                return point

            def _restore(self, request_id: int, identity: Identity) -> dict[str, Any] | None:
                req = portal.engine.state.get_restore_request(request_id)
                if not req: self._json(404, {"error": "restore request not found"}); return None
                if not self._allowed_platform(identity, str(req["source_platform"])):
                    self._json(403, {"error": "restore request outside your tenant scope"}); return None
                return req

            def _send_file(self, file) -> None:
                mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                safe = file.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
                self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(file.size)); self.send_header("Content-Disposition", f'attachment; filename="{safe}"'); self._security_headers(); self.end_headers(); portal.flr.stream_file(file, self.wfile)

            def _metrics_allowed(self) -> bool:
                expected = os.getenv(portal.cfg.observability.metrics_token_env)
                header = self.headers.get("Authorization", "")
                if expected and header.startswith("Bearer ") and hmac.compare_digest(header[7:], expected): return True
                if expected: return False
                return self.client_address[0] in {"127.0.0.1", "::1"}

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path == "/api/v1/auth-config":
                        self._json(200, {"oidc_enabled": portal.cfg.oidc.enabled, "local_tokens_allowed": (not portal.cfg.oidc.enabled or portal.cfg.oidc.allow_local_tokens)}); return
                    if parsed.path == "/auth/login":
                        if not portal.oidc: self._json(404, {"error": "OIDC is disabled"}); return
                        q=parse_qs(parsed.query); location,state_token=portal.oidc.begin_login(return_to=(q.get("return") or ["/"])[0]); secure="; Secure" if portal.cfg.portal.tls_cert else ""; self._redirect(location,cookies=[f"immutavault_oidc_state={state_token}; Path=/auth/callback; HttpOnly; SameSite=Lax; Max-Age=600{secure}"]); return
                    if parsed.path == "/auth/callback":
                        if not portal.oidc or not portal.signer: self._json(404,{"error":"OIDC is disabled"}); return
                        q=parse_qs(parsed.query)
                        if q.get("error"): raise PermissionError("OIDC provider returned: "+str(q.get("error_description",q["error"])[0]))
                        state_cookie=self._cookies().get("immutavault_oidc_state")
                        if not state_cookie: raise PermissionError("OIDC state cookie is missing")
                        identity,return_to=portal.oidc.complete_login(code=(q.get("code") or [""])[0],state=(q.get("state") or [""])[0],state_token=state_cookie.value)
                        session=portal.signer.identity_token(identity,minutes=portal.cfg.oidc.session_minutes); secure="; Secure" if portal.cfg.portal.tls_cert else ""
                        portal.engine.state.audit(identity.subject,"auth.oidc.login","identity",identity.subject,{"role":identity.role,"tenants":list(identity.tenants),"mfa":identity.mfa})
                        self._redirect(return_to,cookies=[f"immutavault_session={session}; Path=/; HttpOnly; SameSite=Lax; Max-Age={portal.cfg.oidc.session_minutes*60}{secure}","immutavault_oidc_state=; Path=/auth/callback; HttpOnly; SameSite=Lax; Max-Age=0"]); return
                    if parsed.path == "/auth/logout":
                        self._redirect("/",cookies=["immutavault_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"]); return
                    if portal.cfg.observability.metrics_enabled and parsed.path == portal.cfg.observability.metrics_path:
                        if not self._metrics_allowed(): self._json(401,{"error":"metrics bearer token required"}); return
                        payload=portal.ops.render_prometheus().encode(); self.send_response(200); self.send_header("Content-Type","text/plain; version=0.0.4; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self._security_headers(); self.end_headers(); self.wfile.write(payload); return
                    if parsed.path == "/":
                        payload=UI.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.send_header("Content-Security-Policy","default-src 'self' 'unsafe-inline' blob:; connect-src 'self' ws: wss:; object-src 'none'; frame-ancestors 'none'"); self._security_headers(); self.end_headers(); self.wfile.write(payload); return

                    identity=self._require({"viewer","restore_operator","approver","admin"})
                    if not identity:return
                    if parsed.path == "/api/v1/health":
                        self._json(200,{"status":"ok","identity":identity.public(),"flr":portal.flr.status(),"websocket_url":portal._websocket_url(self.headers.get("Host","localhost"))});return
                    if parsed.path == "/api/v1/ops/snapshot":
                        snap=portal.ops.snapshot(identity); self._json(200,{"summary":snap.summary,"jobs":snap.jobs});return
                    if parsed.path == "/api/v1/system-health":
                        if not self._global_admin(identity):self._json(403,{"error":"global admin tenant scope required"});return
                        status=portal.engine.status();status["flr"]=portal.flr.status();self._json(200 if status.get("healthy") else 503,status);return
                    if parsed.path == "/api/v1/platforms":
                        rows=[]
                        for p in portal.cfg.platforms:
                            if p.enabled and self._allowed_platform(identity,p.name): rows.append({"name":p.name,"type":p.type,"enabled":p.enabled,"tenant":portal.cfg.tenant_for_platform(p.name)})
                        self._json(200,rows);return
                    if parsed.path == "/api/v1/storage-targets": self._json(200,portal.engine.storage_targets());return
                    if parsed.path == "/api/v1/vms":
                        rows=[]
                        for vm in portal.engine.state.list_vms():
                            if self._allowed_point(identity,{"platform":vm["platform"],"vm_name":vm["vm_name"]}):
                                vm["tenant"]=portal.cfg.tenant_for_platform(str(vm["platform"]));rows.append(vm)
                        self._json(200,rows);return
                    if parsed.path == "/api/v1/recovery-points":
                        q=parse_qs(parsed.query);rows=portal.engine.list_recovery_points(platform=(q.get("platform") or [None])[0],vm_id=(q.get("vm_id") or [None])[0]);allowed=[]
                        for row in rows:
                            if self._allowed_point(identity,row): row["application_consistency"]=point_consistency(row);row["tenant"]=portal.cfg.tenant_for_platform(str(row["platform"]));allowed.append(row)
                        self._json(200,allowed);return
                    if parsed.path == "/api/v1/restores":
                        rows=[]
                        for row in portal.engine.state.list_restore_requests():
                            if not self._allowed_platform(identity,str(row["source_platform"])): continue
                            if identity.role not in {"admin","approver"} and row["requester"]!=identity.subject: continue
                            row["tenant"]=portal.cfg.tenant_for_platform(str(row["source_platform"]));rows.append(row)
                        self._json(200,rows);return
                    if parsed.path.startswith("/api/v1/flr/sessions/") and parsed.path.endswith("/browse"):
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        sid=parsed.path.split("/")[5];q=parse_qs(parsed.query);self._json(200,portal.flr.list_directory(sid,(q.get("path") or ["/"])[0],actor=identity.subject,admin=identity.role=="admin"));return
                    if parsed.path.startswith("/api/v1/flr/sessions/") and parsed.path.endswith("/download"):
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        sid=parsed.path.split("/")[5];q=parse_qs(parsed.query);requested=(q.get("path") or [""])[0];file=portal.flr.open_file(sid,requested,actor=identity.subject,admin=identity.role=="admin");portal.engine.state.audit(identity.subject,"flr.file.download","flr_session",sid,{"path":requested,"size":file.size});self._send_file(file);return
                    if parsed.path == "/api/v1/audit":
                        if not self._global_admin(identity):self._json(403,{"error":"global admin tenant scope required for full audit log"});return
                        self._json(200,portal.engine.state.list_audit());return
                    if parsed.path == "/api/v1/audit/verify":
                        if not self._global_admin(identity):self._json(403,{"error":"global admin tenant scope required"});return
                        ok,errors=portal.engine.state.verify_audit_chain();self._json(200 if ok else 409,{"valid":ok,"errors":errors});return
                    self._json(404,{"error":"not found"})
                except PermissionError as exc:self._json(403,{"error":str(exc)})
                except ValueError as exc:self._json(400,{"error":str(exc)})
                except Exception as exc:
                    print(f"portal internal GET error: {type(exc).__name__}: {exc}");self._json(500,{"error":"internal server error"})

            def do_POST(self) -> None:
                parsed=urlparse(self.path)
                try:
                    identity=self._require({"viewer","restore_operator","approver","admin"})
                    if not identity:return
                    if parsed.path == "/api/v1/ws-ticket":
                        if not portal.ws or not portal.signer:self._json(404,{"error":"WebSocket telemetry is disabled"});return
                        self._json(201,{"ticket":portal.ops.issue_ws_ticket(identity,portal.signer),"expires_in":portal.cfg.observability.websocket_ticket_ttl_seconds});return
                    if parsed.path == "/api/v1/flr/sessions":
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        body=self._body();point=self._point(str(body.get("snapshot_id","")),identity)
                        if not point:return
                        session=portal.flr.open_session(point,actor=identity.subject);portal.engine.state.audit(identity.subject,"flr.session.open","recovery_point",point["snapshot_id"],{"session_id":session["session_id"],"tenant":portal.cfg.tenant_for_platform(str(point["platform"])),"read_only":True});self._json(201,session);return
                    if parsed.path == "/api/v1/restores":
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        body=self._body();point=self._point(str(body.get("snapshot_id","")),identity)
                        if not point:return
                        target=str(body.get("target_platform",""))
                        if not self._allowed_platform(identity,target):self._json(403,{"error":"target platform outside your tenant scope"});return
                        if portal.cfg.tenant_for_platform(target)!=portal.cfg.tenant_for_platform(str(point["platform"])):self._json(403,{"error":"cross-tenant restore is prohibited"});return
                        rid=portal.engine.request_restore(snapshot_id=point["snapshot_id"],requester=identity.subject,target_platform=target,target_name=body.get("target_name"),options=dict(body.get("options") or {}));req=portal.engine.state.get_restore_request(rid) or {};self._json(201,{"request_id":rid,"status":req.get("status")});return
                    if parsed.path.startswith("/api/v1/restores/") and parsed.path.endswith("/approve"):
                        if identity.role not in {"approver","admin"}:self._json(403,{"error":"approver or admin role required"});return
                        rid=int(parsed.path.split("/")[4]);req=self._restore(rid,identity)
                        if not req:return
                        portal.engine.approve_restore(rid,identity.subject);self._json(200,portal.engine.state.get_restore_request(rid));return
                    if parsed.path.startswith("/api/v1/restores/") and parsed.path.endswith("/execute"):
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        rid=int(parsed.path.split("/")[4]);req=self._restore(rid,identity)
                        if not req:return
                        if identity.role!="admin" and req["requester"]!=identity.subject:self._json(403,{"error":"only an admin can execute another identity's restore"});return
                        self._json(200,portal.engine.execute_restore(rid,actor=identity.subject));return
                    if parsed.path.startswith("/api/v1/recovery-points/") and parsed.path.endswith("/verify"):
                        if identity.role!="admin":self._json(403,{"error":"admin role required"});return
                        snapshot=parsed.path.split("/")[4];point=self._point(snapshot,identity)
                        if not point:return
                        self._json(200,{"snapshot_id":snapshot,"verified":portal.engine.verify_recovery_point(snapshot,actor=identity.subject)});return
                    if parsed.path.startswith("/api/v1/recovery-points/") and parsed.path.endswith("/hold"):
                        if identity.role!="admin":self._json(403,{"error":"admin role required"});return
                        snapshot=parsed.path.split("/")[4];point=self._point(snapshot,identity)
                        if not point:return
                        body=self._body();until=portal.engine.hold_recovery_point(snapshot,actor=identity.subject,days=int(body.get("days",30)),reason=str(body.get("reason","manual hold")));self._json(200,{"snapshot_id":snapshot,"immutable_until":until});return
                    self._json(404,{"error":"not found"})
                except PermissionError as exc:self._json(403,{"error":str(exc)})
                except ValueError as exc:self._json(400,{"error":str(exc)})
                except Exception as exc:
                    print(f"portal internal POST error: {type(exc).__name__}: {exc}");self._json(500,{"error":"internal server error"})

            def do_DELETE(self) -> None:
                parsed=urlparse(self.path)
                try:
                    identity=self._require({"restore_operator","admin"})
                    if not identity:return
                    if parsed.path.startswith("/api/v1/flr/sessions/"):
                        parts=parsed.path.strip("/").split("/")
                        if len(parts)!=5:self._json(404,{"error":"not found"});return
                        sid=parts[4];portal.flr.close_session(sid,actor=identity.subject if identity.role!="admin" else None,force=identity.role=="admin");portal.engine.state.audit(identity.subject,"flr.session.close","flr_session",sid,{});self._json(200,{"session_id":sid,"status":"closed"});return
                    self._json(404,{"error":"not found"})
                except PermissionError as exc:self._json(403,{"error":str(exc)})
                except ValueError as exc:self._json(400,{"error":str(exc)})
                except Exception as exc:
                    print(f"portal internal DELETE error: {type(exc).__name__}: {exc}");self._json(500,{"error":"internal server error"})

        httpd=ThreadingHTTPServer((self.cfg.portal.listen,self.cfg.portal.port),Handler)
        if self.cfg.portal.tls_cert and self.cfg.portal.tls_key:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2;ctx.load_cert_chain(self.cfg.portal.tls_cert,self.cfg.portal.tls_key);httpd.socket=ctx.wrap_socket(httpd.socket,server_side=True)
        print(f"Immutavault enterprise portal listening on {self.cfg.portal.listen}:{self.cfg.portal.port}")
        try:httpd.serve_forever()
        finally:
            if self.ws:self.ws.stop()


Portal = EnterprisePortal
