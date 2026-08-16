# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Research orchestration service (ADR-0023 two-stage, ADR-0024 RSS).

``WebResearchService.research()`` is the single public entry point. It takes
a free-text query, pulls feed items from every allowlisted feed, filters
them by a configurable time window, asks the Feed-Filter-LLM which are
relevant to the query, fetches the selected article URLs, and runs each
extracted article through the isolated Fetcher-LLM, returning validated
:class:`WebResearchResult` payloads.

PyQt-free and synchronous. Called from the AIService tool-execution loop
inside a QThread worker; do not add Qt imports here.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from services.ai_service_core import get_ai_service_core as get_ai_service
from services.scraper.json_parser import JsonParseError, parse_extraction_response
from services.web_research.allowlist import (
    AllowlistConfig,
    AllowlistEntry,
    get_effective_window,
    is_allowed,
    load_allowlist,
)
from services.web_research.fetcher import (
    ExtractionError,
    FeedParseError,
    FetchError,
    extract_text,
    fetch_feed,
    fetch_url,
    parse_feed,
)
from services.web_research.models import FeedFilterResult, FeedItem, WebResearchResult

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FETCHER_PROMPT_PATH = _REPO_ROOT / "docs" / "Fetcher_Prompt.md"
_FEED_FILTER_PROMPT_PATH = _REPO_ROOT / "docs" / "Feed_Filter_Prompt.md"

# Matches a bare ``` fence at the start of a line with no language specifier.
# Identical convention to services/scraper/service.py::_FENCE_RE.
_FENCE_RE = re.compile(r"^```\s*$", re.MULTILINE)

# Per-fetch input-size budget for the Fetcher-LLM. Generous enough for 10
# facts at 300 chars each plus schema overhead; bounded so a runaway page
# cannot drain the context window.
_FETCHER_MAX_INPUT_CHARS = 40_000
_FETCHER_TEMPERATURE = 0.0
_FETCHER_TIMEOUT_S = 60.0

# Feed-Filter-LLM settings. The filter sees only titles and short
# descriptions, so token budget is modest — we cap to keep output compact.
_FILTER_TEMPERATURE = 0.0
_FILTER_TIMEOUT_S = 30.0


def _load_prompt(path: Path) -> str:
    """Load a prompt from ``path`` using the bare triple-backtick convention.

    Shared between the Fetcher-LLM and Feed-Filter-LLM prompt loaders.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
        ValueError: If the fence block is missing, unclosed, or empty.
    """
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    matches = list(_FENCE_RE.finditer(text))
    if len(matches) < 1:
        raise ValueError(f"No ``` fence in {path}")
    if len(matches) < 2:
        raise ValueError(f"Unclosed ``` fence in {path}")
    start = matches[0].end()
    end = matches[1].start()
    prompt = text[start:end].strip()
    if not prompt:
        raise ValueError(f"Empty fence block in {path}")
    return prompt


def load_fetcher_prompt(path: Path | None = None) -> str:
    """Load the Fetcher-LLM system prompt from ``docs/Fetcher_Prompt.md``.

    Args:
        path: Override path (for tests). If ``None``, uses the default.

    Returns:
        The prompt text between the first pair of bare triple-backtick fences.

    Raises:
        FileNotFoundError: If the prompt file is missing.
        ValueError: If the fence block is malformed or empty.
    """
    return _load_prompt(path or _FETCHER_PROMPT_PATH)


def load_feed_filter_prompt(path: Path | None = None) -> str:
    """Load the Feed-Filter-LLM system prompt from ``docs/Feed_Filter_Prompt.md``.

    Args:
        path: Override path (for tests). If ``None``, uses the default.

    Returns:
        The prompt text between the first pair of bare triple-backtick fences.

    Raises:
        FileNotFoundError: If the prompt file is missing.
        ValueError: If the fence block is malformed or empty.
    """
    return _load_prompt(path or _FEED_FILTER_PROMPT_PATH)


class WebResearchService:
    """Orchestrates a single research query end-to-end.

    Typical usage::

        svc = WebResearchService()
        results = svc.research("ECB rate decision")
        for r in results:
            print(r.title, r.key_facts)
    """

    def __init__(
        self,
        allowlist: AllowlistConfig | None = None,
        fetcher_prompt: str | None = None,
        feed_filter_prompt: str | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            allowlist: Pre-loaded allowlist config (mainly for tests). If
                ``None``, loads from the default path via
                :func:`~services.web_research.allowlist.load_allowlist`.
            fetcher_prompt: Pre-loaded Fetcher-LLM system prompt (mainly
                for tests). If ``None``, loads from
                ``docs/Fetcher_Prompt.md``.
            feed_filter_prompt: Pre-loaded Feed-Filter-LLM system prompt
                (mainly for tests). If ``None``, loads from
                ``docs/Feed_Filter_Prompt.md``.

        Raises:
            AllowlistError: If the allowlist cannot be loaded.
            FileNotFoundError / ValueError: If a prompt cannot be loaded.
        """
        self._allowlist = allowlist if allowlist is not None else load_allowlist()
        self._fetcher_prompt = (
            fetcher_prompt if fetcher_prompt is not None else load_fetcher_prompt()
        )
        self._feed_filter_prompt = (
            feed_filter_prompt if feed_filter_prompt is not None else load_feed_filter_prompt()
        )

    def research(
        self,
        query: str,
        max_articles: int = 5,
    ) -> list[WebResearchResult]:
        """Resolve, pre-filter, fetch, and extract validated results.

        Per-feed and per-article failures (HTTP errors, extraction errors,
        validation failures) are logged at WARNING and skipped. The method
        never raises for an individual source failure; it returns whatever
        fully validated results it was able to produce, which may be the
        empty list.

        Args:
            query: Free-text research query.
            max_articles: Upper bound on article fetches after pre-filter.
                Default 5.

        Returns:
            A list of :class:`WebResearchResult` objects in the order the
            Feed-Filter-LLM ranked them.
        """
        # --- Stage 1: harvest feed items ---
        all_items = self._harvest_feed_items()
        if not all_items:
            logger.info(
                "WebResearchService.research: query=%r — no feed items "
                "survived time-window filtering.",
                query,
            )
            return []

        # --- Stage 2: LLM pre-filter ---
        selected = self._pre_filter_feed_items(query, all_items, max_articles)
        logger.info(
            "WebResearchService.research: query=%r — %d items sent to pre-filter, %d returned.",
            query,
            len(all_items),
            len(selected),
        )
        if not selected:
            return []

        # --- Stage 3: per-article fetch + Fetcher-LLM ---
        results: list[WebResearchResult] = []
        for item in selected:
            result = self._research_one(item.url)
            if result is not None:
                results.append(result)

        logger.info(
            "WebResearchService.research: query=%r — %d/%d pre-filtered "
            "items yielded validated results.",
            query,
            len(results),
            len(selected),
        )
        return results

    # ------------------------------------------------------------------
    # Stage 1 — feed harvesting
    # ------------------------------------------------------------------

    def harvest_items(self) -> list[FeedItem]:
        """Harvest all in-window feed items (no query, no LLM).

        The public, query-free entry point Irene's RSS delta uses to gather
        the tag-carrying :class:`FeedItem`s it clusters (ADR-0087 Part B).
        Unlike :meth:`research`, this neither pre-filters by relevance nor
        fetches article bodies — it returns exactly the items that survive
        the per-source time window, with their curated source tags attached.
        Inherits the harvest's tolerance: a per-feed failure is logged and
        skipped, and a total failure yields an empty list rather than an
        exception.

        Returns:
            The harvested :class:`FeedItem`s (possibly empty).
        """
        return self._harvest_feed_items()

    def _harvest_feed_items(self) -> list[FeedItem]:
        """Fetch all configured feeds and apply per-entry time windows."""
        entries: tuple[AllowlistEntry, ...] = self._allowlist.entries
        default_hours = self._allowlist.default_window_hours
        now = datetime.now(timezone.utc)

        feeds_fetched = 0
        feeds_failed = 0
        items_before_time_filter = 0
        items_after_time_filter = 0

        combined: list[FeedItem] = []
        for entry in entries:
            window = get_effective_window(entry, default_hours)
            cutoff = now - window
            for feed_url in entry.feeds:
                feeds_fetched += 1
                try:
                    fetched = fetch_feed(feed_url)
                except FetchError as exc:
                    feeds_failed += 1
                    logger.warning(
                        "WebResearchService: feed fetch failed for %s (source=%s): %s",
                        feed_url,
                        entry.name,
                        exc,
                    )
                    continue
                logger.debug(
                    "WebResearchService: fetched feed %s → %d (%d bytes, final=%s)",
                    feed_url,
                    fetched.status_code,
                    fetched.content_length,
                    fetched.final_url,
                )

                # Post-redirect allowlist re-check on the feed URL.
                if not is_allowed(fetched.final_url, list(self._allowlist.entries)):
                    feeds_failed += 1
                    logger.warning(
                        "WebResearchService: feed %s redirected to non-"
                        "allowlisted URL %s; dropping.",
                        feed_url,
                        fetched.final_url,
                    )
                    continue

                try:
                    items = parse_feed(fetched.raw_bytes, entry.name, entry.tags)
                except FeedParseError as exc:
                    feeds_failed += 1
                    logger.warning(
                        "WebResearchService: feed parse failed for %s (source=%s): %s",
                        feed_url,
                        entry.name,
                        exc,
                    )
                    continue

                items_before_time_filter += len(items)
                in_window = [it for it in items if it.published_at >= cutoff]
                items_after_time_filter += len(in_window)
                logger.debug(
                    "WebResearchService: feed %s (source=%s) — %d parsed, %d within %dh window.",
                    feed_url,
                    entry.name,
                    len(items),
                    len(in_window),
                    int(window.total_seconds() // 3600),
                )
                combined.extend(in_window)

        logger.info(
            "WebResearchService: harvest — feeds_fetched=%d, feeds_failed=%d, "
            "items_before_time_filter=%d, items_after_time_filter=%d.",
            feeds_fetched,
            feeds_failed,
            items_before_time_filter,
            items_after_time_filter,
        )
        return combined

    # ------------------------------------------------------------------
    # Stage 2 — Feed-Filter-LLM
    # ------------------------------------------------------------------

    def _pre_filter_feed_items(
        self,
        query: str,
        items: list[FeedItem],
        max_articles: int,
    ) -> list[FeedItem]:
        """Ask the Feed-Filter-LLM which candidates are relevant to ``query``.

        Returns the matched :class:`FeedItem` objects, preserving the LLM's
        relevance ordering. On any failure (no active model, validation
        failure, raised exception), returns an empty list — we refuse to
        proceed with unvalidated URLs.
        """
        ai = get_ai_service()
        model = ai.get_model()
        if not model:
            logger.warning(
                "WebResearchService: no active model selected; cannot run Feed-Filter-LLM."
            )
            return []

        candidate_block = _render_candidates(items)
        user_content = (
            f"query: {query}\n"
            f"max_articles: {max_articles}\n\n"
            "--- BEGIN CANDIDATES ---\n"
            f"{candidate_block}\n"
            "--- END CANDIDATES ---\n"
        )
        messages = [
            {"role": "system", "content": self._feed_filter_prompt},
            {"role": "user", "content": user_content},
        ]
        logger.debug(
            "WebResearchService: pre-filter candidate list length=%d.",
            len(items),
        )

        logger.info(
            "WebResearchService: pre-filter candidates being sent to LLM:\n%s",
            "\n".join(
                f"  [{i + 1}] [{item.source_name[:30]:30s}] "
                f"({item.published_at.strftime('%Y-%m-%d %H:%M')}) {item.title}"
                for i, item in enumerate(items)
            ),
        )

        try:
            raw = ai.send_one_shot_extraction(
                messages=messages,
                model=model,
                temperature=_FILTER_TEMPERATURE,
                timeout=_FILTER_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — filter-level boundary
            logger.warning(
                "WebResearchService: Feed-Filter-LLM call raised %s: %s",
                type(exc).__name__,
                exc,
            )
            return []

        try:
            parsed = parse_extraction_response(raw)
        except JsonParseError as exc:
            logger.warning(
                "WebResearchService: Feed-Filter-LLM returned non-JSON (%s). Raw preview: %r",
                exc,
                raw[:300],
            )
            return []

        try:
            filter_result = FeedFilterResult.model_validate(parsed)
        except ValidationError as exc:
            logger.warning(
                "WebResearchService: Feed-Filter-LLM output failed schema "
                "validation. Errors: %s. Raw preview: %r",
                exc.errors(),
                raw[:300],
            )
            return []

        # Defence in depth: restrict to URLs actually in the candidate list.
        url_to_item = {it.url: it for it in items}
        selected_ordered: list[FeedItem] = []
        invented: list[str] = []
        for url in filter_result.selected_urls:
            item = url_to_item.get(url)
            if item is None:
                invented.append(url)
                continue
            selected_ordered.append(item)
        if invented:
            logger.warning(
                "WebResearchService: Feed-Filter-LLM returned %d URL(s) not "
                "in the candidate list; rejecting them. Examples: %r",
                len(invented),
                invented[:3],
            )

        # Honour max_articles even if the LLM exceeded it.
        return selected_ordered[:max_articles]

    # ------------------------------------------------------------------
    # Stage 3 — per-article fetch + Fetcher-LLM
    # ------------------------------------------------------------------

    def _research_one(self, candidate_url: str) -> WebResearchResult | None:
        """Fetch + extract + validate one candidate URL.

        Returns ``None`` on any failure (logged at WARNING). All exception
        paths are handled here so that :meth:`research` never raises on a
        per-source issue.
        """
        entries = list(self._allowlist.entries)

        try:
            fetched = fetch_url(candidate_url)
        except FetchError as exc:
            logger.warning(
                "WebResearchService: article fetch failed for %s — %s",
                candidate_url,
                exc,
            )
            return None

        logger.debug(
            "WebResearchService: fetched article %s → %d (%d chars, final=%s)",
            candidate_url,
            fetched.status_code,
            fetched.content_length,
            fetched.final_url,
        )

        if not is_allowed(fetched.final_url, entries):
            logger.warning(
                "WebResearchService: post-redirect URL %s (from %s) is not "
                "on the allowlist; dropping.",
                fetched.final_url,
                candidate_url,
            )
            return None

        try:
            text = extract_text(fetched.raw_html, fetched.final_url)
        except ExtractionError as exc:
            logger.warning(
                "WebResearchService: extraction failed for %s — %s",
                fetched.final_url,
                exc,
            )
            return None

        if len(text) > _FETCHER_MAX_INPUT_CHARS:
            logger.info(
                "WebResearchService: truncating extracted text from %d to %d "
                "chars before Fetcher-LLM call (%s).",
                len(text),
                _FETCHER_MAX_INPUT_CHARS,
                fetched.final_url,
            )
            text = text[:_FETCHER_MAX_INPUT_CHARS]

        raw_llm = self._call_fetcher_llm(
            url=fetched.final_url,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            extracted_text=text,
        )
        if raw_llm is None:
            return None

        try:
            parsed = parse_extraction_response(raw_llm)
        except JsonParseError as exc:
            logger.warning(
                "WebResearchService: Fetcher-LLM returned non-JSON for %s (%s). Raw preview: %r",
                fetched.final_url,
                exc,
                raw_llm[:300],
            )
            return None

        try:
            result = WebResearchResult.model_validate(parsed)
        except ValidationError as exc:
            logger.warning(
                "WebResearchService: Fetcher-LLM output failed schema "
                "validation for %s. Errors: %s. Raw preview: %r",
                fetched.final_url,
                exc.errors(),
                raw_llm[:300],
            )
            return None

        if result.injection_detected:
            logger.info(
                "WebResearchService: injection flag set on %s — %s",
                fetched.final_url,
                result.injection_details,
            )

        return result

    def _call_fetcher_llm(
        self,
        url: str,
        fetched_at: str,
        extracted_text: str,
    ) -> str | None:
        """Invoke the Fetcher-LLM via AIService.send_one_shot_extraction.

        Returns the raw string response, or ``None`` on any exception. The
        Fetcher-LLM is told which URL and timestamp to echo back so that
        downstream Shirley-side citations line up with the real resolved URL.
        """
        ai = get_ai_service()
        model = ai.get_model()
        if not model:
            logger.warning(
                "WebResearchService: no active model selected; cannot invoke Fetcher-LLM for %s.",
                url,
            )
            return None

        user_content = (
            f"source_url: {url}\n"
            f"fetched_at: {fetched_at}\n\n"
            "--- BEGIN FETCHED TEXT ---\n"
            f"{extracted_text}\n"
            "--- END FETCHED TEXT ---\n"
        )
        messages = [
            {"role": "system", "content": self._fetcher_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            return ai.send_one_shot_extraction(
                messages=messages,
                model=model,
                temperature=_FETCHER_TEMPERATURE,
                timeout=_FETCHER_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — per-source boundary
            logger.warning(
                "WebResearchService: Fetcher-LLM call raised %s for %s: %s",
                type(exc).__name__,
                url,
                exc,
            )
            return None


def _render_candidates(items: list[FeedItem]) -> str:
    """Render feed items as a compact plain-text candidate list.

    One numbered block per item: URL, title, description (truncated),
    publication date, source name. Keeps the input compact so the
    Feed-Filter-LLM's context is dominated by the candidates, not by
    boilerplate.
    """
    blocks: list[str] = []
    for i, item in enumerate(items, start=1):
        desc = item.description or ""
        if len(desc) > 400:
            desc = desc[:400].rstrip() + "…"
        blocks.append(
            f"[{i}] URL: {item.url}\n"
            f"    Title: {item.title}\n"
            f"    Description: {desc}\n"
            f"    Published: {item.published_at.isoformat()}\n"
            f"    Source: {item.source_name}"
        )
    return "\n\n".join(blocks)
