# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pydantic / dataclass schemas for the Web Research capability (ADR-0023, ADR-0024).

:class:`WebResearchResult` is the structured envelope produced by the
Fetcher-LLM for a single article. Its shape is the public output of the
capability and must stay stable — the field list mirrors
``docs/Fetcher_Prompt.md`` and drift between them is an audit-relevant
defect.

:class:`FeedItem` and :class:`FeedFilterResult` are new under ADR-0024 and
support the RSS-based resolution flow:

- :class:`FeedItem` represents one entry parsed from an RSS/Atom feed.
- :class:`FeedFilterResult` is the output envelope of the Feed-Filter-LLM
  (``docs/Feed_Filter_Prompt.md``) — a list of candidate URLs the filter
  judged relevant to the query.

Neither intermediate type is exposed to Shirley. Only validated
:class:`WebResearchResult` payloads, wrapped in the ADR-0022 trust
delimiters, reach her conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_MAX_KEY_FACTS = 10
_MAX_FACT_LEN = 300
_MAX_SELECTED_URLS = 20

_ALLOWED_ASSET_CLASSES: frozenset[str] = frozenset(
    {
        "equities",
        "fixed_income",
        "private_equity",
        "real_estate",
        "infrastructure",
        "private_debt",
        "hedge_funds",
        "commodities",
        "regulation",
        "macro",
        "m_and_a",
        "secondaries",
        "esg",
        "other",
    }
)


class WebResearchResult(BaseModel):
    """Structured envelope produced by the Fetcher-LLM for a single source.

    Attributes:
        source_url: The URL the content was fetched from. Copied verbatim
            from the fetch pipeline; the Fetcher-LLM is told to echo it
            back but the service layer trusts its own resolved URL, not
            the model's claim.
        fetched_at: ISO-8601 timestamp of the fetch. Timezone-aware.
        title: Article title (from the page or derived by the Fetcher-LLM).
        publication_date: Article publication date if stated; otherwise
            ``None``.
        key_facts: Up to ``_MAX_KEY_FACTS`` short factual statements. Each
            entry is capped at ``_MAX_FACT_LEN`` characters.
        relevant_asset_classes: Zero or more values from a fixed set
            documented in ``docs/Fetcher_Prompt.md``. Unknown values are
            rejected at validation time.
        injection_detected: True if the Fetcher-LLM saw text that appeared
            to be an instruction targeted at an AI agent inside the input.
        injection_details: Human-readable description of the detected
            attempt, or ``None``. Required (non-``None``) when
            ``injection_detected`` is ``True``.
    """

    model_config = ConfigDict(extra="forbid")

    source_url: str
    fetched_at: datetime
    title: str
    publication_date: date | None = None
    key_facts: list[str] = Field(default_factory=list)
    relevant_asset_classes: list[str] = Field(default_factory=list)
    injection_detected: bool = False
    injection_details: str | None = None

    @field_validator("key_facts")
    @classmethod
    def _check_key_facts(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_KEY_FACTS:
            raise ValueError(f"key_facts may contain at most {_MAX_KEY_FACTS} items; got {len(v)}.")
        for i, fact in enumerate(v):
            if not isinstance(fact, str):
                raise TypeError(f"key_facts[{i}] must be a string; got {type(fact).__name__}.")
            if len(fact) > _MAX_FACT_LEN:
                raise ValueError(
                    f"key_facts[{i}] exceeds {_MAX_FACT_LEN} characters (got {len(fact)})."
                )
        return v

    @field_validator("relevant_asset_classes")
    @classmethod
    def _check_asset_classes(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - _ALLOWED_ASSET_CLASSES)
        if unknown:
            raise ValueError(
                f"Unknown asset_class values: {unknown}. Allowed: {sorted(_ALLOWED_ASSET_CLASSES)}."
            )
        return v


@dataclass(frozen=True)
class FeedItem:
    """One entry parsed from an RSS/Atom feed (ADR-0024).

    All fields originate in the feed; ``source_name`` is attached by the
    parser from the owning allowlist entry so downstream logging and
    attribution do not lose the publisher identity.

    Attributes:
        url: The article URL from the feed entry's ``link`` field.
        title: The feed entry title.
        description: Short description or summary, if the feed provides
            one; ``None`` otherwise.
        published_at: Publication timestamp, normalised to a timezone-aware
            UTC :class:`~datetime.datetime`. Naive inputs are coerced to
            UTC (see :meth:`from_components`). Items without a parseable
            publication time are dropped upstream — this field is never
            ``None``.
        source_name: Human-readable name of the feed's allowlist entry,
            for logging and attribution.
        tags: The owning allowlist entry's curated bucket-dimension tags,
            propagated onto every item harvested from its feeds (ADR-0087
            Part B). Multi-valued and deterministic; an empty tuple marks
            an untagged source. This is a curated, source-level input — it
            is formed *before* any LLM sees the item, which is what lets it
            drive Irene's deterministic clustering key.
    """

    url: str
    title: str
    description: str | None
    published_at: datetime
    source_name: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Enforce timezone-awareness on ``published_at``.

        Raises:
            ValueError: If ``published_at`` is naive.
        """
        if self.published_at.tzinfo is None:
            raise ValueError(
                "FeedItem.published_at must be timezone-aware; got naive "
                f"datetime for {self.url!r}."
            )

    @classmethod
    def from_components(
        cls,
        url: str,
        title: str,
        description: str | None,
        published_at: datetime,
        source_name: str,
        tags: tuple[str, ...] = (),
    ) -> FeedItem:
        """Construct a :class:`FeedItem`, coercing naive datetimes to UTC.

        Args:
            url: The entry URL.
            title: The entry title.
            description: Short description, or ``None``.
            published_at: Publication timestamp; if naive, is assumed to
                be UTC (and a DEBUG line is logged recording the
                assumption).
            source_name: The allowlist entry's display name.
            tags: The owning allowlist entry's curated tags, propagated
                verbatim (default ``()`` for an untagged source).

        Returns:
            A frozen :class:`FeedItem`.
        """
        if published_at.tzinfo is None:
            logger.debug(
                "FeedItem.from_components: naive datetime for %s — assuming UTC.",
                url,
            )
            published_at = published_at.replace(tzinfo=timezone.utc)
        return cls(
            url=url,
            title=title,
            description=description,
            published_at=published_at,
            source_name=source_name,
            tags=tags,
        )


class FeedFilterResult(BaseModel):
    """Output envelope of the Feed-Filter-LLM (ADR-0024).

    The model is intentionally minimal — it exists to make the LLM's
    response parseable and bounded, not to carry rich structure. Defence
    in depth against URL invention happens *after* this model validates,
    at the service layer, by filtering the returned list to URLs that
    appeared in the original candidate list.

    Attributes:
        selected_urls: URLs the filter judged relevant, ordered most
            relevant first. Must be unique, non-empty strings, capped at
            ``_MAX_SELECTED_URLS`` to bound pathological LLM output.
    """

    model_config = ConfigDict(extra="forbid")

    selected_urls: list[str] = Field(default_factory=list)

    @field_validator("selected_urls")
    @classmethod
    def _check_selected_urls(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_SELECTED_URLS:
            raise ValueError(
                f"selected_urls may contain at most {_MAX_SELECTED_URLS} items; got {len(v)}."
            )
        for i, url in enumerate(v):
            if not isinstance(url, str):
                raise TypeError(f"selected_urls[{i}] must be a string; got {type(url).__name__}.")
            if not url.strip():
                raise ValueError(f"selected_urls[{i}] is empty or whitespace-only.")
        if len(set(v)) != len(v):
            raise ValueError("selected_urls contains duplicate entries.")
        return v
