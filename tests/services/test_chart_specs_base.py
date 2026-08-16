# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.chart_specs.base``.

The module is the bridge between PortfoliFLOW's canonical chart
theme JSON and Plotly's layout schema. The tests exercise:

* The cache behaviour of :func:`get_chart_theme` /
  :func:`reload_chart_theme`.
* The shape and key set produced by :func:`layout_from_theme`.
* The semantic palette returned by :func:`color_palette`.
"""

from __future__ import annotations

import re

from services.chart_specs.base import (
    color_palette,
    get_chart_theme,
    layout_from_theme,
    reload_chart_theme,
)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_get_chart_theme_returns_canonical_keys() -> None:
    theme = get_chart_theme()
    # Every section the spec builder relies on must be present.
    for required in ("font", "colours", "line", "optimization"):
        assert required in theme, f"chart theme missing {required!r}"


def test_get_chart_theme_is_cached() -> None:
    a = get_chart_theme()
    b = get_chart_theme()
    assert a is b, "Chart theme dict should be cached across calls."


def test_reload_chart_theme_resets_cache() -> None:
    cached = get_chart_theme()
    fresh = reload_chart_theme()
    # Same content, but a freshly loaded dict so identity differs.
    assert fresh == cached
    assert fresh is not cached


def test_layout_from_theme_top_level_keys() -> None:
    layout = layout_from_theme(title="t", xlabel="x", ylabel="y")
    expected = {
        "title",
        "xaxis",
        "yaxis",
        "paper_bgcolor",
        "plot_bgcolor",
        "showlegend",
        "legend",
        "hoverlabel",
        "margin",
    }
    assert expected.issubset(set(layout)), (
        "layout_from_theme must populate the documented top-level keys."
    )


def test_layout_from_theme_uses_theme_colours() -> None:
    theme = get_chart_theme()
    layout = layout_from_theme(title="t", xlabel="x", ylabel="y")
    assert layout["paper_bgcolor"] == theme["colours"]["background"]
    assert layout["plot_bgcolor"] == theme["colours"]["plot_area"]
    assert layout["xaxis"]["gridcolor"] == theme["colours"]["grid"]
    assert layout["yaxis"]["gridcolor"] == theme["colours"]["grid"]
    assert layout["title"]["font"]["color"] == theme["colours"]["text"]


def test_layout_from_theme_carries_titles() -> None:
    layout = layout_from_theme(title="My Chart", xlabel="Vol", ylabel="Return", show_legend=False)
    assert layout["title"]["text"] == "My Chart"
    assert layout["xaxis"]["title"]["text"] == "Vol"
    assert layout["yaxis"]["title"]["text"] == "Return"
    assert layout["showlegend"] is False


def test_layout_from_theme_font_family_is_string_for_plotly() -> None:
    """Plotly expects a font family as a string, not a list."""
    layout = layout_from_theme(title="t", xlabel="x", ylabel="y")
    assert isinstance(layout["title"]["font"]["family"], str)
    assert isinstance(layout["xaxis"]["title"]["font"]["family"], str)
    assert isinstance(layout["legend"]["font"]["family"], str)


def test_color_palette_returns_hex_strings() -> None:
    palette = color_palette()
    for role in ("frontier", "tangency", "min_var", "cml", "cloud", "rf_line"):
        assert role in palette, f"palette missing role {role!r}"
        assert _HEX_RE.match(palette[role]), (
            f"palette[{role!r}]={palette[role]!r} is not a hex colour"
        )
