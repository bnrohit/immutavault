from __future__ import annotations

from datetime import datetime, timezone

from .flr import FLRManager


class EnterpriseFLRManager(FLRManager):
    """Tighten v0.8 FLR semantics for the v0.9 multi-tenant portal.

    The v0.8 portal's `admin=True` convenience allowed an administrator to
    inspect another portal user's active FLR session. In a tenant-aware control
    plane an `admin` role may intentionally be limited to one tenant, while an
    FLR session ID itself carries no tenant claim. The safe v0.9 contract is
    therefore owner-only for browse/download/close operations.

    Expiry cleanup still works automatically. Global administrators can always
    create a fresh FLR session against an in-scope recovery point instead of
    attaching to someone else's mounted guest filesystem.
    """

    def _session(self, session_id: str, *, actor: str, admin: bool = False):
        # Deliberately ignore the inherited admin bypass. Session ownership is a
        # stronger boundary than role for short-lived mounted guest filesystems.
        return super()._session(session_id, actor=actor, admin=False)

    def close_session(self, session_id: str, *, actor: str | None = None, force: bool = False) -> None:
        with self._lock:
            session = self._sessions.get(session_id)

        if session is not None:
            expired = session.expires_at <= datetime.now(timezone.utc)
            if actor is None and not expired:
                raise PermissionError("active FLR sessions can only be closed by their owner")
            if actor is not None and session.owner != actor and not expired:
                raise PermissionError("FLR session belongs to another portal user")
            # Use the real owner for automatic expiry cleanup so the inherited
            # implementation performs a normal owner-authorized close.
            effective_actor = session.owner if expired else actor
            return super().close_session(session_id, actor=effective_actor, force=False)

        # Missing sessions are allowed to clean up an orphaned mount directory
        # only for the manager's internal force-cleanup path.
        return super().close_session(session_id, actor=actor, force=force)
