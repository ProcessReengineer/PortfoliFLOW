# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.report_engine.ReportEngine`."""

from __future__ import annotations

import pandas as pd

from core.data_store import get_data_store
from services.reporting.report_engine import ReportEngine, ReportTile


def test_build_report_returns_portfolio_then_investments(
    populated_store_canonical: None,  # noqa: ARG001 — fixture seeds the DataStore
    investments: tuple[str, ...],
) -> None:
    """Tile order is portfolio first, then investments in canonical order."""
    tiles = ReportEngine().build_report()
    assert len(tiles) == 1 + len(investments)

    portfolio_tile = tiles[0]
    assert isinstance(portfolio_tile, ReportTile)
    assert portfolio_tile.is_portfolio_level is True
    assert portfolio_tile.title.startswith("Portfolio Overview")

    per_inv_titles = [t.title for t in tiles[1:]]
    assert per_inv_titles == list(investments)

    for tile in tiles:
        assert len(tile.figures) == 6
        assert len(tile.figure_titles) == 6
        assert len(tile.figures) == len(tile.figure_titles)
        assert all(fig is not None for fig in tile.figures)


def test_portfolio_tile_uses_portfolio_figure_titles(
    populated_store_canonical: None,  # noqa: ARG001
) -> None:
    """The portfolio tile has the redesigned chart titles in the new order."""
    tiles = ReportEngine().build_report()
    portfolio_tile = tiles[0]
    assert portfolio_tile.figure_titles == [
        "Investiertes Kapital & NAV",
        "Cashflows",
        "Multiples (TVPI / DPI / IRR)",
        "Country split",
        "Vintages",
        "Sector split",
    ]


def test_per_investment_tiles_use_new_figure_titles(
    populated_store_canonical: None,  # noqa: ARG001
) -> None:
    """Per-investment tiles use the redesigned timeseries chart titles."""
    tiles = ReportEngine().build_report()
    expected = [
        "Investiertes Kapital & NAV",
        "Cashflows",
        "Multiples (TVPI / DPI / IRR)",
        "Total Return seit Inception",
        "Country split",
        "Sector split",
    ]
    for tile in tiles[1:]:
        assert tile.figure_titles == expected


def test_portfolio_tile_subtitle_is_empty(
    populated_store_canonical: None,  # noqa: ARG001
) -> None:
    """The portfolio tile has no subtitle by design."""
    tiles = ReportEngine().build_report()
    assert tiles[0].subtitle == ""


def test_per_investment_subtitle_populated_when_attributes_present(
    populated_store_canonical: None,  # noqa: ARG001
) -> None:
    """Per-investment tiles carry a non-empty subtitle when metadata is present."""
    tiles = ReportEngine().build_report()
    # The conftest fixture populates Manager, Vintage Year, Sub-Class,
    # and Asset Class for all three investments.
    for tile in tiles[1:]:
        assert tile.subtitle != ""
        assert "Vintage" in tile.subtitle


def test_per_investment_subtitle_empty_when_attributes_missing(
    clean_store: None,  # noqa: ARG001
) -> None:
    """No metadata rows → subtitle is empty for per-investment tiles."""
    store = get_data_store()
    store.store(
        "attributes",
        pd.DataFrame({"Solo": ["Buyout"]}, index=["Investment Type"]),
    )
    store.store(
        "cash_flow_in_actual",
        pd.DataFrame({"Solo": [0.0]}, index=[pd.Timestamp("2024-01-01")]),
    )
    store.store(
        "cash_flow_out_actual",
        pd.DataFrame({"Solo": [-100.0]}, index=[pd.Timestamp("2023-01-01")]),
    )
    store.store(
        "navs_actual",
        pd.DataFrame(
            {"Solo": [50.0, 110.0]},
            index=[pd.Timestamp("2024-06-30"), pd.Timestamp("2024-12-31")],
        ),
    )
    tiles = ReportEngine().build_report()
    assert tiles[1].subtitle == ""


def test_build_report_empty_when_attributes_missing(
    clean_store: None,  # noqa: ARG001
) -> None:
    """An empty DataStore yields an empty tile list."""
    assert ReportEngine().build_report() == []


def test_build_report_uses_latest_nav_date(
    clean_store: None,  # noqa: ARG001
) -> None:
    """When ``report_date`` is ``None`` the engine uses the latest NAV date."""
    store = get_data_store()
    store.store(
        "attributes",
        pd.DataFrame({"Solo": ["Buyout"]}, index=["Investment Sub-Class"]),
    )
    store.store(
        "cash_flow_in_actual",
        pd.DataFrame({"Solo": [0.0]}, index=[pd.Timestamp("2024-01-01")]),
    )
    store.store(
        "cash_flow_out_actual",
        pd.DataFrame({"Solo": [-100.0]}, index=[pd.Timestamp("2023-01-01")]),
    )
    store.store(
        "navs_actual",
        pd.DataFrame(
            {"Solo": [50.0, 110.0]},
            index=[pd.Timestamp("2024-06-30"), pd.Timestamp("2024-12-31")],
        ),
    )
    tiles = ReportEngine().build_report()
    assert tiles[0].title.endswith("2024-12-31")
