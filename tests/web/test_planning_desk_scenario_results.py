# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Scenario Analysis result view models (S34.5, ADR-0104 §5/§7).

DB-free: the route's presentation builders
(:func:`web.routes.planning_desk._build_scenario_context` and friends) are pure
functions over a hand-built :class:`ScenarioResult`, so they can be pinned as the
projection they are — no client, no session, no book. What is pinned:

* **The KPI pair formats the DTO, never recomputes it** — the baseline/scenario
  strings are the DTO values, the delta badge is the DTO's own delta, and the
  tone is favourability (a rise is good only where a rise is the good move).
* **The headroom row** — the bar tone is the engine's status, the Δ is the
  negative of the utilisation change (headroom shrinks as utilisation grows).
* **The composition diff** — assembled from the ``CompositionPair``, fund by
  fund, with the weight delta as the signal.
* **The empty overlay** — every delta is the zero of its kind and the tone is
  neutral (baseline ≡ scenario, no misleading gap).

The two result templates render from these contexts without error, and the
error context renders a notice rather than a chart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from jinja2 import Environment, FileSystemLoader, select_autoescape

import web.routes.planning_desk as route
from services.analytics.portfolio_aggregation import (
    FundCompositionBreakdown,
    FundCompositionRow,
)
from services.planning_desk.scenario_results import (
    CompositionPair,
    FamilyHeadroomDelta,
    HeadroomClassDelta,
    KpiDelta,
    ScenarioResult,
    ScenarioSeriesPair,
)

_D = Decimal
_GRID = (date(2025, 12, 31), date(2026, 6, 30), date(2026, 9, 30), date(2026, 12, 31))
_LABELS = ["Q4 2025", "Q2 2026", "Q3 2026", "Q4 2026"]


def _templates() -> Environment:
    return Environment(
        loader=FileSystemLoader("web/templates"),
        autoescape=select_autoescape(["html"]),
    )


def _result(*, empty: bool = False) -> ScenarioResult:
    """A scored result. ``empty`` collapses the two worlds (the empty overlay)."""
    nav_scen = (
        (_D("200e6"), _D("205e6"), _D("214e6"), _D("238.6e6"))
        if empty
        else (_D("200e6"), _D("205e6"), _D("196e6"), _D("219.2e6"))
    )
    ret_scen = (None, 100.0, 106.0, 114.8) if empty else (None, 100.0, 98.0, 107.9)
    nav = ScenarioSeriesPair(
        _GRID, 2, (_D("200e6"), _D("205e6"), _D("214e6"), _D("238.6e6")), nav_scen
    )
    ret = ScenarioSeriesPair(_GRID, 2, (None, 100.0, 106.0, 114.8), ret_scen)
    if empty:
        kpis = (
            KpiDelta("aum", "AUM", "functional_currency", _D("212.4e6"), _D("212.4e6"), _D("0")),
            KpiDelta("limit_breaches", "Breaches", "count", 0, 0, 0),
        )
        headroom: tuple[FamilyHeadroomDelta, ...] = ()
    else:
        kpis = (
            KpiDelta(
                "aum",
                "AUM (Σ NAV incl. cash)",
                "functional_currency",
                _D("212.4e6"),
                _D("198.7e6"),
                _D("-13.7e6"),
            ),
            KpiDelta(
                "tightest_anlv_headroom",
                "Tightest AnlV headroom",
                "functional_currency",
                _D("4.8e6"),
                _D("1.9e6"),
                _D("-2.9e6"),
            ),
            KpiDelta(
                "functional_cash_t0_plus_4q",
                "Cash (functional, t₀+4Q)",
                "functional_currency",
                _D("21.5e6"),
                _D("19.1e6"),
                _D("-2.4e6"),
            ),
            KpiDelta("limit_breaches", "Limit breaches (plan horizon)", "count", 0, 1, 1),
        )
        headroom = (
            FamilyHeadroomDelta(
                "anlv",
                (
                    HeadroomClassDelta(
                        "anlv",
                        "listed_equity",
                        _D("30.2"),
                        _D("33.1"),
                        _D("2.9"),
                        _D("4.8e6"),
                        _D("1.9e6"),
                        _D("-2.9e6"),
                        "OK",
                        "WARN",
                    ),
                ),
            ),
        )
    comp = CompositionPair(
        baseline=FundCompositionBreakdown(
            rows=[
                FundCompositionRow(None, "Alpha Fund", 120e6, 56.0, 56.0, 0.12),
                FundCompositionRow(None, "Beta Fund", 90e6, 44.0, 100.0, 0.08),
            ]
        ),
        scenario=FundCompositionBreakdown(
            rows=[
                FundCompositionRow(None, "Alpha Fund", 110e6, 52.4, 52.4, 0.11),
                FundCompositionRow(None, "Beta Fund", 100e6, 47.6, 100.0, 0.09),
            ]
        ),
    )
    return ScenarioResult(nav, ret, kpis, headroom, comp)


def _context(result: ScenarioResult | None, **kw) -> dict:
    return route._build_scenario_context(
        result=result,
        functional_currency=kw.get("currency", "EUR"),
        labels=_LABELS,
        composition_query="t0_kind=fx_shock&t0_currency=USD&t0_magnitude=-10",
        error=kw.get("error"),
    )["scenario"]


# ---------------------------------------------------------------------------
# §5.4 — the KPI strip
# ---------------------------------------------------------------------------


def test_the_kpi_pair_formats_the_dto_without_recompute() -> None:
    """The money tile shows the DTO's own figures and its own delta."""
    scenario = _context(_result())
    aum = scenario["kpis"][0]

    assert aum["base"] == "212.4m EUR", "the baseline is the DTO value, in millions"
    assert aum["scen"] == "198.7m EUR", "the scenario is the DTO value, in millions"
    assert aum["delta"] == "−13.7m EUR", "the badge is the DTO's own delta"
    assert aum["tone"] == "neg", "less AUM is the adverse move"


def test_the_breach_tile_renders_the_count_delta() -> None:
    """The breach tile is a count — the DTO models the delta numerically."""
    breaches = _context(_result())["kpis"][3]
    assert breaches["base"] == "0"
    assert breaches["scen"] == "1"
    assert breaches["delta"] == "+1"
    assert breaches["tone"] == "neg", "more breaches is the adverse move"


# ---------------------------------------------------------------------------
# §5.5 — the headroom table
# ---------------------------------------------------------------------------


def test_the_headroom_row_bar_tone_follows_the_engine_status() -> None:
    """The bar colour is the scenario status, mapped to a token modifier."""
    row = _context(_result())["headroom"][0]["rows"][0]
    assert row["label"] == "AnlV — Listed equity"
    assert row["baseline_util"] == "30.2%"
    assert row["scenario_util"] == "33.1%"
    assert row["bar_pct"] == "33", "the bar width is the scenario utilisation"
    assert row["bar_tone"] == "warn", "the WARN status drives the bar colour"


def test_the_headroom_delta_is_the_negative_of_the_utilisation_change() -> None:
    """Headroom shrinks as utilisation grows: Δ headroom = −Δ utilisation."""
    row = _context(_result())["headroom"][0]["rows"][0]
    # Utilisation rose +2.9pp, so headroom fell −2.9pp — the adverse move.
    assert row["delta"] == "−2.9pp"
    assert row["delta_tone"] == "neg"


# ---------------------------------------------------------------------------
# §5.6 — the composition diff
# ---------------------------------------------------------------------------


def test_the_composition_view_diffs_the_pair() -> None:
    """Each fund's baseline and scenario share, and the weight delta."""
    rows = route._build_composition_context(_result().composition)["composition"]["rows"]
    by_name = {r["name"]: r for r in rows}

    assert by_name["Alpha Fund"]["baseline"] == "56.0%"
    assert by_name["Alpha Fund"]["scenario"] == "52.4%"
    assert by_name["Alpha Fund"]["delta"] == "−3.6pp"
    assert by_name["Beta Fund"]["delta"] == "+3.6pp"


# ---------------------------------------------------------------------------
# §5.7 — the empty overlay
# ---------------------------------------------------------------------------


def test_the_empty_overlay_renders_zero_deltas_and_neutral_tone() -> None:
    """Baseline ≡ scenario: every delta is the zero of its kind, tone neutral."""
    scenario = _context(_result(empty=True))

    for kpi in scenario["kpis"]:
        assert kpi["tone"] == "zero"
        assert kpi["delta"].startswith("±")
    # The panel footers agree too — no gap to read.
    assert scenario["scenario_foot"]["nav_tone"] == "zero"
    assert scenario["scenario_foot"]["ret_tone"] == "zero"


def test_a_single_currency_book_needs_no_per_currency_machinery() -> None:
    """The result region is Σ-only, so a one-currency book renders unbroken."""
    scenario = _context(_result(), currency="EUR")
    assert scenario["baseline_foot"]["nav"].endswith("EUR")
    assert scenario["kpis"], "the KPI strip renders regardless of currency count"


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_the_result_partial_renders_the_pair_kpi_and_headroom() -> None:
    """The OOB result region draws the two chart targets, the strip, the table."""
    env = _templates()
    html = env.get_template("_partials/planning_desk_scenario_results.html").render(
        scenario=_context(_result())
    )

    assert 'id="pd-sa-results"' in html
    assert 'hx-swap-oob="outerHTML"' in html
    assert 'id="pd-sa-chart-baseline"' in html
    assert 'id="pd-sa-chart-scenario"' in html
    assert "pd-kpi__scen" in html
    assert "pd-deltatable" in html
    assert "pd-bar__fill--warn" in html
    # The composition drill-down is a lazy shell, not loaded inline.
    assert 'hx-trigger="revealed"' in html
    assert "/api/planning-desk/scenario-composition" in html


def test_the_error_context_renders_a_notice_not_a_chart() -> None:
    """A scenario that could not be scored says why, and draws no chart."""
    env = _templates()
    context = _context(None, error="No FX rate for USD → EUR")
    html = env.get_template("_partials/planning_desk_scenario_results.html").render(
        scenario=context
    )

    assert "could not be scored" in html
    assert "No FX rate for USD" in html
    assert 'id="pd-sa-chart-scenario"' not in html


def test_the_composition_partial_renders_the_diff_table() -> None:
    """The lazy composition partial draws the per-fund share diff."""
    env = _templates()
    context = route._build_composition_context(_result().composition)
    html = env.get_template("_partials/planning_desk_composition.html").render(**context)

    assert "Alpha Fund" in html
    assert "Beta Fund" in html
    assert "pd-deltatable" in html
