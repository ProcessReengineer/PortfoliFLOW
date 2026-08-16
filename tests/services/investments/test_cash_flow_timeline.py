# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Cash Flow Planning timeline: result assembly (ADR-0104 §3/§5).

Synthetic and DB-free: the pure core takes frames, an actual history and a
converter, so the tests state the plan world they mean directly instead of
seeding it through four services. The one loader test uses the same fake
repositories the plan-world tests do — it exists to pin the *read path* (which
rows become the actual cash history), not to re-test the assembly.

What is pinned here:

* the **grid** — two actual periods and the horizon's plan periods, the seam
  index between them, and 8Q ≙ 24 columns under the monthly toggle
  (ADR-0104 §6),
* **balance sampling** — latest observation at or before the period end, held
  across empty periods: the one convention
  :mod:`services.overlay.steps` writes with and this module reads with,
* **empty is not zero** — a currency with nothing observed contributes an
  empty cell, on either side of the seam,
* the **functional total** — converted at the period end's own carry-forward
  rate, which past the last actual rate *is* that rate held flat (N1), and
  :class:`~core.exceptions.MissingFxRateError` propagating rather than a 1:1
  fallback,
* the **empty-overlay identity** — baseline and scenario value-identical, the
  Baseline/Scenario toggle's contract (ADR-0104 §4) and the deltas-first
  foundation (§5),
* and the **identical-history invariant** (§5) at this layer: a non-empty
  overlay moves scenario cells right of the seam and no baseline cell at all.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.exceptions import (
    DuplicateCashPositionError,
    MissingFxRateError,
    PlanHorizonInvalidError,
)
from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from services.fx.conversion import FxConverter
from services.fx.functional_currency import PortfolioFxConverter
from services.investments.cash_flow_timeline import (
    Periodisation,
    build_actual_cash_paths,
    build_cash_flow_timeline,
    load_cash_flow_planning_inputs,
    project_cash_flow_planning,
)
from services.investments.archetype import Archetype
from services.overlay import (
    FxShock,
    InsertTransaction,
    MarketShock,
    PlanFrames,
    PlanInvestment,
)

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

#: The seam of every fixture below: a quarter end, so the grid's actual side
#: closes exactly on it and the arithmetic of the columns is readable.
_T0 = date(2026, 6, 30)

_D = Decimal


# ---------------------------------------------------------------------------
# The world, stated directly
# ---------------------------------------------------------------------------


def _path(points: dict[str, str]) -> pd.Series:
    """A Decimal-valued balance path — the shape ``plan_world`` lays down."""
    return pd.Series(
        [_D(value) for value in points.values()],
        index=pd.to_datetime([date.fromisoformat(day) for day in points]),
        dtype="object",
    )


def _frames(
    *,
    cash_paths: dict[str, pd.Series] | None = None,
    t0: date = _T0,
    investments: dict[UUID, PlanInvestment] | None = None,
    value_paths: dict[UUID, pd.Series] | None = None,
) -> PlanFrames:
    return PlanFrames(
        t0=t0,
        value_paths=value_paths or {},
        cash_paths=cash_paths or {},
        plan_flows=(),
        investments=investments or {},
    )


def _identity_converter(functional_currency: str = "EUR") -> PortfolioFxConverter:
    """The zero-read fast path: every position is already functional."""
    return PortfolioFxConverter(functional_currency, None)


def _converter(
    rates: dict[str, dict[str, str]],
    *,
    functional_currency: str = "EUR",
    reference_currency: str = "EUR",
) -> PortfolioFxConverter:
    """A rate-backed converter, quoted against ``reference_currency``.

    Args:
        rates: ``{currency: {iso date: rate to reference}}``.
        functional_currency: The currency the totals are stated in.
        reference_currency: The base the rates are quoted against.
    """
    frame = pd.DataFrame(
        [
            {
                "as_of_date": pd.Timestamp(day),
                "currency": currency,
                "rate_to_reference": _D(rate),
                "reference_currency": reference_currency,
            }
            for currency, series in rates.items()
            for day, rate in series.items()
        ]
    )
    return PortfolioFxConverter(functional_currency, FxConverter(frame, reference_currency))


def _row(timeline, currency: str) -> tuple[Decimal | None, ...]:
    """The balances of one currency row."""
    return next(row.balances for row in timeline.currency_rows if row.currency == currency)


# ---------------------------------------------------------------------------
# 1. The period grid (ADR-0104 §6)
# ---------------------------------------------------------------------------


def test_quarterly_grid_has_two_actual_periods_and_the_horizon() -> None:
    """Two actual columns, eight plan columns, the seam between them."""
    actual = {"EUR": _path({"2025-12-31": "100", "2026-06-30": "120"})}
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "120"})}),
        actual_cash=actual,
        converter=_identity_converter(),
        periodisation=Periodisation.QUARTERLY,
        horizon_quarters=8,
    )

    assert len(timeline.periods) == 10
    assert timeline.seam_index == 2
    assert timeline.seam_date == _T0

    ends = [period.end_date for period in timeline.periods]
    assert ends[:3] == [date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]
    assert ends[-1] == date(2028, 6, 30)

    # The seam is a single rule: actual strictly left, plan strictly right.
    assert [period.is_actual for period in timeline.periods] == [True] * 2 + [False] * 8
    assert all(period.end_date <= _T0 for period in timeline.periods[:2])
    assert all(period.end_date > _T0 for period in timeline.periods[2:])
    assert timeline.periods[1].label == "Q2 2026"


def test_monthly_periodisation_cuts_eight_quarters_into_24_columns() -> None:
    """The horizon stays 8Q; the toggle only re-cuts it (ADR-0104 §6)."""
    actual = {"EUR": _path({"2026-04-30": "100", "2026-06-30": "120"})}
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "120"})}),
        actual_cash=actual,
        converter=_identity_converter(),
        periodisation=Periodisation.MONTHLY,
        horizon_quarters=8,
    )

    plan = [period for period in timeline.periods if not period.is_actual]
    assert len(plan) == 24
    assert timeline.seam_index == 2
    assert [period.end_date for period in timeline.periods[:3]] == [
        date(2026, 5, 31),
        date(2026, 6, 30),
        date(2026, 7, 31),
    ]
    # Same span as the quarterly grid: eight quarters past the seam.
    assert plan[-1].end_date == date(2028, 6, 30)
    assert timeline.periods[2].label == "Jul 2026"


@pytest.mark.parametrize(
    ("periodisation", "expected"),
    [(Periodisation.QUARTERLY, 4), (Periodisation.MONTHLY, 12)],
)
def test_four_quarter_horizon(periodisation, expected) -> None:
    """4Q is four quarterly columns or twelve monthly ones."""
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "10"})}),
        actual_cash={"EUR": _path({"2026-06-30": "10"})},
        converter=_identity_converter(),
        periodisation=periodisation,
        horizon_quarters=4,
    )
    assert sum(1 for p in timeline.periods if not p.is_actual) == expected


def test_seam_mid_quarter_puts_the_containing_quarter_on_the_plan_side() -> None:
    """A period is actual iff it *ends* at or before t₀ — one rule, no gap."""
    t0 = date(2026, 5, 15)
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-05-15": "10"})}, t0=t0),
        actual_cash={"EUR": _path({"2025-12-31": "5", "2026-03-31": "8"})},
        converter=_identity_converter(),
        horizon_quarters=4,
    )
    ends = [period.end_date for period in timeline.periods]
    assert ends[:3] == [date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    assert timeline.seam_index == 2
    assert timeline.periods[2].is_actual is False


# ---------------------------------------------------------------------------
# 2. Balance sampling — latest at or before the period end
# ---------------------------------------------------------------------------


def test_mid_period_balance_appears_at_the_period_end_and_holds() -> None:
    """A level set mid-period is the level in force at that period's end.

    And it *holds* across the periods that observe nothing — a balance path
    is a level series, so an empty period is not an empty cell but the same
    balance again (:mod:`services.overlay.steps`).
    """
    plan = _path({"2026-06-30": "100", "2026-08-14": "250"})
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": plan}),
        actual_cash={"EUR": _path({"2026-03-31": "90", "2026-06-30": "100"})},
        converter=_identity_converter(),
        horizon_quarters=4,
    )

    # Q1 / Q2 actual, then Q3 (the 14 Aug level), then three periods that
    # observe nothing and carry it forward unchanged.
    assert _row(timeline, "EUR") == (
        _D("90"),
        _D("100"),
        _D("250"),
        _D("250"),
        _D("250"),
        _D("250"),
    )


# ---------------------------------------------------------------------------
# 3./4. The functional total row (ADR-0099 §4, ADR-0104 §3 N1)
# ---------------------------------------------------------------------------


def test_total_converts_plan_periods_at_the_carried_forward_rate() -> None:
    """Past the last actual rate, the rate is that rate — held flat (N1)."""
    converter = _converter(
        {"USD": {"2026-03-31": "0.90", "2026-06-30": "0.80"}},
        functional_currency="EUR",
    )
    frames = _frames(
        cash_paths={
            "EUR": _path({"2026-06-30": "100"}),
            "USD": _path({"2026-06-30": "200"}),
        }
    )
    timeline = build_cash_flow_timeline(
        frames=frames,
        actual_cash={
            "EUR": _path({"2026-03-31": "50", "2026-06-30": "100"}),
            "USD": _path({"2026-03-31": "100", "2026-06-30": "200"}),
        },
        converter=converter,
        horizon_quarters=4,
    )

    # Q1 (actual): 50 EUR + 100 USD @ 0.90 = 140.
    assert timeline.total[0] == _D("140.00")
    # Q2 (actual, the seam): 100 EUR + 200 USD @ 0.80 = 260.
    assert timeline.total[1] == _D("260.00")
    # Every plan period: the 30 Jun rate — the last one stored — held flat.
    # 100 EUR + 200 USD @ 0.80 = 260, and *not* a rate that keeps moving.
    assert timeline.total[2:] == (_D("260.00"),) * 4
    assert converter.convert_amount(_D("1"), "USD", date(2028, 6, 30)) == _D("0.80")
    assert timeline.functional_currency == "EUR"


def test_total_is_empty_where_no_currency_contributes() -> None:
    """No balance observed anywhere in a column: an empty total, not a zero.

    The book's only cash path opens in Q4, and it has no statement history at
    all — so the first plan column observes nothing. The total states that as
    an empty cell: a zero would read as an account drawn to nil.
    """
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-11-15": "100"})}),
        actual_cash={},
        converter=_identity_converter(),
        horizon_quarters=4,
    )
    assert timeline.seam_index == 0  # no actual history, so no actual column
    assert [period.is_actual for period in timeline.periods] == [False] * 4
    assert timeline.total[0] is None  # Q3 — the path has not opened yet
    assert timeline.total[1:] == (_D("100"),) * 3
    assert _row(timeline, "EUR")[0] is None


def test_missing_fx_rate_propagates_from_the_total_row() -> None:
    """A currency with no rate at or before the period end fails loudly."""
    converter = _converter(
        # The USD rate opens *after* the first period end.
        {"USD": {"2026-06-30": "0.80"}},
        functional_currency="EUR",
    )
    timeline_inputs = {
        "frames": _frames(cash_paths={"USD": _path({"2026-06-30": "200"})}),
        "actual_cash": {"USD": _path({"2026-03-31": "100", "2026-06-30": "200"})},
        "converter": converter,
        "horizon_quarters": 4,
    }
    with pytest.raises(MissingFxRateError) as excinfo:
        build_cash_flow_timeline(**timeline_inputs)

    assert excinfo.value.currency == "USD"
    assert excinfo.value.as_of_date == date(2026, 3, 31)


# ---------------------------------------------------------------------------
# 5. Horizon validation (ADR-0104 §6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("horizon", [4, 8, 12])
def test_offered_horizons_are_accepted(horizon: int) -> None:
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "10"})}),
        actual_cash={"EUR": _path({"2026-06-30": "10"})},
        converter=_identity_converter(),
        horizon_quarters=horizon,
    )
    assert sum(1 for p in timeline.periods if not p.is_actual) == horizon


@pytest.mark.parametrize("horizon", [6, 0, -4, 16])
def test_unoffered_horizon_raises(horizon: int) -> None:
    """Validated, never clamped: a horizon nobody chose is a wrong answer."""
    with pytest.raises(PlanHorizonInvalidError) as excinfo:
        build_cash_flow_timeline(
            frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "10"})}),
            actual_cash={},
            converter=_identity_converter(),
            horizon_quarters=horizon,
        )
    assert excinfo.value.field == "horizon_quarters"


# ---------------------------------------------------------------------------
# 6./7. The composer — the Baseline/Scenario contract (ADR-0104 §4/§5)
# ---------------------------------------------------------------------------


def _one_investment_world() -> tuple[UUID, PlanFrames]:
    """A book with one EUR fund and one EUR cash position."""
    investment_id = uuid4()
    frames = _frames(
        cash_paths={"EUR": _path({"2026-06-30": "1000"})},
        value_paths={investment_id: _path({"2026-06-30": "5000"})},
        investments={
            investment_id: PlanInvestment(
                investment_id=investment_id,
                currency="EUR",
                investment_type="listed_equity",
            )
        },
    )
    return investment_id, frames


def test_empty_overlay_yields_value_identical_timelines() -> None:
    """The toggle's contract: baseline and scenario through one code path."""
    _investment_id, frames = _one_investment_world()
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(),
        actual_cash={"EUR": _path({"2026-03-31": "900", "2026-06-30": "1000"})},
        converter=_identity_converter(),
        horizon_quarters=8,
    )
    assert result.baseline == result.scenario


def test_inserted_transaction_moves_scenario_cells_only() -> None:
    """The identical-history invariant, at the assembly layer (ADR-0104 §5)."""
    investment_id, frames = _one_investment_world()
    actual_cash = {"EUR": _path({"2026-03-31": "900", "2026-06-30": "1000"})}

    # A 300 EUR buy settling on 12 Aug 2026 — inside Q3, right of the seam.
    buy = InsertTransaction(
        investment_id=investment_id,
        txn_type="buy",
        trade_date=date(2026, 8, 12),
        units=_D("3"),
        price_per_unit=_D("100"),
        consideration=None,
        currency="EUR",
    )
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(buy,),
        actual_cash=actual_cash,
        converter=_identity_converter(),
        horizon_quarters=4,
    )

    baseline_row = _row(result.baseline, "EUR")
    scenario_row = _row(result.scenario, "EUR")

    # The baseline is untouched: the cash path holds 1000 across the horizon.
    assert baseline_row == (_D("900"), _D("1000"), _D("1000"), _D("1000"), _D("1000"), _D("1000"))
    # The scenario debits the settling cash path from Q3 onward — and nowhere
    # left of the seam.
    assert scenario_row[:2] == baseline_row[:2]
    assert scenario_row[2:] == (_D("700"),) * 4
    assert result.baseline.periods == result.scenario.periods
    assert result.baseline.seam_index == result.scenario.seam_index

    # The frames the composer was handed are not mutated (ADR-0104 §2).
    assert list(frames.cash_paths["EUR"]) == [_D("1000")]


# ---------------------------------------------------------------------------
# 7b. The fx_shock at the conversion seam (ADR-0104 §2/§3, N3)
# ---------------------------------------------------------------------------


def _multi_currency_world() -> tuple[UUID, PlanFrames, dict[str, pd.Series]]:
    """A EUR-functional book with EUR, USD and GBP cash, and one **USD** fund.

    The fund is USD-denominated so a hypothetical trade on it settles against the
    USD cash path: the overlay never converts (ADR-0104 §3, N2), so an insert
    must settle in the investment's own currency — ``execute_insert_transaction``
    refuses anything else with ``CurrencyMismatchError``. That is exactly the
    leg the composition test needs, since it is the *position-currency* balance a
    value transformation moves and the *seam* that then translates it.

    The seam is a quarter end, so both actual columns close on real balances and
    every number below is readable by hand.
    """
    investment_id = uuid4()
    frames = _frames(
        cash_paths={
            "EUR": _path({"2026-06-30": "1000"}),
            "USD": _path({"2026-06-30": "600"}),
            "GBP": _path({"2026-06-30": "400"}),
        },
        value_paths={investment_id: _path({"2026-06-30": "5000"})},
        investments={
            investment_id: PlanInvestment(
                investment_id=investment_id,
                currency="USD",
                investment_type="listed_equity",
            )
        },
    )
    actual_cash = {
        "EUR": _path({"2026-03-31": "900", "2026-06-30": "1000"}),
        "USD": _path({"2026-03-31": "500", "2026-06-30": "600"}),
        "GBP": _path({"2026-03-31": "300", "2026-06-30": "400"}),
    }
    return investment_id, frames, actual_cash


def _multi_currency_converter() -> PortfolioFxConverter:
    """Rates at both actual period ends, so the realised columns convert."""
    return _converter(
        {
            "USD": {"2026-03-31": "0.90", "2026-06-30": "0.92"},
            "GBP": {"2026-03-31": "1.10", "2026-06-30": "1.15"},
        }
    )


#: One USD shock of −10 %: one USD is worth 90 % of what it was, so a long-USD
#: book is worth *less* in EUR over the plan horizon (ADR-0099 §2's quoting
#: convention — ``rate_to_reference`` is the price of one unit in the reference).
_USD_MINUS_10 = FxShock(currency="USD", magnitude=_D("-10"))


def test_an_fx_shock_moves_the_plan_total_and_leaves_history_alone() -> None:
    """Matrix 1: one foreign currency shocked, and the seam gate holds.

    The plan columns translate 600 USD at 0.828 rather than 0.92 — the whole
    move, and only on the USD leg. The **actual** columns do not budge: they
    convert at the rates that actually prevailed, because a shock restates the
    *held-flat plan path* (ADR-0104 §3, N1) and never realised history (§5, the
    identical-history invariant). A shock that moved a realised total would be
    restating the functional value of a statement the book has already reported.
    """
    _investment_id, frames, actual_cash = _multi_currency_world()
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(_USD_MINUS_10,),
        actual_cash=actual_cash,
        converter=_multi_currency_converter(),
        horizon_quarters=4,
    )

    # Q1 2026: 900 EUR + 500 USD × 0.90 + 300 GBP × 1.10 = 1680  (realised)
    # Q2 2026: 1000   + 600 USD × 0.92 + 400 GBP × 1.15 = 2012   (realised, t₀)
    # Q3–Q6:   1000   + 600 USD × 0.92 + 400 GBP × 1.15 = 2012   (plan, held flat)
    assert result.baseline.total == (
        _D("1680"),
        _D("2012"),
        _D("2012"),
        _D("2012"),
        _D("2012"),
        _D("2012"),
    )
    # The scenario: realised columns identical, plan columns at 600 × 0.828.
    assert result.scenario.total[:2] == result.baseline.total[:2]
    assert result.scenario.total[2:] == (_D("1956.800"),) * 4
    assert result.scenario.seam_index == result.baseline.seam_index == 2


def test_an_fx_shock_leaves_every_currency_row_byte_identical() -> None:
    """Matrix 6: the balances do not move; only their translation does.

    An ``fx_shock`` is a statement about the *conversion seam*, not about any
    value path (ADR-0104 §2). 600 USD of plan cash is still 600 USD after the
    dollar weakens — what changes is what those dollars are worth in EUR. The
    currency rows stay in position currency (§3, N2), so they must be identical
    down to the cell, and only :attr:`CashFlowTimeline.total` may differ.
    """
    _investment_id, frames, actual_cash = _multi_currency_world()
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(_USD_MINUS_10,),
        actual_cash=actual_cash,
        converter=_multi_currency_converter(),
        horizon_quarters=4,
    )

    assert result.scenario.currency_rows == result.baseline.currency_rows
    assert result.scenario.total != result.baseline.total
    assert _row(result.scenario, "USD") == (
        _D("500"),
        _D("600"),
        _D("600"),
        _D("600"),
        _D("600"),
        _D("600"),
    )


def test_only_the_shocked_currencys_leg_moves() -> None:
    """Matrix 2: two foreign currencies, one shocked.

    The GBP leg's functional contribution is byte-identical, which is what the
    delta proves: the whole plan-column move is exactly the USD leg's 10 %
    (600 × 0.92 × 0.1 = 55.20) and nothing else moved to make up the difference.
    """
    _investment_id, frames, actual_cash = _multi_currency_world()
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(_USD_MINUS_10,),
        actual_cash=actual_cash,
        converter=_multi_currency_converter(),
        horizon_quarters=4,
    )

    plan_delta = result.baseline.total[2] - result.scenario.total[2]
    assert plan_delta == _D("55.200")


def test_shocking_the_functional_currency_is_a_no_op() -> None:
    """Matrix 3: the numéraire cannot be shocked — it is the ruler.

    The path an ``fx_shock`` restates translates positions *of that currency*
    into the functional currency (ADR-0104 §2); for the functional currency
    itself that path is the identity, short-circuited before any rate is read.
    So the scenario is the baseline — and the *rate-backed* converter here shows
    the no-op is a property of the currency, not of an absent rate frame.
    """
    _investment_id, frames, actual_cash = _multi_currency_world()
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(FxShock(currency="EUR", magnitude=_D("-10")),),
        actual_cash=actual_cash,
        converter=_multi_currency_converter(),
        horizon_quarters=4,
    )

    assert result.scenario == result.baseline


def test_an_fx_shock_on_an_identity_converter_is_a_no_op() -> None:
    """Matrix 4: a single-currency tenant's scenario is its baseline.

    No FX row was loaded, and an ``fx_shock`` cannot be the reason one gets
    loaded: the converter arrives at this seam already built (ADR-0099 §3, the
    zero-read guarantee). The loader test below pins the *read* side of the same
    fact.
    """
    _investment_id, frames = _one_investment_world()
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=(_USD_MINUS_10,),
        actual_cash={"EUR": _path({"2026-03-31": "900", "2026-06-30": "1000"})},
        converter=_identity_converter(),
        horizon_quarters=4,
    )

    assert result.scenario == result.baseline


def test_a_value_transformation_and_an_fx_shock_compose_in_order() -> None:
    """Matrix 5: values fold first, FX restates the seam, aggregation last.

    The binding order of ADR-0104 §3, read off one number. The insert debits
    100 USD from the plan cash path — a **position-currency** effect, blind to
    FX — and the shock restates that currency's translation. So the Q3 total is
    ``1000 EUR + (600 − 100) USD × 0.828 + 400 GBP × 1.15``: the shocked rate
    multiplies the *transformed* balance. Had the FX been applied first, or the
    value effect been applied to a translated amount, the number would differ.

    The overlay carries all three relevant kinds — an ``insert_transaction`` and
    a ``market_shock`` for the fold, an ``fx_shock`` for the seam — and computes
    without raising, which is the mixed-overlay case: the ``market_shock`` moves
    the fund's value path and is invisible in a *cash* timeline, and that is
    correct rather than a gap.
    """
    investment_id, frames, actual_cash = _multi_currency_world()
    overlay = (
        InsertTransaction(
            investment_id=investment_id,
            txn_type="buy",
            trade_date=date(2026, 8, 12),
            units=_D("1"),
            price_per_unit=_D("100"),
            consideration=None,
            currency="USD",
        ),
        MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=_D("-20")),
        _USD_MINUS_10,
    )
    result = project_cash_flow_planning(
        baseline=frames,
        overlay=overlay,
        actual_cash=actual_cash,
        converter=_multi_currency_converter(),
        horizon_quarters=4,
    )

    # The position-currency balance moved by the insert, and by nothing else.
    assert _row(result.scenario, "USD")[2:] == (_D("500"),) * 4
    # 1000 + 500 × 0.828 + 400 × 1.15 = 1000 + 414 + 460 = 1874
    assert result.scenario.total[2:] == (_D("1874.000"),) * 4
    # And realised history is untouched by any of the three.
    assert result.scenario.total[:2] == result.baseline.total[:2]
    assert (
        result.scenario.currency_rows[0].balances[:2]
        == (result.baseline.currency_rows[0].balances[:2])
    )


def test_an_fx_shock_never_papers_over_a_missing_pair() -> None:
    """Matrix 7: a shock restates an FX path; it does not invent one.

    The book holds USD cash and the dataset never priced USD. The shock cannot
    conjure a path to scale, so the total row still fails loudly with the pair
    named — never a silent zero and never a 1:1 fallback (ADR-0099 §3). A shock
    that made an uncovered currency convertible would be that fallback by
    another route, and the Planning Desk surfaces this as the actionable
    "supply the missing rate" error (ADR-0104 §3).
    """
    _investment_id, frames, actual_cash = _multi_currency_world()

    with pytest.raises(MissingFxRateError) as caught:
        project_cash_flow_planning(
            baseline=frames,
            overlay=(_USD_MINUS_10,),
            actual_cash=actual_cash,
            converter=_converter({"GBP": {"2026-03-31": "1.10"}}),
            horizon_quarters=4,
        )

    assert caught.value.currency == "USD"


# ---------------------------------------------------------------------------
# 8./9. Short and asymmetric histories
# ---------------------------------------------------------------------------


def test_one_actual_period_is_not_padded_into_two() -> None:
    """A column in which nothing was ever observed is not history."""
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "100"})}),
        # The book's first statement *is* the seam quarter.
        actual_cash={"EUR": _path({"2026-06-30": "100"})},
        converter=_identity_converter(),
        horizon_quarters=4,
    )
    actual = [period for period in timeline.periods if period.is_actual]
    assert len(actual) == 1
    assert actual[0].end_date == date(2026, 6, 30)
    assert timeline.seam_index == 1
    assert len(timeline.periods) == 5


def test_currency_without_actual_history_has_empty_actual_cells() -> None:
    """A cash account opened after the seam: empty left of it, not zero."""
    timeline = build_cash_flow_timeline(
        frames=_frames(
            cash_paths={
                "EUR": _path({"2026-06-30": "1000"}),
                "USD": _path({"2026-09-15": "500"}),
            }
        ),
        actual_cash={"EUR": _path({"2026-03-31": "900", "2026-06-30": "1000"})},
        converter=_converter({"USD": {"2026-06-30": "0.80"}}, functional_currency="EUR"),
        horizon_quarters=4,
    )

    assert _row(timeline, "USD") == (
        None,  # Q1 — no USD account yet
        None,  # Q2 — still none at the seam
        _D("500"),  # Q3 — the plan path opens on 15 Sep
        _D("500"),
        _D("500"),
        _D("500"),
    )
    # The total still states the EUR balance in the columns USD is absent from.
    assert timeline.total[0] == _D("900")
    assert timeline.total[2] == _D("1000") + _D("500") * _D("0.80")


def test_currency_with_actual_history_but_no_plan_path() -> None:
    """A closed account: observed left of the seam, empty right of it."""
    timeline = build_cash_flow_timeline(
        frames=_frames(cash_paths={"EUR": _path({"2026-06-30": "1000"})}),
        actual_cash={
            "EUR": _path({"2026-03-31": "900", "2026-06-30": "1000"}),
            "CHF": _path({"2026-03-31": "40"}),
        },
        converter=_converter({"CHF": {"2026-03-31": "1.05"}}, functional_currency="EUR"),
        horizon_quarters=4,
    )

    chf = _row(timeline, "CHF")
    assert chf[0] == _D("40")  # observed
    assert chf[1] == _D("40")  # held flat to the seam
    assert chf[2:] == (None,) * 4  # no plan path: nothing, not zero
    assert timeline.total[2] == _D("1000")  # the plan side states EUR only


# ---------------------------------------------------------------------------
# The read path: which rows become the actual cash history
# ---------------------------------------------------------------------------


def _investment(
    *,
    name: str,
    currency: str = "EUR",
    investment_type: str = "private_equity",
) -> InvestmentDTO:
    return InvestmentDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        name=name,
        investment_type=investment_type,
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency=currency,
        vintage_year=None,
        commitment_amount=None,
        is_active=True,
        type_specific_data=None,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _nav(investment: InvestmentDTO, day: str, value: str) -> InvestmentNavDTO:
    return InvestmentNavDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        investment_id=investment.id,
        as_of_date=date.fromisoformat(day),
        nav_value=_D(value),
        currency=investment.currency,
        nav_kind="actual",
        source="statement",
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
        ingest_origin="excel",
    )


def test_actual_cash_paths_read_the_cash_positions_only() -> None:
    """The history is the cash positions' statement stream (ADR-0103 §1)."""
    fund = _investment(name="Fund I")
    eur_cash = _investment(name="EUR Cash", investment_type="cash")
    usd_cash = _investment(name="USD Cash", currency="usd", investment_type="cash")

    paths = build_actual_cash_paths(
        [
            (fund, [date(2026, 6, 30)], [_D("5000")]),
            (
                eur_cash,
                [date(2026, 3, 31), date(2026, 6, 30)],
                [_D("900"), _D("1000")],
            ),
            (usd_cash, [date(2026, 6, 30)], [_D("200")]),
        ]
    )

    assert set(paths) == {"EUR", "USD"}  # the fund contributes no cash row
    assert list(paths["EUR"]) == [_D("900"), _D("1000")]
    assert paths["EUR"].index[0] == pd.Timestamp("2026-03-31")


def test_cash_position_without_a_statement_contributes_no_path() -> None:
    """No statement, no balance — not an empty path and not a zero."""
    paths = build_actual_cash_paths(
        [(_investment(name="EUR Cash", investment_type="cash"), [], [])]
    )
    assert paths == {}


def test_two_cash_positions_in_one_currency_are_refused() -> None:
    """The reader refuses where a silent overwrite would drop real money."""
    with pytest.raises(DuplicateCashPositionError):
        build_actual_cash_paths(
            [
                (
                    _investment(name="EUR Cash A", investment_type="cash"),
                    [date(2026, 6, 30)],
                    [_D("100")],
                ),
                (
                    _investment(name="EUR Cash B", investment_type="cash"),
                    [date(2026, 6, 30)],
                    [_D("200")],
                ),
            ]
        )


class _FakeInvestmentRepository:
    def __init__(self, investments: list[InvestmentDTO]) -> None:
        self._investments = investments

    async def list_active(self) -> list[InvestmentDTO]:
        return list(self._investments)


class _FakeNavRepository:
    def __init__(self, navs: list[InvestmentNavDTO]) -> None:
        self._navs = navs

    async def list_by_investments_and_kind(
        self, investment_ids: list[UUID], nav_kind: str
    ) -> dict[UUID, list[InvestmentNavDTO]]:
        grouped: dict[UUID, list[InvestmentNavDTO]] = {i: [] for i in investment_ids}
        for nav in self._navs:
            if nav.investment_id in grouped and nav.nav_kind == nav_kind:
                grouped[nav.investment_id].append(nav)
        for rows in grouped.values():
            rows.sort(key=lambda n: n.as_of_date)
        return grouped


class _FakeCashflowRepository:
    async def list_by_investments_and_kind(
        self, investment_ids: list[UUID], flow_kind: str
    ) -> dict[UUID, list[object]]:
        return {i: [] for i in investment_ids}


class _FakeTenantRepository:
    async def get_current_functional_currency(self) -> str:
        return "EUR"


class _FakeFxRateRepository:
    """Records every read, so the zero-read guarantee can be asserted."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def load_rates_frame(self, currencies: list[str]) -> pd.DataFrame:
        self.calls.append(list(currencies))
        return pd.DataFrame(
            columns=[
                "as_of_date",
                "currency",
                "rate_to_reference",
                "reference_currency",
            ]
        )


@pytest.mark.asyncio
async def test_loader_composes_frames_history_and_a_zero_read_converter() -> None:
    """The loader reads; it does not compute — and it reads no FX row here.

    The book holds a USD *fund* but only EUR cash. The timeline converts cash
    and nothing else, so the converter is built over the cash currencies alone
    and the ADR-0099 §3 zero-read fast path holds.
    """
    fund = _investment(name="Fund I", currency="USD")
    eur_cash = _investment(name="EUR Cash", investment_type="cash")
    fx_rates = _FakeFxRateRepository()

    inputs = await load_cash_flow_planning_inputs(
        investments=_FakeInvestmentRepository([fund, eur_cash]),
        navs=_FakeNavRepository(
            [
                _nav(fund, "2026-06-30", "5000"),
                _nav(eur_cash, "2026-03-31", "900"),
                _nav(eur_cash, "2026-06-30", "1000"),
            ]
        ),
        cashflows=_FakeCashflowRepository(),
        tenants=_FakeTenantRepository(),
        fx_rates=fx_rates,
        periodisation=Periodisation.QUARTERLY,
    )

    assert inputs.baseline.t0 == _T0
    assert list(inputs.actual_cash) == ["EUR"]
    assert list(inputs.actual_cash["EUR"]) == [_D("900"), _D("1000")]
    assert inputs.converter.is_identity
    assert fx_rates.calls == []

    # And it feeds the pure core without further arrangement.
    timeline = build_cash_flow_timeline(
        frames=inputs.baseline,
        actual_cash=inputs.actual_cash,
        converter=inputs.converter,
        horizon_quarters=4,
    )
    assert timeline.seam_index == 2
    assert timeline.total[1] == _D("1000")

    # And an `fx_shock` in the parameter set does not make it read one either
    # (ADR-0104 §3 over ADR-0099 §3). The converter reaches the seam already
    # built, so a scenario is *structurally* unable to widen the read: the shock
    # folds through the identity converter as a no-op, and the zero-read
    # guarantee holds under a scenario exactly as it does under a baseline.
    # This is the property a shock-aware *loader* would have quietly broken.
    result = project_cash_flow_planning(
        baseline=inputs.baseline,
        overlay=(FxShock(currency="USD", magnitude=_D("-10")),),
        actual_cash=inputs.actual_cash,
        converter=inputs.converter,
        horizon_quarters=4,
    )
    assert result.scenario == result.baseline
    assert fx_rates.calls == []
