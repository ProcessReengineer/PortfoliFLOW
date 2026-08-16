# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure cash-plan projection (ADR-0103 §6).

Direct unit tests of :func:`services.investments.cash_plan_materialisation
.project_cash_plan` — the DB-free core of the plan path, in the spirit of
``test_holdings.py`` for :func:`~services.investments.holdings.derive_holdings`.
The formula and nothing else:

```
cash_plan(d) = anchor_balance + Σ signed plan flows with t₀ < t ≤ d
```

The DB shell (ledger of anchors, ownership, precedence, triggers) is
exercised in ``test_cash_plan_materialisation.py``; nothing here touches a
repository.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.investments.cash_plan_materialisation import (
    CashPlanPoint,
    PlanFlowEvent,
    project_cash_plan,
)


def _flow(day: str, amount: str) -> PlanFlowEvent:
    return PlanFlowEvent(as_of_date=date.fromisoformat(day), amount=Decimal(amount))


def _project(anchor: str, balance: str, flows: list[PlanFlowEvent]):
    return project_cash_plan(
        anchor_date=date.fromisoformat(anchor),
        anchor_balance=Decimal(balance),
        flows=flows,
    )


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------


def test_no_flows_projects_no_points() -> None:
    """No plan event ahead of the anchor → no forward path. Not an error."""
    assert _project("2024-03-31", "1000", []) == []


def test_a_plan_capital_call_debits_the_path() -> None:
    """``capital_call`` amounts are negative (Out-sheet guard) — ex-D6."""
    points = _project("2024-03-31", "1000", [_flow("2024-04-30", "-250")])
    assert points == [CashPlanPoint(as_of_date=date(2024, 4, 30), balance=Decimal("750.0000"))]


def test_a_plan_distribution_credits_the_path() -> None:
    """``distribution`` amounts are positive (In-sheet guard) — ex-D6."""
    points = _project("2024-03-31", "1000", [_flow("2024-04-30", "250")])
    assert points == [CashPlanPoint(as_of_date=date(2024, 4, 30), balance=Decimal("1250.0000"))]


def test_an_investor_flow_moves_the_path_both_ways() -> None:
    """A contribution is positive, a withdrawal negative (ADR-0103 §5).

    The projection needs no per-type branch to tell them apart: the signs
    carry the direction, so both simply sum.
    """
    points = _project(
        "2024-03-31",
        "1000",
        [_flow("2024-04-30", "500"), _flow("2024-05-31", "-800")],
    )
    assert [p.balance for p in points] == [
        Decimal("1500.0000"),
        Decimal("700.0000"),
    ]


def test_balances_are_cumulative_across_mixed_flows() -> None:
    """Every flow type sums into one running balance."""
    points = _project(
        "2024-03-31",
        "1000",
        [
            _flow("2024-04-30", "-250"),  # plan capital call
            _flow("2024-05-31", "400"),  # plan distribution
            _flow("2024-06-30", "-50"),  # plan fee
            _flow("2024-07-31", "1000"),  # plan investor contribution
        ],
    )
    assert [(p.as_of_date, p.balance) for p in points] == [
        (date(2024, 4, 30), Decimal("750.0000")),
        (date(2024, 5, 31), Decimal("1150.0000")),
        (date(2024, 6, 30), Decimal("1100.0000")),
        (date(2024, 7, 31), Decimal("2100.0000")),
    ]


def test_same_date_flows_collapse_into_one_point() -> None:
    """One row per distinct **event date**, never one per flow."""
    points = _project(
        "2024-03-31",
        "1000",
        [
            _flow("2024-04-30", "-250"),
            _flow("2024-04-30", "600"),
            _flow("2024-04-30", "-100"),
            _flow("2024-05-31", "10"),
        ],
    )
    assert [(p.as_of_date, p.balance) for p in points] == [
        (date(2024, 4, 30), Decimal("1250.0000")),  # 1000 − 250 + 600 − 100
        (date(2024, 5, 31), Decimal("1260.0000")),
    ]


def test_flows_are_projected_in_date_order_regardless_of_input_order() -> None:
    """The caller's ordering is irrelevant — the projection sorts."""
    shuffled = [
        _flow("2024-06-30", "-50"),
        _flow("2024-04-30", "-250"),
        _flow("2024-05-31", "400"),
    ]
    assert [p.balance for p in _project("2024-03-31", "1000", shuffled)] == [
        Decimal("750.0000"),
        Decimal("1150.0000"),
        Decimal("1100.0000"),
    ]


# ---------------------------------------------------------------------------
# Negative balances — the funding-gap signal
# ---------------------------------------------------------------------------


def test_a_projected_funding_gap_goes_negative_without_a_guard() -> None:
    """Negative plan balances are legal and expected (ADR-0103 §6).

    The single most decision-relevant signal the Planning Desk shows. The
    non-negativity rule of ADR-0100 §5 binds *actual* balances only.
    """
    points = _project(
        "2024-03-31",
        "100",
        [_flow("2024-04-30", "-250"), _flow("2024-05-31", "-100")],
    )
    assert [p.balance for p in points] == [
        Decimal("-150.0000"),
        Decimal("-250.0000"),
    ]


def test_a_path_may_recover_from_negative_to_positive() -> None:
    """A gap that a later distribution closes is projected through."""
    points = _project(
        "2024-03-31",
        "100",
        [_flow("2024-04-30", "-250"), _flow("2024-05-31", "500")],
    )
    assert [p.balance for p in points] == [
        Decimal("-150.0000"),
        Decimal("350.0000"),
    ]


# ---------------------------------------------------------------------------
# The t₀ boundary
# ---------------------------------------------------------------------------


def test_a_flow_on_the_anchor_date_contributes_nothing() -> None:
    """``t₀ < t``, strictly: the statement already contains that day."""
    assert _project("2024-03-31", "1000", [_flow("2024-03-31", "-250")]) == []


def test_a_flow_before_the_anchor_date_contributes_nothing() -> None:
    """Stale history — the statement level supersedes it."""
    assert _project("2024-03-31", "1000", [_flow("2024-01-15", "-250")]) == []


def test_a_flow_one_day_after_the_anchor_does_project() -> None:
    """The other side of the boundary."""
    points = _project("2024-03-31", "1000", [_flow("2024-04-01", "-250")])
    assert points == [CashPlanPoint(as_of_date=date(2024, 4, 1), balance=Decimal("750.0000"))]


def test_stale_flows_do_not_shift_the_forward_path() -> None:
    """A mixture: only the flows after t₀ move the balance."""
    points = _project(
        "2024-03-31",
        "1000",
        [
            _flow("2024-02-29", "-9999"),  # stale
            _flow("2024-03-31", "-9999"),  # on the anchor
            _flow("2024-04-30", "-250"),  # the only event
        ],
    )
    assert points == [CashPlanPoint(as_of_date=date(2024, 4, 30), balance=Decimal("750.0000"))]


# ---------------------------------------------------------------------------
# Quantisation — the load-bearing condition for the value-equal no-op
# ---------------------------------------------------------------------------


def test_balances_are_quantised_to_the_column_scale() -> None:
    """``Numeric(20, 4)``, half-away-from-zero — so a re-run compares equal."""
    points = _project("2024-03-31", "1000.00005", [_flow("2024-04-30", "0.00001")])
    assert points[0].balance == Decimal("1000.0001")
    assert points[0].balance.as_tuple().exponent == -4


def test_a_negative_balance_is_quantised_away_from_zero() -> None:
    """Matching Postgres ``numeric`` rounding on the funding-gap side."""
    points = _project("2024-03-31", "0", [_flow("2024-04-30", "-0.00005")])
    assert points[0].balance == Decimal("-0.0001")
