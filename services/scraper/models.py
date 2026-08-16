# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data models for the Report Scraper.

Plain dataclasses, decoupled from PyQt and from any API wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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


@dataclass
class Finding:
    """A single extracted value for one keyword in one report.

    Attributes:
        keyword: The keyword this finding corresponds to.
        value: The extracted value as a string, or "" if not found.
        source: Short source reference (e.g. "Page 12, Cashflow Statement").
        confidence: One of the four Confidence levels.
    """

    keyword: Keyword
    value: str
    source: str
    confidence: Confidence


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
    """

    filename: str
    fund_name: str = ""
    period: str = ""
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None


@dataclass
class ScraperResult:
    """The aggregated result of a scrape run.

    Attributes:
        extractions: One ReportExtraction per input file, in input order.
        cancelled: True if the run was cancelled via the cancel_check callback.
    """

    extractions: list[ReportExtraction] = field(default_factory=list)
    cancelled: bool = False
