# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Postgres-native back-office analysis tools for Shirley (web chat surface).

Read-only AI-callable tools that expose the **back-office analytics
domain** to the assistant — limit coverage, the SAA-hypothetical
comparison, and universe-wide portfolio statistics. Each tool is a
thin adapter over an *existing* service-layer entry point; this module
introduces **no new business logic and no new analytics** (ADR-0069):

- ``get_limit_coverage`` — present and historical Anlagegrenzen
  coverage (SAA + AnlV families) at the most-recent month-end
  Stichtag, via :class:`~services.limits.LimitsCoverageService`.
- ``get_saa_hypothetical_comparison`` — the SAA-vs-actual question
  (allocation / selection effects and cumulative endpoints), via
  :class:`~services.benchmark_comparison.BenchmarkComparisonService`.
  Additionally stores a chart-ready tidy envelope by handle so the
  board chart can be drawn through ``render_chart`` (ADR-0048).
- ``get_portfolio_statistics`` — per-investment annualised return /
  Sharpe / Sortino / max-drawdown plus the pairwise correlation
  matrix, via :class:`~services.statistics.StatisticsService`. One
  service call returns correlation *and* the risk metrics together.

All three register with :class:`~services.tool_classes.ToolClass`
``READ_INTERNAL`` at import time.

Scope boundary — no forward projection
--------------------------------------
``get_limit_coverage`` reports **only** the coverage the engine
already computes for present and past Stichtage. It performs **no
projection, no forecast, and no what-if overlay**: it answers "where
is headroom today / what is in breach now", not "what happens at
end-2030 if we add a €40m call". That forward capability has no engine
in the codebase and is a deliberate Non-Goal of ADR-0069 (a roadmap
item with its own ADR). The tool description makes this boundary
explicit so the model declines the forward question rather than
improvising a projection.

Pattern reuse (ADR-0047)
------------------------
Each tool mirrors ``investment_tools.py`` exactly: a synchronous
``Callable[..., str]`` that builds its async workflow as a coroutine
factory, runs it through
:func:`services.tools._async_bridge.run_async_in_fresh_loop`, opens a
short-lived loop-local session via the shared
:func:`services.tools.investment_tools._tool_session` context manager,
constructs the service from per-tenant repositories — mirroring the
web routes' ``_build_service`` DI — and reads under
``tenant_context``. When the tool-execution context is unset (the GUI
path, which imports this module but never populates the context), each
tool returns a clear explanatory string instead of raising. See
ADR-0047 and the :mod:`services.tools._tool_context` docstring for the
cross-loop hazard and the tenant seam.
"""

from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

import pandas as pd

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
)
from core.repositories import (
    AssetClassBenchmarkMappingRepository,
    AssetClassRepository,
    BenchmarkObservationRepository,
    BenchmarkRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRegionWeightsRepository,
    InvestmentRepository,
    InvestmentSectorWeightsRepository,
    LimitsRepository,
    RegionRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    SectorRepository,
    TenantRepository,
)
from services.benchmark_comparison import (
    BenchmarkComparisonService,
    SAAHypotheticalBundle,
)
from services.front_office_overview import (
    FrontOfficeOverviewService,
    OverviewKpis,
)
from services.limits import LimitsCoverageBundle, LimitsCoverageService
from services.portfolio_review.portfolio_review_service import (
    PortfolioReviewService,
)
from services.saa import SAAService
from services.saa.saa_service import SAAConfigurationDetailDTO
from services.statistics import StatisticsService, UniverseStatisticsBundle
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import (
    get_tool_context,
    store_tool_data,
)

# Reuse the loop-local session helper verbatim — it is module-level in
# investment_tools.py and importable; ADR-0069 directs reuse over
# duplication. Importing it triggers that module's tool registration
# once (import cache), which is harmless: the bootstrap imports both.
from services.tools.investment_tools import _tool_session

logger = logging.getLogger(__name__)

# Returned by every tool when the chat route has not populated the
# tool-execution context — the graceful-degradation path for the GUI,
# which imports this module but never sets the context (no FastAPI
# request). Mirrors ``investment_tools._CONTEXT_NOT_SET_MSG``.
_CONTEXT_NOT_SET_MSG = (
    "Back-office analysis data is not available in this context. (The "
    "tool-execution context was not set — these analysis tools read the "
    "persistent database and are only available from the web chat "
    "surface.)"
)

# Output cap per summary, mirroring the ~2000 character cap
# ``investment_tools.py`` applies to its detail blocks.
_DETAIL_CHAR_CAP = 2000

# Above this many investments the correlation block prints only the
# strongest off-diagonal pairs (a full matrix of N names has
# N·(N−1)/2 pairs, which overruns the char cap quickly).
_CORR_PAIR_NAME_CAP = 6
_CORR_PAIR_SHOW = 12

# Tidy-envelope discriminator for the SAA-hypothetical chart, matched
# by ``render_chart``'s allow-list (chart_tools.py).
_SAA_DATA_DISCRIMINATOR = "saa_hypothetical"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _to_float(value: object) -> float | None:
    """Coerce a Decimal / float / None cell to a finite float or ``None``."""
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pd.isna(result):
        return None
    return result


def _fmt_pct(value: object) -> str:
    """Format a percent-form value (e.g. ``67.23`` → ``67.23%``) or em-dash."""
    f = _to_float(value)
    return f"{f:.2f}%" if f is not None else "—"


def _fmt_pct_from_decimal(value: object) -> str:
    """Format a decimal fraction (e.g. ``0.137`` → ``13.70%``) or em-dash."""
    f = _to_float(value)
    return f"{f * 100.0:.2f}%" if f is not None else "—"


def _fmt_signed_pct_from_decimal(value: object) -> str:
    """Format a decimal fraction as a signed percentage, or em-dash."""
    f = _to_float(value)
    return f"{f * 100.0:+.2f}%" if f is not None else "—"


def _fmt_pp(value: object) -> str:
    """Format an already-percentage-point value as ``+/-N.NN pp`` or em-dash."""
    f = _to_float(value)
    return f"{f:+.2f} pp" if f is not None else "—"


def _fmt_eur(value: object) -> str:
    """Format a EUR amount with thousands separators, or em-dash."""
    f = _to_float(value)
    return f"{f:,.0f}" if f is not None else "—"


def _fmt_num(value: object) -> str:
    """Format a ratio (Sharpe / Sortino / correlation) to 2dp, or em-dash."""
    f = _to_float(value)
    return f"{f:.2f}" if f is not None else "—"


def _fmt_mult(value: object) -> str:
    """Format a multiple (TVPI / DPI) as ``N.NNx`` or em-dash."""
    f = _to_float(value)
    return f"{f:.2f}x" if f is not None else "—"


def _iso_date(value: object) -> str:
    """Render a pandas Timestamp / date index value as an ISO date string."""
    if hasattr(value, "date"):
        return value.date().isoformat()  # type: ignore[union-attr]
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    return str(value)


def _cap(text: str) -> str:
    """Truncate a summary to :data:`_DETAIL_CHAR_CAP`, mirroring investment_tools."""
    if len(text) > _DETAIL_CHAR_CAP:
        return text[: _DETAIL_CHAR_CAP - 30] + "\n...[truncated]"
    return text


# ---------------------------------------------------------------------------
# get_limit_coverage
# ---------------------------------------------------------------------------


def get_limit_coverage(
    from_date: str = "",
    to_date: str = "",
    cut_over: str = "",
) -> str:
    """Report present and historical limit coverage (SAA + AnlV families).

    Wraps :meth:`LimitsCoverageService.get_coverage`. Summarises the
    most-recent month-end Stichtag: the breach/warn/ok KPI strip and,
    per family, every constrained class's status, coverage percentage,
    cap, and headroom. It exposes **only** the coverage the engine
    already computes for present and past Stichtage — it performs no
    projection, no forecast, and no what-if overlay (ADR-0069).

    Args:
        from_date: Optional inclusive ISO ``YYYY-MM-DD`` lower bound of
            the evaluation range. Empty defaults to twelve months
            before ``to_date``.
        to_date: Optional inclusive ISO ``YYYY-MM-DD`` upper bound.
            Empty defaults to the book's NAV horizon.
        cut_over: Optional ISO ``YYYY-MM-DD`` plan/actual cut-over date.
            Empty defaults to today.

    Returns:
        A prose coverage summary, or a clear explanatory string when
        the context is unset, a date is malformed, the book carries no
        NAV, the range carries no month-end Stichtag, or the engine
        reports a missing/out-of-range input.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    parsed: dict[str, date | None] = {}
    for label, raw in (
        ("from_date", from_date),
        ("to_date", to_date),
        ("cut_over", cut_over),
    ):
        if not raw:
            parsed[label] = None
            continue
        try:
            parsed[label] = date.fromisoformat(raw)
        except ValueError:
            return (
                f"Invalid {label} '{raw}'. Use an ISO calendar date in "
                "YYYY-MM-DD form (e.g. 2025-12-31)."
            )

    async def _workflow() -> LimitsCoverageBundle | None:
        async with _tool_session(ctx) as db:
            service = LimitsCoverageService(
                investments=InvestmentRepository(db),
                navs=InvestmentNavRepository(db),
                limits=LimitsRepository(db),
                asset_classes=AssetClassRepository(db),
                tenants=TenantRepository(db),
                fx_rates=FxRateRepository(db),
            )
            return await service.get_coverage(
                from_date=parsed["from_date"],
                to_date=parsed["to_date"],
                cut_over=parsed["cut_over"],
            )

    try:
        bundle = run_async_in_fresh_loop(_workflow)
    except (
        LimitSetNotEffective,
        CoverageInputMissing,
        CoverageInputOutOfRange,
    ) as exc:
        return (
            "Limit coverage could not be evaluated: "
            f"{type(exc).__name__}: {exc}. Check that limit sets are "
            "effective and NAV inputs exist for the requested range."
        )

    if bundle is None:
        return (
            "No limit coverage is available — no investment in this tenant "
            "carries a NAV, so there is nothing to evaluate limits against. "
            "Import investments and their NAVs first."
        )

    if bundle.latest_as_of_date is None:
        return (
            "No month-end Stichtag falls in the evaluation range "
            f"({bundle.from_date} to {bundle.to_date}); there is no "
            "coverage to report. Widen the date range."
        )

    kpi = bundle.kpi_strip
    lines = [
        f"Limit coverage as of {bundle.latest_as_of_date} (present and "
        f"historical only — range {bundle.from_date} to {bundle.to_date}):",
        (
            f"  KPI strip — BREACH: {kpi.breach_count} | "
            f"WARN: {kpi.warn_count} | OK: {kpi.ok_total_count} of "
            f"{kpi.ok_classes_denominator} constrained classes."
        ),
    ]
    if kpi.aum_eur is not None:
        lines.append(f"  AUM denominator: {_fmt_eur(kpi.aum_eur)} EUR.")

    latest_ts = pd.Timestamp(bundle.latest_as_of_date)
    for family_label, family_result in (
        ("SAA", bundle.saa),
        ("AnlV", bundle.anlv),
    ):
        lines.append(f"{family_label} coverage:")
        df = family_result.coverage
        if df.empty:
            lines.append("  (no coverage rows)")
            continue
        slice_df = df[df["as_of_date"] == latest_ts]
        if slice_df.empty:
            lines.append("  (no coverage rows at the latest Stichtag)")
            continue
        rows = sorted(
            (row for _, row in slice_df.iterrows()),
            key=lambda r: str(r["class_key"]),
        )
        for row in rows:
            lines.append(
                f"  {row['class_key']}: {row['status']} | "
                f"coverage={_fmt_pct(row['coverage_pct'])} | "
                f"cap={_fmt_pct(row['max_pct'])} | "
                f"headroom={_fmt_eur(row['headroom_eur'])}"
            )

    return _cap("\n".join(lines))


# ---------------------------------------------------------------------------
# get_saa_hypothetical_comparison
# ---------------------------------------------------------------------------


def _resolve_configuration_name(bundle: SAAHypotheticalBundle) -> str | None:
    """Return the display name of the bundle's selected SAA configuration."""
    for option in bundle.saa_configuration_options:
        if option.saa_configuration_id == bundle.selected_configuration_id:
            return option.name
    return None


def _build_saa_chart_envelope(bundle: SAAHypotheticalBundle) -> str | None:
    """Build, cache, and return the handle for the SAA-hypothetical chart.

    Rebases each of the three monthly return series to a cumulative
    index ``(1 + r).cumprod()`` and lays them out in long form so a
    single ``render_chart`` call with ``series_column="series_name"``
    draws all three lines. Returns the opaque data handle, or ``None``
    when no series has any data to chart.
    """
    series = bundle.series
    if series is None:
        return None

    series_map = [
        ("SAA × Benchmark", series.saa_x_benchmark),
        ("SAA × Composite", series.saa_x_composite),
        ("Actual", series.actual_portfolio_returns),
    ]
    columns = ["as_of_date", "cumulative_index", "series_name"]
    rows: list[list] = []
    included: list[str] = []
    for series_name, monthly in series_map:
        if monthly is None or monthly.empty:
            continue
        cleaned = monthly.dropna().sort_index()
        if cleaned.empty:
            continue
        cumulative = (1.0 + cleaned).cumprod()
        for ts, value in cumulative.items():
            rows.append([_iso_date(ts), float(value), series_name])
        included.append(series_name)

    if not rows:
        return None

    envelope = {
        "__data__": _SAA_DATA_DISCRIMINATOR,
        "columns": columns,
        "rows": rows,
        "meta": {"unit": "index", "base": 1.0, "series": included},
    }
    return store_tool_data(envelope)


def get_saa_hypothetical_comparison(
    weight_set: str = "tangency",
    as_of_date: str = "",
) -> str:
    """Compare the actual book against the hypothetical SAA-weight portfolio.

    Wraps :meth:`BenchmarkComparisonService.get_saa_hypothetical`.
    Summarises the cumulative endpoints (Actual, SAA × Benchmark,
    SAA × Composite) and the allocation / selection effects in
    percentage points, naming the resolved SAA configuration. When a
    series is available it also caches a chart-ready tidy envelope and
    surfaces an opaque data handle the model can pass to
    ``render_chart`` (with ``series_column="series_name"``).

    The SAA configuration is resolved by the service's own preference
    order (active configuration, else the first); it is not selectable
    here.

    Args:
        weight_set: ``"tangency"`` (max-Sharpe) or ``"min_var"``
            (minimum variance). Defaults to ``"tangency"``.
        as_of_date: Optional ISO ``YYYY-MM-DD`` cut-off date. Empty
            defaults to today.

    Returns:
        A prose summary (plus a chart data handle when a series is
        available), or a clear explanatory string when the context is
        unset, an argument is malformed, the tenant has no SAA
        configuration, or the optimisation / period has no result.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    if weight_set not in ("tangency", "min_var"):
        return f"Invalid weight_set '{weight_set}'. Valid values are 'tangency' or 'min_var'."

    try:
        parsed_as_of = date.fromisoformat(as_of_date) if as_of_date else None
    except ValueError:
        return (
            f"Invalid as_of_date '{as_of_date}'. Use an ISO calendar date "
            "in YYYY-MM-DD form (e.g. 2025-12-31)."
        )

    async def _workflow() -> SAAHypotheticalBundle:
        async with _tool_session(ctx) as db:
            saa_service = SAAService(
                configurations=SAAConfigurationRepository(db),
                asset_classes=AssetClassRepository(db),
                inputs=SAAAssetClassInputRepository(db),
                correlations=SAACorrelationRepository(db),
            )
            service = BenchmarkComparisonService(
                investments=InvestmentRepository(db),
                navs=InvestmentNavRepository(db),
                asset_classes=AssetClassRepository(db),
                benchmarks=BenchmarkRepository(db),
                benchmark_observations=BenchmarkObservationRepository(db),
                mappings=AssetClassBenchmarkMappingRepository(db),
                saa_service=saa_service,
                tenants=TenantRepository(db),
                fx_rates=FxRateRepository(db),
            )
            return await service.get_saa_hypothetical(
                saa_configuration_id=None,
                weight_set=weight_set,  # type: ignore[arg-type]
                as_of_date=parsed_as_of,
            )

    bundle = run_async_in_fresh_loop(_workflow)
    config_name = _resolve_configuration_name(bundle)

    if bundle.effects is None or bundle.series is None:
        if bundle.selected_configuration_id is None:
            return (
                "No SAA-hypothetical comparison is available — the tenant "
                "has no SAA configuration to compare against. Create one in "
                "the Back Office SAA section first."
            )
        subject = config_name or "the selected SAA configuration"
        hint = next(
            (
                option.unavailable_hint
                for option in bundle.weight_set_options
                if option.unavailable_hint
            ),
            None,
        )
        message = (
            f"The SAA-hypothetical comparison for {subject} "
            f"({weight_set}) produced no result: the SAA optimisation "
            "could not run, or the period has no aligned data."
        )
        if hint:
            message += f" Reason: {hint}"
        return message

    effects = bundle.effects
    subject = config_name or "the selected SAA configuration"
    prose = [
        f"SAA-hypothetical comparison for {subject} (weight set: {weight_set}):",
        "  Cumulative returns since inception:",
        f"    Actual portfolio: {_fmt_signed_pct_from_decimal(effects.actual_cumulative_endpoint)}",
        f"    SAA × Benchmark: "
        f"{_fmt_signed_pct_from_decimal(effects.saa_x_benchmark_cumulative_endpoint)}",
        f"    SAA × Composite: "
        f"{_fmt_signed_pct_from_decimal(effects.saa_x_composite_cumulative_endpoint)}",
        f"  Allocation effect (Actual − SAA × Benchmark): {_fmt_pp(effects.allocation_effect_pp)}",
        f"  Selection effect (Actual − SAA × Composite): {_fmt_pp(effects.selection_effect_pp)}",
    ]

    summary = _cap("\n".join(prose))

    handle = _build_saa_chart_envelope(bundle)
    if handle is not None:
        summary += (
            f"\ndata_handle: {handle}"
            "\nPass this handle to render_chart's data_handle argument "
            'with series_column="series_name" (chart_type="line") to draw '
            "the board chart — a cumulative-index line per series "
            "(base 1.0)."
        )
    return summary


# ---------------------------------------------------------------------------
# get_portfolio_statistics
# ---------------------------------------------------------------------------


def _build_statistics_summary(
    bundle: UniverseStatisticsBundle,
    unknown_names: list[str],
) -> str:
    """Render the prose summary of a universe-statistics bundle."""
    names = bundle.investment_names
    if not names:
        message = "No portfolio statistics are available — no investments matched the request."
        if unknown_names:
            message += f" Unknown names dropped: {', '.join(unknown_names)}."
        return message

    lines = [f"Portfolio statistics (risk-free rate {bundle.risk_free_rate * 100.0:.2f}%):"]
    if unknown_names:
        lines.append(f"  Unknown names dropped: {', '.join(unknown_names)}.")

    for name in names:
        card = bundle.key_metrics.get(name)
        risk = bundle.risk_metrics.get(name)
        metrics: list[str] = []
        if card is not None:
            metrics.append(f"ann.return={_fmt_pct_from_decimal(card.annualised_return)}")
            metrics.append(f"Sharpe={_fmt_num(card.sharpe_ratio)}")
        if risk is not None:
            metrics.append(f"Sortino={_fmt_num(risk.sortino_ratio)}")
            metrics.append(f"maxDD={_fmt_pct_from_decimal(risk.max_drawdown)}")
        if metrics:
            lines.append(f"  {name}: " + ", ".join(metrics))
        else:
            lines.append(f"  {name}: (no return history)")

    corr = bundle.correlation_matrix
    if corr.empty or corr.shape[0] < 2:
        lines.append(
            "Correlation: not available (need at least two investments with return history)."
        )
    else:
        labels = list(corr.columns)
        pairs: list[tuple[str, str, float]] = []
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                coef = _to_float(corr.iloc[i, j])
                if coef is None:
                    continue
                pairs.append((str(labels[i]), str(labels[j]), coef))
        if len(labels) > _CORR_PAIR_NAME_CAP:
            pairs.sort(key=lambda p: abs(p[2]), reverse=True)
            shown = pairs[:_CORR_PAIR_SHOW]
            lines.append(
                f"Pairwise correlations (Pearson) — universe of "
                f"{len(labels)}; strongest {len(shown)} pairs by "
                "|correlation|:"
            )
        else:
            shown = pairs
            lines.append("Pairwise correlations (Pearson):")
        for first, second, coef in shown:
            lines.append(f"  {first} ↔ {second}: {coef:+.2f}")

    return _cap("\n".join(lines))


def get_portfolio_statistics(
    investment_names: list[str] | None = None,
    as_of_date: str = "",
    risk_free_rate: float = 0.0,
    active_only: bool = True,
) -> str:
    """Report per-investment risk metrics and the pairwise correlation matrix.

    Wraps :meth:`StatisticsService.get_universe_statistics` — one
    service call returning both the per-investment KPI cards
    (annualised return, Sharpe, Sortino, max drawdown) and the Pearson
    correlation matrix.

    Args:
        investment_names: Optional subset of exact investment names.
            Each is resolved to an id; unknown names are dropped and
            noted. Omit to cover the whole universe. When supplied,
            ``active_only`` has no effect (the named subset is used
            verbatim).
        as_of_date: Optional ISO ``YYYY-MM-DD`` truncation date — NAV
            histories are restricted to entries on or before it. Empty
            uses the full history.
        risk_free_rate: Annualised risk-free rate as a decimal (e.g.
            ``0.02`` for 2%) for the Sharpe / Sortino computation.
            Defaults to ``0.0``.
        active_only: When ``True`` (default), the whole-universe path
            includes only active investments. Ignored when
            ``investment_names`` is supplied.

    Returns:
        A prose summary of the per-investment metrics and pairwise
        correlations, or a clear explanatory string when the context
        is unset, the as-of date is malformed, or no investment
        matched.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    try:
        parsed_as_of = date.fromisoformat(as_of_date) if as_of_date else None
    except ValueError:
        return (
            f"Invalid as_of_date '{as_of_date}'. Use an ISO calendar date "
            "in YYYY-MM-DD form (e.g. 2025-12-31)."
        )

    requested = [n.strip() for n in investment_names] if investment_names is not None else None

    async def _workflow() -> tuple[UniverseStatisticsBundle, list[str]]:
        async with _tool_session(ctx) as db:
            investments = InvestmentRepository(db)
            resolved_ids = None
            unknown: list[str] = []
            if requested is not None:
                resolved_ids = []
                for name in requested:
                    dto = await investments.get_by_name(name)
                    if dto is None:
                        unknown.append(name)
                    else:
                        resolved_ids.append(dto.id)
            service = StatisticsService(
                investments=investments,
                navs=InvestmentNavRepository(db),
                tenants=TenantRepository(db),
                fx_rates=FxRateRepository(db),
            )
            bundle = await service.get_universe_statistics(
                investment_ids=resolved_ids,
                as_of_date=parsed_as_of,
                risk_free_rate=risk_free_rate,
                active_only=active_only,
            )
            return bundle, unknown

    bundle, unknown_names = run_async_in_fresh_loop(_workflow)
    return _build_statistics_summary(bundle, unknown_names)


# ---------------------------------------------------------------------------
# get_portfolio_overview
# ---------------------------------------------------------------------------


def get_portfolio_overview(as_of_date: str = "") -> str:
    """Report the portfolio's headline KPI strip at a date.

    Wraps :meth:`FrontOfficeOverviewService.get_overview_kpis`. Summarises
    the Front-Office Overview hero figures: AUM, invested capital, cash,
    IRR, TVPI, DPI, and the active investment count, all at the resolved
    as-of date. This is the portfolio-wide headline — not a single
    investment's figures (use ``get_investment_data``) and not the detailed
    investor-communication review pack (a separate, forthcoming tool).

    Args:
        as_of_date: Optional ISO ``YYYY-MM-DD`` as-of date. Empty resolves
            to the latest activity date observed across the universe (the
            service's own default), so the AUM hero and the multiples share
            one as-of date.

    Returns:
        A prose KPI summary, or a clear explanatory string when the context
        is unset, the date is malformed, or the tenant has no investment
        data.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    try:
        parsed_as_of = date.fromisoformat(as_of_date) if as_of_date else None
    except ValueError:
        return (
            f"Invalid as_of_date '{as_of_date}'. Use an ISO calendar date "
            "in YYYY-MM-DD form (e.g. 2025-12-31)."
        )

    async def _workflow() -> OverviewKpis | None:
        async with _tool_session(ctx) as db:
            review_service = PortfolioReviewService(
                investments=InvestmentRepository(db),
                navs=InvestmentNavRepository(db),
                cashflows=InvestmentCashflowRepository(db),
                region_weights=InvestmentRegionWeightsRepository(db),
                sector_weights=InvestmentSectorWeightsRepository(db),
                regions=RegionRepository(db),
                sectors=SectorRepository(db),
                tenants=TenantRepository(db),
                fx_rates=FxRateRepository(db),
            )
            service = FrontOfficeOverviewService(
                review_service,
                investment_repository=InvestmentRepository(db),
                nav_repository=InvestmentNavRepository(db),
                tenant_repository=TenantRepository(db),
                fx_rate_repository=FxRateRepository(db),
            )
            return await service.get_overview_kpis(as_of_date=parsed_as_of)

    kpis = run_async_in_fresh_loop(_workflow)
    if kpis is None:
        return (
            "No portfolio overview is available — the investment universe "
            "is empty for this tenant. Import investments first."
        )

    lines = [
        f"Portfolio overview as of {kpis.as_of_date.isoformat()}:",
        f"  AUM: {_fmt_eur(kpis.aum_eur)} EUR",
        f"  Invested capital: {_fmt_eur(kpis.invested_eur)} EUR",
        f"  Cash (explicit cash positions): {_fmt_eur(kpis.cash_eur)} EUR",
        f"  IRR since inception: {_fmt_pct_from_decimal(kpis.irr)}",
        f"  TVPI: {_fmt_mult(kpis.tvpi)}",
        f"  DPI: {_fmt_mult(kpis.dpi)}",
        f"  Active investments: {kpis.investment_count}",
    ]
    return _cap("\n".join(lines))


# ---------------------------------------------------------------------------
# get_saa_configuration
# ---------------------------------------------------------------------------


def _build_saa_configuration_summary(
    detail: SAAConfigurationDetailDTO,
    id_to_name: dict[UUID, str],
) -> str:
    """Render the prose summary of an SAA configuration's assumptions.

    Asset-class ids are resolved to human display names via ``id_to_name``;
    raw UUIDs are never printed. When the correlation universe is large the
    matrix is summarised to its strongest pairs rather than dumped in full.
    """
    config = detail.configuration
    active = " (active)" if config.is_active else ""
    lines = [
        f"SAA configuration '{config.name}'{active} — risk-free rate "
        f"{_fmt_pct_from_decimal(config.risk_free_rate)}:",
    ]

    if not detail.inputs:
        lines.append("  Per-asset-class inputs: (none configured)")
    else:
        lines.append(
            "  Per-asset-class assumptions (expected return / volatility / weight bounds):"
        )
        rows = sorted(
            detail.inputs,
            key=lambda r: id_to_name.get(r.asset_class_id, str(r.asset_class_id)),
        )
        for row in rows:
            name = id_to_name.get(row.asset_class_id, "(unknown asset class)")
            lines.append(
                f"    {name}: "
                f"E[r]={_fmt_pct_from_decimal(row.expected_return)}, "
                f"vol={_fmt_pct_from_decimal(row.volatility)}, "
                f"weight {_fmt_pct_from_decimal(row.min_weight)}–"
                f"{_fmt_pct_from_decimal(row.max_weight)}"
            )

    named: list[tuple[str, str, float]] = []
    for row in detail.correlations:
        coef = _to_float(row.correlation)
        if coef is None:
            continue
        first = id_to_name.get(row.asset_class_a_id, "(unknown asset class)")
        second = id_to_name.get(row.asset_class_b_id, "(unknown asset class)")
        named.append((first, second, coef))

    if not named:
        lines.append("  Correlations: (none configured)")
    elif len(named) > _CORR_PAIR_SHOW:
        named.sort(key=lambda p: abs(p[2]), reverse=True)
        shown = named[:_CORR_PAIR_SHOW]
        lines.append(
            f"  Correlations — {len(named)} pairs; strongest {len(shown)} by |correlation|:"
        )
        for first, second, coef in shown:
            lines.append(f"    {first} ↔ {second}: {coef:+.2f}")
    else:
        lines.append("  Correlations:")
        for first, second, coef in named:
            lines.append(f"    {first} ↔ {second}: {coef:+.2f}")

    return _cap("\n".join(lines))


def get_saa_configuration(configuration_name: str = "") -> str:
    """Report an SAA configuration's assumptions (inputs and correlations).

    Wraps :meth:`SAAService.get_configuration_full`. Resolves the target
    configuration — by exact (case-insensitive) name when
    ``configuration_name`` is given, otherwise the tenant's active
    configuration — and summarises its assumptions: the risk-free rate, and
    per asset class the expected return, volatility, and weight bounds, plus
    the pairwise correlations. Asset classes are named, not shown as raw ids.

    This reports the SAA *inputs*, not how the SAA would have *performed*
    against the actual book — for that allocation / selection comparison use
    ``get_saa_hypothetical_comparison``.

    Args:
        configuration_name: Optional exact name of the SAA configuration to
            read. Empty resolves the tenant's active configuration.

    Returns:
        A prose summary of the configuration's assumptions, or a clear
        explanatory string when the context is unset or no configuration
        resolves (with the available names listed).
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    wanted = configuration_name.strip()

    async def _workflow() -> tuple[SAAConfigurationDetailDTO | None, dict[UUID, str], list[str]]:
        async with _tool_session(ctx) as db:
            service = SAAService(
                configurations=SAAConfigurationRepository(db),
                asset_classes=AssetClassRepository(db),
                inputs=SAAAssetClassInputRepository(db),
                correlations=SAACorrelationRepository(db),
            )
            configurations = await service.list_configurations()
            available = [c.name for c in configurations]

            target_id: UUID | None = None
            if wanted:
                folded = wanted.casefold()
                match = next(
                    (c for c in configurations if c.name.casefold() == folded),
                    None,
                )
                if match is not None:
                    target_id = match.id
            else:
                active = await service.get_active_configuration()
                if active is not None:
                    target_id = active.id

            if target_id is None:
                return None, {}, available

            detail = await service.get_configuration_full(target_id)
            asset_classes = await service.list_asset_classes()
            id_to_name = {ac.id: ac.display_name for ac in asset_classes}
            return detail, id_to_name, available

    detail, id_to_name, available = run_async_in_fresh_loop(_workflow)

    if detail is None:
        if not available:
            return (
                "No SAA configurations exist for this tenant yet. Create one "
                "in the Back Office SAA section first."
            )
        if wanted:
            return (
                f"No SAA configuration named '{configuration_name}'. "
                f"Available configurations: {', '.join(available)}."
            )
        return (
            "No active SAA configuration is set for this tenant. Available "
            f"configurations: {', '.join(available)}. Pass configuration_name "
            "to select one."
        )

    return _build_saa_configuration_summary(detail, id_to_name)


# ---------------------------------------------------------------------------
# Register tools at import time
# ---------------------------------------------------------------------------

_registry = get_tool_registry()

_registry.register_tool(
    name="get_limit_coverage",
    function=get_limit_coverage,
    description=(
        "Report investment-limit coverage (Anlagegrenzen) for the active "
        "tenant at the most recent month-end Stichtag: the breach/warn/ok "
        "KPI strip plus, per SAA and AnlV family, each constrained asset "
        "class's status (OK/WARN/BREACH), coverage percentage, ceiling "
        "(cap), and headroom in EUR. IMPORTANT: this tool reports PRESENT "
        "and HISTORICAL coverage ONLY — it does NOT project, forecast, or "
        "model future capital calls or exposures, and it has no what-if "
        "overlay. Use it for 'where is headroom today' / 'what is in "
        "breach now'. It cannot answer forward questions such as 'against "
        "our end-2030 limits, does a future €40m call tip anything into "
        "breach' — there is no projection engine; say so plainly rather "
        "than improvising. Optional from_date/to_date bound the historical "
        "evaluation range; cut_over sets the plan/actual cut-over date."
    ),
    parameters={
        "type": "object",
        "properties": {
            "from_date": {
                "type": "string",
                "description": (
                    "Optional inclusive ISO YYYY-MM-DD lower bound of the "
                    "evaluation range. Omit to default to 12 months before "
                    "to_date."
                ),
            },
            "to_date": {
                "type": "string",
                "description": (
                    "Optional inclusive ISO YYYY-MM-DD upper bound. Omit to "
                    "default to the latest NAV date in the book."
                ),
            },
            "cut_over": {
                "type": "string",
                "description": (
                    "Optional ISO YYYY-MM-DD plan/actual cut-over date. Omit to default to today."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_saa_hypothetical_comparison",
    function=get_saa_hypothetical_comparison,
    description=(
        "Compare the actual portfolio against the hypothetical portfolio "
        "that simply held the Strategic Asset Allocation (SAA) weights. "
        "Returns the cumulative returns of three series (Actual, "
        "SAA × Benchmark, SAA × Composite) and the allocation and "
        "selection effects in percentage points — the answer to 'would we "
        "have done just as well passively holding the SAA weights'. The "
        "SAA configuration is resolved automatically (active, else first); "
        "choose the weight_set ('tangency' for max-Sharpe or 'min_var' for "
        "minimum variance). When a series is available the tool also "
        "returns an opaque data_handle: pass it to render_chart with "
        "series_column='series_name' to draw the three-line board chart. "
        "NOT for the underlying SAA assumptions / inputs themselves "
        "(expected returns, volatilities, weight bounds, correlations) — "
        "for those use get_saa_configuration."
    ),
    parameters={
        "type": "object",
        "properties": {
            "weight_set": {
                "type": "string",
                "enum": ["tangency", "min_var"],
                "description": (
                    "Which optimised SAA weight set to use: 'tangency' "
                    "(maximum Sharpe) or 'min_var' (minimum variance). "
                    "Defaults to 'tangency'."
                ),
            },
            "as_of_date": {
                "type": "string",
                "description": ("Optional ISO YYYY-MM-DD cut-off date. Omit to use today."),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_portfolio_statistics",
    function=get_portfolio_statistics,
    description=(
        "Report universe-wide portfolio statistics for the active tenant: "
        "per-investment annualised return, Sharpe ratio, Sortino ratio, "
        "and maximum drawdown, plus the pairwise Pearson correlation "
        "matrix among the investments. One call returns both the "
        "risk-adjusted return metrics and the correlations — use it for "
        "questions like 'is this fund earning its fee or am I paying "
        "active fees for beta'. Optionally restrict to a subset via "
        "investment_names (exact names; unknown names are dropped and "
        "noted), truncate at as_of_date, and set the annualised "
        "risk_free_rate (decimal) used for Sharpe/Sortino."
    ),
    parameters={
        "type": "object",
        "properties": {
            "investment_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of exact investment names. Each is "
                    "matched against the catalogue; unknown names are "
                    "dropped and reported. Omit to cover the whole "
                    "universe."
                ),
            },
            "as_of_date": {
                "type": "string",
                "description": (
                    "Optional ISO YYYY-MM-DD truncation date; NAV history "
                    "is restricted to on-or-before it. Omit for the full "
                    "history."
                ),
            },
            "risk_free_rate": {
                "type": "number",
                "description": (
                    "Annualised risk-free rate as a decimal (e.g. 0.02 for "
                    "2%) for the Sharpe and Sortino ratios. Defaults to "
                    "0.0."
                ),
            },
            "active_only": {
                "type": "boolean",
                "description": (
                    "When true (default), the whole-universe path includes "
                    "only active investments. Ignored when investment_names "
                    "is supplied."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_portfolio_overview",
    function=get_portfolio_overview,
    description=(
        "Report the portfolio's headline KPI strip for the active tenant at "
        "a date: total AUM, invested capital, cash, IRR since inception, "
        "TVPI, DPI, and the active investment count. Use it for 'how big is "
        "the portfolio' / 'total AUM and IRR' / 'how are we doing overall' "
        "questions. NOT for a single investment's figures — for one "
        "investment use get_investment_data. NOT for the detailed "
        "investor-communication review pack (cashflows, vintage, "
        "region/sector breakdowns, total-return index) — that is a separate, "
        "forthcoming tool. Optional as_of_date sets the valuation date; omit "
        "it for the latest available."
    ),
    parameters={
        "type": "object",
        "properties": {
            "as_of_date": {
                "type": "string",
                "description": (
                    "Optional ISO YYYY-MM-DD as-of date for the figures. "
                    "Omit to use the latest activity date across the "
                    "universe."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_saa_configuration",
    function=get_saa_configuration,
    description=(
        "Report the Strategic Asset Allocation (SAA) assumptions / inputs of "
        "a configuration for the active tenant: the risk-free rate, and per "
        "asset class the expected return, volatility, and min/max weight "
        "bounds, plus the pairwise correlation matrix (asset classes are "
        "named, never raw ids). Use it for 'what does our SAA assume for "
        "expected returns / correlations / constraints' questions. NOT for "
        "how the SAA would have PERFORMED versus the actual book — for that "
        "allocation / selection comparison use "
        "get_saa_hypothetical_comparison. Optional configuration_name "
        "selects a configuration by exact name; omit it for the tenant's "
        "active configuration."
    ),
    parameters={
        "type": "object",
        "properties": {
            "configuration_name": {
                "type": "string",
                "description": (
                    "Optional exact (case-insensitive) name of the SAA "
                    "configuration to read. Omit to use the tenant's active "
                    "configuration."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)
