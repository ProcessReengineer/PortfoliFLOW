# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AI-callable tool implementations.

Each module in this package registers its tools with the ToolRegistry
at import time. To make tools available, import the relevant module
(typically done during AIService initialisation).

Current tool modules:
    datastore_tools — Query and inspect DataStore contents.
    chart_tools — Generate themed charts for display in the chat.
    web_research_tool — RSS-based news research across an allowlisted
        set of domains (class READ_EXTERNAL_UNTRUSTED, ADR-0023 / 0024).
"""
