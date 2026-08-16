# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the drawdown-pacing rows (ADR-0104 §2/§4, S2.5).

The pure half of the pacing block: which funds get a row, whether the row is
enabled, where its remaining profile ends, what it says about an unfunded
commitment, and — the part that must not drift — the quarter arithmetic of the
readout, which is measured with the executor's own date rule.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.repositories.investment_repository import InvestmentDTO
from services.investments.pacing_rows import (
    NO_PLAN_NOTE,
    PLAN_SOURCE_REPORTED,
    PLAN_SOURCE_TA,
    build_pacing_rows,
    capital_account_ids,
    describe_shift,
    quarter_shift,
    repaceable_flows,
)
from services.overlay import PlanFlow, PlanFrames, PlanInvestment

T0 = date(2026, 3, 31)


def _investment(
    investment_id: UUID,
    *,
    name: str,
    investment_type: str,
    currency: str = "EUR",
    commitment: Decimal | None = None,
) -> InvestmentDTO:
    """A book row, with only the fields the pacing builder reads stated."""
    return InvestmentDTO(
        id=investment_id,
        tenant_id=uuid4(),
        name=name,
        investment_type=investment_type,
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency=currency,
        vintage_year=None,
        commitment_amount=commitment,
        is_active=True,
        type_specific_data=None,
        created_by=uuid4(),
        created_at=pd.Timestamp("2026-01-01").to_pydatetime(),
        updated_at=pd.Timestamp("2026-01-01").to_pydatetime(),
    )


def _frames(
    *,
    investments: dict[UUID, PlanInvestment],
    plan_flows: tuple[PlanFlow, ...] = (),
    profile_source: dict[UUID, str] | None = None,
) -> PlanFrames:
    return PlanFrames(
        t0=T0,
        value_paths={},
        cash_paths={},
        plan_flows=plan_flows,
        investments=investments,
        profile_source=profile_source or {},
    )


def _flow(
    investment_id: UUID,
    as_of_date: date,
    *,
    flow_type: str = "capital_call",
    amount: Decimal = Decimal("-100"),
    currency: str = "EUR",
) -> PlanFlow:
    return PlanFlow(
        investment_id=investment_id,
        as_of_date=as_of_date,
        amount=amount,
        currency=currency,
        flow_type=flow_type,
    )


# ---------------------------------------------------------------------------
# Row selection — capital accounts, and nothing else
# ---------------------------------------------------------------------------


def test_only_capital_account_funds_get_a_row() -> None:
    """Every other archetype would draw NotRepaceableError from the executor."""
    buyout, equity, bonds, other = uuid4(), uuid4(), uuid4(), uuid4()
    book = {
        buyout: _investment(buyout, name="Buyout Fund IV", investment_type="private_equity"),
        equity: _investment(equity, name="Equity Fund", investment_type="listed_equity"),
        bonds: _investment(bonds, name="Bond Fund", investment_type="listed_bonds"),
        other: _investment(other, name="Misc", investment_type="other"),
    }
    frames = _frames(
        investments={
            investment_id: PlanInvestment(
                investment_id=investment_id,
                currency="EUR",
                investment_type=investment.investment_type,
            )
            for investment_id, investment in book.items()
        }
    )

    rows = build_pacing_rows(frames=frames, investments_by_id=book, called_by_investment={})

    assert [row.investment_id for row in rows] == [buyout]
    assert capital_account_ids(frames=frames, investments_by_id=book) == [buyout]


def test_all_four_capital_account_types_get_a_row_in_book_order() -> None:
    """Row order follows the caller's mapping — the book's name-sorted order."""
    ids = [uuid4() for _ in range(4)]
    types = ["private_equity", "private_debt", "real_estate", "infra_equity"]
    book = {
        investment_id: _investment(
            investment_id, name=f"Fund {index}", investment_type=investment_type
        )
        for index, (investment_id, investment_type) in enumerate(zip(ids, types, strict=True))
    }
    frames = _frames(
        investments={
            investment_id: PlanInvestment(
                investment_id=investment_id,
                currency="EUR",
                investment_type=book[investment_id].investment_type,
            )
            for investment_id in ids
        }
    )

    rows = build_pacing_rows(frames=frames, investments_by_id=book, called_by_investment={})

    assert [row.investment_id for row in rows] == ids


def test_a_fund_absent_from_the_plan_world_gets_no_row() -> None:
    """The executor would raise UnknownInvestmentError; no slider offers it."""
    orphan = uuid4()
    book = {orphan: _investment(orphan, name="Orphan Fund", investment_type="private_equity")}

    rows = build_pacing_rows(
        frames=_frames(investments={}),
        investments_by_id=book,
        called_by_investment={},
    )

    assert rows == ()


# ---------------------------------------------------------------------------
# Enablement — the repaceable set is non-empty
# ---------------------------------------------------------------------------


def test_the_repaceable_set_is_the_remaining_non_exempt_plan_flows() -> None:
    """Strictly after the seam, and never an investor_flow (ADR-0103 §5)."""
    fund, other_fund = uuid4(), uuid4()
    remaining = _flow(fund, date(2026, 9, 30))
    frames = _frames(
        investments={
            fund: PlanInvestment(
                investment_id=fund,
                currency="EUR",
                investment_type="private_equity",
            )
        },
        plan_flows=(
            _flow(fund, date(2026, 1, 31)),  # before the seam
            _flow(fund, T0),  # *at* the seam — history, not remaining
            _flow(fund, date(2026, 12, 31), flow_type="investor_flow"),
            _flow(other_fund, date(2026, 12, 31)),  # another fund's
            remaining,
        ),
    )

    assert repaceable_flows(frames, fund) == (remaining,)


def test_a_fund_with_no_remaining_profile_is_disabled_not_hidden() -> None:
    """ADR-0104 §4: the row renders, and the note names roadmap #023."""
    fund = uuid4()
    book = {fund: _investment(fund, name="Venture Fund I", investment_type="private_equity")}
    frames = _frames(
        investments={
            fund: PlanInvestment(
                investment_id=fund,
                currency="EUR",
                investment_type="private_equity",
            )
        },
        plan_flows=(_flow(fund, date(2026, 1, 31)),),  # realised history only
    )

    (row,) = build_pacing_rows(frames=frames, investments_by_id=book, called_by_investment={})

    assert row.enabled is False
    assert row.profile_end is None
    assert row.plan_source is None
    # The note names the *current* reason. Since ADR-0105 §4 a plan-less fund
    # gets a generated profile, so a row that is still disabled at this point
    # is one the generator could model nothing for — and the note must not go
    # on promising the profile that already arrived.
    assert "#023" not in NO_PLAN_NOTE
    assert "no manager plan" in NO_PLAN_NOTE


def test_an_enabled_row_carries_the_remaining_profile_end() -> None:
    """The profile end is the *latest* repaceable flow date — the readout's input."""
    fund = uuid4()
    book = {fund: _investment(fund, name="Buyout Fund IV", investment_type="private_equity")}
    frames = _frames(
        investments={
            fund: PlanInvestment(
                investment_id=fund,
                currency="EUR",
                investment_type="private_equity",
            )
        },
        plan_flows=(
            _flow(fund, date(2026, 9, 30)),
            _flow(fund, date(2027, 3, 31)),
            _flow(fund, date(2026, 12, 31)),
            # An investor_flow further out must not extend the profile.
            _flow(fund, date(2029, 12, 31), flow_type="investor_flow"),
        ),
    )

    (row,) = build_pacing_rows(frames=frames, investments_by_id=book, called_by_investment={})

    assert row.enabled is True
    assert row.profile_end == date(2027, 3, 31)
    assert row.plan_source == PLAN_SOURCE_REPORTED


def test_a_ta_marked_fund_enables_with_the_generated_profile() -> None:
    """A generated profile paces exactly like a reported one (ADR-0105 §5).

    The frames arriving from the seam carry the generated flows *in*
    ``plan_flows`` and the fund *in* ``profile_source``. The row must enable on
    them: mid-position is the generated profile, and ``repace_flows`` will
    time-scale it indifferent to where it came from. Only ``plan_source``
    distinguishes the two — which is what the badge renders, and the whole of
    what tells the operator a model from a manager.
    """
    fund = uuid4()
    book = {
        fund: _investment(
            fund,
            name="Plan-less Fund",
            investment_type="private_equity",
            commitment=Decimal("1000"),
        )
    }
    frames = _frames(
        investments={
            fund: PlanInvestment(
                investment_id=fund,
                currency="EUR",
                investment_type="private_equity",
            )
        },
        plan_flows=(
            _flow(fund, date(2026, 12, 31)),
            _flow(fund, date(2027, 6, 30), flow_type="distribution"),
        ),
        profile_source={fund: PLAN_SOURCE_TA},
    )

    (row,) = build_pacing_rows(
        frames=frames,
        investments_by_id=book,
        called_by_investment={fund: Decimal("250")},
    )

    assert row.enabled is True
    assert row.plan_source == PLAN_SOURCE_TA
    assert row.profile_end == date(2027, 6, 30)
    # The unfunded figure is the book's own either way — a generated profile
    # says nothing about how much commitment is left.
    assert row.unfunded == Decimal("750")


def test_an_unmarked_fund_with_flows_is_still_reported() -> None:
    """The marking is read, never assumed: no mark ⇒ the book's own plan.

    The companion to the test above, and the reason ``profile_source`` defaults
    to empty: frames assembled from a book with nothing to generate are the
    frames they always were, and every fund on them stays ``'reported'``.
    """
    fund = uuid4()
    book = {fund: _investment(fund, name="Buyout Fund IV", investment_type="private_equity")}
    frames = _frames(
        investments={
            fund: PlanInvestment(
                investment_id=fund,
                currency="EUR",
                investment_type="private_equity",
            )
        },
        plan_flows=(_flow(fund, date(2026, 12, 31)),),
    )

    (row,) = build_pacing_rows(frames=frames, investments_by_id=book, called_by_investment={})

    assert row.plan_source == PLAN_SOURCE_REPORTED


# ---------------------------------------------------------------------------
# Unfunded — commitment minus realised calls, or nothing at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("commitment", "called", "expected"),
    [
        (Decimal("1000"), Decimal("300"), Decimal("700")),
        (Decimal("1000"), None, Decimal("1000")),
        (None, Decimal("300"), None),
        (None, None, None),
    ],
)
def test_unfunded_is_commitment_less_calls_and_none_without_a_commitment(
    commitment: Decimal | None,
    called: Decimal | None,
    expected: Decimal | None,
) -> None:
    """A book that states no commitment states no unfunded figure either."""
    fund = uuid4()
    book = {
        fund: _investment(
            fund,
            name="Buyout Fund IV",
            investment_type="private_equity",
            commitment=commitment,
        )
    }
    frames = _frames(
        investments={
            fund: PlanInvestment(
                investment_id=fund,
                currency="EUR",
                investment_type="private_equity",
            )
        }
    )

    (row,) = build_pacing_rows(
        frames=frames,
        investments_by_id=book,
        called_by_investment={fund: called} if called is not None else {},
    )

    assert row.unfunded == expected


# ---------------------------------------------------------------------------
# The readout — the executor's date rule, in calendar quarters
# ---------------------------------------------------------------------------


def test_quarter_shift_stretches_and_compresses_symmetrically() -> None:
    """A one-year profile at ×1.5 stretches two quarters; at ×0.5 it compresses two.

    t₀ = 2026-03-31, profile end 2027-03-31 → an offset of 365 days.

    * ×1.5 → 547.5 → 548 days → 2027-09-29, which is Q3 2027: +2 quarters.
    * ×0.5 → 182.5 → 183 days → 2026-09-30, which is Q3 2026: −2 quarters.
    """
    profile_end = date(2027, 3, 31)

    assert quarter_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.5")) == 2
    assert quarter_shift(t0=T0, profile_end=profile_end, factor=Decimal("0.5")) == -2
    assert quarter_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.0")) == 0


def test_a_shift_that_rounds_across_a_quarter_boundary_moves_a_column() -> None:
    """Round-half-up decides the quarter, and the readout follows it exactly.

    Profile end 2026-06-30 — the last day of Q2, 91 days past the seam. At
    ×1.05 the offset scales to 95.55, rounds **up** to 96, and lands on
    2026-07-05: one calendar quarter out. A rule that floored the product, or
    that divided a day count by ninety-one, would still be sitting in Q2.
    """
    profile_end = date(2026, 6, 30)

    assert quarter_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.05")) == 1
    assert (
        describe_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.05"))
        == "stretch +1 quarter"
    )


def test_an_off_mid_factor_inside_the_quarter_is_not_on_plan() -> None:
    """Flows moved, a chip exists — the readout must not say 'on plan'.

    Profile end 2026-04-30, 30 days past the seam. ×1.05 → 31.5 → 32 days →
    2026-05-02: the profile moved, and stayed inside Q2.
    """
    profile_end = date(2026, 4, 30)

    assert quarter_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.05")) == 0
    assert (
        describe_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.05")) == "under a quarter"
    )


def test_the_mid_position_reads_as_the_plan() -> None:
    """FACTOR_NEUTRAL is the manager plan, and says so (ADR-0104 §4)."""
    assert describe_shift(t0=T0, profile_end=date(2027, 3, 31), factor=Decimal("1.0")) == "on plan"


def test_the_readout_pluralises_and_signs_both_directions() -> None:
    """'stretch +N quarters' / 'compress −N quarters', singular at one."""
    profile_end = date(2027, 3, 31)

    assert (
        describe_shift(t0=T0, profile_end=profile_end, factor=Decimal("1.5"))
        == "stretch +2 quarters"
    )
    assert (
        describe_shift(t0=T0, profile_end=profile_end, factor=Decimal("0.5"))
        == "compress −2 quarters"
    )
    # 365 × 0.75 = 273.75 → 274 days → 2026-12-30, which is Q4 2026: −1.
    assert (
        describe_shift(t0=T0, profile_end=profile_end, factor=Decimal("0.75"))
        == "compress −1 quarter"
    )
