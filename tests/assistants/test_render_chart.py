# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the ``render_chart`` generic Plotly chart tool.

``render_chart`` (ADR-0048, Axis 2) is a *pure transform*: it resolves
the structured-data envelope produced by ``get_investment_data`` —
looked up server-side by a *data handle*, not received as an argument
(ADR-0048, amended) — and emits a themed Plotly figure spec. No
database, no DataStore, no matplotlib — so these tests build the
envelope by hand, stash it in the turn-scoped data cache via
``store_tool_data``, and call ``render_chart`` with the handle.

Coverage:

* One test per chart-type primitive — the artefact envelope shape and
  the trace count.
* ``series_column`` — distinct-value partitioning into one trace each.
* Date-axis detection — driven by the data, not the chart type.
* Validation failures — every one returns a clear string, never
  raises — including an unknown / stale data handle.
* The handle resolves more than once within a turn.
* Theme reuse — the layout's background colours come from the
  canonical chart theme.
"""

from __future__ import annotations

import base64
import json

import pytest

from services.chart_specs.base import get_chart_theme
from services.tools._tool_context import clear_tool_data, store_tool_data
from services.tools.chart_tools import render_chart

# ---------------------------------------------------------------------------
# Hand-built structured-data envelopes (the Axis-1 contract shape)
# ---------------------------------------------------------------------------

# Two numeric series against a categorical x — exercises the default
# "every numeric column is a series" path.
_NUMERIC_ENVELOPE: dict = {
    "__data__": "investment_data",
    "bundle": "catalogue",
    "investment_name": None,
    "columns": ["label", "value_a", "value_b"],
    "rows": [
        ["Q1", 10.0, 15.0],
        ["Q2", 20.0, 25.0],
        ["Q3", 30.0, 35.0],
    ],
    "meta": {"row_count": 3},
}

# A NAV series with a ``nav_kind`` discriminator — exercises both the
# date-axis detection and the ``series_column`` overlay path.
_SERIES_ENVELOPE: dict = {
    "__data__": "investment_data",
    "bundle": "nav_series",
    "investment_name": "Alpha Fund",
    "columns": ["as_of_date", "nav_value", "nav_kind"],
    "rows": [
        ["2021-12-31", 100.0, "actual"],
        ["2022-12-31", 150.0, "actual"],
        ["2023-12-31", 200.0, "plan"],
    ],
    "meta": {"currency": "EUR", "row_count": 3},
}

# The SAA-hypothetical envelope (ADR-0069) — a second producer for the
# generic ``render_chart`` path, with its own discriminator. Long-form:
# one cumulative-index block per series, charted via
# ``series_column="series_name"``.
_SAA_ENVELOPE: dict = {
    "__data__": "saa_hypothetical",
    "columns": ["as_of_date", "cumulative_index", "series_name"],
    "rows": [
        ["2022-01-31", 1.01, "SAA × Benchmark"],
        ["2022-02-28", 1.03, "SAA × Benchmark"],
        ["2022-01-31", 1.02, "Actual"],
        ["2022-02-28", 1.05, "Actual"],
    ],
    "meta": {"unit": "index", "base": 1.0, "series": ["SAA × Benchmark", "Actual"]},
}


@pytest.fixture(autouse=True)
def _clean_data_cache() -> object:
    """Each test starts and ends with an empty turn-scoped data cache."""
    clear_tool_data()
    yield
    clear_tool_data()


def _handle(envelope: dict) -> str:
    """Stash an envelope in the turn-scoped data cache, return its handle."""
    return store_tool_data(envelope)


def _spec(result: str) -> dict:
    """Parse a ``render_chart`` result and assert the artefact envelope shape.

    Args:
        result: The string returned by :func:`render_chart`.

    Returns:
        The Plotly ``spec`` dict.
    """
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    assert parsed["__artifact__"] == "chart"
    assert parsed["chart_format"] == "plotly"
    assert "displayed in the chat" in parsed["llm_response"]
    spec = parsed["spec"]
    assert set(spec) >= {"data", "layout", "config"}
    return spec


# ---------------------------------------------------------------------------
# One test per chart-type primitive
# ---------------------------------------------------------------------------


def test_line_chart_one_trace_per_numeric_column() -> None:
    """A line chart over two numeric columns produces two traces."""
    spec = _spec(render_chart("line", "Two series", _handle(_NUMERIC_ENVELOPE)))
    assert len(spec["data"]) == 2
    assert all(t["type"] == "scatter" for t in spec["data"])
    assert all(t["mode"] == "lines" for t in spec["data"])


def test_line_chart_traces_have_no_marker_block() -> None:
    """Line traces stay marker-free to match the Front Office charts.

    Dense time series with hundreds of points turn into a thick stripe
    when every point gets a marker; the Front Office specs already
    render lines only, and Shirley's ``render_chart`` follows suit.
    """
    spec = _spec(render_chart("line", "No markers", _handle(_NUMERIC_ENVELOPE)))
    assert all("marker" not in trace for trace in spec["data"])


def test_bar_chart_one_trace_per_numeric_column() -> None:
    """A bar chart over two numeric columns produces two bar traces."""
    spec = _spec(render_chart("bar", "Bars", _handle(_NUMERIC_ENVELOPE)))
    assert len(spec["data"]) == 2
    assert all(t["type"] == "bar" for t in spec["data"])


def test_grouped_bar_sets_barmode_group() -> None:
    """A grouped bar chart produces bar traces and ``barmode=group``."""
    spec = _spec(render_chart("grouped_bar", "Grouped", _handle(_NUMERIC_ENVELOPE)))
    assert len(spec["data"]) == 2
    assert all(t["type"] == "bar" for t in spec["data"])
    assert spec["layout"]["barmode"] == "group"


def test_scatter_chart_markers_mode() -> None:
    """A scatter chart produces marker-mode scatter traces."""
    spec = _spec(render_chart("scatter", "Scatter", _handle(_NUMERIC_ENVELOPE)))
    assert len(spec["data"]) == 2
    assert all(t["type"] == "scatter" and t["mode"] == "markers" for t in spec["data"])


def test_pie_chart_single_trace() -> None:
    """A pie chart collapses to a single pie trace with labels and values."""
    spec = _spec(render_chart("pie", "Pie", _handle(_NUMERIC_ENVELOPE), y_columns=["value_a"]))
    assert len(spec["data"]) == 1
    trace = spec["data"][0]
    assert trace["type"] == "pie"
    assert trace["labels"] == ["Q1", "Q2", "Q3"]
    assert trace["values"] == [10.0, 20.0, 30.0]
    assert "hole" not in trace


def test_donut_chart_single_trace_with_hole() -> None:
    """A donut chart is a pie trace with a non-zero ``hole``."""
    spec = _spec(render_chart("donut", "Donut", _handle(_NUMERIC_ENVELOPE), y_columns=["value_a"]))
    assert len(spec["data"]) == 1
    trace = spec["data"][0]
    assert trace["type"] == "pie"
    assert trace["hole"] == 0.5


# ---------------------------------------------------------------------------
# series_column — the generic plan-vs-actual overlay
# ---------------------------------------------------------------------------


def test_series_column_produces_one_trace_per_distinct_value() -> None:
    """``series_column`` partitions the rows into one trace per kind."""
    spec = _spec(
        render_chart(
            "line",
            "NAV plan vs actual",
            _handle(_SERIES_ENVELOPE),
            series_column="nav_kind",
        )
    )
    assert len(spec["data"]) == 2
    names = {t["name"] for t in spec["data"]}
    assert names == {"actual", "plan"}
    # The 'actual' trace carries the two actual rows; 'plan' the one plan row.
    by_name = {t["name"]: t for t in spec["data"]}
    assert by_name["actual"]["y"] == [100.0, 150.0]
    assert by_name["plan"]["y"] == [200.0]


# ---------------------------------------------------------------------------
# Date-axis detection — from the data, not the chart type
# ---------------------------------------------------------------------------


def test_iso_date_x_column_produces_date_axis() -> None:
    """An x column of ISO date strings yields ``layout.xaxis.type == 'date'``."""
    spec = _spec(render_chart("line", "NAV", _handle(_SERIES_ENVELOPE)))
    assert spec["layout"]["xaxis"]["type"] == "date"
    assert spec["layout"]["xaxis"]["tickformat"] == "%Y-%m-%d"


def test_categorical_x_column_has_no_date_axis() -> None:
    """A categorical x column does not get a date axis."""
    spec = _spec(render_chart("line", "Quarters", _handle(_NUMERIC_ENVELOPE)))
    assert spec["layout"]["xaxis"].get("type") != "date"


# ---------------------------------------------------------------------------
# The handle resolves more than once within a turn
# ---------------------------------------------------------------------------


def test_same_handle_renders_more_than_once() -> None:
    """A handle can be rendered repeatedly within a turn — no consume-on-read."""
    handle = _handle(_NUMERIC_ENVELOPE)
    first = _spec(render_chart("line", "First", handle))
    second = _spec(render_chart("bar", "Second", handle))
    assert len(first["data"]) == 2
    assert len(second["data"]) == 2


# ---------------------------------------------------------------------------
# Validation failures — clear strings, never raises
# ---------------------------------------------------------------------------


def test_invalid_chart_type_returns_clear_string() -> None:
    """An unsupported chart type returns a plain string, not an artefact."""
    result = render_chart("histogram", "Bad", _handle(_NUMERIC_ENVELOPE))
    assert "Invalid chart_type" in result
    assert "__artifact__" not in result


def test_unknown_data_handle_returns_clear_string() -> None:
    """A handle not in the cache returns a clear string, does not raise."""
    result = render_chart("line", "Stale", "deadbeefdead")
    assert "No data found for handle" in result
    assert "get_investment_data" in result
    assert "__artifact__" not in result


def test_handle_pointing_at_non_envelope_returns_clear_string() -> None:
    """A handle resolving to a non-envelope dict is rejected clearly."""
    not_an_envelope = {
        "columns": ["x", "y"],
        "rows": [[1, 2]],
    }
    result = render_chart("line", "No discriminator", _handle(not_an_envelope))
    assert "not a structured-data envelope" in result
    assert "__artifact__" not in result


def test_unknown_discriminator_returns_clear_string() -> None:
    """A handle with an unrecognised ``__data__`` discriminator is rejected."""
    foreign = {
        "__data__": "some_other_producer",
        "columns": ["as_of_date", "value"],
        "rows": [["2022-01-31", 1.0]],
        "meta": {},
    }
    result = render_chart("line", "Foreign", _handle(foreign))
    assert "not a structured-data envelope" in result
    assert "__artifact__" not in result


# ---------------------------------------------------------------------------
# Second producer — the SAA-hypothetical envelope (ADR-0069)
# ---------------------------------------------------------------------------


def test_saa_hypothetical_handle_renders_three_line_chart() -> None:
    """``render_chart`` accepts the ``saa_hypothetical`` discriminator.

    The SAA-hypothetical envelope shares the tidy columns/rows/meta
    shape; charted with ``series_column="series_name"`` it yields one
    line trace per series against a date x-axis — the existing render
    path unchanged.
    """
    spec = _spec(
        render_chart(
            "line",
            "SAA hypothetical",
            _handle(_SAA_ENVELOPE),
            series_column="series_name",
        )
    )
    names = {t["name"] for t in spec["data"]}
    assert names == {"SAA × Benchmark", "Actual"}
    assert all(t["type"] == "scatter" and t["mode"] == "lines" for t in spec["data"])
    assert spec["layout"]["xaxis"]["type"] == "date"


def test_unknown_x_column_returns_clear_string() -> None:
    """A named x column that is not in the data returns a clear string."""
    result = render_chart("line", "Bad x", _handle(_NUMERIC_ENVELOPE), x_column="nonexistent")
    assert "x_column 'nonexistent' is not in" in result
    assert "__artifact__" not in result


def test_unknown_y_column_returns_clear_string() -> None:
    """A named y column that is not in the data returns a clear string."""
    result = render_chart("line", "Bad y", _handle(_NUMERIC_ENVELOPE), y_columns=["nonexistent"])
    assert "nonexistent" in result
    assert "not in the data columns" in result
    assert "__artifact__" not in result


def test_empty_rows_returns_clear_string() -> None:
    """An envelope with no rows returns a clear string, not an artefact."""
    empty = {**_NUMERIC_ENVELOPE, "rows": []}
    result = render_chart("line", "Empty", _handle(empty))
    assert "no rows" in result
    assert "__artifact__" not in result


# ---------------------------------------------------------------------------
# Theme reuse
# ---------------------------------------------------------------------------


def test_layout_background_colours_match_canonical_theme() -> None:
    """The layout's background colours come from the canonical chart theme."""
    spec = _spec(render_chart("line", "Themed", _handle(_NUMERIC_ENVELOPE)))
    theme = get_chart_theme()
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]
    assert spec["layout"]["plot_bgcolor"] == theme["colours"]["plot_area"]


# ---------------------------------------------------------------------------
# Raster PNG sibling (ADR-0048) — for surfaces that cannot render Plotly
# ---------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG"


def test_render_chart_emits_plotly_spec_and_png_for_line_envelope() -> None:
    """A line render carries BOTH a well-formed spec and a decodable PNG.

    The web surface keeps using the interactive Plotly ``spec``; Telegram
    uses the ``image_base64`` PNG. Both must be present on every artefact.
    """
    result = render_chart("line", "Simple line", _handle(_NUMERIC_ENVELOPE))
    parsed = json.loads(result)

    # (a) The Plotly spec is present and well-formed.
    assert parsed["chart_format"] == "plotly"
    spec = parsed["spec"]
    assert set(spec) >= {"data", "layout", "config"}

    # (b) The PNG is a non-empty base64 string decoding to real PNG bytes.
    image_base64 = parsed["image_base64"]
    assert isinstance(image_base64, str) and image_base64
    assert base64.b64decode(image_base64).startswith(_PNG_MAGIC)


def test_render_chart_png_handles_series_with_different_x_coverage() -> None:
    """A ``series_column`` overlay whose series span different x renders a PNG.

    ``_SERIES_ENVELOPE`` has an 'actual' series over 2021–2022 and a 'plan'
    series at 2023 — distinct date coverage. The raster helper plots one
    trace per series using that subset's own x, so this proves the
    per-series-x handling renders without error and yields a real PNG.
    """
    result = render_chart(
        "line",
        "NAV plan vs actual",
        _handle(_SERIES_ENVELOPE),
        series_column="nav_kind",
    )
    parsed = json.loads(result)

    # The spec still partitions into the two named series.
    assert {t["name"] for t in parsed["spec"]["data"]} == {"actual", "plan"}

    image_base64 = parsed["image_base64"]
    assert isinstance(image_base64, str) and image_base64
    assert base64.b64decode(image_base64).startswith(_PNG_MAGIC)
