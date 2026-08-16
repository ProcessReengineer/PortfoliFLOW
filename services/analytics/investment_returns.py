# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-investment return calculations.

Pure-Python migration of the QT calculation logic in
``modules/front_office/charts.py`` (and its widget twin in
``gui/widgets/chart_widgets.py``). The QT widgets read from the
in-memory ``DataStore`` and call matplotlib in the same function;
this module is the calculation half — DB-free, Qt-free,
matplotlib-free — that both the web side (sub-stream 5b) and the
Phase-6 GUI-on-Postgres reorientation (ADR-0033 follow-up) consume.

Functions take pandas DataFrames or Series as arguments and return
plain pandas objects. Per ADR-0045 §3 they never reach into the
database directly: callers extract data via the appropriate
repositories and pass the DataFrames / Series in.

Cashflow sign convention (see Phase-4 :class:`InvestmentCashflow`
and ADR-0043 §1): ``amount`` is signed. Capital calls and fees are
negative; distributions, dividends, and coupons are positive. The
QT formulas in ``chart_widgets.py`` use ``cf_in`` (positive
distributions) and ``cf_out`` (negative calls) with the identity
``NCG = NAV + cumsum(cf_in) + cumsum(cf_out)`` — equivalent here to
``NCG = NAV + cumsum(amount)``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from services.analytics._dtos import TrailingReturns
from services.reporting.data_providers._calculations import compute_irr


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_cashflows_for_irr(
    cashflows: pd.DataFrame,
) -> tuple[pd.Series, pd.Series]:
    """Split a flat actuals-only cashflow frame into the (cf_in, cf_out) shape.

    The :func:`services.reporting.data_providers._calculations.compute_irr`
    helper expects two date-indexed Series — distribution-side
    inflows (``cf_in``, positive) and capital-call-side outflows
    (``cf_out``, signed) — that mirror the Phase-2 Excel sheets. The
    Phase-4 ``investment_cashflows`` table is flat: every row has a
    ``flow_type`` and a signed ``amount``. We partition by sign rather
    than by ``flow_type`` so secondary types (fees, carry, dividends,
    coupons, other) flow through the IRR engine via the side that
    matches their sign — fees and carry as outflows, dividends /
    coupons as inflows. This is the same partitioning that the QT
    chart code applies indirectly by aggregating the four Cash-Flow-In
    / Cash-Flow-Out sheets.

    Args:
        cashflows: DataFrame with at least the columns
            ``flow_timestamp`` and ``amount``. Caller has already
            filtered to ``flow_kind = 'actual'``.

    Returns:
        ``(cf_in, cf_out)`` — both indexed by ``pd.Timestamp``.
        ``cf_in`` aggregates positive amounts; ``cf_out`` aggregates
        negative amounts. Empty Series when no rows fall on a side.
    """
    if cashflows.empty:
        empty_idx = pd.DatetimeIndex([], tz="UTC")
        empty = pd.Series(dtype="float64", index=empty_idx)
        return empty, empty.copy()

    df = cashflows.copy()
    df["flow_timestamp"] = pd.to_datetime(df["flow_timestamp"], utc=True).dt.normalize()
    df["amount"] = df["amount"].astype("float64")

    in_mask = df["amount"] > 0.0
    out_mask = df["amount"] < 0.0

    cf_in = (
        df.loc[in_mask, ["flow_timestamp", "amount"]]
        .groupby("flow_timestamp")["amount"]
        .sum()
        .sort_index()
    )
    cf_out = (
        df.loc[out_mask, ["flow_timestamp", "amount"]]
        .groupby("flow_timestamp")["amount"]
        .sum()
        .sort_index()
    )
    return cf_in, cf_out


def _signed_cashflow_series(cashflows: pd.DataFrame) -> pd.Series:
    """Aggregate a flat actuals cashflow frame to a signed timestamp series.

    Args:
        cashflows: DataFrame with ``flow_timestamp`` and ``amount``.

    Returns:
        A pandas Series indexed by ``pd.Timestamp``, values are the
        sum of signed amounts per timestamp. Sorted ascending. Empty
        series when ``cashflows`` is empty.
    """
    if cashflows.empty:
        return pd.Series(dtype="float64")
    df = cashflows.copy()
    df["flow_timestamp"] = pd.to_datetime(df["flow_timestamp"], utc=True).dt.normalize()
    df["amount"] = df["amount"].astype("float64")
    return df.groupby("flow_timestamp")["amount"].sum().sort_index()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_total_return_series(nav_series: pd.Series) -> pd.Series:
    """Compute the period-over-period Total Return series from NAVs.

    Mirrors the QT methodology: each periodic return is
    ``(NAV[t] - NAV[t-1]) / NAV[t-1]`` — i.e. ``pct_change()`` on the
    chronologically sorted NAV series. The first datapoint has no
    predecessor and is dropped (NaN under ``pct_change``); rows where
    a NAV is missing are dropped before computation so the series is
    free of NaN.

    Args:
        nav_series: Pandas Series indexed by ``as_of_date`` (or any
            sortable date-like index), values are NAV amounts. May
            contain NaN (silently dropped). Must contain at least two
            non-NaN datapoints to produce a non-empty result.

    Returns:
        Pandas Series indexed by the *later* date of each pair, values
        are decimal returns (``0.05`` for +5 %). Empty when fewer than
        two non-NaN NAVs are supplied.
    """
    cleaned = nav_series.dropna().sort_index()
    if len(cleaned) < 2:
        return pd.Series(dtype="float64")
    return cleaned.pct_change().dropna()


def compute_cashflow_adjusted_return_series(
    nav_series: pd.Series,
    cashflows: pd.DataFrame,
) -> pd.Series:
    """Period-over-period return series corrected for capital flows.

    For each consecutive pair of NAV observations the market return is

        r_t = (NAV_t + a_t) / NAV_{t-1} - 1

    where ``a_t`` is the net SIGNED cashflow amount falling in the
    half-open, upper-inclusive interval ``(date_{t-1}, date_t]``:
    distributions positive, calls negative — the same ``amount``
    convention as :func:`compute_net_capital_gain`. A flow dated
    exactly on the later NAV date ``date_t`` belongs to the interval
    ending at ``date_t``. With no flows in the interval ``a_t = 0``
    and the formula reduces exactly to ``pct_change()``, so liquid
    investments are unchanged bit-for-bit relative to
    :func:`compute_total_return_series`.

    Aggregating signed ``amount`` over ``(date_{t-1}, date_t]`` is the
    discrete-time form of the ``NCG = NAV + cumsum(amount)`` identity
    used in :func:`compute_net_capital_gain`; the two are mutually
    consistent by construction. See ADR-0066.

    Args:
        nav_series: Pandas Series indexed by ``as_of_date`` (or any
            sortable date-like index), values are NAV amounts. May
            contain NaN (silently dropped). Must contain at least two
            non-NaN datapoints to produce a non-empty result.
        cashflows: Flat actuals cashflow frame with at least the
            columns ``flow_timestamp`` and signed ``amount``. Caller
            has already filtered to ``flow_kind = 'actual'``. May be
            empty, in which case every ``a_t = 0``.

    Returns:
        Pandas Series indexed by the *later* date of each pair (same
        index shape as :func:`compute_total_return_series`), values
        are decimal returns (``0.05`` for +5 %). Empty when fewer than
        two non-NaN NAVs are supplied.
    """
    cleaned = nav_series.dropna().sort_index()
    if len(cleaned) < 2:
        return pd.Series(dtype="float64")

    # UTC-normalised signed cashflow series, matching the timestamp
    # normalisation used throughout this module so the interval
    # comparison is apples-to-apples.
    cf_signed = _signed_cashflow_series(cashflows)

    # NAV dates in the same UTC-normalised timestamp space as the
    # cashflows, so the half-open interval comparison aligns.
    nav_ts = pd.to_datetime(cleaned.index, utc=True)

    later_dates = cleaned.index[1:]
    nav_prev = cleaned.to_numpy(dtype="float64")[:-1]
    nav_curr = cleaned.to_numpy(dtype="float64")[1:]

    returns: list[float] = []
    for i in range(len(later_dates)):
        lower = nav_ts[i]
        upper = nav_ts[i + 1]
        if cf_signed.empty:
            a_t = 0.0
        else:
            in_interval = (cf_signed.index > lower) & (cf_signed.index <= upper)
            a_t = float(cf_signed[in_interval].sum())
        returns.append((nav_curr[i] + a_t) / nav_prev[i] - 1.0)

    return pd.Series(returns, index=later_dates, dtype="float64", name=cleaned.name)


def compute_net_capital_gain(
    cashflows: pd.DataFrame,
    nav_series: pd.Series,
    as_of_date: date | None = None,
) -> pd.Series:
    """Compute the Net Capital Gain time series.

    The QT chart code defines NCG as
    ``NCG[t] = NAV[t] + cumsum(cf_in)[t] + cumsum(cf_out)[t]`` where
    ``cf_in`` is positive (distributions) and ``cf_out`` is negative
    (capital calls). With the Phase-4 signed-amount convention this
    becomes ``NCG[t] = NAV[t] + cumsum(amount)[t]`` evaluated on the
    union of NAV and cashflow dates.

    Args:
        cashflows: DataFrame with at least ``flow_timestamp`` and
            ``amount``. Caller has already filtered to
            ``flow_kind = 'actual'`` — the function does not see plan
            rows.
        nav_series: Pandas Series indexed by ``as_of_date``, values
            are NAV amounts. May contain NaN.
        as_of_date: Optional truncation date. When supplied, the
            output is restricted to entries on or before this date.

    Returns:
        Pandas Series indexed by ``pd.Timestamp`` covering the union
        of NAV dates and cashflow timestamps, values in investment
        currency. Empty when both inputs are empty.
    """
    nav_clean = nav_series.dropna().sort_index()
    nav_ts = pd.Series(
        nav_clean.values,
        index=pd.to_datetime(nav_clean.index, utc=True),
        dtype="float64",
    )
    cf_signed = _signed_cashflow_series(cashflows)

    if nav_ts.empty and cf_signed.empty:
        return pd.Series(dtype="float64")

    all_dates = nav_ts.index.union(cf_signed.index).sort_values()
    nav_full = nav_ts.reindex(all_dates)
    cf_full = cf_signed.reindex(all_dates, fill_value=0.0)
    ncg = nav_full + cf_full.cumsum()

    if as_of_date is not None:
        cutoff = pd.Timestamp(as_of_date, tz="UTC")
        ncg = ncg[ncg.index <= cutoff]
    return ncg


def compute_rolling_multiples(
    cashflows: pd.DataFrame,
    nav_series: pd.Series,
) -> pd.DataFrame:
    """Compute TVPI / DPI / RVPI per NAV observation.

    Observations are the dates in ``nav_series``. For each one, calls
    and distributions are cumulated up to and including that date,
    and the multiples are evaluated as

    ``TVPI = (NAV + cumDistributions) / |cumCalls|``,
    ``DPI  = cumDistributions / |cumCalls|``,
    ``RVPI = NAV / |cumCalls|``.

    Where the cumulative-calls magnitude is zero the row's multiples
    are NaN — division-by-zero is guarded the same way as in the QT
    chart.

    Args:
        cashflows: DataFrame with at least ``flow_timestamp`` and
            ``amount``. Caller has already filtered to actuals.
        nav_series: Pandas Series indexed by ``as_of_date``.

    Returns:
        DataFrame with columns ``as_of_date``, ``tvpi``, ``dpi``,
        ``rvpi``. One row per non-NaN NAV observation, sorted
        ascending. Empty DataFrame (with the four named columns) when
        no NAV datapoints are supplied.
    """
    cols = ["as_of_date", "tvpi", "dpi", "rvpi"]
    nav_clean = nav_series.dropna().sort_index()
    if nav_clean.empty:
        return pd.DataFrame(columns=cols)

    if not cashflows.empty:
        df_cf = cashflows.copy()
        df_cf["flow_timestamp"] = pd.to_datetime(df_cf["flow_timestamp"], utc=True).dt.normalize()
        df_cf["amount"] = df_cf["amount"].astype("float64")
        cum_calls_mag = (
            df_cf.assign(call_mag=df_cf["amount"].clip(upper=0.0).abs())
            .groupby("flow_timestamp")["call_mag"]
            .sum()
            .sort_index()
            .cumsum()
        )
        cum_distributions = (
            df_cf.assign(dist=df_cf["amount"].clip(lower=0.0))
            .groupby("flow_timestamp")["dist"]
            .sum()
            .sort_index()
            .cumsum()
        )
    else:
        empty_idx = pd.DatetimeIndex([], tz="UTC")
        cum_calls_mag = pd.Series(dtype="float64", index=empty_idx)
        cum_distributions = pd.Series(dtype="float64", index=empty_idx)

    rows = []
    for as_of, nav_value in nav_clean.items():
        ts = pd.Timestamp(as_of, tz="UTC")
        calls_to_date = cum_calls_mag.loc[cum_calls_mag.index <= ts]
        distributions_to_date_series = cum_distributions.loc[cum_distributions.index <= ts]
        calls_mag_to_date = float(calls_to_date.iloc[-1]) if not calls_to_date.empty else 0.0
        distributions_to_date = (
            float(distributions_to_date_series.iloc[-1])
            if not distributions_to_date_series.empty
            else 0.0
        )
        if calls_mag_to_date <= 0.0:
            tvpi = float("nan")
            dpi = float("nan")
            rvpi = float("nan")
        else:
            tvpi = (float(nav_value) + distributions_to_date) / calls_mag_to_date
            dpi = distributions_to_date / calls_mag_to_date
            rvpi = float(nav_value) / calls_mag_to_date
        rows.append(
            {
                "as_of_date": as_of,
                "tvpi": tvpi,
                "dpi": dpi,
                "rvpi": rvpi,
            }
        )
    return pd.DataFrame(rows, columns=cols)


def compute_rolling_irr_since_inception(
    cashflows: pd.DataFrame,
    nav_series: pd.Series,
) -> pd.Series:
    """Compute rolling XIRR-since-inception per NAV observation.

    For each NAV datapoint, the IRR is evaluated on the actual cash
    flows up to and including that date with the NAV at the
    observation as a synthetic positive terminal cashflow. Reuses
    :func:`services.reporting.data_providers._calculations.compute_irr`
    unchanged, so the Brent-method IRR engine matches Phase-4's
    reporting output bit-for-bit.

    When ``compute_irr`` cannot converge for a given observation
    (fewer than two cashflows, monotone sign, root finder bracketing
    failure) the entry is :data:`numpy.nan`. No exception is raised.

    Args:
        cashflows: DataFrame with ``flow_timestamp`` and ``amount``.
            Actuals only.
        nav_series: Pandas Series indexed by ``as_of_date``.

    Returns:
        Pandas Series indexed by ``as_of_date`` with IRR values as
        decimals (``0.137`` for 13.7 %). Empty when ``nav_series``
        has no non-NaN entries.
    """
    nav_clean = nav_series.dropna().sort_index()
    if nav_clean.empty:
        return pd.Series(dtype="float64")

    cf_in, cf_out = _split_cashflows_for_irr(cashflows)

    irrs: list[float] = []
    for as_of, nav_value in nav_clean.items():
        report_ts = pd.Timestamp(as_of, tz="UTC")
        irr = compute_irr(
            cf_in=cf_in,
            cf_out=cf_out,
            nav_value=float(nav_value),
            report_date=report_ts,
        )
        irrs.append(irr)
    return pd.Series(irrs, index=nav_clean.index, dtype="float64")


def _asof_index_value(ser: pd.Series, when: pd.Timestamp) -> float | None:
    """Index value at or before ``when`` via backward search.

    Args:
        ser: Total-return-index Series with a monotonic-increasing
            ``DatetimeIndex``.
        when: The cut-off timestamp.

    Returns:
        The last value whose index is ``<= when``, or ``None`` when no such
        datapoint exists (``when`` precedes the first observation).
    """
    if ser.empty:
        return None
    value = ser.asof(when)
    if pd.isna(value):
        return None
    return float(value)


def compute_trailing_returns(
    tr_index: pd.Series,
    *,
    as_of: date | None = None,
) -> TrailingReturns:
    """Trailing total-weighted returns over standard factsheet windows.

    Consumes a **total-return-index** series — cumulative growth, e.g.
    rebased to 100 at inception — the same series the mark-to-market hero
    tile draws (ADR-0082 §2). For each window the index value at the
    window start is taken by backward search (last value at or before the
    start date), and the period return is ``tr_index[as_of] /
    tr_index[start] - 1``.

    Windows up to and including one year are cumulative; windows longer
    than one year are annualised (CAGR):

    - ``m1`` / ``m3`` / ``ytd`` / ``y1`` — cumulative period return.
    - ``y3_annualised`` / ``since_inception_annualised`` —
      ``(tr_index[as_of] / tr_index[start]) ** (1 / years) - 1`` with
      ``years = (as_of - start).days / 365.25``.

    Window starts relative to ``as_of``: one / three calendar months back;
    1 January of the ``as_of`` year (YTD); one / three calendar years
    back; and the first datapoint (since-inception). A window whose start
    predates the available history — no datapoint at or before it — yields
    ``None`` for that field (no silent fallback).

    Args:
        tr_index: Total-return-index Series, date-indexed and ordered
            chronologically (sorted internally for safety). May contain
            ``NaN`` (dropped).
        as_of: Evaluation date. Defaults to the last index date.

    Returns:
        A :class:`~services.analytics._dtos.TrailingReturns` bundle. Every
        field is ``None`` when the history does not reach the window start
        (and all fields are ``None`` for an empty input).
    """
    cleaned = tr_index.dropna().sort_index()
    if cleaned.empty:
        return TrailingReturns(None, None, None, None, None, None)

    ser = pd.Series(
        cleaned.to_numpy(dtype="float64"),
        index=pd.to_datetime(cleaned.index),
    ).sort_index()

    as_of_ts = ser.index[-1] if as_of is None else pd.Timestamp(as_of)
    end_value = _asof_index_value(ser, as_of_ts)

    def _window_return(start_ts: pd.Timestamp, *, annualise: bool) -> float | None:
        start_value = _asof_index_value(ser, start_ts)
        if end_value is None or start_value is None or start_value == 0.0:
            return None
        ratio = end_value / start_value
        if not annualise:
            return ratio - 1.0
        years = (as_of_ts - start_ts).days / 365.25
        if years <= 0.0:
            return None
        return ratio ** (1.0 / years) - 1.0

    ytd_start = pd.Timestamp(year=as_of_ts.year, month=1, day=1)
    return TrailingReturns(
        m1=_window_return(as_of_ts - pd.DateOffset(months=1), annualise=False),
        m3=_window_return(as_of_ts - pd.DateOffset(months=3), annualise=False),
        ytd=_window_return(ytd_start, annualise=False),
        y1=_window_return(as_of_ts - pd.DateOffset(years=1), annualise=False),
        y3_annualised=_window_return(as_of_ts - pd.DateOffset(years=3), annualise=True),
        since_inception_annualised=_window_return(ser.index[0], annualise=True),
    )
