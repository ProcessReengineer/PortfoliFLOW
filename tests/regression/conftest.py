# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-test scratch databases for the Alembic migration guards.

Every ``alembic downgrade`` guard in this package used to run against the
**shared dev database** named by ``DATABASE_URL_SUPERUSER``, which made
whether the suite was green a function of that database's *data* rather than
of the migrations under test. Two migrations are data-dependent on the way
down by design — b027 re-narrows ``ck_investments_investment_type`` and b028
``ck_investment_cashflows_flow_type``, and recreating the narrower CHECK
revalidates every existing row — so once a workbook import has put a
``'cash'`` investment or an ``'investor_flow'`` cashflow in the dev database,
every guard whose downgrade path crosses b027/b028 fails deterministically,
while an emptied database passes. A guard crossing b031 has the mirror-image
problem: its downgrade *drops* ``cases``, ``case_entries`` and
``case_attachments``, destroying real Cases data on the way past.

The fix is isolation, not a relaxation of the downgrade semantics: each guard
gets its own throwaway database, created here, migrated to ``head`` here, and
dropped on teardown. The by-design revalidation stays exactly as it is — the
scratch database is simply empty apart from what a test seeds itself, which is
the state those downgrades are documented to require. The dev database is
never touched by these tests again.

The seam that makes this work is :mod:`db.migrations.env`: it resolves the URL
from the ``DATABASE_URL_SUPERUSER`` *environment variable* and loads ``.env``
without ``override``, so an already-set process variable wins. Each scratch
handle therefore runs the Alembic CLI with that one variable rebound.

Recorded during the roadmap #052 flake-fix session (F1), 2026-07-30.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: Name prefix for scratch databases — greppable, and unmistakably disposable.
_SCRATCH_PREFIX = "portfoliflow_rt_"

#: The unprivileged application role. Cluster-wide, so it already exists for a
#: freshly created database; only its *grants* are per-database.
_APP_ROLE = "portfoliflow_app"

#: The per-database half of ``db/init/01-create-app-role.sh``, mirrored so the
#: app-role engine behaves against a scratch database exactly as it does
#: against the dev one. ``CREATE ROLE`` is omitted (cluster-wide, see above)
#: and ``GRANT CONNECT`` is issued on the maintenance connection instead, since
#: it must precede the first connection to the new database.
_PER_DATABASE_GRANTS = (
    f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}",
    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {_APP_ROLE}",
)


def _rebound(url: str, database: str) -> str:
    """Return *url* pointed at *database*, password intact.

    ``str(URL)`` renders the password as ``***`` — a URL that parses but does
    not authenticate. ``render_as_string(hide_password=False)`` is the only
    form that survives the round trip.
    """
    return make_url(url).set(database=database).render_as_string(hide_password=False)


@dataclass(frozen=True)
class ScratchDatabase:
    """Handle on one throwaway database, already migrated to ``head``.

    Attributes:
        name: The database's name — always ``portfoliflow_rt_<hex8>``.
        superuser_url: Superuser URL bound to this database.
        app_url: ``portfoliflow_app`` URL bound to this database, or ``None``
            when ``DATABASE_URL`` is unset (only the b029 guard needs it).
    """

    name: str
    superuser_url: str
    app_url: str | None

    def alembic(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the Alembic CLI against *this* database.

        A fresh subprocess, so it does not contend with a test's own
        connections, with ``DATABASE_URL_SUPERUSER`` rebound to the scratch
        database — which is what keeps a downgrade off the dev one.

        Args:
            *args: Alembic subcommand and arguments, e.g. ``("upgrade", "head")``.

        Returns:
            The completed process; the caller asserts on ``returncode``.
        """
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "db/alembic.ini", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "DATABASE_URL_SUPERUSER": self.superuser_url},
        )


async def _mirror_per_database_grants(scratch: ScratchDatabase) -> None:
    """Apply :data:`_PER_DATABASE_GRANTS` inside the new database."""
    engine = create_async_engine(scratch.superuser_url, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for statement in _PER_DATABASE_GRANTS:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def scratch_db() -> AsyncGenerator[ScratchDatabase, None]:
    """Create, migrate and finally drop one throwaway database per test.

    Skips — like every other live-DB guard in this package — when
    ``DATABASE_URL_SUPERUSER`` is unset or the server is unreachable.
    """
    if not DATABASE_URL_SUPERUSER:
        pytest.skip("DATABASE_URL_SUPERUSER not set; cannot run migration guard.")

    # AUTOCOMMIT: CREATE DATABASE and DROP DATABASE cannot run inside a
    # transaction block. This connection stays on the URL's *own* database and
    # only ever issues cluster-level DDL against the scratch one.
    maintenance = create_async_engine(
        DATABASE_URL_SUPERUSER,
        future=True,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with maintenance.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await maintenance.dispose()
        pytest.skip(f"Cannot reach Postgres at {DATABASE_URL_SUPERUSER!r}: {exc}.")

    name = f"{_SCRATCH_PREFIX}{uuid4().hex[:8]}"
    scratch = ScratchDatabase(
        name=name,
        superuser_url=_rebound(DATABASE_URL_SUPERUSER, name),
        app_url=_rebound(DATABASE_URL, name) if DATABASE_URL else None,
    )
    try:
        async with maintenance.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO {_APP_ROLE}'))
        await _mirror_per_database_grants(scratch)

        built = scratch.alembic("upgrade", "head")
        assert built.returncode == 0, f"scratch-database build failed:\n{built.stderr}"

        yield scratch
    finally:
        # Always, including on a mid-test failure: the whole point is that no
        # guard can leave state behind. WITH (FORCE) terminates any backend
        # still attached (Postgres 13+; the compose image is 16), so a leaked
        # connection cannot strand the database either.
        async with maintenance.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await maintenance.dispose()


@pytest_asyncio.fixture
async def scratch_superuser_engine(
    scratch_db: ScratchDatabase,
) -> AsyncGenerator[AsyncEngine, None]:
    """Superuser engine on the scratch database.

    Disposed before :func:`scratch_db` drops the database — pytest tears a
    dependent fixture down first.
    """
    engine = create_async_engine(scratch_db.superuser_url, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def scratch_app_engine(scratch_db: ScratchDatabase) -> AsyncGenerator[AsyncEngine, None]:
    """The unprivileged role on the scratch database — real RLS applies."""
    if scratch_db.app_url is None:
        pytest.skip("DATABASE_URL not set; cannot connect as the application role.")
    engine = create_async_engine(scratch_db.app_url, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()
