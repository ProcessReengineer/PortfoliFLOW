# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Routing-eval fixtures for Shirley's analysis tools (ADR-0070).

A representative-phrasing → expected-tool table covering the confusable
pairs the second wave of ``READ_INTERNAL`` analysis tools introduces
(``get_portfolio_overview`` / ``get_saa_configuration`` alongside the
ADR-0069 trio). Consumed by two tests in :mod:`test_routing_eval`:

* an **always-on structural guard** (no model) that pins the
  description-disjointness invariants, and
* an **opt-in integration test** (``@pytest.mark.integration``, skipped
  without an API key) that runs each phrasing through the live
  tool-selection path and asserts the model picks the expected tool.

This table is the discriminability measurement that keeps the growing
tool list honest as tools accrue (ADR-0070 Open Question 3 / roadmap B8).
It is deliberately data-only so both tests share one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingCase:
    """One routing expectation: a user phrasing and the tool it should pick.

    Attributes:
        phrasing: A representative user message.
        expected_tool: The registered tool name the model should select
            for that phrasing.
    """

    phrasing: str
    expected_tool: str


# The confusable pairs at minimum (ADR-0070 Stage-2 table). Each phrasing
# is chosen to sit close to a sibling tool it must *not* be routed to.
ROUTING_CASES: tuple[RoutingCase, ...] = (
    RoutingCase(
        "how big is the portfolio — what is our total AUM and IRR right now?",
        "get_portfolio_overview",
    ),
    RoutingCase(
        "what does our SAA assume for expected returns across the asset classes?",
        "get_saa_configuration",
    ),
    RoutingCase(
        "would we have done just as well holding the SAA weights instead?",
        "get_saa_hypothetical_comparison",
    ),
    RoutingCase(
        "give me Investment H's NAV history",
        "get_investment_data",
    ),
    RoutingCase(
        "are we in breach on any investment limit?",
        "get_limit_coverage",
    ),
    RoutingCase(
        "is H earning its fee or just paying for beta?",
        "get_portfolio_statistics",
    ),
    RoutingCase(
        "here's a new PE fund we're looking at — how does it fit our portfolio?",
        "get_portfolio_overview",
    ),
)
