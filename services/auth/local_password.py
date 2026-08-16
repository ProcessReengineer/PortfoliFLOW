# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Local password (Argon2id) authentication backend.

Implementation per ADR-0036 §1, amended by ADR-0063 §1. The backend:

1. Accepts a pre-resolved ``tenant_id`` from the caller. The route
   layer (``web/routes/login.py``) resolves the tenant via the
   :class:`services.tenant_resolution.TenantResolver` *before*
   invoking the backend — the backend is responsible for credential
   verification, not tenant routing.

2. Runs the candidate password through Argon2id verification against
   the user's stored hash inside a tenant-scoped session. The
   verification path is **constant-time across the unknown-user
   case**: if the user does not exist *in the resolved tenant*, a
   dummy Argon2id verify is executed against a known-bad hash so
   the timing of "no such user", "wrong tenant", and "wrong password"
   are indistinguishable.

3. Enforces account lockout (5 failed attempts within 15 minutes)
   *before* attempting verification. The lockout is per-email, not
   per-IP, per OWASP guidance — per-IP lockout enables denial-of-
   service against legitimate users behind shared NATs.

4. Writes a row to ``login_audit`` for every attempt, regardless of
   outcome. The write goes through the **superuser engine** because
   at the moment of a failed login the application does not yet have
   a trusted ``app.tenant_id`` — a tenant-scoped session would either
   fail RLS or require a chicken-and-egg trick. The asymmetry is
   deliberate; a regression test asserts the superuser engine is
   used only for ``login_audit`` writes (see
   ``tests/regression/test_audit_engine_only_writes_login_audit.py``).

Lockout threshold and window are module-level constants. Phase 5
moves them to per-tenant configuration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO, UserRepository
from services.auth.backend import AuthBackend
from services.password_hashing import hash_password, verify_password

_LOG = logging.getLogger(__name__)

# Account-lockout policy (ADR-0036 §8). Module-level constants in
# Phase 2; per-tenant overrides become real in Phase 5.
LOCKOUT_THRESHOLD: int = 5
LOCKOUT_WINDOW: timedelta = timedelta(minutes=15)

# A pre-computed Argon2id hash of an arbitrary string, used for the
# constant-time dummy verify when the user is absent. The exact
# plaintext is irrelevant — verification will always fail because the
# candidate password will not match. Using a real hash (rather than a
# string the verifier rejects as malformed) is what makes the timing
# match the "user found, wrong password" path.
_DUMMY_HASH: str = hash_password("dummy_password_for_constant_time_verification")

# Failure-reason codes recorded in ``login_audit.failure_reason``.
_REASON_INVALID_CREDENTIALS: str = "invalid_credentials"
_REASON_USER_INACTIVE: str = "user_inactive"
_REASON_LOCKOUT: str = "lockout"
_REASON_NO_PASSWORD_HASH: str = "no_password_hash"


class LocalPasswordAuthBackend(AuthBackend):
    """Authenticate ``(email, password)`` against the ``users`` table.

    The backend is constructed once at application startup with the
    application engine (``portfoliflow_app``) and the audit engine
    (superuser). Both engines are owned by the caller; the backend
    does not dispose them.

    Args:
        app_engine: The unprivileged application engine, used for
            tenant-scoped reads of the ``users`` table.
        audit_engine: The superuser engine, used **exclusively** for
            ``login_audit`` inserts. Holding a separate handle makes
            the asymmetry visible — and easy to assert on in
            regression tests.
    """

    def __init__(
        self,
        app_engine: AsyncEngine,
        audit_engine: AsyncEngine,
    ) -> None:
        self._app_engine = app_engine
        self._audit_engine = audit_engine

    async def authenticate(
        self,
        email: str,
        password: str | None = None,
        *,
        tenant_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserDTO | None:
        """Authenticate ``(email, password)`` against ``tenant_id``.

        Returns the authenticated :class:`UserDTO` on success, or
        ``None`` for any failure path. The detailed reason is written
        to ``login_audit`` but never leaked to the caller.

        Per ADR-0063 §1 the caller (the login route) is responsible
        for resolving the tenant from the request *before* invoking
        the backend. A mismatch between the caller-supplied
        ``tenant_id`` and the user's actual tenant resolves
        structurally to "user not found in this tenant" — the dummy
        verify path runs, an audit row records the attempt, and the
        timing matches the wrong-password path.
        """
        if password is None:
            # The local backend requires a password. A None here is a
            # programming error from the route; record it as
            # invalid_credentials and return.
            await self._record_attempt(
                email=email,
                tenant_id=tenant_id,
                user_id=None,
                success=False,
                failure_reason=_REASON_INVALID_CREDENTIALS,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            return None

        # ---- 1. Lockout check (per-email, queries audit log) ---------------
        if await self._is_locked_out(email):
            await self._record_attempt(
                email=email,
                tenant_id=tenant_id,
                user_id=None,
                success=False,
                failure_reason=_REASON_LOCKOUT,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            # Constant-time discipline: still pay the cost of a verify
            # so the lockout response time matches the verify path.
            verify_password(password, _DUMMY_HASH)
            return None

        # ---- 2. User lookup + verification ---------------------------------
        async with tenant_context(self._app_engine, tenant_id) as session:
            repo = UserRepository(session)
            user = await repo.get_by_email(email)

            if user is None:
                # Unknown user inside the resolved tenant: dummy-verify.
                # The dummy verify must be inside the same code path so
                # the timing matches the user-found path that follows.
                verify_password(password, _DUMMY_HASH)
                await self._record_attempt(
                    email=email,
                    tenant_id=tenant_id,
                    user_id=None,
                    success=False,
                    failure_reason=_REASON_INVALID_CREDENTIALS,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                return None

            if not user.is_active:
                # Still pay the verify cost so the timing of an
                # inactive account matches the active-user paths.
                verify_password(password, _DUMMY_HASH)
                await self._record_attempt(
                    email=email,
                    tenant_id=tenant_id,
                    user_id=user.id,
                    success=False,
                    failure_reason=_REASON_USER_INACTIVE,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                return None

            if user.password_hash is None:
                # OIDC-only user (Phase 5) with no local credential.
                verify_password(password, _DUMMY_HASH)
                await self._record_attempt(
                    email=email,
                    tenant_id=tenant_id,
                    user_id=user.id,
                    success=False,
                    failure_reason=_REASON_NO_PASSWORD_HASH,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                return None

            if not verify_password(password, user.password_hash):
                await self._record_attempt(
                    email=email,
                    tenant_id=tenant_id,
                    user_id=user.id,
                    success=False,
                    failure_reason=_REASON_INVALID_CREDENTIALS,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                return None

        # ---- 4. Success ----------------------------------------------------
        await self._record_attempt(
            email=email,
            tenant_id=tenant_id,
            user_id=user.id,
            success=True,
            failure_reason=None,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return user

    # ---- internals ---------------------------------------------------------

    async def _is_locked_out(self, email: str) -> bool:
        """Return True if ``email`` has too many recent failed attempts.

        Queries ``login_audit`` via the audit engine (superuser) so
        the lockout check works even when no tenant can be resolved
        for the email (e.g. after the very first attempt against a
        wholly unknown email).
        """
        # The threshold is computed in Python to avoid asyncpg's
        # "NOW() - interval" type-inference quirk (Postgres rejects
        # the comparison without an explicit cast).
        threshold = datetime.now(timezone.utc) - LOCKOUT_WINDOW
        async with self._audit_engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM login_audit
                    WHERE email_attempted = :email
                      AND success = FALSE
                      AND created_at > :threshold
                    """
                ),
                {"email": email, "threshold": threshold},
            )
            count = int(result.scalar_one())
        return count >= LOCKOUT_THRESHOLD

    async def _record_attempt(
        self,
        *,
        email: str,
        tenant_id: UUID | None,
        user_id: UUID | None,
        success: bool,
        failure_reason: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Insert a row into ``login_audit`` via the superuser engine.

        The superuser engine is the *only* engine ever used for
        ``login_audit`` writes; a regression test asserts this
        invariant.
        """
        async with self._audit_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO login_audit (
                        tenant_id, user_id, email_attempted,
                        ip_address, user_agent, success, failure_reason
                    ) VALUES (
                        :tenant_id, :user_id, :email,
                        :ip_address, :user_agent, :success, :failure_reason
                    )
                    """
                ),
                {
                    "tenant_id": str(tenant_id) if tenant_id else None,
                    "user_id": str(user_id) if user_id else None,
                    "email": email,
                    "ip_address": ip_address,
                    "user_agent": user_agent,
                    "success": success,
                    "failure_reason": failure_reason,
                },
            )
