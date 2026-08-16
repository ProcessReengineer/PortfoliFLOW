# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Smoke tests for the Phase-3 static-asset additions.

These are deliberately grep-based — they assert that the relevant
files exist on disk and contain the expected hooks. Browser-rendering
behaviour is verified manually as part of the sub-stream 3c
acceptance walkthrough.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR: Path = _REPO_ROOT / "web" / "templates"
_STATIC_DIR: Path = _REPO_ROOT / "web" / "static"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_charts_css_defines_chart_container() -> None:
    css = _read(_STATIC_DIR / "css" / "components" / "charts.css")
    assert ".chart-container {" in css
    assert ".plotly-target" in css
    # matplotlib reservation is documented as a CSS hook for Phase 5+.
    assert "img.matplotlib-output" in css


def test_charts_css_uses_theme_variables() -> None:
    """The chart container must read from theme.css variables."""
    css = _read(_STATIC_DIR / "css" / "components" / "charts.css")
    assert "var(--chart-colours-background)" in css
    assert "var(--chart-colours-grid)" in css


def test_tables_css_defines_tabulator_overrides() -> None:
    css = _read(_STATIC_DIR / "css" / "components" / "tables.css")
    assert ".tabulator " in css or ".tabulator{" in css
    assert ".tabulator-header" in css
    assert ".tabulator-row" in css
    assert "var(--chart-table-cell-bg-even)" in css
    # Active-config badge used in the SAA list view.
    assert ".badge-active" in css


def test_tables_css_uses_table_border_token_not_grid_token() -> None:
    """The Tabulator borders must read from the dedicated table-border
    token, not from the chart-grid token. The grid token is intentionally
    transparent for charts; routing Tabulator borders through it lets
    the Tabulator built-in light-grey default CSS shine through and
    breaks the dark-theme visual."""
    css = _read(_STATIC_DIR / "css" / "components" / "tables.css")
    assert "var(--chart-table-border-colour)" in css, (
        "tables.css must reference --chart-table-border-colour for Tabulator borders."
    )
    # Negative assertion — no leftover grid-token reference for borders.
    # (Other tokens like background and cell-bg remain unchanged.)
    assert "var(--chart-colours-grid)" not in css, (
        "tables.css must not reference --chart-colours-grid; that token "
        "is reserved for chart frames and is intentionally transparent."
    )


def test_base_html_includes_chart_container_and_table_css() -> None:
    base = _read(_TEMPLATES_DIR / "base.html")
    assert "components/charts.css" in base
    assert "components/tables.css" in base


def test_base_html_includes_plotly_cdn() -> None:
    base = _read(_TEMPLATES_DIR / "base.html")
    assert "cdn.plot.ly/plotly-" in base


def test_base_html_includes_tabulator_cdn() -> None:
    base = _read(_TEMPLATES_DIR / "base.html")
    # CSS and JS deliveries are pinned to the same Tabulator version.
    assert "tabulator-tables" in base
    assert "/dist/css/tabulator.min.css" in base
    assert "/dist/js/tabulator.min.js" in base


def test_base_html_loads_tabulator_base_css_before_tables_overrides() -> None:
    """The Tabulator base CSS must load before components/tables.css.

    tables.css contains the PortfoliFLOW dark-theme overrides for
    Tabulator headers, borders, and cell backgrounds. If the
    Tabulator base CSS were loaded last (as it was prior to this
    guard), its built-in light-theme defaults would beat our
    dark-theme overrides in the cascade — the table headers would
    render white instead of #2A2A2A and the dark-theme aesthetic
    would silently break.

    This guard asserts the relative file-position of the two
    <link> declarations, not their absolute line numbers, so the
    test stays robust against unrelated additions to the <head>.
    """
    base = _read(_TEMPLATES_DIR / "base.html")
    # Match the stylesheet <link> declarations, not any prose mention of
    # the filenames. The explanatory comment above the Tabulator <link>
    # references "components/tables.css", so a bare-substring search for
    # that filename would match the comment (which sits *before* the
    # Tabulator link) instead of the actual override stylesheet link.
    tabulator_cdn_pos = base.find("tabulator-tables@5.6.1/dist/css/tabulator.min.css")
    tables_override_pos = base.find('href="/static/css/components/tables.css"')
    assert tabulator_cdn_pos != -1, "Tabulator base CSS link is missing."
    assert tables_override_pos != -1, "components/tables.css link is missing."
    assert tabulator_cdn_pos < tables_override_pos, (
        "Tabulator base CSS must be loaded before components/tables.css "
        "so the PortfoliFLOW overrides win the cascade. Found Tabulator "
        f"at position {tabulator_cdn_pos}, tables.css at position "
        f"{tables_override_pos}."
    )


def test_theme_css_exposes_required_chart_variables() -> None:
    """The generated theme.css must carry the variables the Phase-3
    components reference. If the generator drops a property, the SAA
    chart frame and Tabulator overrides break silently — this guard
    surfaces that as a test failure instead."""
    theme_css = _read(_STATIC_DIR / "css" / "theme.css")
    required = (
        "--chart-colours-background",
        "--chart-colours-grid",
        "--chart-colours-text",
        "--chart-table-header-bg",
        "--chart-table-cell-bg-even",
        "--chart-table-cell-bg-odd",
        "--chart-table-border-colour",
    )
    for prop in required:
        assert prop in theme_css, f"theme.css is missing {prop}"


def test_saa_templates_exist() -> None:
    """The SAA surface was consolidated into the long-scroll partials
    layout (ADR-0054); the templates now live under
    web/templates/_partials/ rather than the original web/templates/saa/
    directory. This guard pins the current file locations."""
    partials_dir = _TEMPLATES_DIR / "_partials"
    assert (partials_dir / "saa_section.html").exists()
    assert (partials_dir / "saa_configuration_partial.html").exists()
    assert (partials_dir / "saa_optimization_partial.html").exists()
    assert (partials_dir / "saa_optimization_error.html").exists()
