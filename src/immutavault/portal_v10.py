from __future__ import annotations

# v1.0 deliberately layers on the hardened v0.9 enterprise portal rather than
# duplicating its OIDC/tenant/WebSocket implementation. The v0.9 shim already
# replaces FLRManager with the owner-only enterprise implementation; here we
# replace only the engine and widen the target picker. Server-side V2V policy is
# still authoritative and rejects every uncertified source/target pair.
from . import portal_enterprise  # noqa: F401 - applies enterprise FLR hardening
from . import portal_v09
from .v2v_engine import CertifiedBackupEngine

portal_v09.BackupEngine = CertifiedBackupEngine
portal_v09.UI = portal_v09.UI.replace(
    "platforms.filter(p=>p.type===type&&p.enabled)",
    "platforms.filter(p=>p.enabled)",
).replace(
    "No enabled '+type+' restore target is configured in your tenant scope",
    "No enabled restore target is configured in your tenant scope",
)

Portal = portal_v09.EnterprisePortal
EnterprisePortal = portal_v09.EnterprisePortal
