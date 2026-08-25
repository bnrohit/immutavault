from __future__ import annotations

# Keep the large HTTP/UI implementation in portal_v09 while applying the
# stronger multi-tenant FLR session policy before EnterprisePortal instances are
# constructed. This also keeps the v0.8 portal module available for release
# archaeology without weakening the v0.9 boundary.
from . import portal_v09
from .enterprise_flr import EnterpriseFLRManager

portal_v09.FLRManager = EnterpriseFLRManager
Portal = portal_v09.EnterprisePortal
EnterprisePortal = portal_v09.EnterprisePortal
