# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Load the inputs the deltas-first scenario assembly consumes (ADR-0104 §5).

The DB seam in front of :func:`services.planning_desk.scenario_results.assemble_scenario_result`,
which is pure and takes an already-loaded :class:`ScenarioResultInputs`. This
module is the loader that composes that container from the repositories — the
counterpart to :func:`services.investments.cash_flow_timeline.load_cash_flow_planning_inputs`
for the Cash Flow Planning lens, and it is called **beside** it: the Scenario
Analysis lens reuses the cash-flow inputs' baseline frames and converter, and
adds what the coverage / composition / return engines additionally need — the
classified universe, the realised NAV and cashflow histories, and the two limit
families.

**Position currency in, functional out at the assembly.** The NAV and cashflow
histories are loaded **unconverted** (position currency). The assembly restates
them per world at the ADR-0099 §4 boundary — the baseline through the converter,
the scenario through the ``fx_shock``-restated copy — so this loader must not
convert them here (that would double-convert, and it would collapse the two
worlds' FX into one).

**The grid is the cash-flow lens's grid.** ``evaluation_dates`` and ``cut_over``
are derived by the caller from the :class:`CashFlowPlanningResult` the Cash Flow
Planning lens already produced (its period ends and its seam), so the two lenses
state one period grid and the impact chart aligns column-for-column with the
cash-flow timeline.

Per the layering rules this loader lives in ``services/`` and imports only from
``core/`` and ``services/``; the pure assembly it feeds
(:mod:`services.planning_desk.scenario_results`) stays DB-free.
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowDTO,
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.limits_repository import LimitsRepository
from services.analytics._dtos import (
    InvestmentWithClassCodeDTO,
    LimitSetWithLimitsDTO,
)
from services.planning_desk.scenario_results import ScenarioResultInputs

if TYPE_CHECKING:  # pragma: no cover - types only
    from services.investments.cash_flow_timeline import CashFlowPlanningInputs

_SAA: str = "saa"
_ANLV: str = "anlv"


async def load_scenario_result_inputs(
    *,
    cash_flow_inputs: CashFlowPlanningInputs,
    evaluation_dates: list[_date],
    cut_over: _date,
    investments: InvestmentRepository,
    navs: InvestmentNavRepository,
    cashflows: InvestmentCashflowRepository,
    asset_classes: AssetClassRepository,
    limits: LimitsRepository,
    warn_threshold_pct: Decimal = Decimal("90.0"),
) -> ScenarioResultInputs:
    """Compose a :class:`ScenarioResultInputs` for the active tenant.

    Every repository must be tenant-scoped (the caller obtains them via
    :func:`core.repositories.tenant_context`); RLS hides foreign-tenant rows.

    Args:
        cash_flow_inputs: The Cash Flow Planning inputs already loaded for this
            request — its :attr:`~CashFlowPlanningInputs.baseline` frames and
            :attr:`~CashFlowPlanningInputs.converter` are reused unchanged, so
            the two lenses share one plan world and one conversion seam.
        evaluation_dates: The period-end grid, ascending — the cash-flow lens's
            grid (its period ends), spanning the actual segment and the plan
            horizon.
        cut_over: The plan/actual seam t₀ (ADR-0060) — the cash-flow lens's
            ``seam_date``.
        investments: Investment repository (the full active universe).
        navs: NAV repository (realised streams, position currency).
        cashflows: Cashflow repository (realised streams, position currency).
        asset_classes: Asset-class repository (the class-code snapshot the
            coverage engine classifies on).
        limits: Limits repository (the SAA and AnlV set histories).
        warn_threshold_pct: The coverage WARN floor, forwarded to the engine.

    Returns:
        The :class:`ScenarioResultInputs` the pure assembly consumes.
    """
    active = await investments.list_active()
    class_code_by_id = {ac.id: ac.code for ac in await asset_classes.list_all()}
    classified = [
        InvestmentWithClassCodeDTO(
            investment=inv,
            asset_class_code=class_code_by_id.get(inv.asset_class_id),
        )
        for inv in active
    ]

    investment_ids = [inv.id for inv in active]
    actual_navs = await navs.list_by_investments_and_kind(investment_ids, "actual")
    actual_cashflow_dtos = await cashflows.list_by_investments_and_kind(investment_ids, "actual")
    actual_cashflows = {
        investment_id: _cashflow_frame(rows) for investment_id, rows in actual_cashflow_dtos.items()
    }

    saa_sets = await _load_family_sets(limits, _SAA)
    anlv_sets = await _load_family_sets(limits, _ANLV)

    return ScenarioResultInputs(
        baseline=cash_flow_inputs.baseline,
        converter=cash_flow_inputs.converter,
        investments=classified,
        actual_navs=actual_navs,
        actual_cashflows=actual_cashflows,
        saa_sets=saa_sets,
        anlv_sets=anlv_sets,
        evaluation_dates=evaluation_dates,
        cut_over=cut_over,
        warn_threshold_pct=warn_threshold_pct,
    )


def _cashflow_frame(rows: list[InvestmentCashflowDTO]) -> pd.DataFrame:
    """Project realised cashflow DTOs into the flat ``(flow_timestamp, amount)``.

    The shape the assembly's return-index and composition seams consume
    (:func:`services.planning_desk.scenario_results._performance_cashflows`,
    :func:`~services.planning_desk.scenario_results._split_converted_flows`):
    signed amounts in **position currency**, converted per world inside the
    assembly. An investment with no realised flow yields an empty, correctly
    typed frame.
    """
    if not rows:
        return pd.DataFrame(columns=["flow_timestamp", "amount"])
    return pd.DataFrame(
        {
            "flow_timestamp": [row.flow_timestamp for row in rows],
            "amount": [row.amount for row in rows],
        }
    )


async def _load_family_sets(limits: LimitsRepository, family: str) -> list[LimitSetWithLimitsDTO]:
    """Compose the ``LimitSetWithLimitsDTO`` list for one limit family.

    ``LimitsRepository.list_sets(family=...)`` returns sets ascending by
    ``effective_from`` (ADR-0056 §Selection); the coverage engine relies on that
    order. Mirrors :meth:`services.limits.LimitsCoverageService._load_family_sets`
    — the two surfaces load the same shape, and a shared abstraction is not worth
    the coupling for a twelve-line compose loop.
    """
    composed: list[LimitSetWithLimitsDTO] = []
    for limit_set in await limits.list_sets(family=family):
        rows = await limits.list_limits(limit_set.id)
        composed.append(
            LimitSetWithLimitsDTO(
                set=limit_set,
                limits={row.class_key: row.max_pct for row in rows},
            )
        )
    return composed


__all__ = ["load_scenario_result_inputs"]
