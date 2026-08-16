# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Limit-coverage engine — analytics-pure, tenant-blind, stateless.

This module computes per-class coverage ratios against historical
limit sets for two limit families:

* ``saa`` — Strategic Asset Allocation, classified by
  ``investments.asset_class_code``.
* ``anlv`` — German AnlV (Anlageverordnung) categories, classified
  by ``investments.anlv_code``.

The engine lives under :mod:`services.analytics` and consumes the
repository DTO dataclasses directly: the caller passes pre-resolved
:class:`InvestmentWithClassCodeDTO` rows (each carrying an
:class:`InvestmentDTO` plus its asset-class code), per-investment
:class:`InvestmentNavDTO` streams for actual and plan, and two
:class:`LimitSetWithLimitsDTO` lists (one per family). The engine
builds its internal date-keyed lookups from these flat DTO streams
in one pass, so the heavy-lifting computation loop does not perform
any cross-DTO joins.

**The denominator is the book itself (ADR-0103 §2).** There is no AUM
input. AUM is ``Σ nav_functional(t)`` over every investment handed in —
the same NAVs the numerators are built from, summed once. Cash positions
are ordinary members of that sum (ADR-0103 §8: "coverage/limits: cash
rows remain included"); they need no special handling here precisely
because they are investments with NAV series like any other, and the
engine stays **investment-type-blind** (the AST guard in
``tests/regression/test_analytics_layer_pure.py`` is the proof). The
retired ``portfolio_aum`` series, its carry-forward, and the
``cash_residual`` output went with the residual — a denominator derived
from the numerators cannot go stale against them.

Purity contract (ADR-0013 narrowed by ADR-0045 §3): the engine may
import DTO dataclasses from ``core.repositories.*_repository`` modules
but not the Repository classes themselves, no SQLAlchemy sessions, no
FastAPI symbols, no PyQt6 symbols. The regression guard in
``tests/regression/test_analytics_layer_pure.py`` enforces this.

Behavioural contract:

* **Plan/Actual cut-over with carry-forward and cross-stream fallback
  (ADR-0060).** Both NAV streams are supplied per investment plus a
  single global ``cut_over`` date. For evaluation date ``t`` the
  engine **prefers** the actual stream when ``t <= cut_over`` and the
  plan stream when ``t > cut_over``. In the preferred stream the
  **latest entry at or before ``t``** is used (carry-forward). If the
  preferred stream has no such entry, the engine falls back to the
  **other** stream under the same carry-forward rule (cross-stream
  fallback). :class:`CoverageInputOutOfRange` is raised only when
  **both** streams have no entry at or before ``t`` — the investment
  did not yet exist at ``t`` and there is no historical value to
  carry. Liquidations are still expressed by an explicit
  ``nav_value == 0`` entry; the engine never extrapolates to zero.
* **Denominator resolution (ADR-0103 §2).** ``aum(t)`` is the sum of the
  NAVs resolved above, over every investment. A date at which the book
  carries no value at all — an empty universe, or one whose every
  position resolves to zero — has no denominator to divide by and raises
  :class:`CoverageInputMissing`.
* **Limit-set selection (ADR-0056).** For each ``(family, t)`` pair
  the engine picks the set with the maximum ``effective_from <= t``.
  No such set raises :class:`LimitSetNotEffective`.
* **Unallocated bucket.** Investments without a class for the active
  family (``asset_class_code is None`` for SAA;
  ``anlv_code is None`` for AnlV — the V1 fallback per ADR-0057) are
  aggregated into a single ``'unallocated'`` row, emitted only when
  the bucket carries non-zero NAV.
* **NO_LIMIT bucket.** Classes with invested NAV > 0 that are absent
  from the effective set are surfaced individually with
  ``max_pct=None`` and ``status='NO_LIMIT'``.
* **Numeric conventions.** Coverage and limits are in percentage
  points (``Decimal('63.8076')`` ≙ 63.8076 %). Headroom is in EUR.
  ``Decimal`` is used throughout; output money/percentage columns
  are quantized to four decimal places with banker's rounding
  (``ROUND_HALF_EVEN``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

import pandas as pd

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
)
from core.repositories.investment_nav_repository import InvestmentNavDTO
from services.analytics._dtos import (
    InvestmentWithClassCodeDTO,
    LimitSetWithLimitsDTO,
)
import itertools

_QUANTUM: Decimal = Decimal("0.0001")
_HUNDRED: Decimal = Decimal("100")
_ZERO: Decimal = Decimal("0")

_SAA: str = "saa"
_ANLV: str = "anlv"

_STATUS_OK: str = "OK"
_STATUS_WARN: str = "WARN"
_STATUS_BREACH: str = "BREACH"
_STATUS_UNALLOCATED: str = "UNALLOCATED"
_STATUS_NO_LIMIT: str = "NO_LIMIT"

_UNALLOCATED_KEY: str = "unallocated"

_DF_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "class_key",
    "max_pct",
    "nav_sum_eur",
    "coverage_pct",
    "headroom_eur",
    "status",
)


@dataclass(frozen=True)
class FamilyCoverageResult:
    """All coverage rows for one family across all evaluation dates.

    Attributes:
        family: ``'saa'`` or ``'anlv'``.
        coverage: Long-format DataFrame; see the module docstring for
            the exact schema (columns ``as_of_date``, ``class_key``,
            ``max_pct``, ``nav_sum_eur``, ``coverage_pct``,
            ``headroom_eur``, ``status``).
        set_history: List of ``(effective_from, set_id, label)`` for
            every limit set that governed at least one evaluation
            date, ordered by ``effective_from`` ascending. Renderer
            uses this for limit-line annotations on small-multiples
            charts.
    """

    family: str
    coverage: pd.DataFrame
    set_history: list[tuple[date, UUID, str]]


@dataclass(frozen=True)
class CoverageEngineResult:
    """Top-level result of a single engine invocation.

    Attributes:
        saa: Coverage result for the SAA family.
        anlv: Coverage result for the AnlV family.
        aum_used: Series indexed by ``as_of_date`` carrying the AUM
            figure used as denominator — ``Σ nav_functional(t)`` over
            every investment, cash rows included (ADR-0103 §2). Useful
            for renderer subtitles.
    """

    saa: FamilyCoverageResult
    anlv: FamilyCoverageResult
    aum_used: pd.Series


def compute_coverage(
    *,
    investments: list[InvestmentWithClassCodeDTO],
    actual_navs: dict[UUID, list[InvestmentNavDTO]],
    plan_navs: dict[UUID, list[InvestmentNavDTO]],
    cut_over: date,
    saa_sets: list[LimitSetWithLimitsDTO],
    anlv_sets: list[LimitSetWithLimitsDTO],
    evaluation_dates: list[date],
    warn_threshold_pct: Decimal = Decimal("90.0"),
) -> CoverageEngineResult:
    """Compute per-class coverage ratios for SAA and AnlV families.

    The function is stateless and re-entrant: two calls with equal
    inputs produce equal outputs, and concurrent calls do not share
    state. See the module docstring for the full behavioural contract.

    Args:
        investments: All investments under evaluation, each carrying
            its base :class:`InvestmentDTO` plus its resolved
            ``asset_class_code``. The caller pre-filters (e.g.
            ``is_active=True``) — the engine does not. This list *is*
            the denominator's universe: every member contributes its
            NAV to ``aum(t)``, cash positions included (ADR-0103 §8),
            and the engine never inspects ``investment_type``.
        actual_navs: Per-investment actual-NAV stream, keyed by
            ``investment.id``. Empty list or missing key both mean
            "no actual NAVs for this investment". The engine uses
            carry-forward (latest entry at or before ``t``) with
            cross-stream fallback to ``plan_navs`` per ADR-0060.
        plan_navs: Per-investment plan-NAV stream, keyed by
            ``investment.id``. Symmetric to ``actual_navs`` — the
            engine carries forward within the stream and falls back
            to ``actual_navs`` when no plan entry exists at or
            before ``t``.
        cut_over: Global cut-over date dividing actual-NAV territory
            (``t <= cut_over``) from plan-NAV territory
            (``t > cut_over``).
        saa_sets: Historical SAA limit sets, sorted ascending by
            ``set.effective_from``. May be empty — every SAA
            evaluation will then raise
            :class:`LimitSetNotEffective`.
        anlv_sets: Historical AnlV limit sets, sorted ascending by
            ``set.effective_from``. Same semantics as ``saa_sets``.
        evaluation_dates: Stichtage in chronological order. Empty list
            is a valid input (returns empty DataFrames).
        warn_threshold_pct: Coverage strictly above
            ``max_pct * warn_threshold_pct / 100`` is ``WARN``; equal
            to or below stays ``OK``. Default ``Decimal('90.0')``.

    Returns:
        A :class:`CoverageEngineResult` carrying one
        :class:`FamilyCoverageResult` per family plus the ``aum_used``
        denominator series indexed by evaluation date.

    Raises:
        ValueError: Caller-side contract violation — family list
            unsorted, or ``warn_threshold_pct`` out of ``(0, 100]``.
        CoverageInputMissing: The book carries no value at an
            evaluation date, so there is no denominator to divide by
            (ADR-0103 §2).
        CoverageInputOutOfRange: Both NAV streams of an investment have
            no entry at or before the evaluation date (per ADR-0060
            the engine carries forward within the preferred stream
            and falls back across streams before raising).
        LimitSetNotEffective: No limit set is in force for some
            ``(family, evaluation_date)`` pair.
    """
    _validate(
        saa_sets=saa_sets,
        anlv_sets=anlv_sets,
        warn_threshold_pct=warn_threshold_pct,
    )

    actual_nav_lookup = _build_nav_lookup(actual_navs)
    plan_nav_lookup = _build_nav_lookup(plan_navs)

    aum_used: dict[date, Decimal] = {}
    nav_per_date: dict[date, dict[UUID, Decimal]] = {}

    for t in evaluation_dates:
        nav_t: dict[UUID, Decimal] = {}
        total_nav = _ZERO
        for inv in investments:
            nav_i_t = _resolve_nav(
                t=t,
                investment_id=inv.investment.id,
                cut_over=cut_over,
                actual_nav_lookup=actual_nav_lookup,
                plan_nav_lookup=plan_nav_lookup,
            )
            nav_t[inv.investment.id] = nav_i_t
            total_nav += nav_i_t
        # ADR-0103 §2: the denominator *is* the book. A zero total has no
        # quota to apportion — dividing by it would be a silent nonsense
        # rather than a missing input, so say so.
        if total_nav == _ZERO:
            raise CoverageInputMissing(
                f"The book carries no value at {t}: Σ NAV over "
                f"{len(investments)} investment(s) is zero, so coverage has "
                "no denominator."
            )
        aum_used[t] = total_nav
        nav_per_date[t] = nav_t

    saa_result = _compute_family(
        family=_SAA,
        family_sets=saa_sets,
        investments=investments,
        evaluation_dates=evaluation_dates,
        aum_used=aum_used,
        nav_per_date=nav_per_date,
        warn_threshold_pct=warn_threshold_pct,
    )
    anlv_result = _compute_family(
        family=_ANLV,
        family_sets=anlv_sets,
        investments=investments,
        evaluation_dates=evaluation_dates,
        aum_used=aum_used,
        nav_per_date=nav_per_date,
        warn_threshold_pct=warn_threshold_pct,
    )

    return CoverageEngineResult(
        saa=saa_result,
        anlv=anlv_result,
        aum_used=_to_series(aum_used, evaluation_dates),
    )


def _validate(
    *,
    saa_sets: list[LimitSetWithLimitsDTO],
    anlv_sets: list[LimitSetWithLimitsDTO],
    warn_threshold_pct: Decimal,
) -> None:
    """Caller-side contract checks; raise ``ValueError`` on violation."""
    for family, sets in ((_SAA, saa_sets), (_ANLV, anlv_sets)):
        for prev, curr in itertools.pairwise(sets):
            if curr.set.effective_from < prev.set.effective_from:
                raise ValueError(
                    f"{family}_sets must be sorted ascending by "
                    f"effective_from; found {prev.set.effective_from} "
                    f"before {curr.set.effective_from}"
                )

    if not (Decimal(0) < warn_threshold_pct <= _HUNDRED):
        raise ValueError(f"warn_threshold_pct must lie in (0, 100]; got {warn_threshold_pct}")


def _build_nav_lookup(
    streams: dict[UUID, list[InvestmentNavDTO]],
) -> dict[UUID, dict[date, Decimal]]:
    """Flatten per-investment DTO streams into nested date lookups.

    The DB enforces
    ``UNIQUE(investment_id, as_of_date, nav_kind)`` (ADR-0043 §1), so
    within a single ``nav_kind`` stream a well-formed caller has at
    most one row per date. The defensive assertion catches caller-side
    duplicates loudly rather than silently shadowing.
    """
    out: dict[UUID, dict[date, Decimal]] = {}
    for investment_id, nav_rows in streams.items():
        per_date: dict[date, Decimal] = {}
        for row in nav_rows:
            assert row.as_of_date not in per_date, (
                f"NAV stream for {investment_id} carries duplicate "
                f"as_of_date {row.as_of_date} (nav_kind={row.nav_kind!r})"
            )
            per_date[row.as_of_date] = row.nav_value
        out[investment_id] = per_date
    return out


def _resolve_nav(
    *,
    t: date,
    investment_id: UUID,
    cut_over: date,
    actual_nav_lookup: dict[UUID, dict[date, Decimal]],
    plan_nav_lookup: dict[UUID, dict[date, Decimal]],
) -> Decimal:
    """Return the NAV for ``investment_id`` at ``t`` per ADR-0060.

    Resolution order:

    1. **Stream preference.** ``actual`` is preferred for
       ``t <= cut_over``, ``plan`` for ``t > cut_over``.
    2. **Carry-forward within the preferred stream.** Return the value
       at the latest date ``<= t`` if any such entry exists.
    3. **Cross-stream fallback.** Otherwise consult the other stream
       under the same carry-forward rule.
    4. Raise :class:`CoverageInputOutOfRange` only when both streams
       have no entry at or before ``t``.

    The naive ``max``-over-list-comprehension lookup is O(n) per call.
    At the V1 grid scale (monthly evaluation × daily NAVs) this is
    well below a second; switching to ``bisect`` over presorted
    streams is a follow-up if a future scale demands it.
    """
    if t <= cut_over:
        primary = actual_nav_lookup.get(investment_id, {})
        secondary = plan_nav_lookup.get(investment_id, {})
    else:
        primary = plan_nav_lookup.get(investment_id, {})
        secondary = actual_nav_lookup.get(investment_id, {})

    value = _latest_at_or_before(primary, t)
    if value is not None:
        return value

    value = _latest_at_or_before(secondary, t)
    if value is not None:
        return value

    raise CoverageInputOutOfRange(
        f"Investment {investment_id}: no NAV at or before {t} in either stream."
    )


def _latest_at_or_before(
    stream: dict[date, Decimal],
    t: date,
) -> Decimal | None:
    """Return ``stream[d]`` for the largest ``d <= t``, or ``None``.

    A liquidation is expressed as an explicit ``Decimal(0)`` entry and
    is returned faithfully; callers must therefore distinguish the
    ``None`` sentinel (no entry at or before ``t``) from ``Decimal(0)``
    via ``is not None`` rather than truthiness.
    """
    candidates = [d for d in stream if d <= t]
    if not candidates:
        return None
    return stream[max(candidates)]


def _compute_family(
    *,
    family: str,
    family_sets: list[LimitSetWithLimitsDTO],
    investments: list[InvestmentWithClassCodeDTO],
    evaluation_dates: list[date],
    aum_used: dict[date, Decimal],
    nav_per_date: dict[date, dict[UUID, Decimal]],
    warn_threshold_pct: Decimal,
) -> FamilyCoverageResult:
    """Build the long-format DataFrame and set_history for one family."""
    set_history_seen: dict[UUID, tuple[date, UUID, str]] = {}
    rows: list[dict[str, object]] = []

    for t in evaluation_dates:
        eff_set = _pick_effective(family_sets, t)
        set_history_seen[eff_set.set.id] = (
            eff_set.set.effective_from,
            eff_set.set.id,
            eff_set.set.label,
        )
        aum_t = aum_used[t]
        nav_t = nav_per_date[t]

        nav_by_class: dict[str, Decimal] = {}
        unallocated_nav = _ZERO
        for inv in investments:
            cls = inv.asset_class_code if family == _SAA else inv.investment.anlv_code
            nav_i = nav_t[inv.investment.id]
            if cls is None:
                unallocated_nav += nav_i
            else:
                nav_by_class[cls] = nav_by_class.get(cls, _ZERO) + nav_i

        for class_key, max_pct in eff_set.limits.items():
            nav_sum = nav_by_class.pop(class_key, _ZERO)
            cov_pct = (nav_sum / aum_t) * _HUNDRED
            headroom = (max_pct - cov_pct) * aum_t / _HUNDRED
            status = _classify(cov_pct, max_pct, warn_threshold_pct)
            rows.append(
                {
                    "as_of_date": t,
                    "class_key": class_key,
                    "max_pct": max_pct,
                    "nav_sum_eur": _quantize(nav_sum),
                    "coverage_pct": _quantize(cov_pct),
                    "headroom_eur": _quantize(headroom),
                    "status": status,
                }
            )

        if unallocated_nav > _ZERO:
            cov_pct = (unallocated_nav / aum_t) * _HUNDRED
            rows.append(
                {
                    "as_of_date": t,
                    "class_key": _UNALLOCATED_KEY,
                    "max_pct": None,
                    "nav_sum_eur": _quantize(unallocated_nav),
                    "coverage_pct": _quantize(cov_pct),
                    "headroom_eur": None,
                    "status": _STATUS_UNALLOCATED,
                }
            )

        for class_key in sorted(nav_by_class):
            nav_sum = nav_by_class[class_key]
            cov_pct = (nav_sum / aum_t) * _HUNDRED
            rows.append(
                {
                    "as_of_date": t,
                    "class_key": class_key,
                    "max_pct": None,
                    "nav_sum_eur": _quantize(nav_sum),
                    "coverage_pct": _quantize(cov_pct),
                    "headroom_eur": None,
                    "status": _STATUS_NO_LIMIT,
                }
            )

    df = pd.DataFrame(rows, columns=list(_DF_COLUMNS))
    if not df.empty:
        df["as_of_date"] = pd.to_datetime(df["as_of_date"])

    set_history = sorted(set_history_seen.values(), key=lambda triple: triple[0])
    return FamilyCoverageResult(family=family, coverage=df, set_history=set_history)


def _pick_effective(
    family_sets: list[LimitSetWithLimitsDTO],
    t: date,
) -> LimitSetWithLimitsDTO:
    """Return the active limit set for date ``t`` per ADR-0056 §Selection."""
    candidates = [s for s in family_sets if s.set.effective_from <= t]
    if not candidates:
        raise LimitSetNotEffective(
            f"No limit set in force at {t} (earliest effective_from is later)."
        )
    return max(candidates, key=lambda s: s.set.effective_from)


def _classify(
    cov_pct: Decimal,
    max_pct: Decimal,
    warn_threshold_pct: Decimal,
) -> str:
    """Return ``OK`` / ``WARN`` / ``BREACH`` for a single class row."""
    if cov_pct > max_pct:
        return _STATUS_BREACH
    warn_floor = max_pct * warn_threshold_pct / _HUNDRED
    if cov_pct > warn_floor:
        return _STATUS_WARN
    return _STATUS_OK


def classify_coverage_status(
    coverage_pct: Decimal,
    max_pct: Decimal,
    warn_threshold_pct: Decimal = Decimal("90.0"),
) -> str:
    """Classify a coverage ratio as ``OK`` / ``WARN`` / ``BREACH``.

    Public, single-source wrapper over the engine's internal classifier.
    Exposed so Irene's internal-delta layer (ADR-0087) can re-classify a
    previously acknowledged coverage magnitude against the *current*
    ceiling without duplicating the threshold logic — the boundary
    definition stays owned here.

    Args:
        coverage_pct: Coverage ratio in percentage points.
        max_pct: The ceiling in percentage points.
        warn_threshold_pct: WARN floor as a percentage of ``max_pct``.
            Coverage strictly above ``max_pct * warn_threshold_pct / 100``
            is ``WARN``; at or below is ``OK``. Default ``Decimal('90.0')``
            matches :func:`compute_coverage`.

    Returns:
        One of ``OK`` / ``WARN`` / ``BREACH``.
    """
    return _classify(coverage_pct, max_pct, warn_threshold_pct)


def _quantize(value: Decimal) -> Decimal:
    """Quantize to four decimal places with banker's rounding."""
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _to_series(
    data: dict[date, Decimal],
    evaluation_dates: list[date],
) -> pd.Series:
    """Build a pandas Series with a datetime index over evaluation_dates."""
    if not evaluation_dates:
        index = pd.DatetimeIndex([], name="as_of_date")
        return pd.Series([], index=index, dtype=object)
    values = [data[t] for t in evaluation_dates]
    index = pd.DatetimeIndex(
        [pd.Timestamp(t) for t in evaluation_dates],
        name="as_of_date",
    )
    return pd.Series(values, index=index, dtype=object)
