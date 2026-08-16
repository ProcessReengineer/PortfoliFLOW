# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.investment_multiples``.

The Charts-module variant (``style="lines"``) renders TVPI and DPI
as two line traces with a 1.0× breakeven reference shape. The
Portfolio-Review stacked-bar variant is deferred to sub-stream 5e
and must raise ``NotImplementedError``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from services.chart_specs import build_multiples_spec
from services.chart_specs.base import get_chart_theme


def _sample_multiples() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "as_of_date": [date(2024, 12, 31), date(2025, 6, 30)],
            "tvpi": [1.05, 1.20],
            "dpi": [0.10, 0.30],
            "rvpi": [0.95, 0.90],
        }
    )


def _sample_irr() -> pd.Series:
    return pd.Series(
        [0.05, 0.12],
        index=[date(2024, 12, 31), date(2025, 6, 30)],
    )


def test_lines_style_has_two_line_traces() -> None:
    spec = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X", style="lines")
    assert len(spec["data"]) == 2
    names = [t["name"] for t in spec["data"]]
    assert "TVPI" in names
    assert "DPI" in names
    for trace in spec["data"]:
        assert trace["type"] == "scatter"
        assert "lines" in trace["mode"]


def test_stacked_bars_style_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        build_multiples_spec(
            _sample_multiples(),
            _sample_irr(),
            "Fund X",
            style="stacked_bars",
        )


def test_unknown_style_raises_value_error() -> None:
    with pytest.raises(ValueError):
        build_multiples_spec(
            _sample_multiples(),
            _sample_irr(),
            "Fund X",
            style="cucumber",  # type: ignore[arg-type]
        )


def test_default_style_is_lines() -> None:
    """No explicit ``style`` argument defaults to the Charts-module variant."""
    spec_default = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    spec_lines = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X", style="lines")
    assert spec_default == spec_lines


def test_y_axis_uses_x_suffix() -> None:
    spec = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    assert spec["layout"]["yaxis"]["ticksuffix"] == "x"


def test_breakeven_reference_shape_at_one_x() -> None:
    spec = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    shapes = spec["layout"]["shapes"]
    assert len(shapes) >= 1
    breakeven = shapes[0]
    assert breakeven["y0"] == 1.0
    assert breakeven["y1"] == 1.0


def test_theme_applied() -> None:
    theme = get_chart_theme()
    spec = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]


def test_title_includes_investment_name() -> None:
    spec = build_multiples_spec(_sample_multiples(), _sample_irr(), "My Fund")
    assert "My Fund" in spec["layout"]["title"]["text"]


def test_empty_multiples_still_produces_two_traces() -> None:
    spec = build_multiples_spec(
        pd.DataFrame(columns=["as_of_date", "tvpi", "dpi", "rvpi"]),
        pd.Series(dtype="float64"),
        "Empty Fund",
    )
    assert len(spec["data"]) == 2
    for trace in spec["data"]:
        assert trace["x"] == []
        assert trace["y"] == []


def test_pure_function_deterministic() -> None:
    a = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    b = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    assert a == b


# ---------------------------------------------------------------------------
# ADR-0113 §1 — unified right axis end ("universe as-of")
# ---------------------------------------------------------------------------


def test_axis_end_omitted_leaves_spec_unchanged() -> None:
    """The default is byte-identical to the pre-ADR-0113 output."""
    baseline = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    assert baseline == build_multiples_spec(
        _sample_multiples(), _sample_irr(), "Fund X", axis_end=None
    )
    assert "autorangeoptions" not in baseline["layout"]["xaxis"]


def test_axis_end_extends_the_x_axis_autorange() -> None:
    spec = build_multiples_spec(
        _sample_multiples(), _sample_irr(), "Fund X", axis_end=date(2025, 12, 31)
    )
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-12-31"}


def test_axis_end_leaves_the_data_untouched() -> None:
    without = build_multiples_spec(_sample_multiples(), _sample_irr(), "Fund X")
    with_end = build_multiples_spec(
        _sample_multiples(), _sample_irr(), "Fund X", axis_end=date(2025, 12, 31)
    )
    assert with_end["data"] == without["data"]


def test_axis_end_not_applied_to_an_empty_figure() -> None:
    spec = build_multiples_spec(
        pd.DataFrame(columns=["as_of_date", "tvpi", "dpi", "rvpi"]),
        pd.Series(dtype="float64"),
        "Empty Fund",
        axis_end=date(2025, 12, 31),
    )
    assert "autorangeoptions" not in spec["layout"]["xaxis"]
