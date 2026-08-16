# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""StatisticsService — workflow aggregator for the universe-wide stats page.

Consumes the Phase-4 :class:`InvestmentRepository` and
:class:`InvestmentNavRepository` to build a
:class:`UniverseStatisticsBundle` ready for the chart-spec
generators and the Statistics page template (sub-stream 5c).

The service does not run analytics itself: it loads NAV histories,
derives return series via :func:`compute_total_return_series`, and
delegates to the calculation primitives in
:mod:`services.analytics`. The analytics layer is the single source
of truth for the QT-consistent numerical conventions; this service
is the thin DB-aware wrapper that orchestrates per-investment loads
and packs the result into a single DTO.

Cross-tenant safety: every repository method runs under the active
tenant context (see :func:`core.repositories.tenant_context`); RLS
hides foreign-tenant rows. The service therefore correctly reports
absence rather than exposing cross-tenant data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from uuid import UUID

import pandas as pd

from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.tenant_repository import TenantRepository
from services.analytics import (
    DistributionStats,
    KeyMetricsCard,
    RiskMetrics,
    compute_correlation_matrix,
    compute_full_distribution_stats,
    compute_risk_metrics,
    compute_total_return_series,
)
from services.fx.functional_currency import build_portfolio_fx_converter

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UniverseStatisticsBundle:
    """Pre-computed bundle for the universe-wide Statistics surface.

    The chart-spec generators (sparklines, correlation heatmap) and
    the Jinja template (distribution table, risk pills) consume this
    DTO so the route handler invokes :class:`StatisticsService` once
    and passes the result through.

    Attributes:
        investment_names: Display names of the investments included in
            the bundle, in stable presentation order (alphabetical).
        key_metrics: One :class:`KeyMetricsCard` per investment,
            keyed by display name.
        correlation_matrix: Pairwise Pearson correlation matrix
            (square :class:`pandas.DataFrame`). Empty when fewer than
            two investments have a non-empty return series.
        distribution_stats: One :class:`DistributionStats` per
            investment, keyed by display name. Investments with no
            returns are not included so the consuming template can
            render an empty cell consistently.
        risk_metrics: One :class:`RiskMetrics` per investment, keyed
            by display name.
        risk_free_rate: Annualised risk-free rate threaded through to
            the Sharpe-ratio computation. Echoed back so the template
            can label the metric ("Sharpe @ rf=2.0%").
        as_of_date: Optional analysis as-of date — when set, NAV
            histories are truncated at this date before deriving
            returns. ``None`` means "use the full history".
    """

    investment_names: list[str]
    key_metrics: dict[str, KeyMetricsCard]
    correlation_matrix: pd.DataFrame
    distribution_stats: dict[str, DistributionStats]
    risk_metrics: dict[str, RiskMetrics]
    risk_free_rate: float
    as_of_date: _date | None


class StatisticsService:
    """Universe-wide statistics aggregator.

    Every repository must be tenant-scoped (the caller constructs
    them with a session obtained via
    :func:`core.repositories.tenant_context`). The service does not
    set or read ``app.tenant_id`` itself — that responsibility lives
    on the session.

    Per ADR-0102 the return series this service derives are converted
    into the tenant's functional currency at the ADR-0099 §4 boundary,
    so every *return-derived* statistic — annualised return, Sharpe,
    distribution, risk, and above all the cross-investment correlation
    matrix — is measured in one numéraire. A correlation computed from
    mixed-currency returns measures partly co-movement and partly the
    two currencies' FX paths, which is not a number anyone can use.

    The one figure that stays in its **position** currency is the KPI
    card's ``latest_nav``: it is published as a ``(value, currency)``
    pair, so it is self-describing rather than aggregated — the same
    reasoning that keeps the ADR-0101 §2 native cash balance native.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        tenants: TenantRepository,
        fx_rates: FxRateRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._tenants = tenants
        self._fx_rates = fx_rates

    async def get_universe_statistics(
        self,
        *,
        investment_ids: list[UUID] | None = None,
        as_of_date: _date | None = None,
        risk_free_rate: float = 0.0,
        active_only: bool = True,
    ) -> UniverseStatisticsBundle:
        """Build a :class:`UniverseStatisticsBundle` for the active tenant.

        Steps:

        1. Resolve the investment universe — by default the active
           investments of the tenant; when ``investment_ids`` is
           supplied, restrict to that subset (intersection: ids that
           don't resolve under RLS are silently dropped).
        2. Load each investment's actual NAV history. NAVs are
           optionally truncated to ``as_of_date``.
        3. Convert the NAV history into the tenant's functional
           currency (ADR-0102) and derive the periodic return series
           from the converted history via
           :func:`compute_total_return_series`. The KPI card's
           ``latest_nav`` is taken from the *unconverted* history —
           it is labelled with the position currency.
        4. Compute the per-investment KPI card, distribution stats,
           and risk metrics. Investments with empty return series
           are still represented with a card (NaN annualised return /
           Sharpe, empty sparkline) but excluded from the
           distribution / risk dicts so the template renders an
           "N/A" cell uniformly.
        5. Compute the pairwise Pearson correlation matrix on the
           subset of investments with non-empty return series.

        Args:
            investment_ids: Optional UUID filter. ``None`` means "all".
            as_of_date: Optional truncation date. NAVs and returns
                are restricted to entries on or before this date.
            risk_free_rate: Annualised risk-free rate (decimal) for
                the Sharpe-ratio calculation. Defaults to ``0.0`` to
                match the QT screens.
            active_only: When ``True`` (the default), only
                ``is_active = TRUE`` investments are included in the
                "all" path. Ignored when ``investment_ids`` is
                supplied.

        Returns:
            :class:`UniverseStatisticsBundle` covering every
            resolved investment. Empty universe → bundle with empty
            collections (the route renders the empty-state template).
        """
        investments = await self._resolve_universe(investment_ids, active_only=active_only)
        if not investments:
            return UniverseStatisticsBundle(
                investment_names=[],
                key_metrics={},
                correlation_matrix=pd.DataFrame(),
                distribution_stats={},
                risk_metrics={},
                risk_free_rate=risk_free_rate,
                as_of_date=as_of_date,
            )

        # Stable presentation order: alphabetical by investment name.
        investments_sorted = sorted(investments, key=lambda i: i.name)

        nav_series_by_name: dict[str, pd.Series] = {}
        return_series_by_name: dict[str, pd.Series] = {}
        currency_by_name: dict[str, str] = {}

        investment_ids = [inv.id for inv in investments_sorted]
        nav_rows_by_inv = await self._navs.list_by_investments_and_kind(investment_ids, "actual")

        # ADR-0099 §4 conversion boundary (extended here by ADR-0102). One
        # converter per request, built from the position currencies actually
        # present; a single-currency universe gets the identity pass-through
        # and reads zero FX rows.
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[inv.currency for inv in investments_sorted],
        )

        for inv in investments_sorted:
            nav_rows = nav_rows_by_inv.get(inv.id, [])
            nav_series = pd.Series(
                data=[float(n.nav_value) for n in nav_rows],
                index=[n.as_of_date for n in nav_rows],
                dtype="float64",
            )
            if as_of_date is not None and not nav_series.empty:
                nav_series = nav_series[nav_series.index <= as_of_date]
            # Native NAV history — the KPI card's (latest_nav, currency)
            # pair reads from here, so it stays in the position currency.
            nav_series_by_name[inv.name] = nav_series
            # Returns, however, feed the analytics layer, where they are
            # compared and correlated *across* investments. Convert first
            # (point-in-time, each NAV at its own date's rate) so a foreign
            # investment's return carries its FX effect and every series in
            # the correlation matrix shares one numéraire.
            return_series_by_name[inv.name] = compute_total_return_series(
                fx.convert_series(nav_series, inv.currency)
            )
            currency_by_name[inv.name] = inv.currency

        key_metrics: dict[str, KeyMetricsCard] = {}
        distribution_stats: dict[str, DistributionStats] = {}
        risk_metrics: dict[str, RiskMetrics] = {}

        for inv in investments_sorted:
            nav_series = nav_series_by_name[inv.name]
            return_series = return_series_by_name[inv.name]

            latest_nav = (
                float(nav_series.dropna().iloc[-1]) if not nav_series.dropna().empty else None
            )
            sparkline_values = _build_sparkline_values(return_series)

            risk = compute_risk_metrics(
                return_series,
                risk_free_rate_annual=risk_free_rate,
            )
            mean_ann = _safe_mean_annualised(return_series)

            key_metrics[inv.name] = KeyMetricsCard(
                investment_name=inv.name,
                latest_nav=latest_nav,
                currency=currency_by_name[inv.name],
                annualised_return=mean_ann,
                sharpe_ratio=risk.sharpe_ratio,
                sparkline_values=sparkline_values,
            )

            if not return_series.empty:
                distribution_stats[inv.name] = compute_full_distribution_stats(return_series)
                risk_metrics[inv.name] = risk

        # Correlation: only across investments with non-empty return series.
        correlated_inputs = {
            name: series for name, series in return_series_by_name.items() if not series.empty
        }
        if len(correlated_inputs) < 2:
            correlation_matrix = pd.DataFrame()
        else:
            correlation_matrix = compute_correlation_matrix(correlated_inputs)

        _LOG.debug(
            "StatisticsService: built bundle (n=%d, with_returns=%d, rf=%.4f, as_of=%s).",
            len(investments_sorted),
            len(correlated_inputs),
            risk_free_rate,
            as_of_date,
        )
        return UniverseStatisticsBundle(
            investment_names=[i.name for i in investments_sorted],
            key_metrics=key_metrics,
            correlation_matrix=correlation_matrix,
            distribution_stats=distribution_stats,
            risk_metrics=risk_metrics,
            risk_free_rate=risk_free_rate,
            as_of_date=as_of_date,
        )

    async def _resolve_universe(
        self,
        investment_ids: list[UUID] | None,
        *,
        active_only: bool,
    ) -> list[InvestmentDTO]:
        """Return the investments that will be included in the bundle."""
        if investment_ids is None:
            if active_only:
                return await self._investments.list_active()
            return await self._investments.list_all()

        if not investment_ids:
            return []

        # Resolve each id individually so RLS-hidden ids drop out
        # silently. ``list_all`` followed by an in-memory filter
        # would also work but loads the full universe; the per-id
        # path is cheaper for small filters.
        wanted = set(investment_ids)
        all_invs = await self._investments.list_all()
        return [inv for inv in all_invs if inv.id in wanted]


def _build_sparkline_values(return_series: pd.Series) -> list[float]:
    """``(1 + r).cumprod()`` as a list of floats, empty when too short.

    Mirrors ``gui/widgets/_statistics_helpers.py::_sparkline_data``:
    an empty list when the return series has fewer than two
    datapoints; the cumulative-performance values otherwise.
    """
    cleaned = return_series.dropna()
    if cleaned.size < 2:
        return []
    return [float(v) for v in (1.0 + cleaned).cumprod().to_list()]


def _safe_mean_annualised(return_series: pd.Series) -> float:
    """Annualised arithmetic mean, NaN when the series is empty.

    Centralised here so the KPI card and the distribution stats
    compute the same number even when one is skipped (e.g. an
    investment without returns still gets a KPI card with NaN return,
    while the distribution dict simply omits the entry).
    """
    from services.analytics import (
        annualise_mean_return,
        compute_mean_return,
    )

    return annualise_mean_return(compute_mean_return(return_series))
