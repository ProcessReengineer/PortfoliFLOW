# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.data_providers.TotalReturnTimeseriesProvider`."""

from __future__ import annotations

import pandas as pd
import pytest

from core.data_store import get_data_store
from services.reporting.data_providers import (
    ProviderContext,
    TotalReturnTimeseriesProvider,
)


def _ctx(filter_: str | None) -> ProviderContext:
    return ProviderContext(
        report_date=pd.Timestamp("2024-12-31"),
        all_investments=("A", "B"),
        investment_filter=filter_,
    )


def test_rebased_values_for_known_returns(clean_store: None) -> None:  # noqa: ARG001
    """Daily returns 0.01, 0.02, -0.01 → rebased 100, 102, 100.98."""
    df = pd.DataFrame(
        {
            "A": [0.01, 0.02, -0.01],
            "B": [0.0, 0.0, 0.0],
        },
        index=[
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-01-03"),
            pd.Timestamp("2024-01-04"),
        ],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert list(result.columns) == ["rebased"]
    values = result["rebased"].tolist()
    assert values[0] == pytest.approx(100.0)
    assert values[1] == pytest.approx(102.0)
    assert values[2] == pytest.approx(100.98)


def test_first_value_is_exactly_100(clean_store: None) -> None:  # noqa: ARG001
    """Regardless of the first return value, the rebased series begins at 100.0."""
    df = pd.DataFrame(
        {"A": [0.05, 0.03, -0.02]},
        index=[
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-01-03"),
            pd.Timestamp("2024-01-04"),
        ],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert result["rebased"].iloc[0] == pytest.approx(100.0)


def test_all_nan_returns_empty(clean_store: None) -> None:  # noqa: ARG001
    """All-NaN total return for the investment yields an empty DataFrame."""
    df = pd.DataFrame(
        {"A": [None, None, None]},
        index=[
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-01-03"),
            pd.Timestamp("2024-01-04"),
        ],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert result.empty
    assert list(result.columns) == ["rebased"]


def test_missing_investment_column_returns_empty(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Investment column missing from ``total_return_actual`` yields empty."""
    df = pd.DataFrame(
        {"X": [0.01]},
        index=[pd.Timestamp("2024-01-02")],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert result.empty


def test_dataset_missing_returns_empty(
    clean_store: None,  # noqa: ARG001
) -> None:
    """``total_return_actual`` not in DataStore yields empty."""
    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert result.empty


def test_portfolio_mode_returns_empty(clean_store: None) -> None:  # noqa: ARG001
    """``investment_filter is None`` yields empty (provider is per-investment-only)."""
    df = pd.DataFrame(
        {"A": [0.01, 0.02]},
        index=[pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx(None))
    assert result.empty


def test_single_value_series(clean_store: None) -> None:  # noqa: ARG001
    """A single non-NaN value rebases to exactly 100.0."""
    df = pd.DataFrame(
        {"A": [0.05]},
        index=[pd.Timestamp("2024-06-30")],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert len(result) == 1
    assert result["rebased"].iloc[0] == pytest.approx(100.0)


def test_respects_report_date(clean_store: None) -> None:  # noqa: ARG001
    """Returns after ``ctx.report_date`` are excluded from the rebased series."""
    df = pd.DataFrame(
        {"A": [0.01, 0.02, 0.03]},
        index=[
            pd.Timestamp("2024-12-30"),
            pd.Timestamp("2024-12-31"),
            pd.Timestamp("2025-01-02"),
        ],
    )
    store = get_data_store()
    store.store("total_return_actual", df)

    result = TotalReturnTimeseriesProvider().get(_ctx("A"))
    assert len(result) == 2
    assert result.index[-1] == pd.Timestamp("2024-12-31")
