# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the Investment domain — a lazy (PEP 562) façade.

Exposes :class:`InvestmentService` and the aggregated read DTO. Web
routes (sub-stream 4b) and the Excel-import workflow (sub-stream 4c)
both consume this service so the cross-repository orchestration
lives in exactly one place. The Excel-import path also re-exports
:class:`InvestmentExtractionResult` and :class:`UploadNotFoundError`
through this module so route handlers can import a single namespace.

**Why the façade is lazy (ADR-0104 §1).** The package mixes two kinds
of member. Most are DB-coupled: :class:`InvestmentService` and the
materialisation services reach ``core.repositories`` and therefore
SQLAlchemy. A few are pure, stdlib-only formulations that the
*ephemeral* layers are required to reuse rather than restate —
:func:`services.investments.archetype.resolve_archetype` and
:data:`services.investments.flow_type_invariants.OVERLAY_EXEMPT_FLOW_TYPES`,
which ADR-0104 §2 makes mandatory seams for ``services/overlay/``. With
eager re-exports, importing either pure seam first executed this
``__init__`` and so dragged the whole DB-coupled half into
:data:`sys.modules` — the overlay's transitive import graph could no
longer be *proven* free of the book, even though no overlay module
names a repository. A PEP 562 :func:`__getattr__` resolves each name on
first access instead, so the purity proof in
``tests/regression/test_overlay_layer_pure.py`` holds again as a
machine check rather than a source-scan approximation.

The public surface is exactly :data:`__all__`, unchanged by the
laziness: every name it lists resolves on attribute access, direct
submodule imports keep working, and so does submodule attribute access
after ``import services.investments``. Nothing outside the standard
library executes at package import.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - static resolution for mypy / IDEs
    from services.data_normalization import (
        InvestmentExtractionResult,
        UploadNotFoundError,
    )
    from services.investments.aum import (
        CASH_TYPE,
        AumBreakdown,
        build_nav_series,
        compute_aum,
        load_nav_series,
    )
    from services.investments.cash_flow_timeline import (
        DEFAULT_HORIZON_QUARTERS,
        HORIZON_QUARTERS,
        CashFlowPlanningInputs,
        CashFlowPlanningResult,
        CashFlowTimeline,
        Periodisation,
        build_cash_flow_timeline,
        load_cash_flow_planning_inputs,
        project_cash_flow_planning,
    )
    from services.investments.cash_plan_materialisation import (
        CASH_PLAN_SOURCE,
        CashPlanMaterialisationService,
        CashPlanPoint,
        CashPlanReport,
        PlanFlowEvent,
        project_cash_plan,
    )
    from services.investments.flow_type_invariants import (
        OVERLAY_EXEMPT_FLOW_TYPES,
        is_overlay_exempt,
    )
    from services.investments.investment_service import (
        InvestmentChartsBundle,
        InvestmentDetailDTO,
        InvestmentService,
        LiveIngestReport,
        PositionSummaryDTO,
    )
    from services.investments.nav_materialisation import (
        NavMaterialisationReport,
        NavMaterialisationService,
    )
    from services.investments.pacing_rows import (
        FACTOR_STEP,
        NO_PLAN_NOTE,
        PLAN_SOURCE_REPORTED,
        PLAN_SOURCE_TA,
        PacingRow,
        build_pacing_rows,
        capital_account_ids,
        describe_shift,
        load_called_amounts,
    )
    from services.investments.plan_world import assemble_plan_frames
    from services.investments.unity_price import (
        UNITY_PRICE,
        is_unity_price,
        unity_price_violation,
    )
    from services.investments.valuation_mode import (
        UNITISABLE_TYPES,
        can_flip_to_unitised,
        flip_precondition_error,
        shows_positions_panel,
    )

#: Maps every re-exported name to the module that defines it. This is the
#: whole of the façade's runtime knowledge: :func:`__getattr__` imports the
#: owning module on first access and caches the resolved attribute, so a
#: consumer pays only for the members it actually names.
_ATTR_TO_MODULE: dict[str, str] = {
    "InvestmentExtractionResult": "services.data_normalization",
    "UploadNotFoundError": "services.data_normalization",
    "CASH_TYPE": "services.investments.aum",
    "CASH_PLAN_SOURCE": "services.investments.cash_plan_materialisation",
    "DEFAULT_HORIZON_QUARTERS": "services.investments.cash_flow_timeline",
    "HORIZON_QUARTERS": "services.investments.cash_flow_timeline",
    "CashFlowPlanningInputs": "services.investments.cash_flow_timeline",
    "CashFlowPlanningResult": "services.investments.cash_flow_timeline",
    "CashFlowTimeline": "services.investments.cash_flow_timeline",
    "Periodisation": "services.investments.cash_flow_timeline",
    "build_cash_flow_timeline": "services.investments.cash_flow_timeline",
    "load_cash_flow_planning_inputs": ("services.investments.cash_flow_timeline"),
    "project_cash_flow_planning": "services.investments.cash_flow_timeline",
    "assemble_plan_frames": "services.investments.plan_world",
    "AumBreakdown": "services.investments.aum",
    "build_nav_series": "services.investments.aum",
    "compute_aum": "services.investments.aum",
    "load_nav_series": "services.investments.aum",
    "CashPlanMaterialisationService": ("services.investments.cash_plan_materialisation"),
    "CashPlanPoint": "services.investments.cash_plan_materialisation",
    "CashPlanReport": "services.investments.cash_plan_materialisation",
    "PlanFlowEvent": "services.investments.cash_plan_materialisation",
    "project_cash_plan": "services.investments.cash_plan_materialisation",
    "OVERLAY_EXEMPT_FLOW_TYPES": "services.investments.flow_type_invariants",
    "is_overlay_exempt": "services.investments.flow_type_invariants",
    "InvestmentChartsBundle": "services.investments.investment_service",
    "InvestmentDetailDTO": "services.investments.investment_service",
    "InvestmentService": "services.investments.investment_service",
    "LiveIngestReport": "services.investments.investment_service",
    "PositionSummaryDTO": "services.investments.investment_service",
    "NavMaterialisationReport": "services.investments.nav_materialisation",
    "NavMaterialisationService": "services.investments.nav_materialisation",
    "FACTOR_STEP": "services.investments.pacing_rows",
    "NO_PLAN_NOTE": "services.investments.pacing_rows",
    "PLAN_SOURCE_REPORTED": "services.investments.pacing_rows",
    "PLAN_SOURCE_TA": "services.investments.pacing_rows",
    "PacingRow": "services.investments.pacing_rows",
    "build_pacing_rows": "services.investments.pacing_rows",
    "capital_account_ids": "services.investments.pacing_rows",
    "describe_shift": "services.investments.pacing_rows",
    "load_called_amounts": "services.investments.pacing_rows",
    "UNITY_PRICE": "services.investments.unity_price",
    "is_unity_price": "services.investments.unity_price",
    "unity_price_violation": "services.investments.unity_price",
    "UNITISABLE_TYPES": "services.investments.valuation_mode",
    "can_flip_to_unitised": "services.investments.valuation_mode",
    "flip_precondition_error": "services.investments.valuation_mode",
    "shows_positions_panel": "services.investments.valuation_mode",
}

__all__ = [
    "CASH_PLAN_SOURCE",
    "CASH_TYPE",
    "DEFAULT_HORIZON_QUARTERS",
    "FACTOR_STEP",
    "HORIZON_QUARTERS",
    "NO_PLAN_NOTE",
    "OVERLAY_EXEMPT_FLOW_TYPES",
    "PLAN_SOURCE_REPORTED",
    "PLAN_SOURCE_TA",
    "UNITISABLE_TYPES",
    "UNITY_PRICE",
    "AumBreakdown",
    "CashFlowPlanningInputs",
    "CashFlowPlanningResult",
    "CashFlowTimeline",
    "CashPlanMaterialisationService",
    "CashPlanPoint",
    "CashPlanReport",
    "InvestmentChartsBundle",
    "InvestmentDetailDTO",
    "InvestmentExtractionResult",
    "InvestmentService",
    "LiveIngestReport",
    "NavMaterialisationReport",
    "NavMaterialisationService",
    "PacingRow",
    "Periodisation",
    "PlanFlowEvent",
    "PositionSummaryDTO",
    "UploadNotFoundError",
    "assemble_plan_frames",
    "build_cash_flow_timeline",
    "build_nav_series",
    "build_pacing_rows",
    "can_flip_to_unitised",
    "capital_account_ids",
    "compute_aum",
    "describe_shift",
    "flip_precondition_error",
    "is_overlay_exempt",
    "is_unity_price",
    "load_called_amounts",
    "load_cash_flow_planning_inputs",
    "load_nav_series",
    "project_cash_flow_planning",
    "project_cash_plan",
    "shows_positions_panel",
    "unity_price_violation",
]


def __getattr__(name: str) -> Any:
    """Resolve a re-exported name, or a submodule, on first access (PEP 562).

    Args:
        name: The attribute requested on the ``services.investments``
            package.

    Returns:
        The re-exported object listed in :data:`__all__`, or — for a name
        that is not re-exported — the ``services.investments`` submodule of
        that name, so that ``import services.investments`` followed by
        ``services.investments.aum.compute_aum(...)`` keeps working.

    Raises:
        AttributeError: If ``name`` is neither a re-exported member nor an
            importable submodule of the package.
    """
    module_path = _ATTR_TO_MODULE.get(name)
    if module_path is not None:
        value = getattr(importlib.import_module(module_path), name)
        globals()[name] = value
        return value

    submodule_path = f"services.investments.{name}"
    try:
        submodule = importlib.import_module(submodule_path)
    except ModuleNotFoundError as exc:
        # Only a *missing submodule* means "no such attribute". A
        # ModuleNotFoundError raised from inside an existing submodule (a
        # genuinely absent dependency) must surface as itself.
        if exc.name != submodule_path:
            raise
        raise AttributeError(f"module 'services.investments' has no attribute '{name}'") from None
    globals()[name] = submodule
    return submodule


def __dir__() -> list[str]:
    """List the re-exported names plus the package's submodules.

    Returns:
        The sorted union of :data:`__all__` and every submodule of
        ``services.investments``, so interactive completion sees the same
        surface an eager façade would have offered.
    """
    import pkgutil

    submodules = [info.name for info in pkgutil.iter_modules(__path__)]
    return sorted(set(__all__) | set(submodules))
