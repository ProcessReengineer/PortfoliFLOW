# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Phase-5 Plotly theme — port of chart_theme.json (Phase 3).

Dark canvas, ``#E8304A`` red accent. Matches the visual language of
the QT matplotlib plots.

Per ADR-0045 §1, the canonical theme substrate is
``config/chart_theme.json`` (read by :func:`services.chart_specs.base
.get_chart_theme`); this module exposes a Plotly-shaped layout
template derived from those parameters so the new Phase-5 chart-spec
generators inherit the same dark canvas / colour palette without
duplicating the JSON contract. Existing Phase-3 generators keep
using :func:`layout_from_theme` directly — see
``services/chart_specs/base.py``.
"""

from __future__ import annotations

from typing import Any

from services.chart_specs.base import get_chart_theme


def _font_family_str() -> str:
    """Resolve the canonical font-family list to a Plotly-friendly string."""
    family = get_chart_theme()["font"]["family"]
    if isinstance(family, list):
        return ", ".join(family)
    return str(family)


def _series_colorway() -> list[str]:
    """Return the canonical multi-series palette as a Plotly colorway."""
    palette = get_chart_theme()["colours"].get("series_palette")
    if isinstance(palette, list) and palette:
        return list(palette)
    # Defensive fallback — every shipped theme variant carries a
    # series palette, but the loader does not enforce it.
    colours = get_chart_theme()["colours"]
    return [
        colours["primary"],
        colours["secondary"],
        colours["tertiary"],
        colours.get("quaternary", colours["primary"]),
    ]


def dark_layout_template() -> dict[str, Any]:
    """Build the Phase-5 dark-theme Plotly layout template fresh from the JSON.

    The values are read from the active chart theme so that a runtime
    theme switch (the GUI Phase-B picker calling
    :meth:`ThemeService.set_active_chart_theme`) is reflected on the
    next request without further bookkeeping. Returning a fresh dict
    each call keeps :func:`apply_theme` free of cache-aliasing
    surprises when callers mutate the layout.

    Returns:
        A Plotly-shape ``layout`` dict. Pass to
        :func:`apply_theme` (or merge directly into a figure dict).
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    font = theme["font"]
    family = _font_family_str()

    return {
        "paper_bgcolor": colours["background"],
        "plot_bgcolor": colours["plot_area"],
        "font": {
            "family": family,
            "color": colours["text"],
            "size": font["tick_label_size"],
        },
        "colorway": _series_colorway(),
        "xaxis": {
            "gridcolor": colours["grid"],
            "linecolor": colours["axis_line"],
            "zerolinecolor": colours["grid"],
            "tickfont": {
                "family": family,
                "size": font["tick_label_size"],
                "color": colours["text"],
            },
        },
        "yaxis": {
            "gridcolor": colours["grid"],
            "linecolor": colours["axis_line"],
            "zerolinecolor": colours["grid"],
            "tickfont": {
                "family": family,
                "size": font["tick_label_size"],
                "color": colours["text"],
            },
        },
        "hovermode": "x unified",
        "hoverlabel": {
            "font": {
                "family": family,
                "size": font["tick_label_size"],
                "color": colours["text"],
            },
            "bgcolor": colours["plot_area"],
            "bordercolor": colours["grid"],
        },
        "legend": {
            "font": {
                "family": family,
                "size": font["legend_size"],
                "color": colours["text"],
            },
            "bgcolor": colours["background"],
            "bordercolor": colours["grid"],
            "borderwidth": 1,
        },
        "margin": {"l": 50, "r": 30, "t": 40, "b": 40},
    }


# Re-exposed as a module-level constant for the rare consumer that
# wants the static template (e.g. test fixtures snapshotting the
# baseline). Most code should call :func:`dark_layout_template` so
# that runtime theme switches stay live.
DARK_LAYOUT_TEMPLATE: dict[str, Any] = dark_layout_template()


def themed_secondary_axis() -> dict[str, Any]:
    """Return the theme seam for a secondary or domain-split axis.

    :func:`apply_theme` only themes the primary ``xaxis`` and
    ``yaxis``. Specs that introduce a secondary axis (``xaxis2`` /
    ``yaxis2`` with ``overlaying``) or split a figure into two axis
    domains must theme those extra axes themselves — otherwise they
    fall back to Plotly's default light-theme gridlines and fonts on
    the dark canvas. This helper returns the same ``gridcolor`` /
    ``linecolor`` / ``zerolinecolor`` / ``tickfont`` values that
    :func:`dark_layout_template` applies to the primary axes, so the
    extra axes match. Spread the result into the axis definition::

        layout["xaxis2"] = {"domain": [0.54, 1.0], **themed_secondary_axis()}

    Returns:
        A dict carrying the axis colour and tick-font seam. Axis-shape
        keys (``domain``, ``overlaying``, ``side``, ``title``, …) are
        the caller's responsibility.
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    font = theme["font"]
    family = _font_family_str()
    return {
        "gridcolor": colours["grid"],
        "linecolor": colours["axis_line"],
        "zerolinecolor": colours["grid"],
        "tickfont": {
            "family": family,
            "size": font["tick_label_size"],
            "color": colours["text"],
        },
    }


def apply_theme(fig_dict: dict[str, Any]) -> dict[str, Any]:
    """Merge the dark layout template into a Plotly figure dict.

    Existing ``layout`` keys on ``fig_dict`` win — the template only
    fills in defaults — so spec generators can override individual
    elements (axis types, tick formatters, hover modes) per chart
    without losing the rest of the theme.

    Args:
        fig_dict: A Plotly figure dict (``{"data": [...],
            "layout": {...}}``). Mutated in place and returned.

    Returns:
        The same dict, with theme defaults populated under
        ``layout``.
    """
    layout = fig_dict.setdefault("layout", {})
    template = dark_layout_template()
    for key, value in template.items():
        if key in ("xaxis", "yaxis"):
            axis = layout.setdefault(key, {})
            for axis_key, axis_value in value.items():
                axis.setdefault(axis_key, axis_value)
        elif key in ("font", "hoverlabel", "legend"):
            block = layout.setdefault(key, {})
            for sub_key, sub_value in value.items():
                block.setdefault(sub_key, sub_value)
        else:
            layout.setdefault(key, value)
    return fig_dict
