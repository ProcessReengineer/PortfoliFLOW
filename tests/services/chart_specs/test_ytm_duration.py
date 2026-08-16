# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.investment_ytm_duration``.

These assert the dual-axis invariants the route consumer relies on:
YTM on the left axis and effective duration on the right, the
``overlaying``/``side`` wiring of ``yaxis2``, the conditional OAS
trace, explicit theming of the secondary axis, and empty-input safety.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from services.chart_specs import build_ytm_duration_spec
from services.chart_specs.base import get_chart_theme


def _analytics_with_oas() -> pd.DataFrame:
    idx = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)]
    return pd.DataFrame(
        {
            "ytm": [0.045, 0.048, 0.050],
            "oas": [0.012, 0.011, 0.013],
            "eff_duration": [5.2, 5.0, 4.8],
        },
        index=idx,
    )


def _analytics_govies() -> pd.DataFrame:
    """A government-bond frame: an ``oas`` column that is entirely NaN."""
    idx = [date(2025, 3, 31), date(2025, 6, 30)]
    return pd.DataFrame(
        {
            "ytm": [0.030, 0.032],
            "oas": [np.nan, np.nan],
            "eff_duration": [7.5, 7.3],
        },
        index=idx,
    )


def _trace_named(spec: dict[str, Any], name: str) -> dict[str, Any] | None:
    matches = [t for t in spec["data"] if t.get("name") == name]
    return matches[0] if matches else None


def test_top_level_keys() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_ytm_on_primary_duration_on_secondary_axis() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    ytm = _trace_named(spec, "YTM")
    duration = _trace_named(spec, "Eff. Duration")
    assert ytm is not None and duration is not None
    assert ytm.get("yaxis", "y") == "y"
    assert duration["yaxis"] == "y2"


def test_secondary_axis_overlay_wiring() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    yaxis2 = spec["layout"]["yaxis2"]
    assert yaxis2["overlaying"] == "y"
    assert yaxis2["side"] == "right"


def test_ytm_percent_scaled() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    ytm = _trace_named(spec, "YTM")
    assert ytm is not None
    assert ytm["y"] == [4.5, 4.8, 5.0]


def test_oas_trace_present_when_column_has_values() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    assert _trace_named(spec, "OAS") is not None
    assert len(spec["data"]) == 3


def test_oas_trace_absent_when_column_all_nan() -> None:
    spec = build_ytm_duration_spec(_analytics_govies(), "Govies Fund")
    assert _trace_named(spec, "OAS") is None
    # Only YTM and duration remain.
    assert len(spec["data"]) == 2


def test_left_axis_uses_percent_suffix() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    assert spec["layout"]["yaxis"]["ticksuffix"] == "%"


def test_secondary_axis_is_themed() -> None:
    theme = get_chart_theme()
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    yaxis2 = spec["layout"]["yaxis2"]
    assert yaxis2["linecolor"] == theme["colours"]["axis_line"]
    assert "tickfont" in yaxis2


def test_legend_shown() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    assert spec["layout"]["showlegend"] is True


def test_empty_dataframe_still_valid_figure() -> None:
    spec = build_ytm_duration_spec(pd.DataFrame(), "Empty Fund")
    # YTM and duration traces with empty arrays; no OAS.
    assert _trace_named(spec, "YTM") is not None
    assert _trace_named(spec, "Eff. Duration") is not None
    assert _trace_named(spec, "OAS") is None
    assert _trace_named(spec, "YTM")["y"] == []
    assert "paper_bgcolor" in spec["layout"]


def test_pure_function_deterministic() -> None:
    spec_a = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    spec_b = build_ytm_duration_spec(_analytics_with_oas(), "Test Fund")
    assert spec_a == spec_b


# ---------------------------------------------------------------------------
# ADR-0113 §1 — unified right axis end ("universe as-of")
# ---------------------------------------------------------------------------


def test_axis_end_omitted_leaves_spec_unchanged() -> None:
    """The default is byte-identical to the pre-ADR-0113 output."""
    baseline = build_ytm_duration_spec(_analytics_with_oas(), "Bond Fund")
    assert baseline == build_ytm_duration_spec(_analytics_with_oas(), "Bond Fund", axis_end=None)
    assert "autorangeoptions" not in baseline["layout"]["xaxis"]


def test_axis_end_extends_the_x_axis_autorange() -> None:
    spec = build_ytm_duration_spec(_analytics_with_oas(), "Bond Fund", axis_end=date(2025, 12, 31))
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-12-31"}


def test_axis_end_leaves_the_data_untouched() -> None:
    without = build_ytm_duration_spec(_analytics_with_oas(), "Bond Fund")
    with_end = build_ytm_duration_spec(
        _analytics_with_oas(), "Bond Fund", axis_end=date(2025, 12, 31)
    )
    assert with_end["data"] == without["data"]


def test_axis_end_not_applied_to_an_empty_figure() -> None:
    spec = build_ytm_duration_spec(
        pd.DataFrame(columns=["ytm", "oas", "eff_duration"]),
        "Empty Fund",
        axis_end=date(2025, 12, 31),
    )
    assert "autorangeoptions" not in spec["layout"]["xaxis"]
