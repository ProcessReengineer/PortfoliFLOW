# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-tenant live-import refresh core (ADR-0093).

One module owning the per-tenant refresh, callable from both the
out-of-process tick (``cli/market_data_tick.py``) and tests — the same
due-evaluation/refresh core the ADR draws as a swappable seam. The web
"Refresh now" action does **not** call this: it only sets the schedule
row due (ADR-0093 §"On-demand trigger shares the same core"); the tick
then runs this core.

Flow for one tenant (all inside the caller's tenant-scoped session):

1. Resolve the tenant's **system actor** (ADR-0093 §0.1) — a seeded,
   never-authenticating user that satisfies the ``created_by`` audit FK on
   every written row. Its absence is a provisioning fault (the tenant was
   not seeded), raised as :class:`MarketDataSystemActorMissing`.
2. Enumerate the tenant's active investments and keep the **live-eligible**
   ones (the market-linked predicate, :mod:`services.investments.market_linked`
   / ADR-0090 + ADR-0097 §9): a private-markets position, a listed
   instrument without a primary market identifier, and a non-``'unitised'``
   investment are all skipped cleanly, never fetched.
3. Per eligible investment, take its primary market-usable identifier and,
   for every ingestable ``kind`` the capability matrix routes for that
   identifier's scheme (ADR-0091 — the kind list is **driven by the matrix**,
   not hard-coded), fetch a :class:`NormalizedSeries` over a date window and
   write it via :meth:`InvestmentService.ingest_normalized_series` under the
   Excel-precedence guard (ADR-0092), attributed to the system actor. Since
   ADR-0125 §4 the run first *narrows* the candidate kinds — an intraday run
   (one that is not the first of the UTC calendar day) keeps only the price
   kinds — and the matrix then routes within that narrowed set: the matrix
   still decides *availability*, the run decides *necessity*
   (:func:`_kinds_for_run`).
4. Error containment mirrors the Irene beat: one investment's provider
   failure is logged and counted, never aborts the tenant (the tick, in
   turn, isolates one tenant's failure from the rest).

The fetch window runs from the last successful run to today; a tenant that
never ran falls back to a fixed :data:`DEFAULT_LOOKBACK_DAYS` lookback.

``weight_*`` kinds are deliberately **not** fetched: the ingest write path
cannot route them yet (the DTO carries no bucket dimension — a slice-4
finding awaiting a successor ADR), and the matrix no longer advertises
them (slice-5 cleanup), so they would be skipped anyway. Restricting the
kind list here keeps a weight series from ever reaching
``ingest_normalized_series`` (which would raise ``NotImplementedError``).

The three **per-share** kinds (``nav_price``, ``dividend``, ``coupon``) ARE
fetched now (roadmap #038 strand S3 retired the interim S0 blanket guard):
the eligibility gate above admits only ``'unitised'`` investments (ADR-0097
§9), and for those the write path re-routes per-share series correctly —
``nav_price`` into ``instrument_prices`` (materialised into
``investment_navs`` in the same transaction) and per-share ``dividend`` /
``coupon`` scaled by holdings into ``investment_cashflows`` (ADR-0098 §4,
closing findings F1/F6). A ``'reported'`` investment never reaches a fetch
(the gate excludes it); the service-level refusal
(``skipped_unit_mismatch``) is defence in depth in case a per-share series
ever reaches ``ingest_normalized_series`` by another path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import PortfoliFlowError
from core.repositories import (
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
    InvestmentIdentifierRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
)
from services.investments.investment_service import (
    InvestmentService,
)
from services.investments.market_linked import (
    is_market_linked,
    primary_market_identifier,
)
from services.market_data.dto import (
    DateWindow,
    NormalizedIdentifier,
    SeriesKind,
)
from services.market_data.factory import (
    CapabilityMatrix,
    build_adapter,
    get_capability_matrix,
)
from services.market_data.provider import (
    MarketDataError,
    MarketDataProvider,
    UnsupportedCapabilityError,
)

_LOG = logging.getLogger("portfoliflow.market_data")

# ---------------------------------------------------------------------------
# System actor identity (ADR-0093 §0.1)
# ---------------------------------------------------------------------------

#: Deterministic, recognisable identity for the per-tenant market-data
#: system actor. The local-part is clearly synthetic and the domain uses the
#: reserved ``.invalid`` TLD (RFC 2606) so the address is unmistakably
#: never-deliverable. Seeded (``is_active = False``) through the
#: ``seed_tenant_defaults`` choke-point / bootstrap (ADR-0077 parity); the
#: refresh resolves it per tenant to attribute ``created_by`` on live rows.
MARKET_DATA_SYSTEM_ACTOR_EMAIL: str = "market-data-service@service.portfoliflow.invalid"
#: Human-readable display name for the same actor (audit legibility).
MARKET_DATA_SYSTEM_ACTOR_DISPLAY_NAME: str = "Market Data Service"

# ---------------------------------------------------------------------------
# Fetch-window fallback
# ---------------------------------------------------------------------------

#: Fallback lookback (days) when a tenant has never run a successful refresh
#: (``last_run_at IS NULL``): the fetch window is [today − this, today]. A
#: month of history seeds a new tenant's live series without an unbounded
#: back-fill; a tenant that has run before fetches only the delta since.
DEFAULT_LOOKBACK_DAYS: int = 30

#: The series kinds the ingest write path can route today (ADR-0092):
#: NAV/price plus the seven canonical cashflow kinds. ``weight_*`` is
#: excluded — see the module docstring. Iterating this fixed set (rather
#: than every ``SeriesKind``) both drives the fetch off the matrix and
#: guarantees a weight kind never reaches ``ingest_normalized_series``.
_INGESTABLE_KINDS: tuple[SeriesKind, ...] = (
    SeriesKind.NAV_PRICE,
    SeriesKind.DIVIDEND,
    SeriesKind.COUPON,
    SeriesKind.DISTRIBUTION,
    SeriesKind.CAPITAL_CALL,
    SeriesKind.FEE,
    SeriesKind.CARRY,
    SeriesKind.OTHER,
)

#: The kinds fetched on **every** run (ADR-0125 §4). A price is what actually
#: moves between two runs of a sub-hourly cadence, so ``nav_price`` is
#: re-fetched at whatever cadence the tenant configured; every other member of
#: :data:`_INGESTABLE_KINDS` is a *daily* kind, fetched only on the first run
#: of each UTC calendar day (:func:`_kinds_for_run`). Without the split a
#: quarter-hourly cadence would multiply the provider call volume by the
#: number of routed kinds for no new data.
_PRICE_KINDS: tuple[SeriesKind, ...] = (SeriesKind.NAV_PRICE,)

# The two tuples must not drift: a price kind outside the ingestable set would
# be fetched and then have nowhere to land.
assert all(kind in _INGESTABLE_KINDS for kind in _PRICE_KINDS)


class MarketDataSystemActorMissing(PortfoliFlowError):
    """The tenant has no seeded market-data system actor (a provisioning fault).

    Raised by :func:`refresh_tenant_live_data` when
    :data:`MARKET_DATA_SYSTEM_ACTOR_EMAIL` resolves to no user in the active
    tenant — the tenant was created before slice 5 or was not backfilled.
    The remedy is to re-run ``portfoliflow bootstrap`` (primary tenant) or
    ``portfoliflow create-tenant --subdomain <sd>`` (other tenants), which
    re-run the idempotent seed installers (ADR-0077).
    """


@dataclass(frozen=True)
class TenantRefreshReport:
    """Aggregated outcome of one tenant's live refresh.

    Sums the per-series :class:`LiveIngestReport` counts across every
    fetched series, plus tenant-level tallies. Attributes:
        considered: Live-eligible investments the refresh attempted (the
            market-linked predicate passed).
        refreshed: Investments that ingested at least one non-empty series.
        errors: Investments whose provider fetch failed (contained — the
            tenant refresh continued).
        inserted / updated_live / skipped_excel / skipped_manual / noop_live:
            Summed :class:`LiveIngestReport` point counts (ADR-0092).
        skipped_unit_mismatch: Summed per-share points refused because the
            target investment is ``'reported'`` (ADR-0098 §4). Normally zero
            in a refresh — only ``'unitised'`` investments are considered (the
            :func:`is_market_linked` gate) — but non-zero if an eligibility /
            routing skew ever lets a per-share series reach a reported book;
            the defence-in-depth refusal then counts here.
        skipped_currency_mismatch: Summed per-share points refused because the
            series currency differed from the investment currency (ADR-0097
            §5) — rejected, never converted.
        skipped_zero_holdings: Summed per-share flow points on a date with
            zero derived holdings — nothing to scale by (ADR-0098 §4).
    """

    considered: int = 0
    refreshed: int = 0
    errors: int = 0
    inserted: int = 0
    updated_live: int = 0
    skipped_excel: int = 0
    skipped_manual: int = 0
    noop_live: int = 0
    skipped_unit_mismatch: int = 0
    skipped_currency_mismatch: int = 0
    skipped_zero_holdings: int = 0


def _fetch_window(now: datetime, last_run_at: datetime | None) -> DateWindow:
    """Return the [start, end] fetch window for a tenant.

    ``end`` is today (``now``'s date). ``start`` is the last successful
    run's date, or ``today − DEFAULT_LOOKBACK_DAYS`` when the tenant has
    never run. A ``last_run_at`` in the future relative to ``now`` (clock
    skew) is clamped to ``end`` so the window stays valid (``start <= end``).
    """
    end: date = now.date()
    if last_run_at is not None:
        start = last_run_at.date()
    else:
        start = end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if start > end:
        start = end
    return DateWindow(start=start, end=end)


def _kinds_for_run(now: datetime, last_run_at: datetime | None) -> tuple[SeriesKind, ...]:
    """Return the series kinds this run should fetch (ADR-0125 §4).

    Why: a sub-hourly cadence re-fetches a price usefully and a dividend
    pointlessly, so the run narrows its own candidate kinds rather than asking
    the provider for everything every quarter of an hour. The full
    :data:`_INGESTABLE_KINDS` set is fetched on the first run of each UTC
    calendar day — and for a tenant that has never run; every later run of the
    same day fetches :data:`_PRICE_KINDS` only.

    The day boundary is **UTC by decision**: ``last_run_at`` is persisted in
    UTC and this core knows no tenant timezone, so both instants are converted
    to UTC before their dates are compared and the caller's zone never decides
    the branch. For Europe/Berlin that means the daily kinds are caught up on
    the first run after 01:00 (winter) / 02:00 (summer) local rather than at
    local midnight, which the ADR accepts.

    The failure property follows from the tick: a failed run does not advance
    ``last_run_at`` (``mark_run_done`` runs only on success), so the next
    attempt still sees the older timestamp and asks for every kind again — a
    transient provider error cannot silently cost a tenant its daily kinds for
    the day.

    Args:
        now: The current instant (timezone-aware).
        last_run_at: The tenant's last successful refresh, or ``None``.

    Returns:
        :data:`_INGESTABLE_KINDS` on the first run of the UTC day (and for a
        never-run tenant), else :data:`_PRICE_KINDS`.
    """
    if last_run_at is None:
        return _INGESTABLE_KINDS
    if last_run_at.astimezone(timezone.utc).date() < now.astimezone(timezone.utc).date():
        return _INGESTABLE_KINDS
    return _PRICE_KINDS


def _forced_capability_serves(
    matrix: CapabilityMatrix, provider_name: str, scheme: str, kind: SeriesKind
) -> bool:
    """Return whether ``provider_name`` declares ``(scheme, kind)`` in the matrix.

    Used only on the forced-provider path (``--provider``): the request must
    still respect the matrix's coverage declaration, so a forced provider is
    asked only for kinds it actually serves for the identifier's scheme.
    """
    for capability in matrix.providers:
        if capability.name == provider_name:
            return capability.serves(scheme, kind)
    return False


async def refresh_tenant_live_data(
    session: AsyncSession,
    *,
    now: datetime,
    last_run_at: datetime | None,
    forced_provider: str | None = None,
    matrix: CapabilityMatrix | None = None,
) -> TenantRefreshReport:
    """Refresh every live-eligible investment in the active tenant.

    Runs entirely inside the caller's tenant-scoped ``session`` (RLS
    enforced): the tick opens ``tenant_context`` and claims the advisory
    lock before calling this. Returns an aggregated
    :class:`TenantRefreshReport`; it does not advance the schedule (the tick
    owns that, on success).

    Which kinds are fetched depends on the run (ADR-0125 §4,
    :func:`_kinds_for_run`): the first run of each UTC calendar day — and a
    tenant that has never run — asks for every matrix-routed member of
    :data:`_INGESTABLE_KINDS`, while every later run of the same day asks for
    :data:`_PRICE_KINDS` only. An intraday run therefore re-fetches today's
    ``nav_price`` bar and nothing else: the last traded price while the
    session is open, and a repeated value (counted ``noop_live``) once it has
    closed. The report gains no field for this — the existing counters stay
    correct for whichever kinds ran.

    Args:
        session: A tenant-scoped :class:`AsyncSession` (RLS active).
        now: The current instant (timezone-aware UTC), the window's upper
            bound and the ``created_at`` reference for logging.
        last_run_at: The tenant's last successful refresh, or ``None`` — the
            window's lower bound (falling back to a fixed lookback) and the
            UTC-day marker that selects the kind set.
        forced_provider: When set (threaded from ``--provider``), route every
            fetch to this named provider instead of the matrix's priority
            order, for the kinds it declares for the scheme. Production
            timers pass ``None``.
        matrix: The capability matrix to route with; defaults to the cached
            shipped matrix.

    Returns:
        The aggregated :class:`TenantRefreshReport`.

    Raises:
        MarketDataSystemActorMissing: If the tenant has no seeded system
            actor to attribute writes to.
    """
    resolved_matrix = matrix or get_capability_matrix()

    actor = await UserRepository(session).get_by_email(MARKET_DATA_SYSTEM_ACTOR_EMAIL)
    if actor is None:
        raise MarketDataSystemActorMissing(
            "market-data refresh: no system actor "
            f"{MARKET_DATA_SYSTEM_ACTOR_EMAIL!r} in this tenant; re-run the "
            "seed installers (portfoliflow bootstrap / create-tenant)."
        )

    service = InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )
    identifiers_repo = InvestmentIdentifierRepository(session)
    window = _fetch_window(now, last_run_at)
    kinds = _kinds_for_run(now, last_run_at)

    # Adapters are stateless-per-request but constructing the synthetic one
    # re-reads its fixture, so cache by name within a tenant refresh.
    adapters: dict[str, MarketDataProvider] = {}

    considered = refreshed = errors = 0
    inserted = updated_live = skipped_excel = skipped_manual = noop_live = 0
    skipped_unit_mismatch = skipped_currency_mismatch = skipped_zero_holdings = 0

    # Only active investments are refreshed: an inactive row is a closed /
    # sold position, and position changes are out of scope this slice.
    for investment in await service.list_active_investments():
        identifiers = await identifiers_repo.list_for_investment(investment.id)
        if not is_market_linked(
            investment.investment_type,
            identifiers,
            investment.valuation_mode,
        ):
            # Ineligible: a private-markets type, no primary market
            # identifier, or (ADR-0097 §9) a non-``'unitised'`` valuation
            # mode. A ``'reported'`` listed instrument is skipped cleanly
            # before any fetch — its per-share series would have no correct
            # landing spot (the service-level refusal is defence in depth).
            continue
        primary = primary_market_identifier(identifiers)
        # is_market_linked already guaranteed a primary market identifier.
        assert primary is not None
        considered += 1

        ident = NormalizedIdentifier(scheme=primary.scheme, value=primary.value)
        investment_had_data = False
        try:
            for kind in kinds:
                provider_name = _route(resolved_matrix, primary.scheme, kind, forced_provider)
                if provider_name is None:
                    continue  # matrix routes nothing for (scheme, kind)
                adapter = adapters.get(provider_name)
                if adapter is None:
                    adapter = build_adapter(provider_name)
                    adapters[provider_name] = adapter

                series = await adapter.fetch_series(ident, kind, window)
                if not series.points:
                    continue  # a real "no data in window" gap
                report = await service.ingest_normalized_series(
                    series, investment_id=investment.id, user_id=actor.id
                )
                inserted += report.inserted
                updated_live += report.updated_live
                skipped_excel += report.skipped_excel
                skipped_manual += report.skipped_manual
                noop_live += report.noop_live
                skipped_unit_mismatch += report.skipped_unit_mismatch
                skipped_currency_mismatch += report.skipped_currency_mismatch
                skipped_zero_holdings += report.skipped_zero_holdings
                investment_had_data = True
        except MarketDataError as exc:
            # Contain one investment's provider failure (unresolvable
            # identifier, transport error, ...) — log, count, continue.
            errors += 1
            _LOG.warning(
                "market-data refresh: investment %s (%s:%s) provider error: %s",
                investment.id,
                primary.scheme,
                primary.value,
                exc,
            )
            continue

        if investment_had_data:
            refreshed += 1

    report = TenantRefreshReport(
        considered=considered,
        refreshed=refreshed,
        errors=errors,
        inserted=inserted,
        updated_live=updated_live,
        skipped_excel=skipped_excel,
        skipped_manual=skipped_manual,
        noop_live=noop_live,
        skipped_unit_mismatch=skipped_unit_mismatch,
        skipped_currency_mismatch=skipped_currency_mismatch,
        skipped_zero_holdings=skipped_zero_holdings,
    )
    _LOG.info(
        "market-data refresh: considered=%d refreshed=%d errors=%d "
        "window=[%s..%s] kinds=%s inserted=%d updated_live=%d skipped_excel=%d "
        "skipped_manual=%d noop_live=%d skipped_unit_mismatch=%d "
        "skipped_currency_mismatch=%d skipped_zero_holdings=%d%s",
        report.considered,
        report.refreshed,
        report.errors,
        window.start.isoformat(),
        window.end.isoformat(),
        "all" if kinds == _INGESTABLE_KINDS else "price",
        report.inserted,
        report.updated_live,
        report.skipped_excel,
        report.skipped_manual,
        report.noop_live,
        report.skipped_unit_mismatch,
        report.skipped_currency_mismatch,
        report.skipped_zero_holdings,
        f" provider={forced_provider!r}" if forced_provider else "",
    )
    return report


def _route(
    matrix: CapabilityMatrix,
    scheme: str,
    kind: SeriesKind,
    forced_provider: str | None,
) -> str | None:
    """Return the provider name serving ``(scheme, kind)``, or ``None``.

    On the default path, the matrix's priority order picks the winner (an
    unroutable request returns ``None`` — the declared non-availability of
    ADR-0091 property 2). On the forced-provider path, the named provider is
    used only if the matrix says it serves ``(scheme, kind)``, so a forced
    run still respects the coverage declaration.
    """
    if forced_provider is not None:
        if _forced_capability_serves(matrix, forced_provider, scheme, kind):
            return forced_provider
        return None
    try:
        return matrix.resolve(scheme, kind).name
    except UnsupportedCapabilityError:
        return None


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "MARKET_DATA_SYSTEM_ACTOR_DISPLAY_NAME",
    "MARKET_DATA_SYSTEM_ACTOR_EMAIL",
    "MarketDataSystemActorMissing",
    "TenantRefreshReport",
    "refresh_tenant_live_data",
]
