# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared fixtures for ``services.auth`` tests.

Reuses the live-Postgres engine fixtures from
``tests/repositories/conftest.py`` and adds:

- ``seed_sentinel_tenant`` — insert the row carrying
  ``SENTINEL_TENANT_ID`` so tests can run against the same tenant
  that ``LocalPasswordAuthBackend._resolve_tenant`` returns. This is
  the Phase-2 simplification — Phase 5 will let tests pick an
  arbitrary tenant.
- ``seed_user`` — insert a user row with a chosen password
  (Argon2id-hashed) under a given tenant. Returns the plaintext so
  the test can call ``LocalPasswordAuthBackend.authenticate`` with
  matching credentials.
- ``auth_backend`` — pre-built :class:`LocalPasswordAuthBackend`
  bound to the app engine and the superuser engine.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.auth.local_password import LocalPasswordAuthBackend
from services.password_hashing import hash_password

# Re-export the live-DB fixtures from the shared module so pytest
# discovers them in this directory. The fixtures live in
# ``tests._db_fixtures`` so the repository, service, and auth
# packages share one source of truth.
from tests._db_fixtures import (  # noqa: F401  -- fixture re-export
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)


@dataclass(frozen=True)
class SeededUser:
    """Tuple-style return for ``seed_user``."""

    id: UUID
    email: str
    plaintext_password: str
    tenant_id: UUID


@pytest_asyncio.fixture
async def seed_sentinel_tenant(superuser_engine: AsyncEngine):
    """Insert the sentinel tenant row used by Phase-2 auth.

    ``LocalPasswordAuthBackend._resolve_tenant`` always returns
    ``SENTINEL_TENANT_ID``, so authentication tests need this row.
    """

    async def _seed() -> UUID:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
            )
        return SENTINEL_TENANT_ID

    return _seed


@pytest_asyncio.fixture
async def seed_user(superuser_engine: AsyncEngine):
    """Insert a user row and return its handle.

    Hashing is real Argon2id so the verify path is exercised end-to-
    end. The plaintext is returned so tests can log in with matching
    credentials.
    """

    async def _seed(
        tenant_id: UUID,
        email: str = "user@example.com",
        plaintext_password: str = "correct-horse-battery-staple",
        is_active: bool = True,
        is_tenant_owner: bool = False,
        roles: tuple[str, ...] | None = None,
    ) -> SeededUser:
        new_id = uuid4()
        hashed = hash_password(plaintext_password)
        # Back-compat: callers that still pass is_tenant_owner=True
        # get owner-roles; ``roles=`` wins when both are passed.
        effective_roles = (
            list(roles) if roles is not None else (["owner"] if is_tenant_owner else ["member"])
        )
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (id, tenant_id, email, password_hash,
                         roles, is_active)
                    VALUES
                        (:id, :tid, :email, :hash, :roles, :active)
                    """
                ),
                {
                    "id": str(new_id),
                    "tid": str(tenant_id),
                    "email": email,
                    "hash": hashed,
                    "roles": effective_roles,
                    "active": is_active,
                },
            )
        return SeededUser(
            id=new_id,
            email=email,
            plaintext_password=plaintext_password,
            tenant_id=tenant_id,
        )

    return _seed


@pytest_asyncio.fixture
async def auth_backend(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> AsyncGenerator[LocalPasswordAuthBackend, None]:
    """Pre-built ``LocalPasswordAuthBackend`` for tests."""
    yield LocalPasswordAuthBackend(
        app_engine=app_engine,
        audit_engine=superuser_engine,
    )
