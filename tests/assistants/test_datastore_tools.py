# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :mod:`services.tools.datastore_tools`.

Tests call the tool functions directly (not through the ToolRegistry) so
that they can be run without a QApplication.  The DataStore singleton is
used throughout; fixtures handle setup and teardown.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.data_store import get_data_store
from services.results_serialization import (
    PRODUCER_FO_OPTIMIZER,
    PRODUCER_SCRAPER,
    RESULT_TYPE_FINDINGS,
    RESULT_TYPE_TANGENCY,
    build_analysis_metadata,
)
from services.tools.datastore_tools import (
    get_dataset_slice,
    get_dataset_summary,
    list_analysis_results,
    list_datasets,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_datastore():
    """Populate DataStore with a 100-row DatetimeIndex test DataFrame and clean up.

    Yields:
        The populated DataStore instance.
    """
    store = get_data_store()
    store.clear()  # clean slate
    dates = pd.date_range("2020-01-01", periods=100, freq="D")
    rng = np.random.default_rng(42)
    df = pd.DataFrame(
        rng.standard_normal((100, 5)),
        index=dates,
        columns=["Fund A", "Fund B", "Fund C", "Fund D", "Fund E"],
    )
    store.store(
        "test_navs",
        df,
        metadata={"source": "test_file.xlsx", "import_time": "2024-01-01T10:00:00"},
    )
    yield store
    store.clear()


@pytest.fixture
def empty_datastore():
    """Ensure the DataStore is empty before the test and clean up after.

    Yields:
        The empty DataStore instance.
    """
    store = get_data_store()
    store.clear()
    yield store
    store.clear()


# ---------------------------------------------------------------------------
# list_datasets
# ---------------------------------------------------------------------------


class TestListDatasets:
    """Tests for :func:`~services.tools.datastore_tools.list_datasets`."""

    def test_list_datasets_empty(self, empty_datastore) -> None:
        """Empty DataStore returns the 'no datasets' message."""
        result = list_datasets()
        assert "No datasets" in result
        assert "Data Import" in result

    def test_list_datasets_with_data(self, loaded_datastore) -> None:
        """Populated DataStore output contains name, shape, and columns."""
        result = list_datasets()
        assert "test_navs" in result
        assert "100" in result  # row count
        assert "Fund A" in result

    def test_list_datasets_shows_date_range(self, loaded_datastore) -> None:
        """Output includes the date range for DatetimeIndex datasets."""
        result = list_datasets()
        assert "2020-01-01" in result
        assert "2020-04-09" in result  # 100 days from 2020-01-01

    def test_list_datasets_shows_metadata(self, loaded_datastore) -> None:
        """Output includes metadata fields (source, import_time)."""
        result = list_datasets()
        assert "test_file.xlsx" in result
        assert "2024-01-01T10:00:00" in result

    def test_list_datasets_truncates_long_column_list(self, empty_datastore) -> None:
        """More than 10 columns shows first 10 and '... and N more'."""
        store = get_data_store()
        df = pd.DataFrame(np.zeros((5, 15)), columns=[f"Col{i}" for i in range(15)])
        store.store("wide_df", df)
        result = list_datasets()
        assert "Col0" in result
        assert "and 5 more" in result


# ---------------------------------------------------------------------------
# get_dataset_summary
# ---------------------------------------------------------------------------


class TestGetDatasetSummary:
    """Tests for :func:`~services.tools.datastore_tools.get_dataset_summary`."""

    def test_get_dataset_summary_valid(self, loaded_datastore) -> None:
        """Valid dataset returns summary containing descriptive statistics."""
        result = get_dataset_summary("test_navs")
        assert "test_navs" in result
        assert "100" in result
        assert "Fund A" in result
        # describe() includes 'mean', 'std', 'min', 'max'
        assert "mean" in result

    def test_get_dataset_summary_not_found(self, empty_datastore) -> None:
        """Non-existent dataset returns error mentioning list_datasets."""
        result = get_dataset_summary("does_not_exist")
        assert "does_not_exist" in result
        assert "list_datasets" in result

    def test_get_dataset_summary_includes_index_info(self, loaded_datastore) -> None:
        """DatetimeIndex datasets show the date range in summary."""
        result = get_dataset_summary("test_navs")
        assert "DatetimeIndex" in result
        assert "2020-01-01" in result

    def test_get_dataset_summary_truncated_at_2000_chars(self, empty_datastore) -> None:
        """Output is capped at approximately 2000 characters."""
        store = get_data_store()
        rng = np.random.default_rng(0)
        # 50 numeric columns → very wide describe() output
        df = pd.DataFrame(rng.standard_normal((20, 50)))
        store.store("very_wide", df)
        result = get_dataset_summary("very_wide")
        assert len(result) <= 2100  # allow small tolerance for truncation suffix

    def test_get_dataset_summary_many_columns_note(self, empty_datastore) -> None:
        """More than 10 numeric columns produces a note about truncation."""
        store = get_data_store()
        rng = np.random.default_rng(0)
        df = pd.DataFrame(rng.standard_normal((10, 15)), columns=[f"F{i}" for i in range(15)])
        store.store("many_cols", df)
        result = get_dataset_summary("many_cols")
        assert "10 of 15" in result or "get_dataset_slice" in result


# ---------------------------------------------------------------------------
# get_dataset_slice
# ---------------------------------------------------------------------------


class TestGetDatasetSlice:
    """Tests for :func:`~services.tools.datastore_tools.get_dataset_slice`."""

    def test_get_dataset_slice_basic(self, loaded_datastore) -> None:
        """Unfiltered slice returns header and tabular data."""
        result = get_dataset_slice("test_navs")
        assert "test_navs" in result
        assert "Fund A" in result

    def test_get_dataset_slice_not_found(self, empty_datastore) -> None:
        """Non-existent dataset returns error message."""
        result = get_dataset_slice("missing_dataset")
        assert "missing_dataset" in result
        assert "list_datasets" in result

    def test_get_dataset_slice_column_filter(self, loaded_datastore) -> None:
        """Only requested columns appear in output."""
        result = get_dataset_slice("test_navs", columns=["Fund A", "Fund C"])
        assert "Fund A" in result
        assert "Fund C" in result
        assert "Fund B" not in result

    def test_get_dataset_slice_date_filter(self, loaded_datastore) -> None:
        """Date filter restricts rows to the given range."""
        result = get_dataset_slice(
            "test_navs",
            start_date="2020-01-10",
            end_date="2020-01-20",
        )
        assert "test_navs" in result
        # Should contain dates within range
        assert "2020-01-10" in result or "2020-01-15" in result

    def test_get_dataset_slice_last_n(self, loaded_datastore) -> None:
        """last_n_rows returns the correct tail of the dataset."""
        result = get_dataset_slice("test_navs", last_n_rows=3)
        # 100-row dataset, last 3 rows end on 2020-04-09
        assert "2020-04-09" in result
        assert "3 rows" in result

    def test_get_dataset_slice_row_limit(self, empty_datastore) -> None:
        """Datasets with more than 50 rows show omission note."""
        store = get_data_store()
        dates = pd.date_range("2021-01-01", periods=100, freq="D")
        rng = np.random.default_rng(1)
        df = pd.DataFrame(rng.standard_normal((100, 2)), index=dates, columns=["X", "Y"])
        store.store("hundred_rows", df)
        result = get_dataset_slice("hundred_rows")
        assert "omitted" in result

    def test_get_dataset_slice_missing_column(self, loaded_datastore) -> None:
        """Requesting a non-existent column produces a warning but no crash."""
        result = get_dataset_slice("test_navs", columns=["Fund A", "NonExistent"])
        assert "Warning" in result
        assert "NonExistent" in result
        # Should still return data for Fund A
        assert "Fund A" in result

    def test_get_dataset_slice_all_missing_columns(self, loaded_datastore) -> None:
        """Requesting only non-existent columns returns an error message."""
        result = get_dataset_slice("test_navs", columns=["Ghost"])
        assert "No requested columns" in result

    def test_get_dataset_slice_default_column_limit(self, empty_datastore) -> None:
        """More than 8 columns without explicit selection shows only first 8."""
        store = get_data_store()
        df = pd.DataFrame(np.zeros((5, 12)), columns=[f"Col{i}" for i in range(12)])
        store.store("wide_slice", df)
        result = get_dataset_slice("wide_slice")
        assert "8 of 12" in result

    def test_get_dataset_slice_invalid_date(self, loaded_datastore) -> None:
        """Invalid date format returns a meaningful error message."""
        result = get_dataset_slice("test_navs", start_date="not-a-date")
        assert "Invalid date format" in result or "date" in result.lower()


# ---------------------------------------------------------------------------
# list_analysis_results
# ---------------------------------------------------------------------------


class TestListAnalysisResults:
    """Tests for :func:`~services.tools.datastore_tools.list_analysis_results`."""

    def test_no_analysis_results_in_store(self, empty_datastore) -> None:
        """Empty store returns the generic 'no analysis results' message."""
        result = list_analysis_results()
        assert "No analysis results are currently loaded" in result
        assert (
            "Portfolio Optimiser" in result
            or "Optimizer" in result.lower()
            or "Optimiser" in result
        )
        assert "list_datasets" in result

    def test_no_analysis_results_with_producer_filter(self, empty_datastore) -> None:
        """Empty store with producer filter returns the producer-filtered message."""
        result = list_analysis_results(producer="fo_optimizer")
        assert "fo_optimizer" in result
        assert "list_analysis_results" in result

    def test_single_fo_result(self, empty_datastore) -> None:
        """A single FO result is described with all expected fields."""
        store = get_data_store()
        df = pd.DataFrame(
            {
                "asset": ["A", "B", "C"],
                "weight": [0.4, 0.3, 0.3],
                "expected_return": [0.08, 0.08, 0.08],
                "volatility": [0.12, 0.12, 0.12],
                "sharpe_ratio": [0.5, 0.5, 0.5],
            }
        )
        meta = build_analysis_metadata(
            PRODUCER_FO_OPTIMIZER,
            RESULT_TYPE_TANGENCY,
            risk_free_rate=0.025,
            n_assets=3,
            asset_columns=["A", "B", "C"],
        )
        store.store("analysis_results.fo_optimizer.tangency", df, metadata=meta)

        result = list_analysis_results()
        assert "analysis_results.fo_optimizer.tangency" in result
        assert "Producer: fo_optimizer" in result
        assert "Result type: tangency" in result
        assert "risk_free_rate=0.025" in result
        assert "n_assets=3" in result

    def test_producer_filter_match(self, empty_datastore) -> None:
        """Producer filter that matches returns the same dataset block."""
        store = get_data_store()
        df = pd.DataFrame(
            {
                "asset": ["A", "B"],
                "weight": [0.5, 0.5],
                "expected_return": [0.06, 0.06],
                "volatility": [0.10, 0.10],
                "sharpe_ratio": [0.4, 0.4],
            }
        )
        meta = build_analysis_metadata(
            PRODUCER_FO_OPTIMIZER,
            RESULT_TYPE_TANGENCY,
            risk_free_rate=0.02,
            n_assets=2,
            asset_columns=["A", "B"],
        )
        store.store("analysis_results.fo_optimizer.tangency", df, metadata=meta)

        result = list_analysis_results(producer="fo_optimizer")
        assert "analysis_results.fo_optimizer.tangency" in result
        assert "Producer: fo_optimizer" in result

    def test_producer_filter_no_match(self, empty_datastore) -> None:
        """Producer filter with no match returns the producer-filtered empty message."""
        store = get_data_store()
        df = pd.DataFrame(
            {
                "asset": ["A"],
                "weight": [1.0],
                "expected_return": [0.05],
                "volatility": [0.10],
                "sharpe_ratio": [0.3],
            }
        )
        meta = build_analysis_metadata(
            PRODUCER_FO_OPTIMIZER,
            RESULT_TYPE_TANGENCY,
            risk_free_rate=0.02,
            n_assets=1,
            asset_columns=["A"],
        )
        store.store("analysis_results.fo_optimizer.tangency", df, metadata=meta)

        result = list_analysis_results(producer="scraper")
        assert "scraper" in result
        assert "list_analysis_results" in result

    def test_mixed_datasets_filters_to_analysis_only(self, empty_datastore) -> None:
        """Non-analysis datasets are excluded from the listing."""
        store = get_data_store()

        df_fo = pd.DataFrame(
            {
                "asset": ["A"],
                "weight": [1.0],
                "expected_return": [0.05],
                "volatility": [0.10],
                "sharpe_ratio": [0.3],
            }
        )
        meta_fo = build_analysis_metadata(
            PRODUCER_FO_OPTIMIZER,
            RESULT_TYPE_TANGENCY,
            risk_free_rate=0.02,
            n_assets=1,
            asset_columns=["A"],
        )
        store.store("analysis_results.fo_optimizer.tangency", df_fo, metadata=meta_fo)

        # Non-analysis dataset
        dates = pd.date_range("2020-01-01", periods=10, freq="D")
        df_navs = pd.DataFrame(np.zeros((10, 2)), index=dates, columns=["Fund A", "Fund B"])
        store.store("navs_actual", df_navs, metadata={"source": "test.xlsx"})

        result = list_analysis_results()
        assert "analysis_results.fo_optimizer.tangency" in result
        assert "navs_actual" not in result

    def test_sort_order_by_computed_at_descending(self, empty_datastore) -> None:
        """Datasets are listed in descending order of computed_at timestamp."""
        store = get_data_store()

        df = pd.DataFrame(
            {
                "asset": ["A"],
                "weight": [1.0],
                "expected_return": [0.05],
                "volatility": [0.10],
                "sharpe_ratio": [0.3],
            }
        )

        # Older dataset (manually constructed metadata to control timestamp)
        older_meta = {
            "producer": PRODUCER_FO_OPTIMIZER,
            "result_type": RESULT_TYPE_TANGENCY,
            "computed_at": "2026-01-01T10:00:00+00:00",
            "risk_free_rate": 0.02,
            "n_assets": 1,
            "asset_columns": ["A"],
        }
        store.store("analysis_results.fo_optimizer.tangency", df, metadata=older_meta)

        # Newer dataset
        newer_meta = {
            "producer": PRODUCER_FO_OPTIMIZER,
            "result_type": "min_var",
            "computed_at": "2026-04-28T10:00:00+00:00",
            "risk_free_rate": 0.02,
            "n_assets": 1,
            "asset_columns": ["A"],
        }
        store.store("analysis_results.fo_optimizer.min_var", df, metadata=newer_meta)

        result = list_analysis_results()
        # The newer dataset's name appears before the older one in the output
        idx_min_var = result.find("analysis_results.fo_optimizer.min_var")
        idx_tangency = result.find("analysis_results.fo_optimizer.tangency")
        assert idx_min_var >= 0 and idx_tangency >= 0
        assert idx_min_var < idx_tangency

    def test_filter_by_scraper(self, empty_datastore) -> None:
        """Filtering by scraper returns only the scraper findings dataset."""
        store = get_data_store()

        # FO dataset
        df_fo = pd.DataFrame(
            {
                "asset": ["A"],
                "weight": [1.0],
                "expected_return": [0.05],
                "volatility": [0.10],
                "sharpe_ratio": [0.3],
            }
        )
        meta_fo = build_analysis_metadata(
            PRODUCER_FO_OPTIMIZER,
            RESULT_TYPE_TANGENCY,
            risk_free_rate=0.02,
            n_assets=1,
            asset_columns=["A"],
        )
        store.store("analysis_results.fo_optimizer.tangency", df_fo, metadata=meta_fo)

        # Scraper dataset
        df_scraper = pd.DataFrame(
            {
                "filename": ["report.pdf"],
                "fund_name": ["Fund X"],
                "period": ["Q1 2026"],
                "keyword": ["NAV"],
                "keyword_type": ["Number"],
                "value": ["100.0"],
                "source": ["Page 1"],
                "confidence": ["High"],
                "error": [""],
            }
        )
        meta_scraper = build_analysis_metadata(
            PRODUCER_SCRAPER,
            RESULT_TYPE_FINDINGS,
            cancelled=True,
            n_files=1,
            n_keywords=1,
            n_findings=1,
        )
        store.store("analysis_results.scraper.findings", df_scraper, metadata=meta_scraper)

        result = list_analysis_results(producer="scraper")
        assert "analysis_results.scraper.findings" in result
        assert "analysis_results.fo_optimizer.tangency" not in result
        assert "cancelled=True" in result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestToolsRegisteredOnImport:
    """Verify the four tools are present in the ToolRegistry after import."""

    def test_tools_registered_on_import(self) -> None:
        """All four DataStore tools are registered in the ToolRegistry singleton."""
        import services.tools.datastore_tools  # noqa: F401 — triggers registration

        from services.tool_registry import get_tool_registry

        reg = get_tool_registry()
        assert reg.has_tool("list_datasets")
        assert reg.has_tool("get_dataset_summary")
        assert reg.has_tool("get_dataset_slice")
        assert reg.has_tool("list_analysis_results")
