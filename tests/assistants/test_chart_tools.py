# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :mod:`services.tools.chart_tools`.

Tests call the generate_chart function directly (not through ToolRegistry)
to avoid QApplication dependency. DataStore fixtures handle setup/teardown.
"""

from __future__ import annotations

import base64
import json

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from core.data_store import get_data_store
from services.tools.chart_tools import generate_chart


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_datastore():
    """Populate DataStore with test data and clean up after.

    Yields:
        The populated DataStore instance.
    """
    store = get_data_store()
    store.clear()
    dates = pd.date_range("2020-01-01", periods=100, freq="D")
    rng = np.random.default_rng(42)
    df = pd.DataFrame(
        rng.standard_normal((100, 3)),
        index=dates,
        columns=["Fund A", "Fund B", "Fund C"],
    )
    store.store("test_data", df, metadata={"source": "test"})
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
# Helpers
# ---------------------------------------------------------------------------


def _parse_artefact(result: str) -> dict:
    """Parse and validate a chart artefact JSON string.

    Args:
        result: The string returned by :func:`generate_chart`.

    Returns:
        The parsed artefact dict.

    Raises:
        AssertionError: If the result is not a valid artefact.
    """
    parsed = json.loads(result)
    assert isinstance(parsed, dict), f"Expected dict, got {type(parsed)}"
    assert parsed.get("__artifact__") == "chart", f"No __artifact__ key: {parsed.keys()}"
    assert "image_base64" in parsed, "Missing image_base64"
    assert "llm_response" in parsed, "Missing llm_response"
    assert isinstance(parsed["image_base64"], str) and len(parsed["image_base64"]) > 0
    return parsed


# ---------------------------------------------------------------------------
# Tests: DataStore-based charts
# ---------------------------------------------------------------------------


class TestGenerateChartFromDatastore:
    """Tests for charts sourced from the DataStore."""

    def test_generate_chart_line_from_datastore(self, loaded_datastore) -> None:
        """Line chart from DataStore returns valid artefact JSON."""
        result = generate_chart(
            chart_type="line",
            title="Test Line Chart",
            data_source="datastore",
            datastore_key="test_data",
        )
        artefact = _parse_artefact(result)
        assert "generated" in artefact["llm_response"].lower()

    def test_generate_chart_date_range_filter(self, loaded_datastore) -> None:
        """Date range filter produces a valid chart artefact."""
        result = generate_chart(
            chart_type="line",
            title="Filtered Chart",
            data_source="datastore",
            datastore_key="test_data",
            date_range={"start": "2020-01-15", "end": "2020-02-15"},
        )
        _parse_artefact(result)

    def test_generate_chart_column_filter(self, loaded_datastore) -> None:
        """Requesting a subset of columns produces a valid chart artefact."""
        result = generate_chart(
            chart_type="line",
            title="Two Funds",
            data_source="datastore",
            datastore_key="test_data",
            columns=["Fund A", "Fund B"],
        )
        _parse_artefact(result)


# ---------------------------------------------------------------------------
# Tests: inline charts
# ---------------------------------------------------------------------------


class TestGenerateChartInline:
    """Tests for inline-data charts."""

    def test_generate_chart_bar_inline(self, empty_datastore) -> None:
        """Bar chart with inline data returns valid artefact JSON."""
        result = generate_chart(
            chart_type="bar",
            title="Bar Chart",
            data_source="inline",
            inline_data={"x": ["A", "B", "C"], "y": [10, 20, 30]},
        )
        _parse_artefact(result)

    def test_generate_chart_pie_inline(self, empty_datastore) -> None:
        """Pie chart with inline data returns valid artefact JSON."""
        result = generate_chart(
            chart_type="pie",
            title="Allocation",
            data_source="inline",
            inline_data={"labels": ["PE", "RE", "Infra"], "values": [40, 35, 25]},
        )
        _parse_artefact(result)

    def test_generate_chart_donut_inline(self, empty_datastore) -> None:
        """Donut chart with inline data returns valid artefact JSON."""
        result = generate_chart(
            chart_type="donut",
            title="Donut",
            data_source="inline",
            inline_data={"labels": ["PE", "RE", "Infra"], "values": [40, 35, 25]},
        )
        _parse_artefact(result)

    def test_generate_chart_scatter_inline(self, empty_datastore) -> None:
        """Scatter chart with inline data returns valid artefact JSON."""
        result = generate_chart(
            chart_type="scatter",
            title="Scatter",
            data_source="inline",
            inline_data={"x": [1, 2, 3, 4, 5], "y": [2, 4, 1, 3, 5]},
        )
        _parse_artefact(result)

    def test_generate_chart_grouped_bar_inline(self, empty_datastore) -> None:
        """Grouped bar chart with multi-series inline data returns valid artefact."""
        result = generate_chart(
            chart_type="grouped_bar",
            title="Grouped Bars",
            data_source="inline",
            inline_data={"x": ["Q1", "Q2"], "series": {"Fund A": [10, 20], "Fund B": [15, 25]}},
        )
        _parse_artefact(result)


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------


class TestGenerateChartErrors:
    """Tests for validation and error handling in generate_chart."""

    def test_generate_chart_invalid_type(self, empty_datastore) -> None:
        """Unsupported chart_type returns a plain error string."""
        result = generate_chart(
            chart_type="histogram",
            title="Bad Type",
            data_source="inline",
            inline_data={"x": [1, 2], "y": [3, 4]},
        )
        # Must NOT be a JSON artefact
        try:
            parsed = json.loads(result)
            assert parsed.get("__artifact__") != "chart", "Should not be a chart artefact"
        except json.JSONDecodeError:
            pass  # plain string error — acceptable
        assert "histogram" in result.lower() or "invalid" in result.lower()

    def test_generate_chart_missing_datastore_key(self, empty_datastore) -> None:
        """data_source='datastore' without datastore_key returns an error string."""
        result = generate_chart(
            chart_type="line",
            title="Missing Key",
            data_source="datastore",
        )
        assert "datastore_key" in result.lower() or "not provided" in result.lower()

    def test_generate_chart_datastore_key_not_found(self, empty_datastore) -> None:
        """Non-existent datastore_key returns an error mentioning 'not found'."""
        result = generate_chart(
            chart_type="line",
            title="Missing Dataset",
            data_source="datastore",
            datastore_key="nonexistent",
        )
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# Tests: image validity
# ---------------------------------------------------------------------------


class TestChartImageValidity:
    """Tests verifying the PNG image payload."""

    def test_base64_is_valid_png(self, loaded_datastore) -> None:
        """The Base64 string decodes to valid PNG data (correct magic bytes)."""
        result = generate_chart(
            chart_type="line",
            title="PNG Check",
            data_source="datastore",
            datastore_key="test_data",
        )
        artefact = _parse_artefact(result)
        raw = base64.b64decode(artefact["image_base64"])
        png_magic = b"\x89PNG\r\n\x1a\n"
        assert raw[:8] == png_magic, f"Not a PNG: first 8 bytes are {raw[:8]!r}"


# ---------------------------------------------------------------------------
# Tests: tool registration
# ---------------------------------------------------------------------------


class TestChartToolRegistration:
    """Verify the generate_chart tool is registered in the ToolRegistry."""

    def test_generate_chart_registered_on_import(self) -> None:
        """generate_chart tool is present in the ToolRegistry after import."""
        import services.tools.chart_tools  # noqa: F401 — triggers registration

        from services.tool_registry import get_tool_registry

        reg = get_tool_registry()
        assert reg.has_tool("generate_chart")
