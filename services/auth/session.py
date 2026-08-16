# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Session storage for the local auth backend.

Implements the Phase-2 session model from ADR-0036 §1: server-side
sessions persisted in Postgres, identified by a 256-bit random
``session_token`` stored in plaintext (rotation, not hashing, is the
protection — OWASP session-management guidance is explicit). A second
random token bound to the session, ``csrf_token``, is the value the
CSRF dependency compares against on every mutating request.

Sessions have two timeouts:

- :data:`IDLE_TIMEOUT` — the session expires when ``last_seen_at`` is
  older than the idle window. ``last_seen_at`` is bumped via
  :meth:`SessionRepository.touch` on every authenticated request.
- :data:`ABSOLUTE_TIMEOUT` — the session expires at ``expires_at``
  regardless of activity. Set to ``created_at + ABSOLUTE_TIMEOUT`` at
  creation time and never extended; rotation requires a fresh login.

Both bounds are enforced inside :meth:`SessionRepository.get_by_token`:
an expired session is treated as absent and the caller's
``require_session`` dependency redirects to the login page.

The repository is tenant-scoped — sessions are written and read inside
a :func:`tenant_context` block, which sets ``app.tenant_id`` on the
connection and lets RLS filter cross-tenant access. The login route
acquires the session-creating context with the just-authenticated
user's tenant id; everything else uses the cookie-derived session
already bound to its tenant via this row.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

from core.repositories.base import BaseRepository
from core.repositories.user_repository import UserDTO

# Per ADR-0036 §1: idle 8 hours, absolute 24 hours.
IDLE_TIMEOUT: timedelta = timedelta(hours=8)
ABSOLUTE_TIMEOUT: timedelta = timedelta(hours=24)

# Per ADR-0065 §1a: collapse the per-request touch write-storm to at
# most one UPDATE per session per window. A 60-second granularity is
# invisible to an 8-hour idle timeout.
TOUCH_THROTTLE_SECONDS: int = 60


@dataclass(frozen=True)
class SessionDTO:
    """Plain data-only view of a ``sessions`` row.

    Returned by every repository method so callers do not depend on
    SQLAlchemy lifecycle. The ``session_token`` is the value placed
    in the user's session cookie; ``csrf_token`` is embedded in
    forms / sent via the ``X-CSRF-Token`` header.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    session_token: str
    csrf_token: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


def _now() -> datetime:
    """Return the current UTC time with timezone info attached."""
    return datetime.now(timezone.utc)


class SessionRepository(BaseRepository):
    """CRUD for the ``sessions`` table.

    Sessions live for at most :data:`ABSOLUTE_TIMEOUT` and at most
    :data:`IDLE_TIMEOUT` of inactivity. ``get_by_token`` enforces
    both bounds; expired sessions are returned as ``None`` and
    treated as absent by the caller.

    Construction follows the standard repository pattern: pass a
    tenant-scoped :class:`AsyncSession` (acquired via
    :func:`tenant_context`).
    """

    async def create_session(
        self,
        user: UserDTO,
        ip: str | None,
        ua: str | None,
    ) -> SessionDTO:
        """Create a fresh session for ``user`` and persist it.

        ``session_token`` and ``csrf_token`` are 256-bit random values
        from :func:`secrets.token_urlsafe`. ``expires_at`` is set to
        ``NOW() + ABSOLUTE_TIMEOUT``; the idle bound is enforced
        independently by :meth:`get_by_token`.
        """
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)

        # ``expires_at`` is computed in Python to avoid asyncpg's
        # type-inference quirk on ``NOW() + interval`` placeholders.
        expires_at = _now() + ABSOLUTE_TIMEOUT
        result = await self._session.execute(
            text(
                """
                INSERT INTO sessions (
                    tenant_id, user_id, session_token, csrf_token,
                    expires_at, ip_address, user_agent
                ) VALUES (
                    :tenant_id, :user_id, :session_token, :csrf_token,
                    :expires_at, :ip, :ua
                )
                RETURNING id, tenant_id, user_id, session_token,
                          csrf_token, created_at, last_seen_at, expires_at
                """
            ),
            {
                "tenant_id": str(user.tenant_id),
                "user_id": str(user.id),
                "session_token": session_token,
                "csrf_token": csrf_token,
                "expires_at": expires_at,
                "ip": ip,
                "ua": ua,
            },
        )
        row = result.mappings().one()
        return _row_to_dto(row)

    async def get_by_token(self, session_token: str) -> SessionDTO | None:
        """Return the session matching ``session_token`` or ``None``.

        Returns ``None`` when:

        - No row matches the token.
        - The row's ``expires_at`` is in the past (absolute timeout).
        - The row's ``last_seen_at`` is older than
          :data:`IDLE_TIMEOUT` (idle timeout).

        The caller treats every ``None`` return as "session absent"
        and redirects to login. Hard-deleting expired rows is left
        to the Phase-3 cleanup job that sweeps the
        ``ix_sessions_expires_at`` index — the read path simply
        ignores them.
        """
        result = await self._session.execute(
            text(
                """
                SELECT id, tenant_id, user_id, session_token, csrf_token,
                       created_at, last_seen_at, expires_at
                FROM sessions
                WHERE session_token = :session_token
                """
            ),
            {"session_token": session_token},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        now = _now()
        if row["expires_at"] <= now:
            return None
        if row["last_seen_at"] + IDLE_TIMEOUT <= now:
            return None
        return _row_to_dto(row)

    async def touch(self, session_id: UUID) -> None:
        """Bump ``last_seen_at`` to NOW() — the idle-timeout reset.

        Called on every authenticated request by the session
        middleware. The update intentionally does not extend
        ``expires_at`` — the absolute bound is preserved.
        """
        await self._session.execute(
            text("UPDATE sessions SET last_seen_at = NOW() WHERE id = :session_id"),
            {"session_id": str(session_id)},
        )

    async def touch_throttled(
        self, session_id: UUID, window_seconds: int = TOUCH_THROTTLE_SECONDS
    ) -> None:
        """Bump ``last_seen_at`` at most once per ``window_seconds``.

        Per ADR-0065 §1a. A single atomic conditional UPDATE — no
        read-then-write race. On the common path (a session already
        touched within the window) the statement matches zero rows,
        dirties nothing, and acquires no row lock beyond the statement
        itself. This is what removes the latent ``sessions``-row
        contention without changing the idle-timeout's behaviour.

        ``make_interval(secs => :window)`` is used rather than a
        string-interpolated interval so the window is a bound
        parameter (asyncpg-safe), consistent with the rest of the
        repository.
        """
        await self._session.execute(
            text(
                "UPDATE sessions SET last_seen_at = NOW() "
                "WHERE id = :session_id "
                "AND last_seen_at < NOW() - make_interval(secs => :window)"
            ),
            {"session_id": str(session_id), "window": window_seconds},
        )

    async def delete(self, session_id: UUID) -> None:
        """Delete a single session row — used by the logout route."""
        await self._session.execute(
            text("DELETE FROM sessions WHERE id = :session_id"),
            {"session_id": str(session_id)},
        )

    async def delete_all_for_user(self, user_id: UUID) -> None:
        """Delete every session belonging to ``user_id``.

        Called on password rotation (per OWASP guidance) and on
        explicit "log out everywhere" requests. The CASCADE on
        ``users(id)`` would also handle the case of user deletion,
        but rotation does not delete the user row.
        """
        await self._session.execute(
            text("DELETE FROM sessions WHERE user_id = :user_id"),
            {"user_id": str(user_id)},
        )


def _row_to_dto(row) -> SessionDTO:  # type: ignore[no-untyped-def]
    return SessionDTO(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        session_token=row["session_token"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
    )
