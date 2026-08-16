# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Deltas-first scenario result assembly (ADR-0104 §5).

Synthetic and DB-free: the assembly takes a :class:`ScenarioResultInputs` — the
baseline frames, the converter, the classified universe, the realised histories
and the limit sets — and returns DTOs, so the tests state the plan world they
mean directly rather than seeding it through repositories. This mirrors the
:mod:`tests.services.investments.test_cash_flow_timeline` posture the S34.2 seam
established.

What is pinned here, one section per §5 verification of the S34.3 brief:

* **§5.3 empty-overlay identity** — the scenario built from ``EMPTY_OVERLAY``
  equals the baseline across every DTO (the assembly adds no drift).
* **§5.4 identical-history invariant** — left of t₀ the baseline and scenario
  Σ-NAV pair are equal to the last Decimal (overlays never touch actuals).
* **§5.5 the E5 universe split** — with a non-trivial cash position, the NAV
  line moves with cash while the return index (performance universe) does not.
* **§5.6 the four KPI tiles** — computed against a fixture with a known
  ``market_shock`` and ``fx_shock``, each as a baseline/scenario/delta triple.
* **§5.7 headroom deltas** — two coverage runs give the family tables and their
  deltas, and the tightest-AnlV-headroom KPI equals the AnlV table's minimum
  (the same number reached two ways).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd

from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.limits_repository import LimitSetDTO
from services.analytics._dtos import (
    InvestmentWithClassCodeDTO,
    LimitSetWithLimitsDTO,
)
from services.fx.conversion import FxConverter
from services.fx.functional_currency import PortfolioFxConverter
from services.investments.archetype import Archetype
from services.overlay import (
    EMPTY_OVERLAY,
    FxShock,
    MarketShock,
    PlanFrames,
    PlanInvestment,
)
from services.planning_desk.scenario_results import (
    ScenarioResultInputs,
    assemble_scenario_result,
)

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

#: A quarter-end seam so the grid's arithmetic is readable.
_T0 = date(2026, 6, 30)

#: Two actual quarters, then eight plan quarters — the cash-flow lens grid
#: (ADR-0104 §6), which the assembly's Σ-NAV pair spans so the identical-history
#: invariant has a history to be asserted on.
_GRID: list[date] = [
    date(2025, 12, 31),
    date(2026, 6, 30),
    date(2026, 9, 30),
    date(2026, 12, 31),
    date(2027, 3, 31),
    date(2027, 6, 30),
    date(2027, 9, 30),
    date(2027, 12, 31),
    date(2028, 3, 31),
    date(2028, 6, 30),
]
_PLAN_DATES = [d for d in _GRID if d > _T0]

_D = Decimal


# ---------------------------------------------------------------------------
# Fixture builders — the world, stated directly
# ---------------------------------------------------------------------------


def _investment(
    *,
    name: str,
    investment_type: str,
    currency: str,
    asset_class_code: str | None,
    anlv_code: str | None,
    investment_id: UUID | None = None,
) -> InvestmentWithClassCodeDTO:
    """A classified investment with placeholders for the unread fields."""
    inv = InvestmentDTO(
        id=investment_id or uuid4(),
        tenant_id=_TENANT,
        name=name,
        investment_type=investment_type,
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency=currency,
        vintage_year=None,
        commitment_amount=None,
        is_active=True,
        type_specific_data=None,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
        anlv_code=anlv_code,
    )
    return InvestmentWithClassCodeDTO(investment=inv, asset_class_code=asset_class_code)


def _actual_navs(investment_id: UUID, points: dict[str, str]) -> list[InvestmentNavDTO]:
    """A realised NAV stream in position currency."""
    return [
        InvestmentNavDTO(
            id=uuid4(),
            tenant_id=_TENANT,
            investment_id=investment_id,
            as_of_date=date.fromisoformat(day),
            nav_value=_D(value),
            currency="XXX",
            nav_kind="actual",
            source=None,
            created_by=_USER,
            created_at=_NOW,
            updated_at=_NOW,
        )
        for day, value in points.items()
    ]


def _path(points: dict[str, str]) -> pd.Series:
    """A Decimal-valued balance path — the shape ``plan_world`` lays down."""
    return pd.Series(
        [_D(value) for value in points.values()],
        index=pd.to_datetime([date.fromisoformat(day) for day in points]),
        dtype="object",
    )


def _plan_investments(
    classified: list[InvestmentWithClassCodeDTO],
) -> dict[UUID, PlanInvestment]:
    """The non-cash ``PlanInvestment`` metadata the frames carry."""
    return {
        c.investment.id: PlanInvestment(
            investment_id=c.investment.id,
            currency=c.investment.currency.upper(),
            investment_type=c.investment.investment_type,
        )
        for c in classified
        if c.investment.investment_type != "cash"
    }


def _identity_converter() -> PortfolioFxConverter:
    """The zero-read fast path — every position already functional (EUR)."""
    return PortfolioFxConverter("EUR", None)


def _converter(rates: dict[str, dict[str, str]]) -> PortfolioFxConverter:
    """A EUR-functional, EUR-referenced rate-backed converter."""
    frame = pd.DataFrame(
        [
            {
                "as_of_date": pd.Timestamp(day),
                "currency": currency,
                "rate_to_reference": _D(rate),
                "reference_currency": "EUR",
            }
            for currency, series in rates.items()
            for day, rate in series.items()
        ]
    )
    return PortfolioFxConverter("EUR", FxConverter(frame, "EUR"))


def _set(*, family: str, effective_from: str, limits: dict[str, str]) -> LimitSetWithLimitsDTO:
    """A limit set with per-class ceilings (percentage points)."""
    dto = LimitSetDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        family=family,
        effective_from=date.fromisoformat(effective_from),
        label=f"{family} @ {effective_from}",
        notes=None,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )
    return LimitSetWithLimitsDTO(set=dto, limits={k: _D(v) for k, v in limits.items()})


# ---------------------------------------------------------------------------
# A two-position EUR world: one equity fund + one cash position
# ---------------------------------------------------------------------------


def _eur_world(
    *,
    equity_actual: dict[str, str],
    equity_plan: dict[str, str],
    cash_actual: dict[str, str],
    cash_plan: dict[str, str],
    saa_limits: dict[str, str] | None = None,
    anlv_limits: dict[str, str] | None = None,
) -> ScenarioResultInputs:
    """Assemble inputs for a single-currency (EUR) book — equity + cash."""
    equity = _investment(
        name="Equity Fund",
        investment_type="listed_equity",
        currency="EUR",
        asset_class_code="equity",
        anlv_code="listed_equity",
    )
    cash = _investment(
        name="EUR Cash",
        investment_type="cash",
        currency="EUR",
        asset_class_code="cash",
        anlv_code="cash",
    )
    eq_id = equity.investment.id
    cash_id = cash.investment.id

    frames = PlanFrames(
        t0=_T0,
        value_paths={eq_id: _path(equity_plan)},
        cash_paths={"EUR": _path(cash_plan)},
        plan_flows=(),
        investments=_plan_investments([equity, cash]),
    )
    return ScenarioResultInputs(
        baseline=frames,
        converter=_identity_converter(),
        investments=[equity, cash],
        actual_navs={
            eq_id: _actual_navs(eq_id, equity_actual),
            cash_id: _actual_navs(cash_id, cash_actual),
        },
        actual_cashflows={},
        saa_sets=[
            _set(
                family="saa",
                effective_from="2020-01-01",
                limits=saa_limits or {"equity": "80", "cash": "80"},
            )
        ],
        anlv_sets=[
            _set(
                family="anlv",
                effective_from="2020-01-01",
                limits=anlv_limits or {"listed_equity": "80", "cash": "100"},
            )
        ],
        evaluation_dates=list(_GRID),
        cut_over=_T0,
    )


# ---------------------------------------------------------------------------
# §5.3 — the empty-overlay identity
# ---------------------------------------------------------------------------


def test_empty_overlay_round_trips_to_the_baseline() -> None:
    """Scenario-from-``EMPTY_OVERLAY`` equals the baseline across every DTO."""
    inputs = _eur_world(
        equity_actual={"2025-12-31": "100", "2026-06-30": "100"},
        equity_plan={"2026-09-30": "110"},
        cash_actual={"2025-12-31": "200", "2026-06-30": "200"},
        cash_plan={"2026-06-30": "200", "2026-09-30": "180"},
    )

    result = assemble_scenario_result(inputs, EMPTY_OVERLAY)

    # 2b / 2c: the pairs are identical leg for leg.
    assert result.nav_path.baseline == result.nav_path.scenario
    assert result.return_index.baseline == result.return_index.scenario

    # 2d: every KPI delta is the zero of its kind.
    for kpi in result.kpis:
        assert kpi.baseline == kpi.scenario
        assert kpi.delta == 0

    # 2e: every headroom delta is zero.
    for family in result.headroom:
        for row in family.rows:
            assert row.delta_coverage_pct == 0
            assert row.delta_headroom_eur == 0
            assert row.baseline_status == row.scenario_status

    # 2f: composition is identical.
    assert result.composition.baseline.rows == result.composition.scenario.rows


# ---------------------------------------------------------------------------
# §5.4 — the identical-history invariant on the NAV pair
# ---------------------------------------------------------------------------


def test_nav_pair_identical_left_of_the_seam_under_a_shock() -> None:
    """Left of t₀ the Σ-NAV pair is equal to the last Decimal (§5.4)."""
    inputs = _eur_world(
        equity_actual={"2025-12-31": "100", "2026-06-30": "100"},
        equity_plan={"2026-09-30": "100"},
        cash_actual={"2025-12-31": "200", "2026-06-30": "200"},
        cash_plan={"2026-06-30": "200", "2026-09-30": "200"},
    )

    result = assemble_scenario_result(
        inputs,
        (MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=_D("-30")),),
    )

    seam = result.nav_path.seam_index
    assert seam == 2  # 2025-12-31 and 2026-06-30 are the actual columns
    # check_exact — the actual segment is byte-identical across worlds.
    assert result.nav_path.baseline[:seam] == result.nav_path.scenario[:seam]
    # The scenario diverges strictly right of the seam (equity marked down).
    assert result.nav_path.scenario[seam] < result.nav_path.baseline[seam]


# ---------------------------------------------------------------------------
# §5.5 — the E5 universe split (NAV incl. cash, return index excl. cash)
# ---------------------------------------------------------------------------


def test_nav_line_includes_cash_but_return_index_excludes_it() -> None:
    """A rising cash balance moves the NAV line, not the return index (§5.5)."""
    # Equity NAV flat (return index therefore rebases to a flat 100), cash
    # rising over the plan horizon (the NAV line must climb with it).
    inputs = _eur_world(
        equity_actual={"2025-12-31": "100", "2026-06-30": "100"},
        equity_plan={"2026-09-30": "100"},
        cash_actual={"2025-12-31": "200", "2026-06-30": "200"},
        cash_plan={
            "2026-06-30": "200",
            "2026-09-30": "260",
            "2027-06-30": "320",
        },
    )

    result = assemble_scenario_result(inputs, EMPTY_OVERLAY)
    nav = result.nav_path.baseline
    index = result.return_index.baseline
    seam = result.nav_path.seam_index

    # The NAV line climbs across the plan horizon — cash is in it.
    assert nav[-1] > nav[seam - 1]
    assert nav[seam] == _D("360")  # equity 100 + cash 260 at t0+1Q

    # The return index is flat at 100 over the plan horizon — cash is *not*
    # in it; only the (flat) equity performance drives it.
    plan_index = [value for value in index[seam:] if value is not None]
    assert plan_index, "the plan horizon must carry return-index points"
    assert all(abs(value - 100.0) < 1e-9 for value in plan_index)


# ---------------------------------------------------------------------------
# §5.6 — the four KPI tiles against a market_shock + fx_shock fixture
# ---------------------------------------------------------------------------


def _usd_shock_world() -> ScenarioResultInputs:
    """A USD equity + EUR cash book, sized so the KPIs are hand-computable.

    Equity 100 USD flat; cash 100 EUR flat. Baseline USD/EUR is 1.0, so at any
    plan date the baseline book is 200 EUR (100 equity + 100 cash). A
    ``market_shock`` of −20 % on the equity archetype and an ``fx_shock`` of
    −10 % on USD together take the plan-horizon equity to 100·0.8·0.9 = 72 EUR,
    so the scenario book is 172 EUR.
    """
    equity = _investment(
        name="US Equity",
        investment_type="listed_equity",
        currency="USD",
        asset_class_code="equity",
        anlv_code="listed_equity",
    )
    cash = _investment(
        name="EUR Cash",
        investment_type="cash",
        currency="EUR",
        asset_class_code="cash",
        anlv_code="cash",
    )
    eq_id = equity.investment.id
    cash_id = cash.investment.id

    frames = PlanFrames(
        t0=_T0,
        value_paths={eq_id: _path({"2026-09-30": "100"})},
        cash_paths={"EUR": _path({"2026-06-30": "100"})},
        plan_flows=(),
        investments=_plan_investments([equity, cash]),
    )
    return ScenarioResultInputs(
        baseline=frames,
        converter=_converter({"USD": {"2020-01-01": "1.0"}}),
        investments=[equity, cash],
        actual_navs={
            eq_id: _actual_navs(eq_id, {"2025-12-31": "100", "2026-06-30": "100"}),
            cash_id: _actual_navs(cash_id, {"2025-12-31": "100", "2026-06-30": "100"}),
        },
        actual_cashflows={},
        # SAA cash band is tight enough that the marked-down book breaches it.
        saa_sets=[
            _set(
                family="saa",
                effective_from="2020-01-01",
                limits={"equity": "60", "cash": "55"},
            )
        ],
        anlv_sets=[
            _set(
                family="anlv",
                effective_from="2020-01-01",
                limits={"listed_equity": "50", "cash": "100"},
            )
        ],
        evaluation_dates=list(_GRID),
        cut_over=_T0,
    )


def _kpi(result, key: str):
    """The KPI tile with machine key ``key``."""
    return next(kpi for kpi in result.kpis if kpi.key == key)


def test_kpi_tiles_against_market_and_fx_shock() -> None:
    """The four KPI tiles compute correctly, each a baseline/scenario/delta."""
    inputs = _usd_shock_world()
    overlay = (
        MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=_D("-20")),
        FxShock(currency="USD", magnitude=_D("-10")),
    )

    result = assemble_scenario_result(inputs, overlay)

    # Tile order (ADR-0104 §5, §7).
    assert [kpi.key for kpi in result.kpis] == [
        "aum",
        "tightest_anlv_headroom",
        "functional_cash_t0_plus_4q",
        "limit_breaches",
    ]

    aum = _kpi(result, "aum")
    assert aum.baseline == _D("200")
    assert aum.scenario == _D("172")
    assert aum.delta == _D("-28")

    # Baseline equity sits exactly at its 50 % AnlV ceiling (headroom 0); the
    # mark-down opens it up. Tightest AnlV headroom is the minimum over the
    # AnlV rows at the horizon.
    tightest = _kpi(result, "tightest_anlv_headroom")
    assert tightest.baseline == _D("0.0000")
    assert tightest.scenario == _D("14.0000")
    assert tightest.delta == _D("14.0000")

    cash = _kpi(result, "functional_cash_t0_plus_4q")
    assert cash.baseline == _D("100")
    assert cash.scenario == _D("100")
    assert cash.delta == _D("0")

    # Cash's SAA band (55 %) is breached at every plan date once the book is
    # marked down (cash rises to ~58 % of a smaller book).
    breaches = _kpi(result, "limit_breaches")
    assert breaches.baseline == 0
    assert breaches.scenario == len(_PLAN_DATES)
    assert breaches.delta == len(_PLAN_DATES)


# ---------------------------------------------------------------------------
# §5.7 — headroom deltas, and the tightest-AnlV KPI reached two ways
# ---------------------------------------------------------------------------


def _family(result, family: str):
    return next(f for f in result.headroom if f.family == family)


def test_headroom_family_deltas_and_tightest_kpi_agree() -> None:
    """The AnlV tightest-headroom KPI equals the AnlV table's minimum (§5.7)."""
    inputs = _usd_shock_world()
    overlay = (
        MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=_D("-20")),
        FxShock(currency="USD", magnitude=_D("-10")),
    )

    result = assemble_scenario_result(inputs, overlay)

    anlv = _family(result, "anlv")
    # Both families are present, ordered SAA then AnlV.
    assert [f.family for f in result.headroom] == ["saa", "anlv"]

    # The per-class deltas are the scenario minus the baseline.
    equity_row = next(r for r in anlv.rows if r.class_key == "listed_equity")
    assert equity_row.baseline_headroom_eur == _D("0.0000")
    assert equity_row.scenario_headroom_eur == _D("14.0000")
    assert equity_row.delta_headroom_eur == _D("14.0000")

    # §5.7: the same number two ways — the tightest-AnlV KPI is exactly the
    # minimum headroom over the AnlV family table.
    tightest = _kpi(result, "tightest_anlv_headroom")
    table_min_baseline = min(
        r.baseline_headroom_eur for r in anlv.rows if r.baseline_headroom_eur is not None
    )
    table_min_scenario = min(
        r.scenario_headroom_eur for r in anlv.rows if r.scenario_headroom_eur is not None
    )
    assert tightest.baseline == table_min_baseline
    assert tightest.scenario == table_min_scenario


# ---------------------------------------------------------------------------
# Structural: the grid, the seam, and shared period ends
# ---------------------------------------------------------------------------


def test_pairs_share_the_grid_and_seam() -> None:
    """Both pairs carry the same period ends and seam index (deltas-first)."""
    inputs = _eur_world(
        equity_actual={"2025-12-31": "100", "2026-06-30": "100"},
        equity_plan={"2026-09-30": "100"},
        cash_actual={"2025-12-31": "200", "2026-06-30": "200"},
        cash_plan={"2026-06-30": "200"},
    )
    result = assemble_scenario_result(inputs, EMPTY_OVERLAY)

    assert result.nav_path.period_ends == tuple(_GRID)
    assert result.return_index.period_ends == tuple(_GRID)
    assert result.nav_path.seam_index == result.return_index.seam_index == 2
