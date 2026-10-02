# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""A Postgres advisory lock that keeps database-backed pytest runs apart.

Database-backed tests truncate the shared development database before and
after every test (``tests/_db_fixtures.py`` and the module-local truncate
lists in ``tests/web``). Two such runs at once truncate the schema under each
other. The symptoms are a spurious ``303`` where a ``200`` was expected, and an
``IntegrityError`` on ``users_tenant_id_fkey`` when a tenant row vanishes
between two inserts.

``tests/conftest.py`` makes the rule in ``docs/testing.md`` mechanical: it
calls :meth:`RunLock.ensure` from a listener on every SQLAlchemy pool. The
first time a run opens a database connection, :class:`RunLock` takes a
session-level advisory lock on a dedicated connection and keeps it until the
run ends. A second run that opens a connection while the lock is held gets
:class:`RunLockRefused`, naming the holder, before the connection is handed
to the test. A run that never connects never takes the lock, so database-free
runs stay independent of database-backed ones.

The dedicated connection lives on a private thread with its own event loop:
an asyncpg connection is bound to the loop that created it, and a test process
creates and closes many loops.

Only pytest runs take the lock. Other clients of the database, such as
``portfoliflow bootstrap`` or the web server, are not covered.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import asyncpg
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

#: The advisory-lock key: arbitrary but fixed. It is below 2**31, so
#: ``pg_locks`` shows it as ``classid = 0, objid = RUN_LOCK_KEY, objsubid = 1``.
RUN_LOCK_KEY = 0x5046_4C57

#: Prefix of the lock connection's ``application_name``. The full name adds
#: the process id and the start time, and the refusal message quotes it.
APPLICATION_NAME_PREFIX = "portfoliflow-pytest"

_HOLDER_SQL = (
    "SELECT a.application_name, a.backend_start "
    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'advisory' AND l.granted "
    "AND l.classid = 0 AND l.objid = $1 AND l.objsubid = 1"
)

_T = TypeVar("_T")


class RunLockRefused(RuntimeError):
    """Another pytest run holds the run lock."""


def database_url(rootdir: Path) -> str | None:
    """Return the SQLAlchemy URL the lock is taken on, or ``None``.

    The process environment wins over ``.env``, as in
    ``tests/_db_fixtures.py``. The superuser URL is preferred; the
    application URL names the same database and serves when it is the only
    one set. ``.env`` is read, not loaded: this module leaves the environment
    unchanged.
    """
    url = os.environ.get("DATABASE_URL_SUPERUSER") or os.environ.get("DATABASE_URL")
    if not url:
        values = dotenv_values(rootdir / ".env")
        url = values.get("DATABASE_URL_SUPERUSER") or values.get("DATABASE_URL")
    return url or None


def _application_name() -> str:
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{APPLICATION_NAME_PREFIX} pid={os.getpid()} {started}"


def _refusal_message(holder: tuple[str, datetime] | None) -> str:
    if holder is None:
        who = "a run this role cannot see, or one that has just finished"
    else:
        name, since = holder
        who = f"{name}, holding it since {since.isoformat(timespec='seconds')}"
    return (
        f"Another pytest run is using the test database ({who}). "
        "Database-backed runs must not overlap: each truncates the schema under "
        "the other (docs/testing.md, 'The database'). Wait for that run to "
        "finish, or stop it, then start this one again."
    )


class RunLock:
    """The run's advisory lock, taken on first use and held until :meth:`release`."""

    def __init__(self, url: str | None) -> None:
        self._dsn = (
            None
            if url is None
            else make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
        )
        self._mutex = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conn: asyncpg.Connection | None = None
        self._refusal: str | None = None

    def ensure(self) -> None:
        """Take the lock unless this run holds it; raise if another run does."""
        if self._dsn is None or self._conn is not None:
            return
        with self._mutex:
            if self._conn is not None:
                return
            if self._refusal is not None:
                raise RunLockRefused(self._refusal)
            self._start_loop()
            conn, holder = self._run(self._acquire(self._dsn))
            if conn is None:
                self._refusal = _refusal_message(holder)
                raise RunLockRefused(self._refusal)
            self._conn = conn

    def release(self) -> None:
        """Release the lock and stop the private loop. Safe to call twice."""
        with self._mutex:
            loop, thread, conn = self._loop, self._thread, self._conn
            self._loop = self._thread = self._conn = None
            if loop is None:
                return
            try:
                if conn is not None:
                    self._run_on(loop, self._close(conn))
            finally:
                loop.call_soon_threadsafe(loop.stop)
                if thread is not None:
                    thread.join(timeout=5)
                if not loop.is_running():
                    loop.close()

    def _start_loop(self) -> None:
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="pytest-run-lock", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        assert self._loop is not None
        return self._run_on(self._loop, coro)

    @staticmethod
    def _run_on(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, _T]) -> _T:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)

    @staticmethod
    async def _acquire(
        dsn: str,
    ) -> tuple[asyncpg.Connection | None, tuple[str, datetime] | None]:
        conn = await asyncpg.connect(dsn, server_settings={"application_name": _application_name()})
        holder: tuple[str, datetime] | None = None
        try:
            # Two attempts: the holder may finish between the failed try and
            # the lookup, which then finds no row.
            for _attempt in range(2):
                if await conn.fetchval("SELECT pg_try_advisory_lock($1)", RUN_LOCK_KEY):
                    return conn, None
                row = await conn.fetchrow(_HOLDER_SQL, RUN_LOCK_KEY)
                if row is not None:
                    holder = (row["application_name"], row["backend_start"])
                    break
        except BaseException:
            await conn.close()
            raise
        await conn.close()
        return None, holder

    @staticmethod
    async def _close(conn: asyncpg.Connection) -> None:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", RUN_LOCK_KEY)
        finally:
            await conn.close()
