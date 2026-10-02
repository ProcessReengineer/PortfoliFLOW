# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""A database-backed pytest run holds the run lock (``tests/_run_lock.py``).

The test opens one SQLAlchemy connection, which makes this run take the lock
through ``tests/conftest.py`` if it does not hold it yet. From that
connection's own session it then checks that the lock is taken by another
session, and that the holder is this process. It writes nothing and truncates
nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests._run_lock import APPLICATION_NAME_PREFIX, RUN_LOCK_KEY, RunLockRefused, database_url

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HOLDER_NAME_SQL = text(
    "SELECT a.application_name "
    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.granted "
    "AND l.classid = 0 AND l.objid = :key AND l.objsubid = 1"
)


async def test_a_database_backed_run_holds_the_run_lock() -> None:
    url = database_url(_REPO_ROOT)
    if url is None:
        pytest.skip("DATABASE_URL_SUPERUSER / DATABASE_URL not set; no database to lock.")
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        try:
            conn = await engine.connect()
        except RunLockRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - unreachable database is a skip, as elsewhere
            pytest.skip(f"Cannot reach Postgres at {url!r}: {exc}")
        try:
            taken_here = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": RUN_LOCK_KEY}
            )
            if taken_here:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_LOCK_KEY})
            holder = await conn.scalar(_HOLDER_NAME_SQL, {"key": RUN_LOCK_KEY})
        finally:
            await conn.close()
    finally:
        await engine.dispose()

    assert not taken_here, "a second session could take the run lock: this run does not hold it"
    assert holder is not None
    assert holder.startswith(f"{APPLICATION_NAME_PREFIX} pid={os.getpid()} "), holder
