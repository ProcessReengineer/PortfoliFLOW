# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Planning-Desk result assembly (ADR-0104 §5).

The seam that turns a scenario overlay into the deltas-first result DTOs the
Scenario Analysis lens renders. It is a **pure consumer** of the existing
engines (ADR-0104 §5, D17): coverage, composition, and the ADR-0066 return
logic are fed transformed frames, never forked. See
:mod:`services.planning_desk.scenario_results`.
"""

from services.planning_desk.scenario_inputs import (
    load_scenario_result_inputs,
)
from services.planning_desk.scenario_results import (
    CompositionPair,
    FamilyHeadroomDelta,
    HeadroomClassDelta,
    KpiDelta,
    ScenarioResult,
    ScenarioResultInputs,
    ScenarioSeriesPair,
    assemble_scenario_result,
)

__all__ = [
    "CompositionPair",
    "FamilyHeadroomDelta",
    "HeadroomClassDelta",
    "KpiDelta",
    "ScenarioResult",
    "ScenarioResultInputs",
    "ScenarioSeriesPair",
    "assemble_scenario_result",
    "load_scenario_result_inputs",
]
