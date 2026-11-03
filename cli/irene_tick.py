# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow irene-tick`` — the tenant-blind Irene heartbeat tick.

It also wakes waiting cases: the shared runner's case wake-up step
(:func:`services.scheduler.tick_runner.run_case_wake_tick`, ADR-0138 §4)
runs first on every invocation, so the external tick source drives the
same three steps as the in-process scheduler (ADR-0117 §2, D-CS-B-24).

Per ADR-0086 the *tick source* is a thin, swappable adapter: a dumb,
fixed-interval trigger that asks the domain "who is due now?" and beats
each due tenant. This command is the **external** tick source — an
out-of-process entry point driven by a systemd timer (see
``docs/deploy/irene-tick.timer``); ADR-0117 makes the built-in in-process
scheduler the default and demotes these units to a documented opt-out.
Because all the due logic lives in the query + advisory lock, either source
drives the same tick without touching domain logic.

Since ADR-0117 §2 the per-tick orchestration itself — the due read, the
advisory-lock claim, per-tenant credential and model resolution, the beat,
the schedule advance, per-tenant failure isolation and every log line —
lives in :mod:`services.scheduler.tick_runner`, shared byte-for-byte with
the in-process scheduler. **Read that module's docstring for the flow and
the model chain.**

What remains here is the wrapper: the Typer command, ``configure_logging()``,
the superuser engine's lifecycle, and the exit-code mapping. Following the
existing CLI pattern (``cli/bootstrap.py``, ``cli/inspect_tenant.py``): a
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
from services.scheduler.tick_runner import (
    irene_credentials_reachable,
    run_case_wake_tick,
    run_irene_tick,
)
from web.settings import get_web_settings

_LOG = logging.getLogger("portfoliflow.cli")


async def _run() -> None:
    """Run one tick on a per-run superuser engine, disposed on the way out."""
    settings = get_web_settings()
    if not irene_credentials_reachable(settings):
        # The gate is evaluated *before* the engine is built (and logs its
        # own reason). A deployment where no scope can resolve a credential
        # has always been a no-op that exits 0 without needing
        # DATABASE_URL_SUPERUSER, and it stays one (D-CS-B-27): the case
        # wake-up needs no LLM, so with a superuser URL this branch wakes
        # cases and skips the beat; without one it logs a line and exits 0
        # instead of turning the no-op into a configuration error. The
        # runner re-evaluates the same gate, so a host that cannot pre-check
        # loses nothing but this ordering.
        try:
            engine = superuser_engine()
        except ConfigurationError:
            _LOG.info("irene-tick: no superuser URL — skipping the case wake-up.")
            return
        try:
            await run_case_wake_tick(engine)
        finally:
            await engine.dispose()
        return

    engine = superuser_engine()
    try:
        await run_case_wake_tick(engine)
        await run_irene_tick(engine, settings=settings)
    finally:
        await engine.dispose()


def irene_tick_command() -> None:
    """Beat every tenant whose Irene schedule is due (ADR-0086); also wakes waiting cases.

    The case wake-up (ADR-0138 §4) runs first and independent of the LLM
    credential gate: a box where no scope can resolve a credential still
    wakes cases when ``DATABASE_URL_SUPERUSER`` is set, and still exits 0
    without an engine when it is not (D-CS-B-27).

    Tenant-blind: the due tenants are discovered from ``irene_schedule``
    at run time. Exits 0 on success (including "nothing due", "no scope can
    resolve a credential", and "every due tenant lacks one"); exit 2 on a
    configuration error (e.g. ``DATABASE_URL_SUPERUSER`` unset), exit 3 on
    another PortfoliFLOW error. Neither a single tenant's beat failure nor
    its missing credential fails the tick.

    Model: resolved per tenant (ADR-0112 §4b) along
    ``tenant irene_model → tenant model → env IRENE_MODEL → env
    SHIRLEY_MODEL → default``, so Irene is pinned neither to Shirley's
    model nor to one process-wide choice.
    """
    configure_logging()
    try:
        asyncio.run(_run())
    except ConfigurationError as exc:
        _LOG.error("irene-tick: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("irene-tick: %s", exc.message)
        raise typer.Exit(code=3) from exc
