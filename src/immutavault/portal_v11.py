from __future__ import annotations

import fnmatch
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
from .enterprise_auth import Identity
from .enterprise_ops import EnterpriseOps
from .flr_broker import FLRBrokerClient
from .management_broker import ManagementBrokerClient
from .portal_v09 import EnterprisePortal as BasePortal
from .v2v_engine import CertifiedBackupEngine


UI = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Immutavault</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:Inter,system-ui,Segoe UI,sans-serif;margin:0;background:#0a0f15;color:#e6edf3}.shell{display:grid;grid-template-columns:220px 1fr;min-height:100vh}.side{border-right:1px solid #26303b;padding:22px 14px;background:#0d131b}.brand{font-size:21px;font-weight:800;margin:0 8px 24px}.nav button{width:100%;text-align:left;margin:3px 0;border:0;background:transparent}.nav button.active{background:#1b2634}.main{padding:24px;max-width:1500px;width:100%}.card{background:#121a24;border:1px solid #293442;border-radius:13px;padding:16px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.metric{font-size:28px;font-weight:750}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.pane{display:none}.pane.active{display:block}input,select,button,textarea,a.btn{padding:9px;border-radius:8px;border:1px solid #3b4858;background:#0d141d;color:#e6edf3;text-decoration:none}input,select{min-width:150px}button,a.btn{cursor:pointer}.good{border-color:#2ea043}.danger{border-color:#f85149}.ok{color:#7ee787}.bad{color:#ff7b72}.warn{color:#d29922}.muted{color:#8b949e}.pill{border:1px solid #3b4858;border-radius:999px;padding:3px 8px;font-size:12px}table{width:100%;border-collapse:collapse}th,td{padding:9px;border-bottom:1px solid #26303b;text-align:left;font-size:13px}.bar{width:160px;height:9px;border:1px solid #3b4858;border-radius:99px;overflow:hidden}.bar span{display:block;height:100%;background:#2ea043}.hidden{display:none!important}.step{border-left:3px solid #3b4858;padding-left:12px;margin:18px 0}.vm{display:block;padding:4px}.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.hero{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}.hero h1{margin:0}.compact input,.compact select{min-width:120px;max-width:280px}@media(max-width:800px){.shell{grid-template-columns:1fr}.side{border-right:0;border-bottom:1px solid #26303b}.nav{display:flex;overflow:auto}.nav button{width:auto}.main{padding:14px}}</style></head><body><div class="shell"><aside class="side"><div class="brand">Immutavault</div><div class="nav"><button class="active" data-pane="overview" onclick="showPane(this)">Overview</button><button data-pane="protect" onclick="showPane(this)">Protect</button><button data-pane="recovery" onclick="showPane(this)">Recovery</button><button data-pane="management" onclick="showPane(this)">Setup & Manage</button></div></aside><main class="main">
<div class="hero"><div><h1>Enterprise Recovery Console</h1><div class="muted">Immutable backup, policy protection and guided recovery from one console.</div></div><div class="row"><a class="btn" id="oidcLogin" href="/auth/login">Sign in with Entra / OIDC</a><label id="localAuth">Break-glass token <input id="token" type="password" autocomplete="off"></label><button onclick="loadAll()">Connect</button><a class="btn" href="/auth/logout">Sign out</a><span id="who" class="pill"></span><span id="live" class="pill">live: disconnected</span></div></div>
<section id="overview" class="pane active"><div class="grid"><div class="card"><div class="muted">Recovery points</div><div id="mPoints" class="metric">—</div></div><div class="card"><div class="muted">Verified</div><div id="mVerified" class="metric">—</div></div><div class="card"><div class="muted">Suspicious</div><div id="mSuspicious" class="metric">—</div></div><div class="card"><div class="muted">Running jobs</div><div id="mRunning" class="metric">—</div></div></div><div class="card"><h2>Live operations</h2><div id="jobs">Connect to view operations.</div></div><div class="card"><h2>Protected VMs</h2><table><thead><tr><th>Tenant</th><th>Source</th><th>Hypervisor</th><th>VM</th><th>Latest point</th><th>Points</th><th></th></tr></thead><tbody id="vms"></tbody></table></div></section>
<section id="protect" class="pane"><div class="card"><div class="hero"><div><h2>Protection policies</h2><div class="muted">Named jobs with exact VM selections, schedules, immutable retention and verification.</div></div><button onclick="loadPolicies()">Refresh</button></div><div id="policies"></div></div><div class="card" id="policyEditor"><h2>Create / update policy</h2><div class="grid compact"><label>Policy ID<input id="polId" placeholder="daily-production"></label><label>Display name<input id="polName" placeholder="Daily Production"></label><label>Platform<select id="polPlatform"></select></label><label>Schedule<select id="polFrequency"><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="hourly">Every N hours</option><option value="manual">Manual only</option></select></label><label>Time<input id="polTime" type="time" value="22:00"></label><label>Every hours<input id="polHours" type="number" min="1" max="24" value="1"></label><label>Weekdays<input id="polDays" value="mon,tue,wed,thu,fri"></label><label>Immutable days<input id="polImmutable" type="number" min="1" max="3650" value="30"></label></div><div class="row"><button onclick="discoverPolicyVMs()">Discover VMs</button><label><input id="polVerify" type="checkbox" checked> verify after backup</label><button onclick="selectPolicyVMs(true)">Select all</button><button onclick="selectPolicyVMs(false)">Clear</button></div><div id="policyVMs" class="card muted">Choose a platform and discover.</div><button class="good" onclick="savePolicy()">Save protection policy</button></div></section>
<section id="recovery" class="pane"><div class="card"><h2>Recovery points</h2><div id="points" class="muted">Select Recovery points beside a protected VM.</div></div><div class="card"><h2>File-level recovery</h2><div class="row"><button id="flrUp" onclick="flrUp()" disabled>Up</button><button id="flrClose" onclick="closeFlr()" disabled>Close session</button><span id="flrPath" class="path muted">No FLR session.</span></div><div id="files"></div></div><div class="card"><h2>VM restore approvals</h2><div id="restores"></div></div></section>
<section id="management" class="pane"><div id="manageDenied" class="card muted">Global admin access is required for setup and configuration changes.</div><div id="manageBody" class="hidden"><div class="card"><div class="hero"><div><h2>Guided onboarding</h2><div class="muted">Configuration changes go through the local privileged management broker; the web portal remains unprivileged.</div></div><span id="manageStatus" class="pill">broker: unknown</span></div><div id="setupSummary" class="muted"></div></div><div class="card step"><h3>1 · Add hypervisor</h3><div class="grid compact"><label>Name<input id="mgPn" placeholder="vc-primary"></label><label>Type<select id="mgPt"><option value="vmware">VMware/vCenter</option><option value="proxmox">Proxmox</option><option value="xcpng">XCP-ng</option></select></label><label>Endpoint<input id="mgPe"></label><label>Username<input id="mgPu"></label><label>Password<input id="mgPp" type="password"></label><label>SSH user<input id="mgPsu" value="backupsvc"></label><label>SSH key path<input id="mgPk"></label></div><div class="row"><button onclick="testPlatform()">Test + discover</button><button class="good" onclick="savePlatform()">Save</button></div><pre id="mgPo"></pre></div><div class="card step"><h3>2 · Select VMs</h3><div class="row"><select id="mgPlatform"></select><button onclick="discoverManageVMs()">Discover VMs</button><button onclick="selectManageVMs(true)">Select all</button><button onclick="selectManageVMs(false)">Clear</button></div><div id="mgVMs"></div><button class="good" onclick="saveProtectionSelection()">Save selected VMs</button></div><div class="card step"><h3>3 · Storage / cloud</h3><div class="grid compact"><label>Name<input id="mgSn" placeholder="wasabi-dr"></label><label>Backend<select id="mgSb"><option value="s3">S3 / object storage</option><option value="filesystem">Mounted NFS / SMB / NAS</option><option value="rest">Second Immutavault vault</option></select></label><label>Provider<select id="mgSp"><option>wasabi</option><option>idrive_e2</option><option>backblaze_b2</option><option>cloudflare_r2</option><option>aws</option><option>minio</option><option>ceph</option><option>custom</option></select></label><label>Endpoint / REST URL<input id="mgSe"></label><label>Region<input id="mgSr"></label><label>Bucket<input id="mgSbu"></label><label>Prefix<input id="mgSpre" value="prod"></label><label>Mounted path<input id="mgSpath"></label><label>Access key<input id="mgSak"></label><label>Secret key<input id="mgSsk" type="password"></label><label>Repository password<input id="mgSrp" type="password"></label><label>Immutable days<input id="mgSid" type="number" value="30"></label></div><label><input id="mgSil" type="checkbox" checked> provider-side immutability</label><div class="row"><button onclick="testStorage()">Test</button><button onclick="saveStorage()">Save</button><button class="good" onclick="initStorage()">Initialize</button></div><pre id="mgSo"></pre></div><div class="card step"><h3>4 · Validate and start</h3><div class="row"><button onclick="manageAction('doctor')">Health check</button><button onclick="manageAction('backup_dry_run')">Backup dry-run</button><button class="good" onclick="manageAction('backup_run',true)">Run first real backup</button><button onclick="manageAction('immutable_verify')">Verify immutable copies</button></div><pre id="mgFo"></pre></div><div class="card"><h3>Isolated DR test networks</h3><p class="muted">Register only networks/bridges that are physically or logically isolated from production. DR-test execution remains fail-closed unless a target network is explicitly allow-listed.</p><div class="row"><select id="drNetPlatform"></select><input id="drNetName" placeholder="isolated-recovery"><button onclick="saveDrNetwork()">Allow isolated network</button></div></div></div></section>
<script>
let role='',tenants=[],platforms=[],flrSession=null,flrPath='/',ws=null,wsURL=null,manageVMCache=[];const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const authHeaders=()=>{const t=$('token').value;return t?{'Authorization':'Bearer '+t}:{}};const api=async(path,opts={})=>{opts.credentials='same-origin';opts.headers=Object.assign(authHeaders(),{'Content-Type':'application/json'},opts.headers||{});const r=await fetch(path,opts),t=await r.text();let j;try{j=JSON.parse(t)}catch{j={error:t}}if(!r.ok)throw Error(j.error||r.statusText);return j};
function showPane(btn){document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');document.querySelectorAll('.pane').forEach(x=>x.classList.remove('active'));$(btn.dataset.pane).classList.add('active')}async function authConfig(){try{const c=await fetch('/api/v1/auth-config',{credentials:'same-origin'}).then(r=>r.json());$('oidcLogin').classList.toggle('hidden',!c.oidc_enabled);$('localAuth').classList.toggle('hidden',!c.local_tokens_allowed)}catch{}}
function metrics(s){$('mPoints').textContent=s.recovery_points??0;$('mVerified').textContent=s.verified??0;$('mSuspicious').textContent=s.suspicious??0;$('mRunning').textContent=s.running_jobs??0}function jobsTable(rows){$('jobs').innerHTML='<table><tr><th>Tenant</th><th>VM</th><th>Source</th><th>Stage</th><th>Progress</th><th>Status</th></tr>'+rows.map(x=>`<tr><td>${esc(x.tenant)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.platform)}</td><td>${esc(x.stage)}</td><td><div class="row"><div class="bar"><span style="width:${Number(x.progress_percent||0)}%"></span></div>${esc(x.progress_percent)}%</div></td><td>${esc(x.status)}</td></tr>`).join('')+'</table>'}
async function loadAll(){try{const h=await api('/api/v1/health');role=h.identity.role;tenants=h.identity.tenants||[];$('who').textContent=h.identity.name+' · '+role+(h.identity.mfa?' · MFA':'');wsURL=h.websocket_url||null;platforms=await api('/api/v1/platforms');const o=await api('/api/v1/ops/snapshot');metrics(o.summary);jobsTable(o.jobs);const v=await api('/api/v1/vms');$('vms').innerHTML=v.map(x=>`<tr><td>${esc(x.tenant)}</td><td>${esc(x.platform)}</td><td>${esc(x.platform_type)}</td><td>${esc(x.vm_name)}</td><td>${esc(x.latest_point)}</td><td>${esc(x.restore_points)}</td><td><button onclick='pointsFor(${JSON.stringify(x.platform)},${JSON.stringify(x.vm_id)})'>Recovery points</button></td></tr>`).join('');await loadRestores();await loadPolicies();fillPlatformSelectors();const ga=role==='admin'&&tenants.includes('*');$('manageDenied').classList.toggle('hidden',ga);$('manageBody').classList.toggle('hidden',!ga);if(ga)await loadManagement();await connectWS()}catch(e){alert(e.message)}}
async function connectWS(){if(!wsURL)return;try{if(ws)ws.close();const t=await api('/api/v1/ws-ticket',{method:'POST',body:'{}'});ws=new WebSocket(wsURL+(wsURL.includes('?')?'&':'?')+'ticket='+encodeURIComponent(t.ticket));ws.onopen=()=>$('live').textContent='live: connected';ws.onclose=()=>$('live').textContent='live: disconnected';ws.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='operations'){metrics(d.summary);jobsTable(d.jobs)}}}catch(e){$('live').textContent='live: '+e.message}}
function fillPlatformSelectors(){const opts=platforms.map(p=>`<option value="${esc(p.name)}">${esc(p.name)} (${esc(p.type)})</option>`).join('');$('polPlatform').innerHTML=opts;$('mgPlatform').innerHTML=opts;$('drNetPlatform').innerHTML=opts}function consistency(x){const c=x.application_consistency||{},state=c.state||'unknown';return `<span class="${c.application_consistent?'ok':state==='crash-consistent'?'warn':'muted'}">${esc(state)}</span>`}
async function pointsFor(p,id){document.querySelector('[data-pane="recovery"]').click();const rows=await api('/api/v1/recovery-points?platform='+encodeURIComponent(p)+'&vm_id='+encodeURIComponent(id));$('points').innerHTML='<table><tr><th>Captured</th><th>Snapshot</th><th>Consistency</th><th>Verified</th><th>Immutable until</th><th>Score</th><th>Actions</th></tr>'+rows.map(x=>{let a='';if(['restore_operator','admin'].includes(role)){a+=`<button onclick='openFlr(${JSON.stringify(x.snapshot_id)})'>Files</button> <button onclick='requestRestore(${JSON.stringify(x.snapshot_id)},${JSON.stringify(x.vm_name)},${JSON.stringify(x.available_restore_sources||['primary'])})'>Full VM</button>`}return `<tr><td>${esc(x.created_at)}</td><td>${esc(x.snapshot_id.slice(0,12))}</td><td>${consistency(x)}</td><td>${x.verified?'✓':'—'}</td><td>${esc(x.immutable_until)}</td><td>${esc(x.recovery_score)}</td><td>${a}</td></tr>`}).join('')+'</table>'}
async function openFlr(snapshot){try{if(flrSession)await closeFlr(false);const s=await api('/api/v1/flr/sessions',{method:'POST',body:JSON.stringify({snapshot_id:snapshot})});flrSession=s.session_id;flrPath='/';$('flrClose').disabled=false;await browseFlr('/')}catch(e){alert(e.message)}}async function browseFlr(path){if(!flrSession)return;const d=await api('/api/v1/flr/sessions/'+encodeURIComponent(flrSession)+'/browse?path='+encodeURIComponent(path));flrPath=d.path;$('flrPath').textContent=flrPath;$('flrUp').disabled=flrPath==='/';$('files').innerHTML='<table><tr><th>Name</th><th>Type</th><th>Size</th><th></th></tr>'+d.entries.map(x=>{const next=(flrPath==='/'?'':flrPath)+'/'+x.name;return `<tr><td>${esc(x.name)}</td><td>${esc(x.type)}</td><td>${x.size??''}</td><td>${x.type==='directory'?`<button onclick='browseFlr(${JSON.stringify(next)})'>Open</button>`:x.downloadable?`<button onclick='downloadFlr(${JSON.stringify(next)},${JSON.stringify(x.name)})'>Download</button>`:''}</td></tr>`}).join('')+'</table>'}function flrUp(){const p=flrPath.split('/').filter(Boolean);p.pop();browseFlr('/'+p.join('/'))}async function closeFlr(show=true){if(!flrSession)return;const sid=flrSession;try{await api('/api/v1/flr/sessions/'+encodeURIComponent(sid),{method:'DELETE'})}catch(e){if(show)alert(e.message)}finally{flrSession=null;flrPath='/';$('files').innerHTML='';$('flrPath').textContent='No FLR session.';$('flrClose').disabled=true}}async function downloadFlr(path,name){const r=await fetch('/api/v1/flr/sessions/'+encodeURIComponent(flrSession)+'/download?path='+encodeURIComponent(path),{headers:authHeaders(),credentials:'same-origin'});if(!r.ok)throw Error(await r.text());const blob=await r.blob(),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;a.click();URL.revokeObjectURL(u)}
async function requestRestore(snapshot,name,sources){const target=prompt('Target platform:',platforms[0]?.name||'');if(!target)return;const source=prompt('Backup copy:',(sources||['primary'])[0]);if(!source)return;const newName=prompt('New VM name:',name+'-restore');if(!newName)return;const r=await api('/api/v1/restores',{method:'POST',body:JSON.stringify({snapshot_id:snapshot,target_platform:target,target_name:newName,options:{source_repository:source}})});alert('Restore request #'+r.request_id+' created');await loadRestores()}async function loadRestores(){try{const r=await api('/api/v1/restores');$('restores').innerHTML='<table><tr><th>ID</th><th>VM</th><th>Target</th><th>Status</th><th>Requester</th><th>Approver</th><th></th></tr>'+r.map(x=>{let a='';if(x.status==='pending_approval'&&['approver','admin'].includes(role))a+=`<button onclick="approve(${x.id})">Approve</button> `;if(['approved','ready'].includes(x.status)&&['restore_operator','admin'].includes(role))a+=`<button class="danger" onclick="executeRestore(${x.id})">Execute</button>`;return `<tr><td>${x.id}</td><td>${esc(x.vm_name)}</td><td>${esc(x.target_platform)} / ${esc(x.target_name)}</td><td>${esc(x.status)}</td><td>${esc(x.requester)}</td><td>${esc(x.approved_by||'')}</td><td>${a}</td></tr>`}).join('')+'</table>'}catch(e){$('restores').textContent=e.message}}async function approve(id){await api('/api/v1/restores/'+id+'/approve',{method:'POST',body:'{}'});await loadRestores()}async function executeRestore(id){if(!confirm('Execute approved restore as a NEW VM?'))return;await api('/api/v1/restores/'+id+'/execute',{method:'POST',body:'{}'});await loadRestores()}
async function loadPolicies(){try{const rows=await api('/api/v1/manage/policies');$('policies').innerHTML=rows.length?'<table><tr><th>Name</th><th>Schedule</th><th>Immutable</th><th>VMs</th><th></th></tr>'+rows.map(p=>`<tr><td>${esc(p.name)}<br><span class="muted">${esc(p.id)}</span></td><td>${esc(p.schedule.on_calendar||'manual')}</td><td>${esc(p.immutable_days)} days</td><td>${p.selections.reduce((n,x)=>n+x.vms.length,0)}</td><td><button onclick='runPolicy(${JSON.stringify(p.id)},true)'>Dry run</button> <button class="good" onclick='runPolicy(${JSON.stringify(p.id)},false)'>Run now</button> <button class="danger" onclick='deletePolicy(${JSON.stringify(p.id)})'>Delete</button></td></tr>`).join('')+'</table>':'No named policies yet.'}catch(e){$('policies').textContent=role==='admin'?e.message:'Admin access required.'}}async function discoverPolicyVMs(){const p=$('polPlatform').value;if(!p)return;const r=await api('/api/v1/manage/platform/discover',{method:'POST',body:JSON.stringify({name:p})});manageVMCache=r.inventory||[];$('policyVMs').innerHTML=manageVMCache.map(x=>`<label class="vm"><input class="pvm" type="checkbox" value="${esc(x.name)}" checked> ${esc(x.name)} <span class="muted">${esc(x.power_state)}</span></label>`).join('')||'No VMs discovered'}function selectPolicyVMs(v){document.querySelectorAll('.pvm').forEach(x=>x.checked=v)}async function savePolicy(){const v=[...document.querySelectorAll('.pvm:checked')].map(x=>x.value),f=$('polFrequency').value,body={id:$('polId').value,name:$('polName').value,enabled:true,selections:[{platform:$('polPlatform').value,vms:v}],schedule:{frequency:f,time:$('polTime').value,every_hours:Number($('polHours').value),weekdays:$('polDays').value.split(',').map(x=>x.trim()).filter(Boolean)},immutable_days:Number($('polImmutable').value),verify_after_backup:$('polVerify').checked,replica_targets:[]};await api('/api/v1/manage/policy/save',{method:'POST',body:JSON.stringify(body)});await loadPolicies()}async function deletePolicy(id){if(!confirm('Delete policy '+id+'?'))return;await api('/api/v1/manage/policy/delete',{method:'POST',body:JSON.stringify({id})});await loadPolicies()}async function runPolicy(id,dry){const r=await api('/api/v1/manage/policy/run',{method:'POST',body:JSON.stringify({id,dry_run:dry})});alert(JSON.stringify(r,null,2))}
async function loadManagement(){try{const s=await api('/api/v1/manage/status');$('manageStatus').textContent=s.broker_available?'broker: connected':'broker: unavailable';$('setupSummary').textContent=`${s.platforms||0} hypervisor(s) · ${s.replicas||0} storage replica(s) · ${s.policies||0} named policy(s)`}catch(e){$('manageStatus').textContent='broker: error';$('setupSummary').textContent=e.message}}function platformBody(){return{name:$('mgPn').value,type:$('mgPt').value,endpoint:$('mgPe').value,username:$('mgPu').value,password:$('mgPp').value,ssh_user:$('mgPsu').value,ssh_key_path:$('mgPk').value}}function renderManageVMs(v){manageVMCache=v;$('mgVMs').innerHTML=v.map(x=>`<label class="vm"><input class="mgvc" type="checkbox" value="${esc(x.name)}" checked> ${esc(x.name)} (${esc(x.power_state)})</label>`).join('')||'No VMs discovered'}async function testPlatform(){try{const r=await api('/api/v1/manage/platform/test',{method:'POST',body:JSON.stringify(platformBody())});$('mgPo').textContent=JSON.stringify(r,null,2);renderManageVMs(r.inventory||[])}catch(e){$('mgPo').textContent=e.message}}async function savePlatform(){const r=await api('/api/v1/manage/platform/save',{method:'POST',body:JSON.stringify(platformBody())});$('mgPo').textContent=JSON.stringify(r,null,2);await loadAll()}async function discoverManageVMs(){const r=await api('/api/v1/manage/platform/discover',{method:'POST',body:JSON.stringify({name:$('mgPlatform').value})});renderManageVMs(r.inventory||[])}function selectManageVMs(v){document.querySelectorAll('.mgvc').forEach(x=>x.checked=v)}async function saveProtectionSelection(){const v=[...document.querySelectorAll('.mgvc:checked')].map(x=>x.value);alert(JSON.stringify(await api('/api/v1/manage/protection/save',{method:'POST',body:JSON.stringify({platform:$('mgPlatform').value,vms:v})}),null,2))}
function storageBody(){return{name:$('mgSn').value,backend:$('mgSb').value,provider:$('mgSp').value,endpoint:$('mgSe').value,url:$('mgSe').value,region:$('mgSr').value,bucket:$('mgSbu').value,prefix:$('mgSpre').value,path:$('mgSpath').value,access_key:$('mgSak').value,secret_key:$('mgSsk').value,password:$('mgSrp').value,immutable:$('mgSil').checked,lock_days:Number($('mgSid').value||30)}}async function testStorage(){try{$('mgSo').textContent=JSON.stringify(await api('/api/v1/manage/storage/test',{method:'POST',body:JSON.stringify(storageBody())}),null,2)}catch(e){$('mgSo').textContent=e.message}}async function saveStorage(){try{$('mgSo').textContent=JSON.stringify(await api('/api/v1/manage/storage/save',{method:'POST',body:JSON.stringify(storageBody())}),null,2)}catch(e){$('mgSo').textContent=e.message}}async function initStorage(){try{$('mgSo').textContent=JSON.stringify(await api('/api/v1/manage/storage/init',{method:'POST',body:JSON.stringify({name:$('mgSn').value})}),null,2)}catch(e){$('mgSo').textContent=e.message}}async function manageAction(action,confirmReal=false){if(confirmReal&&!confirm('Run a real backup of the selected protection scope?'))return;try{$('mgFo').textContent=JSON.stringify(await api('/api/v1/manage/'+action.replaceAll('_','-'),{method:'POST',body:'{}'}),null,2)}catch(e){$('mgFo').textContent=e.message}}async function saveDrNetwork(){alert(JSON.stringify(await api('/api/v1/manage/dr-test-network/save',{method:'POST',body:JSON.stringify({platform:$('drNetPlatform').value,network:$('drNetName').value})}),null,2))}
authConfig();</script></main></div></body></html>'''


class EnterprisePortal(BasePortal):
    def __init__(self, cfg: Any) -> None:
        super().__init__(cfg)
        self.engine = CertifiedBackupEngine(cfg)
        self.flr = FLRBrokerClient(cfg)
        self.ops = EnterpriseOps(cfg, self.engine.state)
        self.management = ManagementBrokerClient(cfg)
        if self.ws is not None:
            self.ws.ops = self.ops

    def serve(self) -> None:
        if not self.cfg.oidc.enabled and not self._static_users():
            raise RuntimeError("portal has no active authentication method")
        listen = self.cfg.portal.listen.strip(); loopback = listen in {"127.0.0.1", "::1", "localhost"}
        if not loopback and not (self.cfg.portal.tls_cert and self.cfg.portal.tls_key):
            raise RuntimeError("portal refuses non-loopback plaintext exposure; configure TLS or bind to loopback")
        portal = self
        if self.ws: self.ws.start(tls_cert=self.cfg.portal.tls_cert, tls_key=self.cfg.portal.tls_key)

        class Handler(BaseHTTPRequestHandler):
            server_version = "ImmutavaultEnterprisePortal/1.1"
            def log_message(self, fmt: str, *args: Any) -> None: print(f"portal {self.client_address[0]} {fmt % args}")
            def _security_headers(self) -> None:
                self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Permissions-Policy","camera=(), microphone=(), geolocation=()")
            def _json(self, code: int, value: Any) -> None:
                payload=json.dumps(value,default=str).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(payload))); self._security_headers(); self.end_headers(); self.wfile.write(payload)
            def _redirect(self, location: str, cookies: list[str] | None=None) -> None:
                self.send_response(302); self.send_header("Location",location); [self.send_header("Set-Cookie",c) for c in cookies or []]; self._security_headers(); self.end_headers()
            def _body(self) -> dict[str, Any]:
                length=int(self.headers.get("Content-Length","0") or 0)
                if length>1024*1024: raise ValueError("request body too large")
                value=json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(value,dict): raise ValueError("request body must be a JSON object")
                return value
            def _cookies(self) -> SimpleCookie: cookie=SimpleCookie(); cookie.load(self.headers.get("Cookie","")); return cookie
            def _auth(self) -> Identity | None:
                header=self.headers.get("Authorization","")
                if header.startswith("Bearer "):
                    identity=portal._static_identity(header[7:])
                    if identity:return identity
                if portal.signer:
                    morsel=self._cookies().get("immutavault_session")
                    if morsel:
                        try:return portal.signer.identity_from_token(morsel.value)
                        except PermissionError:pass
                return None
            def _require(self, roles: set[str]) -> Identity | None:
                identity=self._auth()
                if not identity:self._json(401,{"error":"authentication required"});return None
                if identity.role not in roles:self._json(403,{"error":f"role {identity.role} is not permitted"});return None
                return identity
            def _global_admin(self, identity: Identity) -> bool:return identity.role=="admin" and "*" in identity.tenants
            def _local_policy(self, identity: Identity):
                if identity.source!="local-token":return None
                return next((u for u in portal.cfg.portal.users if u.name==identity.name),None)
            def _allowed_platform(self, identity: Identity, platform: str) -> bool:
                try:tenant=portal.cfg.tenant_for_platform(platform)
                except ValueError:return False
                return identity.allows_tenant(tenant)
            def _allowed_point(self, identity: Identity, point: dict[str, Any]) -> bool:
                if not self._allowed_platform(identity,str(point["platform"])):return False
                local=self._local_policy(identity)
                if local is None:return True
                return any(fnmatch.fnmatch(str(point["platform"]),p) for p in local.sources) and any(fnmatch.fnmatch(str(point["vm_name"]),p) for p in local.vm_patterns)
            def _point(self, snapshot: str, identity: Identity):
                point=portal.engine.state.get_point(snapshot)
                if not point:self._json(404,{"error":"recovery point not found"});return None
                if not self._allowed_point(identity,point):self._json(403,{"error":"recovery point outside your tenant/scope"});return None
                return point
            def _restore(self, rid: int, identity: Identity):
                req=portal.engine.state.get_restore_request(rid)
                if not req:self._json(404,{"error":"restore request not found"});return None
                if not self._allowed_platform(identity,str(req["source_platform"])):self._json(403,{"error":"restore request outside your tenant scope"});return None
                return req
            def _send_file(self,file) -> None:
                mime=mimetypes.guess_type(file.name)[0] or "application/octet-stream";safe=file.name.replace("\\","_").replace('"',"_").replace("\r","_").replace("\n","_");self.send_response(200);self.send_header("Content-Type",mime);self.send_header("Content-Length",str(file.size));self.send_header("Content-Disposition",f'attachment; filename="{safe}"');self._security_headers();self.end_headers();portal.flr.stream_file(file,self.wfile)
            def _metrics_allowed(self)->bool:
                expected=os.getenv(portal.cfg.observability.metrics_token_env);header=self.headers.get("Authorization","")
                if expected and header.startswith("Bearer ") and hmac.compare_digest(header[7:],expected):return True
                return not expected and self.client_address[0] in {"127.0.0.1","::1"}
            def _manage(self, identity: Identity, action: str, body: dict[str, Any]|None=None):
                if not self._global_admin(identity):raise PermissionError("global admin tenant scope is required for management changes")
                result=portal.management.request(action,body=body or {})
                portal.engine.state.audit(identity.subject,"management."+action,"management","configuration",{"action":action})
                return result

            def do_GET(self)->None:
                parsed=urlparse(self.path)
                try:
                    if parsed.path=="/api/v1/auth-config":self._json(200,{"oidc_enabled":portal.cfg.oidc.enabled,"local_tokens_allowed":not portal.cfg.oidc.enabled or portal.cfg.oidc.allow_local_tokens});return
                    if parsed.path=="/auth/login":
                        if not portal.oidc:self._json(404,{"error":"OIDC is disabled"});return
                        q=parse_qs(parsed.query);location,state=portal.oidc.begin_login(return_to=(q.get("return") or ["/"])[0]);secure="; Secure" if portal.cfg.portal.tls_cert else "";self._redirect(location,[f"immutavault_oidc_state={state}; Path=/auth/callback; HttpOnly; SameSite=Lax; Max-Age=600{secure}"]);return
                    if parsed.path=="/auth/callback":
                        if not portal.oidc or not portal.signer:self._json(404,{"error":"OIDC is disabled"});return
                        q=parse_qs(parsed.query);state_cookie=self._cookies().get("immutavault_oidc_state")
                        if not state_cookie:raise PermissionError("OIDC state cookie is missing")
                        identity,return_to=portal.oidc.complete_login(code=(q.get("code") or [""])[0],state=(q.get("state") or [""])[0],state_token=state_cookie.value);session=portal.signer.identity_token(identity,minutes=portal.cfg.oidc.session_minutes);secure="; Secure" if portal.cfg.portal.tls_cert else "";portal.engine.state.audit(identity.subject,"auth.oidc.login","identity",identity.subject,{"role":identity.role,"tenants":list(identity.tenants),"mfa":identity.mfa});self._redirect(return_to,[f"immutavault_session={session}; Path=/; HttpOnly; SameSite=Lax; Max-Age={portal.cfg.oidc.session_minutes*60}{secure}","immutavault_oidc_state=; Path=/auth/callback; HttpOnly; SameSite=Lax; Max-Age=0"]);return
                    if parsed.path=="/auth/logout":self._redirect("/",["immutavault_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"]);return
                    if portal.cfg.observability.metrics_enabled and parsed.path==portal.cfg.observability.metrics_path:
                        if not self._metrics_allowed():self._json(401,{"error":"metrics bearer token required"});return
                        payload=portal.ops.render_prometheus().encode();self.send_response(200);self.send_header("Content-Type","text/plain; version=0.0.4; charset=utf-8");self.send_header("Content-Length",str(len(payload)));self._security_headers();self.end_headers();self.wfile.write(payload);return
                    if parsed.path=="/":
                        payload=UI.encode();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(payload)));self.send_header("Content-Security-Policy","default-src 'self' 'unsafe-inline' blob:; connect-src 'self' ws: wss:; object-src 'none'; frame-ancestors 'none'");self._security_headers();self.end_headers();self.wfile.write(payload);return
                    identity=self._require({"viewer","restore_operator","approver","admin"})
                    if not identity:return
                    if parsed.path=="/api/v1/health":self._json(200,{"status":"ok","identity":identity.public(),"flr":portal.flr.status(),"management":portal.management.status() if self._global_admin(identity) else {"visible":False},"websocket_url":portal._websocket_url(self.headers.get("Host","localhost"))});return
                    if parsed.path=="/api/v1/ops/snapshot":snap=portal.ops.snapshot(identity);self._json(200,{"summary":snap.summary,"jobs":snap.jobs});return
                    if parsed.path=="/api/v1/system-health":
                        if not self._global_admin(identity):self._json(403,{"error":"global admin tenant scope required"});return
                        self._json(200,portal.engine.status());return
                    if parsed.path=="/api/v1/platforms":self._json(200,[{"name":p.name,"type":p.type,"enabled":p.enabled,"tenant":portal.cfg.tenant_for_platform(p.name)} for p in portal.cfg.platforms if p.enabled and self._allowed_platform(identity,p.name)]);return
                    if parsed.path=="/api/v1/storage-targets":self._json(200,portal.engine.storage_targets());return
                    if parsed.path=="/api/v1/vms":
                        rows=[]
                        for vm in portal.engine.state.list_vms():
                            if self._allowed_point(identity,vm):vm["tenant"]=portal.cfg.tenant_for_platform(str(vm["platform"]));rows.append(vm)
                        self._json(200,rows);return
                    if parsed.path=="/api/v1/recovery-points":
                        q=parse_qs(parsed.query);rows=portal.engine.list_recovery_points(platform=(q.get("platform") or [None])[0],vm_id=(q.get("vm_id") or [None])[0]);allowed=[]
                        for row in rows:
                            if self._allowed_point(identity,row):row["application_consistency"]=point_consistency(row);row["tenant"]=portal.cfg.tenant_for_platform(str(row["platform"]));allowed.append(row)
                        self._json(200,allowed);return
                    if parsed.path=="/api/v1/restores":
                        rows=[]
                        for row in portal.engine.state.list_restore_requests():
                            if not self._allowed_platform(identity,str(row["source_platform"])) or (identity.role not in {"admin","approver"} and row["requester"]!=identity.subject):continue
                            row["tenant"]=portal.cfg.tenant_for_platform(str(row["source_platform"]));rows.append(row)
                        self._json(200,rows);return
                    if parsed.path.startswith("/api/v1/flr/sessions/") and parsed.path.endswith("/browse"):
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        sid=parsed.path.split("/")[5];q=parse_qs(parsed.query);self._json(200,portal.flr.list_directory(sid,(q.get("path") or ["/"])[0],actor=identity.subject,admin=False));return
                    if parsed.path.startswith("/api/v1/flr/sessions/") and parsed.path.endswith("/download"):
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        sid=parsed.path.split("/")[5];q=parse_qs(parsed.query);requested=(q.get("path") or [""])[0];file=portal.flr.open_file(sid,requested,actor=identity.subject,admin=False);portal.engine.state.audit(identity.subject,"flr.file.download","flr_session",sid,{"path":requested,"size":file.size});self._send_file(file);return
                    if parsed.path=="/api/v1/audit":
                        if not self._global_admin(identity):self._json(403,{"error":"global admin tenant scope required"});return
                        self._json(200,portal.engine.state.list_audit());return
                    if parsed.path=="/api/v1/audit/verify":
                        if not self._global_admin(identity):self._json(403,{"error":"global admin tenant scope required"});return
                        ok,errors=portal.engine.state.verify_audit_chain();self._json(200 if ok else 409,{"valid":ok,"errors":errors});return
                    if parsed.path=="/api/v1/manage/status":self._json(200,self._manage(identity,"status"));return
                    if parsed.path=="/api/v1/manage/dashboard":self._json(200,self._manage(identity,"dashboard"));return
                    if parsed.path=="/api/v1/manage/platforms":self._json(200,self._manage(identity,"platforms"));return
                    if parsed.path=="/api/v1/manage/policies":self._json(200,self._manage(identity,"policies"));return
                    self._json(404,{"error":"not found"})
                except PermissionError as exc:self._json(403,{"error":str(exc)})
                except ValueError as exc:self._json(400,{"error":str(exc)})
                except Exception as exc:print(f"portal GET error: {type(exc).__name__}: {exc}");self._json(500,{"error":"internal server error"})

            def do_POST(self)->None:
                parsed=urlparse(self.path)
                try:
                    identity=self._require({"viewer","restore_operator","approver","admin"})
                    if not identity:return
                    if parsed.path=="/api/v1/ws-ticket":
                        if not portal.ws or not portal.signer:self._json(404,{"error":"WebSocket telemetry is disabled"});return
                        self._json(201,{"ticket":portal.ops.issue_ws_ticket(identity,portal.signer),"expires_in":portal.cfg.observability.websocket_ticket_ttl_seconds});return
                    if parsed.path=="/api/v1/flr/sessions":
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        body=self._body();point=self._point(str(body.get("snapshot_id") or ""),identity)
                        if not point:return
                        session=portal.flr.open_session(point,actor=identity.subject);portal.engine.state.audit(identity.subject,"flr.session.open","recovery_point",str(point["snapshot_id"]),{"session_id":session["session_id"],"read_only":True});self._json(201,session);return
                    if parsed.path=="/api/v1/restores":
                        if identity.role not in {"restore_operator","admin"}:self._json(403,{"error":"restore_operator or admin role required"});return
                        body=self._body();point=self._point(str(body.get("snapshot_id") or ""),identity)
                        if not point:return
                        target=str(body.get("target_platform") or "")
                        if not self._allowed_platform(identity,target):self._json(403,{"error":"target platform outside your tenant scope"});return
                        if portal.cfg.tenant_for_platform(target)!=portal.cfg.tenant_for_platform(str(point["platform"])):self._json(403,{"error":"cross-tenant restore is prohibited"});return
                        rid=portal.engine.request_restore(snapshot_id=str(point["snapshot_id"]),requester=identity.subject,target_platform=target,target_name=body.get("target_name"),options=dict(body.get("options") or {}));self._json(201,{"request_id":rid,"status":(portal.engine.state.get_restore_request(rid) or {}).get("status")});return
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
                    manage_routes={
                        "/api/v1/manage/platform/test":"platform_test","/api/v1/manage/platform/save":"platform_save","/api/v1/manage/platform/discover":"platform_discover","/api/v1/manage/protection/save":"protection_save","/api/v1/manage/storage/test":"storage_test","/api/v1/manage/storage/save":"storage_save","/api/v1/manage/storage/init":"storage_init","/api/v1/manage/doctor":"doctor","/api/v1/manage/backup-dry-run":"backup_dry_run","/api/v1/manage/backup-run":"backup_run","/api/v1/manage/immutable-verify":"immutable_verify","/api/v1/manage/policy/save":"policy_save","/api/v1/manage/policy/delete":"policy_delete","/api/v1/manage/policy/run":"policy_run","/api/v1/manage/dr-test-network/save":"dr_test_network_save"
                    }
                    if parsed.path in manage_routes:self._json(200,self._manage(identity,manage_routes[parsed.path],self._body()));return
                    self._json(404,{"error":"not found"})
                except PermissionError as exc:self._json(403,{"error":str(exc)})
                except ValueError as exc:self._json(400,{"error":str(exc)})
                except Exception as exc:print(f"portal POST error: {type(exc).__name__}: {exc}");self._json(500,{"error":"internal server error"})

            def do_DELETE(self)->None:
                try:
                    identity=self._require({"restore_operator","admin"})
                    if not identity:return
                    parsed=urlparse(self.path)
                    if parsed.path.startswith("/api/v1/flr/sessions/"):
                        parts=parsed.path.strip("/").split("/")
                        if len(parts)!=5:self._json(404,{"error":"not found"});return
                        sid=parts[4];portal.flr.close_session(sid,actor=identity.subject,force=False);portal.engine.state.audit(identity.subject,"flr.session.close","flr_session",sid,{});self._json(200,{"session_id":sid,"status":"closed"});return
                    self._json(404,{"error":"not found"})
                except PermissionError as exc:self._json(403,{"error":str(exc)})
                except ValueError as exc:self._json(400,{"error":str(exc)})
                except Exception as exc:print(f"portal DELETE error: {type(exc).__name__}: {exc}");self._json(500,{"error":"internal server error"})

        httpd=ThreadingHTTPServer((self.cfg.portal.listen,self.cfg.portal.port),Handler)
        if self.cfg.portal.tls_cert and self.cfg.portal.tls_key:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2;ctx.load_cert_chain(self.cfg.portal.tls_cert,self.cfg.portal.tls_key);httpd.socket=ctx.wrap_socket(httpd.socket,server_side=True)
        print(f"Immutavault v1.1 unified portal listening on {self.cfg.portal.listen}:{self.cfg.portal.port}")
        try:httpd.serve_forever()
        finally:
            if self.ws:self.ws.stop()


Portal = EnterprisePortal
