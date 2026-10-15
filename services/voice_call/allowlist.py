# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The tool allowlist of a voice call's ``ask_shirley`` turn (ADR-0137 §3.1).

The call job passes :data:`CALL_TOOL_ALLOWLIST` as ``tool_names`` to
``AIServiceCore.stream_response``: the turn is offered only these tools and
the tool registry refuses every other name for that turn. Read-only is thus
enforced in code, not by the prompt alone.

This module imports nothing; the names are plain strings so the call package
does not pull in the AI core or the tool registry.
"""

from __future__ import annotations

#: Every tool a voice call's ``ask_shirley`` turn may use (ADR-0137 §3.1).
#: Today this is the whole registry, because every registered tool is a
#: read; the tripwire test pins the equality, so the first tool that writes
#: — or any new tool — fails that test and forces a decision here.
CALL_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "generate_chart",
        "get_dataset_slice",
        "get_dataset_summary",
        "get_investment_data",
        "get_investment_detail",
        "get_investment_nav_history",
        "get_limit_coverage",
        "get_portfolio_overview",
        "get_portfolio_statistics",
        "get_saa_configuration",
        "get_saa_hypothetical_comparison",
        "list_analysis_results",
        "list_datasets",
        "list_investments",
        "render_chart",
        "web_research",
    }
)
