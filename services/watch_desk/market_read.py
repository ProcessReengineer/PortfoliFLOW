# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Watch Desk market read — what the priceable book did this week (D-WD-20).

ADR-0133 §1.2 hands the briefing writer typed market inputs beside the
family status (:mod:`services.watch_desk.family_status`): how each priceable
instrument moved, how each book currency moved against the dataset's
reference currency, and how each asset class moved, value-weighted. This
module computes all three **finished**. Irene states these figures; she
never derives an aggregate from them.

Three reads, each on a seam that already exists
-----------------------------------------------
* **Instruments.** The universe is every active investment with at least
  one market identifier — the ADR-0116 §8 priceability precedent the
  Watch Desk's ``price`` picker applies, **without** that picker's
  already-watched exclusion: this is a read of the whole priceable book,
  not an offer of a new watchpoint identity. One
  :meth:`~core.repositories.instrument_price_repository.InstrumentPriceRepository.list_by_investments`
  read covers them all; its soft lower bound supplies the carry-forward
  reference, exactly as ``price`` observation relies on it.
* **FX legs.** Every distinct book currency, one unit of it (BASE) priced
  in the dataset's reference currency (QUOTE) — the orientation
  ``fx`` observation fixes for a ``BASE/QUOTE`` watchpoint — derived through
  :class:`~services.fx.conversion.FxConverter` from one rate frame. Dates
  before a leg's first stored rate are skipped, never defaulted 1:1
  (ADR-0099). The reference currency is the identity and has no record.
* **Asset classes.** Σ(weight·move)/Σ(weight) over the instruments of a
  class that **have** a move — numerator and denominator over one set, so
  an instrument without a move is absent from both rather than counted as
  0 %. The weight is the instrument's functional-currency value from the
  one AUM definition (:func:`services.investments.aum.compute_aum`,
  ADR-0103 §2): the latest actual NAV carried forward to ``as_of`` and
  converted per investment at the ADR-0099 §4 seam.

The window is fixed at :data:`MARKET_WINDOW_DAYS` days (D-WD-20). A move is
the last value at or before ``as_of`` against the last value at or before
``as_of − 5 days``, in percent; a figure without both points is **absent**,
never zero.

Reads only, and outside the observation layer
---------------------------------------------
Nothing here writes, and nothing here classifies: no watchpoint, no
threshold, no watch-state. The four signal families' fetch stays in
:mod:`services.watch_desk.signal_observation` alone — the structural guard
in ``tests/regression/test_watch_desk_single_resolution.py`` names this
module as the one approved batched read beside it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import MissingFxRateError
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.signal_watch import (
    DatedValue,
    NoObservation,
    percentage_move,
    quantize_pp,
    resolve_window,
    window_start,
)
from services.fx.conversion import FxConverter
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)
from services.investments.aum import NavSeries, compute_aum, load_nav_series

_LOG = logging.getLogger(__name__)

#: The fixed observation window of every figure in the read (D-WD-20).
MARKET_WINDOW_DAYS: int = 5

#: The unit every move is stated in: a signed percentage, four places.
MOVE_UNIT: str = "pct"

#: One unit of BASE converted into QUOTE *is* the leg's rate.
_ONE: Decimal = Decimal("1")

_ZERO: Decimal = Decimal("0")


@dataclass(frozen=True)
class InstrumentMove:
    """One priceable instrument's move over the window.

    Attributes:
        id: Stable record identity, ``instrument:{investment_id}``.
        label: The investment's name.
        value: The signed move in percent, quantised to four places —
            positive when the price rose. Measured within the instrument's
            own price currency, so it is FX-free by construction.
        unit: Always :data:`MOVE_UNIT`.
        as_of: The read's evaluation date — the window's end.
        window_start: ``as_of − window_days``; the reference is the last
            price at or before it.
        window_days: Always :data:`MARKET_WINDOW_DAYS`.
        investment_id: The instrument.
        asset_class_id: The instrument's asset class.
        currency: The instrument's position currency.
        reference_date: The date of the reference price.
        reference_price: The price at the window's start (carried forward).
        latest_date: The date of the latest price.
        latest_price: The price at the window's end (carried forward).
    """

    id: str
    label: str
    value: Decimal
    unit: str
    as_of: date
    window_start: date
    window_days: int
    investment_id: UUID
    asset_class_id: UUID
    currency: str
    reference_date: date
    reference_price: Decimal
    latest_date: date
    latest_price: Decimal


@dataclass(frozen=True)
class FxMove:
    """One book currency's move against the dataset's reference currency.

    Attributes:
        id: Stable record identity, ``fx:{BASE}/{QUOTE}``.
        label: The pair, ``BASE/QUOTE``.
        value: The signed move of one unit of BASE priced in QUOTE, in
            percent, quantised to four places.
        unit: Always :data:`MOVE_UNIT`.
        as_of: The read's evaluation date — the window's end.
        window_start: ``as_of − window_days``.
        window_days: Always :data:`MARKET_WINDOW_DAYS`.
        base_currency: The book currency.
        quote_currency: The dataset's reference currency.
        reference_date: The date of the reference rate.
        reference_rate: The pair rate at the window's start (carried forward).
        latest_date: The date of the latest rate.
        latest_rate: The pair rate at the window's end (carried forward).
    """

    id: str
    label: str
    value: Decimal
    unit: str
    as_of: date
    window_start: date
    window_days: int
    base_currency: str
    quote_currency: str
    reference_date: date
    reference_rate: Decimal
    latest_date: date
    latest_rate: Decimal


@dataclass(frozen=True)
class AssetClassMove:
    """One asset class's value-weighted move over the window.

    Attributes:
        id: Stable record identity, ``asset_class:{asset_class_id}``.
        label: The asset class's display name.
        value: Σ(weight·move)/Σ(weight) over the class's instruments that
            have a move and a positive value, in percent, quantised to four
            places.
        unit: Always :data:`MOVE_UNIT`.
        as_of: The read's evaluation date — the window's end.
        window_start: ``as_of − window_days``.
        window_days: Always :data:`MARKET_WINDOW_DAYS`.
        asset_class_id: The asset class.
        instrument_count: How many instruments the aggregate is taken over.
        weight_total: Σ(weight) — their value in the functional currency.
        functional_currency: The currency ``weight_total`` is stated in.
    """

    id: str
    label: str
    value: Decimal
    unit: str
    as_of: date
    window_start: date
    window_days: int
    asset_class_id: UUID
    instrument_count: int
    weight_total: Decimal
    functional_currency: str


@dataclass(frozen=True)
class MarketRead:
    """The whole market read at one date — every figure finished.

    Attributes:
        as_of: The evaluation date — the window's end.
        window_start: ``as_of − window_days``.
        window_days: Always :data:`MARKET_WINDOW_DAYS`.
        instruments: The instruments that have a move, in book order.
        fx: The book currencies that have a move, by currency code.
        asset_classes: The asset classes that have a value-weighted move,
            by label.
    """

    as_of: date
    window_start: date
    window_days: int
    instruments: tuple[InstrumentMove, ...]
    fx: tuple[FxMove, ...]
    asset_classes: tuple[AssetClassMove, ...]


async def read_market(session: AsyncSession, *, as_of: date) -> MarketRead:
    """Read the priceable book's moves over the fixed window ending ``as_of``.

    Read-only. Runs inside the caller's tenant-scoped session: one
    identifier read per active investment (the priceability precedent),
    one batched price read, one rate frame, one NAV read and the
    functional-currency converter's frame.

    Args:
        session: The tenant-scoped session opened by the caller.
        as_of: The evaluation date — the window's end.

    Returns:
        The :class:`MarketRead`. A book with nothing priceable, no rates or
        no values yields empty tuples, never an error.
    """
    active = await InvestmentRepository(session).list_active()
    universe = await _priceable(session, active)

    instruments = await _instrument_moves(session, universe, as_of=as_of)

    frame = await FxRateRepository(session).load_rates_frame(
        sorted({investment.currency for investment in active}),
        from_date=window_start(as_of, MARKET_WINDOW_DAYS),
        to_date=as_of,
    )
    fx = _fx_moves(frame, [investment.currency for investment in active], as_of=as_of)

    asset_classes: tuple[AssetClassMove, ...] = ()
    if instruments:
        series = await load_nav_series(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
        )
        converter = await build_portfolio_fx_converter(
            tenants=TenantRepository(session),
            fx_rates=FxRateRepository(session),
            position_currencies=[move.currency for move in instruments],
        )
        labels = {
            asset_class.id: asset_class.display_name
            for asset_class in await AssetClassRepository(session).list_all()
        }
        asset_classes = _asset_class_moves(
            instruments,
            _weights(instruments, series, converter, as_of=as_of),
            labels,
            functional_currency=converter.functional_currency,
            as_of=as_of,
        )

    return MarketRead(
        as_of=as_of,
        window_start=window_start(as_of, MARKET_WINDOW_DAYS),
        window_days=MARKET_WINDOW_DAYS,
        instruments=instruments,
        fx=fx,
        asset_classes=asset_classes,
    )


async def _priceable(session: AsyncSession, active: list[InvestmentDTO]) -> list[InvestmentDTO]:
    """Return the active investments with at least one market identifier.

    The ADR-0116 §8 precedent, one identifier read per investment exactly
    as the seeder and the ``price`` picker run it. No watched-exclusion:
    that exists to keep a second watchpoint identity off one instrument,
    which has no meaning for a read of the whole book.
    """
    identifiers = InvestmentIdentifierRepository(session)
    priceable: list[InvestmentDTO] = []
    for investment in active:
        if await identifiers.list_for_investment(investment.id):
            priceable.append(investment)
    return priceable


async def _instrument_moves(
    session: AsyncSession, universe: list[InvestmentDTO], *, as_of: date
) -> tuple[InstrumentMove, ...]:
    """Fetch the universe's prices once, then measure each instrument."""
    rows = await InstrumentPriceRepository(session).list_by_investments(
        [investment.id for investment in universe],
        from_date=window_start(as_of, MARKET_WINDOW_DAYS),
        to_date=as_of,
    )
    series_by_investment: dict[UUID, list[DatedValue]] = defaultdict(list)
    for row in rows:
        series_by_investment[row.investment_id].append(
            DatedValue(as_of_date=row.as_of_date, value=row.price)
        )

    moves: list[InstrumentMove] = []
    for investment in universe:
        window = _window_move(series_by_investment.get(investment.id, []), as_of=as_of)
        if window is None:
            continue
        reference, latest, move = window
        moves.append(
            InstrumentMove(
                id=f"instrument:{investment.id}",
                label=investment.name,
                value=move,
                unit=MOVE_UNIT,
                as_of=as_of,
                window_start=window_start(as_of, MARKET_WINDOW_DAYS),
                window_days=MARKET_WINDOW_DAYS,
                investment_id=investment.id,
                asset_class_id=investment.asset_class_id,
                currency=investment.currency,
                reference_date=reference.as_of_date,
                reference_price=reference.value,
                latest_date=latest.as_of_date,
                latest_price=latest.value,
            )
        )
    return tuple(moves)


def _window_move(
    series: Sequence[DatedValue], *, as_of: date
) -> tuple[DatedValue, DatedValue, Decimal] | None:
    """Return ``(reference, latest, move)`` over the fixed window, or ``None``.

    The window resolves through the same pure function the ``price`` and
    ``fx`` producers use, so the carry-forward on both ends and the refusal
    of a zero reference are theirs. ``None`` is absence: there is no move
    to state, which is not a move of zero.
    """
    window = resolve_window(
        series,
        as_of=as_of,
        window_days=MARKET_WINDOW_DAYS,
        subject_key="market",
        quantity="value",
    )
    if isinstance(window, NoObservation):
        return None
    reference, latest = window
    return reference, latest, percentage_move(reference.value, latest.value)


def _fx_moves(frame: pd.DataFrame, currencies: Iterable[str], *, as_of: date) -> tuple[FxMove, ...]:
    """Measure every book currency against the frame's reference currency.

    The reference currency is read off the frame — a property of the
    dataset, not of the tenant (ADR-0099 §2). An empty frame has none and
    yields no record.
    """
    if frame.empty:
        return ()
    quote = str(frame["reference_currency"].iloc[0])
    converter = FxConverter(frame, quote)
    observation_dates = sorted({ts.date() for ts in frame["as_of_date"]})

    moves: list[FxMove] = []
    for base in sorted(set(currencies)):
        if base == quote:
            continue
        window = _window_move(_leg_series(converter, base, quote, observation_dates), as_of=as_of)
        if window is None:
            continue
        reference, latest, move = window
        moves.append(
            FxMove(
                id=f"fx:{base}/{quote}",
                label=f"{base}/{quote}",
                value=move,
                unit=MOVE_UNIT,
                as_of=as_of,
                window_start=window_start(as_of, MARKET_WINDOW_DAYS),
                window_days=MARKET_WINDOW_DAYS,
                base_currency=base,
                quote_currency=quote,
                reference_date=reference.as_of_date,
                reference_rate=reference.value,
                latest_date=latest.as_of_date,
                latest_rate=latest.value,
            )
        )
    return tuple(moves)


def _leg_series(
    converter: FxConverter, base: str, quote: str, observation_dates: list[date]
) -> list[DatedValue]:
    """Build one unit of ``base`` priced in ``quote`` on every observed date.

    The orientation ``fx`` observation fixes for a ``BASE/QUOTE`` pair,
    through the same conversion seam. A date before the leg's first stored
    rate raises :class:`~core.exceptions.MissingFxRateError` and is skipped:
    a fabricated rate would be the silent 1:1 fallback ADR-0099 forbids.
    """
    series: list[DatedValue] = []
    for as_of_date in observation_dates:
        try:
            series.append(
                DatedValue(
                    as_of_date=as_of_date,
                    value=converter.convert(_ONE, base, quote, as_of_date),
                )
            )
        except MissingFxRateError:
            continue
    return series


def _weights(
    instruments: Sequence[InstrumentMove],
    series: list[NavSeries],
    converter: PortfolioFxConverter,
    *,
    as_of: date,
) -> dict[UUID, Decimal]:
    """Value each moved instrument in the functional currency at ``as_of``.

    Each value is :func:`~services.investments.aum.compute_aum` over that
    one investment — the AUM definition's own carry-forward and conversion,
    applied per investment, so a weight here is the investment's share of
    the AUM the book states. An instrument whose value cannot be converted
    gets no weight and is logged; it is never valued at a 1:1 rate.
    """
    series_by_investment = {investment.id: (investment, d, v) for investment, d, v in series}
    weights: dict[UUID, Decimal] = {}
    for move in instruments:
        own = series_by_investment.get(move.investment_id)
        if own is None:
            continue
        try:
            weights[move.investment_id] = compute_aum([own], as_of, converter).total
        except MissingFxRateError as exc:
            _LOG.warning(
                "watch-desk market read: %s carries no class weight — %s",
                move.id,
                exc,
            )
    return weights


def _asset_class_moves(
    instruments: Sequence[InstrumentMove],
    weights: dict[UUID, Decimal],
    labels: dict[UUID, str],
    *,
    functional_currency: str,
    as_of: date,
) -> tuple[AssetClassMove, ...]:
    """Aggregate the moved instruments into value-weighted class moves.

    Only instruments with a move *and* a positive weight enter, on both
    sides of the quotient; a class none of whose instruments qualifies is
    absent.
    """
    by_class: dict[UUID, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for move in instruments:
        weight = weights.get(move.investment_id)
        if weight is None or weight <= _ZERO:
            continue
        by_class[move.asset_class_id].append((weight, move.value))

    moves: list[AssetClassMove] = []
    for asset_class_id, pairs in by_class.items():
        weight_total = sum((weight for weight, _move in pairs), _ZERO)
        weighted = sum((weight * move for weight, move in pairs), _ZERO)
        moves.append(
            AssetClassMove(
                id=f"asset_class:{asset_class_id}",
                label=labels.get(asset_class_id, str(asset_class_id)),
                value=quantize_pp(weighted / weight_total),
                unit=MOVE_UNIT,
                as_of=as_of,
                window_start=window_start(as_of, MARKET_WINDOW_DAYS),
                window_days=MARKET_WINDOW_DAYS,
                asset_class_id=asset_class_id,
                instrument_count=len(pairs),
                weight_total=weight_total,
                functional_currency=functional_currency,
            )
        )
    return tuple(sorted(moves, key=lambda move: (move.label, str(move.asset_class_id))))


__all__ = [
    "MARKET_WINDOW_DAYS",
    "MOVE_UNIT",
    "AssetClassMove",
    "FxMove",
    "InstrumentMove",
    "MarketRead",
    "read_market",
]
