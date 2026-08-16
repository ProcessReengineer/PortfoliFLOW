# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow seed-watchpoints`` — re-run the default watchpoint seed.

Tenant provisioning already installs the ADR-0116 §8 defaults, but it
runs before any data exists: at ``bootstrap`` / ``create-tenant`` time a
tenant has no book, so only the two singletons (``freshness``,
``liquidity``) can be installed. The currency pairs an ``fx`` watchpoint
needs — and, for the demo tenant, the instruments a ``price`` watchpoint
names — only become derivable once a workbook has been imported.

This subcommand is that second run. It is the same installer, so it is
idempotent on the subject key: nothing already present is touched, a
revised threshold survives, and re-running it twice creates nothing the
second time. It reports how many watchpoints it created.

Typical use, once after the first import::

    portfoliflow seed-watchpoints                  # the primary tenant
    portfoliflow seed-watchpoints --tenant <uuid>  # any other tenant

Like every other CLI subcommand it connects as the Postgres superuser
(ADR-0040 §2) and scopes each write through ``tenant_context``, so RLS
and the audit trigger behave exactly as they do for the web surface.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import typer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cli._db import superuser_engine
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging
from core.tenant_constants import PRIMARY_TENANT_ID
from services.watch_desk.seeding import install_default_watchpoints_for_tenant

_LOG = logging.getLogger("portfoliflow.cli")


async def _resolve_actor(engine: AsyncEngine, tenant_id: UUID) -> UUID:
    """Return an owner of ``tenant_id`` to attribute the seeded rows to.

    The audit trigger records ``app.user_id``, so the seed needs a real
    actor. An owner is the right one — these are that tenant's defaults,
    and no system actor "decided" them.

    Args:
        engine: Superuser engine.
        tenant_id: The tenant being seeded.

    Returns:
        The id of an active owner, preferring the oldest account.

    Raises:
        PortfoliFlowError: If the tenant has no active owner, which means
            it was never provisioned and seeding it would be premature.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id FROM users "
                    "WHERE tenant_id = :tid AND 'owner' = ANY(roles) AND is_active "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"tid": str(tenant_id)},
            )
        ).first()
    if row is None:
        raise PortfoliFlowError(
            f"seed-watchpoints: tenant {tenant_id} has no active owner. "
            "Provision it first (`portfoliflow bootstrap` or "
            "`portfoliflow create-tenant`)."
        )
    return UUID(str(row.id))


async def _seed_with_engine_lifecycle(tenant_id: UUID) -> int:
    engine = superuser_engine()
    try:
        actor_id = await _resolve_actor(engine, tenant_id)
        return await install_default_watchpoints_for_tenant(engine, tenant_id, actor_id)
    finally:
        await engine.dispose()


def seed_watchpoints_command(
    tenant: str | None = typer.Option(
        None,
        "--tenant",
        help="Target tenant UUID; defaults to the primary tenant.",
    ),
) -> None:
    """Install any default watchpoints the tenant does not have yet.

    Idempotent: run it as often as you like. Exits non-zero on a missing
    tenant, an unprovisioned one, or a database failure.
    """
    configure_logging()
    try:
        tenant_id = UUID(tenant) if tenant else PRIMARY_TENANT_ID
    except ValueError as exc:
        _LOG.error("seed-watchpoints: %r is not a valid UUID.", tenant)
        raise typer.Exit(code=2) from exc

    try:
        created = asyncio.run(_seed_with_engine_lifecycle(tenant_id))
    except ConfigurationError as exc:
        _LOG.error("seed-watchpoints: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("seed-watchpoints: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface clean exit code
        _LOG.error("seed-watchpoints: unexpected failure: %s", exc)
        raise typer.Exit(code=1) from exc

    if created:
        _LOG.info("seed-watchpoints: created %d watchpoint(s) for tenant %s", created, tenant_id)
    else:
        _LOG.info(
            "seed-watchpoints: tenant %s already carries every default watchpoint (no-op)",
            tenant_id,
        )
