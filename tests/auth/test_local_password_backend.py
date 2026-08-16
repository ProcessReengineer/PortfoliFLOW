# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``LocalPasswordAuthBackend``.

Live-DB tests against the compose Postgres. Per ADR-0063 §1 the
backend takes ``tenant_id`` as an explicit keyword argument from
the caller (the login route); these tests pass it directly.
"""

from __future__ import annotations


import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from services.auth.local_password import (
    LOCKOUT_THRESHOLD,
    LocalPasswordAuthBackend,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_authenticate_succeeds_with_correct_credentials(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    seed_user,
) -> None:
    tenant_id = await seed_sentinel_tenant()
    user = await seed_user(tenant_id, email="ok@example.com")

    result = await auth_backend.authenticate(
        user.email,
        user.plaintext_password,
        tenant_id=tenant_id,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )

    assert result is not None
    assert result.id == user.id
    assert result.email == user.email
    assert result.tenant_id == tenant_id


async def test_authenticate_returns_none_on_wrong_password(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    seed_user,
) -> None:
    tenant_id = await seed_sentinel_tenant()
    user = await seed_user(tenant_id, email="wrong@example.com")

    result = await auth_backend.authenticate(
        user.email,
        "definitely-not-the-password",
        tenant_id=tenant_id,
    )
    assert result is None


async def test_authenticate_returns_none_for_unknown_email(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
) -> None:
    """Unknown user must not authenticate."""
    tenant_id = await seed_sentinel_tenant()

    result = await auth_backend.authenticate(
        "nobody@example.com",
        "any-password",
        tenant_id=tenant_id,
    )
    assert result is None


async def test_authenticate_returns_none_for_inactive_user(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    seed_user,
) -> None:
    tenant_id = await seed_sentinel_tenant()
    user = await seed_user(tenant_id, email="off@example.com", is_active=False)

    result = await auth_backend.authenticate(
        user.email,
        user.plaintext_password,
        tenant_id=tenant_id,
    )
    assert result is None


async def test_authenticate_returns_none_when_user_lives_in_other_tenant(
    auth_backend: LocalPasswordAuthBackend,
    seed_tenant,
    seed_user,
    superuser_engine: AsyncEngine,
) -> None:
    """Wrong-tenant lookup must dummy-verify, audit, and return None.

    Per ADR-0063 §1 the caller resolves the tenant before calling
    the backend. A user that exists in tenant A but is authenticated
    against tenant B must structurally fail through the same path as
    "user not found".
    """
    tenant_a = await seed_tenant(name="A", subdomain="tenant-a")
    tenant_b = await seed_tenant(name="B", subdomain="tenant-b")
    user = await seed_user(tenant_a, email="moved@example.com")

    result = await auth_backend.authenticate(
        user.email,
        user.plaintext_password,
        tenant_id=tenant_b,  # deliberately the wrong tenant
    )

    assert result is None

    # The audit row must be scoped to the *requested* tenant — that
    # is the tenant the attacker tried to reach.
    async with superuser_engine.connect() as conn:
        row = await conn.execute(
            text(
                """
                SELECT tenant_id, failure_reason, success
                FROM login_audit
                WHERE email_attempted = :email
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"email": user.email},
        )
        latest = row.mappings().one()
    assert latest["tenant_id"] == tenant_b
    assert latest["success"] is False
    assert latest["failure_reason"] == "invalid_credentials"


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


async def test_lockout_after_threshold_failed_attempts(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    seed_user,
    superuser_engine: AsyncEngine,
) -> None:
    """``LOCKOUT_THRESHOLD`` failed attempts lock the account.

    The (THRESHOLD+1)-th attempt with the correct password is still
    rejected, and the audit row carries ``failure_reason='lockout'``.
    """
    tenant_id = await seed_sentinel_tenant()
    user = await seed_user(tenant_id, email="lockme@example.com")

    # Burn the threshold with wrong passwords.
    for _ in range(LOCKOUT_THRESHOLD):
        result = await auth_backend.authenticate(
            user.email,
            "still-wrong",
            tenant_id=tenant_id,
        )
        assert result is None

    # Now try with the correct password — must still fail because of
    # the lockout.
    result = await auth_backend.authenticate(
        user.email,
        user.plaintext_password,
        tenant_id=tenant_id,
    )
    assert result is None

    # The most recent audit row must record reason='lockout'.
    async with superuser_engine.connect() as conn:
        row = await conn.execute(
            text(
                """
                SELECT failure_reason, success
                FROM login_audit
                WHERE email_attempted = :email
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"email": user.email},
        )
        latest = row.mappings().one()

    assert latest["success"] is False
    assert latest["failure_reason"] == "lockout"


# ---------------------------------------------------------------------------
# Audit row written for every attempt
# ---------------------------------------------------------------------------


async def test_each_attempt_writes_one_audit_row(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    seed_user,
    superuser_engine: AsyncEngine,
) -> None:
    tenant_id = await seed_sentinel_tenant()
    user = await seed_user(tenant_id, email="auditme@example.com")

    # One success and one failure → exactly two rows for this email.
    await auth_backend.authenticate(user.email, user.plaintext_password, tenant_id=tenant_id)
    await auth_backend.authenticate(user.email, "wrong", tenant_id=tenant_id)

    async with superuser_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT COUNT(*) FROM login_audit WHERE email_attempted = :email"),
            {"email": user.email},
        )
        count = int(row.scalar_one())
    assert count == 2


async def test_authenticate_writes_audit_row_for_unknown_email(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    superuser_engine: AsyncEngine,
) -> None:
    """Unknown emails must still produce an audit row.

    The caller-supplied tenant id is recorded on the row; the
    failure reason is ``invalid_credentials`` because lookup inside
    the requested tenant produced no user.
    """
    tenant_id = await seed_sentinel_tenant()

    await auth_backend.authenticate("ghost@example.com", "any", tenant_id=tenant_id)

    async with superuser_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT failure_reason FROM login_audit WHERE email_attempted = :email"),
            {"email": "ghost@example.com"},
        )
        reason = row.scalar_one()

    assert reason == "invalid_credentials"


@pytest.mark.timing
@pytest.mark.skip(
    reason=(
        "Constant-time discipline test: timing comparisons are flaky "
        "in CI. Kept here for manual verification."
    ),
)
async def test_constant_time_unknown_user_vs_wrong_password(
    auth_backend: LocalPasswordAuthBackend,
    seed_sentinel_tenant,
    seed_user,
) -> None:
    """The unknown-user path must be timing-comparable to wrong-password.

    Marked as a manual-verification test: timing comparisons are
    flaky under variable CI load. Run locally with
    ``pytest -m timing`` and inspect the ratio.
    """
    import time

    tenant_id = await seed_sentinel_tenant()
    user = await seed_user(tenant_id, email="timing@example.com")

    # Wrong password against a real user.
    t0 = time.perf_counter()
    await auth_backend.authenticate(user.email, "wrong-password", tenant_id=tenant_id)
    t_wrong = time.perf_counter() - t0

    # Unknown user.
    t0 = time.perf_counter()
    await auth_backend.authenticate("ghost@example.com", "wrong-password", tenant_id=tenant_id)
    t_unknown = time.perf_counter() - t0

    ratio = max(t_wrong, t_unknown) / min(t_wrong, t_unknown)
    assert ratio < 2.0, f"Timing diverged: wrong={t_wrong}, unknown={t_unknown}"
