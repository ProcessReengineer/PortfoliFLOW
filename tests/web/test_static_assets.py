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


# Third-party assets are served from the tree, not a CDN (ADR-0037 §9).
# The paths below are the contract between base.html and web/static/vendor/;
# test_vendor_files_present asserts each one resolves to a file on disk, so a
# version bump that misses the template — or vice versa — fails here rather
# than as an empty page in a browser.
_VENDOR_PLOTLY_JS = "/static/vendor/plotly-2.35.2/plotly.min.js"
_VENDOR_TABULATOR_CSS = "/static/vendor/tabulator-5.6.1/tabulator.min.css"
_VENDOR_TABULATOR_JS = "/static/vendor/tabulator-5.6.1/tabulator.min.js"
_VENDOR_HTMX_JS = "/static/vendor/htmx-1.9.12/htmx.min.js"
_VENDOR_HTMX_SSE_JS = "/static/vendor/htmx-1.9.12/ext/sse.js"

_VENDOR_PATHS = (
    _VENDOR_PLOTLY_JS,
    _VENDOR_TABULATOR_CSS,
    _VENDOR_TABULATOR_JS,
    _VENDOR_HTMX_JS,
    _VENDOR_HTMX_SSE_JS,
)


def test_base_html_loads_plotly_from_vendor() -> None:
    """Plotly is served from the tree; no CDN delivery remains."""
    base = _read(_TEMPLATES_DIR / "base.html")
    assert _VENDOR_PLOTLY_JS in base
    assert "cdn.plot.ly" not in base


def test_base_html_loads_tabulator_from_vendor() -> None:
    """CSS and JS deliveries are pinned to the same vendored version."""
    base = _read(_TEMPLATES_DIR / "base.html")
    assert _VENDOR_TABULATOR_CSS in base
    assert _VENDOR_TABULATOR_JS in base
    assert "unpkg.com" not in base


def test_base_html_loads_htmx_from_vendor() -> None:
    """HTMX and its SSE extension load locally, without SRI.

    Subresource Integrity guards bytes fetched over the network. These
    are read from the same checkout as the template, so an ``integrity``
    attribute would pin the shell to a hash that a legitimate version
    bump has to remember to update — a guard with no threat left to
    cover. Byte-identity with the CDN copies was verified once, when the
    files were vendored (docs/reports/P-UX-A0v-report.md).
    """
    base = _read(_TEMPLATES_DIR / "base.html")
    assert _VENDOR_HTMX_JS in base
    assert _VENDOR_HTMX_SSE_JS in base
    assert "unpkg.com" not in base
    assert "integrity=" not in base


def test_vendor_files_present() -> None:
    """Every vendored path base.html references resolves to a file."""
    for href in _VENDOR_PATHS:
        relative = href.removeprefix("/static/")
        path = _STATIC_DIR / relative
        assert path.is_file(), f"Vendored asset missing from the tree: {href}"


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
    # Since the local-vendor switch the anchor is the vendored path; the
    # assertion is unchanged, only the string it looks for.
    tabulator_base_pos = base.find(_VENDOR_TABULATOR_CSS)
    tables_override_pos = base.find('href="/static/css/components/tables.css"')
    assert tabulator_base_pos != -1, "Tabulator base CSS link is missing."
    assert tables_override_pos != -1, "components/tables.css link is missing."
    assert tabulator_base_pos < tables_override_pos, (
        "Tabulator base CSS must be loaded before components/tables.css "
        "so the PortfoliFLOW overrides win the cascade. Found Tabulator "
        f"at position {tabulator_base_pos}, tables.css at position "
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
