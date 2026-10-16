# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""What the call agent may ask Shirley about — the capability block of the call prompt.

The vendor agent of a voice call holds no program function: every fact comes
from Shirley through ``ask_shirley``. To route a question there, it must know
what Shirley can reach. This module states that in plain language, one line
per area a user asks about, and :func:`render_call_capabilities` renders the
block that closes the composed call prompt.

The block names no tool: the agent cannot call Shirley's tools and must not
try. Each capability records the registered tools it stands for, and the
tests pin that every name in :data:`CALL_TOOL_ALLOWLIST` — which the
allowlist tripwire pins to the whole registry — is covered exactly once. A
new tool therefore fails the suite until its capability line is written, and
the agent's picture of Shirley cannot drift from what Shirley can do. A
changed line changes the call prompt and with it the definition hash, so the
vendor agent is brought in line at the next call start.

This module imports only the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallCapability:
    """One area the call agent may ask Shirley about.

    Attributes:
        text: The line the agent reads — plain language, no tool name.
        tools: The registered tools behind it; empty for an ability of
            Shirley's own that needs no tool (drafting, explaining).
    """

    text: str
    tools: tuple[str, ...]


#: The heading of the block; the addendum's rule 1 refers to it by name.
CAPABILITIES_HEADING = "### What Shirley can look up"

#: Every area ``ask_shirley`` reaches, in the order a user meets them.
CALL_CAPABILITIES: tuple[CallCapability, ...] = (
    CallCapability(
        "The portfolio's headline figures: total assets, invested capital, cash, "
        "IRR since inception, TVPI, DPI and the number of active investments.",
        ("get_portfolio_overview",),
    ),
    CallCapability(
        "Individual investments: which ones there are, their details, their data "
        "over time and their NAV history.",
        (
            "list_investments",
            "get_investment_detail",
            "get_investment_data",
            "get_investment_nav_history",
        ),
    ),
    CallCapability(
        "Risk and statistics: correlations, Sharpe and Sortino ratios, drawdowns.",
        ("get_portfolio_statistics",),
    ),
    CallCapability(
        "Investment limits: coverage, headroom and breaches as of the latest month-end.",
        ("get_limit_coverage",),
    ),
    CallCapability(
        "The strategic asset allocation: its assumptions, and how the actual "
        "portfolio did against the strategic weights.",
        ("get_saa_configuration", "get_saa_hypothetical_comparison"),
    ),
    CallCapability(
        "Uploaded datasets and saved analysis results.",
        ("list_datasets", "get_dataset_summary", "get_dataset_slice", "list_analysis_results"),
    ),
    CallCapability(
        "Charts of any of the above; a chart appears in the chat window, not on the call.",
        ("generate_chart", "render_chart"),
    ),
    CallCapability(
        "Current news and press coverage from vetted sources such as regulators, "
        "central banks and the financial press.",
        ("web_research",),
    ),
    CallCapability(
        "Explanations of PortfoliFLOW's features, and written drafts such as investor "
        "commentary; a long text appears in the chat window.",
        (),
    ),
)


def render_call_capabilities() -> str:
    """Render the capability block that closes the call prompt.

    Returns:
        The heading, one sentence on how to use the list, and one ``- `` line
        per capability. Deterministic: the same code renders the same text.
    """
    lines = [
        CAPABILITIES_HEADING,
        "",
        "`ask_shirley` reaches everything below. Use it for any question in one of "
        "these areas. If a request needs a fact outside them, say plainly that "
        "Shirley cannot look that up.",
        "",
    ]
    lines += [f"- {capability.text}" for capability in CALL_CAPABILITIES]
    return "\n".join(lines)
