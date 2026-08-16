# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression anchors: the invariants of the overlay (ADR-0104 §2/§5).

These are not unit tests of the executors — those live in
``tests/services/overlay/test_executors.py``. These are the properties the
Planning Desk's *credibility* rests on, and ADR-0104 §5 names them as
regression-testable:

1. **AUM invariance under ``insert_transaction``.** A hypothetical transaction
   re-allocates the plan world; it does not fund it. Value ``+C`` and cash
   ``−C``, same currency, same date — so the sum over a currency is unchanged
   at every point. An overlay that could quietly grow the portfolio would make
   every scenario figure unreconcilable against the book.
2. **Mid-position bit-identity.** A re-pacing at factor 1.0 *is* the manager
   plan. Not "within a tolerance of" — bit-identical (``check_exact=True``),
   or the neutral position of a slider silently perturbs the plan it claims to
   reproduce.
3. **Identical history.** Left of the plan/actual seam, baseline and scenario
   are the same path by definition. An overlay only ever touches the future.
4. **The investor-flow exemption** (ADR-0103 §5, binding). No transformation
   creates, deletes, re-paces, or re-scales an ``investor_flow``: a scenario
   asks what if the portfolio's *investments* behaved differently, and it has
   no standing to invent capital the investor never committed.
5. **AUM *non*-invariance under ``market_shock``** (S34.1). The counterpart to
   anchor 1, not an exception to it. A shock **revalues** where a trade
   *re-allocates*: value paths move and no cash leg answers them, so ``Σ NAV``
   moves too. That is the transformation's entire content — a scenario in which
   private markets fall 20 % and the portfolio total does not budge would be
   describing nothing — and it is anchored so that a future change which
   silently restored invariance here (a "correction" of the apparent leak)
   would fail loudly. Anchors 1 and 5 are deliberately separate tests over
   separate kinds; neither is folded into the other.

Plus a **no-mutation anchor** — the executors' purity contract (ADR-0104 §2)
asserted in practice rather than only at source level: the baseline frames
survive an application untouched.

The frames are synthetic and built in-test: **pure, DB-free, runnable
anywhere**. They deliberately do not reuse the fixtures under
``tests/services/``, whose autouse DB fixture skips when Postgres is down —
an anchor that can be skipped is not an anchor.
"""

from __future__ import annotations

import operator
from datetime import date
from decimal import Decimal
from functools import reduce
from uuid import UUID

import pandas as pd
from pandas.testing import assert_series_equal

from services.investments.archetype import Archetype
from services.investments.flow_type_invariants import OVERLAY_EXEMPT_FLOW_TYPES
from services.overlay import (
    FACTOR_NEUTRAL,
    InsertTransaction,
    MarketShock,
    Overlay,
    PlanFlow,
    PlanFrames,
    PlanInvestment,
    RepaceFlows,
    apply_overlay,
)

#: The plan/actual seam every anchor is measured about (ADR-0060).
T0: date = date(2026, 6, 30)

_PE = UUID("11111111-1111-1111-1111-111111111111")  # private equity, EUR
_RE = UUID("22222222-2222-2222-2222-222222222222")  # real estate, EUR
_EQ = UUID("33333333-3333-3333-3333-333333333333")  # listed equity, USD

_D = Decimal


def _index() -> pd.DatetimeIndex:
    """A quarterly grid straddling the seam — history left, plan right."""
    return pd.to_datetime(
        [
            "2025-12-31",
            "2026-03-31",
            "2026-06-30",  # the seam
            "2026-09-30",
            "2026-12-31",
            "2027-03-31",
            "2027-06-30",
            "2027-12-31",
        ]
    )


def _path(*values: str) -> pd.Series:
    """A Decimal-valued balance path on the quarterly grid."""
    return pd.Series([_D(value) for value in values], index=_index())


def _frames() -> PlanFrames:
    """Baseline frames: two currencies, three investments, mixed flow types."""
    return PlanFrames(
        t0=T0,
        value_paths={
            _PE: _path("100", "150", "200", "260", "320", "380", "440", "500"),
            _RE: _path("80", "80", "90", "90", "95", "100", "105", "110"),
            _EQ: _path("500", "520", "540", "560", "580", "600", "620", "640"),
        },
        cash_paths={
            "EUR": _path("2000", "1900", "1800", "1750", "1550", "1500", "1450", "1400"),
            "USD": _path("900", "900", "880", "880", "860", "860", "840", "840"),
        },
        plan_flows=(
            # History — outside every "remaining" profile, immovable.
            PlanFlow(_PE, date(2026, 3, 31), _D("-50"), "EUR", "capital_call"),
            # The remaining profile of the capital-account fund.
            PlanFlow(_PE, date(2026, 7, 31), _D("-10"), "EUR", "fee"),
            PlanFlow(_PE, date(2026, 12, 31), _D("-200"), "EUR", "capital_call"),
            PlanFlow(_PE, date(2027, 6, 30), _D("75"), "EUR", "distribution"),
            # The investor's own flows — exempt from every transformation.
            PlanFlow(_PE, date(2026, 9, 30), _D("1000"), "EUR", "investor_flow"),
            PlanFlow(_PE, date(2027, 3, 31), _D("-400"), "EUR", "investor_flow"),
            # Other investments' flows — never in this transformation's scope.
            PlanFlow(_RE, date(2026, 12, 31), _D("-30"), "EUR", "capital_call"),
            PlanFlow(_EQ, date(2026, 12, 31), _D("12"), "USD", "dividend"),
        ),
        investments={
            _PE: PlanInvestment(_PE, "EUR", "private_equity"),
            _RE: PlanInvestment(_RE, "EUR", "real_estate"),
            _EQ: PlanInvestment(_EQ, "USD", "listed_equity"),
        },
    )


def _union_index(*frames: PlanFrames) -> pd.DatetimeIndex:
    """Every date any path in any of the frames is observed on."""
    index = pd.DatetimeIndex([])
    for one in frames:
        for path in (*one.value_paths.values(), *one.cash_paths.values()):
            index = index.union(path.index)
    return index


def _levels(path: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """The balance in force at every date of ``index``.

    A balance path is a step function: the level at a date is the latest
    observation at or before it, and zero before the path begins. Reading the
    paths this way is what lets AUM invariance be checked on the *union* grid,
    where an inserted transaction has added a date the baseline never carried.
    """
    return path.reindex(index, method="ffill").fillna(_D(0))


def _currency_total(frames: PlanFrames, currency: str, index: pd.DatetimeIndex) -> pd.Series:
    """Σ value paths of that currency's investments + its cash path.

    The per-currency AUM of the plan world (ADR-0103 §2: AUM is Σ NAV, cash
    included) — stated in position currency, since the overlay never converts
    (ADR-0104 §3, N2).
    """
    paths = [
        _levels(frames.value_paths[investment_id], index)
        for investment_id, investment in frames.investments.items()
        if investment.currency == currency and investment_id in frames.value_paths
    ]
    paths.append(_levels(frames.cash_paths[currency], index))
    return reduce(operator.add, paths)


def _flows_of(frames: PlanFrames, investment_id: UUID) -> tuple[PlanFlow, ...]:
    """The plan flows of one investment, in tuple order."""
    return tuple(flow for flow in frames.plan_flows if flow.investment_id == investment_id)


# --------------------------------------------------------------------------
# Anchor 1 — AUM invariance (ADR-0104 §2)
# --------------------------------------------------------------------------


def test_inserted_transactions_leave_aum_invariant_per_currency() -> None:
    """Buys and sells re-allocate the plan world; they never fund it.

    Four transactions across both currencies and both derivation paths
    (``consideration`` stated outright, and derived from
    ``units × price_per_unit``), including an off-grid trade date that adds an
    index point the baseline never carried.
    """
    baseline = _frames()
    overlay: Overlay = (
        # A buy with the consideration derived: 10 × 11 = +110.
        InsertTransaction(
            investment_id=_PE,
            txn_type="buy",
            trade_date=date(2026, 9, 30),
            units=_D("10"),
            price_per_unit=_D("11"),
            consideration=None,
            currency="EUR",
        ),
        # A sell with the consideration stated — signed negative (ADR-0097 §2).
        InsertTransaction(
            investment_id=_EQ,
            txn_type="sell",
            trade_date=date(2026, 11, 15),  # off-grid: inserts an index point
            units=_D("-20"),
            price_per_unit=_D("25"),
            consideration=_D("-500"),
            currency="USD",
        ),
        # A buy in the second EUR investment, derived, on the far side of the
        # grid — so the EUR total is a sum over two moving value paths.
        InsertTransaction(
            investment_id=_RE,
            txn_type="buy",
            trade_date=date(2027, 3, 31),
            units=_D("4"),
            price_per_unit=_D("62.5"),
            consideration=None,
            currency="EUR",
        ),
        # A second trade on an investment already traded — the fold applies to
        # the output of the previous executor, not to the baseline.
        InsertTransaction(
            investment_id=_PE,
            txn_type="sell",
            trade_date=date(2027, 8, 15),
            units=_D("-3"),
            price_per_unit=None,
            consideration=_D("-90"),
            currency="EUR",
        ),
    )

    scenario = apply_overlay(baseline, overlay)
    index = _union_index(baseline, scenario)

    for currency in ("EUR", "USD"):
        assert_series_equal(
            _currency_total(scenario, currency, index),
            _currency_total(baseline, currency, index),
            check_exact=True,
            obj=f"{currency} plan AUM",
        )


# --------------------------------------------------------------------------
# Anchor 2 — mid-position bit-identity (ADR-0104 §2)
# --------------------------------------------------------------------------


def test_repacing_at_the_neutral_factor_reproduces_the_plan_exactly() -> None:
    """Factor 1.0 is the manager plan — bit-identical, not merely close."""
    baseline = _frames()
    scenario = apply_overlay(baseline, (RepaceFlows(investment_id=_PE, factor=FACTOR_NEUTRAL),))

    assert scenario.plan_flows == baseline.plan_flows

    for currency, path in baseline.cash_paths.items():
        assert_series_equal(
            scenario.cash_paths[currency],
            path,
            check_exact=True,
            obj=f"{currency} cash path",
        )
    for investment_id, path in baseline.value_paths.items():
        assert_series_equal(
            scenario.value_paths[investment_id],
            path,
            check_exact=True,
            obj=f"value path {investment_id}",
        )


# --------------------------------------------------------------------------
# Anchor 3 — identical history (ADR-0104 §5)
# --------------------------------------------------------------------------


def test_a_mixed_overlay_leaves_realised_history_identical() -> None:
    """Everything at or before the seam is untouched by any transformation.

    The overlay mixes all three executable kinds, the ``market_shock``
    included — the kind with the widest reach, since it rewrites whole value
    paths rather than stepping them at one date. The seam observation is the
    last **actual** NAV, so the shock must stop strictly to the right of it:
    ADR-0104 §2's "timing v1 = immediate at t₀" fixes the timing *regime*, and
    is not a licence to re-mark a realised valuation (ADR-0104 §5).
    """
    baseline = _frames()
    scenario = apply_overlay(
        baseline,
        (
            InsertTransaction(
                investment_id=_PE,
                txn_type="buy",
                trade_date=date(2026, 9, 30),
                units=_D("10"),
                price_per_unit=_D("11"),
                consideration=None,
                currency="EUR",
            ),
            RepaceFlows(investment_id=_PE, factor=_D("0.5")),
            InsertTransaction(
                investment_id=_EQ,
                txn_type="sell",
                trade_date=date(2026, 7, 15),
                units=_D("-1"),
                price_per_unit=_D("30"),
                consideration=None,
                currency="USD",
            ),
            # Reaches _PE and _RE (both capital-account) — two of the three
            # value paths, in the currency the other transformations also move.
            MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=_D("-30")),
            # And the other archetype, so every value path in the fixture has
            # been rewritten by the time history is checked.
            MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=_D("15")),
        ),
    )
    seam = pd.Timestamp(T0)

    # The shock must have *done* something, or the anchor proves nothing about
    # it: an inert transformation trivially preserves history.
    assert not scenario.value_paths[_PE].equals(baseline.value_paths[_PE])
    assert not scenario.value_paths[_RE].equals(baseline.value_paths[_RE])
    assert not scenario.value_paths[_EQ].equals(baseline.value_paths[_EQ])

    for investment_id, path in baseline.value_paths.items():
        after = scenario.value_paths[investment_id]
        assert_series_equal(
            after[after.index <= seam],
            path[path.index <= seam],
            check_exact=True,
            obj=f"value path {investment_id} up to the seam",
        )
    for currency, path in baseline.cash_paths.items():
        after = scenario.cash_paths[currency]
        assert_series_equal(
            after[after.index <= seam],
            path[path.index <= seam],
            check_exact=True,
            obj=f"{currency} cash path up to the seam",
        )

    historic = tuple(flow for flow in baseline.plan_flows if flow.as_of_date <= T0)
    assert historic, "the fixture must carry at least one pre-seam plan flow"
    assert all(flow in scenario.plan_flows for flow in historic)


# --------------------------------------------------------------------------
# Anchor 4 — the investor-flow exemption (ADR-0103 §5)
# --------------------------------------------------------------------------


def test_repacing_never_touches_an_investor_flow() -> None:
    """Exempt flows keep their dates, amounts, and count; the rest move.

    The exempt set is imported from
    :mod:`services.investments.flow_type_invariants` — the single formulation
    (ADR-0103 §5). A test that restated it could not catch the invariant
    drifting from its definition.
    """
    baseline = _frames()
    scenario = apply_overlay(baseline, (RepaceFlows(investment_id=_PE, factor=_D("2.0")),))

    before = _flows_of(baseline, _PE)
    after = _flows_of(scenario, _PE)
    assert len(after) == len(before), "no flow may be created or deleted"

    exempt_before = [flow for flow in before if flow.flow_type in OVERLAY_EXEMPT_FLOW_TYPES]
    exempt_after = [flow for flow in after if flow.flow_type in OVERLAY_EXEMPT_FLOW_TYPES]
    assert exempt_before, "the fixture must carry post-seam investor flows"
    assert all(flow.as_of_date > T0 for flow in exempt_before), (
        "the exemption is only meaningful for flows a re-pacing could reach"
    )
    # Dates, amounts, count — nothing created, deleted, re-paced, or re-scaled.
    assert exempt_after == exempt_before

    moved = [
        flow
        for flow in before
        if flow.flow_type not in OVERLAY_EXEMPT_FLOW_TYPES and flow.as_of_date > T0
    ]
    assert moved, "the fixture must carry re-paceable post-seam flows"
    for flow in moved:
        assert flow not in after, (
            f"the {flow.flow_type} dated {flow.as_of_date} is portfolio "
            "behaviour and must have been re-paced"
        )

    # And the other investments' flows are outside the transformation's scope.
    assert _flows_of(scenario, _RE) == _flows_of(baseline, _RE)
    assert _flows_of(scenario, _EQ) == _flows_of(baseline, _EQ)


def test_a_market_shock_moves_no_flow_at_all_least_of_all_an_investor_flow() -> None:
    """A shock acts on value paths; flows are a different frame entirely.

    The exemption invariant (ADR-0103 §5) therefore holds **vacuously** for this
    kind rather than by enforcement — and that is exactly why it is asserted
    rather than assumed. A future shock variant that did reach flows (an ADR-E
    regime that re-scaled a drawdown profile, say) would otherwise inherit the
    claim without ever having earned it.

    The exempt set is imported from
    :mod:`services.investments.flow_type_invariants` — the single formulation.
    """
    baseline = _frames()
    scenario = apply_overlay(
        baseline,
        (
            MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=_D("-40")),
            MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=_D("-40")),
        ),
    )

    # Not one flow of any type moved — the tuple is identical, in order.
    assert scenario.plan_flows == baseline.plan_flows

    # Stated again against the imported exempt set, so the anchor fails for the
    # right reason if a shock ever learns to touch a flow.
    exempt = [flow for flow in baseline.plan_flows if flow.flow_type in OVERLAY_EXEMPT_FLOW_TYPES]
    assert exempt, "the fixture must carry investor flows"
    assert all(flow in scenario.plan_flows for flow in exempt)

    # The shock did land, though — on the value paths, where it belongs.
    assert not scenario.value_paths[_PE].equals(baseline.value_paths[_PE])


# --------------------------------------------------------------------------
# Anchor 5 — AUM *non*-invariance under market_shock (ADR-0104 §2, S34.1)
# --------------------------------------------------------------------------


def test_a_market_shock_moves_aum_because_that_is_what_it_is_for() -> None:
    """The counterpart to Anchor 1 — a separate anchor, not an exception to it.

    An ``insert_transaction`` re-allocates the plan world: value ``+C``, cash
    ``−C``, netting to nothing, so AUM is invariant *by construction*. A
    ``market_shock`` **revalues** it: the value paths of the targeted archetype
    move and no cash leg answers them, so ``Σ NAV`` (cash included, ADR-0103 §2)
    moves with them. The delta is not a leak in the transformation — it *is* the
    transformation, and a change that quietly restored invariance here, mistaking
    the one for the other, must fail this test rather than pass Anchor 1's.

    Asserted in direction **and** magnitude: a shock that moved AUM by the wrong
    amount, or in the wrong currency, would satisfy a mere "something changed".
    """
    baseline = _frames()
    magnitude = _D("-20")
    scenario = apply_overlay(
        baseline,
        (MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=magnitude),),
    )
    index = _union_index(baseline, scenario)
    seam = pd.Timestamp(T0)
    history, plan = index <= seam, index > seam

    before = _currency_total(baseline, "EUR", index)
    after = _currency_total(scenario, "EUR", index)

    # The shocked sleeve: the fixture's two capital-account funds, both EUR.
    # _EQ resolves to TOTAL_RETURN_EQUITY and is not in it.
    sleeve = _levels(baseline.value_paths[_PE], index) + _levels(baseline.value_paths[_RE], index)

    # Left of the seam and at it, AUM is untouched — the shock is not a licence
    # to restate a realised valuation (Anchor 3, from the AUM side).
    assert_series_equal(
        after[history],
        before[history],
        check_exact=True,
        obj="EUR plan AUM through the seam",
    )

    # Right of it, AUM moves by exactly the sleeve's share of the shock: cash is
    # not revalued, so only the targeted value paths carry the −20 %.
    assert_series_equal(
        after[plan],
        before[plan] + sleeve[plan] * (magnitude / _D("100")),
        check_exact=True,
        obj="EUR plan AUM after the shock",
    )

    # And it is a real, downward move at every plan point — not a rounding
    # artefact that the equality above would also have accepted.
    assert (after[plan] < before[plan]).all()

    # The USD side holds only listed equity: a capital-account shock is not its
    # business, and its AUM does not move. A shock that leaked across the
    # archetype scope would show up here.
    assert_series_equal(
        _currency_total(scenario, "USD", index),
        _currency_total(baseline, "USD", index),
        check_exact=True,
        obj="USD plan AUM",
    )


# --------------------------------------------------------------------------
# The purity contract, asserted in practice (ADR-0104 §2)
# --------------------------------------------------------------------------


def test_executors_never_mutate_the_frames_they_are_handed() -> None:
    """The baseline survives an application bit-identically.

    The source-level guard (``test_overlay_layer_pure.py``) proves the overlay
    cannot reach the book. This proves it does not reach *backwards* either:
    an executor that mutated its input would corrupt the baseline the scenario
    is measured against — and the Baseline/Scenario toggle renders both from
    the same frames (ADR-0104 §4).
    """
    baseline = _frames()
    value_before = {
        investment_id: path.copy(deep=True) for investment_id, path in baseline.value_paths.items()
    }
    cash_before = {currency: path.copy(deep=True) for currency, path in baseline.cash_paths.items()}
    flows_before = baseline.plan_flows

    apply_overlay(
        baseline,
        (
            InsertTransaction(
                investment_id=_PE,
                txn_type="buy",
                trade_date=date(2026, 8, 15),
                units=_D("7"),
                price_per_unit=_D("13"),
                consideration=None,
                currency="EUR",
            ),
            RepaceFlows(investment_id=_PE, factor=_D("1.75")),
            MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=_D("-35")),
        ),
    )

    for investment_id, path in value_before.items():
        assert_series_equal(
            baseline.value_paths[investment_id],
            path,
            check_exact=True,
            obj=f"baseline value path {investment_id}",
        )
    for currency, path in cash_before.items():
        assert_series_equal(
            baseline.cash_paths[currency],
            path,
            check_exact=True,
            obj=f"baseline {currency} cash path",
        )
    assert baseline.plan_flows == flows_before
