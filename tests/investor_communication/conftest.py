# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared fixtures for ``tests/investor_communication/``.

All fixtures construct synthetic in-memory data that is loaded into the
DataStore singleton.  No real Excel file is read.

Each fixture using the DataStore yields after populating it and clears it
afterwards so test order does not matter.
"""

from __future__ import annotations

from collections.abc import Generator

import pandas as pd
import pytest

from core.data_store import get_data_store
from services.reporting.data_providers import ProviderContext


@pytest.fixture
def clean_store() -> Generator[None, None, None]:
    """Yield with the DataStore cleared, and clear again on teardown."""
    store = get_data_store()
    store.clear()
    try:
        yield
    finally:
        store.clear()


@pytest.fixture
def report_date() -> pd.Timestamp:
    """Return a deterministic report date used across the synthetic fixtures."""
    return pd.Timestamp("2024-12-31")


@pytest.fixture
def investments() -> tuple[str, ...]:
    """Three canonical investments used in the synthetic fixtures."""
    return ("Investition A", "Investition B", "Investition C")


@pytest.fixture
def basic_ctx(
    report_date: pd.Timestamp,
    investments: tuple[str, ...],
) -> ProviderContext:
    """Return a :class:`ProviderContext` for the portfolio aggregate."""
    return ProviderContext(
        report_date=report_date,
        all_investments=investments,
        investment_filter=None,
    )


def _build_attributes(investments: tuple[str, ...]) -> pd.DataFrame:
    """Build a synthetic ``attributes`` DataFrame with sector + country breakdowns.

    Args:
        investments: Investment column names.

    Returns:
        DataFrame with the canonical scalar attribute rows followed by a
        sector breakdown block (3 rows) and a country breakdown block
        (2 rows).
    """
    a, b, c = investments
    rows: dict[str, dict[str, object]] = {
        "Investment Type": {a: "Private Equity", b: "Infrastructure", c: "Private Debt"},
        "Investment Sub-Class": {a: "Buyout", b: "Energy", c: "Direct Lending"},
        "Region": {a: "Europe", b: "Europe", c: "USA"},
        "Vintage Year": {a: 2020, b: 2021, c: 2019},
        "Währung": {a: "EUR", b: "EUR", c: "USD"},
        "Asset Class": {a: "Private Equity", b: "Infrastructure", c: "Private Debt"},
        "Manager / Fondsname": {a: "Mgr A", b: "Mgr B", c: "Mgr C"},
        "Tech": {a: 0.6, b: 0.0, c: 0.0},
        "Healthcare": {a: 0.4, b: 0.0, c: 0.0},
        "Energy": {a: 0.0, b: 1.0, c: 0.0},
        "DE": {a: 1.0, b: 0.5, c: 0.0},
        "US": {a: 0.0, b: 0.5, c: 1.0},
    }
    df = pd.DataFrame.from_dict(rows, orient="index", columns=list(investments))
    return df


@pytest.fixture
def sample_attributes(investments: tuple[str, ...]) -> pd.DataFrame:
    """Return a synthetic attributes DataFrame for the three investments."""
    return _build_attributes(investments)


def _build_cashflow_data(
    investments: tuple[str, ...],
    cf_out_sign: int = -1,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build cash_flow_in_actual, cash_flow_out_actual, navs_actual fixtures.

    Args:
        investments: Investment column names.
        cf_out_sign: ``-1`` writes capital calls as negatives (canonical),
            ``+1`` writes them as positives (sign-drift).  Used to test the
            sign-defensive behaviour of the providers.

    Returns:
        ``(cf_in, cf_out, navs)`` — three date-indexed DataFrames.
    """
    a, b, c = investments
    dates_call = [pd.Timestamp("2024-01-31"), pd.Timestamp("2024-04-30")]
    dates_dist = [pd.Timestamp("2024-07-31"), pd.Timestamp("2024-10-31")]
    dates_nav = [pd.Timestamp("2024-11-30"), pd.Timestamp("2024-12-31")]

    sign = float(cf_out_sign)

    cf_in = pd.DataFrame(
        {
            a: [0.0, 0.0, 30_000.0, 50_000.0, 0.0, 0.0],
            b: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            c: [0.0, 0.0, 10_000.0, 20_000.0, 0.0, 0.0],
        },
        index=dates_call + dates_dist + dates_nav,
    ).iloc[:4]

    cf_out = pd.DataFrame(
        {
            a: [sign * 100_000.0, sign * 50_000.0],
            b: [sign * 200_000.0, 0.0],
            c: [sign * 80_000.0, 0.0],
        },
        index=dates_call,
    )

    navs = pd.DataFrame(
        {
            a: [120_000.0, 130_000.0],
            b: [220_000.0, 230_000.0],
            c: [70_000.0, 60_000.0],
        },
        index=dates_nav,
    )

    return cf_in, cf_out, navs


@pytest.fixture
def populated_store_canonical(
    clean_store: None,
    sample_attributes: pd.DataFrame,
    investments: tuple[str, ...],
) -> None:
    """Populate the DataStore with sign-canonical fixture data (CF Out negative).

    Args:
        clean_store: Ensures the DataStore is empty before populating.
        sample_attributes: Synthetic attributes DataFrame.
        investments: Investment names.
    """
    cf_in, cf_out, navs = _build_cashflow_data(investments, cf_out_sign=-1)
    store = get_data_store()
    store.store("attributes", sample_attributes)
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)


@pytest.fixture
def populated_store_positive_out(
    clean_store: None,
    sample_attributes: pd.DataFrame,
    investments: tuple[str, ...],
) -> None:
    """Populate the DataStore with sign-drifted CF Out (positive values).

    Used to verify the providers' sign-defensive behaviour.

    Args:
        clean_store: Ensures the DataStore is empty before populating.
        sample_attributes: Synthetic attributes DataFrame.
        investments: Investment names.
    """
    cf_in, cf_out, navs = _build_cashflow_data(investments, cf_out_sign=+1)
    store = get_data_store()
    store.store("attributes", sample_attributes)
    store.store("cash_flow_in_actual", cf_in)
    store.store("cash_flow_out_actual", cf_out)
    store.store("navs_actual", navs)
