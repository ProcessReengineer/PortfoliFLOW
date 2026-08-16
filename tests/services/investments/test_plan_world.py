# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plan-world baseline assembly: the Layer-1 → Layer-2 seam (ADR-0104 §1/§3).

Pure tests over fake repositories — the assembly is a *reader*, so a fake that
returns rows is a complete stand-in for the book, and the tests state the book
they mean in one place instead of seeding it through four services.

What is pinned here:

* the **shape** of the frames the executors were anchored against (value paths
  per non-cash investment, cash paths per currency, flows as a flat tuple),
* the **seam** ``t₀`` — the book's last actual statement (ADR-0060),
* the **cash/non-cash split** (ADR-0103 §2): a cash position contributes a cash
  path and *nothing else*, or Σ over the frames would double-count it,
* the **ADR-0060 cross-stream fallback** for an investment with no plan stream,
  asserted on values rather than on presence,
* the **exemption invariant's boundary** (ADR-0103 §5): ``investor_flow`` rows
  reach the frames unfiltered — the executors spare them, the assembly does not
  hide them,
* **position-currency purity** (ADR-0104 §3, N2): no conversion happens here,
* and the **identity law** against real assembled frames, not only the
  synthetic anchor fixtures.

§9 pins what ADR-0105 added — the one rule that puts something in the frames
the book does not hold, and therefore the one that needs the most pinning:

* **activation**: a committed, plan-less capital account gains a generated
  profile, marked ``profile_source='ta'``; one with no commitment does not,
* **non-interference** (§6): a fund *with* a manager plan is byte-identical
  whether the TA module runs or not — asserted against a generator that fails
  if it is called at all, rather than trusted,
* **book silence** (§4): the seam performs zero writes — asserted out loud,
  against repositories whose every write method is a trap,
* **flows only** (§5, E4): no TA NAV path surfaces; ``value_paths`` stays the
  ADR-0060 carry-forward it was,
* **settlement** (§2): generated flows reach their currency's cash path exactly
  as manager-plan flows do,
* and the **exemption boundary** (§6): the generator emits two flow types,
  neither exempt, so no ``investor_flow`` can enter through it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.exceptions import DuplicateCashPositionError, PlanSeamMissingError
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowDTO,
)
from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from services.investments.cash_flow_timeline import Periodisation
from services.investments.cash_plan_materialisation import CASH_PLAN_SOURCE
from services.investments.pacing_rows import PLAN_SOURCE_TA
from services.investments.plan_world import assemble_plan_frames
from services.overlay import apply_overlay

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

_D = Decimal


# ---------------------------------------------------------------------------
# The book, as DTOs
# ---------------------------------------------------------------------------


def _investment(
    *,
    name: str,
    currency: str = "EUR",
    investment_type: str = "private_equity",
    is_active: bool = True,
    commitment: str | None = None,
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
        commitment_amount=_D(commitment) if commitment is not None else None,
        is_active=is_active,
        type_specific_data=None,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _nav(
    investment: InvestmentDTO,
    day: str,
    value: str,
    *,
    nav_kind: str,
    source: str | None = "excel",
    ingest_origin: str = "excel",
) -> InvestmentNavDTO:
    return InvestmentNavDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        investment_id=investment.id,
        as_of_date=date.fromisoformat(day),
        nav_value=_D(value),
        currency=investment.currency,
        nav_kind=nav_kind,
        source=source,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
        ingest_origin=ingest_origin,
    )


def _actual(investment: InvestmentDTO, day: str, value: str) -> InvestmentNavDTO:
    """A statement row — the stream the seam and the fallback are read from."""
    return _nav(investment, day, value, nav_kind="actual", source="statement")


def _plan(investment: InvestmentDTO, day: str, value: str) -> InvestmentNavDTO:
    """A workbook plan row (ADR-0043: the manager's projection)."""
    return _nav(investment, day, value, nav_kind="plan")


def _cash_plan(investment: InvestmentDTO, day: str, value: str) -> InvestmentNavDTO:
    """A materialised cash-plan row (ADR-0103 §6) — the reader's own rows."""
    return _nav(
        investment,
        day,
        value,
        nav_kind="plan",
        source=CASH_PLAN_SOURCE,
        ingest_origin="system",
    )


def _flow(
    investment: InvestmentDTO,
    day: str,
    amount: str,
    flow_type: str,
    *,
    flow_kind: str = "plan",
    currency: str | None = None,
) -> InvestmentCashflowDTO:
    return InvestmentCashflowDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        investment_id=investment.id,
        flow_timestamp=datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(
            hour=12, tzinfo=timezone.utc
        ),
        flow_type=flow_type,
        flow_kind=flow_kind,
        amount=_D(amount),
        currency=currency or investment.currency,
        description=None,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Fake repositories — the three reads the assembly makes
# ---------------------------------------------------------------------------


class _FakeInvestmentRepository:
    """Serves ``list_active()``; the assembly asks for nothing else."""

    def __init__(self, investments: list[InvestmentDTO]) -> None:
        self._investments = investments

    async def list_active(self) -> list[InvestmentDTO]:
        return [i for i in self._investments if i.is_active]


class _FakeNavRepository:
    """Serves the batch read, grouped and ascending, as the real one does."""

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
    def __init__(self, flows: list[InvestmentCashflowDTO]) -> None:
        self._flows = flows

    async def list_by_investments_and_kind(
        self, investment_ids: list[UUID], flow_kind: str
    ) -> dict[UUID, list[InvestmentCashflowDTO]]:
        grouped: dict[UUID, list[InvestmentCashflowDTO]] = {i: [] for i in investment_ids}
        for flow in self._flows:
            if flow.investment_id in grouped and flow.flow_kind == flow_kind:
                grouped[flow.investment_id].append(flow)
        for rows in grouped.values():
            rows.sort(key=lambda f: f.flow_timestamp)
        return grouped


async def _assemble(
    investments: list[InvestmentDTO],
    navs: list[InvestmentNavDTO],
    flows: list[InvestmentCashflowDTO] | None = None,
    *,
    periodisation: Periodisation = Periodisation.QUARTERLY,
):
    """Assemble frames from a book stated as three lists of rows."""
    return await assemble_plan_frames(
        investments=_FakeInvestmentRepository(investments),  # type: ignore[arg-type]
        navs=_FakeNavRepository(navs),  # type: ignore[arg-type]
        cashflows=_FakeCashflowRepository(flows or []),  # type: ignore[arg-type]
        periodisation=periodisation,
    )


def _index(*days: str) -> pd.DatetimeIndex:
    return pd.to_datetime([date.fromisoformat(day) for day in days])


# ---------------------------------------------------------------------------
# The standard book: two investments, two currencies, two cash positions
# ---------------------------------------------------------------------------


def _book() -> tuple[
    list[InvestmentDTO],
    list[InvestmentNavDTO],
    list[InvestmentCashflowDTO],
]:
    pe = _investment(name="Fund I", currency="EUR")
    eq = _investment(name="Global Equity", currency="USD", investment_type="listed_equity")
    cash_eur = _investment(name="Cash EUR", currency="EUR", investment_type="cash")
    cash_usd = _investment(name="Cash USD", currency="USD", investment_type="cash")

    navs = [
        # The capital-account fund: actual history, and a plan stream beyond.
        _actual(pe, "2026-03-31", "150"),
        _actual(pe, "2026-06-30", "200"),
        _plan(pe, "2026-09-30", "260"),
        _plan(pe, "2026-12-31", "320"),
        # The listed mandate: never forecasted (ADR-0060 §Context) — actual only.
        _actual(eq, "2026-05-31", "520"),
        _actual(eq, "2026-06-30", "540.25"),
        # The EUR cash position: an anchor, and its materialised plan path.
        _actual(cash_eur, "2026-06-30", "1000"),
        _cash_plan(cash_eur, "2026-09-30", "900"),
        _cash_plan(cash_eur, "2026-12-31", "1400"),
        # The USD cash position: an anchor, no plan flows ahead of it.
        _actual(cash_usd, "2026-06-15", "300.5000"),
    ]
    flows = [
        _flow(pe, "2026-09-30", "-100", "capital_call"),
        # The investor's own money — exempt from every transformation, and
        # bookable on a cash position only (ADR-0103 §5).
        _flow(cash_eur, "2026-12-31", "500", "investor_flow"),
        # An actual flow: informational on cash, never part of the plan world.
        _flow(pe, "2026-02-28", "-40", "capital_call", flow_kind="actual"),
    ]
    return [pe, eq, cash_eur, cash_usd], navs, flows


# ---------------------------------------------------------------------------
# 1. Shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frames_carry_the_book_in_the_shape_the_executors_expect() -> None:
    """Value paths, cash paths, flows and metadata, keyed as ADR-0104 §1 says."""
    investments, navs, flows = _book()
    pe, eq, cash_eur, _cash_usd = investments

    frames = await _assemble(investments, navs, flows)

    # t₀: the book's last actual statement, over the whole active universe.
    assert frames.t0 == date(2026, 6, 30)

    assert set(frames.value_paths) == {pe.id, eq.id}
    assert set(frames.investments) == {pe.id, eq.id}
    assert set(frames.cash_paths) == {"EUR", "USD"}

    # The plan stream, laid down as a balance path in position currency.
    pd.testing.assert_series_equal(
        frames.value_paths[pe.id],
        pd.Series(
            [_D("260"), _D("320")],
            index=_index("2026-09-30", "2026-12-31"),
            dtype="object",
        ),
        check_exact=True,
    )

    assert frames.investments[pe.id].currency == "EUR"
    assert frames.investments[pe.id].investment_type == "private_equity"
    assert frames.investments[eq.id].currency == "USD"

    # Plan flows only: the actual capital call of February is not a plan event.
    assert len(frames.plan_flows) == 2
    assert {(f.investment_id, f.flow_type) for f in frames.plan_flows} == {
        (pe.id, "capital_call"),
        (cash_eur.id, "investor_flow"),
    }
    call = next(f for f in frames.plan_flows if f.flow_type == "capital_call")
    assert call.as_of_date == date(2026, 9, 30)
    assert call.amount == _D("-100")
    assert call.currency == "EUR"


# ---------------------------------------------------------------------------
# 2. The cash split (ADR-0103 §2 — Σ must not double-count)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cash_position_contributes_a_cash_path_and_nothing_else() -> None:
    """A cash row is in ``cash_paths`` only — never in ``value_paths``."""
    investments, navs, flows = _book()
    _pe, _eq, cash_eur, cash_usd = investments

    frames = await _assemble(investments, navs, flows)

    assert cash_eur.id not in frames.value_paths
    assert cash_eur.id not in frames.investments
    assert cash_usd.id not in frames.value_paths
    assert cash_usd.id not in frames.investments

    # The EUR path: the anchor balance (ADR-0103 §6's ``balance(t₀)`` term),
    # then the materialised forward rows.
    pd.testing.assert_series_equal(
        frames.cash_paths["EUR"],
        pd.Series(
            [_D("1000"), _D("900"), _D("1400")],
            index=_index("2026-06-30", "2026-09-30", "2026-12-31"),
            dtype="object",
        ),
        check_exact=True,
    )


@pytest.mark.asyncio
async def test_cash_path_without_plan_rows_opens_at_its_anchor() -> None:
    """A funded account with no plan flows ahead of it still holds its balance.

    Its path is the anchor alone — not an empty path, which would let a
    hypothetical trade settle against a zero balance and invent a funding gap.
    """
    investments, navs, flows = _book()

    frames = await _assemble(investments, navs, flows)

    pd.testing.assert_series_equal(
        frames.cash_paths["USD"],
        pd.Series([_D("300.5000")], index=_index("2026-06-15"), dtype="object"),
        check_exact=True,
    )


@pytest.mark.asyncio
async def test_anchorless_cash_position_yields_no_path() -> None:
    """No statement, no anchor, no path (ADR-0103 §6).

    The scenario then fails loudly against that currency rather than settling
    against a balance nobody stated.
    """
    pe = _investment(name="Fund I", currency="EUR")
    cash_chf = _investment(name="Cash CHF", currency="CHF", investment_type="cash")
    navs = [_actual(pe, "2026-06-30", "200"), _plan(pe, "2026-09-30", "260")]

    frames = await _assemble([pe, cash_chf], navs)

    assert "CHF" not in frames.cash_paths
    assert frames.cash_paths == {}


# ---------------------------------------------------------------------------
# 3. Carry-forward (ADR-0060 cross-stream fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_investment_without_plan_stream_falls_back_to_its_actuals() -> None:
    """The listed mandate is not forecasted: its actual path *is* its plan path.

    ADR-0060 §Decision 3: with no entry in the preferred (plan) stream, the
    engine consults the actual stream under carry-forward. A balance path holds
    its last level until the next observation, so the actual stream laid down as
    a path is that rule — the last actual NAV held flat across the horizon.
    """
    investments, navs, flows = _book()
    _pe, eq, _cash_eur, _cash_usd = investments

    frames = await _assemble(investments, navs, flows)

    pd.testing.assert_series_equal(
        frames.value_paths[eq.id],
        pd.Series(
            [_D("520"), _D("540.25")],
            index=_index("2026-05-31", "2026-06-30"),
            dtype="object",
        ),
        check_exact=True,
    )
    # The carried level, read at any plan date: the last actual, unchanged.
    carried = frames.value_paths[eq.id].asof(pd.Timestamp("2027-12-31"))
    assert carried == _D("540.25")


@pytest.mark.asyncio
async def test_investment_with_no_nav_at_all_carries_metadata_but_no_path() -> None:
    """Not yet in the book: it contributes nothing — not zero, *nothing*.

    The :func:`services.investments.aum.build_nav_series` semantics. The
    executors read the absent path as the zero level it is
    (:func:`services.overlay.steps.zero_path`), so the investment stays
    addressable by an ``insert_transaction``.
    """
    pe = _investment(name="Fund I", currency="EUR")
    fresh = _investment(name="Fund II — unfunded", currency="EUR")
    navs = [_actual(pe, "2026-06-30", "200")]

    frames = await _assemble([pe, fresh], navs)

    assert fresh.id in frames.investments
    assert fresh.id not in frames.value_paths


# ---------------------------------------------------------------------------
# 4. Flows are unfiltered (ADR-0103 §5 is the executors' invariant, not ours)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_investor_flows_reach_the_frames_unfiltered() -> None:
    """The exempt type is carried, not hidden.

    The exemption is enforced by the executors through ``is_overlay_exempt``.
    Pre-filtering here would enforce it in a second place — and would hide the
    mandate's own contributions from a Planning Desk that has to show them.
    """
    investments, navs, flows = _book()
    _pe, _eq, cash_eur, _cash_usd = investments

    frames = await _assemble(investments, navs, flows)

    investor = [f for f in frames.plan_flows if f.flow_type == "investor_flow"]
    assert len(investor) == 1
    assert investor[0].investment_id == cash_eur.id
    assert investor[0].amount == _D("500")
    assert investor[0].currency == "EUR"


# ---------------------------------------------------------------------------
# 5. Two cash positions in one currency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_cash_currency_is_refused_by_name() -> None:
    """One currency, one cash path — or the plan world cannot say which."""
    pe = _investment(name="Fund I", currency="EUR")
    custodian_a = _investment(name="Cash EUR — Custodian A", currency="EUR", investment_type="cash")
    custodian_b = _investment(name="Cash EUR — Custodian B", currency="EUR", investment_type="cash")
    navs = [
        _actual(pe, "2026-06-30", "200"),
        _actual(custodian_a, "2026-06-30", "600"),
        _actual(custodian_b, "2026-06-30", "400"),
    ]

    with pytest.raises(DuplicateCashPositionError) as excinfo:
        await _assemble([pe, custodian_a, custodian_b], navs)

    message = str(excinfo.value)
    assert "EUR" in message
    assert "Custodian A" in message
    assert "Custodian B" in message


@pytest.mark.asyncio
async def test_an_inactive_duplicate_cash_position_is_not_a_duplicate() -> None:
    """The universe is the *active* one — a closed account settles nothing."""
    pe = _investment(name="Fund I", currency="EUR")
    live = _investment(name="Cash EUR", currency="EUR", investment_type="cash")
    closed = _investment(
        name="Cash EUR — closed",
        currency="EUR",
        investment_type="cash",
        is_active=False,
    )
    navs = [
        _actual(pe, "2026-06-30", "200"),
        _actual(live, "2026-06-30", "600"),
        _actual(closed, "2025-12-31", "400"),
    ]

    frames = await _assemble([pe, live, closed], navs)

    assert set(frames.cash_paths) == {"EUR"}
    assert frames.cash_paths["EUR"].iloc[0] == _D("600")


# ---------------------------------------------------------------------------
# 6. The empty book
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_universe_has_no_plan_world() -> None:
    """No active investment: nothing to state, and no seam to state it about."""
    with pytest.raises(PlanSeamMissingError):
        await _assemble([], [])


@pytest.mark.asyncio
async def test_book_without_a_statement_has_no_seam() -> None:
    """Plan rows but no actual row: ``t₀`` is underivable, and is not invented.

    A seam taken from the wall clock would make the same book assemble
    differently tomorrow — the one thing the ADR-0104 §2 reproducibility
    contract forbids.
    """
    pe = _investment(name="Fund I", currency="EUR")

    with pytest.raises(PlanSeamMissingError) as excinfo:
        await _assemble([pe], [_plan(pe, "2026-09-30", "260")])

    assert "seam" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 7. Position-currency purity (ADR-0104 §3, N2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_conversion_happens_at_assembly() -> None:
    """Frames are assembled in position currency — bit-identically to the book.

    The USD paths carry the USD figures, unscaled and untouched: conversion
    belongs to the ADR-0099 §4 seam downstream, and this module holds no
    converter to do it with.
    """
    investments, navs, flows = _book()
    _pe, eq, _cash_eur, cash_usd = investments

    frames = await _assemble(investments, navs, flows)

    book_values = [
        nav.nav_value for nav in navs if nav.investment_id == eq.id and nav.nav_kind == "actual"
    ]
    assert list(frames.value_paths[eq.id]) == book_values
    assert all(isinstance(v, Decimal) for v in frames.value_paths[eq.id])

    usd_anchor = next(
        nav.nav_value
        for nav in navs
        if nav.investment_id == cash_usd.id and nav.nav_kind == "actual"
    )
    assert frames.cash_paths["USD"].iloc[0] == usd_anchor
    # A USD figure that had been converted at any plausible rate would not
    # still be its own Decimal, digit for digit.
    assert str(frames.cash_paths["USD"].iloc[0]) == "300.5000"


# ---------------------------------------------------------------------------
# Round-trip: real frames satisfy the pipeline, not only the anchor fixtures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assembled_frames_satisfy_the_identity_law() -> None:
    """``apply_overlay(frames, ())`` returns the very frames — the Baseline side.

    The anchors pin this on synthetic fixtures; this pins it on frames the
    assembly actually produced, which is what the Planning Desk will hand the
    pipeline (ADR-0104 §4).
    """
    investments, navs, flows = _book()

    frames = await _assemble(investments, navs, flows)

    assert apply_overlay(frames, ()) is frames


# ---------------------------------------------------------------------------
# 9. Ephemeral TA profiles for plan-less funds (ADR-0105 §4/§5/§6)
# ---------------------------------------------------------------------------
#
# The book of this section: `_book()`'s `pe` fund *has* a remaining plan flow,
# so the generator must never see it — that is the non-interference invariant,
# and it is why every test above still states the frames it always did. The
# fund added here is its opposite: a capital-account fund with a commitment and
# no plan flow at all.


def _plan_less_book(
    *, commitment: str | None = "1000", investment_type: str = "private_equity"
) -> tuple[
    list[InvestmentDTO],
    list[InvestmentNavDTO],
    list[InvestmentCashflowDTO],
    InvestmentDTO,
]:
    """A book whose one capital-account fund carries no remaining plan.

    Deliberately minimal: one fund, one cash position in its currency, one
    statement each. Everything the generator needs comes off it — the
    commitment from the row, ``called_to_date`` from the realised call,
    ``current_nav`` from the statement, ``t₀`` from the seam.
    """
    fund = _investment(
        name="Fund II",
        currency="EUR",
        investment_type=investment_type,
        commitment=commitment,
    )
    cash_eur = _investment(name="Cash EUR", currency="EUR", investment_type="cash")
    navs = [
        _actual(fund, "2026-06-30", "200"),
        _actual(cash_eur, "2026-06-30", "1000"),
    ]
    flows = [
        # Realised, so it seeds `called_to_date` and is *not* a remaining plan
        # flow: the fund is un-paceable and the generator runs.
        _flow(fund, "2026-02-28", "-250", "capital_call", flow_kind="actual"),
    ]
    return [fund, cash_eur], navs, flows, fund


@pytest.mark.asyncio
async def test_plan_less_capital_account_fund_gains_a_generated_profile() -> None:
    """A commitment and no plan ⇒ generated flows, marked ``'ta'`` (§4)."""
    investments, navs, flows, fund = _plan_less_book()

    frames = await _assemble(investments, navs, flows)

    # The fund is marked at the frame level — the ADR's "profile_source='ta'".
    assert frames.profile_source == {fund.id: PLAN_SOURCE_TA}

    generated = [f for f in frames.plan_flows if f.investment_id == fund.id]
    assert generated, "a committed plan-less fund must gain a profile"
    # Every generated flow is a *remaining* one: strictly after the seam, so it
    # is repaceable and cannot disturb realised history (ADR-0104 §5).
    assert all(f.as_of_date > frames.t0 for f in generated)
    assert all(f.currency == "EUR" for f in generated)
    # Calls debit, distributions credit (ADR-0043 sign convention).
    assert all(f.amount < 0 if f.flow_type == "capital_call" else f.amount > 0 for f in generated)


@pytest.mark.asyncio
async def test_generated_profile_draws_only_the_uncalled_remainder() -> None:
    """``called_to_date`` is read from the book and seeds the schedule (§4).

    The read path, asserted through its effect rather than by spying on it: the
    same book with a larger realised call has less commitment left to draw, so
    the generated calls are strictly smaller. A generator handed a zeroed
    ``called_to_date`` would produce the same profile for both.
    """
    investments, navs, flows, fund = _plan_less_book()
    frames = await _assemble(investments, navs, flows)

    drawn_more = [_flow(fund, "2026-02-28", "-900", "capital_call", flow_kind="actual")]
    heavier = await _assemble(investments, navs, drawn_more)

    def _calls(f) -> Decimal:
        return sum(
            (
                flow.amount
                for flow in f.plan_flows
                if flow.investment_id == fund.id and flow.flow_type == "capital_call"
            ),
            _D(0),
        )

    # Calls are negative, so "less drawn" is the larger (less negative) sum.
    assert _calls(heavier) > _calls(frames)


@pytest.mark.asyncio
async def test_plan_less_fund_without_a_commitment_stays_disabled() -> None:
    """No commitment ⇒ no profile, no mark, no flows (§Consequences).

    The un-modellable residue. The generator will not invent a commitment to
    model from, and the seam does not mark a fund it generated nothing for —
    the pacing row stays exactly as disabled as it was.
    """
    investments, navs, flows, fund = _plan_less_book(commitment=None)

    frames = await _assemble(investments, navs, flows)

    assert frames.profile_source == {}
    assert [f for f in frames.plan_flows if f.investment_id == fund.id] == []


@pytest.mark.asyncio
async def test_generated_flows_settle_against_the_cash_path() -> None:
    """Generated flows reach the cash path like manager-plan flows (§2/§5).

    ADR-0105 §2: "settle against the cash path of that currency **exactly like
    manager-plan flows** … no new settlement rule". A manager-plan flow gets
    there through the materialisation service; a generated flow never touches
    the book, so the seam applies the same ADR-0103 §6 projection in memory.

    Without this the cash lens would show no drawdown at all — and, worse,
    ``execute_repace_flows`` would lift a flow off a path it was never on,
    making a *deferred* capital call *raise* the balance.
    """
    investments, navs, flows, fund = _plan_less_book()

    frames = await _assemble(investments, navs, flows)

    path = frames.cash_paths["EUR"]
    # The anchor is untouched: settlement is strictly forward of the seam.
    assert path.loc[pd.Timestamp("2026-06-30")] == _D("1000")

    first_call = min(
        (
            f
            for f in frames.plan_flows
            if f.investment_id == fund.id and f.flow_type == "capital_call"
        ),
        key=lambda f: f.as_of_date,
    )
    # The balance at the first generated call is the anchor plus that call —
    # the §6 projection, `balance(t₀) + Σ signed plan flows`.
    assert path.loc[pd.Timestamp(first_call.as_of_date)] == (_D("1000") + first_call.amount)


@pytest.mark.asyncio
async def test_generation_never_touches_the_plan_nav_path() -> None:
    """The fund's ``value_paths`` entry stays ADR-0060 carry-forward (§5, E4).

    The generator seeds its recursion with ``current_nav`` and a reader may
    expect a NAV trajectory back out. There is none: v1 surfaces TA *flows*
    only, and the fund's plan NAV is the same carried-forward actual it was
    before the profile existed. A J-curve the platform cannot source is a
    modelling claim, and this is where it would leak in.
    """
    investments, navs, flows, fund = _plan_less_book()

    frames = await _assemble(investments, navs, flows)

    assert frames.profile_source == {fund.id: PLAN_SOURCE_TA}
    # The ADR-0060 cross-stream fallback, unchanged: the single actual
    # statement, and nothing the recursion produced.
    pd.testing.assert_series_equal(
        frames.value_paths[fund.id],
        pd.Series([_D("200")], index=_index("2026-06-30"), dtype="object"),
        check_exact=True,
    )


@pytest.mark.asyncio
async def test_generated_flows_are_only_calls_and_distributions() -> None:
    """No ``investor_flow`` can enter through the generator (§6, exemption).

    The exemption invariant holds for generated flows **by construction**: the
    generator emits two flow types and neither is exempt, so no transformation
    can ever be asked to move the mandate's own capital because TA invented
    some. The mandate's real exempt flows travel beside them, untouched — the
    seam filters nothing (ADR-0104 §1).
    """
    investments, navs, flows, fund = _plan_less_book()
    _fund, cash_eur = investments
    flows = [*flows, _flow(cash_eur, "2026-12-31", "500", "investor_flow")]

    frames = await _assemble(investments, navs, flows)

    assert {f.flow_type for f in frames.plan_flows if f.investment_id == fund.id} == {
        "capital_call",
        "distribution",
    }
    # The mandate's own flow reaches the frames unfiltered, and is not marked.
    assert [
        (f.investment_id, f.flow_type) for f in frames.plan_flows if f.flow_type == "investor_flow"
    ] == [(cash_eur.id, "investor_flow")]
    assert cash_eur.id not in frames.profile_source


@pytest.mark.asyncio
async def test_periodisation_re_cuts_the_generated_flow_dates() -> None:
    """The grid is the requested one (§2) — the same economics, different dates.

    The one thing the view parameter reaching the seam buys: a generated profile
    sits on the same period-end grid a manager plan would, so switching the
    Planning Desk to monthly moves the flows with the columns.
    """
    investments, navs, flows, fund = _plan_less_book()

    quarterly = await _assemble(investments, navs, flows, periodisation=Periodisation.QUARTERLY)
    monthly = await _assemble(investments, navs, flows, periodisation=Periodisation.MONTHLY)

    def _dates(frames) -> list[date]:
        return sorted(f.as_of_date for f in frames.plan_flows if f.investment_id == fund.id)

    assert _dates(quarterly) != _dates(monthly)
    # Both are marked, and both are profiles of the same fund: only the grid
    # moved, so each carries the same number of flows.
    assert monthly.profile_source == quarterly.profile_source == {fund.id: PLAN_SOURCE_TA}


# ---------------------------------------------------------------------------
# 9a. Non-interference (ADR-0105 §6, binding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fund_with_a_manager_plan_is_byte_identical_with_ta_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fund *with* a plan produces the same frames whether TA runs or not.

    The load-bearing safety property (ADR-0105 §6). D18's never-calibrate rule
    holds by construction — the generator runs only where the repaceable set is
    empty — and this pins the construction rather than trusting it: the module
    is replaced with a fake that **fails if it is called at all**, and the
    frames are compared against the ones the real seam produces.

    The fund here is `_plan_less_book()`'s, made *planned*: same commitment,
    same statement, same realised call — plus one remaining manager-plan flow.
    That flow is the only difference from the fund the test above generates a
    profile for, so it is the only thing standing between this fund and the
    generator.
    """
    investments, navs, flows, fund = _plan_less_book()
    flows = [*flows, _flow(fund, "2026-09-30", "-100", "capital_call")]

    with_ta = await _assemble(investments, navs, flows)

    def _explode(**_kwargs):
        raise AssertionError(
            "the TA generator was invoked for a fund that has a manager plan: "
            "ADR-0104 D18 forbids calibrating TA to an existing plan, and "
            "ADR-0105 §6 requires such a fund's frames to be byte-identical "
            "whether the TA module runs or not"
        )

    monkeypatch.setattr("services.investments.plan_world.generate_remaining_profile", _explode)
    without_ta = await _assemble(investments, navs, flows)

    # The generator was never called, so nothing was marked...
    assert with_ta.profile_source == without_ta.profile_source == {}
    # ...and every frame is identical, flows and paths alike.
    assert with_ta.t0 == without_ta.t0
    assert with_ta.plan_flows == without_ta.plan_flows
    assert set(with_ta.value_paths) == set(without_ta.value_paths)
    for investment_id, path in with_ta.value_paths.items():
        pd.testing.assert_series_equal(
            path, without_ta.value_paths[investment_id], check_exact=True
        )
    assert set(with_ta.cash_paths) == set(without_ta.cash_paths)
    for currency, path in with_ta.cash_paths.items():
        pd.testing.assert_series_equal(path, without_ta.cash_paths[currency], check_exact=True)


@pytest.mark.asyncio
async def test_a_book_with_nothing_to_generate_gets_its_frames_back_uncopied() -> None:
    """Non-interference at its strongest: the same object, not a rebuild.

    `_book()` has no fund the generator can model — its capital-account fund is
    planned, and its listed mandate is not a capital-account archetype at all.
    So the TA hook is a no-op, and it says so by returning the frames it was
    handed rather than reconstructing an equal set.
    """
    investments, navs, flows = _book()

    frames = await _assemble(investments, navs, flows)

    assert frames.profile_source == {}
    assert apply_overlay(frames, ()) is frames


@pytest.mark.asyncio
async def test_a_non_capital_account_fund_is_never_generated_for() -> None:
    """Scope is the imported predicate's, not a type list of our own (§4).

    A listed mandate has no manager plan either — and no drawdown profile to
    model, so `capital_account_ids` excludes it. Were the scope restated here it
    would be this case that broke: the generator *raises* for a non-capital-
    account type rather than defaulting, so a widened scope would take the whole
    plan world down with it.
    """
    equity = _investment(
        name="Global Equity",
        currency="EUR",
        investment_type="listed_equity",
        commitment="1000",
    )
    cash_eur = _investment(name="Cash EUR", currency="EUR", investment_type="cash")
    navs = [
        _actual(equity, "2026-06-30", "500"),
        _actual(cash_eur, "2026-06-30", "1000"),
    ]

    frames = await _assemble([equity, cash_eur], navs, [])

    assert frames.profile_source == {}
    assert frames.plan_flows == ()


# ---------------------------------------------------------------------------
# 9b. Book silence (ADR-0105 §4/§6 — "nothing is written", binding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assembling_a_ta_plan_world_writes_nothing() -> None:
    """Zero repository writes, asserted explicitly rather than structurally.

    ADR-0105 §4 is binding that a generated profile is written nowhere: no
    ``investment_cashflows`` row, no ``investment_navs`` row, no ``source``
    marker, and no entry in the Strand-1 §2.6 disjointness registry — there is
    no second writer to be disjoint from. The invariant is covered structurally
    by the seam's read-only fakes; the ADR asks for it once, out loud, so a
    future writer added here trips a test rather than a review.

    Every write method of the three repositories is stood up as a trap. Only the
    reads the fakes serve may fire.
    """
    investments, navs, flows, _fund = _plan_less_book()

    class _WriteTrap:
        """Any attribute that is not a served read is a write attempt."""

        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            if name.startswith(("list_", "get_", "find_", "count_")):
                return getattr(self._inner, name)
            raise AssertionError(
                f"the plan-world seam reached for {name!r}: assembling a plan "
                f"world with TA-profiled funds performs zero writes "
                f"(ADR-0105 §4/§6)"
            )

    frames = await assemble_plan_frames(
        investments=_WriteTrap(_FakeInvestmentRepository(investments)),  # type: ignore[arg-type]
        navs=_WriteTrap(_FakeNavRepository(navs)),  # type: ignore[arg-type]
        cashflows=_WriteTrap(_FakeCashflowRepository(flows)),  # type: ignore[arg-type]
        periodisation=Periodisation.QUARTERLY,
    )

    # The profile exists in the frames and nowhere else.
    assert frames.profile_source != {}
