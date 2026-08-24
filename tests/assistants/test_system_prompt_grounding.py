# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""System-prompt grounding tests for Shirley (B8).

``AIServiceCore.get_system_prompt`` injects a *generated* tool-inventory
block — rendered from the :class:`~services.tool_registry.ToolRegistry`,
the single source of truth (ADR-0012) — between the Soul fence content
and the hand-authored orchestration context. These tests pin the
anti-drift guarantee that motivates B8: the prompt's tool list is
generated, never hand-listed, so it cannot fall out of step with the
tools actually exposed to the model.

The grounding must also *augment* rather than replace: the Soul identity
and the refactored orchestration judgment still appear. And it must
degrade gracefully — an empty or broken registry falls back to the
un-grounded prompt without raising.

A second, independent grounding lives at the same seam: the temporal
block prepended ahead of everything else (ADR-0127 T1). Its tests sit
at the foot of this file.
"""

from __future__ import annotations

from datetime import date

from services.ai_service_core import get_ai_service_core
from services.tool_classes import ToolClass
from services.tool_registry import ToolRegistry, get_tool_registry

# The five back-office analysis tools the static orchestration doc never
# mentioned — the concrete drift B8 removes (ADR-0069/0070).
_ANALYSIS_TOOLS = (
    "get_limit_coverage",
    "get_saa_hypothetical_comparison",
    "get_portfolio_statistics",
    "get_portfolio_overview",
    "get_saa_configuration",
)

_INVENTORY_HEADING = "## Your currently available tools"


def test_every_registered_tool_appears_in_prompt() -> None:
    """Anti-drift guarantee: every registered tool is in the prompt.

    The composed prompt must name every tool the registry exposes — the
    invariant that makes hand-listing unnecessary and drift impossible.
    """
    service = get_ai_service_core()  # idempotent; registers default tools
    prompt = service.get_system_prompt("shirley")

    names = [d["function"]["name"] for d in get_tool_registry().get_tool_definitions()]
    assert names, "expected the default tool set to be registered"
    missing = [name for name in names if f"**{name}**" not in prompt]
    assert not missing, f"tools missing from generated inventory: {missing}"


def test_throwaway_tool_appears_proving_generation() -> None:
    """A tool registered at test time appears — proving generation.

    If the inventory were a hand-maintained list, a brand-new tool could
    not show up. Registering one and finding it in the prompt proves the
    block is rendered from the live registry.
    """
    service = get_ai_service_core()
    registry = get_tool_registry()
    name = "test_grounding_throwaway_tool"
    registry.register_tool(
        name=name,
        function=lambda: "ok",
        description=(
            "A throwaway tool registered only to prove the inventory is "
            "generated from the registry rather than hand-listed."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.READ_INTERNAL,
    )
    try:
        prompt = service.get_system_prompt("shirley")
        assert f"**{name}**" in prompt
    finally:
        registry.unregister_tool(name)


def test_inventory_heading_and_analysis_tools_present() -> None:
    """The inventory heading and all five analysis tools appear by name."""
    service = get_ai_service_core()
    prompt = service.get_system_prompt("shirley")

    assert _INVENTORY_HEADING in prompt
    for name in _ANALYSIS_TOOLS:
        assert f"**{name}**" in prompt, f"{name} missing from inventory"


def test_grounding_augments_soul_and_orchestration() -> None:
    """Grounding augments; it does not replace Soul or orchestration.

    The Soul identity content and the hand-authored orchestration
    judgment (kept through the B8 refactor) must both survive, and the
    inventory must sit between them: Soul → inventory → routing guide.
    """
    service = get_ai_service_core()
    prompt = service.get_system_prompt("shirley")

    assert "You are Shirley, the AI assistant embedded in PortfoliFLOW" in prompt
    assert "two-step charting flow" in prompt
    assert "When to use which tool" in prompt

    soul_idx = prompt.index("You are Shirley, the AI assistant embedded")
    inventory_idx = prompt.index(_INVENTORY_HEADING)
    routing_idx = prompt.index("When to use which tool")
    assert soul_idx < inventory_idx < routing_idx, (
        "expected ordering Soul -> inventory -> orchestration routing guide"
    )


def test_empty_registry_falls_back_to_ungrounded(monkeypatch) -> None:
    """An empty registry yields no inventory block but a valid prompt.

    The helper resolves ``get_tool_registry`` at call time, so pointing
    it at a fresh empty registry exercises the empty-registry branch
    without disturbing the real singleton.
    """
    service = get_ai_service_core()
    monkeypatch.setattr("services.tool_registry.get_tool_registry", ToolRegistry)

    prompt = service.get_system_prompt("shirley")

    assert _INVENTORY_HEADING not in prompt
    # The un-grounded prompt is still complete.
    assert "You are Shirley, the AI assistant embedded in PortfoliFLOW" in prompt
    assert "When to use which tool" in prompt


def test_registry_error_falls_back_without_raising(monkeypatch) -> None:
    """A registry that raises degrades to the un-grounded prompt."""
    service = get_ai_service_core()

    def _boom() -> ToolRegistry:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("services.tool_registry.get_tool_registry", _boom)

    # Must not raise — grounding failure falls back to the un-grounded prompt.
    prompt = service.get_system_prompt("shirley")

    assert _INVENTORY_HEADING not in prompt
    assert "You are Shirley, the AI assistant embedded in PortfoliFLOW" in prompt


def test_render_tool_inventory_empty_registry_returns_blank(monkeypatch) -> None:
    """``_render_tool_inventory`` returns an empty string for no tools."""
    service = get_ai_service_core()
    monkeypatch.setattr("services.tool_registry.get_tool_registry", ToolRegistry)
    assert service._render_tool_inventory() == ""


# ---------------------------------------------------------------------------
# Temporal grounding — the current-date block (ADR-0127 T1)
# ---------------------------------------------------------------------------

_PLAN_SENTENCE = "Treat any data dated after this as plan/forecast data, not observed fact."


def test_prompt_begins_with_current_date_block() -> None:
    """The composed prompt opens with today's date, ahead of everything.

    Shirley cannot classify a tool-reported Stichtag as past, present or
    future without a reference point (ADR-0127 §Context). The block is
    *prepended* so date salience does not compete with the Soul, the
    inventory or the orchestration context for positional attention.
    """
    service = get_ai_service_core()
    prompt = service.get_system_prompt("shirley")

    assert prompt.startswith(f"Current date: {date.today().isoformat()}")
    assert _PLAN_SENTENCE in prompt

    date_idx = prompt.index("Current date: ")
    soul_idx = prompt.index("You are Shirley, the AI assistant embedded")
    inventory_idx = prompt.index(_INVENTORY_HEADING)
    assert date_idx < soul_idx < inventory_idx, (
        "expected the grounding block ahead of both Soul and inventory"
    )


def test_fallback_prompt_carries_date_block() -> None:
    """A missing soul file still yields a temporally grounded prompt.

    Temporal grounding must not depend on ``docs/Soul_<Name>.md`` being
    present and well-formed (ADR-0127 T1), so the minimal fallback
    carries the block too.
    """
    service = get_ai_service_core()
    prompt = service.get_system_prompt("nonexistent")

    assert prompt.startswith(f"Current date: {date.today().isoformat()}")
    assert _PLAN_SENTENCE in prompt
    assert "You are Shirley, an AI assistant for institutional portfolio management." in prompt
