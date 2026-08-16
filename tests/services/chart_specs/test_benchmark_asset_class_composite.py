# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``build_benchmark_asset_class_composite_spec`` (Stage b).

Pure-spec tests: no DB, no FastAPI. Covers the Phase-1b Quick-Wins
visual polish per ADR-0062 §3:

- Rows sorted by annualised excess descending; empty tiles sink to
  the end.
- Per-tile excess badge emitted in top-right of populated tiles only,
  with green/red colour matching positive/negative excess.
- Empty-tile benchmark line dimmed via the project's rgba alpha
  treatment (alpha 0.45).
"""

from __future__ import annotations

import pandas as pd

from services.chart_specs.benchmark_asset_class_composite import (
    build_benchmark_asset_class_composite_spec,
)
from services.chart_specs.base import get_chart_theme


def _ts_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2023-01-01", periods=n, freq="ME")


def _populated_row(
    name: str,
    excess: float,
    *,
    n_investments: int = 2,
) -> dict:
    idx = _ts_index(12)
    return {
        "asset_class_display_name": name,
        "benchmark_display_name": f"{name} Bench",
        "composite_cumulative": pd.Series([i * 0.02 for i in range(12)], index=idx),
        "benchmark_cumulative": pd.Series([i * 0.015 for i in range(12)], index=idx),
        "excess_return_annualised": excess,
        "information_ratio": excess * 3,
        "n_investments": n_investments,
    }


def _empty_row(name: str) -> dict:
    idx = _ts_index(12)
    return {
        "asset_class_display_name": name,
        "benchmark_display_name": f"{name} Bench",
        "composite_cumulative": pd.Series(dtype=float),
        "benchmark_cumulative": pd.Series([i * 0.001 for i in range(12)], index=idx),
        "excess_return_annualised": None,
        "information_ratio": None,
        "n_investments": 0,
    }


def _populated_tile_titles(spec: dict) -> list[str]:
    """Extract subplot title annotations in render order."""
    titles: list[str] = []
    for ann in spec["layout"]["annotations"]:
        text = ann.get("text", "")
        if not isinstance(text, str):
            continue
        if (
            ann.get("xanchor") == "center"
            and ann.get("yanchor") == "bottom"
            and not text.startswith("<b>")
        ):
            titles.append(text)
    return titles


def _badges(spec: dict) -> list[dict]:
    return [
        a
        for a in spec["layout"]["annotations"]
        if isinstance(a.get("text"), str) and a["text"].startswith("<b>") and "%" in a["text"]
    ]


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def test_rows_are_sorted_by_excess_descending() -> None:
    rows = [
        _populated_row("Bonds", -0.03),
        _populated_row("Cash-Empty", 0.0, n_investments=0),
        _populated_row("Equities", 0.05),
        _populated_row("Real Estate", 0.02),
    ]
    # Override the empty row to actually be empty.
    rows[1] = _empty_row("Cash-Empty")

    spec = build_benchmark_asset_class_composite_spec(rows)
    titles = _populated_tile_titles(spec)
    # Equities (+5%) → Real Estate (+2%) → Bonds (-3%) → Cash-Empty
    assert titles[:3] == ["Equities", "Real Estate", "Bonds"]
    assert "Cash-Empty" in titles
    assert titles.index("Cash-Empty") == len(titles) - 1


# ---------------------------------------------------------------------------
# Excess badge
# ---------------------------------------------------------------------------


def test_excess_badge_is_emitted_for_populated_tiles_only() -> None:
    rows = [
        _populated_row("Equities", 0.05),
        _empty_row("Cash"),
    ]
    spec = build_benchmark_asset_class_composite_spec(rows)
    badges = _badges(spec)
    # One badge for the populated Equities tile, none for empty Cash.
    assert len(badges) == 1


def test_excess_badge_uses_positive_colour_when_excess_positive() -> None:
    spec = build_benchmark_asset_class_composite_spec([_populated_row("Equities", 0.05)])
    colours = get_chart_theme()["colours"]
    badges = _badges(spec)
    assert len(badges) == 1
    assert badges[0]["font"]["color"] == colours["positive_bar"]
    # Plus sign in front, true minus only used for negatives.
    assert badges[0]["text"].startswith("<b>+")


def test_excess_badge_uses_negative_colour_when_excess_negative() -> None:
    spec = build_benchmark_asset_class_composite_spec([_populated_row("Bonds", -0.03)])
    colours = get_chart_theme()["colours"]
    badges = _badges(spec)
    assert len(badges) == 1
    assert badges[0]["font"]["color"] == colours["negative_bar"]
    # True minus sign U+2212, not a hyphen.
    assert "−" in badges[0]["text"]


# ---------------------------------------------------------------------------
# Empty-tile dimming
# ---------------------------------------------------------------------------


def test_empty_tile_benchmark_line_is_dimmed() -> None:
    spec = build_benchmark_asset_class_composite_spec([_empty_row("Cash")])
    # One trace — the dimmed benchmark line.
    assert len(spec["data"]) == 1
    line_colour = spec["data"][0]["line"]["color"]
    assert line_colour.startswith("rgba(")
    assert "0.45" in line_colour


# ---------------------------------------------------------------------------
# Subplot title / footer fonts
# ---------------------------------------------------------------------------


def test_subplot_title_and_footer_font_sizes_are_enlarged() -> None:
    """Per Step 9b: title 11 → 13, footer 9 → 11."""
    spec = build_benchmark_asset_class_composite_spec([_populated_row("Equities", 0.05)])
    title_annotations = [
        a
        for a in spec["layout"]["annotations"]
        if a.get("yanchor") == "bottom"
        and isinstance(a.get("text"), str)
        and not a["text"].startswith("<b>")
    ]
    footer_annotations = [a for a in spec["layout"]["annotations"] if a.get("yanchor") == "top"]
    assert title_annotations
    assert title_annotations[0]["font"]["size"] == 13
    assert footer_annotations
    assert footer_annotations[0]["font"]["size"] == 11


# ---------------------------------------------------------------------------
# Regression: empty rows list still renders an empty-state spec
# ---------------------------------------------------------------------------


def test_empty_rows_list_renders_empty_state_spec() -> None:
    spec = build_benchmark_asset_class_composite_spec([])
    assert spec["data"] == []
    # The empty-state annotation is present.
    texts = [a.get("text", "") for a in spec["layout"]["annotations"]]
    assert any("No asset classes" in t for t in texts)
