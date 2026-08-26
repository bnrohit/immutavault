from __future__ import annotations

from typing import Any

from . import portal_v11 as base
from .enterprise_ops import EnterpriseOps
from .managed_engine import ManagedBackupEngine


# Keep the large HTML shell in one place while adding the v1.1 recovery-test
# action. The standard restore API is intentionally reused so RBAC, tenant scope
# and four-eyes approval remain authoritative.
UI = base.UI.replace(
    "'>Full VM</button>`}",
    "'>Full VM</button> <button class=\"good\" onclick='requestDrTest(${JSON.stringify(x.snapshot_id)},${JSON.stringify(x.vm_name)},${JSON.stringify(x.available_restore_sources||[\"primary\"])})'>Run DR Test</button>`}",
)
UI = UI.replace(
    "authConfig();</script>",
    r'''async function requestDrTest(snapshot,name,sources){
const target=prompt('DR-test target platform:',platforms[0]?.name||'');if(!target)return;
const network=prompt('Isolated recovery network/bridge (must be allow-listed by a global admin):','');if(!network)return;
const source=prompt('Backup copy:',(sources||['primary'])[0]);if(!source)return;
const newName=prompt('Disposable test VM name:',name+'-drtest');if(!newName)return;
try{const r=await api('/api/v1/restores',{method:'POST',body:JSON.stringify({snapshot_id:snapshot,target_platform:target,target_name:newName,options:{source_repository:source,dr_test:{network:network}}})});
if(r.status==='ready'){if(!confirm('Recovery-test request is ready. Restore, isolate, boot-test, power off and auto-clean up now?'))return;const done=await api('/api/v1/restores/'+r.request_id+'/execute',{method:'POST',body:'{}'});alert('DR test complete: '+JSON.stringify(done,null,2));}
else alert('DR test request #'+r.request_id+' created. Four-eyes approval is required before execution.');await loadRestores();}
catch(e){alert(e.message)}}
authConfig();</script>''',
)
base.UI = UI


class EnterprisePortal(base.EnterprisePortal):
    def __init__(self, cfg: Any) -> None:
        super().__init__(cfg)
        self.engine = ManagedBackupEngine(cfg)
        self.ops = EnterpriseOps(cfg, self.engine.state)
        if self.ws is not None:
            self.ws.ops = self.ops


Portal = EnterprisePortal
