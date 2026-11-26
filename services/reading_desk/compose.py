# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure composition of a reading from one scraper extraction (ADR-0139 §2, §5).

No database here: every function takes plain values and returns plain
values, so the composition is testable without a connection. The writer in
:mod:`services.reading_desk.writer` passes the result of
:func:`compose_reading` to ``DocumentReadingRepository.create`` unchanged.

A reading holds untrusted GP text, so no message raised here repeats a value
taken from the extraction.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from core.exceptions import PortfoliFlowError
from services.scraper.bank_filter import remove_bank_details
from services.scraper.models import Finding, Keyword, ReadingNote, ReportExtraction

#: The file name the route has always used when an upload carries none.
_UNTITLED = "untitled.pdf"
#: What PostgreSQL TEXT/JSONB refuses (NUL) and UTF-8 cannot encode (lone surrogates).
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")
#: The only origin of this strand; door 2 (``case_attachment``) is strand RD-3.
_ORIGIN_UPLOAD = "upload"


class ReadingNotComposable(PortfoliFlowError, ValueError):
    """The extraction is not a successful reading, so no reading is composed."""


@dataclass(frozen=True)
class UploadedDocument:
    """What the route knows about one uploaded document.

    Attributes:
        filename: The sanitised file name, never empty.
        sha256: The lower-case hex SHA-256 of the bytes.
        size_bytes: The size of the bytes.
    """

    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class AssignmentProposal:
    """A proposed assignment, as text only; nothing is assigned (strand RD-2).

    Attributes:
        kind: ``"investment"`` or ``"manager"``.
        name: The stored investment name or manager name, as stored.
    """

    kind: Literal["investment", "manager"]
    name: str


def sanitise_text(text: str) -> str:
    """Remove every NUL and every lone surrogate code point from ``text``.

    PostgreSQL refuses NUL in TEXT and JSONB, and a lone surrogate cannot be
    encoded to UTF-8, so either would lose the reading at the write. All
    other characters, astral ones included, stay.

    Args:
        text: Any string.

    Returns:
        The string without NUL and without code points U+D800 to U+DFFF.
    """
    return _UNSTORABLE.sub("", text)


def describe_upload(filename: str, data: bytes) -> UploadedDocument:
    """Describe one upload by its sanitised file name, its hash and its size.

    Args:
        filename: The file name the client sent.
        data: The uploaded bytes.

    Returns:
        The description; the file name is ``untitled.pdf`` when nothing is
        left of it after sanitising and stripping.
    """
    name = sanitise_text(filename).strip()
    return UploadedDocument(
        filename=name or _UNTITLED,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def _blank_to_none(value: str) -> str | None:
    """``None`` for a value that is empty after stripping, else the value as read."""
    return value if value.strip() else None


def _label(finding: Finding) -> str:
    return finding.label or finding.keyword.name


def _finding_dict(finding: Finding) -> dict[str, Any]:
    return {
        "key": finding.key,
        "label": _label(finding),
        "value": finding.value,
        "type": finding.keyword.type.value,
        "unit": finding.unit,
        "page": finding.page,
        "source": finding.source,
        "quote": finding.quote,
        "confidence": finding.confidence.value,
        "origin": finding.origin,
    }


def _note_dict(note: ReadingNote | None) -> dict[str, Any]:
    if note is None:
        return {"highlights": [], "quotes": [], "events_and_risks": [], "outlook": ""}
    return {
        "highlights": list(note.highlights),
        "quotes": [{"text": quote.text, "page": quote.page} for quote in note.quotes],
        "events_and_risks": list(note.events_and_risks),
        "outlook": note.outlook,
    }


def compose_findings(extraction: ReportExtraction) -> list[dict[str, Any]]:
    """The findings of an extraction in the shape a reading stores them.

    The run view renders a result from this shape, so a result block and the
    stored reading show the same thing (D-RD-2-10).
    """
    return [_finding_dict(finding) for finding in extraction.findings]


def compose_note(extraction: ReportExtraction) -> dict[str, Any]:
    """The reading note of an extraction in the shape a reading stores it."""
    return _note_dict(extraction.note)


def compose_search_text(extraction: ReportExtraction, filename: str) -> str:
    """Compose the plain text the library search matches (ADR-0139 §2).

    One piece per line, blank pieces skipped, in this order: the head (file
    name, fund name, manager name, period, document date as ISO date,
    currency); per finding with a non-blank value its line ``label: value
    unit`` and then its quote; the note's highlights, quote texts, events and
    risks, and outlook. The joined text passes the bank-detail filter once
    more, since joining filtered pieces can set a term and a number side by
    side again.

    Args:
        extraction: The extraction the reading is composed from.
        filename: The document's file name.

    Returns:
        The search text, without bank details.
    """
    pieces: list[str] = [
        filename,
        extraction.fund_name,
        extraction.manager_name,
        extraction.period,
        extraction.document_date.isoformat() if extraction.document_date is not None else "",
        extraction.currency,
    ]
    for finding in extraction.findings:
        if not finding.value.strip():
            continue
        pieces.append(f"{_label(finding)}: {finding.value} {finding.unit}".strip())
        pieces.append(finding.quote)
    note = extraction.note
    if note is not None:
        pieces.extend(note.highlights)
        pieces.extend(quote.text for quote in note.quotes)
        pieces.extend(note.events_and_risks)
        pieces.append(note.outlook)
    joined = "\n".join(piece.strip() for piece in pieces if piece.strip())
    return remove_bank_details(joined)


def compose_reading(
    extraction: ReportExtraction,
    document: UploadedDocument,
    *,
    read_by: UUID,
    model: str,
    prompt_version: str,
    keywords: Sequence[Keyword],
    now: datetime,
) -> dict[str, Any]:
    """Compose the keyword arguments of ``DocumentReadingRepository.create``.

    The findings are neither filtered nor validated here: the repository
    validates, and its refusal reaches the writer.

    Args:
        extraction: One extraction of the run.
        document: The upload the extraction belongs to (paired by position).
        read_by: The user who started the run.
        model: The model that read the document.
        prompt_version: The version of the prompt the run used.
        keywords: The run's ad-hoc keywords.
        now: The timestamp for ``read_at``.

    Returns:
        Exactly the keyword arguments of ``create``.

    Raises:
        ReadingNotComposable: If the extraction failed, has no document type
            or has no profile version.
    """
    if extraction.error is not None:
        raise ReadingNotComposable("The extraction failed, so there is no reading to compose.")
    if not extraction.document_type.strip():
        raise ReadingNotComposable("The extraction has no document type.")
    if extraction.profile_version is None:
        raise ReadingNotComposable("The extraction has no profile version.")
    return {
        "filename": document.filename,
        "sha256": document.sha256,
        "size_bytes": document.size_bytes,
        "page_count": extraction.page_count,
        "read_by": read_by,
        "origin": _ORIGIN_UPLOAD,
        "case_attachment_id": None,
        "document_type": extraction.document_type,
        "profile_version": extraction.profile_version,
        "language": _blank_to_none(extraction.language),
        "fund_name_read": _blank_to_none(extraction.fund_name),
        "manager_name_read": _blank_to_none(extraction.manager_name),
        "document_date": extraction.document_date,
        "period_label": _blank_to_none(extraction.period),
        "currency": _blank_to_none(extraction.currency),
        "findings": compose_findings(extraction),
        "note": compose_note(extraction),
        "model": model,
        "prompt_version": prompt_version,
        "adhoc_keywords": [
            {"name": sanitise_text(keyword.name), "type": keyword.type.value}
            for keyword in keywords
        ],
        "search_text": compose_search_text(extraction, document.filename),
        "now": now,
    }


def _fold(value: str | None) -> str:
    return (value or "").strip().casefold()


def propose_for_names(
    fund_name: str | None,
    manager_name: str | None,
    investments: Sequence[tuple[str, str | None]],
) -> AssignmentProposal | None:
    """Propose an assignment from a fund name and a manager name (ADR-0139 §6).

    The one matching rule, for a fresh extraction and for a stored reading
    alike. Both sides are compared after stripping and case folding; a blank
    name never matches. A fund name matching an investment name comes first,
    then a manager name matching a known manager name. With several equal
    matches the first in the given order wins.

    Args:
        fund_name: The fund name as read, or ``None``.
        manager_name: The manager name as read, or ``None``.
        investments: ``(name, manager_name)`` of the tenant's investments.

    Returns:
        The proposal with the stored spelling, or ``None`` without a match.
    """
    fund = _fold(fund_name)
    if fund:
        for name, _ in investments:
            if _fold(name) == fund:
                return AssignmentProposal(kind="investment", name=name)
    manager = _fold(manager_name)
    if manager:
        for _, known_manager in investments:
            if known_manager is not None and _fold(known_manager) == manager:
                return AssignmentProposal(kind="manager", name=known_manager)
    return None


def propose_assignment(
    extraction: ReportExtraction,
    investments: Sequence[tuple[str, str | None]],
) -> AssignmentProposal | None:
    """Propose an assignment from the names an extraction read (ADR-0139 §6).

    Args:
        extraction: The extraction whose fund and manager names are compared.
        investments: ``(name, manager_name)`` of the tenant's investments.

    Returns:
        The proposal of :func:`propose_for_names` for the extraction's names.
    """
    return propose_for_names(extraction.fund_name, extraction.manager_name, investments)
