# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Watch Desk market read (D-WD-20, ADR-0133 §1.2).

Two halves. The pure half pins the arithmetic on plain values: the move and
its carry-forward, absence where a figure has no window pair, the FX leg's
orientation against the observation layer's own pair series, and the
value-weighted class aggregate. The live-DB half pins the read end to end
against the compose Postgres: the priceable universe (watched or not), and
one mixed-currency book whose class aggregate is computed by hand below.

The claims that carry weight:

* a figure without both window points is **absent**, never zero;
* an FX leg is one unit of the book currency priced in the reference
  currency, and a date before the leg's first rate is skipped, never 1:1;
* the class aggregate's numerator and denominator run over the same set —
  the instruments that have a move;
* an already-watched instrument is part of the read.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InstrumentPriceRepository,
    InvestmentRepository,
    tenant_context,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.watchpoint_repository import WatchpointRepository
from services.analytics.signal_watch import DatedValue
from services.fx.conversion import FxConverter
from services.watch_desk.market_read import (
    MARKET_WINDOW_DAYS,
    MOVE_UNIT,
    InstrumentMove,
    _asset_class_moves,
    _fx_moves,
    _window_move,
    read_market,
)
from services.watch_desk.signal_observation import _pair_series
from tests.services.irene._book_fixtures import D, seed_user

_AS_OF = date(2026, 8, 10)
#: ``as_of − 5 days``: the reference is the last value at or before it.
_WINDOW_START = date(2026, 8, 5)


def _series(*points: tuple[date, str]) -> list[DatedValue]:
    return [DatedValue(as_of_date=day, value=D(value)) for day, value in points]


def _frame(*rows: tuple[date, str, str], reference: str = "EUR") -> pd.DataFrame:
    """Build a rates frame in the repository's column contract."""
    return pd.DataFrame(
        {
            "as_of_date": pd.to_datetime([day for day, _ccy, _rate in rows]),
            "currency": [ccy for _day, ccy, _rate in rows],
            "rate_to_reference": [D(rate) for _day, _ccy, rate in rows],
            "reference_currency": [reference for _row in rows],
        }
    )


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "as_of_date": pd.Series([], dtype="datetime64[ns]"),
            "currency": pd.Series([], dtype="object"),
            "rate_to_reference": pd.Series([], dtype="object"),
            "reference_currency": pd.Series([], dtype="object"),
        }
    )


def _move(investment_id: UUID, asset_class_id: UUID, value: str) -> InstrumentMove:
    return InstrumentMove(
        id=f"instrument:{investment_id}",
        label=str(investment_id),
        value=D(value),
        unit=MOVE_UNIT,
        as_of=_AS_OF,
        window_start=_WINDOW_START,
        window_days=MARKET_WINDOW_DAYS,
        investment_id=investment_id,
        asset_class_id=asset_class_id,
        currency="EUR",
        reference_date=_WINDOW_START,
        reference_price=D("100"),
        latest_date=_AS_OF,
        latest_price=D("100"),
    )


# ---------------------------------------------------------------------------
# The move — carry-forward and absence
# ---------------------------------------------------------------------------


def test_a_sparse_series_carries_forward_to_both_ends_of_the_window() -> None:
    """The reference is the last price at or before the window's start."""
    window = _window_move(
        _series((date(2026, 8, 1), "100"), (date(2026, 8, 7), "103")), as_of=_AS_OF
    )

    assert window is not None
    reference, latest, move = window
    assert reference.as_of_date == date(2026, 8, 1)
    assert latest.as_of_date == date(2026, 8, 7)
    assert move == D("3.0000")


def test_the_move_is_signed() -> None:
    window = _window_move(_series((_WINDOW_START, "100"), (_AS_OF, "96.5")), as_of=_AS_OF)

    assert window is not None
    assert window[2] == D("-3.5000")


def test_a_series_without_a_point_at_the_window_start_is_absent_not_zero() -> None:
    """Prices only inside the window: no reference, so no move at all."""
    assert _window_move(_series((date(2026, 8, 6), "100"), (_AS_OF, "90")), as_of=_AS_OF) is None
    assert _window_move([], as_of=_AS_OF) is None


# ---------------------------------------------------------------------------
# FX legs — orientation, anchors, the reference currency
# ---------------------------------------------------------------------------


def test_the_fx_leg_agrees_with_the_observation_layers_pair_series() -> None:
    """Same frame, same seam: BASE = the book currency, QUOTE = the reference."""
    frame = _frame(
        (date(2026, 8, 3), "USD", "0.80"),
        (date(2026, 8, 6), "USD", "0.82"),
        (_AS_OF, "USD", "0.84"),
    )
    (leg,) = _fx_moves(frame, ["USD", "EUR"], as_of=_AS_OF)

    converter = FxConverter(frame, "EUR")
    dates = sorted({ts.date() for ts in frame["as_of_date"]})
    observed = {
        point.as_of_date: point.value for point in _pair_series(converter, "USD", "EUR", dates)
    }

    assert (leg.base_currency, leg.quote_currency) == ("USD", "EUR")
    assert leg.id == "fx:USD/EUR"
    assert leg.reference_date == date(2026, 8, 3)
    assert leg.reference_rate == observed[date(2026, 8, 3)]
    assert leg.latest_rate == observed[_AS_OF]
    assert leg.value == D("5.0000")
    assert leg.unit == MOVE_UNIT
    assert (leg.window_start, leg.window_days) == (_WINDOW_START, MARKET_WINDOW_DAYS)


def test_a_leg_without_an_anchor_is_skipped_never_defaulted() -> None:
    """GBP's first rate falls inside the window: absent, not a 1:1 move.

    USD's earlier quote puts 2026-08-03 among the observation dates; GBP
    has no rate there, and the leg series must skip that date rather than
    price GBP at 1 against the reference.
    """
    frame = _frame(
        (date(2026, 8, 3), "USD", "0.80"),
        (_AS_OF, "USD", "0.84"),
        (date(2026, 8, 7), "GBP", "1.17"),
        (_AS_OF, "GBP", "1.18"),
    )
    legs = _fx_moves(frame, ["GBP", "USD"], as_of=_AS_OF)

    assert [leg.base_currency for leg in legs] == ["USD"]


def test_an_empty_frame_yields_no_fx_record() -> None:
    assert _fx_moves(_empty_frame(), ["USD", "EUR"], as_of=_AS_OF) == ()


def test_the_reference_currency_has_no_record() -> None:
    """EUR against EUR is the identity, not a move of zero."""
    frame = _frame((date(2026, 8, 3), "USD", "0.80"), (_AS_OF, "USD", "0.80"))
    legs = _fx_moves(frame, ["EUR", "USD", "EUR"], as_of=_AS_OF)

    assert [leg.label for leg in legs] == ["USD/EUR"]
    assert legs[0].value == D("0.0000")


# ---------------------------------------------------------------------------
# The class aggregate
# ---------------------------------------------------------------------------


def test_the_class_aggregate_runs_over_the_weighted_moved_instruments_only() -> None:
    """Σ(w·m)/Σ(w), with a weightless instrument out of both sides.

    (300·2 + 100·(−4)) / 400 = 200 / 400 = 0.5. The third instrument has a
    move but no value at the date, so it is in neither sum — counting it
    at weight zero would be the same thing, counting its move as 0 % would
    not be.
    """
    equities = uuid4()
    a, b, c = uuid4(), uuid4(), uuid4()
    (aggregate,) = _asset_class_moves(
        [_move(a, equities, "2"), _move(b, equities, "-4"), _move(c, equities, "9")],
        {a: D("300"), b: D("100")},
        {equities: "Equities"},
        functional_currency="EUR",
        as_of=_AS_OF,
    )

    assert aggregate.value == D("0.5000")
    assert aggregate.instrument_count == 2
    assert aggregate.weight_total == D("400")
    assert (aggregate.id, aggregate.label) == (f"asset_class:{equities}", "Equities")
    assert (aggregate.functional_currency, aggregate.unit) == ("EUR", MOVE_UNIT)


def test_a_class_with_no_weighted_moved_instrument_is_absent() -> None:
    equities, bonds = uuid4(), uuid4()
    a, b = uuid4(), uuid4()
    moves = _asset_class_moves(
        [_move(a, equities, "1"), _move(b, bonds, "-1")],
        {a: D("100"), b: D("0")},
        {equities: "Equities", bonds: "Bonds"},
        functional_currency="EUR",
        as_of=_AS_OF,
    )

    assert [move.label for move in moves] == ["Equities"]


# ---------------------------------------------------------------------------
# Live DB — the universe and one hand-computed mixed-currency book
# ---------------------------------------------------------------------------


async def _seed_book(app_engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> dict[str, UUID]:
    """Seed a two-currency priceable book, plus what must stay out of it.

    * ``euro`` — EUR, Equities. 100 on 08-03 (carried to the window start),
      102 on 08-10: +2 %. NAV 1,000,000 EUR.
    * ``dollar`` — USD, Equities. 50 on 08-05, 48 on 08-10: −4 %.
      NAV 500,000 USD; USD is 0.80 EUR on 08-05 and 0.84 on 08-10.
    * ``bund`` — EUR, Bonds, priceable but quoted only inside the window:
      no move, so Bonds has no aggregate.
    * ``fund`` — prices that move, but no market identifier: unpriceable.
    * ``retired`` — identifier and moving prices, but inactive.
    """
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        classes = AssetClassRepository(session)
        equities = await classes.create(code="equities", display_name="Equities")
        bonds = await classes.create(code="bonds", display_name="Bonds")
        investments = InvestmentRepository(session)
        identifiers = InvestmentIdentifierRepository(session)
        prices = InstrumentPriceRepository(session)
        navs = InvestmentNavRepository(session)

        seeded: dict[str, UUID] = {"equities": equities.id, "bonds": bonds.id}
        book = (
            ("euro", "Euro Equity", equities.id, "EUR", True, True),
            ("dollar", "US Equity", equities.id, "USD", True, True),
            ("bund", "Bund", bonds.id, "EUR", True, True),
            ("fund", "Buyout Fund", equities.id, "EUR", False, True),
            ("retired", "Retired Equity", equities.id, "EUR", True, False),
        )
        for key, name, class_id, currency, priceable, active in book:
            investment = await investments.create(
                name=name,
                investment_type="listed_equity" if priceable else "private_equity",
                asset_class_id=class_id,
                currency=currency,
                created_by=user_id,
                is_active=active,
            )
            seeded[key] = investment.id
            if priceable:
                await identifiers.add(
                    investment_id=investment.id,
                    scheme="isin",
                    value=f"XX{investment.id.hex[:10]}",
                    created_by=user_id,
                )

        price_points = {
            "euro": [(date(2026, 8, 3), "100"), (date(2026, 8, 7), "101"), (_AS_OF, "102")],
            "dollar": [(_WINDOW_START, "50"), (_AS_OF, "48")],
            "bund": [(date(2026, 8, 7), "99"), (_AS_OF, "98")],
            "fund": [(_WINDOW_START, "10"), (_AS_OF, "20")],
            "retired": [(_WINDOW_START, "10"), (_AS_OF, "5")],
        }
        currency_of = {"dollar": "USD"}
        for key, points in price_points.items():
            for as_of_date, value in points:
                await prices.upsert(
                    investment_id=seeded[key],
                    as_of_date=as_of_date,
                    price=D(value),
                    currency=currency_of.get(key, "EUR"),
                    source="test",
                    created_by=user_id,
                )

        for key, value, currency in (
            ("euro", "1000000", "EUR"),
            ("dollar", "500000", "USD"),
            ("bund", "250000", "EUR"),
        ):
            await navs.upsert(
                seeded[key], date(2026, 7, 31), "actual", D(value), currency, "test", user_id
            )

        rates = FxRateRepository(session)
        for as_of_date, value in ((_WINDOW_START, "0.80"), (_AS_OF, "0.84")):
            await rates.upsert(
                currency="USD",
                as_of_date=as_of_date,
                rate_to_reference=D(value),
                reference_currency="EUR",
                source="test",
                created_by=user_id,
            )
    return seeded


async def _read(app_engine: AsyncEngine, tenant_id: UUID):
    async with tenant_context(app_engine, tenant_id) as session:
        return await read_market(session, as_of=_AS_OF)


async def test_the_read_values_a_mixed_currency_book_as_computed_by_hand(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The Equities aggregate, by hand.

    Weights at 08-10 in EUR: euro 1,000,000; dollar 500,000 × 0.84 =
    420,000. (1,000,000 × 2 + 420,000 × (−4)) / 1,420,000 =
    320,000 / 1,420,000 = 0.225352… → 0.2254.
    """
    tenant_id = await seed_tenant("market-read-book")
    user_id = await seed_user(superuser_engine, tenant_id)
    seeded = await _seed_book(app_engine, tenant_id, user_id)

    read = await _read(app_engine, tenant_id)

    assert (read.as_of, read.window_start, read.window_days) == (_AS_OF, _WINDOW_START, 5)

    moves = {move.investment_id: move for move in read.instruments}
    assert set(moves) == {seeded["euro"], seeded["dollar"]}
    assert moves[seeded["euro"]].value == D("2.0000")
    assert moves[seeded["euro"]].reference_date == date(2026, 8, 3)
    assert moves[seeded["dollar"]].value == D("-4.0000")
    assert moves[seeded["dollar"]].label == "US Equity"

    (usd,) = read.fx
    assert (usd.label, usd.value) == ("USD/EUR", D("5.0000"))

    (equities,) = read.asset_classes
    assert equities.asset_class_id == seeded["equities"]
    assert equities.label == "Equities"
    assert equities.value == D("0.2254")
    assert equities.instrument_count == 2
    assert equities.weight_total == D("1420000")
    assert equities.functional_currency == "EUR"


async def test_an_already_watched_instrument_stays_in_the_read(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The ``price`` picker's watched-exclusion is not applied here."""
    tenant_id = await seed_tenant("market-read-watched")
    user_id = await seed_user(superuser_engine, tenant_id)
    seeded = await _seed_book(app_engine, tenant_id, user_id)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await WatchpointRepository(session).create(
            family="price",
            subject_key=f"price:{seeded['euro']}",
            display_name="Euro Equity",
            effective_from=datetime.now(timezone.utc) - timedelta(hours=1),
            instrument_id=seeded["euro"],
            drop_pct=D("5.0"),
            window_days=5,
        )

    read = await _read(app_engine, tenant_id)

    assert seeded["euro"] in {move.investment_id for move in read.instruments}


async def test_a_book_with_nothing_priceable_reads_empty(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("market-read-empty")
    await seed_user(superuser_engine, tenant_id)

    read = await _read(app_engine, tenant_id)

    assert (read.instruments, read.fx, read.asset_classes) == ((), (), ())
