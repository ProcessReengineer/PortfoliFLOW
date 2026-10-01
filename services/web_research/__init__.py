# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Research capability (ADR-0023, ADR-0024).

Two-stage, allowlisted web research pipeline exposed to the AI assistant via a
single tool of class :class:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED`.

Stage 1 — HTTP fetch against a curated domain allowlist
(``config/web_research.yaml``). Candidate articles are resolved from RSS/Atom
feeds declared per allowlist entry (ADR-0024) and pre-filtered for relevance
by an isolated Feed-Filter-LLM (``docs/Feed_Filter_Prompt.md``).

Stage 2 — isolated, tool-free Fetcher-LLM extraction into a validated
pydantic schema (``docs/Fetcher_Prompt.md``).

Both LLMs run on a resolution the **caller** supplies per tool call, walked
per tenant through the credential façade by :mod:`services.web_research.llm`
(ADR-0132). The service holds no credential and no model of its own.

The module is Qt-free and synchronous. The tool wrapper that exposes it to
the AIService is in :mod:`services.tools.web_research_tool`.
"""

from services.web_research.allowlist import (
    AllowlistConfig,
    AllowlistEntry,
    AllowlistError,
    get_effective_window,
    is_allowed,
    load_allowlist,
)
from services.web_research.fetcher import (
    ExtractionError,
    FeedFetchResult,
    FeedParseError,
    FetchError,
    FetchResult,
    extract_text,
    fetch_feed,
    fetch_url,
    parse_feed,
)
from services.web_research.llm import (
    NO_RESEARCH_LLM_MESSAGE,
    ResearchLLMUnavailableError,
    resolve_research_llm,
    resolve_research_model,
)
from services.web_research.models import (
    FeedFilterResult,
    FeedItem,
    WebResearchResult,
)
from services.web_research.service import WebResearchService

__all__ = [
    "NO_RESEARCH_LLM_MESSAGE",
    "AllowlistConfig",
    "AllowlistEntry",
    "AllowlistError",
    "ExtractionError",
    "FeedFetchResult",
    "FeedFilterResult",
    "FeedItem",
    "FeedParseError",
    "FetchError",
    "FetchResult",
    "ResearchLLMUnavailableError",
    "WebResearchResult",
    "WebResearchService",
    "extract_text",
    "fetch_feed",
    "fetch_url",
    "get_effective_window",
    "is_allowed",
    "load_allowlist",
    "parse_feed",
    "resolve_research_llm",
    "resolve_research_model",
]
