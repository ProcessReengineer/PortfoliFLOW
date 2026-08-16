# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow market-data-tick`` — the tenant-blind live-import tick.

Per ADR-0093 the market-data trigger reuses Irene's topology 1:1
(ADR-0086): a dumb, fixed-interval trigger asks the domain "who is due
now?" and refreshes each due tenant's live-eligible investments. This
command is the **external** tick source, driven by a systemd timer (see
``docs/deploy/market-data-tick.timer``); ADR-0117 makes the built-in
in-process scheduler the default and demotes these units to a documented
opt-out. Because all the due logic lives in the query + advisory lock,
either source drives the same tick without touching domain logic.

Since ADR-0117 §2 the per-tick orchestration itself — the due read, the
market_data-domain advisory-lock claim (disjoint from Irene's key), the
refresh, the schedule advance, per-tenant failure isolation and every log
line — lives in :mod:`services.scheduler.tick_runner`, shared byte-for-byte
with the in-process scheduler. **Read that module's docstring for the
flow.**

Unlike ``irene-tick`` this command has **no AI dependency**: it reaches no
LLM and no OpenRouter, so it carries none of Irene's credential preamble.

Test-seam flags (ADR-0093 §0.4) — **neither persists schedule state**, so a
test run never perturbs production cadence, and production timers pass
neither:

- ``--tenant <id-or-subdomain>``: restrict the tick to one tenant and
  bypass the due gate (still honouring the advisory lock).
- ``--provider <name>``: force the factory to a named provider from the
  capability matrix (the documented way to point a test run at
  ``synthetic``) instead of the matrix's priority routing.

Flag *parsing* is this wrapper's concern; the values are plumbed straight
into the runner. What else remains here is ``configure_logging()``, the
superuser engine's lifecycle, and the exit-code mapping. Following the
existing CLI pattern (``cli/irene_tick.py``, ``cli/inspect_tenant.py``): a
thin Typer wrapper that calls ``configure_logging()`` then
``asyncio.run(_run())``, mapping ``ConfigurationError`` → exit 2 and
``PortfoliFlowError`` → exit 3.
"""

from __future__ import annotations

import asyncio
import logging

import typer

from cli._db import superuser_engine
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging
from services.scheduler.tick_runner import run_market_data_tick

_LOG = logging.getLogger("portfoliflow.cli")


async def _run(tenant_ref: str | None, provider: str | None) -> None:
    """Run one tick on a per-run superuser engine, disposed on the way out."""
    engine = superuser_engine()
    try:
        await run_market_data_tick(engine, tenant_ref=tenant_ref, provider=provider)
    finally:
        await engine.dispose()


def market_data_tick_command(
    tenant: str | None = typer.Option(
        None,
        "--tenant",
        help=(
            "Restrict the tick to one tenant (UUID or subdomain), bypassing "
            "the due gate. Test seam — does not persist schedule state."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "Force the factory to a named provider from the capability "
            "matrix (e.g. 'synthetic'). Test seam — does not persist "
            "schedule state."
        ),
    ),
) -> None:
    """Refresh every tenant whose market-data schedule is due (ADR-0093).

    Tenant-blind: the due tenants are discovered from
    ``market_data_schedule`` at run time. Exits 0 on success (including
    "nothing due"); exit 2 on a configuration error (e.g.
    ``DATABASE_URL_SUPERUSER`` unset), exit 3 on another PortfoliFLOW error.
    A single tenant's refresh failure does not fail the tick. This command
    has no AI/LLM dependency.
    """
    configure_logging()
    try:
        asyncio.run(_run(tenant, provider))
    except ConfigurationError as exc:
        _LOG.error("market-data-tick: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("market-data-tick: %s", exc.message)
        raise typer.Exit(code=3) from exc
