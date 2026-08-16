# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Idempotent default-watchpoint seeding (ADR-0116 §8).

A freshly provisioned tenant should observe *something* without anyone
opening the Calibration editor first, so tenant provisioning installs a
small, deliberately conservative default set:

* one ``freshness`` singleton at ``max_age_days = 120`` — the Watch Desk
  watching the ground it stands on;
* one ``liquidity`` singleton at ``horizon_months = 12``,
  ``min_coverage_ratio = 1.2``;
* one ``fx`` watchpoint per currency pair **present in the book at seed
  time** (``move_pct = 3.0``, ``window_days = 5``).

``price`` watchpoints are **not** seeded for a new tenant: one per
instrument is a lot of subjects to hand someone who did not ask for
them, and per-instrument noise is the fastest way to teach an operator
to ignore the monitor. The demo tenant is the exception (ADR-0116 §8) —
it gets a ``price`` watchpoint on each of its market-identified
instruments (``drop_pct = 5.0``, ``window_days = 5``) so the family is
visibly live in the release screenshots.

No ``floor_calibration`` row is seeded, ever: an absent row means code
defaults (ADR-0116 §7), and materialising a copy of the defaults would
turn every future default change into a per-tenant migration.

Seeding runs *after* provisioning, and the book arrives later
------------------------------------------------------------------
At ``portfoliflow bootstrap`` time a tenant has no investments at all,
so the two singletons are everything that can be installed; the fx pairs
and (for the demo tenant) the price watchpoints only become derivable
once a workbook has been imported. This installer is therefore written
to be **re-run**: every step is idempotent on the subject key, so a
second run after the import adds exactly the newly derivable rows and
changes nothing else. ``portfoliflow seed-watchpoints`` is the operator
entry point for that second run.

Idempotency is keyed on what each family is *about* among the tenant's
live watchpoints — a singleton family by its family, a currency pair by
its pair, an instrument by its instrument — and never on "have I run
before". Two consequences worth stating: an operator who merely revised a
seeded watchpoint's thresholds, or renamed it, keeps their version,
because the identity is still live; one who *retired* it on purpose gets
it back on the next run, which is the honest behaviour for something
called a default installer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import TenantRepository, tenant_context
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.watchpoint_repository import WatchpointRepository
from core.tenant_constants import PRIMARY_TENANT_ID
from services.analytics.signal_watch import (
    FRESHNESS_WILDCARD_SUBJECT_KEY,
    LIQUIDITY_SUBJECT_KEY,
)

_LOG = logging.getLogger("portfoliflow.services.watch_desk")

__all__ = [
    "FRESHNESS_SUBJECT_KEY",
    "LIQUIDITY_SUBJECT_KEY",
    "default_display_name",
    "fx_subject_key",
    "install_default_watchpoints",
    "install_default_watchpoints_for_tenant",
    "price_subject_key",
    "seeds_price_watchpoints",
]

# --- v1 default parameter values (ADR-0116 §8), refinable per tenant. ------

_FRESHNESS_MAX_AGE_DAYS: int = 120
_LIQUIDITY_HORIZON_MONTHS: int = 12
_LIQUIDITY_MIN_COVERAGE_RATIO: Decimal = Decimal("1.2")
_FX_MOVE_PCT: Decimal = Decimal("3.0")
_FX_WINDOW_DAYS: int = 5
_PRICE_DROP_PCT: Decimal = Decimal("5.0")
_PRICE_WINDOW_DAYS: int = 5

#: The ``freshness`` singleton's subject key, under the name this installer
#: has always exported it as. The string itself belongs to the family's
#: contract (:mod:`services.analytics.signal_watch`) since P5, because the
#: producer's sensitivity lookups fall back to it: what the installer writes
#: and what the resolution reads must be one constant, not two that agree.
FRESHNESS_SUBJECT_KEY: str = FRESHNESS_WILDCARD_SUBJECT_KEY


def fx_subject_key(pair: str) -> str:
    """Return the ``fx`` subject key for a ``BASE/QUOTE`` pair."""
    return f"fx:{pair}"


def price_subject_key(instrument_id: UUID) -> str:
    """Return the ``price`` subject key for one instrument."""
    return f"price:{instrument_id}"


#: The label a watchpoint carries when nobody named it — the seeded rows and
#: the ones the operator adds from the Watch Desk (ADR-0116 §6) alike. Stated
#: once so a seeded ``fx`` row and a hand-added one for the same pair are not
#: two different-looking things in the same list.
_DEFAULT_DISPLAY_NAME: dict[str, str] = {
    "freshness": "NAV freshness (all investments)",
    "liquidity": "Cash coverage of projected calls",
}


def default_display_name(family: str, *, subject: str = "") -> str:
    """Return the default operator-readable label for a new watchpoint.

    Args:
        family: The watchpoint's family.
        subject: What the watchpoint is about, for the two families that
            name one — the instrument's name (``price``) or the pair
            (``fx``). Ignored by the singleton families, whose subject is
            "everything" and needs no naming.

    Returns:
        The label. Falls back to the family name for a family with no
        stated default, which cannot happen through either write path but
        keeps the function total.
    """
    if family == "price":
        return f"Price decline — {subject}"
    if family == "fx":
        return f"FX move {subject}"
    return _DEFAULT_DISPLAY_NAME.get(family, family)


def seeds_price_watchpoints(tenant_id: UUID) -> bool:
    """Return whether this tenant is the one seeded with ``price`` rows.

    The demo tenant (``minathena-capital``, i.e. the Primary Tenant) and
    nobody else, per ADR-0116 §8. Stated once here so both provisioning
    paths — ``portfoliflow bootstrap`` and
    :func:`services.super_admin.operations.seed_tenant_defaults` — cannot
    drift on the rule.

    Args:
        tenant_id: The tenant being seeded.

    Returns:
        ``True`` for the Primary Tenant, ``False`` otherwise.
    """
    return tenant_id == PRIMARY_TENANT_ID


async def install_default_watchpoints_for_tenant(
    engine: AsyncEngine, tenant_id: UUID, actor_user_id: UUID
) -> int:
    """Install the default watchpoints for one tenant, in its own transaction.

    The shared entry point behind all three callers — the ``bootstrap``
    seed pipeline, :func:`services.super_admin.operations.seed_tenant_defaults`
    for ``create-tenant``, and ``portfoliflow seed-watchpoints`` — so no
    two of them can drift on what a provisioned tenant observes. The audit
    trigger on ``watchpoints`` attributes the seeded rows to
    ``actor_user_id``.

    Args:
        engine: An engine capable of opening tenant-scoped sessions.
        tenant_id: The tenant to seed.
        actor_user_id: The user attributed for the seeded rows.

    Returns:
        The number of watchpoints created — zero when nothing was new.
    """
    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        functional_currency = await TenantRepository(session).get_current_functional_currency()
        if not functional_currency:
            # The column is NOT NULL, so this means "no tenant row visible",
            # which is worth saying out loud rather than silently seeding a
            # pairless set. The singletons still install: they need no book.
            _LOG.warning(
                "watchpoint seed: no functional currency readable for tenant %s; "
                "seeding the singletons only",
                tenant_id,
            )
        return await install_default_watchpoints(
            WatchpointRepository(session),
            InvestmentRepository(session),
            InvestmentIdentifierRepository(session),
            functional_currency=functional_currency or "",
            now=datetime.now(timezone.utc),
            seed_price_watchpoints=seeds_price_watchpoints(tenant_id),
        )


async def install_default_watchpoints(
    watchpoints: WatchpointRepository,
    investments: InvestmentRepository,
    identifiers: InvestmentIdentifierRepository,
    *,
    functional_currency: str,
    now: datetime,
    seed_price_watchpoints: bool = False,
) -> int:
    """Install the tenant's default watchpoints, skipping what already exists.

    Args:
        watchpoints: Watchpoint repository bound to a tenant-scoped
            session.
        investments: Investment repository on the same session — the
            active book is read to derive the currency pairs and, for the
            demo tenant, the priced instruments.
        identifiers: Identifier repository on the same session. An
            investment with at least one market identifier is one the
            platform can price; a private-markets fund carries none, which
            is precisely what excludes it from ``price`` seeding.
        functional_currency: The tenant's reporting currency — the QUOTE
            side of every seeded pair.
        now: The instant the seeded versions take effect (timezone-aware).
        seed_price_watchpoints: Install ``price`` watchpoints for every
            market-identified instrument. ``True`` for the demo tenant
            only (ADR-0116 §8).

    Returns:
        The number of watchpoints actually created — zero on a re-run
        that finds nothing new, which is what the idempotency test pins.
    """
    # Every live identity, not just the ones already in force: a version
    # someone scheduled for next week is not something to seed a duplicate
    # of (see WatchpointRepository.list_live_identities).
    live = await watchpoints.list_live_identities()
    # Matched on what each family is *about*, not on the seeded subject key:
    # a singleton family by its family (there can only be one, whatever it
    # was named), a pair by its pair, an instrument by its instrument. An
    # operator who renamed a seeded watchpoint keeps one, not two.
    live_families = {row.family for row in live}
    live_pairs = {row.currency_pair for row in live if row.family == "fx"}
    live_instruments = {row.instrument_id for row in live if row.family == "price"}
    created = 0

    if "freshness" not in live_families:
        await watchpoints.create(
            family="freshness",
            subject_key=FRESHNESS_SUBJECT_KEY,
            display_name=default_display_name("freshness"),
            effective_from=now,
            max_age_days=_FRESHNESS_MAX_AGE_DAYS,
            notes="Seeded default (ADR-0116 §8).",
        )
        created += 1
        _LOG.info("watchpoint seed: created the freshness singleton")

    if "liquidity" not in live_families:
        await watchpoints.create(
            family="liquidity",
            subject_key=LIQUIDITY_SUBJECT_KEY,
            display_name=default_display_name("liquidity"),
            effective_from=now,
            horizon_months=_LIQUIDITY_HORIZON_MONTHS,
            min_coverage_ratio=_LIQUIDITY_MIN_COVERAGE_RATIO,
            notes="Seeded default (ADR-0116 §8).",
        )
        created += 1
        _LOG.info("watchpoint seed: created the liquidity singleton")

    # ``list_active`` rather than ``list_all``: "the book" means the active
    # universe everywhere else that reads it — AUM, coverage, statistics,
    # the chart services — and a deactivated position is not something to
    # start watching the price or the currency of.
    book = await investments.list_active()

    for pair in _currency_pairs_in_book(book, functional_currency):
        if pair in live_pairs:
            continue
        await watchpoints.create(
            family="fx",
            subject_key=fx_subject_key(pair),
            display_name=default_display_name("fx", subject=pair),
            effective_from=now,
            currency_pair=pair,
            move_pct=_FX_MOVE_PCT,
            window_days=_FX_WINDOW_DAYS,
            notes="Seeded from the currency pairs present in the book (ADR-0116 §8).",
        )
        created += 1
        _LOG.info("watchpoint seed: created the fx watchpoint for %s", pair)

    if seed_price_watchpoints:
        for investment in book:
            if investment.id in live_instruments:
                continue
            if not await identifiers.list_for_investment(investment.id):
                continue
            await watchpoints.create(
                family="price",
                subject_key=price_subject_key(investment.id),
                display_name=default_display_name("price", subject=investment.name),
                effective_from=now,
                instrument_id=investment.id,
                drop_pct=_PRICE_DROP_PCT,
                window_days=_PRICE_WINDOW_DAYS,
                notes="Seeded demo-tenant default (ADR-0116 §8).",
            )
            created += 1
            _LOG.info("watchpoint seed: created the price watchpoint for %r", investment.name)

    _LOG.info("watchpoint seed: %d watchpoint(s) created", created)
    return created


def _currency_pairs_in_book(book: list[InvestmentDTO], functional_currency: str) -> list[str]:
    """Derive the ``BASE/QUOTE`` pairs the book actually needs.

    One pair per distinct position currency that is not the functional
    currency, quoted against the functional currency — the direction the
    conversion boundary uses (ADR-0099 §2/§4). A single-currency book
    yields none, and correctly so: there is nothing to watch. Without a
    functional currency there is no QUOTE side, so no pair is derivable
    either — better none than a malformed one.
    """
    quote = functional_currency.strip().upper()
    if not quote:
        return []
    bases = sorted(
        {
            investment.currency.strip().upper()
            for investment in book
            if investment.currency and investment.currency.strip().upper() != quote
        }
    )
    return [f"{base}/{quote}" for base in bases]
