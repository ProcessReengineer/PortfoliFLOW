# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfolioAnalysisService — orchestrator for the Portfolio Analysis page.

Loads the active investment universe of the current tenant, derives
periodic-return series from each investment's actual NAV history,
restricts the matrix to a common observation window (so the sample
covariance is well-conditioned for SLSQP), annualises with the
QT-side convention, and runs the sub-stream 5d analytics layer.

Per ADR-0042 §1, the bundle is computed on demand and not persisted;
the route handler caches nothing across requests.

Cross-tenant safety: every repository call runs under the active
tenant context (see :func:`core.repositories.tenant_context`); RLS
hides foreign-tenant rows. The service therefore correctly reports
absence rather than exposing cross-tenant data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from uuid import UUID

import numpy as np
import pandas as pd

from services.analytics.sample_window import restrict_to_common_window
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.tenant_repository import TenantRepository
from services.analytics import (
    CapitalMarketLine,
    EfficientFrontierResult,
    MinVariancePortfolio,
    TangencyPortfolio,
    compute_capital_market_line,
    compute_cashflow_adjusted_return_series,
    compute_current_portfolio_position,
    compute_efficient_frontier,
    compute_min_variance_portfolio,
    compute_tangency_portfolio,
    derive_expected_returns_and_cov,
)
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioAnalysisBundle:
    """Pre-computed bundle for the Portfolio Analysis surface.

    Attributes:
        frontier_result: :class:`EfficientFrontierResult` covering
            every investment in the resolved universe.
        tangency: Tangency (max-Sharpe) portfolio.
        min_variance: Global minimum-variance portfolio.
        current_portfolio: ``(volatility, expected_return)`` of the
            current NAV-weighted portfolio. ``(nan, nan)`` when the
            tenant has no NAV history that produces a definable
            allocation (e.g. all-zero weights).
        current_weights: Normalised current allocation keyed by
            investment display name — weights sum to 1 over the
            frontier asset universe (every name in
            ``frontier_result.asset_names`` is present, zero-weight
            assets included). ``None`` when the current portfolio is
            undefined, i.e. exactly when ``current_portfolio`` is
            ``(nan, nan)`` (all-zero / unusable weights). Kept in
            lock-step with ``current_portfolio`` so the weights
            table's "Current" column appears precisely when the
            "Current Portfolio" summary card does.
        capital_market_line: CML geometry sampled past the tangency
            so the chart extends past every individual investment
            and the tangency marker.
        investment_points: Per-investment ``(volatility, expected_return)``
            for the individual-investment markers, keyed by display
            name.
        risk_free_rate: Annualised risk-free rate threaded through to
            the Sharpe-ratio computation. Echoed back so the template
            and chart can label the metric.
        as_of_date: Optional analysis as-of date — when set, NAV
            histories are truncated at this date before deriving
            returns. ``None`` means "use the full history".
        n_points_requested: Number of frontier samples requested.
            ``len(frontier_result.frontier_returns)`` may be smaller
            when the optimiser fails on extreme target returns.
    """

    frontier_result: EfficientFrontierResult
    tangency: TangencyPortfolio
    min_variance: MinVariancePortfolio
    current_portfolio: tuple[float, float]
    current_weights: dict[str, float] | None
    capital_market_line: CapitalMarketLine
    investment_points: dict[str, tuple[float, float]]
    risk_free_rate: float
    as_of_date: _date | None
    n_points_requested: int


class PortfolioAnalysisService:
    """Investment-universe Portfolio Analysis aggregator.

    Every repository must be tenant-scoped (the caller constructs
    them with a session obtained via
    :func:`core.repositories.tenant_context`). The service does not
    set or read ``app.tenant_id`` itself — that responsibility lives
    on the session.

    Per ADR-0102 this service sits on the ADR-0099 §4 conversion
    boundary: the NAV and cashflow series it assembles are converted
    from their position currencies into the tenant's functional
    currency *before* they reach the pure analytics layer, so the
    frontier optimises over one currency's returns. A single-currency
    tenant reads no FX row and its frontier is unchanged.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        cashflows: InvestmentCashflowRepository,
        tenants: TenantRepository,
        fx_rates: FxRateRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._cashflows = cashflows
        self._tenants = tenants
        self._fx_rates = fx_rates

    async def compute_frontier(
        self,
        *,
        n_points: int = 100,
        risk_free_rate: float = 0.025,
        as_of_date: _date | None = None,
        investment_ids: list[UUID] | None = None,
    ) -> PortfolioAnalysisBundle | None:
        """Build a :class:`PortfolioAnalysisBundle` for the active tenant.

        Steps:

        1. Resolve the investment universe — by default the active
           investments of the tenant; when ``investment_ids`` is
           supplied, restrict to that subset (intersection: ids that
           don't resolve under RLS are silently dropped). Cash
           positions are excluded from the universe in either case
           (ADR-0103 §8) — see :meth:`_resolve_universe`.
        2. Load each investment's actual NAV history. NAVs are
           optionally truncated to ``as_of_date``.
        3. Derive cash-flow-adjusted periodic returns via
           :func:`services.analytics.compute_cashflow_adjusted_return_series`,
           so capital calls and distributions are not mis-read as
           market return for drawdown vehicles (ADR-0066). Each
           investment's actual cashflows are loaded alongside its
           NAVs and truncated to the same ``as_of_date``.
        4. Drop investments whose return series is empty (fewer than
           two NAV observations).
        5. Restrict to the common observation window across the
           remaining investments. Investments that bind the start
           or end of the window are kept; investments that have no
           overlap with the rest of the universe are dropped via
           the dropna in :func:`restrict_to_common_window`.
        6. Annualise via :func:`derive_expected_returns_and_cov`.
        7. Run the analytics layer: efficient frontier, tangency,
           minimum-variance, capital-market-line, current portfolio.
        8. Compute per-investment ``(vol, return)`` for the
           individual-investment markers — each investment's
           annualised volatility is the square root of its diagonal
           entry in the annualised covariance matrix.

        Args:
            n_points: Number of frontier samples. Range 20–500;
                default 100. The QT widget defaults to 100.
            risk_free_rate: Annualised risk-free rate (decimal) used
                for tangency and CML. Defaults to ``0.025`` (2.5 %)
                — the QT widget's default. This is where cash enters
                the optimisation (ADR-0103 §8).
            as_of_date: Optional truncation date. NAVs and returns
                are restricted to entries on or before this date.
            investment_ids: Optional UUID filter. ``None`` means
                "all active, excluding cash". Cash ids listed here
                are filtered, not honoured (ADR-0103 §8).

        Returns:
            :class:`PortfolioAnalysisBundle` when a frontier could
            be computed (≥ 2 investments survive the common-window
            restriction). ``None`` when the universe is too small or
            has no overlapping observations — the route renders an
            empty-state template in that case.
        """
        investments = await self._resolve_universe(investment_ids)
        if len(investments) < 2:
            _LOG.debug(
                "PortfolioAnalysisService: universe too small (n=%d).",
                len(investments),
            )
            return None

        investments_sorted = sorted(investments, key=lambda i: i.name)

        nav_series_by_name: dict[str, pd.Series] = {}
        return_series_by_name: dict[str, pd.Series] = {}

        # P6-H batched fetch — one SQL ``SELECT ... WHERE
        # investment_id = ANY(:ids)`` replaces the per-investment loop
        # that previously triggered an N+1 against the NAV table.
        investment_ids = [inv.id for inv in investments_sorted]
        nav_rows_by_inv = await self._navs.list_by_investments_and_kind(investment_ids, "actual")
        cf_rows_by_inv = await self._cashflows.list_by_investments_and_kind(
            investment_ids, "actual"
        )

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
                index=pd.to_datetime([n.as_of_date for n in nav_rows]),
                dtype="float64",
            ).sort_index()
            if as_of_date is not None and not nav_series.empty:
                cutoff = pd.Timestamp(as_of_date)
                nav_series = nav_series[nav_series.index <= cutoff]
            # Convert point-in-time before the series feeds either the return
            # derivation or the current-weight NAV shares: a covariance built
            # from mixed-currency returns measures partly FX and partly
            # market, and NAV weights summed across currencies are nominal
            # nonsense. Both now read one currency.
            nav_series = fx.convert_series(nav_series, inv.currency)
            nav_series_by_name[inv.name] = nav_series

            # Flat actuals cashflow frame — same construction as
            # InvestmentService — truncated to the same as_of_date the
            # NAV series receives so the two surfaces share a window.
            cf_rows = cf_rows_by_inv.get(inv.id, [])
            cashflows_df = pd.DataFrame(
                {
                    "flow_timestamp": [c.flow_timestamp for c in cf_rows],
                    "flow_type": [c.flow_type for c in cf_rows],
                    "amount": [float(c.amount) for c in cf_rows],
                }
            )
            if as_of_date is not None and not cashflows_df.empty:
                cf_cutoff = pd.Timestamp(as_of_date, tz="UTC")
                flow_ts = pd.to_datetime(cashflows_df["flow_timestamp"], utc=True)
                cashflows_df = cashflows_df[flow_ts <= cf_cutoff]
            # The flows must live in the same currency as the NAVs they
            # adjust — ADR-0066's cashflow adjustment subtracts one from the
            # other, so a mixed pair would corrupt the return series.
            cashflows_df = self._convert_cashflow_frame(fx, cashflows_df, inv.currency)

            return_series_by_name[inv.name] = compute_cashflow_adjusted_return_series(
                nav_series, cashflows_df
            )

        # Drop investments without a usable return series before
        # building the alignment frame.
        usable = {
            name: series for name, series in return_series_by_name.items() if not series.empty
        }
        if len(usable) < 2:
            _LOG.debug(
                "PortfolioAnalysisService: fewer than 2 investments have a non-empty return series."
            )
            return None

        # Align on a DatetimeIndex so restrict_to_common_window
        # finds the binding edges.
        aligned = pd.DataFrame(usable)
        if not isinstance(aligned.index, pd.DatetimeIndex):
            aligned.index = pd.to_datetime(aligned.index)

        try:
            df_window, window_report = restrict_to_common_window(aligned)
        except ValueError as exc:
            _LOG.debug(
                "PortfolioAnalysisService: common-window restriction failed — %s",
                exc,
            )
            return None

        if window_report.n_rows_complete == 0 or df_window.shape[1] < 2:
            _LOG.debug(
                "PortfolioAnalysisService: no common observation window "
                "across the universe (rows=%d, cols=%d).",
                window_report.n_rows_complete,
                df_window.shape[1],
            )
            return None

        return_series_by_window = {col: df_window[col] for col in df_window.columns}

        expected_returns, cov_matrix = derive_expected_returns_and_cov(return_series_by_window)

        try:
            frontier = compute_efficient_frontier(
                expected_returns=expected_returns,
                cov_matrix=cov_matrix,
                n_points=n_points,
            )
        except ValueError as exc:
            _LOG.warning(
                "PortfolioAnalysisService: frontier computation failed: %s",
                exc,
            )
            return None

        try:
            tangency = compute_tangency_portfolio(frontier, risk_free_rate=risk_free_rate)
        except ValueError as exc:
            _LOG.warning(
                "PortfolioAnalysisService: tangency computation failed: %s",
                exc,
            )
            return None

        try:
            min_variance = compute_min_variance_portfolio(frontier)
        except ValueError as exc:
            _LOG.warning(
                "PortfolioAnalysisService: min-variance computation failed: %s",
                exc,
            )
            return None

        # Per-investment markers: each entry is the investment's own
        # (annualised vol, annualised expected return). Vol is the
        # square root of the covariance diagonal.
        per_asset_vol = np.sqrt(np.diag(cov_matrix.to_numpy(dtype=float)))
        investment_points: dict[str, tuple[float, float]] = {
            name: (float(per_asset_vol[i]), float(expected_returns.iloc[i]))
            for i, name in enumerate(expected_returns.index)
        }

        # Current portfolio: latest actual NAV per investment, share
        # of total NAV. Investments without a NAV in the window get
        # zero weight by absence.
        current_weights = self._latest_nav_weights(
            nav_series_by_name, allowed_names=set(expected_returns.index)
        )
        current_portfolio = compute_current_portfolio_position(
            current_weights, expected_returns, cov_matrix
        )
        # Normalised per-asset current allocation for the weights
        # table. Mirrors the normalisation in
        # compute_current_portfolio_position so this is ``None``
        # exactly when current_portfolio is ``(nan, nan)``.
        current_weights_normalised = self._normalise_current_weights(
            current_weights, asset_names=list(expected_returns.index)
        )

        # CML extension: stretch to whichever vol marker sits furthest
        # right so the chart shows a complete line through the
        # tangency point.
        max_individual_vol = float(per_asset_vol.max()) if per_asset_vol.size else 0.0
        max_frontier_vol = (
            float(frontier.frontier_volatilities.max())
            if frontier.frontier_volatilities.size
            else 0.0
        )
        x_max = max(
            1.5 * tangency.volatility,
            1.05 * max_individual_vol,
            1.05 * max_frontier_vol,
        )
        cml = compute_capital_market_line(
            risk_free_rate=risk_free_rate,
            tangency=tangency,
            x_max=x_max,
            n_points=50,
        )

        _LOG.debug(
            "PortfolioAnalysisService: bundle built (n=%d, n_points=%d, "
            "rf=%.4f, as_of=%s, window=%s..%s).",
            df_window.shape[1],
            n_points,
            risk_free_rate,
            as_of_date,
            window_report.window_start,
            window_report.window_end,
        )

        return PortfolioAnalysisBundle(
            frontier_result=frontier,
            tangency=tangency,
            min_variance=min_variance,
            current_portfolio=current_portfolio,
            current_weights=current_weights_normalised,
            capital_market_line=cml,
            investment_points=investment_points,
            risk_free_rate=risk_free_rate,
            as_of_date=as_of_date,
            n_points_requested=n_points,
        )

    async def _resolve_universe(self, investment_ids: list[UUID] | None) -> list[InvestmentDTO]:
        """Return the investments included in the analysis.

        Cash positions are excluded from the frontier universe
        (ADR-0103 §8). Cash is the risk-free anchor of the capital
        market line, and it enters the optimisation through the
        ``risk_free_rate`` parameter — never as a frontier asset, whose
        risk/return coordinates it would distort. The exclusion is
        pinned here, at the data-assembly seam, because the pure
        analytics layer must stay blind to investment types
        (ADR-0013 / ADR-0045): ``compute_efficient_frontier`` optimises
        over whatever matrix it is handed, and it is assembly's job to
        hand it the right one.

        The filter covers **both** resolution branches — a cash id
        passed explicitly in ``investment_ids`` is filtered too, since
        §8 admits no exception ("cash is never a frontier asset").

        ADR-0103 §8 extends the same rule to any future
        optimiser-adjacent assembly; the next one copies this seam
        rather than inventing a variant of it.

        Args:
            investment_ids: Optional UUID filter. ``None`` means "all
                active".

        Returns:
            The active, non-cash investments of the universe (ids that
            don't resolve under RLS are silently dropped).
        """
        all_active = await self._investments.list_active()
        if investment_ids is not None:
            wanted = set(investment_ids)
            all_active = [inv for inv in all_active if inv.id in wanted]
        return [inv for inv in all_active if inv.investment_type != "cash"]

    @staticmethod
    def _convert_cashflow_frame(
        fx: PortfolioFxConverter,
        cashflows_df: pd.DataFrame,
        from_currency: str,
    ) -> pd.DataFrame:
        """Convert a cashflow frame's ``amount`` column point-in-time.

        Each flow converts at the carry-forward rate of its own
        ``flow_timestamp`` (ADR-0099 §4). Row order and every other column
        (``flow_timestamp``, ``flow_type``) are untouched; only ``amount``
        is restated. An empty frame is returned as an unchanged copy.

        Args:
            fx: The request's converter into the functional currency.
            cashflows_df: Flat actuals frame with ``flow_timestamp``,
                ``flow_type`` and ``amount`` columns.
            from_currency: The investment's position currency.

        Returns:
            A new frame whose ``amount`` column is expressed in the
            functional currency.

        Raises:
            MissingFxRateError: If a flow date has no resolvable rate.
        """
        if cashflows_df.empty:
            return cashflows_df.copy()
        amounts = pd.Series(
            cashflows_df["amount"].to_numpy(dtype="float64"),
            index=pd.to_datetime(cashflows_df["flow_timestamp"]),
        )
        converted = fx.convert_series(amounts, from_currency)
        out = cashflows_df.copy()
        out["amount"] = converted.to_numpy(dtype="float64")
        return out

    @staticmethod
    def _latest_nav_weights(
        nav_series_by_name: dict[str, pd.Series],
        *,
        allowed_names: set[str],
    ) -> dict[str, float]:
        """Last-observation NAV share per investment.

        Mirrors
        :meth:`gui.widgets.portfolio_analysis_widget.PortfolioAnalysisWidget._compute_current_weights`:
        for each investment, take the last non-NaN NAV value as a
        proxy for the current allocation; the per-investment
        weights are the share of the total. Investments not in
        ``allowed_names`` (i.e. dropped by the common-window
        restriction) are excluded so the current-portfolio
        evaluation shares the same universe as the frontier.

        Args:
            nav_series_by_name: All NAV series available, indexed by
                investment name.
            allowed_names: Subset to include. Names absent from
                ``nav_series_by_name`` are silently skipped.

        Returns:
            Mapping of investment name to NAV-share weight. Weights
            are not normalised here — :func:`compute_current_portfolio_position`
            normalises before computing.
        """
        weights: dict[str, float] = {}
        for name in allowed_names:
            series = nav_series_by_name.get(name)
            if series is None or series.empty:
                continue
            cleaned = series.dropna()
            if cleaned.empty:
                continue
            weights[name] = float(cleaned.iloc[-1])
        return weights

    @staticmethod
    def _normalise_current_weights(
        weights: dict[str, float],
        *,
        asset_names: list[str],
    ) -> dict[str, float] | None:
        """Normalise the current allocation over the frontier universe.

        Mirrors the normalisation convention of
        :func:`compute_current_portfolio_position`: build the weight
        vector over ``asset_names`` (the frontier asset universe,
        names absent from ``weights`` get an implicit zero), then
        divide by the finite, non-zero total. The result therefore
        contains every name in ``asset_names`` and sums to 1.

        Args:
            weights: Raw, un-normalised NAV-share weights keyed by
                investment display name, as returned by
                :meth:`_latest_nav_weights`.
            asset_names: The frontier asset universe, in canonical
                order (``frontier_result.asset_names`` /
                ``expected_returns.index``).

        Returns:
            Normalised mapping of investment name to weight (sums to
            1), or ``None`` when the total is not finite or ``~0`` —
            the same guard that makes
            :func:`compute_current_portfolio_position` return
            ``(nan, nan)``.
        """
        vector = np.array(
            [float(weights.get(name, 0.0)) for name in asset_names],
            dtype=float,
        )
        total = float(np.sum(vector))
        if not np.isfinite(total) or abs(total) < 1e-12:
            return None
        return {name: float(vector[i] / total) for i, name in enumerate(asset_names)}
