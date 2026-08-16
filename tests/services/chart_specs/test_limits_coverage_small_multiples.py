# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for the Investment Limits small-multiples chart spec.

Full-fidelity dict diffs would be brittle; these tests assert the
subplot count, step-line shape, axis ranges, Decimal → float
boundary, and empty-input handling that the route consumer relies
on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from services.chart_specs import build_limits_coverage_spec
from services.chart_specs.base import get_chart_theme


def _D(value: str | int | float) -> Decimal:
    return Decimal(str(value))


def _build_coverage_df(
    rows: list[tuple[date, str, Decimal | None, float, str]],
) -> pd.DataFrame:
    """Build a coverage DataFrame in the engine's schema.

    Each row tuple is ``(as_of_date, class_key, max_pct,
    coverage_pct, status)`` — the columns the spec actually consumes.
    """
    return pd.DataFrame(
        [
            {
                "as_of_date": pd.Timestamp(d),
                "class_key": cls,
                "max_pct": mp,
                "nav_sum_eur": _D("100000"),
                "coverage_pct": _D(str(cov)),
                "headroom_eur": _D("50000"),
                "status": status,
            }
            for d, cls, mp, cov, status in rows
        ]
    )


# ---------------------------------------------------------------------------
# Empty / sentinel handling
# ---------------------------------------------------------------------------


def test_empty_coverage_df_produces_themed_empty_spec() -> None:
    spec = build_limits_coverage_spec(
        pd.DataFrame(
            columns=[
                "as_of_date",
                "class_key",
                "max_pct",
                "nav_sum_eur",
                "coverage_pct",
                "headroom_eur",
                "status",
            ]
        ),
        limit_step_lines={},
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    assert spec["data"] == []
    # The dark theme template was applied (paper_bgcolor populated).
    assert "paper_bgcolor" in spec["layout"]
    annotations = spec["layout"]["annotations"]
    assert len(annotations) == 1
    assert "No coverage data" in annotations[0]["text"]


def test_no_limit_classes_not_charted() -> None:
    # Two rows for the same Stichtag: one limited, one NO_LIMIT.
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
            (date(2024, 1, 31), "rogue", None, 5.0, "NO_LIMIT"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    # One subplot → two traces (coverage + limit).
    assert len(spec["data"]) == 2
    annotation_texts = [a["text"] for a in spec["layout"]["annotations"]]
    assert "equities" in annotation_texts
    assert "rogue" not in annotation_texts


def test_unallocated_class_not_charted() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
            (date(2024, 1, 31), "unallocated", None, 1.0, "UNALLOCATED"),
        ]
    )
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines={"equities": [(date(2022, 1, 1), _D("30.0"))]},
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    annotation_texts = [a["text"] for a in spec["layout"]["annotations"]]
    assert "unallocated" not in annotation_texts


# ---------------------------------------------------------------------------
# Subplot structure
# ---------------------------------------------------------------------------


def test_spec_has_subplot_per_limited_class() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
            (date(2024, 1, 31), "bonds", _D("40.0"), 38.0, "WARN"),
            (date(2024, 1, 31), "alts", _D("10.0"), 3.0, "OK"),
        ]
    )
    step_lines = {
        "equities": [(date(2022, 1, 1), _D("30.0"))],
        "bonds": [(date(2022, 1, 1), _D("40.0"))],
        "alts": [(date(2022, 1, 1), _D("10.0"))],
    }
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    # Three subplots × 2 traces each = 6 traces.
    assert len(spec["data"]) == 6
    # xaxis + xaxis2 + xaxis3 (the first axis is named just "xaxis").
    assert "xaxis" in spec["layout"]
    assert "xaxis2" in spec["layout"]
    assert "xaxis3" in spec["layout"]
    assert "yaxis" in spec["layout"]
    assert "yaxis2" in spec["layout"]
    assert "yaxis3" in spec["layout"]


def test_limit_step_line_uses_hv_shape() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {
        "equities": [
            (date(2022, 1, 1), _D("30.0")),
            (date(2023, 1, 1), _D("25.0")),
        ]
    }
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    # The limit trace is the second trace per subplot.
    limit_trace = spec["data"][1]
    assert limit_trace["line"]["shape"] == "hv"
    assert limit_trace["line"]["dash"] == "dash"


def test_limit_step_line_extends_to_to_date() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    to = date(2024, 12, 31)
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=to,
    )
    limit_trace = spec["data"][1]
    assert limit_trace["x"][-1] == to.isoformat()
    assert limit_trace["y"][-1] == 30.0


def test_step_line_gap_when_class_removed_from_set() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "alts", _D("10.0"), 3.0, "OK"),
        ]
    )
    # Class "alts" was present in set #1 then removed in set #2 —
    # the service emitted a trailing None marker.
    step_lines = {
        "alts": [
            (date(2022, 1, 1), _D("10.0")),
            (date(2023, 1, 1), None),
        ]
    }
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    limit_trace = spec["data"][1]
    # The trailing None survives as a Plotly gap marker; no extra
    # extension to to_date because the most-recent transition was None.
    assert limit_trace["y"][-1] is None
    assert limit_trace["x"][-1] == date(2023, 1, 1).isoformat()


# ---------------------------------------------------------------------------
# Axis ranges
# ---------------------------------------------------------------------------


def test_y_axis_range_includes_max_coverage_and_max_limit() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 27.5, "WARN"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    y_range = spec["layout"]["yaxis"]["range"]
    # Range must accommodate max(coverage=27.5, limit=30.0) plus headroom.
    assert y_range[0] == 0.0
    assert y_range[1] >= 30.0


def test_x_axis_range_spans_from_to() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
            (date(2024, 6, 30), "equities", _D("30.0"), 14.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    to = date(2024, 12, 31)
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=to,
    )
    x_range = spec["layout"]["xaxis"]["range"]
    assert x_range[0] == date(2024, 1, 31).isoformat()
    assert x_range[1] == to.isoformat()


# ---------------------------------------------------------------------------
# Decimal → float boundary
# ---------------------------------------------------------------------------


def test_decimal_values_converted_to_float() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    # No Decimal survives into either trace.
    for trace in spec["data"]:
        for v in trace.get("y", []):
            assert v is None or isinstance(v, float)


# ---------------------------------------------------------------------------
# Theme applied
# ---------------------------------------------------------------------------


def test_spec_uses_canonical_theme_colours() -> None:
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    colours = get_chart_theme()["colours"]
    coverage_trace, limit_trace = spec["data"]
    assert coverage_trace["line"]["color"] == colours["primary"]
    assert limit_trace["line"]["color"] == colours["text"]


# ---------------------------------------------------------------------------
# Readability: coverage area fill + dynamic figure height
# ---------------------------------------------------------------------------


def _build_n_class_inputs(
    n: int,
) -> tuple[pd.DataFrame, dict[str, list[tuple[date, Decimal | None]]]]:
    """Build an ``n``-class coverage DataFrame plus matching step lines.

    Each class carries one Stichtag row and a single step-line transition,
    enough for the spec to materialise ``n`` subplots.
    """
    class_keys = [f"class_{i:02d}" for i in range(n)]
    df = _build_coverage_df(
        [(date(2024, 1, 31), cls, _D("30.0"), 12.0, "OK") for cls in class_keys]
    )
    step_lines: dict[str, list[tuple[date, Decimal | None]]] = {
        cls: [(date(2022, 1, 1), _D("30.0"))] for cls in class_keys
    }
    return df, step_lines


def test_coverage_trace_has_filled_area_below() -> None:
    """The coverage trace fills the area down to zero with alpha 0.18."""
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )

    coverage_traces = [t for t in spec["data"] if t.get("name") == "Coverage"]
    assert len(coverage_traces) == 1
    trace = coverage_traces[0]

    assert trace["fill"] == "tozeroy"
    assert trace["fillcolor"].startswith("rgba(")
    assert trace["fillcolor"].endswith(", 0.18)")


def test_limit_trace_has_no_fill() -> None:
    """The dashed limit line stays a pure stroke — no area fill."""
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )

    limit_traces = [t for t in spec["data"] if t.get("name") == "Limit"]
    assert len(limit_traces) == 1
    trace = limit_traces[0]
    # Plotly defaults to "none" when fill is unset; allow both forms.
    assert trace.get("fill", "none") == "none"


def test_coverage_fillcolor_matches_primary_with_alpha() -> None:
    """The coverage fill colour is colours['primary'] at alpha 0.18."""
    df = _build_coverage_df(
        [
            (date(2024, 1, 31), "equities", _D("30.0"), 12.0, "OK"),
        ]
    )
    step_lines = {"equities": [(date(2022, 1, 1), _D("30.0"))]}
    spec = build_limits_coverage_spec(
        df,
        limit_step_lines=step_lines,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    coverage_trace = next(t for t in spec["data"] if t.get("name") == "Coverage")

    # The primary colour from chart_theme.json is #E8304A → rgb(232, 48, 74).
    assert coverage_trace["fillcolor"] == "rgba(232, 48, 74, 0.18)"


def test_figure_height_scales_with_number_of_rows() -> None:
    """Layout.height = max(450, n_rows * 220)."""
    # One-class input → 1 row → floor of 450.
    df_small, step_lines_small = _build_n_class_inputs(1)
    spec_small = build_limits_coverage_spec(
        df_small,
        limit_step_lines=step_lines_small,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    assert spec_small["layout"]["height"] == 450

    # Seven-class input → ceil(7/3) = 3 rows → 3 * 220 = 660.
    df_large, step_lines_large = _build_n_class_inputs(7)
    spec_large = build_limits_coverage_spec(
        df_large,
        limit_step_lines=step_lines_large,
        family_label="SAA",
        to_date=date(2024, 12, 31),
    )
    assert spec_large["layout"]["height"] == 660


def test_hex_to_rgba_helper_converts_correctly() -> None:
    """_hex_to_rgba parses #RRGGBB and emits rgba(...)."""
    from services.chart_specs.limits_coverage_small_multiples import (
        _hex_to_rgba,
    )

    assert _hex_to_rgba("#E8304A", 0.18) == "rgba(232, 48, 74, 0.18)"
    assert _hex_to_rgba("#000000", 1.0) == "rgba(0, 0, 0, 1.0)"
    assert _hex_to_rgba("#FFFFFF", 0.0) == "rgba(255, 255, 255, 0.0)"


def test_hex_to_rgba_rejects_short_hex() -> None:
    """Three-digit hex is not accepted (we use the canonical six-digit form)."""
    from services.chart_specs.limits_coverage_small_multiples import (
        _hex_to_rgba,
    )

    with pytest.raises(ValueError, match="six-digit hex"):
        _hex_to_rgba("#FFF", 0.5)
