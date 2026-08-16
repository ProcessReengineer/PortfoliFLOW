# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.investment_returns``.

Pure-function tests — no DB, no Qt, no FastAPI. Each test builds a
deterministic pandas DataFrame / Series and asserts numerical
output. The QT-consistency tests at the bottom replicate the
formulas implemented in ``gui/widgets/chart_widgets.py`` (the
calculation half of the QT chart widgets) and assert that the new
analytics functions produce identical results to within 1e-9.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from services.analytics.investment_returns import (
    compute_cashflow_adjusted_return_series,
    compute_net_capital_gain,
    compute_rolling_irr_since_inception,
    compute_rolling_multiples,
    compute_total_return_series,
    compute_trailing_returns,
)


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


def _make_actual_cashflows(rows: list[tuple[datetime, str, float]]) -> pd.DataFrame:
    """Helper: build a Phase-4-shaped flat actuals cashflow frame."""
    return pd.DataFrame(
        {
            "flow_timestamp": [r[0] for r in rows],
            "flow_type": [r[1] for r in rows],
            "amount": [r[2] for r in rows],
        }
    )


# ---------------------------------------------------------------------------
# compute_total_return_series
# ---------------------------------------------------------------------------


def test_total_return_pct_change_against_handcomputed_values() -> None:
    nav = pd.Series(
        [1000.0, 1100.0, 1100.0, 1320.0],
        index=[
            date(2024, 12, 31),
            date(2025, 3, 31),
            date(2025, 6, 30),
            date(2025, 9, 30),
        ],
    )
    result = compute_total_return_series(nav)
    expected = pd.Series(
        [0.10, 0.0, 0.20],
        index=[
            date(2025, 3, 31),
            date(2025, 6, 30),
            date(2025, 9, 30),
        ],
    )
    pd.testing.assert_series_equal(result, expected, check_names=False, atol=1e-12)


def test_total_return_drops_first_datapoint_and_nans() -> None:
    nav = pd.Series(
        [np.nan, 100.0, np.nan, 110.0, 121.0],
        index=[
            date(2024, 1, 1),
            date(2024, 6, 30),
            date(2024, 9, 30),
            date(2024, 12, 31),
            date(2025, 6, 30),
        ],
    )
    result = compute_total_return_series(nav)
    # NaN entries dropped → effective series [100, 110, 121]; pct_change
    # → [+0.10, +0.10] indexed by the 2024-12-31 and 2025-06-30 dates.
    assert result.values == pytest.approx([0.10, 0.10], abs=1e-12)
    assert list(result.index) == [date(2024, 12, 31), date(2025, 6, 30)]


def test_total_return_empty_when_one_or_zero_navs() -> None:
    assert compute_total_return_series(pd.Series(dtype="float64")).empty
    nav = pd.Series([100.0], index=[date(2025, 1, 1)])
    assert compute_total_return_series(nav).empty


# ---------------------------------------------------------------------------
# compute_cashflow_adjusted_return_series (ADR-0066)
# ---------------------------------------------------------------------------


def test_cashflow_adjusted_reduces_to_pct_change_with_no_flows() -> None:
    """Empty cashflows → identical to ``compute_total_return_series``.

    This reduction property is the safety net for every liquid
    investment and for all existing no-cashflow tests: with no flows
    ``a_t = 0`` and the formula collapses to ``pct_change()``.
    """
    nav = pd.Series(
        [1000.0, 1100.0, 1100.0, 1320.0],
        index=[
            date(2024, 12, 31),
            date(2025, 3, 31),
            date(2025, 6, 30),
            date(2025, 9, 30),
        ],
    )
    adjusted = compute_cashflow_adjusted_return_series(nav, _make_actual_cashflows([]))
    plain = compute_total_return_series(nav)
    pd.testing.assert_series_equal(adjusted, plain)


def test_cashflow_adjusted_recovers_known_market_return() -> None:
    """Synthetic NAVs embedding a constant return + interior flows.

    Build daily NAVs from a known constant market return ``m`` via the
    identity ``NAV_t = NAV_{t-1} * (1 + m) - a_t`` (signed ``a_t``:
    calls negative, distributions positive). The adjusted return must
    recover ``m`` on every interval — including the flow intervals.
    """
    m = 0.001
    start = date(2025, 1, 1)
    dates = [start + timedelta(days=i) for i in range(10)]
    # Signed amounts keyed by NAV date: a capital call (negative) on
    # day 3, a distribution (positive) on day 7. Both land exactly on a
    # NAV observation, so each is attributed to the interval ending on
    # its own date (upper-inclusive).
    signed_by_date = {dates[3]: -500_000.0, dates[7]: 80_000.0}

    nav_values = [1_000_000.0]
    for i in range(1, len(dates)):
        a_t = signed_by_date.get(dates[i], 0.0)
        nav_values.append(nav_values[-1] * (1.0 + m) - a_t)
    nav = pd.Series(nav_values, index=dates, dtype="float64")

    cashflows = _make_actual_cashflows(
        [
            (
                datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc),
                "capital_call" if amount < 0 else "distribution",
                amount,
            )
            for d, amount in signed_by_date.items()
        ]
    )

    result = compute_cashflow_adjusted_return_series(nav, cashflows)
    assert result.values == pytest.approx([m] * 9, abs=1e-12)
    assert list(result.index) == dates[1:]


def test_cashflow_adjusted_attributes_flow_to_enclosing_interval() -> None:
    """A flow strictly between two NAV dates lands in that interval.

    NAV observations are sparse (quarterly); the flow date equals no
    NAV date. It must be attributed to the interval that encloses it,
    not the next one and not dropped.
    """
    m = 0.01
    d0, d1, d2 = date(2025, 1, 1), date(2025, 4, 1), date(2025, 7, 1)
    # Capital call of 10 (signed -10) strictly between d0 and d1.
    # NAV_d1 = NAV_d0*(1+m) - (-10); NAV_d2 = NAV_d1*(1+m).
    nav_d0 = 100.0
    nav_d1 = nav_d0 * (1.0 + m) + 10.0
    nav_d2 = nav_d1 * (1.0 + m)
    nav = pd.Series([nav_d0, nav_d1, nav_d2], index=[d0, d1, d2])

    cashflows = _make_actual_cashflows([(_ts(2025, 2, 15), "capital_call", -10.0)])
    result = compute_cashflow_adjusted_return_series(nav, cashflows)

    assert list(result.index) == [d1, d2]
    # First interval recovers m because the flow is attributed to it.
    assert result.iloc[0] == pytest.approx(m, abs=1e-12)
    # Second interval (no flow) is plain pct_change == m as well.
    assert result.iloc[1] == pytest.approx(m, abs=1e-12)
    # Sanity: plain pct_change would mis-read the first interval.
    plain = compute_total_return_series(nav)
    assert plain.iloc[0] != pytest.approx(m, abs=1e-3)


def test_cashflow_adjusted_interval_is_upper_inclusive() -> None:
    """Half-open ``(d_{t-1}, d_t]``: flow on ``d_t`` is included; on the
    lower bound is excluded."""
    m = 0.01
    d0, d1 = date(2025, 1, 1), date(2025, 4, 1)
    nav_d0 = 100.0
    # Flow on d1 (amount -50) must be counted in the single interval.
    nav_d1 = nav_d0 * (1.0 + m) + 50.0
    nav = pd.Series([nav_d0, nav_d1], index=[d0, d1])

    cashflows = _make_actual_cashflows(
        [
            # Flow on the lower bound d0 — must be EXCLUDED from (d0, d1].
            (_ts(2025, 1, 1), "capital_call", -1000.0),
            # Flow on the upper bound d1 — must be INCLUDED.
            (_ts(2025, 4, 1), "capital_call", -50.0),
        ]
    )
    result = compute_cashflow_adjusted_return_series(nav, cashflows)

    assert list(result.index) == [d1]
    # Recovers m only if the d1 flow is included and the d0 flow is not.
    assert result.iloc[0] == pytest.approx(m, abs=1e-12)


def test_cashflow_adjusted_empty_or_single_nav_returns_empty() -> None:
    empty = compute_cashflow_adjusted_return_series(
        pd.Series(dtype="float64"), _make_actual_cashflows([])
    )
    assert empty.empty
    single = compute_cashflow_adjusted_return_series(
        pd.Series([100.0], index=[date(2025, 1, 1)]),
        _make_actual_cashflows([(_ts(2025, 1, 31), "capital_call", -10.0)]),
    )
    assert single.empty


# ---------------------------------------------------------------------------
# compute_net_capital_gain
# ---------------------------------------------------------------------------


def test_net_capital_gain_matches_qt_formula() -> None:
    """``NCG = NAV + cumsum(amount)`` evaluated on the union of dates.

    Mirrors ``gui/widgets/chart_widgets.py::_make_cash_flow_nav_chart``:
    QT's ``cf_in`` (positive distributions) and ``cf_out`` (negative
    calls) are subsumed by the signed ``amount`` column in the
    Phase-4 schema, so the formula collapses to a single cumulative
    sum. At inception (call equals NAV magnitude before any value
    growth) NCG starts at zero by construction.
    """
    cashflows = _make_actual_cashflows(
        [
            (_ts(2024, 1, 31), "capital_call", -100.0),
            (_ts(2024, 7, 1), "capital_call", -50.0),
            (_ts(2025, 6, 30), "distribution", 30.0),
        ]
    )
    nav = pd.Series(
        [100.0, 160.0, 200.0],
        index=[date(2024, 1, 31), date(2024, 12, 31), date(2025, 6, 30)],
    )
    result = compute_net_capital_gain(cashflows, nav)
    # Cashflow timestamps are normalised to midnight UTC so they
    # coincide with NAV dates on the same calendar day. Union dates:
    # 2024-01-31 (NAV+call), 2024-07-01 (call), 2024-12-31 (NAV),
    # 2025-06-30 (NAV+dist).
    # cumsum(amount) at those dates: -100, -150, -150, -120.
    # NAV reindexed: 100, NaN, 160, 200.
    # NCG: 0, NaN, 10, 80.
    timestamps = [
        pd.Timestamp(date(2024, 1, 31), tz="UTC"),
        pd.Timestamp(date(2024, 7, 1), tz="UTC"),
        pd.Timestamp(date(2024, 12, 31), tz="UTC"),
        pd.Timestamp(date(2025, 6, 30), tz="UTC"),
    ]
    assert list(result.index) == timestamps
    assert result.iloc[0] == 0.0
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == 10.0
    assert result.iloc[3] == 80.0


def test_net_capital_gain_empty_inputs_return_empty_series() -> None:
    result = compute_net_capital_gain(_make_actual_cashflows([]), pd.Series(dtype="float64"))
    assert result.empty


def test_net_capital_gain_truncates_to_as_of_date() -> None:
    cashflows = _make_actual_cashflows(
        [
            (_ts(2024, 1, 31), "capital_call", -100.0),
            (_ts(2025, 6, 30), "distribution", 30.0),
        ]
    )
    nav = pd.Series(
        [100.0, 200.0],
        index=[date(2024, 1, 31), date(2025, 6, 30)],
    )
    truncated = compute_net_capital_gain(cashflows, nav, as_of_date=date(2024, 12, 31))
    # Only the 2024-01-31 datapoint survives.
    assert len(truncated) == 1
    assert truncated.iloc[0] == 0.0


# ---------------------------------------------------------------------------
# compute_rolling_multiples
# ---------------------------------------------------------------------------


def test_rolling_multiples_matches_qt_formula_at_each_observation() -> None:
    """At each NAV observation: TVPI = (NAV+cumDist)/|cumCalls|, etc.

    Mirrors ``_make_tvpi_dpi_chart`` in ``chart_widgets.py``.
    """
    cashflows = _make_actual_cashflows(
        [
            (_ts(2024, 1, 31), "capital_call", -100.0),
            (_ts(2024, 7, 1), "capital_call", -50.0),
            (_ts(2025, 6, 30), "distribution", 30.0),
        ]
    )
    nav = pd.Series(
        [100.0, 160.0, 200.0],
        index=[date(2024, 1, 31), date(2024, 12, 31), date(2025, 6, 30)],
    )
    result = compute_rolling_multiples(cashflows, nav)
    assert list(result.columns) == ["as_of_date", "tvpi", "dpi", "rvpi"]
    # 2024-01-31: cumCalls=100, cumDist=0, NAV=100 → TVPI=1.0, DPI=0.0, RVPI=1.0
    # 2024-12-31: cumCalls=150, cumDist=0, NAV=160 → TVPI=160/150, DPI=0,
    #             RVPI=160/150
    # 2025-06-30: cumCalls=150, cumDist=30, NAV=200 → TVPI=230/150, DPI=30/150,
    #             RVPI=200/150
    assert result.loc[0, "tvpi"] == 1.0
    assert result.loc[0, "dpi"] == 0.0
    assert result.loc[0, "rvpi"] == 1.0
    assert abs(result.loc[1, "tvpi"] - (160.0 / 150.0)) < 1e-12
    assert abs(result.loc[2, "tvpi"] - (230.0 / 150.0)) < 1e-12
    assert abs(result.loc[2, "dpi"] - (30.0 / 150.0)) < 1e-12
    assert abs(result.loc[2, "rvpi"] - (200.0 / 150.0)) < 1e-12


def test_rolling_multiples_nan_when_no_calls_yet() -> None:
    cashflows = _make_actual_cashflows([])
    nav = pd.Series([100.0], index=[date(2024, 12, 31)])
    result = compute_rolling_multiples(cashflows, nav)
    assert np.isnan(result.loc[0, "tvpi"])
    assert np.isnan(result.loc[0, "dpi"])
    assert np.isnan(result.loc[0, "rvpi"])


def test_rolling_multiples_empty_when_no_navs() -> None:
    result = compute_rolling_multiples(_make_actual_cashflows([]), pd.Series(dtype="float64"))
    assert result.empty
    assert list(result.columns) == ["as_of_date", "tvpi", "dpi", "rvpi"]


# ---------------------------------------------------------------------------
# compute_rolling_irr_since_inception
# ---------------------------------------------------------------------------


def test_rolling_irr_per_observation_against_compute_irr() -> None:
    """The rolling output must agree with direct ``compute_irr`` calls.

    This is the ADR-0045 §3 reuse promise: the analytics rolling
    helper composes the Phase-4 IRR engine without re-implementing
    it. Asserts that the per-observation IRR matches the engine's
    answer for the same truncation, to within 1e-12.
    """
    from services.reporting.data_providers._calculations import compute_irr

    cashflows = _make_actual_cashflows(
        [
            (_ts(2024, 1, 31), "capital_call", -100.0),
            (_ts(2025, 6, 30), "distribution", 60.0),
        ]
    )
    nav = pd.Series(
        [100.0, 80.0],
        index=[date(2024, 1, 31), date(2025, 6, 30)],
    )
    rolling = compute_rolling_irr_since_inception(cashflows, nav)

    # The analytics function normalises cashflow timestamps to
    # midnight UTC before passing them to ``compute_irr``; the
    # reference must match.
    cf_in = pd.Series({pd.Timestamp(date(2025, 6, 30), tz="UTC"): 60.0}, dtype="float64")
    cf_out = pd.Series({pd.Timestamp(date(2024, 1, 31), tz="UTC"): -100.0}, dtype="float64")
    expected_2024 = compute_irr(
        cf_in=cf_in,
        cf_out=cf_out,
        nav_value=100.0,
        report_date=pd.Timestamp(date(2024, 1, 31), tz="UTC"),
    )
    expected_2025 = compute_irr(
        cf_in=cf_in,
        cf_out=cf_out,
        nav_value=80.0,
        report_date=pd.Timestamp(date(2025, 6, 30), tz="UTC"),
    )
    assert abs(rolling.iloc[0] - expected_2024) < 1e-12 or (
        np.isnan(rolling.iloc[0]) and np.isnan(expected_2024)
    )
    assert abs(rolling.iloc[1] - expected_2025) < 1e-12


def test_rolling_irr_returns_nan_when_compute_irr_cannot_converge() -> None:
    cashflows = _make_actual_cashflows([])
    nav = pd.Series([100.0], index=[date(2025, 1, 1)])
    rolling = compute_rolling_irr_since_inception(cashflows, nav)
    # Single positive flow (the synthetic terminal NAV) is monotone
    # → IRR engine returns NaN.
    assert np.isnan(rolling.iloc[0])


def test_rolling_irr_empty_when_no_navs() -> None:
    cashflows = _make_actual_cashflows([(_ts(2024, 1, 31), "capital_call", -100.0)])
    rolling = compute_rolling_irr_since_inception(cashflows, pd.Series(dtype="float64"))
    assert rolling.empty


# ---------------------------------------------------------------------------
# QT-consistency: identical methodology, identical resulting numbers
# ---------------------------------------------------------------------------


def _qt_reference_net_capital_gain(
    cf_in: pd.Series, cf_out: pd.Series, nav: pd.Series
) -> pd.Series:
    """Reference implementation lifted from chart_widgets.py.

    Defined here in the test to make the QT-consistency assertion
    self-contained — the QT widget code itself imports PyQt6, so we
    can't import it from the test. Any drift between this reference
    and the QT module body is a behavioural regression that must be
    surfaced via an explicit ADR.
    """
    all_dates = nav.index.union(cf_in.index).union(cf_out.index).sort_values()
    nav_full = nav.reindex(all_dates)
    cf_in_full = cf_in.reindex(all_dates, fill_value=0.0)
    cf_out_full = cf_out.reindex(all_dates, fill_value=0.0)
    return nav_full + cf_in_full.cumsum() + cf_out_full.cumsum()


def test_qt_consistency_net_capital_gain() -> None:
    """New analytics function == QT reference within 1e-9.

    QT reads Excel dates into a date-only index — both cashflow and
    NAV entries land on midnight UTC. The migration normalises
    cashflow ``flow_timestamp`` (TIMESTAMPTZ at 12:00 UTC by V2
    convention) to midnight UTC so the two surfaces evaluate on the
    same date grid.
    """
    timestamps_qt = pd.DatetimeIndex(
        [
            pd.Timestamp(date(2024, 1, 31), tz="UTC"),
            pd.Timestamp(date(2024, 7, 1), tz="UTC"),
            pd.Timestamp(date(2025, 6, 30), tz="UTC"),
        ]
    )
    cf_in_qt = pd.Series([0.0, 0.0, 30.0], index=timestamps_qt)
    cf_out_qt = pd.Series([-100.0, -50.0, 0.0], index=timestamps_qt)
    nav_qt = pd.Series(
        [100.0, 160.0, 200.0],
        index=pd.DatetimeIndex(
            [
                pd.Timestamp(date(2024, 1, 31), tz="UTC"),
                pd.Timestamp(date(2024, 12, 31), tz="UTC"),
                pd.Timestamp(date(2025, 6, 30), tz="UTC"),
            ]
        ),
    )
    qt_ncg = _qt_reference_net_capital_gain(cf_in_qt, cf_out_qt, nav_qt)

    cashflows = _make_actual_cashflows(
        [
            (_ts(2024, 1, 31), "capital_call", -100.0),
            (_ts(2024, 7, 1), "capital_call", -50.0),
            (_ts(2025, 6, 30), "distribution", 30.0),
        ]
    )
    nav_new = pd.Series(
        [100.0, 160.0, 200.0],
        index=[date(2024, 1, 31), date(2024, 12, 31), date(2025, 6, 30)],
    )
    new_ncg = compute_net_capital_gain(cashflows, nav_new)

    aligned_qt = qt_ncg.reindex(new_ncg.index)
    diff = (new_ncg - aligned_qt).abs().dropna().max()
    assert pd.isna(diff) or diff < 1e-9, (
        f"NCG drift > 1e-9 between QT reference and new analytics: {diff}"
    )


def _qt_reference_tvpi_dpi(cf_in: pd.Series, cf_out: pd.Series, nav: pd.Series) -> pd.DataFrame:
    """Reference implementation lifted from
    ``_make_tvpi_dpi_chart`` in ``chart_widgets.py``."""
    all_dates = nav.index.union(cf_in.index).union(cf_out.index).sort_values()
    nav_full = nav.reindex(all_dates)
    cf_in_full = cf_in.reindex(all_dates, fill_value=0.0)
    cf_out_full = cf_out.reindex(all_dates, fill_value=0.0)
    cum_cf_in = cf_in_full.cumsum()
    cum_cf_out_abs = cf_out_full.cumsum().abs()
    tvpi = pd.Series(np.nan, index=all_dates, dtype="float64")
    dpi = pd.Series(np.nan, index=all_dates, dtype="float64")
    nonzero = cum_cf_out_abs > 0.0
    tvpi[nonzero] = (nav_full[nonzero] + cum_cf_in[nonzero]) / cum_cf_out_abs[nonzero]
    dpi[nonzero] = cum_cf_in[nonzero] / cum_cf_out_abs[nonzero]
    return pd.DataFrame({"tvpi": tvpi, "dpi": dpi})


def test_qt_consistency_tvpi_dpi() -> None:
    """TVPI/DPI from new analytics matches QT reference at NAV observations."""
    timestamps_qt = pd.DatetimeIndex(
        [
            pd.Timestamp(date(2024, 1, 31), tz="UTC"),
            pd.Timestamp(date(2024, 7, 1), tz="UTC"),
            pd.Timestamp(date(2025, 6, 30), tz="UTC"),
        ]
    )
    cf_in_qt = pd.Series([0.0, 0.0, 30.0], index=timestamps_qt)
    cf_out_qt = pd.Series([-100.0, -50.0, 0.0], index=timestamps_qt)
    nav_index = pd.DatetimeIndex(
        [
            pd.Timestamp(date(2024, 1, 31), tz="UTC"),
            pd.Timestamp(date(2024, 12, 31), tz="UTC"),
            pd.Timestamp(date(2025, 6, 30), tz="UTC"),
        ]
    )
    nav_qt = pd.Series([100.0, 160.0, 200.0], index=nav_index)
    qt_table = _qt_reference_tvpi_dpi(cf_in_qt, cf_out_qt, nav_qt)

    cashflows = _make_actual_cashflows(
        [
            (_ts(2024, 1, 31), "capital_call", -100.0),
            (_ts(2024, 7, 1), "capital_call", -50.0),
            (_ts(2025, 6, 30), "distribution", 30.0),
        ]
    )
    nav_new = pd.Series(
        [100.0, 160.0, 200.0],
        index=[date(2024, 1, 31), date(2024, 12, 31), date(2025, 6, 30)],
    )
    new_table = compute_rolling_multiples(cashflows, nav_new)

    # Compare at the three NAV observations.
    for as_of, expected in zip(
        nav_new.index,
        [
            (
                qt_table.loc[pd.Timestamp(date(2024, 1, 31), tz="UTC"), "tvpi"],
                qt_table.loc[pd.Timestamp(date(2024, 1, 31), tz="UTC"), "dpi"],
            ),
            (
                qt_table.loc[pd.Timestamp(date(2024, 12, 31), tz="UTC"), "tvpi"],
                qt_table.loc[pd.Timestamp(date(2024, 12, 31), tz="UTC"), "dpi"],
            ),
            (
                qt_table.loc[pd.Timestamp(date(2025, 6, 30), tz="UTC"), "tvpi"],
                qt_table.loc[pd.Timestamp(date(2025, 6, 30), tz="UTC"), "dpi"],
            ),
        ],
        strict=True,
    ):
        row = new_table[new_table["as_of_date"] == as_of].iloc[0]
        assert abs(row["tvpi"] - expected[0]) < 1e-9
        assert abs(row["dpi"] - expected[1]) < 1e-9


# ---------------------------------------------------------------------------
# compute_trailing_returns (ADR-0082 §5 / ADR-0079 §1)
# ---------------------------------------------------------------------------


def _constant_growth_tr_index(monthly_growth: float = 0.01) -> pd.Series:
    """Monthly TR index from 2023-01-01 to 2026-06-01 at a constant rate.

    Month-start dates so every window start (``as_of`` minus N whole
    months / years, 1 January) lands exactly on an index date — the asof
    backward search then returns the value at that date, not an earlier one.
    Rebased to 100 at inception; value at month ``k`` is ``100·(1+g)^k``.
    """
    idx = pd.date_range("2023-01-01", periods=42, freq="MS")
    values = [100.0 * (1.0 + monthly_growth) ** k for k in range(len(idx))]
    return pd.Series(values, index=idx, dtype="float64")


def test_trailing_returns_constant_growth_per_window() -> None:
    g = 0.01
    tr = _constant_growth_tr_index(g)
    result = compute_trailing_returns(tr)  # as_of defaults to 2026-06-01

    # Cumulative windows (<= 1 year).
    assert result.m1 == pytest.approx((1.0 + g) ** 1 - 1.0, abs=1e-12)
    assert result.m3 == pytest.approx((1.0 + g) ** 3 - 1.0, abs=1e-12)
    # YTD: 2026-01-01 → 2026-06-01 is five months.
    assert result.ytd == pytest.approx((1.0 + g) ** 5 - 1.0, abs=1e-12)
    assert result.y1 == pytest.approx((1.0 + g) ** 12 - 1.0, abs=1e-12)

    # Annualised windows (> 1 year) — CAGR.
    as_of_ts = pd.Timestamp("2026-06-01")
    years_y3 = (as_of_ts - pd.Timestamp("2023-06-01")).days / 365.25
    expected_y3 = ((1.0 + g) ** 36) ** (1.0 / years_y3) - 1.0
    assert result.y3_annualised == pytest.approx(expected_y3, abs=1e-12)
    # The annualised figure must differ from the raw cumulative 3y return.
    assert result.y3_annualised != pytest.approx((1.0 + g) ** 36 - 1.0, abs=1e-6)

    years_si = (as_of_ts - pd.Timestamp("2023-01-01")).days / 365.25
    expected_si = ((1.0 + g) ** 41) ** (1.0 / years_si) - 1.0
    assert result.since_inception_annualised == pytest.approx(expected_si, abs=1e-12)


def test_trailing_returns_explicit_as_of_truncates() -> None:
    g = 0.01
    tr = _constant_growth_tr_index(g)
    # Evaluate as of 2024-01-01 (month index 12); m1 looks back one month.
    result = compute_trailing_returns(tr, as_of=date(2024, 1, 1))
    assert result.m1 == pytest.approx((1.0 + g) ** 1 - 1.0, abs=1e-12)
    assert result.m3 == pytest.approx((1.0 + g) ** 3 - 1.0, abs=1e-12)


def test_trailing_returns_none_when_history_too_short() -> None:
    """Six months of history → 1Y and 3Y windows have no start datapoint."""
    idx = pd.date_range("2026-01-01", periods=6, freq="MS")
    tr = pd.Series([100.0 * 1.01**k for k in range(6)], index=idx)
    result = compute_trailing_returns(tr)  # as_of = 2026-06-01

    assert result.m1 is not None
    assert result.m3 is not None
    assert result.ytd is not None  # YTD start 2026-01-01 is the first point
    assert result.y1 is None  # 2025-06-01 predates the history
    assert result.y3_annualised is None  # 2023-06-01 predates the history
    # Since-inception is always annualised and always has a start point.
    assert result.since_inception_annualised is not None


def test_trailing_returns_ytd_respects_year_boundary() -> None:
    """YTD measures from 1 January of the as_of year, not 12 months back."""
    idx = pd.to_datetime(
        [
            "2025-11-01",
            "2025-12-01",
            "2026-01-01",
            "2026-02-01",
            "2026-03-01",
        ]
    )
    tr = pd.Series([90.0, 100.0, 110.0, 115.0, 121.0], index=idx)
    result = compute_trailing_returns(tr)  # as_of = 2026-03-01, end = 121

    # YTD base is the 2026-01-01 value (110), NOT the 2025-12-01 value (100).
    assert result.ytd == pytest.approx(121.0 / 110.0 - 1.0, abs=1e-12)
    # m3 reaches back to 2025-12-01 (100) and crosses the year boundary.
    assert result.m3 == pytest.approx(121.0 / 100.0 - 1.0, abs=1e-12)
    # m1 reaches the 2026-02-01 value (115).
    assert result.m1 == pytest.approx(121.0 / 115.0 - 1.0, abs=1e-12)


def test_trailing_returns_empty_index_all_none() -> None:
    result = compute_trailing_returns(pd.Series(dtype="float64"))
    assert result.m1 is None
    assert result.m3 is None
    assert result.ytd is None
    assert result.y1 is None
    assert result.y3_annualised is None
    assert result.since_inception_annualised is None
