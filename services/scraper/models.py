# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data models for the Report Scraper.

Plain dataclasses, Qt-free and decoupled from any API wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

#: Where a finding comes from (ADR-0139 §2).
FINDING_ORIGIN_PROFILE = "profile"
FINDING_ORIGIN_ADHOC = "adhoc"


class KeywordType(Enum):
    """The five supported keyword types, used as formatting hints to the LLM."""

    NUMBER = "Number"
    PERCENTAGE = "Percentage"
    DATE = "Date"
    TEXT = "Text"
    LIST = "List"


class Confidence(Enum):
    """Four-level confidence for an extraction."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    NOT_FOUND = "Not_Found"


@dataclass(frozen=True)
class Keyword:
    """A single extraction keyword.

    Attributes:
        name: Display name, e.g. "NAV" or "Capital Called".
        type: The type hint passed to the LLM.
    """

    name: str
    type: KeywordType


@dataclass
class Attachment:
    """A file (or pre-extracted text) to attach to an extraction request.

    Using ``bytes | str`` for ``data`` keeps this reusable by the upcoming DD
    Support module, which will pre-extract XLSX content into text before
    sending (no current LLM understands XLSX natively).

    Attributes:
        filename: Original filename; used as label and in logs.
        mime_type: MIME type. ``"application/pdf"`` for PDFs. For pre-extracted
            text attachments, use ``"text/plain"`` or ``"text/markdown"``.
        data: Raw bytes (binary attachment) or str (pre-extracted text).
    """

    filename: str
    mime_type: str
    data: bytes | str


@dataclass(frozen=True)
class NoteQuote:
    """A quote as the model read it — unverified, there is no text layer (ADR-0139 §3).

    Attributes:
        text: The quoted passage.
        page: The page as reported by the model, or ``None``.
    """

    text: str
    page: int | None = None


@dataclass
class ReadingNote:
    """The reading note (ADR-0139 §2).

    Attributes:
        highlights: The document's main points.
        quotes: Passages as the model read them, each with its page.
        events_and_risks: The events and risks the document names.
        outlook: The outlook the document gives, or "".
    """

    highlights: list[str] = field(default_factory=list)
    quotes: list[NoteQuote] = field(default_factory=list)
    events_and_risks: list[str] = field(default_factory=list)
    outlook: str = ""


@dataclass
class Finding:
    """A single extracted value for one keyword or profile field in one report.

    Attributes:
        keyword: The keyword this finding corresponds to.
        value: The extracted value as a string, or "" if not found.
        source: Short source reference (e.g. "Page 12, Cashflow Statement").
        confidence: One of the four Confidence levels.
        key: The profile field's key, or "".
        label: The field's display label, or "".
        unit: The field's unit, or "".
        page: The page as reported by the model, or ``None``.
        quote: The supporting passage as the model read it (unverified), or "".
        origin: ``FINDING_ORIGIN_PROFILE`` or ``FINDING_ORIGIN_ADHOC`` (the default).
    """

    keyword: Keyword
    value: str
    source: str
    confidence: Confidence
    key: str = ""
    label: str = ""
    unit: str = ""
    page: int | None = None
    quote: str = ""
    origin: str = FINDING_ORIGIN_ADHOC


@dataclass
class ReportExtraction:
    """All findings for a single report file.

    Attributes:
        filename: The PDF filename.
        fund_name: Extracted fund name, or "" if the model did not return it.
        period: Extracted reporting period, or "".
        findings: One Finding per requested keyword.
        error: If the file failed to process, a human-readable error message.
            When set, ``findings`` should be considered unreliable.
        document_type: The profile id the document was read as, or "".
        profile_version: The version of that profile, or ``None``.
        language: The document's language, or "".
        manager_name: The manager as read, or "".
        document_date: The document or reporting date, or ``None``.
        currency: The document's currency, or "".
        page_count: The page count as reported by the model, or ``None``.
        note: The reading note, or ``None``.
    """

    filename: str
    fund_name: str = ""
    period: str = ""
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    document_type: str = ""
    profile_version: int | None = None
    language: str = ""
    manager_name: str = ""
    document_date: date | None = None
    currency: str = ""
    page_count: int | None = None
    note: ReadingNote | None = None


@dataclass
class ScraperResult:
    """The aggregated result of a scrape run.

    Attributes:
        extractions: One ReportExtraction per input file, in input order.
        cancelled: True if the run was cancelled via the cancel_check callback.
        prompt_version: A hash prefix of the prompt text the run used, or "".
    """

    extractions: list[ReportExtraction] = field(default_factory=list)
    cancelled: bool = False
    prompt_version: str = ""
