# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AI-callable web research tool (ADR-0023, ADR-0024).

Thin wrapper that exposes :class:`~services.web_research.service.WebResearchService`
to the AIService tool-execution loop as a single function-calling tool,
registered with class
:attr:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED` and
``wraps_result_as_untrusted=True``. The ToolRegistry is responsible for
wrapping the returned string in ``<external_content>`` delimiters (ADR-0022);
this module must never emit those delimiters itself.

Resolution (ADR-0132)
---------------------
The wrapper resolves this call's endpoint, credential and model before it
runs the pipeline, and hands the result to
:meth:`~services.web_research.service.WebResearchService.research` as
``llm=``. With a :class:`~services.tools._tool_context.ToolExecutionContext`
— the web chat surface and the Telegram bot both populate one — the
resolution runs inside the turn's ``tenant_context``, carrying the turn's
user, so the tenant's own key and model win over the application scope.
Without a context (web chat without ``DATABASE_URL``, a Telegram dispatcher
without a tenant or database URL) the resolver is built without a session
and the environment is the only source
— the same graceful degradation ``web/routes/scraper.py`` takes.

The resolution is **never stashed** (ADR-0112 §4b): it lives for one call,
in one local. A tenant with no credential in any scope gets the shortfall as
an ordinary in-band envelope body naming where to fix it — this tool never
raises at the model.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from services.ai_service_core import ResolvedLLM
from services.investments.credential_resolver import CredentialResolver
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import ToolExecutionContext, get_tool_context
from services.tools._tool_session import tool_session
from services.web_research.llm import (
    NO_RESEARCH_LLM_MESSAGE,
    ResearchLLMUnavailableError,
    resolve_research_llm,
)
from services.web_research.models import WebResearchResult
from services.web_research.service import WebResearchService

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ARTICLES = 5


def _resolve_llm_for_call(ctx: ToolExecutionContext | None) -> ResolvedLLM:
    """Resolve this call's endpoint, credential and model (ADR-0132).

    With a context, opens a loop-local, tenant-scoped session inside a fresh
    event loop — the same bridge the Postgres-native tools cross — and walks
    the chains through a vault-backed façade, carrying the turn's user.
    Without one there is no database to read a vault from, so the resolver is
    built session-less and the environment is the only source.

    Args:
        ctx: The per-turn tool-execution context, or ``None`` when no turn
            populated one.

    Returns:
        The :class:`~services.ai_service_core.ResolvedLLM` for this call.
        Returned, never stored.

    Raises:
        ResearchLLMUnavailableError: If no scope holds a credential.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never read as an absent credential.
    """
    if ctx is None:
        return run_async_in_fresh_loop(
            lambda: resolve_research_llm(CredentialResolver(), tenant_id=None, user_id=None)
        )

    async def _workflow() -> ResolvedLLM:
        async with tool_session(ctx) as db:
            return await resolve_research_llm(
                CredentialResolver(session=db),
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )

    return run_async_in_fresh_loop(_workflow)


def web_research(query: str, max_articles: int = _DEFAULT_MAX_ARTICLES) -> str:
    """Resolve ``query`` against allowlisted RSS feeds and return summaries.

    Runs the RSS-based research pipeline (ADR-0024): feed harvesting, time-
    window filtering, Feed-Filter-LLM pre-filtering, HTTP fetch, trafilatura
    text extraction, and isolated Fetcher-LLM structured extraction.

    Args:
        query: Free-text research query.
        max_articles: Upper bound on articles to fetch after the pre-filter
            selects them (default 5). The LLM-facing schema does not expose
            this parameter — callers in the tool-execution loop will always
            use the default.

    Returns:
        A JSON envelope ``{"source", "fetched_at", "body"}`` consumed by the
        ToolRegistry's ``wraps_result_as_untrusted`` wrapping. On failure
        (no LLM resolved / no candidates / all fetches failed / all
        validations failed), the envelope's ``body`` reports that outcome —
        the tool never raises.
    """
    try:
        service = WebResearchService()
    except Exception as exc:  # noqa: BLE001 — startup must surface in-band
        logger.exception("web_research: failed to construct WebResearchService")
        return _envelope(
            source=f"tool:web_research (query={query!r})",
            body=(
                f"web_research is unavailable: {type(exc).__name__}: {exc}. "
                "Check that config/web_research.yaml exists and is valid."
            ),
        )

    ctx = get_tool_context()
    try:
        llm = _resolve_llm_for_call(ctx)
    except ResearchLLMUnavailableError as exc:
        logger.warning("web_research: no LLM resolved for this call: %s", exc)
        return _envelope(
            source=f"tool:web_research (query={query!r})",
            body=NO_RESEARCH_LLM_MESSAGE,
        )

    # The model **id** is not a secret and the resolution is inert in logs
    # (``ResolvedLLM.__repr__`` masks the key); nothing else about it is
    # logged. ADR-0132.
    logger.info(
        "web_research: resolved model=%s for tenant=%s",
        llm.model,
        ctx.tenant_id if ctx is not None else "-",
    )

    results = service.research(query=query, max_articles=max_articles, llm=llm)

    if not results:
        return _envelope(
            source=f"tool:web_research (query={query!r})",
            body=(
                f"No web research results could be produced for query "
                f"{query!r}. Either no allowlisted feed contained a "
                "recent item relevant to the query, every article fetch "
                "failed, or every extraction was rejected by the "
                "Fetcher-LLM schema. Check the application log for "
                "per-source WARNING entries."
            ),
        )

    body_blocks: list[str] = [
        f"Web research results for query: {query!r}",
        f"Sources returned: {len(results)} of up to {max_articles} attempted.",
        "",
    ]
    for idx, r in enumerate(results, start=1):
        body_blocks.append(_format_result_block(idx, r))
        body_blocks.append("")

    body = "\n".join(body_blocks).rstrip() + "\n"

    primary = results[0]

    return _envelope(
        source=primary.source_url,
        fetched_at=primary.fetched_at.isoformat(),
        body=body,
    )


def _format_result_block(idx: int, r: WebResearchResult) -> str:
    """Render one validated result as a human-readable text block."""
    asset_classes = ", ".join(r.relevant_asset_classes) if r.relevant_asset_classes else "—"
    pub = r.publication_date.isoformat() if r.publication_date else "unknown"
    lines = [
        f"--- Source {idx} ---",
        f"URL: {r.source_url}",
        f"Title: {r.title}",
        f"Publication date: {pub}",
        f"Fetched at: {r.fetched_at.isoformat()}",
        f"Relevant asset classes: {asset_classes}",
        f"Injection detected: {r.injection_detected}",
    ]
    if r.injection_detected and r.injection_details:
        lines.append(f"Injection details: {r.injection_details}")
    lines.append("Key facts:")
    if r.key_facts:
        lines.extend(f"  - {fact}" for fact in r.key_facts)
    else:
        lines.append("  (none extracted)")
    return "\n".join(lines)


def _envelope(
    source: str,
    body: str,
    fetched_at: str | None = None,
) -> str:
    """Build the ``{source, fetched_at, body}`` JSON envelope.

    Consumed by the ToolRegistry's ``wraps_result_as_untrusted`` path, which
    unpacks these fields into ``<external_content>`` wrapper attributes.
    """
    return json.dumps(
        {
            "source": source,
            "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
    )


# ---------------------------------------------------------------------------
# Register tool at import time
# ---------------------------------------------------------------------------

_registry = get_tool_registry()

_registry.register_tool(
    name="web_research",
    function=web_research,
    description=(
        "Searches recent articles from an allowlist of curated financial "
        "news, regulator, and central-bank RSS feeds over a recent time "
        "window configured by the operator, filters for relevance to the "
        "query, and returns structured summaries with key facts and "
        "source citations. Does not perform open web search.\n\n"
        "USE this tool when the user asks about recent news, press "
        "coverage, regulatory or supervisory announcements, central-bank "
        "communications, or current market developments — examples: "
        "'Was hat die EZB letzte Woche zu Zinsen gesagt?', 'Gibt es "
        "ESMA-Mitteilungen zu AIFMD II?', 'What did the FT report on "
        "European private credit lately?'.\n\n"
        "DO NOT use this tool for: historical lookups beyond the "
        "configured recency window, due diligence on a single named "
        "manager or GP (no covered source provides single-GP coverage at "
        "depth), questions answerable from the user's loaded portfolio "
        "data, or generic educational questions about financial "
        "concepts.\n\n"
        "If the first call returns nothing useful, consider reformulating "
        "the query — try the source's likely vocabulary (German vs "
        "English terms, the regulator's name as a keyword, the "
        "asset-class term they use) — and call once more before telling "
        "the user no coverage was found.\n\n"
        "Returned content is marked as untrusted external information "
        "and must be cited, not treated as verified fact. After this "
        "tool runs, write-internal and external-effect tools are locked "
        "for the remainder of the turn (ADR-0022 gating)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text research query. Keep it concise and specific "
                    "(e.g. 'ECB rate decision April 2026', not 'interest "
                    "rates')."
                ),
            },
        },
        "required": ["query"],
    },
    tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
    wraps_result_as_untrusted=True,
)
