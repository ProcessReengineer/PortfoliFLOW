# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Executor unit tests: the error paths, the edges, and the arithmetic.

The four ADR-0104 §5 invariants are anchored in
``tests/regression/test_overlay_transformation_anchors.py``. This module
covers what an anchor does not: every way a well-formed transformation can be
refused by the frames, the boundaries of the factor range, the determinism of
the re-pacing arithmetic, and the edges (a flow exactly at the seam, an
investment with no plan NAV path at all).

Pure — no database, no fixtures. Every frame is built in-test.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest

from services.investments.archetype import Archetype
from services.overlay import (
    EXECUTABLE_KINDS,
    CurrencyMismatchError,
    ExecutorNotRegisteredError,
    FxShock,
    HistoricTradeDateError,
    InsertTransaction,
    MarketShock,
    MissingCashPathError,
    NotRepaceableError,
    PlanFlow,
    PlanFrames,
    PlanInvestment,
    RepaceFlows,
    TransformationKind,
    UnderivableConsiderationError,
    UnknownInvestmentError,
    apply_overlay,
)
from services.overlay.pipeline import _EXECUTORS

_T0 = date(2026, 6, 30)
_PE = UUID("11111111-1111-1111-1111-111111111111")  # private equity, EUR
_EQ = UUID("22222222-2222-2222-2222-222222222222")  # listed equity, EUR
_ABSENT = UUID("99999999-9999-9999-9999-999999999999")

_D = Decimal


def _path(*points: tuple[str, str]) -> pd.Series:
    """A Decimal balance path from ``(iso date, value)`` pairs."""
    return pd.Series(
        [_D(value) for _, value in points],
        index=pd.to_datetime([day for day, _ in points]),
    )


def _frames(
    *,
    plan_flows: tuple[PlanFlow, ...] = (),
    value_paths: dict[UUID, pd.Series] | None = None,
    cash_paths: dict[str, pd.Series] | None = None,
) -> PlanFrames:
    """Baseline frames: one capital-account fund, one listed holding, EUR cash."""
    default_value = {
        _PE: _path(("2026-06-30", "200"), ("2026-12-31", "300")),
        _EQ: _path(("2026-06-30", "500"), ("2026-12-31", "550")),
    }
    default_cash = {"EUR": _path(("2026-06-30", "1000"), ("2026-12-31", "900"))}
    return PlanFrames(
        t0=_T0,
        value_paths=default_value if value_paths is None else value_paths,
        cash_paths=default_cash if cash_paths is None else cash_paths,
        plan_flows=plan_flows,
        investments={
            _PE: PlanInvestment(_PE, "EUR", "private_equity"),
            _EQ: PlanInvestment(_EQ, "EUR", "listed_equity"),
        },
    )


def _buy(
    *,
    investment_id: UUID = _PE,
    trade_date: date = date(2026, 9, 30),
    units: Decimal = _D("10"),
    price_per_unit: Decimal | None = _D("11"),
    consideration: Decimal | None = None,
    currency: str = "EUR",
) -> InsertTransaction:
    """A hypothetical buy, with every field overridable."""
    return InsertTransaction(
        investment_id=investment_id,
        txn_type="buy",
        trade_date=trade_date,
        units=units,
        price_per_unit=price_per_unit,
        consideration=consideration,
        currency=currency,
    )


# --------------------------------------------------------------------------
# The registry (ADR-0104 §2 — the discriminator is closed)
# --------------------------------------------------------------------------


def test_the_registry_holds_exactly_the_executable_kinds() -> None:
    """``fx_shock`` stays unregistered until its seam exists (S34.2).

    The registry and :data:`EXECUTABLE_KINDS` must agree — the constant is what
    the rest of the codebase reads, and a constant that lied about the registry
    would be worse than no constant.
    """
    assert set(_EXECUTORS) == set(EXECUTABLE_KINDS)
    assert set(_EXECUTORS) == {
        TransformationKind.INSERT_TRANSACTION,
        TransformationKind.REPACE_FLOWS,
        TransformationKind.MARKET_SHOCK,
    }


# --------------------------------------------------------------------------
# insert_transaction — the refusals
# --------------------------------------------------------------------------


def test_insert_on_an_unknown_investment_is_refused() -> None:
    """A scenario shared after the book moved on must say so."""
    with pytest.raises(UnknownInvestmentError) as excinfo:
        apply_overlay(_frames(), (_buy(investment_id=_ABSENT),))
    assert str(_ABSENT) in str(excinfo.value)


def test_insert_settling_in_a_currency_without_a_cash_path_is_refused() -> None:
    """A scenario cannot invent a balance nobody funded (ADR-0103 §6)."""
    frames = _frames(
        value_paths={_PE: _path(("2026-06-30", "200"))},
        cash_paths={"USD": _path(("2026-06-30", "1000"))},
    )
    with pytest.raises(MissingCashPathError) as excinfo:
        apply_overlay(frames, (_buy(),))
    assert "EUR" in str(excinfo.value)


def test_insert_settling_in_a_foreign_currency_is_refused() -> None:
    """The overlay never converts (ADR-0104 §3, N2).

    A EUR-denominated fund settled from a USD cash account would need the two
    legs squared across an FX rate. Executing it as a silent 1:1 is the one
    outcome ADR-0099 forbids above all others, so it is refused instead.
    """
    frames = _frames(
        cash_paths={
            "EUR": _path(("2026-06-30", "1000")),
            "USD": _path(("2026-06-30", "1000")),
        }
    )
    with pytest.raises(CurrencyMismatchError) as excinfo:
        apply_overlay(frames, (_buy(currency="USD"),))
    message = str(excinfo.value)
    assert "USD" in message and "EUR" in message


@pytest.mark.parametrize(
    "trade_date",
    [
        pytest.param(_T0, id="at-the-seam"),
        pytest.param(_T0 - timedelta(days=1), id="one-day-before-the-seam"),
        pytest.param(date(2020, 1, 1), id="deep-in-realised-history"),
    ],
)
def test_insert_at_or_before_the_seam_is_refused(trade_date: date) -> None:
    """Overlays never touch actuals (ADR-0104 §5)."""
    with pytest.raises(HistoricTradeDateError) as excinfo:
        apply_overlay(_frames(), (_buy(trade_date=trade_date),))
    assert trade_date.isoformat() in str(excinfo.value)


def test_insert_the_day_after_the_seam_is_accepted() -> None:
    """The seam is the boundary, and it is exclusive on the plan side."""
    scenario = apply_overlay(_frames(), (_buy(trade_date=_T0 + timedelta(days=1)),))
    stepped = scenario.value_paths[_PE]
    assert stepped.loc[pd.Timestamp(_T0 + timedelta(days=1))] == _D("310")


def test_insert_without_a_consideration_or_a_price_is_refused() -> None:
    """No cash effect to settle, and no value step to take."""
    with pytest.raises(UnderivableConsiderationError) as excinfo:
        apply_overlay(_frames(), (_buy(price_per_unit=None, consideration=None),))
    assert str(_PE) in str(excinfo.value)


# --------------------------------------------------------------------------
# insert_transaction — the effect
# --------------------------------------------------------------------------


def test_a_stated_consideration_wins_over_the_derived_one() -> None:
    """``consideration`` is authoritative where the form supplied it."""
    scenario = apply_overlay(
        _frames(),
        (_buy(units=_D("10"), price_per_unit=_D("11"), consideration=_D("120")),),
    )
    # 200 + 120, not 200 + 110.
    assert scenario.value_paths[_PE].loc[pd.Timestamp("2026-09-30")] == _D("320")
    assert scenario.cash_paths["EUR"].loc[pd.Timestamp("2026-09-30")] == _D("880")


def test_a_sell_moves_value_out_and_cash_in() -> None:
    """Units are signed (ADR-0097 §2), so the consideration is too."""
    sell = InsertTransaction(
        investment_id=_PE,
        txn_type="sell",
        trade_date=date(2026, 9, 30),
        units=_D("-10"),
        price_per_unit=_D("11"),
        consideration=None,
        currency="EUR",
    )
    scenario = apply_overlay(_frames(), (sell,))
    assert scenario.value_paths[_PE].loc[pd.Timestamp("2026-09-30")] == _D("90")
    assert scenario.cash_paths["EUR"].loc[pd.Timestamp("2026-09-30")] == _D("1110")


def test_insert_on_an_investment_with_no_plan_nav_path_creates_one() -> None:
    """A plan world where the investment exists but carries no plan NAV.

    Legal — the contributing-nothing member of the universe
    (:func:`services.investments.aum.build_nav_series`). Its balance before the
    trade is zero, and the trade creates the path.
    """
    frames = _frames(value_paths={_EQ: _path(("2026-06-30", "500"))})
    scenario = apply_overlay(frames, (_buy(),))

    created = scenario.value_paths[_PE]
    assert list(created.index) == [pd.Timestamp("2026-09-30")]
    assert created.loc[pd.Timestamp("2026-09-30")] == _D("110")
    assert scenario.cash_paths["EUR"].loc[pd.Timestamp("2026-09-30")] == _D("890")


def test_insert_appends_no_plan_flow_and_leaves_the_others_alone() -> None:
    """The frames carry the *effect*; the parameter set carries the trade."""
    flows = (PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call"),)
    frames = _frames(plan_flows=flows)
    scenario = apply_overlay(frames, (_buy(),))

    assert scenario.plan_flows == flows
    assert scenario.value_paths[_EQ] is frames.value_paths[_EQ]


# --------------------------------------------------------------------------
# repace_flows — the refusals
# --------------------------------------------------------------------------


def test_repace_on_an_unknown_investment_is_refused() -> None:
    """Same seam, same error as the inserted transaction."""
    with pytest.raises(UnknownInvestmentError):
        apply_overlay(_frames(), (RepaceFlows(investment_id=_ABSENT, factor=_D("1.5")),))


@pytest.mark.parametrize(
    ("investment_type", "archetype"),
    [
        ("listed_equity", "total_return_equity"),
        ("listed_bonds", "fixed_income"),
        ("cash", "nav_only"),
        ("other", "nav_only"),
    ],
)
def test_repace_off_a_capital_account_is_refused(investment_type: str, archetype: str) -> None:
    """Only a manager-plan drawdown profile can be re-paced (ADR-0104 §2).

    Dispatch is through ``resolve_archetype`` — the error names the archetype
    it resolved to, never the ``investment_type`` it was handed.
    """
    frames = PlanFrames(
        t0=_T0,
        value_paths={_EQ: _path(("2026-06-30", "500"))},
        cash_paths={"EUR": _path(("2026-06-30", "1000"))},
        plan_flows=(),
        investments={_EQ: PlanInvestment(_EQ, "EUR", investment_type)},
    )
    with pytest.raises(NotRepaceableError) as excinfo:
        apply_overlay(frames, (RepaceFlows(investment_id=_EQ, factor=_D("1.5")),))
    assert archetype in str(excinfo.value)


def test_repace_of_a_flow_with_no_cash_path_is_refused() -> None:
    """A re-paced flow cannot settle against a balance that does not exist."""
    frames = _frames(
        plan_flows=(PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "USD", "capital_call"),)
    )
    with pytest.raises(MissingCashPathError) as excinfo:
        apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=_D("2.0")),))
    assert "USD" in str(excinfo.value)


# --------------------------------------------------------------------------
# repace_flows — scope and effect
# --------------------------------------------------------------------------


def test_a_flow_exactly_at_the_seam_is_not_remaining_and_does_not_move() -> None:
    """ "Remaining" is *strictly* after ``t0`` — the seam belongs to history."""
    at_seam = PlanFlow(_PE, _T0, _D("-100"), "EUR", "capital_call")
    after_seam = PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call")
    frames = _frames(plan_flows=(at_seam, after_seam))

    scenario = apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=_D("2.0")),))

    assert scenario.plan_flows[0] == at_seam
    assert scenario.plan_flows[1].as_of_date == date(2027, 7, 3)
    assert scenario.plan_flows[1].amount == after_seam.amount


@pytest.mark.parametrize(
    "factor",
    [pytest.param(_D("0.5"), id="fastest"), pytest.param(_D("2.0"), id="slowest")],
)
def test_the_factor_boundaries_execute(factor: Decimal) -> None:
    """Both bounds are inclusive and executable (ADR-0104 §2)."""
    offset = 184  # 2026-06-30 → 2026-12-31
    frames = _frames(
        plan_flows=(PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call"),)
    )
    scenario = apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=factor),))
    expected = _T0 + timedelta(days=int(offset * factor))

    assert scenario.plan_flows[0].as_of_date == expected
    assert scenario.plan_flows[0].as_of_date > _T0


def test_re_pacing_moves_capital_in_time_but_never_resizes_it() -> None:
    """The amount is invariant; only the date moves."""
    frames = _frames(
        plan_flows=(
            PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call"),
            PlanFlow(_PE, date(2027, 6, 30), _D("75"), "EUR", "distribution"),
        )
    )
    scenario = apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=_D("0.5")),))

    assert [flow.amount for flow in scenario.plan_flows] == [_D("-200"), _D("75")]
    assert [flow.flow_type for flow in scenario.plan_flows] == [
        "capital_call",
        "distribution",
    ]
    # 184 → 92 days, and 365 → 183 days (both ×0.5, exact).
    assert scenario.plan_flows[0].as_of_date == date(2026, 9, 30)
    assert scenario.plan_flows[1].as_of_date == date(2026, 12, 30)


def test_the_cash_path_follows_a_re_paced_flow() -> None:
    """Lift the flow off its old date, set it down on the new one."""
    frames = _frames(
        plan_flows=(PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call"),)
    )
    scenario = apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=_D("0.5")),))
    cash = scenario.cash_paths["EUR"]

    # The call is lifted off 2026-12-31 (+200 back onto the balance) and set
    # down on 2026-09-30 (−200 from there onward).
    assert cash.loc[pd.Timestamp("2026-06-30")] == _D("1000")
    assert cash.loc[pd.Timestamp("2026-09-30")] == _D("800")
    assert cash.loc[pd.Timestamp("2026-12-31")] == _D("900")


def test_re_pacing_leaves_the_value_paths_untouched() -> None:
    """A v1 simplification, pinned: ADR-0104 §2 scopes the kind to the flows."""
    frames = _frames(
        plan_flows=(PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call"),)
    )
    scenario = apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=_D("2.0")),))
    assert scenario.value_paths[_PE] is frames.value_paths[_PE]
    assert scenario.value_paths[_EQ] is frames.value_paths[_EQ]


# --------------------------------------------------------------------------
# The re-pacing arithmetic (ADR-0104 §2 — deterministic, Decimal, half-up)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_days", "factor", "expected_days"),
    [
        # Exact halvings and doublings.
        (10, "0.5", 5),
        (100, "2.0", 200),
        (1, "2.0", 2),
        # Half-way cases: ROUND_HALF_UP, never banker's rounding.
        (11, "0.5", 6),  # 5.5 → 6
        (9, "0.5", 5),  # 4.5 → 5
        (3, "0.5", 2),  # 1.5 → 2
        (7, "1.5", 11),  # 10.5 → 11
        (5, "1.5", 8),  # 7.5 → 8
        # The nearest a compression can come to the seam — and it stays after.
        (1, "0.5", 1),  # 0.5 → 1
        # Integer-day identity at the mid-position, for every offset.
        (7, "1.0", 7),
        (184, "1.0", 184),
        # Non-half fractions.
        (91, "1.25", 114),  # 113.75 → 114
        (365, "0.75", 274),  # 273.75 → 274
    ],
)
def test_the_re_pacing_date_arithmetic_is_deterministic(
    offset_days: int, factor: str, expected_days: int
) -> None:
    """The reference table for the re-pacing arithmetic.

    ``new_date = t0 + round_half_up(factor × (as_of_date − t0).days)``, the
    product taken in :class:`~decimal.Decimal`. Driven through the executor
    rather than the private helper, so the table pins the *seam's* behaviour.
    """
    flow_date = _T0 + timedelta(days=offset_days)
    frames = _frames(plan_flows=(PlanFlow(_PE, flow_date, _D("-100"), "EUR", "capital_call"),))
    scenario = apply_overlay(frames, (RepaceFlows(investment_id=_PE, factor=_D(factor)),))

    assert scenario.plan_flows[0].as_of_date == _T0 + timedelta(days=expected_days)
    assert scenario.plan_flows[0].as_of_date > _T0


# --------------------------------------------------------------------------
# market_shock — the archetype scope, the seam, and the arithmetic
# --------------------------------------------------------------------------


def _shock(
    *,
    archetype: Archetype = Archetype.CAPITAL_ACCOUNT,
    magnitude: Decimal = _D("-20"),
) -> MarketShock:
    """A market shock, with both fields overridable."""
    return MarketShock(archetype=archetype, magnitude=magnitude)


def test_a_market_shock_scales_only_the_targeted_archetype() -> None:
    """Dispatch is by archetype, never by investment type literal.

    ``_PE`` is ``private_equity`` → CAPITAL_ACCOUNT and is shocked; ``_EQ`` is
    ``listed_equity`` → TOTAL_RETURN_EQUITY and is not. Neither type string
    appears in the executor: ``resolve_archetype`` routes both (ADR-0104 §2,
    the ADR-0103 §8 type-blindness rule).
    """
    baseline = _frames()
    scenario = apply_overlay(baseline, (_shock(),))

    # −20 % → × 0.8, on the post-seam point only.
    assert scenario.value_paths[_PE].loc[pd.Timestamp("2026-12-31")] == _D("240")
    # The listed holding resolves elsewhere and is untouched — the same object.
    assert scenario.value_paths[_EQ] is baseline.value_paths[_EQ]


def test_a_market_shock_leaves_the_seam_observation_identical() -> None:
    """The value *at* t₀ is the last actual, and no overlay touches an actual.

    ADR-0104 §5 is binding on this, and §2's "immediate at t₀" is a statement
    about the timing *regime* — full magnitude at once, no ramp — not a licence
    to re-mark a realised valuation. The first **plan** point carries the whole
    shock, which is what "immediate" buys.
    """
    baseline = _frames()
    scenario = apply_overlay(baseline, (_shock(magnitude=_D("-50")),))

    seam = pd.Timestamp(_T0)
    assert baseline.value_paths[_PE].loc[seam] == _D("200")
    assert scenario.value_paths[_PE].loc[seam] == _D("200")
    # And the plan point beyond it took the full 50 %, not half of it.
    assert scenario.value_paths[_PE].loc[pd.Timestamp("2026-12-31")] == _D("150")


def test_a_market_shock_is_multiplicative_not_additive() -> None:
    """A per-cent magnitude scales each level; it does not displace all of them.

    Two post-seam points at different levels take the same *ratio* and therefore
    different euro amounts. An additive reading would move both by one constant,
    which is not what a level shift is — and ADR-0104 §2 states the magnitude
    "in %", which has no additive reading on a NAV at all.
    """
    path = _path(
        ("2026-06-30", "100"),
        ("2026-09-30", "200"),
        ("2026-12-31", "400"),
    )
    baseline = _frames(value_paths={_PE: path, _EQ: _path(("2026-06-30", "1"))})
    scenario = apply_overlay(baseline, (_shock(magnitude=_D("-25")),))

    shocked = scenario.value_paths[_PE]
    assert shocked.loc[pd.Timestamp("2026-06-30")] == _D("100")  # the seam
    assert shocked.loc[pd.Timestamp("2026-09-30")] == _D("150")  # −50, not −25
    assert shocked.loc[pd.Timestamp("2026-12-31")] == _D("300")  # −100
    # Decimal throughout — a money level never round-trips through a float.
    assert all(isinstance(level, Decimal) for level in shocked)


def test_a_market_shock_touches_no_cash_path_and_no_flow() -> None:
    """A shock revalues; it does not settle. Cash and flows are the same objects.

    This is why the shock moves AUM where an inserted transaction does not: the
    value leg moves and no cash leg answers it.
    """
    flows = (PlanFlow(_PE, date(2026, 12, 31), _D("-100"), "EUR", "capital_call"),)
    baseline = _frames(plan_flows=flows)
    scenario = apply_overlay(baseline, (_shock(),))

    assert scenario.plan_flows == baseline.plan_flows
    assert scenario.cash_paths["EUR"] is baseline.cash_paths["EUR"]


def test_a_shock_of_an_archetype_the_book_holds_nothing_in_is_the_identity() -> None:
    """Revaluing nothing changes nothing — computed, not skipped.

    Distinct from the silent-baseline failure the pipeline guards against: that
    one is "not computed" wearing the face of "no impact". This *is* computed,
    and the impact of shocking an empty archetype is nil.
    """
    baseline = _frames()
    scenario = apply_overlay(baseline, (_shock(archetype=Archetype.NAV_ONLY),))
    assert scenario is baseline


def test_a_nought_per_cent_shock_is_the_identity() -> None:
    """The shock that says nothing returns the frames themselves, exactly."""
    baseline = _frames()
    assert apply_overlay(baseline, (_shock(magnitude=_D("0")),)) is baseline


def test_a_market_shock_can_mark_up() -> None:
    """A positive magnitude is a mark-up — the kind is not a stress test only."""
    baseline = _frames()
    scenario = apply_overlay(baseline, (_shock(magnitude=_D("10")),))
    assert scenario.value_paths[_PE].loc[pd.Timestamp("2026-12-31")] == _D("330")


def test_a_market_shock_skips_an_investment_with_no_plan_value_path() -> None:
    """A holding the plan world carries no value path for is shocked to nothing.

    Not an error: an investment contributing no plan NAV has no level to
    revalue, and inventing a path to shock would be inventing a holding.
    """
    baseline = _frames(value_paths={_EQ: _path(("2026-12-31", "500"))})
    scenario = apply_overlay(baseline, (_shock(),))  # targets _PE's archetype

    assert scenario is baseline
    assert _PE not in scenario.value_paths


def test_an_fx_shock_has_no_executor_and_says_so() -> None:
    """S34.2 owns the conversion-seam hook; until then the fold refuses (§4).

    The parser *accepts* an ``fx_shock`` — it is a well-formed value — so this
    is the seam that must fail, and it must fail loudly: a scenario that quietly
    equalled its baseline would tell the operator "no impact" where the truth is
    "not computed".
    """
    with pytest.raises(ExecutorNotRegisteredError) as excinfo:
        apply_overlay(_frames(), (FxShock(currency="USD", magnitude=_D("-10")),))
    assert "fx_shock" in str(excinfo.value)


def test_a_market_shock_never_mutates_the_frames_it_is_handed() -> None:
    """The purity contract, on the kind that rewrites the most (ADR-0104 §2)."""
    baseline = _frames()
    before = baseline.value_paths[_PE].copy(deep=True)

    apply_overlay(baseline, (_shock(magnitude=_D("-40")),))

    pd.testing.assert_series_equal(baseline.value_paths[_PE], before, check_exact=True)
