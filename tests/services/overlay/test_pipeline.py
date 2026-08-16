# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pipeline tests: the identity of the empty overlay, and the loud failure.

ADR-0104 §4: the Baseline toggle renders the same regions with an empty
transformation list — so the empty overlay must be the identity of
:func:`apply_overlay`, not a special case rendered by other code.

The registry holds three of the four kinds, and the fold executes them. The
fourth — ``fx_shock`` — is not a value transformation: it acts at the conversion
seam (ADR-0104 §2/§3, N3), on a rate path that is not in :class:`PlanFrames`.
:func:`partition_fx_shocks` routes it there, and the fold goes on **refusing**
one handed to it directly, which since S34.2 means *mis-routed* rather than
*unimplemented*. Either way it must not pass the frames through: a scenario that
silently equals its baseline would tell the operator "no impact" where the truth
is "not computed".

The executors' own semantics are tested in ``test_executors.py`` and anchored
in ``tests/regression/test_overlay_transformation_anchors.py``; the FX
restatement itself in ``tests/services/fx/test_plan_shock.py`` and its seam in
``tests/services/investments/test_cash_flow_timeline.py``. What is pinned here
is the *fold* and its *router*.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest

from services.investments.archetype import Archetype
from services.overlay import (
    EMPTY_OVERLAY,
    ExecutorNotRegisteredError,
    FxShock,
    InsertTransaction,
    MarketShock,
    PlanFlow,
    PlanFrames,
    PlanInvestment,
    RepaceFlows,
    apply_overlay,
    partition_fx_shocks,
)

_INVESTMENT = UUID("11111111-1111-1111-1111-111111111111")


def _frames() -> PlanFrames:
    """A minimal, syntactically valid set of baseline frames.

    The values are irrelevant to this strand — S2.2 owns the assembly. What
    matters is that the container the pipeline passes through can be built
    from pandas and stdlib types alone, with no repository in sight.
    """
    index = pd.to_datetime(["2026-06-30", "2026-09-30"])
    return PlanFrames(
        t0=date(2026, 6, 30),
        value_paths={_INVESTMENT: pd.Series([100.0, 110.0], index=index)},
        cash_paths={"EUR": pd.Series([50.0, 45.0], index=index)},
        plan_flows=(
            PlanFlow(
                investment_id=_INVESTMENT,
                as_of_date=date(2026, 9, 30),
                amount=Decimal("-5"),
                currency="EUR",
                flow_type="capital_call",
            ),
        ),
        investments={
            _INVESTMENT: PlanInvestment(
                investment_id=_INVESTMENT,
                currency="EUR",
                investment_type="private_equity",
            )
        },
    )


def test_empty_overlay_is_the_identity() -> None:
    """The baseline path returns the very frames it was handed."""
    frames = _frames()
    assert apply_overlay(frames, EMPTY_OVERLAY) is frames


def test_the_fold_executes_a_registered_kind() -> None:
    """Importing the package registers the executors; the fold reaches them."""
    transformation = InsertTransaction(
        investment_id=_INVESTMENT,
        txn_type="buy",
        trade_date=date(2026, 9, 30),
        units=Decimal("10"),
        price_per_unit=Decimal("11"),
        consideration=None,
        currency="EUR",
    )
    frames = _frames()
    scenario = apply_overlay(frames, (transformation,))

    assert scenario is not frames
    assert scenario.value_paths[_INVESTMENT].loc[pd.Timestamp("2026-09-30")] == Decimal("220")


def test_the_fold_applies_transformations_in_list_order() -> None:
    """Each executor sees the output of the previous one (ADR-0104 §2).

    Two buys on the same investment compound: the second steps a value path
    that already carries the first.
    """
    first = InsertTransaction(
        investment_id=_INVESTMENT,
        txn_type="buy",
        trade_date=date(2026, 9, 30),
        units=Decimal("10"),
        price_per_unit=Decimal("11"),
        consideration=None,
        currency="EUR",
    )
    second = InsertTransaction(
        investment_id=_INVESTMENT,
        txn_type="buy",
        trade_date=date(2026, 9, 30),
        units=Decimal("1"),
        price_per_unit=Decimal("5"),
        consideration=None,
        currency="EUR",
    )
    scenario = apply_overlay(_frames(), (first, second))

    assert scenario.value_paths[_INVESTMENT].loc[pd.Timestamp("2026-09-30")] == Decimal("225")
    assert scenario.cash_paths["EUR"].loc[pd.Timestamp("2026-09-30")] == Decimal("-70")


def test_a_neutral_re_pacing_returns_the_frames_themselves() -> None:
    """The mid-position *is* the plan (ADR-0104 §2/§4)."""
    frames = _frames()
    overlay = (RepaceFlows(investment_id=_INVESTMENT, factor=Decimal("1.0")),)

    assert apply_overlay(frames, overlay) is frames


def test_the_fold_still_refuses_an_fx_shock_handed_to_it() -> None:
    """A seam operation routed to the fold is a mis-route, and says so.

    ``fx_shock`` is a **real, well-formed value** — it constructs, it round-trips
    through the encoding, and since S34.2 it is fully *applied*. But not here:
    it acts at the conversion seam (ADR-0104 §2/§3, N3), on a plan-world FX path
    that is not in :class:`PlanFrames` at all. A ``frames → frames`` executor
    would have nothing to act on, so there is none, and there will not be one.

    The fold therefore keeps raising — deliberately unchanged from S34.1, with a
    changed meaning. Before, it meant *not yet implemented*; now it means the
    caller failed to :func:`partition_fx_shocks` first. Either way it must not
    pass the frames through: an operator who read "no impact" where the truth
    was "not computed" would be misled by a scenario that looked like it ran.
    """
    frames = _frames()
    with pytest.raises(ExecutorNotRegisteredError) as excinfo:
        apply_overlay(frames, (FxShock(currency="USD", magnitude=Decimal("-10")),))
    assert "fx_shock" in str(excinfo.value)


def test_partition_routes_the_two_halves_of_an_overlay() -> None:
    """The router: value transformations to the fold, FX shocks to the seam."""
    repace = RepaceFlows(investment_id=_INVESTMENT, factor=Decimal("1.5"))
    shock = MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-20"))
    fx = FxShock(currency="USD", magnitude=Decimal("-10"))

    value_overlay, fx_shocks = partition_fx_shocks((repace, fx, shock))

    assert value_overlay == (repace, shock)
    assert fx_shocks == (fx,)


def test_partition_preserves_the_relative_order_of_both_halves() -> None:
    """Application order is list order (ADR-0104 §2) — within each seam.

    The split is legitimate *because* the two halves act on disjoint state: the
    value executors touch only frames, a shock touches only rates. So an
    ``fx_shock`` interleaved between two value transformations reorders neither,
    and two FX shocks keep their own order, which is what makes them compose
    multiplicatively at the seam.
    """
    first = MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-20"))
    second = MarketShock(archetype=Archetype.NAV_ONLY, magnitude=Decimal("10"))
    usd = FxShock(currency="USD", magnitude=Decimal("-10"))
    gbp = FxShock(currency="GBP", magnitude=Decimal("5"))

    value_overlay, fx_shocks = partition_fx_shocks((usd, first, gbp, second))

    assert value_overlay == (first, second)
    assert fx_shocks == (usd, gbp)


def test_partition_of_an_overlay_without_fx_shocks_keeps_it_whole() -> None:
    """The common case costs nothing and loses nothing."""
    overlay = (
        RepaceFlows(investment_id=_INVESTMENT, factor=Decimal("0.8")),
        MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-20")),
    )
    value_overlay, fx_shocks = partition_fx_shocks(overlay)

    assert value_overlay == overlay
    assert fx_shocks == ()


def test_partition_of_the_empty_overlay_is_two_empties() -> None:
    """The baseline splits into two baselines — no branch anywhere downstream."""
    assert partition_fx_shocks(EMPTY_OVERLAY) == ((), ())


def test_the_partitioned_value_overlay_folds_where_the_whole_one_would_not() -> None:
    """The router is what makes an fx_shock-carrying overlay computable.

    The same overlay that :func:`apply_overlay` refuses whole, it executes once
    the FX half is split out — which is precisely the S34.2 seam hook, seen from
    the fold's side.
    """
    frames = _frames()
    overlay = (
        FxShock(currency="USD", magnitude=Decimal("-10")),
        MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-50")),
    )

    with pytest.raises(ExecutorNotRegisteredError):
        apply_overlay(frames, overlay)

    value_overlay, fx_shocks = partition_fx_shocks(overlay)
    scenario = apply_overlay(frames, value_overlay)

    assert fx_shocks == (FxShock(currency="USD", magnitude=Decimal("-10")),)
    assert scenario.value_paths[_INVESTMENT].iloc[-1] == 55.0  # 110 × 0.5


def test_the_market_shock_kind_is_executable_since_s34_1() -> None:
    """The counterpart: the other shock kind *does* fold, and changes the world.

    Pinned here as well as in the executor tests because the two facts are one
    decision — ``market_shock`` executes and ``fx_shock`` does not — and a fold
    that quietly stopped dispatching the executable one would fail no other test
    in this module.
    """
    frames = _frames()
    scenario = apply_overlay(
        frames,
        (MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-50")),),
    )
    assert scenario is not frames
    assert scenario.value_paths[_INVESTMENT].iloc[-1] == 55.0  # 110 × 0.5


def test_plan_frames_are_frozen() -> None:
    """The frames are a value the executors replace, never mutate in place."""
    frames = _frames()
    with pytest.raises(AttributeError):
        frames.t0 = date(2026, 12, 31)  # type: ignore[misc]


def test_plan_flow_carries_the_flow_type_the_exemption_needs() -> None:
    """ADR-0103 §5: an executor must see an investor flow to spare it.

    The invariant itself is imported from
    :mod:`services.investments.flow_type_invariants`, never restated — this
    only pins that the frames carry the field the invariant is applied to.
    """
    from services.investments.flow_type_invariants import is_overlay_exempt

    investor_flow = PlanFlow(
        investment_id=_INVESTMENT,
        as_of_date=date(2026, 9, 30),
        amount=Decimal("1000"),
        currency="EUR",
        flow_type="investor_flow",
    )
    plan_call = _frames().plan_flows[0]

    assert is_overlay_exempt(investor_flow.flow_type)
    assert not is_overlay_exempt(plan_call.flow_type)
