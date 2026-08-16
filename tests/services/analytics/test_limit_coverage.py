# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the limit-coverage engine (Kickoff #2 §6.2, Kickoff #3a).

The engine consumes repository DTOs directly: the test helpers below
construct flat DTO lists (investments, NAV streams, limit sets) without
touching the DB or the Excel importer, so the tests run in any
environment in which the analytics-purity guard is green.

Since ADR-0103 §2 there is no AUM input to fabricate: the denominator is
``Σ NAV`` over the book itself. Where a test needs a class to sit at a
given percentage, it *holds* the remainder as an explicit cash position
(:func:`_cash_ballast`) — which is what the retired residual always was.

Test categories mirror Kickoff #2 §6.2:

* A. Cut-over logic (plan/actual selection around ``cut_over``).
* B. Denominator resolution — ``Σ NAV`` over the book, cash rows
  included (ADR-0103 §2/§8); carry-forward is inherited from the NAVs,
  and a valueless book raises.
* C. Limit-set selection (latest ``effective_from <= t``,
  no-effective-set raises).
* D. Aggregation and classification (status thresholds, unallocated
  bucket, NO_LIMIT bucket, empty class still emitted).
* E. Output structure (column order, row order, set_history,
  ``aum_used`` series).
* F. Decimal precision (quantize to four decimals, no float leakage).
* G. Stateless / reentrancy.
* H. Input validation (sorted family lists, warn_threshold range,
  empty evaluation dates).

§6.3's Excel-reference parity test is included as a skipped placeholder
(see :func:`test_engine_matches_excel_reference_three_dates`); the
helper schema lives in :mod:`tests.services.analytics._reference_loader`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
)
from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.limits_repository import LimitSetDTO
from services.analytics._dtos import (
    InvestmentWithClassCodeDTO,
    LimitSetWithLimitsDTO,
)
from services.analytics.limit_coverage import (
    CoverageEngineResult,
    FamilyCoverageResult,
    compute_coverage,
)


# ---------------------------------------------------------------------
# Helpers — DTO fabrication
# ---------------------------------------------------------------------


def _D(value: str | int | float) -> Decimal:
    """Compact ``Decimal`` constructor for test setup readability."""
    return Decimal(str(value))


_NOW: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc)
_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID: UUID = UUID("00000000-0000-0000-0000-0000000000aa")


def _make_inv(
    *,
    asset_class_code: str | None = "saa_a",
    anlv_code: str | None = "anlv_a",
    investment_id: UUID | None = None,
    investment_type: str = "private_equity",
) -> InvestmentWithClassCodeDTO:
    """Construct an :class:`InvestmentWithClassCodeDTO` with sane defaults.

    Fields the engine does not read (``manager_name``,
    ``commitment_amount``, audit columns, etc.) carry placeholder
    values; ``asset_class_code`` and ``anlv_code`` are the values the
    engine actually classifies on.

    ``investment_type`` is settable so a test can hold a ``'cash'`` row, but
    the engine must not *read* it — it is investment-type-blind by contract
    (ADR-0013/0045; the AST guard in ``tests/regression`` enforces it). Cash
    behaves here exactly like any other investment, which is the point.
    """
    inv = InvestmentDTO(
        id=investment_id or uuid4(),
        tenant_id=_TENANT_ID,
        name="Test Investment",
        investment_type=investment_type,
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency="EUR",
        vintage_year=None,
        commitment_amount=None,
        is_active=True,
        type_specific_data=None,
        created_by=_USER_ID,
        created_at=_NOW,
        updated_at=_NOW,
        anlv_code=anlv_code,
    )
    return InvestmentWithClassCodeDTO(
        investment=inv,
        asset_class_code=asset_class_code,
    )


def _make_set(
    *,
    family: str,
    effective_from: date,
    limits: dict[str, Decimal],
    label: str | None = None,
    set_id: UUID | None = None,
) -> LimitSetWithLimitsDTO:
    """Construct a :class:`LimitSetWithLimitsDTO` with sane defaults."""
    dto = LimitSetDTO(
        id=set_id or uuid4(),
        tenant_id=_TENANT_ID,
        family=family,
        effective_from=effective_from,
        label=label or f"{family} @ {effective_from}",
        notes=None,
        created_by=_USER_ID,
        created_at=_NOW,
        updated_at=_NOW,
    )
    return LimitSetWithLimitsDTO(set=dto, limits=limits)


def _make_nav_stream(
    investment_id: UUID,
    nav_kind: str,
    series: dict[date, Decimal],
) -> list[InvestmentNavDTO]:
    """Build an :class:`InvestmentNavDTO` stream for one investment."""
    return [
        InvestmentNavDTO(
            id=uuid4(),
            tenant_id=_TENANT_ID,
            investment_id=investment_id,
            as_of_date=d,
            nav_value=v,
            currency="EUR",
            nav_kind=nav_kind,
            source=None,
            created_by=_USER_ID,
            created_at=_NOW,
            updated_at=_NOW,
        )
        for d, v in series.items()
    ]


def _attach_navs(
    inv: InvestmentWithClassCodeDTO,
    *,
    actual: dict[date, Decimal] | None = None,
    plan: dict[date, Decimal] | None = None,
) -> tuple[list[InvestmentNavDTO], list[InvestmentNavDTO]]:
    """Build ``(actual_stream, plan_stream)`` for one investment."""
    actual_stream = _make_nav_stream(inv.investment.id, "actual", actual) if actual else []
    plan_stream = _make_nav_stream(inv.investment.id, "plan", plan) if plan else []
    return actual_stream, plan_stream


#: The book total every denominator-sensitive test sizes its cash ballast
#: against. Before ADR-0103 this was the ``portfolio_aum`` row; now it is a
#: property of the book, so the tests state it by *holding* it.
_BOOK_TOTAL: Decimal = _D("1000000")


def _cash_ballast(
    navs: dict[date, Decimal],
) -> tuple[InvestmentWithClassCodeDTO, dict[UUID, list[InvestmentNavDTO]]]:
    """The explicit cash position that used to be the residual.

    ADR-0103 §2 retires ``aum − Σ nav``: the float is modelled, not inferred.
    A test that wants a class to sit at 30 % of a 1,000,000 book therefore
    *holds* the other 700,000 — as cash, which is what it always was. Cash is
    an ordinary member of the denominator (ADR-0103 §8: "coverage/limits:
    cash rows remain included"), and it is classified like any other
    investment, so it produces an ordinary limit row rather than a synthetic
    bucket.

    Returns:
        The cash investment and its ``{id: actual_stream}`` NAV map, ready to
        merge into a test's book.
    """
    inv = _make_inv(
        asset_class_code="saa_cash",
        anlv_code="anlv_cash",
        investment_type="cash",
    )
    stream = _make_nav_stream(inv.investment.id, "actual", navs)
    return inv, {inv.investment.id: stream}


def _simple_call(
    *,
    investments: list[InvestmentWithClassCodeDTO] | None = None,
    actual_navs: dict[UUID, list[InvestmentNavDTO]] | None = None,
    plan_navs: dict[UUID, list[InvestmentNavDTO]] | None = None,
    cut_over: date = date(2025, 12, 31),
    saa_sets: list[LimitSetWithLimitsDTO] | None = None,
    anlv_sets: list[LimitSetWithLimitsDTO] | None = None,
    evaluation_dates: list[date] | None = None,
    warn_threshold_pct: Decimal | None = None,
) -> CoverageEngineResult:
    """Invoke ``compute_coverage`` with minimal-but-complete inputs.

    Since ADR-0103 §2 there is no AUM input: the denominator is ``Σ NAV``
    over ``investments``. A test that needs a particular coverage percentage
    tops its book up with :func:`_cash_ballast`.
    """
    if investments is None:
        inv = _make_inv(asset_class_code="saa_a", anlv_code="anlv_a")
        investments = [inv]
        if actual_navs is None and plan_navs is None:
            actual_navs = {
                inv.investment.id: _make_nav_stream(
                    inv.investment.id,
                    "actual",
                    {date(2024, 6, 30): _D("300000")},
                )
            }
    if actual_navs is None:
        actual_navs = {}
    if plan_navs is None:
        plan_navs = {}
    if saa_sets is None:
        saa_sets = [
            _make_set(
                family="saa",
                effective_from=date(2020, 1, 1),
                limits={
                    "saa_a": _D("50.0"),
                    "saa_b": _D("30.0"),
                    "saa_cash": _D("100.0"),
                },
            )
        ]
    if anlv_sets is None:
        anlv_sets = [
            _make_set(
                family="anlv",
                effective_from=date(2020, 1, 1),
                limits={
                    "anlv_a": _D("60.0"),
                    "anlv_b": _D("25.0"),
                    "anlv_cash": _D("100.0"),
                },
            )
        ]
    if evaluation_dates is None:
        evaluation_dates = [date(2024, 6, 30)]
    kwargs: dict[str, object] = dict(
        investments=investments,
        actual_navs=actual_navs,
        plan_navs=plan_navs,
        cut_over=cut_over,
        saa_sets=saa_sets,
        anlv_sets=anlv_sets,
        evaluation_dates=evaluation_dates,
    )
    if warn_threshold_pct is not None:
        kwargs["warn_threshold_pct"] = warn_threshold_pct
    return compute_coverage(**kwargs)  # type: ignore[arg-type]


def _row_for(
    df: pd.DataFrame,
    *,
    as_of_date: date,
    class_key: str,
) -> pd.Series:
    """Locate the single row matching ``(as_of_date, class_key)``."""
    mask = (df["as_of_date"] == pd.Timestamp(as_of_date)) & (df["class_key"] == class_key)
    matches = df[mask]
    assert len(matches) == 1, (
        f"expected exactly one row for ({as_of_date}, {class_key}); got {len(matches)}"
    )
    return matches.iloc[0]


# ---------------------------------------------------------------------
# A. Cut-over logic
# ---------------------------------------------------------------------


def test_actual_used_at_exactly_cut_over() -> None:
    """``t == cut_over`` reads from ``actual_navs`` (boundary is inclusive)."""
    t = date(2025, 12, 31)
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={t: _D("400000")},
        plan={t: _D("999999")},
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=t,
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("400000.0000")


def test_plan_used_one_day_past_cut_over() -> None:
    """``t == cut_over + 1`` reads from ``plan_navs``."""
    cut = date(2025, 12, 31)
    t = date(2026, 1, 1)
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={t: _D("999999")},
        plan={t: _D("500000")},
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=cut,
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("500000.0000")


def test_missing_actual_at_or_before_cut_over_raises_out_of_range() -> None:
    """Both streams empty at or before ``t <= cut_over`` raises ``CoverageInputOutOfRange``.

    Under the ADR-0060 carry-forward semantics the engine first tries
    the cut-over-preferred (actual) stream and then falls back to the
    plan stream; the hard error is only raised when *both* are empty
    at or before ``t``. The plan entry sits strictly after ``t`` so
    cross-stream fallback cannot rescue the lookup.
    """
    t = date(2024, 6, 30)
    inv = _make_inv()
    # Plan-only entry strictly *after* t — cross-stream fallback must
    # not pick it up (carry-forward only considers d <= t).
    actual, plan = _attach_navs(inv, plan={date(2024, 12, 31): _D("500000")})
    with pytest.raises(CoverageInputOutOfRange, match="no NAV at or before"):
        _simple_call(
            investments=[inv],
            actual_navs={inv.investment.id: actual},
            plan_navs={inv.investment.id: plan},
            cut_over=date(2025, 12, 31),
            evaluation_dates=[t],
        )


def test_missing_plan_past_cut_over_raises_out_of_range() -> None:
    """Both streams empty at or before ``t > cut_over`` raises ``CoverageInputOutOfRange``.

    Symmetric counterpart to the previous test: the cut-over-preferred
    plan stream is empty and the actual stream's only entry sits
    strictly after ``t``, so cross-stream fallback cannot succeed.
    """
    cut = date(2025, 12, 31)
    t = date(2026, 1, 1)
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={date(2026, 6, 30): _D("999999")})
    with pytest.raises(CoverageInputOutOfRange, match="no NAV at or before"):
        _simple_call(
            investments=[inv],
            actual_navs={inv.investment.id: actual},
            plan_navs={inv.investment.id: plan},
            cut_over=cut,
            evaluation_dates=[t],
        )


# ---------------------------------------------------------------------
# A2. NAV carry-forward and cross-stream fallback (ADR-0060)
# ---------------------------------------------------------------------


def test_carry_forward_within_preferred_stream() -> None:
    """Carry-forward fills gaps in the cut-over-preferred (actual) stream."""
    t1 = date(2024, 1, 31)
    t2 = date(2024, 2, 29)
    t3 = date(2024, 3, 31)
    inv = _make_inv()
    # Only one actual entry at t1; t2 and t3 must inherit it via
    # carry-forward.
    actual, plan = _attach_navs(inv, actual={t1: _D("300000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=date(2025, 12, 31),
        evaluation_dates=[t1, t2, t3],
    )
    for t in (t1, t2, t3):
        row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
        assert row["nav_sum_eur"] == _D("300000.0000"), f"at {t}"


def test_carry_forward_within_plan_stream() -> None:
    """Carry-forward also applies to the plan stream beyond ``cut_over``."""
    cut = date(2024, 1, 31)
    t1 = date(2024, 2, 29)
    t2 = date(2024, 3, 31)
    inv = _make_inv()
    # Only one plan entry at t1; t2 must inherit it via carry-forward.
    actual, plan = _attach_navs(inv, plan={t1: _D("400000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=cut,
        evaluation_dates=[t1, t2],
    )
    for t in (t1, t2):
        row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
        assert row["nav_sum_eur"] == _D("400000.0000"), f"at {t}"


def test_cross_stream_fallback_plan_to_actual_no_plan_navs() -> None:
    """Past ``cut_over`` with no plan NAVs falls back to the actual stream.

    Reproduces the V21 data profile: a liquid investment that is
    daily-valued (actual NAVs) but never forecasted (no plan NAVs).
    Beyond ``cut_over`` the engine must carry forward the last actual
    NAV rather than abort with ``CoverageInputOutOfRange``.
    """
    cut = date(2024, 1, 31)
    last_actual = date(2024, 1, 15)
    t = date(2024, 6, 30)  # well past cut_over, no plan NAV exists
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={last_actual: _D("250000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},  # empty plan stream
        cut_over=cut,
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("250000.0000")


def test_cross_stream_fallback_actual_to_plan_no_actual_navs() -> None:
    """Before ``cut_over`` with no actual NAVs falls back to the plan stream.

    Symmetric counterpart to the V21-style case: an investment that has
    only plan NAVs (semantically unusual but valid). For
    ``t <= cut_over`` the engine prefers actual but must fall back to
    plan when actual is empty.
    """
    cut = date(2024, 12, 31)
    plan_date = date(2024, 1, 15)
    t = date(2024, 6, 30)  # <= cut_over
    inv = _make_inv()
    actual, plan = _attach_navs(inv, plan={plan_date: _D("150000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},  # empty actual stream
        plan_navs={inv.investment.id: plan},
        cut_over=cut,
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("150000.0000")


def test_both_streams_empty_before_t_raises() -> None:
    """Both streams entry-free at or before ``t`` raises ``CoverageInputOutOfRange``.

    Models an investment that did not exist at ``t``: its first
    observation in either stream sits strictly after ``t``. There is
    no historical value to carry forward, so the engine must raise.
    """
    t = date(2024, 6, 30)
    inv = _make_inv()
    # Both streams carry only post-t entries.
    actual, plan = _attach_navs(
        inv,
        actual={date(2024, 12, 1): _D("100000")},
        plan={date(2025, 1, 1): _D("110000")},
    )
    with pytest.raises(
        CoverageInputOutOfRange,
        match=r"no NAV at or before .* in either stream",
    ):
        _simple_call(
            investments=[inv],
            actual_navs={inv.investment.id: actual},
            plan_navs={inv.investment.id: plan},
            cut_over=date(2025, 12, 31),
            evaluation_dates=[t],
        )


def test_exact_match_still_wins_over_carry_forward() -> None:
    """When an entry sits exactly at ``t`` it is used, not an earlier one.

    Guards against an off-by-one in the ``<= t`` filter that could
    yield "latest entry strictly before ``t``" instead.
    """
    t1 = date(2024, 1, 31)
    t2 = date(2024, 6, 30)
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={t1: _D("100000"), t2: _D("200000")},
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=date(2025, 12, 31),
        evaluation_dates=[t2],
    )
    row = _row_for(result.saa.coverage, as_of_date=t2, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("200000.0000")


def test_cut_over_boundary_picks_actual_under_carry_forward() -> None:
    """At ``t == cut_over`` the actual stream stays primary under carry-forward.

    Sister-case of :func:`test_actual_used_at_exactly_cut_over` but
    with no exact-match entry — both streams have observations
    strictly before ``cut_over``. The engine must carry forward from
    actual, never silently switch primary to plan at the boundary.
    """
    cut = date(2024, 6, 30)
    earlier = date(2024, 1, 31)
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={earlier: _D("400000")},
        plan={earlier: _D("999999")},
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=cut,
        evaluation_dates=[cut],
    )
    row = _row_for(result.saa.coverage, as_of_date=cut, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("400000.0000")


def test_carry_forward_does_not_jump_past_t() -> None:
    """Stream entries strictly after ``t`` must be ignored by carry-forward."""
    before = date(2024, 1, 31)
    middle = date(2024, 6, 30)  # the evaluation date
    after = date(2024, 9, 30)
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={
            before: _D("100000"),
            after: _D("999999"),  # must NOT be selected
        },
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=date(2025, 12, 31),
        evaluation_dates=[middle],
    )
    row = _row_for(result.saa.coverage, as_of_date=middle, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("100000.0000")


def test_liquidation_zero_persisted_through_carry_forward() -> None:
    """An explicit ``nav_value == 0`` (liquidation) propagates via carry-forward.

    Guards the engine against the classic ``if value:`` mistake — a
    liquidated position carries ``Decimal(0)``, which is falsy in
    Python but is the engine's authoritative value at and after the
    liquidation date.
    """
    liquidation = date(2024, 1, 31)
    later = date(2024, 6, 30)
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={liquidation: _D("0")})
    # The book holds cash alongside: since ADR-0103 §2 the denominator *is*
    # the book, and a book whose every position is zero has nothing to divide
    # by. The liquidation proceeds have to sit somewhere — and under this ADR
    # that somewhere is modelled rather than left as an unstated float.
    cash, cash_navs = _cash_ballast({liquidation: _BOOK_TOTAL})
    result = _simple_call(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        plan_navs={inv.investment.id: plan},
        cut_over=date(2025, 12, 31),
        evaluation_dates=[later],
    )
    row = _row_for(result.saa.coverage, as_of_date=later, class_key="saa_a")
    assert row["nav_sum_eur"] == _D("0.0000")


# ---------------------------------------------------------------------
# B. Denominator resolution — Σ NAV (ADR-0103 §2)
# ---------------------------------------------------------------------


def test_denominator_is_sum_of_navs() -> None:
    """``aum_used`` is Σ NAV over the book — no AUM input exists."""
    t = date(2024, 6, 30)
    inv_a = _make_inv(asset_class_code="saa_a")
    inv_b = _make_inv(asset_class_code="saa_b")
    a_actual, _ = _attach_navs(inv_a, actual={t: _D("300000")})
    b_actual, _ = _attach_navs(inv_b, actual={t: _D("200000")})
    result = _simple_call(
        investments=[inv_a, inv_b],
        actual_navs={
            inv_a.investment.id: a_actual,
            inv_b.investment.id: b_actual,
        },
        evaluation_dates=[t],
    )
    assert result.aum_used[pd.Timestamp(t)] == _D("500000")


def test_denominator_includes_cash_rows() -> None:
    """Cash is an ordinary member of the denominator (ADR-0103 §8).

    The book below is the pre-ADR-0103 world made explicit: 300,000 invested
    against a 1,000,000 book, with the other 700,000 held as cash. The
    coverage the engine reports — 30 % — is exactly what the old residual
    formulation produced when it divided by a persisted AUM row of
    1,000,000. Same number, one fewer table.
    """
    t = date(2024, 6, 30)
    inv = _make_inv(asset_class_code="saa_a")
    actual, _ = _attach_navs(inv, actual={t: _D("300000")})
    cash, cash_navs = _cash_ballast({t: _BOOK_TOTAL - _D("300000")})

    result = _simple_call(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        evaluation_dates=[t],
    )

    assert result.aum_used[pd.Timestamp(t)] == _BOOK_TOTAL
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["coverage_pct"] == _D("30.0000")
    # And the cash row is a limit row like any other — no synthetic bucket.
    cash_row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_cash")
    assert cash_row["nav_sum_eur"] == _D("700000.0000")
    assert cash_row["coverage_pct"] == _D("70.0000")


def test_denominator_carries_nav_forward() -> None:
    """A date with no NAV entry carries the book's last level forward.

    The denominator inherits ADR-0060 carry-forward from the NAVs it sums;
    it has no separate carry-forward rule of its own any more.
    """
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={date(2024, 1, 1): _D("200000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[date(2024, 1, 1), date(2024, 6, 30)],
    )
    assert result.aum_used[pd.Timestamp(date(2024, 6, 30))] == _D("200000")


def test_valueless_book_raises_missing() -> None:
    """A book whose every position is zero has no denominator to divide by."""
    t = date(2024, 6, 30)
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("0")})
    with pytest.raises(CoverageInputMissing, match="carries no value"):
        _simple_call(
            investments=[inv],
            actual_navs={inv.investment.id: actual},
            plan_navs={inv.investment.id: plan},
            evaluation_dates=[t],
        )


# ---------------------------------------------------------------------
# C. Limit-set selection
# ---------------------------------------------------------------------


def test_effective_set_picks_latest_at_or_before_t() -> None:
    """Selection picks the set with the maximum ``effective_from <= t``."""
    set_a = _make_set(
        family="saa",
        effective_from=date(2023, 1, 1),
        limits={"saa_a": _D("40.0")},
        label="SAA v1",
    )
    set_b = _make_set(
        family="saa",
        effective_from=date(2025, 7, 1),
        limits={"saa_a": _D("55.0")},
        label="SAA v2",
    )
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={
            date(2024, 6, 30): _D("300000"),
            date(2025, 7, 1): _D("300000"),
        },
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        cut_over=date(2026, 12, 31),
        saa_sets=[set_a, set_b],
        evaluation_dates=[date(2024, 6, 30), date(2025, 7, 1)],
    )
    row_v1 = _row_for(result.saa.coverage, as_of_date=date(2024, 6, 30), class_key="saa_a")
    row_v2 = _row_for(result.saa.coverage, as_of_date=date(2025, 7, 1), class_key="saa_a")
    assert row_v1["max_pct"] == _D("40.0")
    assert row_v2["max_pct"] == _D("55.0")


def test_no_effective_set_raises_limit_set_not_effective() -> None:
    """All ``effective_from`` later than ``t`` raises ``LimitSetNotEffective``."""
    late_set = _make_set(
        family="saa",
        effective_from=date(2026, 1, 1),
        limits={"saa_a": _D("40.0")},
    )
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={date(2024, 6, 30): _D("100000")})
    with pytest.raises(LimitSetNotEffective, match="No limit set in force"):
        _simple_call(
            investments=[inv],
            actual_navs={inv.investment.id: actual},
            plan_navs={inv.investment.id: plan},
            cut_over=date(2025, 12, 31),
            saa_sets=[late_set],
            evaluation_dates=[date(2024, 6, 30)],
        )


# ---------------------------------------------------------------------
# D. Aggregation and classification
# ---------------------------------------------------------------------


def test_status_ok_at_low_coverage() -> None:
    """Coverage well below ``warn_floor`` is ``OK``."""
    t = date(2024, 6, 30)
    # Coverage = 10 %, max = 50 %, warn_floor = 45 % → OK.
    # 100,000 invested in a 1,000,000 book; the balance is held as cash.
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("100000")})
    cash, cash_navs = _cash_ballast({t: _BOOK_TOTAL - _D("100000")})
    result = _simple_call(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["status"] == "OK"
    assert row["coverage_pct"] == _D("10.0000")


def test_status_warn_just_above_threshold() -> None:
    """Coverage just above ``warn_floor`` flips to ``WARN``."""
    t = date(2024, 6, 30)
    # max = 50, warn = 90 % of 50 = 45. Coverage = 46 % → WARN.
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("460000")})
    cash, cash_navs = _cash_ballast({t: _BOOK_TOTAL - _D("460000")})
    result = _simple_call(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["status"] == "WARN"


def test_status_breach_above_limit() -> None:
    """Coverage strictly above ``max_pct`` is ``BREACH``."""
    t = date(2024, 6, 30)
    # max = 50, coverage = 60 → BREACH; headroom = -100_000 EUR.
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("600000")})
    cash, cash_navs = _cash_ballast({t: _BOOK_TOTAL - _D("600000")})
    result = _simple_call(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    assert row["status"] == "BREACH"
    assert row["headroom_eur"] == _D("-100000.0000")


def test_status_warn_threshold_parameter_changes_boundary() -> None:
    """A configured ``warn_threshold_pct=80`` rebases the WARN boundary."""
    t = date(2024, 6, 30)
    # max = 50, warn = 80 % of 50 = 40. Coverage = 41 → WARN with 80%-threshold,
    # but OK under the default 90 %-threshold.
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("410000")})
    cash, cash_navs = _cash_ballast({t: _BOOK_TOTAL - _D("410000")})
    nav_kwargs: dict[str, object] = dict(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    default = _simple_call(**nav_kwargs)  # type: ignore[arg-type]
    custom = _simple_call(
        **nav_kwargs,  # type: ignore[arg-type]
        warn_threshold_pct=_D("80.0"),
    )
    assert _row_for(default.saa.coverage, as_of_date=t, class_key="saa_a")["status"] == "OK"
    assert _row_for(custom.saa.coverage, as_of_date=t, class_key="saa_a")["status"] == "WARN"


def test_unallocated_bucket_aggregates_null_class() -> None:
    """``anlv_code is None`` rolls up into a single ``'unallocated'`` row."""
    t = date(2024, 6, 30)
    inv_a = _make_inv(asset_class_code="saa_a", anlv_code=None)
    inv_b = _make_inv(asset_class_code="saa_a", anlv_code=None)
    actual_a, _ = _attach_navs(inv_a, actual={t: _D("100000")})
    actual_b, _ = _attach_navs(inv_b, actual={t: _D("150000")})
    result = _simple_call(
        investments=[inv_a, inv_b],
        actual_navs={
            inv_a.investment.id: actual_a,
            inv_b.investment.id: actual_b,
        },
        plan_navs={},
        evaluation_dates=[t],
    )
    row = _row_for(result.anlv.coverage, as_of_date=t, class_key="unallocated")
    assert row["status"] == "UNALLOCATED"
    assert row["nav_sum_eur"] == _D("250000.0000")
    assert row["max_pct"] is None
    assert row["headroom_eur"] is None


def test_unallocated_row_absent_when_no_unallocated_nav() -> None:
    """No ``'unallocated'`` row when every investment is classified."""
    t = date(2024, 6, 30)
    inv = _make_inv(asset_class_code="saa_a", anlv_code="anlv_a")
    actual, plan = _attach_navs(inv, actual={t: _D("250000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    assert "unallocated" not in result.saa.coverage["class_key"].tolist()
    assert "unallocated" not in result.anlv.coverage["class_key"].tolist()


def test_no_limit_bucket_for_invested_class_not_in_set() -> None:
    """A class invested but absent from the effective set emits NO_LIMIT."""
    t = date(2024, 6, 30)
    inv = _make_inv(asset_class_code="saa_unknown", anlv_code="anlv_a")
    actual, plan = _attach_navs(inv, actual={t: _D("200000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_unknown")
    assert row["status"] == "NO_LIMIT"
    assert row["max_pct"] is None
    assert row["headroom_eur"] is None
    assert row["nav_sum_eur"] == _D("200000.0000")


def test_empty_class_in_set_still_emitted() -> None:
    """A class in the set with NAV=0 still appears as an ``OK`` row."""
    t = date(2024, 6, 30)
    # saa_b appears in the limit set but no investment invests in it.
    result = _simple_call(evaluation_dates=[t])
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_b")
    assert row["nav_sum_eur"] == _D("0.0000")
    assert row["coverage_pct"] == _D("0.0000")
    assert row["status"] == "OK"


# ---------------------------------------------------------------------
# E. Output structure
# ---------------------------------------------------------------------


def test_dataframe_columns_and_order() -> None:
    """Engine emits columns in the documented order (§4.2)."""
    expected = [
        "as_of_date",
        "class_key",
        "max_pct",
        "nav_sum_eur",
        "coverage_pct",
        "headroom_eur",
        "status",
    ]
    result = _simple_call()
    assert list(result.saa.coverage.columns) == expected
    assert list(result.anlv.coverage.columns) == expected


def test_row_order_within_date_follows_set_keys_order() -> None:
    """The order of classes in ``limits`` dictates the row order."""
    t = date(2024, 6, 30)
    # Insertion order: Z first, then A, then M — should be preserved.
    saa_set = _make_set(
        family="saa",
        effective_from=date(2020, 1, 1),
        limits={
            "saa_Z": _D("20.0"),
            "saa_A": _D("30.0"),
            "saa_M": _D("25.0"),
        },
    )
    inv = _make_inv(asset_class_code="saa_A")
    actual, plan = _attach_navs(inv, actual={t: _D("100000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
        saa_sets=[saa_set],
    )
    keys = result.saa.coverage["class_key"].tolist()
    assert keys[:3] == ["saa_Z", "saa_A", "saa_M"]


def test_set_history_only_lists_active_sets() -> None:
    """``set_history`` lists only sets that governed at least one date."""
    set_old = _make_set(
        family="saa",
        effective_from=date(2010, 1, 1),
        limits={"saa_a": _D("40.0")},
        label="ancient",
    )
    set_active = _make_set(
        family="saa",
        effective_from=date(2024, 1, 1),
        limits={"saa_a": _D("50.0")},
        label="active",
    )
    set_future = _make_set(
        family="saa",
        effective_from=date(2030, 1, 1),
        limits={"saa_a": _D("60.0")},
        label="future",
    )
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={date(2024, 6, 30): _D("100000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[date(2024, 6, 30)],
        saa_sets=[set_old, set_active, set_future],
    )
    labels = [triple[2] for triple in result.saa.set_history]
    assert labels == ["active"]


def test_set_history_sorted_by_effective_from_asc() -> None:
    """``set_history`` is sorted ascending by ``effective_from``."""
    set_a = _make_set(
        family="saa",
        effective_from=date(2020, 1, 1),
        limits={"saa_a": _D("40.0")},
        label="A",
    )
    set_b = _make_set(
        family="saa",
        effective_from=date(2024, 1, 1),
        limits={"saa_a": _D("50.0")},
        label="B",
    )
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={
            date(2023, 6, 30): _D("100000"),
            date(2024, 6, 30): _D("100000"),
        },
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[date(2023, 6, 30), date(2024, 6, 30)],
        saa_sets=[set_a, set_b],
    )
    effective_dates = [triple[0] for triple in result.saa.set_history]
    assert effective_dates == sorted(effective_dates)


def test_aum_used_series_tracks_the_book() -> None:
    """``aum_used`` is a Series over the eval dates carrying Σ NAV.

    The engine has no ``cash_residual`` output any more: ADR-0103 §2 retired
    the residual, and a denominator derived from the numerators has no gap
    left to report.
    """
    t1 = date(2024, 6, 30)
    t2 = date(2024, 12, 31)
    inv = _make_inv()
    actual, plan = _attach_navs(
        inv,
        actual={t1: _D("300000"), t2: _D("400000")},
    )
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t1, t2],
    )
    assert list(result.aum_used.index) == [pd.Timestamp(t1), pd.Timestamp(t2)]
    assert result.aum_used[pd.Timestamp(t1)] == _D("300000")
    assert result.aum_used[pd.Timestamp(t2)] == _D("400000")
    assert not hasattr(result, "cash_residual")


# ---------------------------------------------------------------------
# F. Decimal precision
# ---------------------------------------------------------------------


def test_quantize_outputs_to_four_decimals() -> None:
    """Output ``Decimal`` values quantize to four decimal places."""
    # 333333 / 1000000 * 100 = 33.3333; choose 333_333 to land exactly on 4dp.
    t = date(2024, 6, 30)
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("333333")})
    cash, cash_navs = _cash_ballast({t: _BOOK_TOTAL - _D("333333")})
    result = _simple_call(
        investments=[inv, cash],
        actual_navs={inv.investment.id: actual, **cash_navs},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    row = _row_for(result.saa.coverage, as_of_date=t, class_key="saa_a")
    # coverage_pct has exactly four decimal places per quantize
    assert row["coverage_pct"] == _D("33.3333")
    assert row["coverage_pct"].as_tuple().exponent == -4
    assert row["nav_sum_eur"].as_tuple().exponent == -4
    assert row["headroom_eur"].as_tuple().exponent == -4


def test_quantize_uses_bankers_rounding() -> None:
    """The fifth-decimal half-even tie rounds to the even neighbour."""
    from services.analytics.limit_coverage import _quantize  # private but stable

    # 0.12345 → tie at 4th decimal between 0.1234 and 0.1235. Banker's:
    # 4 is even, so round down to 0.1234.
    assert _quantize(Decimal("0.12345")) == Decimal("0.1234")
    # 0.12355 → tie between 0.1235 and 0.1236. Banker's: 6 is even → up to 0.1236.
    assert _quantize(Decimal("0.12355")) == Decimal("0.1236")


def test_decimal_throughout_no_float_leak() -> None:
    """Engine output numeric columns are ``Decimal``, never ``float``."""
    t = date(2024, 6, 30)
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={t: _D("300000")})
    result = _simple_call(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        evaluation_dates=[t],
    )
    for col in ("max_pct", "nav_sum_eur", "coverage_pct", "headroom_eur"):
        for value in result.saa.coverage[col]:
            if value is None:
                continue
            assert isinstance(value, Decimal), (
                f"column {col} produced non-Decimal value {value!r} ({type(value).__name__})"
            )
    for value in result.aum_used:
        assert isinstance(value, Decimal)


# ---------------------------------------------------------------------
# G. Stateless / reentrancy
# ---------------------------------------------------------------------


def test_two_invocations_produce_equal_results() -> None:
    """Two identical inputs produce equal DataFrames and series."""
    inv = _make_inv()
    actual, plan = _attach_navs(inv, actual={date(2024, 6, 30): _D("300000")})
    # Pre-build the limit sets so both invocations see the same UUIDs;
    # ``_simple_call`` would otherwise materialise fresh defaults
    # (with new ``uuid4`` ids) on each call.
    saa_sets = [
        _make_set(
            family="saa",
            effective_from=date(2020, 1, 1),
            limits={"saa_a": _D("50.0"), "saa_b": _D("30.0")},
        )
    ]
    anlv_sets = [
        _make_set(
            family="anlv",
            effective_from=date(2020, 1, 1),
            limits={"anlv_a": _D("60.0"), "anlv_b": _D("25.0")},
        )
    ]
    kwargs: dict[str, object] = dict(
        investments=[inv],
        actual_navs={inv.investment.id: actual},
        plan_navs={inv.investment.id: plan},
        saa_sets=saa_sets,
        anlv_sets=anlv_sets,
    )
    r1 = _simple_call(**kwargs)  # type: ignore[arg-type]
    r2 = _simple_call(**kwargs)  # type: ignore[arg-type]
    pd.testing.assert_frame_equal(r1.saa.coverage, r2.saa.coverage)
    pd.testing.assert_frame_equal(r1.anlv.coverage, r2.anlv.coverage)
    pd.testing.assert_series_equal(r1.aum_used, r2.aum_used)
    assert r1.saa.set_history == r2.saa.set_history
    assert r1.anlv.set_history == r2.anlv.set_history


# ---------------------------------------------------------------------
# H. Input validation
# ---------------------------------------------------------------------


def test_unsorted_family_sets_raises_value_error() -> None:
    """A family list out of ``effective_from`` order is rejected."""
    s_late = _make_set(
        family="saa",
        effective_from=date(2025, 1, 1),
        limits={"saa_a": _D("40.0")},
    )
    s_early = _make_set(
        family="saa",
        effective_from=date(2020, 1, 1),
        limits={"saa_a": _D("50.0")},
    )
    with pytest.raises(ValueError, match="must be sorted ascending"):
        compute_coverage(
            investments=[],
            actual_navs={},
            plan_navs={},
            cut_over=date(2025, 12, 31),
            saa_sets=[s_late, s_early],
            anlv_sets=[],
            evaluation_dates=[],
        )


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("100.0001")])
def test_invalid_warn_threshold_raises_value_error(bad: Decimal) -> None:
    """``warn_threshold_pct`` must lie in (0, 100]."""
    with pytest.raises(ValueError, match="warn_threshold_pct"):
        _simple_call(warn_threshold_pct=bad)


def test_empty_evaluation_dates_returns_empty_dataframes() -> None:
    """``evaluation_dates=[]`` returns empty DataFrames, not an error."""
    result = _simple_call(evaluation_dates=[])
    assert isinstance(result, CoverageEngineResult)
    assert isinstance(result.saa, FamilyCoverageResult)
    assert isinstance(result.anlv, FamilyCoverageResult)
    assert result.saa.coverage.empty
    assert result.anlv.coverage.empty
    assert result.saa.set_history == []
    assert result.anlv.set_history == []
    assert result.aum_used.empty


# ---------------------------------------------------------------------
# §6.3 Excel-reference parity (skipped until the reference XLSX lands)
# ---------------------------------------------------------------------


_REFERENCE_XLSX: Path = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "sample"
    / "PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx"
)


@pytest.mark.skipif(
    not _REFERENCE_XLSX.exists(),
    reason=(
        f"Reference XLSX not yet provided (expected at {_REFERENCE_XLSX}). See Kickoff #2 §6.3."
    ),
)
def test_engine_matches_excel_reference_three_dates() -> None:
    """Parity check against the hand-validated reference workbook.

    Loads three stichtage (2020-12-31, 2023-06-30, 2026-03-31) from
    the reference XLSX and compares engine output row-by-row with
    Decimal tolerances (±0.01 EUR for money, ±0.0001 pp for shares).
    """
    # The loader skeleton currently raises NotImplementedError. When
    # the workbook is added the loader will return real
    # ExpectedCoverage rows and this test will construct the engine
    # inputs from the same workbook's Engine_Inputs sheet.
    from tests.services.analytics._reference_loader import load_reference

    expected = load_reference(_REFERENCE_XLSX)
    assert expected, "loader returned no expected rows"
    # Inputs and per-row assertions will be wired up once the workbook
    # is committed; the skipif above guards activation.
