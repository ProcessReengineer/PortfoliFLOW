# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow reset-dev`` subcommand.

A destructive operator command that returns the development database
to a known clean state in three phases: every domain table is
truncated, the sentinel tenant + sentinel user are re-installed, and
the full bootstrap seed pipeline (:data:`cli.bootstrap._SEED_STEPS`)
is re-run. The reset database therefore carries the same seed state as
a fresh ``portfoliflow bootstrap`` — SAA seeds, the ``unclassified``
and default asset classes, the ``unclassified`` sector, the default
regions, and the market-data system actor + schedule. The Alembic
``alembic_version`` table is **not** touched — schema migrations stay
in place, and the operator never has to re-run ``alembic upgrade head``
after a reset.

The command is gated by ``--confirm`` so an accidental keystroke
cannot wipe data. It is **never** safe to run against a production
database; ``portfoliflow status`` (sub-stream 3a, Task 3) is the
non-destructive way to inspect the database state.

Sub-stream 3a, Task 2 introduces this command alongside the
test-hygiene fix: ``pytest`` no longer leaks rows into the dev DB,
but operators still need a fast path back to a known clean state
when manual experiments or interrupted tests have left the DB in a
half-configured shape. This is the seam.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import typer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cli import bootstrap as _bootstrap_module
from cli._db import superuser_engine
from cli.bootstrap import (
    _resolve_email,
    _resolve_password,
    _run_bootstrap,
)
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging

_LOG = logging.getLogger("portfoliflow.cli")

# Domain tables truncated by ``reset-dev``. Ordering is irrelevant
# because ``CASCADE`` walks the FK graph.
#
# This list is a historical core set, not an exhaustive inventory of
# the tenant-scoped schema. Completeness is structural, not
# enumerated: every tenant-scoped table carries a direct FK to
# ``tenants`` (the RLS design — see
# ``tests/regression/test_rls_schema_invariants.py``), and ``tenants``
# is itself in the truncate set, so ``TRUNCATE ... CASCADE``
# transitively reaches every tenant-scoped table, including ones
# added after this list was last touched.
#
# The only tables that survive are the intentionally global,
# migration-seeded lookup tables — ``countries`` (ADR-0045 §2) and
# ``anlv_categories`` (ADR-0057). They MUST survive: ``reset-dev``
# preserves migration state and does not re-run migrations, so their
# rows would never come back. ``alembic_version`` is intentionally
# absent — schema state survives a reset.
_DOMAIN_TABLES: tuple[str, ...] = (
    "investment_region_weights",
    "region_country_memberships",
    "regions",
    "investment_country_weights",
    "investment_sector_weights",
    "sectors",
    "investment_cashflows",
    "investment_navs",
    "investments",
    "saa_correlations",
    "saa_asset_class_inputs",
    "saa_configurations",
    "asset_classes",
    "data_upload_sheets",
    "data_uploads",
    "login_audit",
    "sessions",
    "audit_log",
    "data_store_entries",
    "users",
    "tenants",
)


async def _truncate_domain_tables(engine: AsyncEngine) -> None:
    """Truncate every table in :data:`_DOMAIN_TABLES`.

    Uses ``CASCADE`` to follow the FK chain so the order in
    :data:`_DOMAIN_TABLES` does not have to be topologically perfect.
    ``RESTART IDENTITY`` resets sequences (none in Phase 1, but a
    cheap insurance for later).

    Args:
        engine: A superuser-bound :class:`AsyncEngine`.
    """
    table_list = ", ".join(_DOMAIN_TABLES)
    sql = f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"
    async with engine.begin() as conn:
        await conn.execute(text(sql))
    _LOG.info(
        "reset-dev: truncated %d domain tables (%s)",
        len(_DOMAIN_TABLES),
        table_list,
    )


async def _reset_dev_with_engine_lifecycle(email: str, password: str | None) -> None:
    """Build the engine, truncate, re-bootstrap, install seeds, dispose.

    The seed steps are the canonical :data:`cli.bootstrap._SEED_STEPS`
    pipeline, iterated through the module reference so this command seeds
    exactly what ``portfoliflow bootstrap`` seeds. That parity is the point:
    a reset leaves the dev database in the same shape a fresh bootstrap
    would, and a newly added seed step reaches both commands at once.
    """
    engine = superuser_engine()
    try:
        await _truncate_domain_tables(engine)
        sentinel_user_id = await _run_bootstrap(engine, email, password)
        for _seed_step in _bootstrap_module._SEED_STEPS:
            await _seed_step(engine, sentinel_user_id)
    finally:
        await engine.dispose()


def reset_dev_command(
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help=(
            "REQUIRED. Confirms the destructive intent. "
            "Without this flag the command refuses to run."
        ),
    ),
    email: str | None = typer.Option(
        None,
        "--email",
        help="Sentinel email; falls back to SENTINEL_EMAIL env var.",
    ),
    password_stdin: bool = typer.Option(
        False,
        "--password-stdin",
        help=(
            "Read the sentinel password from stdin (one line). Falls "
            "back to SENTINEL_PASSWORD env var when this flag is "
            "absent. Required when the previous DB state had no "
            "sentinel user — after a reset, the sentinel is created "
            "from scratch."
        ),
    ),
) -> None:
    """Truncate every domain table, re-bootstrap the sentinel and re-run the full seed pipeline.

    Destructive — only safe in development environments. The sentinel tenant
    and user are re-installed and the canonical bootstrap seed pipeline is
    re-run, so the reset database matches a fresh ``portfoliflow bootstrap``.
    ``alembic_version`` is preserved so the schema migrations remain in place.

    Exits non-zero on missing ``--confirm``, on missing sentinel
    credentials, or on any drift detected during the post-reset
    bootstrap.
    """
    configure_logging()

    if not confirm:
        _LOG.error(
            "reset-dev: refusing to run without --confirm. This command "
            "truncates every domain table; pass --confirm to proceed."
        )
        raise typer.Exit(code=2)

    # Production guard: a defensive check on DATABASE_URL_SUPERUSER
    # so a misconfigured operator cannot wipe a non-dev database. The
    # check is heuristic — operators are responsible for their own
    # .env discipline — but it costs nothing and catches the obvious
    # foot-gun where ``portfoliflow_dev`` was renamed for a staging
    # smoke-test and the operator forgot to switch back.
    superuser_url = os.getenv("DATABASE_URL_SUPERUSER", "")
    if superuser_url and "portfoliflow_dev" not in superuser_url:
        _LOG.error(
            "reset-dev: DATABASE_URL_SUPERUSER does not target "
            "portfoliflow_dev (got %r). Refusing to run.",
            superuser_url,
        )
        raise typer.Exit(code=2)

    try:
        resolved_email = _resolve_email(email)
        resolved_password = _resolve_password(password_stdin)
    except ConfigurationError as exc:
        _LOG.error("reset-dev: %s", exc.message)
        raise typer.Exit(code=2) from exc

    try:
        asyncio.run(_reset_dev_with_engine_lifecycle(resolved_email, resolved_password))
    except ConfigurationError as exc:
        _LOG.error("reset-dev: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("reset-dev: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface clean exit code
        _LOG.error("reset-dev: unexpected failure: %s", exc)
        raise typer.Exit(code=1) from exc

    _LOG.info(
        "reset-dev: completed. Domain tables truncated; sentinel "
        "tenant, user and seed data reinstalled."
    )
    sys.stdout.flush()
