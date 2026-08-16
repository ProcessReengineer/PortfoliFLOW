# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Orchestrator for the Portfolio Review report.

Composes data providers and chart builders to produce a list of
:class:`ReportTile` instances — one for the portfolio aggregate plus one per
investment in canonical (Excel-row-1) order.

Tile 1 (portfolio) layout (German titles per existing user convention)::

    [invested_nav, cashflow_with_nav, multiples_timeseries,
     country, vintages, sector]

Per-investment tiles share four chart types with the portfolio tile, plus a
single-investment Total Return time series and country / sector treemaps::

    [invested_nav, cashflow_with_nav, multiples_timeseries,
     total_return, country, sector]

A subtitle line below each per-investment tile title carries compact fund
metadata (Manager, Vintage, Sub-Class, Asset Class).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
from matplotlib.figure import Figure

from core.chart_theme import get_chart_theme
from core.data_store import get_data_store
from services.reporting.chart_builders import (
    ClusteredHorizontalBarBuilder,
    HorizontalBarBuilder,
    LineChartBuilder,
    StackedAreaWithLineBuilder,
    StackedBarBuilder,
    StackedBarWithLineBuilder,
    TreemapBuilder,
    VerticalBarBuilder,
)
from services.reporting.data_providers import (
    CashflowProvider,
    CashflowWithNavProvider,
    CountryProvider,
    InvestedNavProvider,
    IRRProvider,
    KeyFigures,
    KeyFiguresProvider,
    MultiplesProvider,
    MultiplesTimeseriesProvider,
    ProviderContext,
    SectorProvider,
    StrategyProvider,
    TotalReturnTimeseriesProvider,
    VintagesProvider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportTile:
    """One tile in the rendered report.

    Attributes:
        title: Tile header text (e.g. ``"Portfolio Overview — Stichtag:
            2025-09-30"`` or ``"Investition A"``).
        is_portfolio_level: ``True`` for the first (aggregate) tile.
        key_figures: :class:`KeyFigures` shown in the strip beneath the
            header.
        figures: List of 6 matplotlib :class:`~matplotlib.figure.Figure`
            instances in render order.  For the portfolio tile:
            ``[invested_nav, cashflow_with_nav, multiples_timeseries,
            country, vintages, sector]``.  For per-investment tiles:
            ``[invested_nav, cashflow_with_nav, multiples_timeseries,
            total_return, country, sector]``.
        figure_titles: Aligned with ``figures`` — the human-readable title
            for each chart cell, used by the GUI widget for labelling
            no-data placeholders.  Length must match ``figures``.
        subtitle: Optional one-line metadata string rendered below the tile
            title.  Empty string for the portfolio tile.
    """

    title: str
    is_portfolio_level: bool
    key_figures: KeyFigures
    figures: list[Figure]
    figure_titles: list[str]
    subtitle: str = ""

    @property
    def chart_titles(self) -> list[str]:
        """Backwards-compatible alias for :attr:`figure_titles`."""
        return self.figure_titles


_PORTFOLIO_FIGURE_TITLES: tuple[str, str, str, str, str, str] = (
    "Investiertes Kapital & NAV",
    "Cashflows",
    "Multiples (TVPI / DPI / IRR)",
    "Country split",
    "Vintages",
    "Sector split",
)

_INVESTMENT_FIGURE_TITLES: tuple[str, str, str, str, str, str] = (
    "Investiertes Kapital & NAV",
    "Cashflows",
    "Multiples (TVPI / DPI / IRR)",
    "Total Return seit Inception",
    "Country split",
    "Sector split",
)


_CASHFLOW_NAV_CONFIG = StackedBarWithLineBuilder(
    bar_columns=("calls", "distributions"),
    line_columns=("nav", "ncg"),
    bar_y_format="millions_eur",
    line_y_format="millions_eur",
    line_axis="primary",
)

_MULTIPLES_TS_CONFIG = StackedBarWithLineBuilder(
    bar_columns=("dpi", "rvpi"),
    line_columns=("irr",),
    bar_label_column="tvpi",
    bar_y_format="multiple_x",
    line_y_format="percent",
    line_axis="secondary",
)


_SUBTITLE_PLACEHOLDERS = frozenset({"nan", "none", "klasse der investition"})


class ReportEngine:
    """Build the multi-tile Portfolio Review report from DataStore contents."""

    def __init__(self) -> None:
        # ---- Per-investment tile providers (legacy — kept for back-compat) ----
        self._cashflow = CashflowProvider()
        self._multiples = MultiplesProvider()
        self._irr = IRRProvider()
        self._strategy = StrategyProvider()
        self._country = CountryProvider()
        self._sector = SectorProvider()
        self._key_figures = KeyFiguresProvider()

        # Legacy builders are still registered so future re-use stays cheap;
        # the redesigned per-investment tile no longer calls them.
        self._stacked_bar = StackedBarBuilder()
        self._multiples_bar = ClusteredHorizontalBarBuilder(value_format="x")
        self._irr_bar = ClusteredHorizontalBarBuilder(value_format="%")
        self._strategy_bar = HorizontalBarBuilder(
            category_column="sub_class",
            value_column="nav_share",
            as_percent=True,
        )
        self._breakdown_bar = HorizontalBarBuilder(
            category_column="category",
            value_column="share",
            as_percent=True,
        )

        # ---- Shared (portfolio + per-investment) providers ----
        self._invested_nav = InvestedNavProvider()
        self._cashflow_with_nav = CashflowWithNavProvider(self._cashflow)
        self._multiples_ts = MultiplesTimeseriesProvider()
        self._vintages = VintagesProvider()
        self._total_return_ts = TotalReturnTimeseriesProvider()

        # ---- Chart builders ----
        self._area_with_line = StackedAreaWithLineBuilder()
        self._cashflow_nav_chart = _CASHFLOW_NAV_CONFIG
        self._multiples_ts_chart = _MULTIPLES_TS_CONFIG
        self._treemap = TreemapBuilder(category_column="category", share_column="share")
        self._vintages_bar = VerticalBarBuilder(
            value_column="nav_share",
            label_column="investment_count",
            as_percent=True,
        )
        self._line_chart = LineChartBuilder(
            y_label="Rebased (Inception = 100)",
            baseline=100.0,
        )

    def build_report(
        self,
        report_date: pd.Timestamp | None = None,
    ) -> list[ReportTile]:
        """Build all tiles in render order.

        Args:
            report_date: As-of date for the report.  If ``None`` the engine
                uses the latest non-all-NaN date in ``navs_actual``.

        Returns:
            Ordered list ``[portfolio_tile, *per_investment_tiles_in_canonical_order]``.
            Empty list if the DataStore lacks the essential ``attributes`` or
            ``navs_actual`` datasets.
        """
        store = get_data_store()
        df_attr = store.get("attributes")
        df_nav = store.get("navs_actual")
        if df_attr is None or df_nav is None:
            return []

        all_investments = tuple(str(c) for c in df_attr.columns)
        if not all_investments:
            return []

        resolved_date = self._resolve_report_date(df_nav, report_date)
        if resolved_date is None:
            return []

        theme = get_chart_theme()
        tiles: list[ReportTile] = []

        portfolio_ctx = ProviderContext(
            report_date=resolved_date,
            all_investments=all_investments,
            investment_filter=None,
        )
        portfolio_title = f"Portfolio Overview — Stichtag: {resolved_date.strftime('%Y-%m-%d')}"
        tiles.append(self._build_portfolio_tile(portfolio_ctx, portfolio_title, theme))

        for inv in all_investments:
            ctx = ProviderContext(
                report_date=resolved_date,
                all_investments=all_investments,
                investment_filter=inv,
            )
            subtitle = self._build_subtitle(inv, df_attr)
            tiles.append(self._build_investment_tile(ctx, inv, theme, subtitle))

        return tiles

    # ------------------------------------------------------------------
    # Tile builders
    # ------------------------------------------------------------------

    def _build_portfolio_tile(
        self,
        ctx: ProviderContext,
        title: str,
        theme: dict,
    ) -> ReportTile:
        """Run all six providers and builders for the portfolio overview tile.

        Args:
            ctx: Provider context (portfolio scope).
            title: Tile header text.
            theme: The full chart theme dict.

        Returns:
            A populated :class:`ReportTile`.
        """
        invested_nav_df = self._invested_nav.get(ctx)
        cashflow_with_nav_df = self._cashflow_with_nav.get(ctx)
        multiples_ts_df = self._multiples_ts.get(ctx)
        country_df = self._country.get(ctx)
        vintages_df = self._vintages.get(ctx)
        sector_df = self._sector.get(ctx)
        kf = self._key_figures.get(ctx)

        figures: list[Figure] = [
            self._area_with_line.build(invested_nav_df, theme, _PORTFOLIO_FIGURE_TITLES[0]),
            self._cashflow_nav_chart.build(
                cashflow_with_nav_df, theme, _PORTFOLIO_FIGURE_TITLES[1]
            ),
            self._multiples_ts_chart.build(multiples_ts_df, theme, _PORTFOLIO_FIGURE_TITLES[2]),
            self._treemap.build(country_df, theme, _PORTFOLIO_FIGURE_TITLES[3]),
            self._vintages_bar.build(vintages_df, theme, _PORTFOLIO_FIGURE_TITLES[4]),
            self._treemap.build(sector_df, theme, _PORTFOLIO_FIGURE_TITLES[5]),
        ]
        return ReportTile(
            title=title,
            is_portfolio_level=True,
            key_figures=kf,
            figures=figures,
            figure_titles=list(_PORTFOLIO_FIGURE_TITLES),
        )

    def _build_investment_tile(
        self,
        ctx: ProviderContext,
        title: str,
        theme: dict,
        subtitle: str,
    ) -> ReportTile:
        """Run all six providers and builders for a per-investment tile.

        Args:
            ctx: Provider context (single-investment scope).
            title: Tile header text — the investment name.
            theme: The full chart theme dict.
            subtitle: Pre-built metadata line (Manager, Vintage, Sub-Class,
                Asset Class).  Empty string suppresses subtitle rendering
                in the widget.

        Returns:
            A populated :class:`ReportTile`.
        """
        invested_nav_df = self._invested_nav.get(ctx)
        cashflow_with_nav_df = self._cashflow_with_nav.get(ctx)
        multiples_ts_df = self._multiples_ts.get(ctx)
        total_return_df = self._total_return_ts.get(ctx)
        country_df = self._country.get(ctx)
        sector_df = self._sector.get(ctx)
        kf = self._key_figures.get(ctx)

        figures: list[Figure] = [
            self._area_with_line.build(invested_nav_df, theme, _INVESTMENT_FIGURE_TITLES[0]),
            self._cashflow_nav_chart.build(
                cashflow_with_nav_df, theme, _INVESTMENT_FIGURE_TITLES[1]
            ),
            self._multiples_ts_chart.build(multiples_ts_df, theme, _INVESTMENT_FIGURE_TITLES[2]),
            self._line_chart.build(total_return_df, theme, _INVESTMENT_FIGURE_TITLES[3]),
            self._treemap.build(country_df, theme, _INVESTMENT_FIGURE_TITLES[4]),
            self._treemap.build(sector_df, theme, _INVESTMENT_FIGURE_TITLES[5]),
        ]
        return ReportTile(
            title=title,
            is_portfolio_level=False,
            key_figures=kf,
            figures=figures,
            figure_titles=list(_INVESTMENT_FIGURE_TITLES),
            subtitle=subtitle,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_report_date(
        self,
        df_nav: pd.DataFrame,
        report_date: pd.Timestamp | None,
    ) -> pd.Timestamp | None:
        """Return the effective as-of date, or ``None`` if NAVs has no usable rows.

        Args:
            df_nav: The ``navs_actual`` DataFrame.
            report_date: Optional caller-supplied as-of date.

        Returns:
            A normalised :class:`pandas.Timestamp`, or ``None`` if the input
            DataFrame has no non-NaN rows.
        """
        if report_date is not None:
            return pd.Timestamp(report_date)
        df = df_nav.dropna(how="all")
        if df.empty:
            return None
        return pd.Timestamp(df.index.max())

    def _build_subtitle(self, inv: str, df_attr: pd.DataFrame) -> str:
        """Build a comma-separated subtitle line for a per-investment tile.

        Reads four attribute rows (Manager / Fondsname, Vintage Year,
        Investment Sub-Class, Asset Class) from the attributes DataFrame for
        the given investment.  Drops any field whose value is empty, NaN, or
        a known placeholder string.

        Args:
            inv: Investment column name.
            df_attr: The attributes DataFrame.

        Returns:
            A single-line comma-separated string.  Empty if no fields are
            populated.
        """
        if inv not in df_attr.columns:
            return ""

        fields: list[str] = []

        manager = self._read_attr(df_attr, "Manager / Fondsname", inv)
        if manager:
            fields.append(manager)

        vintage = self._read_attr(df_attr, "Vintage Year", inv)
        if vintage:
            fields.append(f"Vintage {vintage}")

        sub_class = self._read_attr(df_attr, "Investment Sub-Class", inv)
        if sub_class:
            fields.append(sub_class)

        asset_class = self._read_attr(df_attr, "Asset Class", inv)
        if asset_class:
            fields.append(asset_class)

        return ", ".join(fields)

    @staticmethod
    def _read_attr(df_attr: pd.DataFrame, row: str, inv: str) -> str:
        """Read one attribute cell, returning ``""`` for missing/empty/placeholder values.

        Args:
            df_attr: The attributes DataFrame.
            row: Row label to read.
            inv: Investment (column) name.

        Returns:
            Stripped string value, or ``""`` if the cell is missing, NaN,
            empty, or a known placeholder.  Integer-valued floats (e.g.
            ``2018.0``) are rendered as plain integers.
        """
        if row not in df_attr.index:
            return ""
        val = df_attr.loc[row, inv]
        if pd.isna(val):
            return ""
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        s = str(val).strip()
        if not s or s.lower() in _SUBTITLE_PLACEHOLDERS:
            return ""
        return s


__all__ = [
    "ReportEngine",
    "ReportTile",
]
