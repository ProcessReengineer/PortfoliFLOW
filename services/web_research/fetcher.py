# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP fetch, HTML extraction, and RSS/Atom parsing for Web Research.

Pure functions. No DataStore, no AIService, no PyQt. This module implements
Stage 1 of the two-stage pipeline described in ADR-0023 and the feed-fetch
addition from ADR-0024. Stage 2 (the Fetcher-LLM call) lives in
:mod:`services.web_research.service`.

Trafilatura is the only article-extraction backend. There is deliberately no
BeautifulSoup fallback: rule-based extraction on arbitrary financial-news
HTML is brittle, and silent degradation under a failing extractor is more
dangerous than a logged skip at the service layer.

Feed parsing uses :mod:`feedparser` (ADR-0024). Entries without a parseable
publication timestamp, a link, or a title are dropped with a WARNING log —
honesty over silent inclusion.
"""

from __future__ import annotations

import logging
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
import trafilatura
from dateutil import parser as dateutil_parser

from core.exceptions import PortfoliFlowError
from services.web_research.models import FeedItem

logger = logging.getLogger(__name__)

# Minimum length (in characters) of extracted article text. Anything below
# this threshold is treated as an extraction failure — typical navigation /
# paywall / consent pages cleanly fall into this bucket.
_MIN_EXTRACTED_CHARS = 200

# User-Agent identifies PortfoliFLOW so site operators can recognise and
# rate-limit this traffic as they see fit.
_USER_AGENT = (
    "PortfoliFLOW-Research/0.1 (+https://github.com/ProcessReengineer/PortfoliFLOW; "
    "automated research fetch for institutional portfolio management)"
)

# Matches <time ... datetime="…"> in RSS HTML descriptions. ESMA's feed omits
# every standard timestamp field and embeds the publication time only inside
# the description HTML via Drupal's <time datetime="…"> markup.
_HTML_TIME_RE = re.compile(r'<time\b[^>]*\bdatetime="([^"]+)"', re.IGNORECASE)


class FetchError(PortfoliFlowError):
    """Raised when an HTTP fetch fails or yields a non-2xx status."""


class ExtractionError(PortfoliFlowError):
    """Raised when article text extraction yields too little clean content."""


class FeedParseError(PortfoliFlowError):
    """Raised when :func:`parse_feed` cannot produce any valid feed items."""


@dataclass(frozen=True)
class FetchResult:
    """The outcome of a successful HTTP fetch.

    Attributes:
        final_url: The URL after redirects. This is the URL that must be
            re-checked against the allowlist — an allowlisted domain can
            redirect to a non-allowlisted one.
        status_code: The HTTP status code of the final response.
        raw_html: The response body as text.
        content_length: The length of ``raw_html`` in characters.
    """

    final_url: str
    status_code: int
    raw_html: str
    content_length: int


@dataclass(frozen=True)
class FeedFetchResult:
    """The outcome of a successful feed fetch (ADR-0024).

    Attributes:
        final_url: The URL after redirects.
        status_code: The HTTP status code of the final response.
        raw_bytes: The response body as raw bytes — feedparser handles the
            character encoding, so we avoid decoding here.
        content_length: The byte length of ``raw_bytes``.
    """

    final_url: str
    status_code: int
    raw_bytes: bytes
    content_length: int


def fetch_url(url: str, timeout: float = 8.0) -> FetchResult:
    """Fetch a URL via HTTPS GET, following redirects.

    Args:
        url: The URL to fetch.
        timeout: Per-request timeout in seconds.

    Returns:
        A :class:`FetchResult` on HTTP 2xx.

    Raises:
        FetchError: On any network failure, timeout, or non-2xx status.
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        with httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise FetchError(f"Timeout fetching {url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"HTTP error fetching {url}: {exc}") from exc

    if response.status_code >= 400:
        raise FetchError(
            f"Fetch of {url} returned HTTP {response.status_code} (final URL: {response.url})."
        )

    raw = response.text
    return FetchResult(
        final_url=str(response.url),
        status_code=response.status_code,
        raw_html=raw,
        content_length=len(raw),
    )


def extract_text(raw_html: str, url: str) -> str:
    """Extract clean article text from HTML using trafilatura.

    Uses ``favor_precision=True`` (prefer dropping ambiguous blocks over
    including boilerplate) and ``include_comments=False`` (comments are a
    common prompt-injection vector).

    Args:
        raw_html: The HTML source.
        url: The URL the HTML came from — passed to trafilatura for any
            URL-dependent heuristics. Only used for logging on failure.

    Returns:
        The extracted article text.

    Raises:
        ExtractionError: If extraction returns ``None`` or produces fewer
            than :data:`_MIN_EXTRACTED_CHARS` characters. The service layer
            logs this and moves on to the next candidate URL.
    """
    try:
        extracted = trafilatura.extract(
            raw_html,
            url=url,
            favor_precision=True,
            include_comments=False,
        )
    except Exception as exc:  # noqa: BLE001 — trafilatura wraps many error types
        raise ExtractionError(
            f"trafilatura.extract raised {type(exc).__name__} for {url}: {exc}"
        ) from exc

    if extracted is None or len(extracted) < _MIN_EXTRACTED_CHARS:
        length = 0 if extracted is None else len(extracted)
        raise ExtractionError(
            f"Extracted only {length} characters from {url} "
            f"(minimum {_MIN_EXTRACTED_CHARS}); treating as extraction failure."
        )
    return extracted


def fetch_feed(url: str, timeout: float = 8.0) -> FeedFetchResult:
    """Fetch an RSS/Atom feed via HTTPS GET, following redirects.

    Returns raw bytes so the caller can hand them to
    :func:`parse_feed`/:mod:`feedparser` without pre-decoding. Accept
    headers advertise RSS/Atom/XML; servers occasionally return
    ``text/html`` anyway, so we do not enforce a content-type check here.

    Args:
        url: The feed URL to fetch.
        timeout: Per-request timeout in seconds.

    Returns:
        A :class:`FeedFetchResult` on HTTP 2xx.

    Raises:
        FetchError: On any network failure, timeout, or non-2xx status.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": (
            "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
            "text/xml;q=0.8, */*;q=0.5"
        ),
    }
    try:
        with httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise FetchError(f"Timeout fetching feed {url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"HTTP error fetching feed {url}: {exc}") from exc

    if response.status_code >= 400:
        raise FetchError(
            f"Feed fetch of {url} returned HTTP {response.status_code} (final URL: {response.url})."
        )

    body = response.content
    return FeedFetchResult(
        final_url=str(response.url),
        status_code=response.status_code,
        raw_bytes=body,
        content_length=len(body),
    )


def _coerce_published_at(entry: Any) -> tuple[datetime | None, list[str]]:
    """Derive a UTC publication timestamp from a feedparser entry.

    Tries, in order:

    1. ``published_parsed`` / ``updated_parsed`` (feedparser's own struct_time).
    2. The string fields ``published`` / ``updated`` / ``date`` / ``dc_date``,
       parsed with :func:`dateutil.parser.parse`. Some feeds (notably older
       Dublin-Core-only feeds) populate the string field but not the struct.
    3. A ``<time datetime="…">`` element embedded in the ``summary`` or
       ``description`` HTML. ESMA's RSS feed has no timestamp at the entry
       level and exposes the publication time only here.

    Args:
        entry: A feedparser entry (dict-like).

    Returns:
        A tuple ``(published_at, tried_fields)`` where ``published_at`` is a
        timezone-aware UTC :class:`datetime` if any strategy succeeded, or
        ``None`` if every strategy failed. ``tried_fields`` lists every
        field that was attempted, in order — surfaced in the drop WARNING
        log so operators can diagnose why an entry was rejected.
    """
    tried: list[str] = []

    for struct_field in ("published_parsed", "updated_parsed"):
        tried.append(struct_field)
        struct_time = entry.get(struct_field)
        if struct_time is not None:
            try:
                epoch = timegm(struct_time)
                return datetime.fromtimestamp(epoch, tz=timezone.utc), tried
            except (TypeError, ValueError, OverflowError):
                pass

    for str_field in ("published", "updated", "date", "dc_date"):
        tried.append(str_field)
        val = entry.get(str_field)
        if isinstance(val, str) and val.strip():
            try:
                dt = dateutil_parser.parse(val)
            except (ValueError, TypeError, OverflowError):
                continue
            return _to_utc(dt), tried

    for html_field in ("summary", "description"):
        tried.append(f"{html_field}:<time datetime>")
        html = entry.get(html_field)
        if not isinstance(html, str):
            continue
        match = _HTML_TIME_RE.search(html)
        if not match:
            continue
        try:
            dt = dateutil_parser.parse(match.group(1))
        except (ValueError, TypeError, OverflowError):
            continue
        return _to_utc(dt), tried

    return None, tried


def _to_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a timezone-aware UTC datetime.

    Naive inputs are assumed to already be UTC — feed timestamps without a
    timezone are rare and the safer default is UTC over the local wall clock.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(raw: bytes, source_name: str, tags: tuple[str, ...] = ()) -> list[FeedItem]:
    """Parse feed bytes into :class:`FeedItem`s.

    Uses :func:`feedparser.parse`. For each entry:

    - ``link`` and ``title`` are required; entries missing either are
      dropped with a WARNING.
    - Publication time comes from ``published_parsed`` (or
      ``updated_parsed`` as a fallback). Entries without either are
      dropped with a WARNING — ADR-0024 prefers honest drop over
      guesswork.
    - Publication times are normalised to timezone-aware UTC via
      :meth:`FeedItem.from_components`.

    Args:
        raw: The raw feed body bytes.
        source_name: Human-readable name of the owning allowlist entry,
            attached to each produced :class:`FeedItem`.
        tags: The owning allowlist entry's curated tags (ADR-0087 Part B),
            propagated onto every produced :class:`FeedItem`. Default
            ``()`` for an untagged source.

    Returns:
        A list of :class:`FeedItem`, in feed order. May be empty if every
        entry was dropped.

    Raises:
        FeedParseError: If the feed cannot be parsed at all (fatal
            structural error — feedparser signals this via ``bozo`` with
            a non-recoverable exception and no entries).
    """
    parsed = feedparser.parse(raw)

    entries = parsed.get("entries") or []
    if not entries:
        # feedparser sets bozo=1 on malformed input. If bozo is set AND
        # there are no entries at all, we treat this as unparseable.
        bozo_exc = parsed.get("bozo_exception")
        if parsed.get("bozo") and bozo_exc is not None:
            raise FeedParseError(
                f"feedparser could not parse feed for {source_name!r}: "
                f"{type(bozo_exc).__name__}: {bozo_exc}"
            )
        logger.info(
            "parse_feed: feed for %s contains no entries.",
            source_name,
        )
        return []

    items: list[FeedItem] = []
    for i, entry in enumerate(entries):
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            logger.warning(
                "parse_feed: dropping %s entry %d — missing link or title (link=%r, title=%r).",
                source_name,
                i,
                link,
                title,
            )
            continue

        published_at, tried_fields = _coerce_published_at(entry)
        if published_at is None:
            logger.warning(
                "parse_feed: dropping %s entry %d (%s) — no parseable "
                "publication timestamp (tried: %s).",
                source_name,
                i,
                link,
                ", ".join(tried_fields),
            )
            continue

        description_raw = entry.get("summary") or entry.get("description")
        description = description_raw.strip() if isinstance(description_raw, str) else None

        items.append(
            FeedItem.from_components(
                url=link,
                title=title,
                description=description,
                published_at=published_at,
                source_name=source_name,
                tags=tags,
            )
        )

    logger.debug(
        "parse_feed: %s — %d entries in, %d items out.",
        source_name,
        len(entries),
        len(items),
    )
    return items
