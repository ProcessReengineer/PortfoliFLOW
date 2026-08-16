# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AI-callable chart tools for PortfoliFLOW.

Two tools live here, one per rendering stack:

* ``generate_chart`` — the legacy PyQt6-GUI path. Renders themed
  matplotlib charts to Base64 PNGs, reading from the in-memory
  ``DataStore`` or hand-typed inline data. Kept unchanged for the GUI,
  which populates the DataStore on Excel import (ADR-0041).
* ``render_chart`` — the web-assistant path (ADR-0048, Axis 2).
  Consumes the structured-data envelope produced by the
  ``get_investment_data`` tool — looked up server-side by the *data
  handle* that tool returns, not received as an argument (ADR-0048,
  amended) — and renders a *themed Plotly figure spec* — no
  matplotlib, no DataStore, no hand-typed data. The chat surface hands
  the spec straight to ``Plotly.newPlot``, so Shirley's charts look
  and behave exactly like the web pages' charts.

``generate_chart`` renders using the Agg backend (headless,
thread-safe) so it can be called safely from a QThread worker.
``render_chart`` is pure dict construction — no rendering engine, no
event loop, no thread concerns.

All visual parameters come from ``config/chart_theme.json``:
``generate_chart`` via :func:`core.chart_theme.get_chart_theme`,
``render_chart`` via :func:`services.chart_specs.base.get_chart_theme`
(the matplotlib-free loader). All registered tools return strings;
chart tools return a JSON artefact envelope that the streaming core
detects and strips before forwarding to the LLM.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re

# NOTE: matplotlib.use() is intentionally omitted here. The GUI widgets set the
# backend to QtAgg at import time, and matplotlib does not allow switching backends
# after initialisation. This module is thread-safe without it because it uses the
# Figure class directly — never pyplot — and never instantiates a Qt canvas.
# FigureCanvasAgg is attached explicitly so fig.savefig() always uses the Agg
# renderer regardless of the global rcParams backend.

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.patches import Circle

from core.chart_helpers import apply_axes_theme, create_themed_figure, get_series_colour
from core.chart_theme import get_chart_theme
from core.data_store import get_data_store
from services.chart_specs.base import get_chart_theme as get_plotly_chart_theme
from services.chart_specs.base import layout_from_theme
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._tool_context import get_tool_data

logger = logging.getLogger(__name__)

_VALID_CHART_TYPES = frozenset({"line", "bar", "grouped_bar", "scatter", "pie", "donut"})
_VALID_DATA_SOURCES = frozenset({"datastore", "inline"})

# Structured-data envelope discriminators ``render_chart`` will resolve.
# ``get_investment_data`` (investment_tools.py) produces "investment_data";
# ``get_saa_hypothetical_comparison`` (analysis_tools.py, ADR-0069) produces
# "saa_hypothetical". Both carry the same tidy columns/rows/meta shape, so the
# downstream rendering path is identical — only the discriminator differs.
_ACCEPTED_DATA_DISCRIMINATORS = frozenset({"investment_data", "saa_hypothetical"})

# Matches an ISO date or datetime prefix (``2021-12-31`` /
# ``2021-12-31T12:00:00+00:00``). ``render_chart`` uses it to decide,
# from the data alone, whether an x column is a Plotly date axis.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def generate_chart(
    chart_type: str,
    title: str,
    data_source: str,
    x_label: str = "",
    y_label: str = "",
    datastore_key: str = "",
    columns: list[str] | None = None,
    inline_data: dict | None = None,
    date_range: dict | None = None,
) -> str:
    """Generate a themed matplotlib chart and return it as a Base64 PNG artefact.

    The return value is a JSON envelope detected by the ``_StreamWorker`` tool-
    execution loop.  The image data is stripped before forwarding to the LLM;
    the model only receives the short ``llm_response`` confirmation string.

    Args:
        chart_type: One of ``"line"``, ``"bar"``, ``"grouped_bar"``,
            ``"scatter"``, ``"pie"``, ``"donut"``.
        title: Chart title displayed above the chart.
        data_source: ``"datastore"`` to read from the DataStore, or
            ``"inline"`` to use data provided directly.
        x_label: Label for the X axis (ignored for pie/donut).
        y_label: Label for the Y axis (ignored for pie/donut).
        datastore_key: DataStore key to fetch data from.  Required when
            ``data_source`` is ``"datastore"``.
        columns: Column names to plot from the DataStore dataset.  If omitted,
            all numeric columns are used.
        inline_data: Data provided as a dict.  Required when ``data_source`` is
            ``"inline"``.  For line/bar/scatter: ``{"x": [...], "y": [...]}``
            or ``{"x": [...], "series": {"Name1": [...], "Name2": [...]}}``.
            For pie/donut: ``{"labels": [...], "values": [...]}``.
        date_range: Optional date range filter for DataStore datasets with a
            DatetimeIndex.  Dict with ``"start"`` and/or ``"end"`` ISO date strings.

    Returns:
        JSON string with ``__artifact__`` key on success, or a plain error
        string on failure.
    """
    # ------------------------------------------------------------------
    # 1. Validate inputs
    # ------------------------------------------------------------------
    if chart_type not in _VALID_CHART_TYPES:
        return f"Invalid chart_type '{chart_type}'. Valid types: {sorted(_VALID_CHART_TYPES)}."
    if data_source not in _VALID_DATA_SOURCES:
        return f"Invalid data_source '{data_source}'. Must be 'datastore' or 'inline'."
    if data_source == "datastore" and not datastore_key:
        return (
            "data_source is 'datastore' but datastore_key was not provided. "
            "Use the list_datasets tool to discover available keys."
        )
    if data_source == "inline" and inline_data is None:
        return (
            "data_source is 'inline' but inline_data was not provided. "
            "Supply the data as a JSON object."
        )

    # ------------------------------------------------------------------
    # 2. Load data
    # ------------------------------------------------------------------
    df: pd.DataFrame | None = None
    x_data: list | None = None
    series_data: dict[str, list] | None = None
    pie_labels: list[str] | None = None
    pie_values: list[float] | None = None

    if data_source == "datastore":
        store = get_data_store()
        df = store.get(datastore_key)
        if df is None:
            return (
                f"Dataset '{datastore_key}' not found in the DataStore. "
                "Use list_datasets to see available datasets."
            )

        # Column filter
        if columns is not None:
            missing = [c for c in columns if c not in df.columns]
            if missing:
                return (
                    f"Columns not found in '{datastore_key}': {missing}. "
                    f"Available columns: {list(df.columns[:20])}."
                )
            df = df[columns]

        # Keep only numeric columns (after optional column filter)
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if not numeric_cols:
            return f"Dataset '{datastore_key}' has no numeric columns to plot."
        df = df[numeric_cols]

        # Date range filter
        if date_range and isinstance(df.index, pd.DatetimeIndex):
            try:
                start = pd.to_datetime(date_range.get("start")) if date_range.get("start") else None
                end = pd.to_datetime(date_range.get("end")) if date_range.get("end") else None
                df = df.loc[start:end]
            except (ValueError, KeyError) as exc:
                return f"Invalid date_range: {exc}. Use ISO format (YYYY-MM-DD)."

        if df.empty:
            return f"No data remaining after applying filters to '{datastore_key}'."

    else:  # inline
        assert inline_data is not None  # already validated above
        if chart_type in {"pie", "donut"}:
            pie_labels = inline_data.get("labels")
            raw_values = inline_data.get("values")
            if pie_labels is None or raw_values is None:
                return (
                    "inline_data for pie/donut must have 'labels' and 'values' keys. "
                    f"Got: {list(inline_data.keys())}."
                )
            pie_values = [float(v) for v in raw_values]
        else:
            x_data = inline_data.get("x")
            if x_data is None:
                return f"inline_data must have an 'x' key. Got: {list(inline_data.keys())}."
            if "series" in inline_data:
                series_data = {k: list(v) for k, v in inline_data["series"].items()}
            elif "y" in inline_data:
                series_data = {"": list(inline_data["y"])}
            else:
                return (
                    "inline_data must have a 'y' key (single series) or a 'series' key "
                    f"(multi-series dict). Got: {list(inline_data.keys())}."
                )

    # ------------------------------------------------------------------
    # 3. Render chart
    # ------------------------------------------------------------------
    theme = get_chart_theme()

    try:
        fig = create_themed_figure(theme, width=6.0, height_px=400)
        # Explicitly attach Agg canvas so the figure renders headlessly in a thread
        FigureCanvasAgg(fig)

        if chart_type in {"pie", "donut"}:
            ax = fig.add_subplot(111)
            assert pie_labels is not None and pie_values is not None
            colours = [get_series_colour(theme, i) for i in range(len(pie_labels))]
            _wedges, texts, autotexts = ax.pie(
                pie_values,
                labels=pie_labels,
                colors=colours,
                autopct="%1.1f%%",
                startangle=90,
            )
            for text in texts:
                text.set_color(theme["colours"]["text"])
            for autotext in autotexts:
                autotext.set_color(theme["colours"]["text"])
            if chart_type == "donut":
                centre_circle = Circle((0, 0), 0.5, fc=theme["colours"]["background"])
                ax.add_artist(centre_circle)
            ax.set_aspect("equal")
            ax.set_title(
                title,
                color=theme["colours"]["text"],
                fontsize=theme["font"]["title_size"],
                fontweight=theme["font"]["weight_title"],
                pad=theme["layout"]["title_pad"],
                fontfamily=theme["font"]["family"],
            )

        elif chart_type == "line":
            ax = fig.add_subplot(111)
            apply_axes_theme(ax, theme)
            handles = []
            labels = []

            if df is not None:
                for i, col in enumerate(df.columns):
                    colour = get_series_colour(theme, i)
                    (line,) = ax.plot(
                        df.index,
                        df[col].values,
                        color=colour,
                        linewidth=theme["line"]["width_primary"],
                    )
                    handles.append(line)
                    labels.append(str(col))
            else:
                assert x_data is not None and series_data is not None
                for i, (name, y_vals) in enumerate(series_data.items()):
                    colour = get_series_colour(theme, i)
                    (line,) = ax.plot(
                        x_data, y_vals, color=colour, linewidth=theme["line"]["width_primary"]
                    )
                    handles.append(line)
                    labels.append(name)

            if len(labels) > 1 or (len(labels) == 1 and labels[0]):
                ax.legend(
                    handles,
                    labels,
                    loc=theme["legend"]["location"],
                    frameon=theme["legend"]["frame_on"],
                    fontsize=theme["font"]["legend_size"],
                    labelcolor=theme["colours"]["text"],
                )
            _set_axis_labels(ax, title, x_label, y_label, theme)

        elif chart_type == "bar":
            ax = fig.add_subplot(111)
            apply_axes_theme(ax, theme)
            if df is not None:
                col = df.columns[0]
                ax.bar(
                    range(len(df)),
                    df[col].values,
                    color=theme["colours"]["primary"],
                    alpha=theme["bar"]["alpha"],
                    linewidth=theme["bar"]["edge_width"],
                )
                ax.set_xticks(range(len(df)))
                ax.set_xticklabels(
                    [str(idx) for idx in df.index],
                    rotation=theme["axis"]["x_label_rotation"],
                    ha=theme["axis"]["x_label_ha"],
                    fontsize=theme["font"]["tick_label_size"],
                    color=theme["colours"]["text"],
                )
            else:
                assert x_data is not None and series_data is not None
                y_vals = next(iter(series_data.values()))
                ax.bar(
                    range(len(x_data)),
                    y_vals,
                    color=theme["colours"]["primary"],
                    alpha=theme["bar"]["alpha"],
                )
                ax.set_xticks(range(len(x_data)))
                ax.set_xticklabels(
                    [str(x) for x in x_data],
                    rotation=theme["axis"]["x_label_rotation"],
                    ha=theme["axis"]["x_label_ha"],
                    fontsize=theme["font"]["tick_label_size"],
                    color=theme["colours"]["text"],
                )
            _set_axis_labels(ax, title, x_label, y_label, theme)

        elif chart_type == "grouped_bar":
            ax = fig.add_subplot(111)
            apply_axes_theme(ax, theme)
            if df is not None:
                n_series = len(df.columns)
                n_groups = len(df)
                bar_width = 0.8 / n_series
                x_pos = np.arange(n_groups)
                for i, col in enumerate(df.columns):
                    offset = (i - n_series / 2 + 0.5) * bar_width
                    ax.bar(
                        x_pos + offset,
                        df[col].values,
                        width=bar_width,
                        color=get_series_colour(theme, i),
                        alpha=theme["bar"]["alpha"],
                        label=str(col),
                    )
                ax.set_xticks(x_pos)
                ax.set_xticklabels(
                    [str(idx) for idx in df.index],
                    rotation=theme["axis"]["x_label_rotation"],
                    ha=theme["axis"]["x_label_ha"],
                    fontsize=theme["font"]["tick_label_size"],
                    color=theme["colours"]["text"],
                )
            else:
                assert x_data is not None and series_data is not None
                n_series = len(series_data)
                n_groups = len(x_data)
                bar_width = 0.8 / n_series
                x_pos = np.arange(n_groups)
                for i, (name, y_vals) in enumerate(series_data.items()):
                    offset = (i - n_series / 2 + 0.5) * bar_width
                    ax.bar(
                        x_pos + offset,
                        y_vals,
                        width=bar_width,
                        color=get_series_colour(theme, i),
                        alpha=theme["bar"]["alpha"],
                        label=name,
                    )
                ax.set_xticks(x_pos)
                ax.set_xticklabels(
                    [str(x) for x in x_data],
                    rotation=theme["axis"]["x_label_rotation"],
                    ha=theme["axis"]["x_label_ha"],
                    fontsize=theme["font"]["tick_label_size"],
                    color=theme["colours"]["text"],
                )
            ax.legend(
                loc=theme["legend"]["location"],
                frameon=theme["legend"]["frame_on"],
                fontsize=theme["font"]["legend_size"],
                labelcolor=theme["colours"]["text"],
            )
            _set_axis_labels(ax, title, x_label, y_label, theme)

        elif chart_type == "scatter":
            ax = fig.add_subplot(111)
            apply_axes_theme(ax, theme)
            if df is not None:
                for i, col in enumerate(df.columns):
                    ax.scatter(
                        df.index,
                        df[col].values,
                        color=get_series_colour(theme, i),
                        s=40,
                        label=str(col),
                    )
                if len(df.columns) > 1:
                    ax.legend(
                        loc=theme["legend"]["location"],
                        frameon=theme["legend"]["frame_on"],
                        fontsize=theme["font"]["legend_size"],
                        labelcolor=theme["colours"]["text"],
                    )
            else:
                assert x_data is not None and series_data is not None
                for i, (name, y_vals) in enumerate(series_data.items()):
                    ax.scatter(x_data, y_vals, color=get_series_colour(theme, i), s=40, label=name)
                if len(series_data) > 1:
                    ax.legend(
                        loc=theme["legend"]["location"],
                        frameon=theme["legend"]["frame_on"],
                        fontsize=theme["font"]["legend_size"],
                        labelcolor=theme["colours"]["text"],
                    )
            _set_axis_labels(ax, title, x_label, y_label, theme)

        fig.tight_layout()

        # ------------------------------------------------------------------
        # 4. Convert to Base64 PNG
        # ------------------------------------------------------------------
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            dpi=theme["layout"]["figure_dpi"],
            facecolor=theme["colours"]["background"],
            bbox_inches="tight",
        )
        # fig was created with Figure() directly, not through pyplot's figure
        # manager, so plt.close() would have no effect. Let it go out of scope.
        buf.seek(0)
        image_base64 = base64.b64encode(buf.read()).decode("utf-8")

    except Exception as exc:  # noqa: BLE001 — tool must not raise
        logger.exception("generate_chart: rendering failed for '%s'.", title)
        return f"Chart generation failed: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 5. Return artefact envelope
    # ------------------------------------------------------------------
    return json.dumps(
        {
            "__artifact__": "chart",
            "chart_format": "png",
            "image_base64": image_base64,
            "caption": title,
            "llm_response": (
                f'Chart "{title}" ({chart_type}) has been generated and displayed in the chat.'
            ),
        }
    )


def _set_axis_labels(ax: object, title: str, x_label: str, y_label: str, theme: dict) -> None:
    """Apply title and axis labels to a non-pie chart axes.

    Args:
        ax: A matplotlib Axes instance.
        title: Chart title.
        x_label: X-axis label (empty string to skip).
        y_label: Y-axis label (empty string to skip).
        theme: The full chart theme dict.
    """
    colours = theme["colours"]
    font = theme["font"]
    ax.set_title(  # type: ignore[union-attr]
        title,
        color=colours["text"],
        fontsize=font["title_size"],
        fontweight=font["weight_title"],
        pad=theme["layout"]["title_pad"],
        fontfamily=font["family"],
    )
    if x_label:
        ax.set_xlabel(x_label, color=colours["text"], fontsize=font["axis_label_size"])  # type: ignore[union-attr]
    if y_label:
        ax.set_ylabel(y_label, color=colours["text"], fontsize=font["axis_label_size"])  # type: ignore[union-attr]


# ===========================================================================
# render_chart — the generic Plotly chart tool (ADR-0048, Axis 2)
# ===========================================================================


def _values_look_like_dates(values: list) -> bool:
    """Return ``True`` when every non-null value is an ISO date string.

    Used to decide — from the data alone, not from a hardcoded chart
    identity — whether the x axis should be a Plotly ``date`` axis.

    Args:
        values: The x-column values, in row order.

    Returns:
        ``True`` if at least one value is present and all present
        values are ISO date / datetime strings; ``False`` otherwise.
    """
    seen = False
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
            return False
        seen = True
    return seen


def _column_is_numeric(rows: list[list], idx: int) -> bool:
    """Return ``True`` when every non-null value in a column is a real number.

    ``bool`` is rejected even though it is an ``int`` subclass — the
    catalogue's ``is_active`` flag must not be treated as a plottable
    series.

    Args:
        rows: The envelope's row list.
        idx: The column position to inspect.

    Returns:
        ``True`` if at least one value is present and all present
        values are ``int`` / ``float`` (and not ``bool``).
    """
    seen = False
    for row in rows:
        value = row[idx]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        seen = True
    return seen


def _as_float(value: object) -> float | None:
    """Coerce a cell value to ``float``; ``None`` / non-numeric become ``None``.

    A ``None`` y value is a legitimate gap in a Plotly line — it is
    preserved, not dropped.
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _build_xy_trace(chart_type: str, *, name: str, x: list, y: list, colour: str) -> dict:
    """Build one Plotly trace for the line / scatter / bar chart families.

    Args:
        chart_type: One of ``line``, ``scatter``, ``bar``,
            ``grouped_bar``.
        name: Legend label for the trace.
        x: X-axis values.
        y: Y-axis values (``None`` entries render as gaps).
        colour: Hex colour for the trace.

    Returns:
        A Plotly trace dict.
    """
    if chart_type == "line":
        return {
            "type": "scatter",
            "mode": "lines",
            "name": name,
            "x": x,
            "y": y,
            "line": {"color": colour, "width": 2},
        }
    if chart_type == "scatter":
        return {
            "type": "scatter",
            "mode": "markers",
            "name": name,
            "x": x,
            "y": y,
            "marker": {"color": colour, "size": 8},
        }
    # bar / grouped_bar — both are Plotly "bar" traces; grouped_bar
    # only differs in the layout-level ``barmode``.
    return {
        "type": "bar",
        "name": name,
        "x": x,
        "y": y,
        "marker": {"color": colour},
    }


def _coerce_x_for_mpl(x_raw: list, is_dates: bool) -> list:
    """Coerce x-axis values into a matplotlib-plottable sequence.

    Mirrors ``render_chart``'s data-driven axis choice for the raster
    sibling: ISO date strings become real timestamps so series with
    *different* date coverage align on one continuous axis; otherwise
    numeric values are used as-is, and anything else falls back to ordinal
    positions (so a categorical x still renders rather than raising).

    Args:
        x_raw: The x values for one series, in row order.
        is_dates: Whether the x column looks like ISO dates (decided once
            for the whole envelope by :func:`_values_look_like_dates`).

    Returns:
        A list parallel to ``x_raw`` suitable for ``ax.plot`` / ``ax.scatter``.
    """
    if is_dates:
        return pd.to_datetime(pd.Series(x_raw), errors="coerce").tolist()
    floats = [_as_float(v) for v in x_raw]
    if all(f is not None for f in floats):
        return floats
    return list(range(len(x_raw)))


def _render_envelope_to_png(
    *,
    chart_type: str,
    title: str,
    columns: list,
    rows: list,
    x_col: str,
    y_cols: list[str],
    series_column: str,
    x_label: str,
    y_label: str,
) -> str:
    """Render the resolved envelope selection to a Base64 PNG.

    Best-effort raster sibling of :func:`render_chart`'s Plotly spec, for
    surfaces (Telegram) that cannot render a Plotly figure. Returns an
    empty string on any failure — never raises, so the Plotly path the web
    surface depends on is unaffected.

    It mirrors :func:`generate_chart`'s themed-matplotlib rendering (Agg
    canvas, theme application, ``savefig`` → Base64) but resolves the data
    the same way :func:`render_chart` already did: by column selection over
    ``columns`` / ``rows``. Unlike ``generate_chart``'s inline path it
    handles a ``series_column`` whose series each have their *own* x values
    (e.g. NAV ``actual`` vs ``plan`` with different date coverage) — one
    trace per distinct series value, plotted against that subset's own x.

    Using matplotlib here is permitted: the ADR-0042 guard
    (``tests/regression/test_no_matplotlib_in_web.py``) scans only ``web/``
    and ``services/chart_specs/``, not ``chart_tools.py``.

    Args:
        chart_type: One of :data:`_VALID_CHART_TYPES`.
        title: Chart title.
        columns: The envelope's column names.
        rows: The envelope's rows (row-major, parallel to ``columns``).
        x_col: The x-axis column (pie/donut labels).
        y_cols: The plotted value column(s).
        series_column: Optional column partitioning rows into one trace per
            distinct value (only the first ``y_cols`` entry is plotted).
        x_label: X-axis label.
        y_label: Y-axis label.

    Returns:
        A Base64-encoded PNG string, or ``""`` on any failure or for a
        shape this helper cannot render.
    """
    try:
        if chart_type not in _VALID_CHART_TYPES:
            return ""

        theme = get_chart_theme()

        col_idx = {name: i for i, name in enumerate(columns)}
        if x_col not in col_idx or not y_cols:
            return ""
        x_idx = col_idx[x_col]
        x_values = [row[x_idx] for row in rows]

        fig = create_themed_figure(theme, width=6.0, height_px=400)
        # Attach the Agg canvas explicitly so savefig renders headlessly.
        FigureCanvasAgg(fig)

        if chart_type in {"pie", "donut"}:
            value_idx = col_idx[y_cols[0]]
            labels = ["" if v is None else str(v) for v in x_values]
            values = [(_as_float(row[value_idx]) or 0.0) for row in rows]
            ax = fig.add_subplot(111)
            wedge_colours = [get_series_colour(theme, i) for i in range(len(labels))]
            _wedges, texts, autotexts = ax.pie(
                values,
                labels=labels,
                colors=wedge_colours,
                autopct="%1.1f%%",
                startangle=90,
            )
            for text in texts:
                text.set_color(theme["colours"]["text"])
            for autotext in autotexts:
                autotext.set_color(theme["colours"]["text"])
            if chart_type == "donut":
                ax.add_artist(Circle((0, 0), 0.5, fc=theme["colours"]["background"]))
            ax.set_aspect("equal")
            ax.set_title(
                title,
                color=theme["colours"]["text"],
                fontsize=theme["font"]["title_size"],
                fontweight=theme["font"]["weight_title"],
                pad=theme["layout"]["title_pad"],
                fontfamily=theme["font"]["family"],
            )
        else:
            ax = fig.add_subplot(111)
            apply_axes_theme(ax, theme)

            # Build the (name, x, y) series list. With a series_column each
            # series keeps its own x; without one, every y column shares the
            # single envelope x.
            series: list[tuple[str, list, list]] = []
            if series_column:
                if series_column not in col_idx:
                    return ""
                series_idx = col_idx[series_column]
                y_idx = col_idx[y_cols[0]]
                seen: list = []
                for row in rows:
                    sv = row[series_idx]
                    if sv not in seen:
                        seen.append(sv)
                for sv in seen:
                    subset = [row for row in rows if row[series_idx] == sv]
                    series.append(
                        (
                            "" if sv is None else str(sv),
                            [r[x_idx] for r in subset],
                            [_as_float(r[y_idx]) for r in subset],
                        )
                    )
            else:
                for y_col in y_cols:
                    y_idx = col_idx[y_col]
                    series.append((y_col, x_values, [_as_float(row[y_idx]) for row in rows]))

            if chart_type in {"bar", "grouped_bar"}:
                cats = ["" if v is None else str(v) for v in x_values]
                n_groups = len(cats)
                if chart_type == "bar":
                    _name, _sx, y_vals = series[0]
                    ax.bar(
                        range(n_groups),
                        [(y or 0.0) for y in y_vals],
                        color=theme["colours"]["primary"],
                        alpha=theme["bar"]["alpha"],
                    )
                else:
                    n_series = max(len(series), 1)
                    bar_width = 0.8 / n_series
                    x_pos = np.arange(n_groups)
                    for i, (name, _sx, y_vals) in enumerate(series):
                        offset = (i - n_series / 2 + 0.5) * bar_width
                        ax.bar(
                            x_pos + offset,
                            [(y or 0.0) for y in y_vals],
                            width=bar_width,
                            color=get_series_colour(theme, i),
                            alpha=theme["bar"]["alpha"],
                            label=name,
                        )
                    ax.legend(
                        loc=theme["legend"]["location"],
                        frameon=theme["legend"]["frame_on"],
                        fontsize=theme["font"]["legend_size"],
                        labelcolor=theme["colours"]["text"],
                    )
                ax.set_xticks(range(n_groups))
                ax.set_xticklabels(
                    cats,
                    rotation=theme["axis"]["x_label_rotation"],
                    ha=theme["axis"]["x_label_ha"],
                    fontsize=theme["font"]["tick_label_size"],
                    color=theme["colours"]["text"],
                )
            else:
                is_dates = _values_look_like_dates(x_values)
                handles = []
                labels = []
                for i, (name, sx, y_vals) in enumerate(series):
                    x_plot = _coerce_x_for_mpl(sx, is_dates)
                    colour = get_series_colour(theme, i)
                    if chart_type == "scatter":
                        handle = ax.scatter(x_plot, y_vals, color=colour, s=40, label=name)
                    else:
                        (handle,) = ax.plot(
                            x_plot,
                            y_vals,
                            color=colour,
                            linewidth=theme["line"]["width_primary"],
                            label=name,
                        )
                    handles.append(handle)
                    labels.append(name)
                if len(labels) > 1 or (len(labels) == 1 and labels[0]):
                    ax.legend(
                        handles,
                        labels,
                        loc=theme["legend"]["location"],
                        frameon=theme["legend"]["frame_on"],
                        fontsize=theme["font"]["legend_size"],
                        labelcolor=theme["colours"]["text"],
                    )
                if is_dates:
                    fig.autofmt_xdate()

            _set_axis_labels(ax, title, x_label, y_label, theme)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            dpi=theme["layout"]["figure_dpi"],
            facecolor=theme["colours"]["background"],
            bbox_inches="tight",
        )
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except Exception:  # noqa: BLE001 — best-effort: never break the Plotly path
        logger.debug(
            "render_chart: best-effort PNG render failed for '%s'; returning empty image.",
            title,
            exc_info=True,
        )
        return ""


def render_chart(
    chart_type: str,
    title: str,
    data_handle: str,
    x_column: str = "",
    y_columns: list[str] | None = None,
    series_column: str = "",
    x_label: str = "",
    y_label: str = "",
) -> str:
    """Render a cached structured-data envelope as a themed Plotly figure spec.

    The presentation half of the two-axis chart architecture
    (ADR-0048, Axis 2). It resolves the structured-data envelope
    ``get_investment_data`` cached server-side — looked up by the
    ``data_handle`` that tool returned, never received as an argument
    (ADR-0048, amended) — and emits a Plotly figure spec themed via
    :func:`services.chart_specs.base.layout_from_theme`, so Shirley's
    charts match the web pages' charts. It never reads the database or
    the DataStore itself.

    The return value is a JSON artefact envelope detected by the
    streaming core. The spec is stripped before forwarding to the LLM;
    the model only receives the short ``llm_response`` confirmation.

    The envelope also carries a best-effort ``image_base64`` PNG of the
    same selection (see :func:`_render_envelope_to_png`), so raster
    surfaces such as Telegram can show the chart while the web surface
    keeps using the interactive ``spec``. Rendering the PNG with
    matplotlib is permitted here because the ADR-0042 matplotlib guard
    scans only ``web/`` and ``services/chart_specs/`` — not this module.
    A PNG-render failure leaves ``image_base64`` empty and never affects
    the Plotly spec.

    Args:
        chart_type: One of ``"line"``, ``"bar"``, ``"grouped_bar"``,
            ``"scatter"``, ``"pie"``, ``"donut"``.
        title: Chart title displayed above the chart.
        data_handle: The handle returned by ``get_investment_data``.
            The chart data is looked up server-side from it — the rows
            never travel through the model. Handles are valid only
            within the current turn.
        x_column: Which column is the x axis (or the pie/donut
            labels). Defaults to the first column.
        y_columns: Which column(s) are the plotted series. Defaults to
            every numeric column that is not the x or series column.
        series_column: Optional. A column whose distinct values
            partition the rows into one trace each — this is how a
            "plan vs actual" overlay is produced generically (e.g.
            ``series_column="nav_kind"``). When set, only the first
            ``y_columns`` entry is plotted.
        x_label: X-axis label. Defaults to ``x_column``.
        y_label: Y-axis label. Defaults to the sole y column's name.

    Returns:
        A JSON artefact envelope on success, or a plain explanatory
        string on any validation failure. Never raises.
    """
    # ------------------------------------------------------------------
    # 1. Validate chart type and resolve the structured-data envelope
    # ------------------------------------------------------------------
    if chart_type not in _VALID_CHART_TYPES:
        return f"Invalid chart_type '{chart_type}'. Valid types: {sorted(_VALID_CHART_TYPES)}."
    data = get_tool_data(data_handle)
    if data is None:
        return (
            f"No data found for handle '{data_handle}'. Call a "
            "data-producing tool first (e.g. get_investment_data or "
            "get_saa_hypothetical_comparison), then pass the handle it "
            "returns here. (Handles are valid only within the current turn.)"
        )
    if not isinstance(data, dict) or data.get("__data__") not in _ACCEPTED_DATA_DISCRIMINATORS:
        return (
            "The data for that handle is not a structured-data envelope. "
            "Call a data-producing tool first (e.g. get_investment_data or "
            "get_saa_hypothetical_comparison) and pass the handle it "
            "returns here."
        )
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return "The structured-data envelope is malformed: 'columns' and 'rows' must both be lists."
    if not rows:
        return "The structured-data envelope has no rows — there is nothing to chart."

    # ------------------------------------------------------------------
    # 2. Resolve and validate the column selection
    # ------------------------------------------------------------------
    x_col = x_column or columns[0]
    if x_col not in columns:
        return f"x_column '{x_col}' is not in the data columns {columns}."
    if series_column and series_column not in columns:
        return f"series_column '{series_column}' is not in the data columns {columns}."

    col_idx = {name: i for i, name in enumerate(columns)}
    reserved = {x_col}
    if series_column:
        reserved.add(series_column)

    if y_columns:
        missing = [c for c in y_columns if c not in columns]
        if missing:
            return f"y_columns {missing} are not in the data columns {columns}."
        y_cols = [c for c in y_columns if c not in reserved]
    else:
        y_cols = [c for c in columns if c not in reserved and _column_is_numeric(rows, col_idx[c])]
    if not y_cols:
        return (
            "No numeric y column found to plot. Pass y_columns "
            f"explicitly (available columns: {columns})."
        )

    # ------------------------------------------------------------------
    # 3. Build the figure spec (pure dict construction — never raises
    #    on bad data shapes; wrapped defensively all the same).
    # ------------------------------------------------------------------
    try:
        theme = get_plotly_chart_theme()
        palette = theme["colours"].get("series_palette") or [theme["colours"]["primary"]]
        x_idx = col_idx[x_col]
        x_values = [row[x_idx] for row in rows]

        if chart_type in {"pie", "donut"}:
            value_col = y_cols[0]
            value_idx = col_idx[value_col]
            labels = ["" if v is None else str(v) for v in x_values]
            values = [(_as_float(row[value_idx]) or 0.0) for row in rows]
            trace: dict = {
                "type": "pie",
                "labels": labels,
                "values": values,
                "marker": {"colors": palette},
                "textinfo": "label+percent",
                "hovertemplate": "%{label}: %{value} (%{percent})<extra></extra>",
            }
            if chart_type == "donut":
                trace["hole"] = 0.5
            traces = [trace]
            layout = layout_from_theme(title=title, xlabel="", ylabel="", show_legend=True)
        else:
            traces = []
            if series_column:
                series_idx = col_idx[series_column]
                y_col = y_cols[0]
                y_idx = col_idx[y_col]
                # Preserve first-seen order of the series values so the
                # legend order is stable across calls.
                series_values: list = []
                for row in rows:
                    sv = row[series_idx]
                    if sv not in series_values:
                        series_values.append(sv)
                for i, sv in enumerate(series_values):
                    subset = [row for row in rows if row[series_idx] == sv]
                    traces.append(
                        _build_xy_trace(
                            chart_type,
                            name="" if sv is None else str(sv),
                            x=[row[x_idx] for row in subset],
                            y=[_as_float(row[y_idx]) for row in subset],
                            colour=palette[i % len(palette)],
                        )
                    )
            else:
                for i, y_col in enumerate(y_cols):
                    y_idx = col_idx[y_col]
                    traces.append(
                        _build_xy_trace(
                            chart_type,
                            name=y_col,
                            x=x_values,
                            y=[_as_float(row[y_idx]) for row in rows],
                            colour=palette[i % len(palette)],
                        )
                    )

            layout = layout_from_theme(
                title=title,
                xlabel=x_label or x_col,
                ylabel=y_label or (y_cols[0] if len(y_cols) == 1 else ""),
                show_legend=len(traces) > 1,
            )
            # Axis formatters — driven by the data, not a hardcoded
            # chart identity. ``layout_from_theme`` defaults both axes
            # to the SAA percentage formatter; investment data is dates
            # on x and raw amounts on y.
            if _values_look_like_dates(x_values):
                layout["xaxis"]["type"] = "date"
                layout["xaxis"]["tickformat"] = "%Y-%m-%d"
            else:
                layout["xaxis"]["tickformat"] = ""
            layout["yaxis"]["tickformat"] = ",.2f"
            if chart_type == "grouped_bar":
                layout["barmode"] = "group"

        config = {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        }
        spec = {"data": traces, "layout": layout, "config": config}
    except Exception as exc:  # noqa: BLE001 — tool must not raise
        logger.exception("render_chart: spec construction failed for '%s'.", title)
        return f"Chart rendering failed: {type(exc).__name__}: {exc}"

    # Best-effort raster sibling of the Plotly spec, for surfaces that
    # cannot render a Plotly figure (Telegram). The streaming core forwards
    # both ``spec`` and ``image_base64`` on the chart artifact, so the web
    # surface keeps using the interactive spec while Telegram uses the PNG —
    # no change to the model, the core, or the bot handler. A failure here
    # yields "" and never disturbs the spec above.
    image_base64 = _render_envelope_to_png(
        chart_type=chart_type,
        title=title,
        columns=columns,
        rows=rows,
        x_col=x_col,
        y_cols=y_cols,
        series_column=series_column,
        x_label=x_label or x_col,
        y_label=y_label or (y_cols[0] if len(y_cols) == 1 else ""),
    )

    return json.dumps(
        {
            "__artifact__": "chart",
            "chart_format": "plotly",
            "spec": spec,
            "image_base64": image_base64,
            "caption": title,
            "llm_response": (
                f'Chart "{title}" ({chart_type}) has been generated and displayed in the chat.'
            ),
        }
    )


# ---------------------------------------------------------------------------
# Register tool at import time
# ---------------------------------------------------------------------------

_registry = get_tool_registry()

_registry.register_tool(
    name="generate_chart",
    function=generate_chart,
    description=(
        "Generate a themed chart and display it in the chat. "
        "Supports line, bar, grouped_bar, scatter, pie, and donut chart types. "
        "Data can come from the DataStore (use list_datasets to discover available data) "
        "or be provided inline as JSON. "
        "Always provide a descriptive title. "
        "For DataStore data, specify the datastore_key and optionally filter by columns "
        "and date_range. "
        "For inline data, provide the data directly in inline_data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["line", "bar", "grouped_bar", "scatter", "pie", "donut"],
                "description": "The type of chart to generate.",
            },
            "title": {
                "type": "string",
                "description": "Chart title displayed above the chart.",
            },
            "data_source": {
                "type": "string",
                "enum": ["datastore", "inline"],
                "description": (
                    "Where the data comes from: 'datastore' to read from a loaded "
                    "dataset, 'inline' to use data provided directly."
                ),
            },
            "x_label": {
                "type": "string",
                "description": "Label for the X axis. Ignored for pie/donut charts.",
            },
            "y_label": {
                "type": "string",
                "description": "Label for the Y axis. Ignored for pie/donut charts.",
            },
            "datastore_key": {
                "type": "string",
                "description": (
                    "DataStore key to fetch data from. Required when data_source is "
                    "'datastore'. Use the list_datasets tool to discover available keys."
                ),
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Column names to plot from the DataStore dataset. "
                    "If omitted, all numeric columns are used."
                ),
            },
            "inline_data": {
                "type": "object",
                "description": (
                    "Data provided directly as JSON. Required when data_source is 'inline'. "
                    'For line/bar/scatter: {"x": [...], "y": [...]} or '
                    '{"x": [...], "series": {"Name1": [...], "Name2": [...]}}'
                    '. For pie/donut: {"labels": [...], "values": [...]}.'
                ),
            },
            "date_range": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "Start date (ISO format YYYY-MM-DD, inclusive).",
                    },
                    "end": {
                        "type": "string",
                        "description": "End date (ISO format YYYY-MM-DD, inclusive).",
                    },
                },
                "description": (
                    "Optional date range filter. Only applies when data_source is "
                    "'datastore' and the dataset has a DatetimeIndex."
                ),
            },
        },
        "required": ["chart_type", "title", "data_source"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="render_chart",
    function=render_chart,
    description=(
        "Render structured data as an interactive themed chart displayed "
        "in the chat. This is the charting path for the web assistant: "
        "first call get_investment_data to fetch a data bundle — it "
        "returns a data handle — then pass that handle as the "
        "'data_handle' argument here. The chart data is looked up "
        "server-side from the handle; you never copy the rows. Supports "
        "line, bar, grouped_bar, scatter, pie, and donut charts. By "
        "default the first column is the x axis and every numeric column "
        "is plotted; override with x_column and y_columns. Use "
        "series_column to split rows into one series per distinct value "
        "of a column — e.g. series_column='nav_kind' overlays plan vs "
        "actual on one chart. Prefer this tool over generate_chart for "
        "investment data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["line", "bar", "grouped_bar", "scatter", "pie", "donut"],
                "description": "The type of chart to generate.",
            },
            "title": {
                "type": "string",
                "description": "Chart title displayed above the chart.",
            },
            "data_handle": {
                "type": "string",
                "description": (
                    "The handle returned by get_investment_data. The "
                    "chart data is looked up server-side from it; the "
                    "rows never travel through the model. Handles are "
                    "valid only within the current turn."
                ),
            },
            "x_column": {
                "type": "string",
                "description": (
                    "Which column of the data is the x axis (or the "
                    "pie/donut labels). Defaults to the first column."
                ),
            },
            "y_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Which column(s) to plot as series. If omitted, every "
                    "numeric column that is not the x or series column is "
                    "plotted."
                ),
            },
            "series_column": {
                "type": "string",
                "description": (
                    "Optional. A column whose distinct values split the "
                    "rows into one series each (e.g. 'nav_kind' for a "
                    "plan-vs-actual overlay). When set, only the first "
                    "y_columns entry is plotted."
                ),
            },
            "x_label": {
                "type": "string",
                "description": "Label for the X axis. Defaults to x_column.",
            },
            "y_label": {
                "type": "string",
                "description": (
                    "Label for the Y axis. Defaults to the y column name "
                    "when a single series is plotted."
                ),
            },
        },
        "required": ["chart_type", "title", "data_handle"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)
