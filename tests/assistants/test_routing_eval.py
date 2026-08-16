# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Routing eval for Shirley's analysis tools (ADR-0070, Stage 2).

Two guards keep the growing tool list discriminable (ADR-0070 Open
Question 3 / roadmap B8):

1. :class:`TestDescriptionDisjointness` — an **always-on structural
   guard** (no model, runs on every CI pass). It pins the
   description-disjointness invariants: each ADR-0070-touched analysis
   tool carries an explicit negative cross-reference naming a sibling
   tool, the two SAA tools point at each other, and no two analysis-tool
   descriptions share an identical leading sentence.

2. :func:`test_routing_selects_expected_tool` — an **opt-in integration
   test** (``@pytest.mark.integration``, skipped without an API key). It
   runs each fixture phrasing through the live tool-selection path and
   asserts the model selects the expected tool. This is the measurement
   that keeps discriminability honest as tools accrue; it is meant to be
   run deliberately, not on every CI pass.

Scope note (ADR-0070 Stage-0 flag): the negative-cross-reference
invariant is asserted over the set ADR-0070 *introduces or touches*
(``get_portfolio_overview``, ``get_saa_configuration``, and the amended
``get_saa_hypothetical_comparison``). The two untouched ADR-0069 tools
(``get_limit_coverage``, ``get_portfolio_statistics``) predate this
convention and are out of this guard's scope by design — they may not be
edited here. The identical-leading-sentence invariant still ranges over
*all* analysis-tool descriptions.
"""

from __future__ import annotations

import os
from pathlib import Path

import openai
import pytest
from dotenv import load_dotenv

from services.ai_service_core import get_ai_service_core
from services.tool_registry import get_tool_registry
from tests.assistants.routing_eval_cases import ROUTING_CASES

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# The five tools defined in services/tools/analysis_tools.py.
_ANALYSIS_TOOL_NAMES = (
    "get_limit_coverage",
    "get_saa_hypothetical_comparison",
    "get_portfolio_statistics",
    "get_portfolio_overview",
    "get_saa_configuration",
)

# The subset ADR-0070 introduces or amends — the only tools this guard
# requires to carry a negative cross-reference (see module docstring).
_CROSS_REF_REQUIRED = (
    "get_portfolio_overview",
    "get_saa_configuration",
    "get_saa_hypothetical_comparison",
)


def _registered_descriptions() -> dict[str, str]:
    """Return ``{tool_name: description}`` for every registered tool.

    Constructing the AI service core registers the full default tool set
    (datastore, chart, web-research, investment, analysis) on the
    singleton registry, so sibling cross-references can resolve against
    any real tool name.
    """
    get_ai_service_core()  # idempotent; ensures all default tools register
    return {
        d["function"]["name"]: d["function"]["description"]
        for d in get_tool_registry().get_tool_definitions()
    }


def _leading_sentence(description: str) -> str:
    """Return the lowercased first sentence of a description."""
    head = description.split(". ", 1)[0]
    return head.strip().casefold()


# ---------------------------------------------------------------------------
# Always-on structural guard (no model)
# ---------------------------------------------------------------------------


class TestDescriptionDisjointness:
    """Description-disjointness invariants — model-free, CI-safe."""

    def test_touched_tools_name_a_sibling_in_a_negation(self) -> None:
        """Each ADR-0070-touched description steers away from a named sibling."""
        descriptions = _registered_descriptions()
        all_names = set(descriptions)

        for name in _CROSS_REF_REQUIRED:
            assert name in descriptions, f"{name} is not registered"
            description = descriptions[name]

            # (a) an explicit negation marker, and ...
            assert "NOT" in description or "Not " in description, (
                f"{name} description carries no explicit negative cross-reference"
            )
            # ... (b) at least one *other* registered tool named in it.
            siblings = {other for other in all_names if other != name and other in description}
            assert siblings, f"{name} description names no sibling tool to disambiguate against"

    def test_saa_tools_point_at_each_other(self) -> None:
        """The two SAA reads cross-reference each other by name."""
        descriptions = _registered_descriptions()
        assert "get_saa_hypothetical_comparison" in descriptions["get_saa_configuration"]
        assert "get_saa_configuration" in descriptions["get_saa_hypothetical_comparison"]

    def test_no_two_analysis_tools_share_a_leading_sentence(self) -> None:
        """No two analysis-tool descriptions open with the same sentence."""
        descriptions = _registered_descriptions()
        leads = [_leading_sentence(descriptions[name]) for name in _ANALYSIS_TOOL_NAMES]
        assert len(set(leads)) == len(leads), (
            f"two analysis tools share an identical leading sentence: {leads}"
        )

    def test_every_routing_case_targets_a_registered_tool(self) -> None:
        """Every fixture's expected tool is actually registered."""
        descriptions = _registered_descriptions()
        for case in ROUTING_CASES:
            assert case.expected_tool in descriptions, (
                f"routing case {case.phrasing!r} targets unregistered tool {case.expected_tool!r}"
            )


# ---------------------------------------------------------------------------
# Integration routing test (opt-in; needs a live model)
# ---------------------------------------------------------------------------


async def _select_tool(base_url: str, api_key: str, model: str, phrasing: str) -> str | None:
    """Run one phrasing through the model's tool-selection path.

    Mirrors :class:`AIServiceCore`'s per-call client construction and
    reuses the same system prompt and registered tool definitions, but
    asks the model to choose a tool for a single user turn
    (``tool_choice="auto"``) without executing anything.

    Returns:
        The name of the first tool the model selected, or ``None`` if it
        chose to answer without a tool call.
    """
    service = get_ai_service_core()  # also registers the default tools
    system_prompt = service.get_system_prompt("shirley")
    tools = get_tool_registry().get_tool_definitions()

    client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": phrasing},
            ],
            tools=tools,
            tool_choice="auto",
            temperature=0.0,
        )
    finally:
        await client.close()

    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None)
    if not tool_calls:
        return None
    return tool_calls[0].function.name


@pytest.mark.integration
@pytest.mark.parametrize("case", ROUTING_CASES, ids=lambda c: c.expected_tool)
async def test_routing_selects_expected_tool(case) -> None:
    """The live model routes each confusable phrasing to the expected tool."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("SHIRLEY_MODEL")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    if not api_key or not model:
        pytest.skip(
            "OPENROUTER_API_KEY and SHIRLEY_MODEL must be set to run the integration routing eval."
        )

    selected = await _select_tool(base_url, api_key, model, case.phrasing)
    assert selected == case.expected_tool, (
        f"phrasing {case.phrasing!r} routed to {selected!r}, expected {case.expected_tool!r}"
    )
