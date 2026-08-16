# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.chart_specs.investment_nav_timeseries``.

The spec builder is a pure function that turns an
:class:`InvestmentDTO` plus a list of :class:`InvestmentNavDTO` rows
into a Plotly figure dict. Tests exercise:

* Top-level shape (``data`` / ``layout`` / ``config`` keys).
* Trace count (always 2: actual + plan), correct names and dash
  styles.
* Theme propagation into the layout colours.
* Date and currency conventions on the axes.
* That an empty NAV list still produces a stable two-trace shape.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from services.chart_specs import build_nav_timeseries_spec
from services.chart_specs.base import get_chart_theme


def _make_investment(currency: str = "EUR", name: str = "Test Fund") -> InvestmentDTO:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return InvestmentDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        name=name,
        investment_type="private_equity",
        asset_class_id=uuid4(),
        manager_name="Test GP",
        region="Europe",
        currency=currency,
        vintage_year=2020,
        commitment_amount=Decimal("100000.00"),
        is_active=True,
        type_specific_data=None,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _make_nav(investment_id, kind: str, as_of: date, value: str) -> InvestmentNavDTO:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return InvestmentNavDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        investment_id=investment_id,
        as_of_date=as_of,
        nav_value=Decimal(value),
        currency="EUR",
        nav_kind=kind,
        source=None,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def investment_with_navs() -> tuple[InvestmentDTO, list[InvestmentNavDTO]]:
    investment = _make_investment()
    navs = [
        _make_nav(investment.id, "actual", date(2024, 12, 31), "1000.00"),
        _make_nav(investment.id, "actual", date(2025, 6, 30), "1100.00"),
        _make_nav(investment.id, "actual", date(2025, 12, 31), "1200.00"),
        _make_nav(investment.id, "plan", date(2024, 12, 31), "1000.00"),
        _make_nav(investment.id, "plan", date(2025, 12, 31), "1300.00"),
    ]
    return investment, navs


def test_top_level_keys(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs)
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_trace_count_and_names(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs)
    assert len(spec["data"]) == 2
    names = [trace["name"] for trace in spec["data"]]
    assert "Actual" in names
    assert "Plan" in names


def test_actual_solid_plan_dashed(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs)
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    plan = next(t for t in spec["data"] if t["name"] == "Plan")
    # The actual trace has no `dash` key (solid is the Plotly default).
    assert "dash" not in actual["line"]
    assert plan["line"]["dash"] == "dash"


def test_traces_sorted_ascending_by_date(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    # Reverse the list to confirm the function sorts internally.
    spec = build_nav_timeseries_spec(investment, list(reversed(navs)))
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    assert actual["x"] == [
        "2024-12-31",
        "2025-06-30",
        "2025-12-31",
    ]
    assert actual["y"] == [1000.0, 1100.0, 1200.0]


def test_theme_colours_match_canonical_theme(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    theme = get_chart_theme()
    spec = build_nav_timeseries_spec(investment, navs)
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]
    assert spec["layout"]["plot_bgcolor"] == theme["colours"]["plot_area"]


def test_y_axis_currency_label_contains_investment_currency(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs)
    assert spec["layout"]["yaxis"]["title"]["text"] == "NAV (EUR)"


def test_x_axis_is_date_typed(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs)
    assert spec["layout"]["xaxis"]["type"] == "date"
    assert spec["layout"]["xaxis"]["tickformat"] == "%Y-%m-%d"


def test_empty_nav_list_still_produces_two_traces() -> None:
    investment = _make_investment(currency="USD", name="Fresh Fund")
    spec = build_nav_timeseries_spec(investment, [])
    assert len(spec["data"]) == 2
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    plan = next(t for t in spec["data"] if t["name"] == "Plan")
    assert actual["x"] == []
    assert actual["y"] == []
    assert plan["x"] == []
    assert plan["y"] == []
    # Currency on the axis label still reflects the investment.
    assert spec["layout"]["yaxis"]["title"]["text"] == "NAV (USD)"


def test_only_actual_navs_renders_empty_plan_trace() -> None:
    investment = _make_investment()
    navs = [
        _make_nav(investment.id, "actual", date(2025, 1, 1), "100.00"),
        _make_nav(investment.id, "actual", date(2025, 6, 30), "120.00"),
    ]
    spec = build_nav_timeseries_spec(investment, navs)
    plan = next(t for t in spec["data"] if t["name"] == "Plan")
    assert plan["x"] == []
    assert plan["y"] == []
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    assert len(actual["x"]) == 2


def test_pure_function_deterministic(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec_a = build_nav_timeseries_spec(investment, navs)
    spec_b = build_nav_timeseries_spec(investment, navs)
    assert spec_a == spec_b


def test_title_includes_investment_name(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs)
    assert investment.name in spec["layout"]["title"]["text"]


# ---------------------------------------------------------------------------
# ADR-0113 §1 — unified right axis end ("universe as-of")
# ---------------------------------------------------------------------------


def test_axis_end_omitted_leaves_spec_unchanged(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    """The default is byte-identical to the pre-ADR-0113 output."""
    investment, navs = investment_with_navs
    baseline = build_nav_timeseries_spec(investment, navs)
    assert baseline == build_nav_timeseries_spec(investment, navs, axis_end=None)
    assert "autorangeoptions" not in baseline["layout"]["xaxis"]


def test_axis_end_extends_the_x_axis_autorange(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_navs
    spec = build_nav_timeseries_spec(investment, navs, axis_end=date(2026, 3, 31))
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2026-03-31"}


def test_axis_end_leaves_the_data_untouched(
    investment_with_navs: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    """The axis end extends the range only — it never adds a datapoint."""
    investment, navs = investment_with_navs
    without = build_nav_timeseries_spec(investment, navs)
    with_end = build_nav_timeseries_spec(investment, navs, axis_end=date(2026, 3, 31))
    assert with_end["data"] == without["data"]


def test_axis_end_not_applied_when_there_are_no_navs() -> None:
    spec = build_nav_timeseries_spec(_make_investment(), [], axis_end=date(2026, 3, 31))
    assert "autorangeoptions" not in spec["layout"]["xaxis"]


# ---------------------------------------------------------------------------
# ADR-0113 §2 — plan-tail display trace
# ---------------------------------------------------------------------------


def _plan_trace(spec: dict) -> dict:
    return next(t for t in spec["data"] if t["name"] == "Plan")


@pytest.fixture
def investment_with_plan_tail() -> tuple[InvestmentDTO, list[InvestmentNavDTO]]:
    """Actuals through 2025-06-30, plan rows straddling that boundary."""
    investment = _make_investment()
    navs = [
        _make_nav(investment.id, "actual", date(2024, 12, 31), "1000.00"),
        _make_nav(investment.id, "actual", date(2025, 6, 30), "1100.00"),
        # Before / at the last actual — inside the solid line's period.
        _make_nav(investment.id, "plan", date(2024, 12, 31), "980.00"),
        _make_nav(investment.id, "plan", date(2025, 6, 30), "1050.00"),
        # The tail proper.
        _make_nav(investment.id, "plan", date(2025, 9, 30), "1150.00"),
        _make_nav(investment.id, "plan", date(2025, 12, 31), "1200.00"),
        # Beyond the unified axis end.
        _make_nav(investment.id, "plan", date(2026, 6, 30), "1400.00"),
    ]
    return investment, navs


def test_plan_tail_end_omitted_leaves_spec_unchanged(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    """The default draws the full plan horizon — the detail-page behaviour."""
    investment, navs = investment_with_plan_tail
    baseline = build_nav_timeseries_spec(investment, navs)
    assert baseline == build_nav_timeseries_spec(investment, navs, plan_tail_end=None)
    plan = _plan_trace(baseline)
    assert plan["x"] == ["2024-12-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-06-30"]
    assert "opacity" not in plan


def test_plan_tail_filters_to_the_window_and_prepends_the_anchor(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_plan_tail
    spec = build_nav_timeseries_spec(investment, navs, plan_tail_end=date(2025, 12, 31))
    plan = _plan_trace(spec)
    # Anchor (the last actual) + the two rows inside the window. Rows at
    # or before the last actual and beyond the tail end are dropped.
    assert plan["x"] == ["2025-06-30", "2025-09-30", "2025-12-31"]
    assert plan["y"] == [1100.0, 1150.0, 1200.0]


def test_plan_tail_anchor_equals_the_last_actual_point(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    """The dashed line starts exactly where the solid line ends."""
    investment, navs = investment_with_plan_tail
    spec = build_nav_timeseries_spec(investment, navs, plan_tail_end=date(2025, 12, 31))
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    plan = _plan_trace(spec)
    assert plan["x"][0] == actual["x"][-1]
    assert plan["y"][0] == actual["y"][-1]


def test_plan_tail_leaves_the_actual_trace_untouched(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_plan_tail
    without = build_nav_timeseries_spec(investment, navs)
    with_tail = build_nav_timeseries_spec(investment, navs, plan_tail_end=date(2025, 12, 31))
    actual_without = next(t for t in without["data"] if t["name"] == "Actual")
    actual_with = next(t for t in with_tail["data"] if t["name"] == "Actual")
    assert actual_with == actual_without


def test_plan_tail_without_actuals_has_an_open_lower_bound_and_no_anchor() -> None:
    investment = _make_investment()
    navs = [
        _make_nav(investment.id, "plan", date(2025, 3, 31), "100.00"),
        _make_nav(investment.id, "plan", date(2025, 9, 30), "120.00"),
        _make_nav(investment.id, "plan", date(2026, 6, 30), "140.00"),
    ]
    spec = build_nav_timeseries_spec(investment, navs, plan_tail_end=date(2025, 12, 31))
    plan = _plan_trace(spec)
    # Every plan row up to the tail end qualifies; nothing is prepended.
    assert plan["x"] == ["2025-03-31", "2025-09-30"]
    assert plan["y"] == [100.0, 120.0]


def test_plan_tail_empty_after_filtering_renders_an_empty_trace(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    """No plan row beyond the last actual: the line ends, nothing is drawn."""
    investment, navs = investment_with_plan_tail
    spec = build_nav_timeseries_spec(investment, navs, plan_tail_end=date(2025, 6, 30))
    plan = _plan_trace(spec)
    assert plan["x"] == []
    assert plan["y"] == []
    # The trace survives for legend stability.
    assert len(spec["data"]) == 2


def test_plan_tail_styling_is_dashed_muted_and_same_hue_as_the_actual(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    investment, navs = investment_with_plan_tail
    spec = build_nav_timeseries_spec(investment, navs, plan_tail_end=date(2025, 12, 31))
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    plan = _plan_trace(spec)
    assert plan["line"]["dash"] == "dash"
    assert plan["line"]["color"] == actual["line"]["color"]
    assert plan["opacity"] == 0.65
    assert plan["name"] == "Plan"
    assert plan["hovertemplate"].endswith("(Plan)<extra></extra>")
    assert "<b>Plan</b>" not in plan["hovertemplate"]


def test_plan_tail_and_axis_end_are_independent_parameters(
    investment_with_plan_tail: tuple[InvestmentDTO, list[InvestmentNavDTO]],
) -> None:
    """The data window and the axis range are separate concerns."""
    investment, navs = investment_with_plan_tail
    spec = build_nav_timeseries_spec(
        investment,
        navs,
        axis_end=date(2026, 3, 31),
        plan_tail_end=date(2025, 12, 31),
    )
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2026-03-31"}
    assert _plan_trace(spec)["x"][-1] == "2025-12-31"
