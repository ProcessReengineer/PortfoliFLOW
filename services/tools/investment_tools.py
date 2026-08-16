# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Postgres-native investment tools for Shirley (the web chat surface).

Read-only AI-callable tools that query the **persistent** investment
database (the Postgres tables ``investments``, ``investment_navs``,
``investment_cashflows``) through the repository layer — exactly the
path every FastAPI route takes:

- ``list_investments`` — every investment in the active tenant.
- ``get_investment_detail`` — one investment resolved by name, with a
  NAV / cashflow summary.
- ``get_investment_nav_history`` — the NAV time series for one named
  investment, optionally filtered to a single ``nav_kind``.
- ``get_investment_data`` — the **structured-data** access tool: the
  same domain entities as the prose tools above, but returned as a
  machine-readable envelope the ``render_chart`` tool consumes. See
  the *Structured-data envelope contract* section below.

All are registered with :class:`~services.tool_classes.ToolClass`
``READ_INTERNAL`` at import time.

Structured-data envelope contract (Axis 1 → Axis 2)
---------------------------------------------------
``get_investment_data`` is the data-acquisition half of the two-axis
chart architecture (ADR-0048). It builds a structured-data envelope —
the **stable contract** between the data-access tools and the generic
``render_chart`` rendering tool — shaped as tidy columnar data::

    {
      "__data__": "investment_data",   # discriminator render_chart validates
      "bundle": "nav_series",          # which of the five bundles this is
      "investment_name": "Alpha Fund", # null for tenant-wide bundles
      "columns": ["as_of_date", "nav_value", "nav_kind"],
      "rows": [["2021-12-31", 3000000.0, "actual"], ...],
      "meta": {"currency": "EUR", "row_count": 412, "truncated": false}
    }

``columns`` and ``rows`` are kept separate (tidy form) so the same
envelope feeds a line chart or a bar chart unchanged; ``meta`` carries
units / context the chart tool puts onto axis labels. The five
bundles are ``catalogue``, ``nav_series``, ``cashflow_series``,
``return_metrics``, and ``portfolio_nav_series`` (long-form
multi-investment NAVs, charted via ``series_column="investment_name"``
on ``render_chart``). Rows are capped defensively at
:data:`_DATA_ROW_CAP`; when the cap bites ``meta["truncated"]`` is
``true``. This contract is also the seam the deferred Stage-2
matplotlib-code render path will consume.

The envelope travels by handle, not by value (ADR-0048, amended).
``get_investment_data`` does **not** return the envelope to the model:
a tool call's arguments are model-generated output, so handing the
rows back would force the model to re-emit every one of them
token-by-token as the ``render_chart`` argument. Instead the envelope
is stored server-side via
:func:`~services.tools._tool_context.store_tool_data`, and the tool
returns a compact **summary string** carrying an opaque *data handle*
plus the shape facts (row count, column names, date span, units) the
model needs to compose a ``render_chart`` call. ``render_chart`` takes
the handle and resolves the envelope from the cache. The model decides
*which* data and *how* to chart it; it never transports the data. The
:data:`_DATA_ROW_CAP` is therefore now only a memory guard — the rows
no longer pass through the model's context.

Relationship to ``datastore_tools.py``
--------------------------------------
The four ``datastore_tools.py`` tools (``list_datasets`` &c.) read the
in-memory ``DataStore`` singleton, which the PyQt6 GUI populates on
Excel import but the web variant does not (ADR-0041). These tools are
the web-side counterpart: they read what the web Excel-import path
actually wrote — Postgres. Both families coexist in the registry
during the strangler period; neither replaces the other here.

How the async repository layer is reached from a sync tool
----------------------------------------------------------
The :class:`~services.tool_registry.ToolRegistry` contract is
``Callable[..., str]`` and tools run synchronously inside the live
event loop driving ``stream_response``. The repository layer is
async. Each tool therefore builds its workflow as a coroutine factory
and runs it through
:func:`services.tools._async_bridge.run_async_in_fresh_loop` — the
same fresh-loop-on-a-thread pattern ADR-0038 established for
``send_one_shot_extraction``.

Because that workflow runs on a *fresh* event loop, it cannot reuse
the application's ``AsyncEngine`` — its asyncpg connections are bound
to the uvicorn loop, and crossing that boundary raises
``RuntimeError: ... got Future ... attached to a different loop``.
Each workflow instead builds its own short-lived, loop-local engine
from the connection URL via :func:`_tool_session`, and disposes it
when the workflow ends. See ADR-0047 (amended) and the
:mod:`services.tools._tool_context` docstring for the cross-loop
hazard.

The request's tenant id and database URL arrive via the module-level
:class:`~services.tools._tool_context.ToolExecutionContext`, populated
by the chat route per turn. When that context is unset — the GUI
imports this module but never populates it — the tools return a clear
explanatory string rather than raising. See ADR-0047.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories import (
    InvestmentCashflowRepository,
    InvestmentDTO,
    InvestmentNavDTO,
    InvestmentNavRepository,
    InvestmentRepository,
    create_engine_from_url,
    tenant_context,
)
from services.investments.investment_service import (
    InvestmentChartsBundle,
    InvestmentDetailDTO,
    InvestmentService,
)
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import (
    ToolExecutionContext,
    get_tool_context,
    store_tool_data,
)

logger = logging.getLogger(__name__)

# Returned by every tool when the chat route has not populated the
# tool-execution context — the graceful-degradation path for the GUI,
# which imports this module but never sets the context (no FastAPI
# request). A clean explanatory return reads better to the model than
# a raised exception caught by ``execute_tool``.
_CONTEXT_NOT_SET_MSG = (
    "Investment data is not available in this context. (The tool-execution "
    "context was not set — these investment tools read the persistent "
    "database and are only available from the web chat surface.)"
)

# Output cap for the per-investment detail block, mirroring the ~2000
# character cap ``datastore_tools.py`` applies to its summaries.
_DETAIL_CHAR_CAP = 2000

# Row cap for the NAV history table, mirroring ``get_dataset_slice``'s
# 50-row head/tail split in ``datastore_tools.py``.
_NAV_ROW_CAP = 50

# Defensive row cap for the structured-data envelope. Since the
# envelope travels by handle (ADR-0048, amended) and never passes
# through the model's context, this is purely a memory guard, not a
# token-budget concern. 200 000 rows covers realistic
# institutional portfolios with margin — 50 investments × 10 years of
# daily NAVs is ~130 k rows; 200 investments × 15 years of monthly
# NAVs is ~36 k rows. Above 200 k is overwhelmingly likely to be a
# data-import error or unresampled raw tick data, not a legitimate use
# case, and the cap exists to keep a pathological dataset from growing
# the cached envelope without bound. When the cap bites the envelope's
# ``meta`` block carries ``"truncated": true`` plus
# ``"row_count_uncapped": <int>`` recording the pre-cap count.
_DATA_ROW_CAP = 200000

# The five semantic data bundles ``get_investment_data`` can return.
_VALID_BUNDLES = frozenset(
    {
        "catalogue",
        "nav_series",
        "cashflow_series",
        "return_metrics",
        "portfolio_nav_series",
    }
)

# Reused by the three per-investment bundles when name resolution fails.
_UNKNOWN_INVESTMENT_MSG = (
    "No investment named '{name}' exists in the current data. Use "
    "list_investments to see the available names."
)


def _fmt_decimal(value: Decimal | None) -> str:
    """Format an optional Decimal as a thousands-separated string or an em-dash."""
    if value is None:
        return "—"
    return f"{value:,.2f}"


@asynccontextmanager
async def _tool_session(
    ctx: ToolExecutionContext,
) -> AsyncIterator[AsyncSession]:
    """Yield a tenant-scoped session backed by a loop-local engine.

    Constructs a short-lived ``AsyncEngine`` from ``ctx.database_url``
    *inside the caller's event loop* — the fresh loop
    :func:`~services.tools._async_bridge.run_async_in_fresh_loop`
    provides — so no loop-bound object crosses a thread boundary, and
    every asyncpg connection it pools is born and dies on that one
    loop. The engine is disposed when the context exits.

    A fresh engine per tool call is not free — it opens a new pool,
    runs the asyncpg connection handshake, and tears it down — but at
    Shirley's human-paced call volume the cost is negligible, and it
    is the same tradeoff :func:`web.main._read_schema_revision`
    already accepts. A per-thread / per-loop engine cache is a
    possible future optimisation, deliberately not built now. See
    ADR-0047 (amended).

    Args:
        ctx: The per-turn tool-execution context carrying the tenant
            id and the database connection URL.

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` scoped to
        ``ctx.tenant_id`` via
        :func:`core.repositories.tenant_context`.
    """
    engine = create_engine_from_url(ctx.database_url)
    try:
        async with tenant_context(engine, ctx.tenant_id) as db:
            yield db
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# list_investments
# ---------------------------------------------------------------------------


def list_investments() -> str:
    """List every investment in the active tenant's persistent database.

    Returns:
        A human-readable, one-line-per-investment listing with a count
        header. An explanatory string when the tool-execution context
        is unset, or when the database holds no investments.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    async def _workflow() -> list:
        async with _tool_session(ctx) as db:
            service = InvestmentService(
                InvestmentRepository(db),
                InvestmentNavRepository(db),
                InvestmentCashflowRepository(db),
            )
            return await service.list_investments()

    investments = run_async_in_fresh_loop(_workflow)

    if not investments:
        return (
            "No investments are present in the persistent investment "
            "database. Import portfolio data via the Front Office Data "
            "Import section first."
        )

    lines = [f"{len(investments)} investment(s) in the persistent database:"]
    for inv in investments:
        vintage = str(inv.vintage_year) if inv.vintage_year is not None else "—"
        suffix = "" if inv.is_active else " (inactive)"
        lines.append(
            f"- {inv.name}{suffix} | type={inv.investment_type} | "
            f"manager={inv.manager_name or '—'} | currency={inv.currency} | "
            f"vintage={vintage} | "
            f"commitment={_fmt_decimal(inv.commitment_amount)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_investment_detail
# ---------------------------------------------------------------------------


def get_investment_detail(investment_name: str) -> str:
    """Return catalogue fields plus a NAV / cashflow summary for one investment.

    Args:
        investment_name: Exact name of the investment, matched against
            the per-tenant catalogue. Use :func:`list_investments` to
            discover valid names.

    Returns:
        A header block with the investment's catalogue fields followed
        by a NAV summary and a cashflow summary. An explanatory string
        when the context is unset or no investment with this name
        exists.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    async def _workflow() -> InvestmentDetailDTO | None:
        async with _tool_session(ctx) as db:
            investments = InvestmentRepository(db)
            dto = await investments.get_by_name(investment_name)
            if dto is None:
                return None
            service = InvestmentService(
                investments,
                InvestmentNavRepository(db),
                InvestmentCashflowRepository(db),
            )
            return await service.get_investment_detail(dto.id)

    detail = run_async_in_fresh_loop(_workflow)

    if detail is None:
        return (
            f"No investment named '{investment_name}' exists in the current "
            "data. Use list_investments to see the available names."
        )

    inv = detail.investment
    vintage = str(inv.vintage_year) if inv.vintage_year is not None else "—"
    lines = [
        f"Investment: {inv.name}",
        f"  Type: {inv.investment_type}",
        f"  Manager: {inv.manager_name or '—'}",
        f"  Currency: {inv.currency}",
        f"  Vintage year: {vintage}",
        f"  Commitment: {_fmt_decimal(inv.commitment_amount)}",
        f"  Region: {inv.region or '—'}",
        f"  Active: {'yes' if inv.is_active else 'no'}",
    ]

    # NAV summary — count, date range, latest actual value.
    navs = detail.navs
    if navs:
        nav_dates = [n.as_of_date for n in navs]
        lines.append(f"  NAVs: {len(navs)} row(s), {min(nav_dates)} to {max(nav_dates)}")
        actual_navs = [n for n in navs if n.nav_kind == "actual"]
        if actual_navs:
            latest = max(actual_navs, key=lambda n: n.as_of_date)
            lines.append(
                f"    Latest actual NAV: {_fmt_decimal(latest.nav_value)} "
                f"{latest.currency} on {latest.as_of_date}"
            )
        else:
            lines.append("    No actual NAVs yet (plan only).")
    else:
        lines.append("  NAVs: none recorded")

    # Cashflow summary — count, date range, signed actual totals.
    cashflows = detail.cashflows
    if cashflows:
        cf_dates = [c.flow_timestamp.date() for c in cashflows]
        actual = [c for c in cashflows if c.flow_kind == "actual"]
        inflows = sum((c.amount for c in actual if c.amount > 0), Decimal("0"))
        outflows = sum((c.amount for c in actual if c.amount < 0), Decimal("0"))
        lines.append(f"  Cashflows: {len(cashflows)} row(s), {min(cf_dates)} to {max(cf_dates)}")
        lines.append(
            f"    Actual inflows: {_fmt_decimal(inflows)} | "
            f"actual outflows: {_fmt_decimal(outflows)}"
        )
    else:
        lines.append("  Cashflows: none recorded")

    result = "\n".join(lines)
    if len(result) > _DETAIL_CHAR_CAP:
        result = result[: _DETAIL_CHAR_CAP - 30] + "\n...[truncated]"
    return result


# ---------------------------------------------------------------------------
# get_investment_nav_history
# ---------------------------------------------------------------------------


def _format_nav_row(nav: InvestmentNavDTO) -> str:
    """Render one NAV row as a fixed-width table line."""
    return f"  {nav.as_of_date}  {nav.nav_kind:<7}  {nav.nav_value:>18,.2f}  {nav.currency}"


def get_investment_nav_history(investment_name: str, nav_kind: str | None = None) -> str:
    """Return the NAV time series for one named investment as a compact table.

    Args:
        investment_name: Exact name of the investment, matched against
            the per-tenant catalogue. Use :func:`list_investments` to
            discover valid names.
        nav_kind: Optional filter — ``"actual"`` or ``"plan"``. When
            omitted, both kinds are returned.

    Returns:
        A date-sorted table of ``as_of_date``, ``nav_kind``,
        ``nav_value``, ``currency`` with a count header. Long series
        are capped at 50 rows (first 25 + last 25 with an omission
        note). An explanatory string when the context is unset, the
        investment is unknown, or ``nav_kind`` is invalid.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    if nav_kind is not None and nav_kind not in ("actual", "plan"):
        return (
            f"Invalid nav_kind '{nav_kind}'. Valid values are 'actual' or "
            "'plan'. Omit the argument to return both kinds."
        )

    async def _workflow() -> tuple[str, list[InvestmentNavDTO]] | None:
        async with _tool_session(ctx) as db:
            investments = InvestmentRepository(db)
            dto = await investments.get_by_name(investment_name)
            if dto is None:
                return None
            navs = InvestmentNavRepository(db)
            if nav_kind is None:
                rows = await navs.list_by_investment(dto.id)
            else:
                rows = await navs.list_by_investment_and_kind(dto.id, nav_kind)
            return dto.name, rows

    resolved = run_async_in_fresh_loop(_workflow)

    if resolved is None:
        return (
            f"No investment named '{investment_name}' exists in the current "
            "data. Use list_investments to see the available names."
        )

    name, rows = resolved
    kind_label = nav_kind if nav_kind is not None else "all kinds"

    if not rows:
        return f"Investment '{name}' has no NAV rows ({kind_label})."

    header = (
        f"NAV history for '{name}' ({kind_label}) — {len(rows)} row(s):\n"
        f"  {'as_of_date':<10}  {'kind':<7}  {'nav_value':>18}  currency"
    )

    # ``list_by_investment*`` already returns rows sorted by
    # ``as_of_date`` ascending; the row cap mirrors ``get_dataset_slice``.
    if len(rows) > _NAV_ROW_CAP:
        head = rows[:25]
        tail = rows[-25:]
        omitted = len(rows) - 50
        body = (
            "\n".join(_format_nav_row(n) for n in head)
            + f"\n  ... ({omitted} rows omitted) ...\n"
            + "\n".join(_format_nav_row(n) for n in tail)
        )
    else:
        body = "\n".join(_format_nav_row(n) for n in rows)

    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# get_investment_data — the structured-data access tool (Axis 1)
# ---------------------------------------------------------------------------


# Column names whose values are ISO date / timestamp strings. The
# series bundles lead with one of these, with rows sorted ascending,
# so the first and last rows bound the date span in the model-facing
# summary.
_DATE_COLUMNS = frozenset({"as_of_date", "flow_timestamp"})


def _in_window(iso_value: str, start: date | None, end: date | None) -> bool:
    """Return whether an ISO date/datetime value falls in ``[start, end]``.

    The bounds are inclusive and open on the side where they are
    ``None``. The row's date is parsed from the first ten characters of
    ``iso_value`` so both ISO dates (``2021-12-31``) and ISO datetimes
    (``2021-03-15T12:00:00+00:00``) window the same way.

    Defensive by design: an unparseable ``iso_value`` returns ``True``
    rather than silently dropping the row on a formatting surprise. The
    bounds themselves are validated up front by
    :func:`get_investment_data`, so they are trusted here.

    Args:
        iso_value: The row's date column as an ISO string.
        start: Inclusive lower bound, or ``None`` for open-on-the-left.
        end: Inclusive upper bound, or ``None`` for open-on-the-right.

    Returns:
        ``True`` when the parsed date lies within the window.
    """
    try:
        parsed = date.fromisoformat(iso_value[:10])
    except (ValueError, TypeError):
        logger.debug("Unparseable row date %r; keeping it (no window drop).", iso_value)
        return True
    if start is not None and parsed < start:
        return False
    return not (end is not None and parsed > end)


def _data_envelope(
    bundle: str,
    columns: list[str],
    rows: list[list],
    *,
    meta: dict[str, object],
    investment_name: str | None,
) -> str:
    """Build and cache one structured-data envelope; return its summary.

    Builds the Axis-1 → Axis-2 envelope exactly as the contract
    documents it, applies the defensive :data:`_DATA_ROW_CAP`, and
    records the outcome (``row_count``, ``truncated``) into ``meta``.
    The envelope is then stored server-side via
    :func:`~services.tools._tool_context.store_tool_data`, and a
    compact **summary string** carrying the resulting handle is
    returned to the model.

    The full envelope never reaches the model: a tool call's arguments
    are model-generated output, so passing the rows *as an argument*
    to ``render_chart`` would force the model to re-emit every one of
    them token-by-token. The model instead receives only the handle
    plus the shape facts it needs (see :func:`_data_summary`), and
    ``render_chart`` resolves the rows by handle. See ADR-0048
    (amended) and the :mod:`services.tools._tool_context` docstring.

    Args:
        bundle: Which of the four bundles this envelope carries.
        columns: Tidy column names, in row order.
        rows: Row tuples, each aligned to ``columns``.
        meta: Bundle-specific context (units, counts). ``row_count``
            and ``truncated`` are added here.
        investment_name: The resolved investment name, or ``None`` for
            the tenant-wide ``catalogue`` bundle.

    Returns:
        A compact summary string — the data handle plus the row count,
        column names, date span, and units the model needs to drive
        ``render_chart``.
    """
    truncated = len(rows) > _DATA_ROW_CAP
    uncapped_count = len(rows)
    if truncated:
        rows = rows[:_DATA_ROW_CAP]
    enriched_meta = {**meta, "row_count": len(rows), "truncated": truncated}
    if truncated:
        enriched_meta["row_count_uncapped"] = uncapped_count
    envelope = {
        "__data__": "investment_data",
        "bundle": bundle,
        "investment_name": investment_name,
        "columns": columns,
        "rows": rows,
        "meta": enriched_meta,
    }
    handle = store_tool_data(envelope)
    return _data_summary(handle, envelope)


def _data_summary(handle: str, envelope: dict[str, object]) -> str:
    """Render the model-facing summary of a cached structured-data envelope.

    The model never sees the envelope's rows — only this summary and
    the handle. The summary therefore has to carry everything the
    model needs to compose a correct ``render_chart`` call *without
    seeing a single row*: the bundle, the row count, the column names
    (so it can pick ``x_column`` / ``y_columns`` / ``series_column``),
    the date span for the series bundles, and the currency.

    Args:
        handle: The cache handle :func:`store_tool_data` returned.
        envelope: The structured-data envelope just cached.

    Returns:
        A short multi-line summary string ending with the
        ``data_handle`` line and the ``render_chart`` instruction.
    """
    bundle = envelope["bundle"]
    columns: list[str] = envelope["columns"]  # type: ignore[assignment]
    rows: list[list] = envelope["rows"]  # type: ignore[assignment]
    meta: dict[str, object] = envelope["meta"]  # type: ignore[assignment]
    investment_name = envelope["investment_name"]

    subject = f'"{investment_name}"' if investment_name else "all investments"
    facts = [f"Fetched {bundle} for {subject}: {len(rows)} row(s)"]
    if columns and columns[0] in _DATE_COLUMNS and rows:
        facts.append(f"{rows[0][0]} to {rows[-1][0]}")
    currency = meta.get("currency")
    if currency:
        facts.append(str(currency))
    currencies = meta.get("currencies")
    if currencies and len(currencies) > 1:
        facts.append(f"mixed currencies: {', '.join(currencies)}")
    elif currencies and len(currencies) == 1:
        facts.append(str(currencies[0]))

    lines = [", ".join(facts) + "."]
    lines.append(f"Columns: {', '.join(columns)}.")
    if meta.get("truncated"):
        lines.append(
            f"(Row-capped at {_DATA_ROW_CAP}; render_chart still receives every cached row.)"
        )
    lines.append(f"data_handle: {handle}")
    lines.append("Pass this handle to render_chart's data_handle argument to chart it.")
    return "\n".join(lines)


def _catalogue_envelope(ctx: ToolExecutionContext) -> str:
    """Build the ``catalogue`` bundle — one row per investment, stamp data."""

    async def _workflow() -> list[InvestmentDTO]:
        async with _tool_session(ctx) as db:
            return await InvestmentRepository(db).list_all()

    investments = run_async_in_fresh_loop(_workflow)

    if not investments:
        return (
            "No investments are present in the persistent investment "
            "database. Import portfolio data via the Front Office Data "
            "Import section first."
        )

    columns = [
        "name",
        "investment_type",
        "manager_name",
        "currency",
        "vintage_year",
        "commitment_amount",
        "region",
        "is_active",
    ]
    rows: list[list] = [
        [
            inv.name,
            inv.investment_type,
            inv.manager_name,
            inv.currency,
            inv.vintage_year,
            (float(inv.commitment_amount) if inv.commitment_amount is not None else None),
            inv.region,
            inv.is_active,
        ]
        for inv in investments
    ]
    return _data_envelope(
        "catalogue",
        columns,
        rows,
        meta={"investment_count": len(investments)},
        investment_name=None,
    )


def _nav_series_envelope(
    ctx: ToolExecutionContext,
    name: str,
    *,
    start: date | None = None,
    end: date | None = None,
    nav_kind: str = "",
) -> str:
    """Build the ``nav_series`` bundle for one named investment.

    Args:
        ctx: The per-turn tool-execution context.
        name: Exact investment name.
        start: Inclusive lower ``as_of_date`` bound, or ``None`` for open.
        end: Inclusive upper ``as_of_date`` bound, or ``None`` for open.
        nav_kind: When non-empty (``'actual'`` / ``'plan'``), keep only
            rows of that kind; when empty, keep both kinds.
    """

    async def _workflow() -> tuple[InvestmentDTO, list[InvestmentNavDTO]] | None:
        async with _tool_session(ctx) as db:
            investments = InvestmentRepository(db)
            dto = await investments.get_by_name(name)
            if dto is None:
                return None
            navs = await InvestmentNavRepository(db).list_by_investment(dto.id)
            return dto, navs

    resolved = run_async_in_fresh_loop(_workflow)
    if resolved is None:
        return _UNKNOWN_INVESTMENT_MSG.format(name=name)

    dto, navs = resolved
    if not navs:
        return f"Investment '{dto.name}' has no NAV rows recorded."

    # ``list_by_investment`` already sorts by ``as_of_date`` ascending.
    # Filter on the rows, before the row cap, so windowing a long series
    # can never be truncated away.
    columns = ["as_of_date", "nav_value", "nav_kind"]
    rows: list[list] = [
        [n.as_of_date.isoformat(), float(n.nav_value), n.nav_kind]
        for n in navs
        if _in_window(n.as_of_date.isoformat(), start, end)
        and (not nav_kind or n.nav_kind == nav_kind)
    ]
    if not rows:
        return (
            f"Investment '{dto.name}' has no NAV rows matching the requested "
            "date window / nav_kind."
        )
    return _data_envelope(
        "nav_series",
        columns,
        rows,
        meta={"currency": navs[0].currency},
        investment_name=dto.name,
    )


def _portfolio_nav_series_envelope(
    ctx: ToolExecutionContext,
    *,
    names: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    nav_kind: str = "",
) -> str:
    """Build the ``portfolio_nav_series`` bundle — every investment's NAV history.

    Tidy / long-form: one row per ``(date, investment)`` pair. Feeds
    ``render_chart`` with ``series_column="investment_name"`` to produce
    one trace per investment in a single Plotly figure — the
    multi-investment counterpart to the per-investment ``nav_series``
    bundle, using the same generic ``series_column`` mechanism the
    plan-vs-actual overlay already relies on (ADR-0048).

    Args:
        ctx: The per-turn tool-execution context.
        names: Optional subset of investment names to include. When
            provided, only investments whose exact (whitespace-trimmed)
            name is in this set are charted; when ``None``, every
            investment is included.
        start: Inclusive lower ``as_of_date`` bound, or ``None`` for open.
        end: Inclusive upper ``as_of_date`` bound, or ``None`` for open.
        nav_kind: When non-empty (``'actual'`` / ``'plan'``), keep only
            rows of that kind; when empty, keep both kinds.
    """
    name_set = {n.strip() for n in names} if names is not None else None

    async def _workflow() -> tuple[int, list[tuple[str, list[InvestmentNavDTO]]]]:
        async with _tool_session(ctx) as db:
            inv_repo = InvestmentRepository(db)
            nav_repo = InvestmentNavRepository(db)
            investments = await inv_repo.list_all()
            # N+1 by design: the repository layer does not yet expose a
            # tenant-wide ``list_navs_by_tenant``, and at 7–200
            # investments the per-investment roundtrip cost is
            # negligible against the asyncpg baseline. All queries run
            # on the same loop-local session — do not fan out.
            collected: list[tuple[str, list[InvestmentNavDTO]]] = []
            for dto in investments:
                # Subset filter: skip non-selected investments BEFORE
                # fetching their NAV rows, so the subset avoids needless
                # N+1 round-trips rather than fetching then discarding.
                if name_set is not None and dto.name not in name_set:
                    continue
                navs = await nav_repo.list_by_investment(dto.id)
                collected.append((dto.name, navs))
            return len(investments), collected

    total_investment_count, investments_data = run_async_in_fresh_loop(_workflow)

    if total_investment_count == 0:
        return (
            "No investments are present in the persistent investment "
            "database. Import portfolio data via the Front Office Data "
            "Import section first."
        )

    columns = ["as_of_date", "investment_name", "nav_value", "nav_kind"]
    rows: list[list] = []
    currencies: set[str] = set()
    investment_count = 0
    # Iterate in ``list_all``'s order; within each investment the rows
    # are already sorted ascending by ``as_of_date`` from
    # ``list_by_investment``. A global resort by date would be lossy
    # work — ``render_chart``'s ``series_column`` splits the rows into
    # one trace per investment before Plotly orders each trace's x
    # axis, so the per-trace ordering we already have is what counts.
    # Filtering happens here, on the rows, before the row cap, so a
    # date window can never be truncated away.
    for name, navs in investments_data:
        contributed = False
        for n in navs:
            if not _in_window(n.as_of_date.isoformat(), start, end):
                continue
            if nav_kind and n.nav_kind != nav_kind:
                continue
            rows.append([n.as_of_date.isoformat(), name, float(n.nav_value), n.nav_kind])
            currencies.add(n.currency)
            contributed = True
        # ``investment_count`` counts only investments that contributed
        # at least one row after filtering.
        if contributed:
            investment_count += 1

    if not rows:
        filters_active = (
            name_set is not None or start is not None or end is not None or bool(nav_kind)
        )
        if filters_active:
            return "No NAV rows match the requested investments / date window / nav_kind."
        return "No NAV rows are recorded for any investment in the portfolio."

    return _data_envelope(
        "portfolio_nav_series",
        columns,
        rows,
        meta={
            "investment_count": investment_count,
            "currencies": sorted(currencies),
        },
        investment_name=None,
    )


def _cashflow_series_envelope(
    ctx: ToolExecutionContext,
    name: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> str:
    """Build the ``cashflow_series`` bundle for one named investment.

    Args:
        ctx: The per-turn tool-execution context.
        name: Exact investment name.
        start: Inclusive lower ``flow_timestamp`` bound, or ``None`` for open.
        end: Inclusive upper ``flow_timestamp`` bound, or ``None`` for open.
    """

    async def _workflow() -> tuple[InvestmentDTO, list] | None:
        async with _tool_session(ctx) as db:
            investments = InvestmentRepository(db)
            dto = await investments.get_by_name(name)
            if dto is None:
                return None
            cashflows = await InvestmentCashflowRepository(db).list_by_investment(dto.id)
            return dto, cashflows

    resolved = run_async_in_fresh_loop(_workflow)
    if resolved is None:
        return _UNKNOWN_INVESTMENT_MSG.format(name=name)

    dto, cashflows = resolved
    if not cashflows:
        return f"Investment '{dto.name}' has no cashflow rows recorded."

    # ``list_by_investment`` already sorts by ``flow_timestamp`` ascending.
    # Window on the rows, before the row cap.
    columns = ["flow_timestamp", "flow_type", "flow_kind", "amount"]
    rows: list[list] = [
        [
            c.flow_timestamp.isoformat(),
            c.flow_type,
            c.flow_kind,
            float(c.amount),
        ]
        for c in cashflows
        if _in_window(c.flow_timestamp.isoformat(), start, end)
    ]
    if not rows:
        return f"Investment '{dto.name}' has no cashflow rows matching the requested date window."
    return _data_envelope(
        "cashflow_series",
        columns,
        rows,
        meta={"currency": cashflows[0].currency},
        investment_name=dto.name,
    )


def _return_metrics_envelope(
    ctx: ToolExecutionContext,
    name: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> str:
    """Build the ``return_metrics`` bundle for one named investment.

    Reuses :meth:`InvestmentService.get_charts_data` — the same
    analytics aggregation the Phase-5b investment-detail charts use —
    rather than recomputing. The total-return series, the rolling
    TVPI / DPI / RVPI multiples, and the rolling IRR are all keyed by
    ``as_of_date``; they are merged into one tidy frame so a single
    envelope can drive a multi-series chart.

    Args:
        ctx: The per-turn tool-execution context.
        name: Exact investment name.
        start: Inclusive lower ``as_of_date`` bound, or ``None`` for open.
        end: Inclusive upper ``as_of_date`` bound, or ``None`` for open.
    """

    async def _workflow() -> InvestmentChartsBundle | None:
        async with _tool_session(ctx) as db:
            investments = InvestmentRepository(db)
            dto = await investments.get_by_name(name)
            if dto is None:
                return None
            service = InvestmentService(
                investments,
                InvestmentNavRepository(db),
                InvestmentCashflowRepository(db),
            )
            return await service.get_charts_data(dto.id)

    charts = run_async_in_fresh_loop(_workflow)
    if charts is None:
        return _UNKNOWN_INVESTMENT_MSG.format(name=name)

    # Merge the three date-keyed series into one record per date.
    record: dict[str, dict[str, float | None]] = {}

    def _key(value: object) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    for as_of, value in charts.total_return_series.items():
        record.setdefault(_key(as_of), {})["total_return"] = (
            None if pd.isna(value) else float(value)
        )
    for tup in charts.rolling_multiples.itertuples(index=False):
        rec = record.setdefault(_key(tup.as_of_date), {})
        rec["tvpi"] = None if pd.isna(tup.tvpi) else float(tup.tvpi)
        rec["dpi"] = None if pd.isna(tup.dpi) else float(tup.dpi)
        rec["rvpi"] = None if pd.isna(tup.rvpi) else float(tup.rvpi)
    for as_of, value in charts.rolling_irr.items():
        record.setdefault(_key(as_of), {})["rolling_irr"] = None if pd.isna(value) else float(value)

    if not record:
        return (
            f"Investment '{charts.investment_name}' has no computed return "
            "metrics yet — it needs actual NAV history (and cashflows for "
            "the multiples and IRR) before these can be derived."
        )

    columns = [
        "as_of_date",
        "total_return",
        "tvpi",
        "dpi",
        "rvpi",
        "rolling_irr",
    ]
    # Window on the merged records, before the row cap. ``key`` is the
    # per-date ISO string, so ``_in_window`` filters on ``as_of_date``.
    rows: list[list] = [
        [key, *(record[key].get(c) for c in columns[1:])]
        for key in sorted(record)
        if _in_window(key, start, end)
    ]
    if not rows:
        return (
            f"Investment '{charts.investment_name}' has no return metrics in "
            "the requested date window."
        )
    populated = [
        c for i, c in enumerate(columns[1:], start=1) if any(r[i] is not None for r in rows)
    ]
    return _data_envelope(
        "return_metrics",
        columns,
        rows,
        meta={"series": populated},
        investment_name=charts.investment_name,
    )


def get_investment_data(
    bundle: str,
    investment_name: str = "",
    investment_names: list[str] | None = None,
    start_date: str = "",
    end_date: str = "",
    nav_kind: str = "",
) -> str:
    """Fetch one investment-domain data bundle for charting.

    The data-acquisition half of the two-axis chart architecture
    (ADR-0048). Unlike :func:`list_investments` /
    :func:`get_investment_detail` / :func:`get_investment_nav_history`
    — which return human-readable prose — this tool builds a
    machine-readable structured-data envelope (see the module
    docstring's *Structured-data envelope contract*), caches it
    server-side, and returns a compact summary carrying an opaque
    **data handle**. The usual flow is two steps: call this tool to
    fetch a bundle, then pass the handle it returns to ``render_chart``
    as its ``data_handle`` argument. The rows themselves never travel
    through the model.

    To chart several named investments overlaid in one chart, call this
    with ``bundle="portfolio_nav_series"`` and
    ``investment_names=[...]`` (not one ``nav_series`` call per
    investment), then ``render_chart`` with
    ``series_column="investment_name"``.

    Args:
        bundle: Which data bundle to return — one of ``catalogue``
            (every investment with its stamp data), ``nav_series``
            (the NAV time series of one investment), ``cashflow_series``
            (the cashflow time series of one investment),
            ``return_metrics`` (the computed total-return series,
            rolling TVPI / DPI / RVPI, and rolling IRR of one
            investment), or ``portfolio_nav_series`` (every
            investment's NAV time series concatenated into one tidy
            long-form envelope keyed by ``investment_name``, for
            charting all investments overlaid via
            ``series_column="investment_name"``).
        investment_name: Exact name of the investment, matched against
            the per-tenant catalogue. Required for the ``nav_series``,
            ``cashflow_series``, and ``return_metrics`` bundles;
            ignored for ``catalogue`` and ``portfolio_nav_series``.
            Use :func:`list_investments` to discover valid names.
        investment_names: Optional subset filter that applies *only* to
            the ``portfolio_nav_series`` bundle: when given, only the
            investments whose exact (whitespace-trimmed) name is in this
            list are included, each matched case-sensitively against the
            catalogue. Ignored for every other bundle. Omit (``None``)
            to include every investment — today's behaviour.
        start_date: Optional inclusive ISO ``YYYY-MM-DD`` lower bound.
            Windows the time-series bundles on their date column
            (``as_of_date`` for ``nav_series`` / ``return_metrics`` /
            ``portfolio_nav_series``; ``flow_timestamp`` for
            ``cashflow_series``). Ignored for ``catalogue``. Empty means
            open on the left. An unparseable value returns a clear
            ``YYYY-MM-DD`` guidance string rather than raising.
        end_date: Optional inclusive ISO ``YYYY-MM-DD`` upper bound,
            symmetric with ``start_date``. Empty means open on the right.
        nav_kind: Optional NAV-kind filter (``'actual'`` or ``'plan'``)
            for the ``nav_series`` and ``portfolio_nav_series`` bundles.
            Omit to include both kinds — today's behaviour. For a clean
            multi-investment overlay pass ``'actual'`` so actual and plan
            values are not mixed into one line.

    Returns:
        A compact summary string carrying a data handle, the row
        count, and the column names on success — the model passes the
        handle to ``render_chart``. An explanatory string when the
        tool-execution context is unset, the bundle name is invalid,
        a date bound or ``nav_kind`` is malformed, the investment name
        is missing or unknown, or the requested (and now optionally
        filtered) data is empty.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG

    if bundle not in _VALID_BUNDLES:
        return f"Invalid bundle '{bundle}'. Valid bundles are: {', '.join(sorted(_VALID_BUNDLES))}."

    # Validate the optional date bounds and nav_kind up front — clear
    # return string on bad input, never raise. Parsed into
    # ``datetime.date | None`` so every builder windows uniformly.
    try:
        start_bound = date.fromisoformat(start_date) if start_date else None
    except ValueError:
        return (
            f"Invalid start_date '{start_date}'. Use an ISO calendar date in "
            "YYYY-MM-DD form (e.g. 2019-01-01)."
        )
    try:
        end_bound = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        return (
            f"Invalid end_date '{end_date}'. Use an ISO calendar date in "
            "YYYY-MM-DD form (e.g. 2022-12-31)."
        )
    if nav_kind and nav_kind not in ("actual", "plan"):
        return (
            f"Invalid nav_kind '{nav_kind}'. Valid values are 'actual' or "
            "'plan'. Omit the argument to include both kinds."
        )

    if bundle == "catalogue":
        return _catalogue_envelope(ctx)
    if bundle == "portfolio_nav_series":
        return _portfolio_nav_series_envelope(
            ctx,
            names=investment_names,
            start=start_bound,
            end=end_bound,
            nav_kind=nav_kind,
        )

    # The remaining three bundles are per-investment.
    if not investment_name:
        return (
            f"The '{bundle}' bundle needs an investment_name. Use "
            "list_investments to discover valid names."
        )

    if bundle == "nav_series":
        return _nav_series_envelope(
            ctx,
            investment_name,
            start=start_bound,
            end=end_bound,
            nav_kind=nav_kind,
        )
    if bundle == "cashflow_series":
        return _cashflow_series_envelope(ctx, investment_name, start=start_bound, end=end_bound)
    return _return_metrics_envelope(ctx, investment_name, start=start_bound, end=end_bound)


# ---------------------------------------------------------------------------
# Register tools at import time
# ---------------------------------------------------------------------------

_registry = get_tool_registry()

_registry.register_tool(
    name="list_investments",
    function=list_investments,
    description=(
        "List every investment in PortfoliFLOW's persistent investment "
        "database (the Postgres-backed catalogue the web Excel import "
        "writes to). Returns each investment's name, type, manager, "
        "currency, vintage year, commitment amount, and active flag. "
        "Prefer this over list_datasets when the user asks about "
        "investments, funds, NAVs, or cashflows — list_datasets reads a "
        "separate in-memory store that the web app does not populate. "
        "Use this tool to discover the exact investment names accepted "
        "by get_investment_detail and get_investment_nav_history."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_investment_detail",
    function=get_investment_detail,
    description=(
        "Get the catalogue fields plus a NAV and cashflow summary for "
        "one investment from the persistent investment database, "
        "resolved by its exact name. The investment_name argument is "
        "matched exactly against the catalogue — call list_investments "
        "first to discover valid names. For the full NAV time series, "
        "use get_investment_nav_history instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "investment_name": {
                "type": "string",
                "description": (
                    "Exact name of the investment, as returned by "
                    "list_investments. Matched exactly against the "
                    "per-tenant catalogue."
                ),
            }
        },
        "required": ["investment_name"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_investment_nav_history",
    function=get_investment_nav_history,
    description=(
        "Return the NAV (net asset value) time series for one "
        "investment from the persistent investment database, as a "
        "compact date-sorted table. The investment_name argument is "
        "matched exactly against the catalogue — call list_investments "
        "first to discover valid names. Optionally filter to a single "
        "nav_kind ('actual' or 'plan'); omit it to return both. Long "
        "series are capped at 50 rows."
    ),
    parameters={
        "type": "object",
        "properties": {
            "investment_name": {
                "type": "string",
                "description": (
                    "Exact name of the investment, as returned by "
                    "list_investments. Matched exactly against the "
                    "per-tenant catalogue."
                ),
            },
            "nav_kind": {
                "type": "string",
                "description": (
                    "Optional NAV-kind filter: 'actual' (realised "
                    "valuations) or 'plan' (projections). Omit to "
                    "return both kinds."
                ),
                "enum": ["actual", "plan"],
            },
        },
        "required": ["investment_name"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_investment_data",
    function=get_investment_data,
    description=(
        "Fetch investment-domain data for charting. This is the "
        "data-fetch step of charting: call get_investment_data to "
        "obtain a bundle; it caches the data server-side and returns a "
        "short summary carrying a data handle; pass that handle as "
        "render_chart's 'data_handle' argument. The 'bundle' argument "
        "selects which data to return: "
        "'catalogue' (every investment with its stamp data — name, "
        "type, manager, currency, vintage, commitment, active flag), "
        "'nav_series' (the NAV time series of one named investment), "
        "'cashflow_series' (the cashflow time series of one named "
        "investment), 'return_metrics' (the computed total-return "
        "series, rolling TVPI/DPI/RVPI multiples, and rolling IRR of "
        "one named investment), or 'portfolio_nav_series' (the NAV "
        "time series of every investment in the portfolio in long "
        "form, one row per (date, investment) pair — the right bundle "
        "for charting all investments overlaid in a single chart via "
        "series_column='investment_name' on render_chart). The "
        "'catalogue' and 'portfolio_nav_series' bundles are "
        "tenant-wide and ignore investment_name; the other three are "
        "per-investment and require investment_name, matched exactly "
        "against the catalogue — call list_investments first to "
        "discover valid names. Prefer this tool over "
        "get_investment_nav_history when the user wants a chart. "
        "To chart SEVERAL named investments overlaid in ONE chart, call "
        "this with bundle='portfolio_nav_series' and "
        "investment_names=[...] (NOT one nav_series call per "
        "investment), then render_chart with "
        "series_column='investment_name'. For a clean overlay, pass "
        "nav_kind='actual' so actual and plan values are not mixed into "
        "one line. Use start_date/end_date to window the range."
    ),
    parameters={
        "type": "object",
        "properties": {
            "investment_name": {
                "type": "string",
                "description": (
                    "Exact name of the investment, as returned by "
                    "list_investments. Required for the 'nav_series', "
                    "'cashflow_series', and 'return_metrics' bundles; "
                    "ignored for 'catalogue' and 'portfolio_nav_series'."
                ),
            },
            "bundle": {
                "type": "string",
                "enum": [
                    "catalogue",
                    "nav_series",
                    "cashflow_series",
                    "return_metrics",
                    "portfolio_nav_series",
                ],
                "description": (
                    "Which data bundle to return. 'catalogue' and "
                    "'portfolio_nav_series' are tenant-wide; the other "
                    "three are per-investment and need investment_name."
                ),
            },
            "investment_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset filter for the 'portfolio_nav_series' "
                    "bundle: only these investments are included, each name "
                    "matched exactly against the catalogue. Ignored for "
                    "other bundles. Omit to include every investment."
                ),
            },
            "start_date": {
                "type": "string",
                "description": (
                    "Optional inclusive ISO YYYY-MM-DD bound; filters the "
                    "time-series bundles to a date window. Ignored for "
                    "'catalogue'."
                ),
            },
            "end_date": {
                "type": "string",
                "description": (
                    "Optional inclusive ISO YYYY-MM-DD bound; filters the "
                    "time-series bundles to a date window. Ignored for "
                    "'catalogue'."
                ),
            },
            "nav_kind": {
                "type": "string",
                "enum": ["actual", "plan"],
                "description": (
                    "Optional NAV-kind filter for 'nav_series' and "
                    "'portfolio_nav_series'. Omit to include both kinds."
                ),
            },
        },
        "required": ["bundle"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)
