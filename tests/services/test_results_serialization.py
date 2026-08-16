# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :mod:`services.results_serialization`.

Covers the four public serialisation functions plus their edge cases:
empty inputs, name collisions, failed extractions, and reserved
metadata keys. The serializer is pure — no DataStore, no I/O, no PyQt6
— so all tests run as plain pytest with no special fixtures beyond
synthetic data builders.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.analytics.portfolio_optimizer import PortfolioResult
from services.results_serialization import (
    PRODUCER_FO_OPTIMIZER,
    PRODUCER_SCRAPER,
    RESULT_TYPE_FINDINGS,
    RESULT_TYPE_TANGENCY,
    build_analysis_metadata,
    frontier_to_dataframe,
    portfolio_result_to_dataframe,
    scraper_result_to_dataframe,
)
from services.scraper.models import (
    Confidence,
    Finding,
    Keyword,
    KeywordType,
    ReportExtraction,
    ScraperResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_portfolio_result(
    n_assets: int = 3,
    ret: float = 0.08,
    vol: float = 0.12,
    sharpe: float = 0.5,
    asset_names: list[str] | None = None,
    weights: np.ndarray | None = None,
) -> PortfolioResult:
    """Build a synthetic PortfolioResult with deterministic equal weights."""
    if asset_names is None:
        asset_names = [f"Asset {i + 1}" for i in range(n_assets)]
    n = len(asset_names)
    if weights is None:
        weights = np.full(n, 1.0 / n)
    return PortfolioResult(
        weights=weights,
        expected_return=ret,
        volatility=vol,
        sharpe_ratio=sharpe,
        asset_names=asset_names,
    )


def make_frontier(
    n_points: int = 5,
    n_assets: int = 3,
    asset_names: list[str] | None = None,
) -> list[PortfolioResult]:
    """Build a synthetic efficient frontier with consistent asset_names."""
    if asset_names is None:
        asset_names = [f"Asset {i + 1}" for i in range(n_assets)]
    n = len(asset_names)
    points: list[PortfolioResult] = []
    for k in range(n_points):
        ret = 0.05 + 0.01 * k
        vol = 0.10 + 0.005 * k
        sharpe = (ret - 0.02) / vol if vol > 0 else 0.0
        w = np.full(n, 1.0 / n)
        if n >= 2:
            tilt = 0.005 * k
            w = w.copy()
            w[0] = w[0] + tilt
            w[1] = w[1] - tilt
        points.append(
            PortfolioResult(
                weights=w,
                expected_return=ret,
                volatility=vol,
                sharpe_ratio=sharpe,
                asset_names=asset_names,
            )
        )
    return points


def make_finding(
    name: str = "NAV",
    kw_type: KeywordType = KeywordType.NUMBER,
    value: str = "100.0",
    source: str = "Page 1",
    conf: Confidence = Confidence.HIGH,
) -> Finding:
    """Build a synthetic Finding for tests."""
    return Finding(
        keyword=Keyword(name=name, type=kw_type),
        value=value,
        source=source,
        confidence=conf,
    )


def make_scraper_result(
    extractions: list[ReportExtraction] | None = None,
    cancelled: bool = False,
) -> ScraperResult:
    """Build a ScraperResult with the given extractions."""
    return ScraperResult(
        extractions=list(extractions) if extractions is not None else [],
        cancelled=cancelled,
    )


# ---------------------------------------------------------------------------
# portfolio_result_to_dataframe
# ---------------------------------------------------------------------------


class TestPortfolioResultToDataframe:
    """Tests for :func:`portfolio_result_to_dataframe`."""

    def test_happy_path_three_assets(self) -> None:
        """3 assets → 3 rows × 5 columns in the specified order with correct dtypes."""
        r = make_portfolio_result(n_assets=3, ret=0.08, vol=0.12, sharpe=0.5)
        df = portfolio_result_to_dataframe(r)

        assert df.shape == (3, 5)
        assert list(df.columns) == [
            "asset",
            "weight",
            "expected_return",
            "volatility",
            "sharpe_ratio",
        ]
        assert df["asset"].dtype == object
        assert df["weight"].dtype == np.float64
        assert df["expected_return"].dtype == np.float64
        assert df["volatility"].dtype == np.float64
        assert df["sharpe_ratio"].dtype == np.float64
        # Portfolio-level metrics repeated identically on every row
        assert (df["expected_return"] == 0.08).all()
        assert (df["volatility"] == 0.12).all()
        assert (df["sharpe_ratio"] == 0.5).all()
        assert isinstance(df.index, pd.RangeIndex)

    def test_two_asset_minimum(self) -> None:
        """Smallest portfolio the optimizer permits: 2 assets, equal-weighted."""
        r = make_portfolio_result(n_assets=2)
        df = portfolio_result_to_dataframe(r)
        assert df.shape == (2, 5)
        assert df["asset"].tolist() == ["Asset 1", "Asset 2"]
        np.testing.assert_array_almost_equal(df["weight"].values, np.array([0.5, 0.5]))

    def test_unicode_and_special_asset_names(self) -> None:
        """Asset names with spaces, em-dashes, and non-ASCII round-trip exactly."""
        names = [
            "Apollo Global Mgmt — Fund IX",
            "金融 Fund 中国",
            "Plain Asset",
        ]
        r = make_portfolio_result(asset_names=names)
        df = portfolio_result_to_dataframe(r)
        assert df["asset"].tolist() == names

    def test_weights_round_trip(self) -> None:
        """Provided weights appear unchanged (within float precision) in the column."""
        weights = np.array([0.25, 0.5, 0.25])
        r = make_portfolio_result(weights=weights)
        df = portfolio_result_to_dataframe(r)
        np.testing.assert_array_almost_equal(df["weight"].values, weights)

    def test_none_input_raises(self) -> None:
        """Passing None gives a clear, named ValueError."""
        with pytest.raises(ValueError, match="must not be None"):
            portfolio_result_to_dataframe(None)  # type: ignore[arg-type]

    def test_mismatched_lengths_raises_with_both_lengths_in_message(self) -> None:
        """Frozen dataclass does not validate, so we must catch length mismatches."""
        bad = PortfolioResult(
            weights=np.array([0.5, 0.5]),
            expected_return=0.05,
            volatility=0.1,
            sharpe_ratio=0.3,
            asset_names=["A"],
        )
        with pytest.raises(ValueError) as exc_info:
            portfolio_result_to_dataframe(bad)
        msg = str(exc_info.value)
        assert "2" in msg and "1" in msg


# ---------------------------------------------------------------------------
# frontier_to_dataframe
# ---------------------------------------------------------------------------


class TestFrontierToDataframe:
    """Tests for :func:`frontier_to_dataframe`."""

    def test_happy_path_five_points_three_assets(self) -> None:
        """5 frontier points × 3 assets → 5 rows × 7 columns in the specified order."""
        frontier = make_frontier(n_points=5, n_assets=3)
        df = frontier_to_dataframe(frontier)

        assert df.shape == (5, 7)
        assert list(df.columns) == [
            "point_index",
            "expected_return",
            "volatility",
            "sharpe_ratio",
            "Asset 1",
            "Asset 2",
            "Asset 3",
        ]
        assert df["point_index"].dtype == np.int64
        for col in (
            "expected_return",
            "volatility",
            "sharpe_ratio",
            "Asset 1",
            "Asset 2",
            "Asset 3",
        ):
            assert df[col].dtype == np.float64
        assert isinstance(df.index, pd.RangeIndex)

    def test_empty_frontier_returns_only_metric_columns(self) -> None:
        """Empty frontier → empty DataFrame with the four metric columns only."""
        df = frontier_to_dataframe([])
        assert df.shape == (0, 4)
        assert list(df.columns) == [
            "point_index",
            "expected_return",
            "volatility",
            "sharpe_ratio",
        ]
        # Dtypes are still correct on the empty frame
        assert df["point_index"].dtype == np.int64
        assert df["expected_return"].dtype == np.float64
        assert df["volatility"].dtype == np.float64
        assert df["sharpe_ratio"].dtype == np.float64

    def test_single_point_frontier(self) -> None:
        """A 1-point frontier produces a 1-row DataFrame with all columns."""
        frontier = make_frontier(n_points=1, n_assets=2)
        df = frontier_to_dataframe(frontier)
        assert df.shape == (1, 6)  # 4 metrics + 2 assets
        assert df["point_index"].tolist() == [0]

    def test_point_index_zero_based_sequential_matches_input_order(self) -> None:
        """Serializer preserves the input order exactly — does not re-sort."""
        names = ["A", "B"]
        # Pass the points in DECREASING expected_return order so any
        # accidental re-sorting would be detectable.
        points: list[PortfolioResult] = []
        for i in range(4):
            points.append(
                PortfolioResult(
                    weights=np.array([0.5, 0.5]),
                    expected_return=float(10 - i),
                    volatility=0.10 + 0.01 * i,
                    sharpe_ratio=0.5,
                    asset_names=names,
                )
            )
        df = frontier_to_dataframe(points)
        assert df["point_index"].tolist() == [0, 1, 2, 3]
        assert df["expected_return"].tolist() == [10.0, 9.0, 8.0, 7.0]

    def test_inconsistent_asset_names_raises(self) -> None:
        """Differing asset_names across points: ValueError naming both lists."""
        p1 = make_portfolio_result(asset_names=["A", "B", "C"])
        p2 = make_portfolio_result(asset_names=["A", "B", "X"])
        with pytest.raises(ValueError) as exc_info:
            frontier_to_dataframe([p1, p2])
        msg = str(exc_info.value)
        # Both differing names appear in the error message
        assert "C" in msg and "X" in msg

    def test_asset_collides_with_metric_column_volatility(self) -> None:
        """Asset literally named 'volatility' is rejected with a clear error."""
        p = make_portfolio_result(asset_names=["Asset 1", "volatility", "Asset 3"])
        with pytest.raises(ValueError, match="collide"):
            frontier_to_dataframe([p])

    def test_asset_collides_with_metric_column_point_index(self) -> None:
        """Asset literally named 'point_index' is rejected."""
        p = make_portfolio_result(asset_names=["point_index", "Asset 2"])
        with pytest.raises(ValueError, match="point_index"):
            frontier_to_dataframe([p])

    def test_point_weights_length_mismatch_raises(self) -> None:
        """Internally inconsistent PortfolioResult is caught, not silently truncated."""
        bad_point = PortfolioResult(
            weights=np.array([0.4, 0.3, 0.3]),  # length 3
            expected_return=0.06,
            volatility=0.10,
            sharpe_ratio=0.4,
            asset_names=["A", "B"],  # length 2
        )
        with pytest.raises(ValueError) as exc_info:
            frontier_to_dataframe([bad_point])
        assert "weights" in str(exc_info.value)

    def test_asset_weights_round_trip_in_columns(self) -> None:
        """Each asset's weights across frontier points appear in its own column."""
        names = ["X", "Y"]
        p0 = PortfolioResult(
            weights=np.array([0.7, 0.3]),
            expected_return=0.06,
            volatility=0.10,
            sharpe_ratio=0.4,
            asset_names=names,
        )
        p1 = PortfolioResult(
            weights=np.array([0.4, 0.6]),
            expected_return=0.08,
            volatility=0.12,
            sharpe_ratio=0.5,
            asset_names=names,
        )
        df = frontier_to_dataframe([p0, p1])
        np.testing.assert_array_almost_equal(df["X"].values, [0.7, 0.4])
        np.testing.assert_array_almost_equal(df["Y"].values, [0.3, 0.6])


# ---------------------------------------------------------------------------
# scraper_result_to_dataframe
# ---------------------------------------------------------------------------


class TestScraperResultToDataframe:
    """Tests for :func:`scraper_result_to_dataframe`."""

    def test_happy_path_two_extractions_three_findings_each(self) -> None:
        """2 extractions × 3 findings → 6 rows × 9 columns in specified order."""
        ext1 = ReportExtraction(
            filename="r1.pdf",
            fund_name="Fund A",
            period="Q1 2026",
            findings=[
                make_finding("NAV", KeywordType.NUMBER, "100.0", "Page 5", Confidence.HIGH),
                make_finding(
                    "Capital Called", KeywordType.NUMBER, "50.0", "Page 6", Confidence.MEDIUM
                ),
                make_finding("Strategy", KeywordType.TEXT, "Buyout", "Page 1", Confidence.HIGH),
            ],
        )
        ext2 = ReportExtraction(
            filename="r2.pdf",
            fund_name="Fund B",
            period="Q1 2026",
            findings=[
                make_finding("NAV", KeywordType.NUMBER, "200.0", "Page 3", Confidence.HIGH),
                make_finding(
                    "Capital Called", KeywordType.NUMBER, "75.0", "Page 4", Confidence.LOW
                ),
                make_finding("Strategy", KeywordType.TEXT, "Growth", "Page 1", Confidence.HIGH),
            ],
        )
        df = scraper_result_to_dataframe(make_scraper_result([ext1, ext2]))

        assert df.shape == (6, 9)
        assert list(df.columns) == [
            "filename",
            "fund_name",
            "period",
            "keyword",
            "keyword_type",
            "value",
            "source",
            "confidence",
            "error",
        ]
        # All columns are object dtype
        for col in df.columns:
            assert df[col].dtype == object
        # Enum .value strings, not Enum objects
        assert df.iloc[0]["keyword_type"] == "Number"
        assert df.iloc[0]["confidence"] == "High"
        # Field round-trip
        assert df.iloc[0]["filename"] == "r1.pdf"
        assert df.iloc[0]["keyword"] == "NAV"
        assert df.iloc[0]["value"] == "100.0"
        assert df.iloc[0]["fund_name"] == "Fund A"
        assert df.iloc[0]["period"] == "Q1 2026"
        # Rows 0-2 from r1.pdf, rows 3-5 from r2.pdf
        assert df.iloc[2]["filename"] == "r1.pdf"
        assert df.iloc[3]["filename"] == "r2.pdf"

    def test_empty_result_returns_zero_row_dataframe_with_all_columns(self) -> None:
        """Empty result → empty DataFrame with all 9 columns."""
        df = scraper_result_to_dataframe(make_scraper_result([]))
        assert df.shape == (0, 9)
        assert list(df.columns) == [
            "filename",
            "fund_name",
            "period",
            "keyword",
            "keyword_type",
            "value",
            "source",
            "confidence",
            "error",
        ]
        for col in df.columns:
            assert df[col].dtype == object

    def test_failed_extraction_emits_one_row_with_error_populated(self) -> None:
        """Error + empty findings → exactly one visible row, error populated."""
        ext = ReportExtraction(
            filename="bad.pdf",
            fund_name="",
            period="",
            findings=[],
            error="PDF could not be parsed",
        )
        df = scraper_result_to_dataframe(make_scraper_result([ext]))
        assert df.shape == (1, 9)
        row = df.iloc[0]
        assert row["filename"] == "bad.pdf"
        assert row["error"] == "PDF could not be parsed"
        # Keyword-related columns are empty strings (not NaN)
        for col in ("keyword", "keyword_type", "value", "source", "confidence"):
            assert row[col] == ""
            assert not pd.isna(row[col])

    def test_no_findings_no_error_emits_zero_rows(self) -> None:
        """No findings + no error → extraction contributes nothing."""
        ext = ReportExtraction(
            filename="empty.pdf",
            fund_name="",
            period="",
            findings=[],
            error=None,
        )
        df = scraper_result_to_dataframe(make_scraper_result([ext]))
        assert df.shape == (0, 9)

    def test_mixed_extractions_findings_error_empty(self) -> None:
        """Combination of all three categories produces correct row counts."""
        ext_with_findings = ReportExtraction(
            filename="ok.pdf",
            fund_name="OK Fund",
            period="Q1 2026",
            findings=[
                make_finding("NAV", KeywordType.NUMBER, "100", "Page 1", Confidence.HIGH),
                make_finding("Capital Called", KeywordType.NUMBER, "50", "Page 2", Confidence.HIGH),
            ],
        )
        ext_with_error = ReportExtraction(
            filename="bad.pdf",
            fund_name="",
            period="",
            findings=[],
            error="Parse error",
        )
        ext_empty = ReportExtraction(
            filename="empty.pdf",
            fund_name="",
            period="",
            findings=[],
            error=None,
        )
        df = scraper_result_to_dataframe(
            make_scraper_result([ext_with_findings, ext_with_error, ext_empty])
        )
        # 2 finding rows + 1 error row + 0 empty rows = 3
        assert df.shape == (3, 9)
        assert df.iloc[0]["filename"] == "ok.pdf"
        assert df.iloc[1]["filename"] == "ok.pdf"
        assert df.iloc[2]["filename"] == "bad.pdf"
        assert df.iloc[2]["error"] == "Parse error"
        # The error row's keyword fields are empty strings
        assert df.iloc[2]["keyword"] == ""

    def test_empty_fund_name_and_period_round_trip_as_empty_strings(self) -> None:
        """Empty fund_name/period stay as '' (not NaN) so column type is uniform."""
        ext = ReportExtraction(
            filename="r.pdf",
            fund_name="",
            period="",
            findings=[
                make_finding("NAV", KeywordType.NUMBER, "1.0", "p", Confidence.HIGH),
            ],
        )
        df = scraper_result_to_dataframe(make_scraper_result([ext]))
        assert df.iloc[0]["fund_name"] == ""
        assert df.iloc[0]["period"] == ""
        assert not pd.isna(df.iloc[0]["fund_name"])
        assert not pd.isna(df.iloc[0]["period"])

    def test_cancelled_field_is_not_a_dataframe_column(self) -> None:
        """ScraperResult.cancelled belongs in metadata, not in the DataFrame."""
        ext = ReportExtraction(
            filename="r.pdf",
            findings=[
                make_finding("NAV", KeywordType.NUMBER, "1", "p", Confidence.HIGH),
            ],
        )
        df = scraper_result_to_dataframe(make_scraper_result([ext], cancelled=True))
        assert "cancelled" not in df.columns


# ---------------------------------------------------------------------------
# build_analysis_metadata
# ---------------------------------------------------------------------------


class TestBuildAnalysisMetadata:
    """Tests for :func:`build_analysis_metadata`."""

    def test_minimal_call_returns_three_standard_keys(self) -> None:
        """Bare call returns exactly producer, result_type, computed_at."""
        meta = build_analysis_metadata("p", "t")
        assert set(meta.keys()) == {"producer", "result_type", "computed_at"}
        assert meta["producer"] == "p"
        assert meta["result_type"] == "t"

    def test_computed_at_is_iso8601_utc_and_parseable(self) -> None:
        """computed_at ends with '+00:00' and round-trips through pd.Timestamp."""
        meta = build_analysis_metadata(PRODUCER_FO_OPTIMIZER, RESULT_TYPE_TANGENCY)
        ts_str = meta["computed_at"]
        assert ts_str.endswith("+00:00")
        parsed = pd.Timestamp(ts_str)
        assert parsed.tzinfo is not None
        # Second precision: no fractional component
        assert "." not in ts_str.split("+")[0]

    def test_extra_kwargs_are_merged_into_result(self) -> None:
        """Arbitrary extras pass through unchanged alongside the standard keys."""
        meta = build_analysis_metadata("p", "t", foo=1, bar="x", baz=[1, 2, 3])
        assert meta["foo"] == 1
        assert meta["bar"] == "x"
        assert meta["baz"] == [1, 2, 3]
        assert meta["producer"] == "p"
        assert meta["result_type"] == "t"
        assert "computed_at" in meta

    def test_empty_producer_raises(self) -> None:
        """Empty string for producer is rejected."""
        with pytest.raises(ValueError, match="producer"):
            build_analysis_metadata("", "t")

    def test_empty_result_type_raises(self) -> None:
        """Empty string for result_type is rejected."""
        with pytest.raises(ValueError, match="result_type"):
            build_analysis_metadata("p", "")

    def test_reserved_key_collision_producer_raises(self) -> None:
        """Caller cannot override producer via **extra."""
        with pytest.raises(ValueError, match="producer"):
            build_analysis_metadata("p", "t", producer="other")

    def test_reserved_key_collision_result_type_raises(self) -> None:
        """Caller cannot override result_type via **extra."""
        with pytest.raises(ValueError, match="result_type"):
            build_analysis_metadata("p", "t", result_type="other")

    def test_reserved_key_collision_computed_at_raises(self) -> None:
        """Caller cannot override computed_at via **extra."""
        with pytest.raises(ValueError, match="computed_at"):
            build_analysis_metadata("p", "t", computed_at="2026-01-01T00:00:00+00:00")

    def test_realistic_optimizer_extras(self) -> None:
        """The metadata Phase 2 will pass for an FO optimizer result."""
        meta = build_analysis_metadata(
            PRODUCER_FO_OPTIMIZER,
            RESULT_TYPE_TANGENCY,
            risk_free_rate=0.025,
            n_assets=7,
            asset_columns=["Fund A", "Fund B", "Fund C"],
        )
        assert meta["producer"] == "fo_optimizer"
        assert meta["result_type"] == "tangency"
        assert meta["risk_free_rate"] == 0.025
        assert meta["n_assets"] == 7
        assert meta["asset_columns"] == ["Fund A", "Fund B", "Fund C"]

    def test_realistic_scraper_extras(self) -> None:
        """The metadata Phase 2 will pass for a scraper result."""
        meta = build_analysis_metadata(
            PRODUCER_SCRAPER,
            RESULT_TYPE_FINDINGS,
            cancelled=False,
            n_files=3,
            n_keywords=8,
        )
        assert meta["producer"] == "scraper"
        assert meta["result_type"] == "findings"
        assert meta["cancelled"] is False
        assert meta["n_files"] == 3
        assert meta["n_keywords"] == 8

    def test_consecutive_calls_have_consistent_format(self) -> None:
        """Two calls produce well-formed timestamps; freezegun is not available."""
        meta1 = build_analysis_metadata("p", "t")
        meta2 = build_analysis_metadata("p", "t")
        for m in (meta1, meta2):
            assert m["computed_at"].endswith("+00:00")
            pd.Timestamp(m["computed_at"])  # raises if unparseable
        # Format string length is identical (same precision and tz offset)
        assert len(meta1["computed_at"]) == len(meta2["computed_at"])
