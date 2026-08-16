# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard for ``super_admin_audit`` schema invariants.

Asserts the post-b014 invariant: ``super_admin_user_id`` is nullable
so the bootstrap path (creating the very first super-admin) can
write an honest "no prior actor" row instead of self-attributing
the row to the newly-created user.

Skips if the live DB is unreachable so contributors without Podman
can still run the rest of the suite.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    if not DATABASE_URL_SUPERUSER:
        pytest.skip("DATABASE_URL_SUPERUSER not set; cannot run schema guard.")
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_super_admin_audit_actor_column_is_nullable(
    superuser_engine: AsyncEngine,
) -> None:
    """b014 relaxed ``super_admin_user_id`` from NOT NULL to NULL."""
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'super_admin_audit'
                  AND column_name = 'super_admin_user_id'
                """
            )
        )
        row = result.first()
    assert row is not None, "super_admin_audit.super_admin_user_id missing"
    assert row.is_nullable == "YES", (
        "super_admin_audit.super_admin_user_id must be nullable after b014 — "
        "the bootstrap path writes NULL to record 'no prior actor'."
    )
