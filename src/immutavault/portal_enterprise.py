from __future__ import annotations

# Keep the large HTTP/UI implementation in portal_v09 while replacing direct
# FUSE/libguestfs access with the privilege-separated local FLR broker. The
# broker enforces owner-only mounted-session access and the portal no longer
# needs /dev/fuse, setuid mount helpers, or elevated process privileges.
from . import portal_v09
from .flr_broker import FLRBrokerClient

portal_v09.FLRManager = FLRBrokerClient
Portal = portal_v09.EnterprisePortal
EnterprisePortal = portal_v09.EnterprisePortal
