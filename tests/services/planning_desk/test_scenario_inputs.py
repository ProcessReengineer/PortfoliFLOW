# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The scenario-assembly seam's failure contract (ADR-0104 §4).

:func:`services.planning_desk.assemble_scenario_from_book` is the one place a
*(book, overlay)* → :class:`ScenarioResult` question is asked — the Planning
Desk's result region and, from S6, the Transactions area's impact panel. What
is pinned here is the seam's *rail*, not its arithmetic (that is
:mod:`tests.services.planning_desk.test_scenario_results`): a
:data:`~services.planning_desk.SCENARIO_NOTICE_ERRORS` member becomes a message
the caller renders as a notice, and anything else propagates so a genuine bug
still surfaces as a 500.

DB-free: the repositories are stubs returning empty results, since the assembly
itself is monkeypatched — the loader only has to run, not to find anything.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import pytest

from services.overlay import EMPTY_OVERLAY, OverlayError
from services.planning_desk import (
    SCENARIO_NOTICE_ERRORS,
    assemble_scenario_from_book,
)


class _EmptyListRepo:
    """A repository stub whose every loader method yields nothing."""

    async def list_active(self) -> list[Any]:
        return []

    async def list_all(self) -> list[Any]:
        return []

    async def list_by_investments_and_kind(
        self, investment_ids: list[UUID], kind: str
    ) -> dict[UUID, list[Any]]:
        return {}

    async def list_sets(self, family: str | None = None) -> list[Any]:
        return []

    async def list_limits(self, set_id: UUID) -> list[Any]:
        return []


def _stub_kwargs(cash_flow_inputs: Any) -> dict[str, Any]:
    """The seam's arguments with every repository stubbed out."""
    repo = _EmptyListRepo()
    return {
        "cash_flow_inputs": cash_flow_inputs,
        "evaluation_dates": [date(2026, 3, 31)],
        "cut_over": date(2026, 1, 1),
        "overlay": EMPTY_OVERLAY,
        "investments": repo,
        "navs": repo,
        "cashflows": repo,
        "asset_classes": repo,
        "limits": repo,
    }


class _CashFlowInputsStub:
    """Only the two attributes the loader forwards to the inputs container."""

    baseline: Any = None
    converter: Any = None


async def test_notice_error_becomes_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A :data:`SCENARIO_NOTICE_ERRORS` member is caught and returned."""
    assert OverlayError in SCENARIO_NOTICE_ERRORS

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise OverlayError("x")

    monkeypatch.setattr("services.planning_desk.scenario_inputs.assemble_scenario_result", _raise)

    result, message = await assemble_scenario_from_book(**_stub_kwargs(_CashFlowInputsStub()))

    assert result is None
    assert message == "x"


async def test_other_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything outside the notice tuple stays a genuine failure."""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("services.planning_desk.scenario_inputs.assemble_scenario_result", _raise)

    with pytest.raises(RuntimeError, match="boom"):
        await assemble_scenario_from_book(**_stub_kwargs(_CashFlowInputsStub()))
