# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The database side of the Reading Desk writer (ADR-0139 §5, RD-D-23).

:func:`write_readings` writes one reading per successfully read document, in
one short transaction per file, and never raises for a single file: a
failure costs that file's reading and leaves one log line naming the file
and the exception type. :func:`find_earlier_readings` is the dedup lookup
(RD-D-9).

No log line carries an exception's text or a traceback: the text of a
database error carries the statement parameters, which here are document
text.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentRepository,
    tenant_context,
)
from core.repositories.document_reading_repository import is_reading_number_conflict
from services.reading_desk.compose import (
    AssignmentProposal,
    UploadedDocument,
    compose_reading,
    propose_assignment,
    sanitise_text,
)
from services.scraper.models import Keyword, ReportExtraction, ScraperResult

logger = logging.getLogger(__name__)

#: Whole-transaction attempts per file. The repository retries a reading-number
#: conflict once inside its transaction; parallel runs in one tenant defeat that.
_CREATE_ATTEMPTS = 4


@dataclass(frozen=True)
class ReadingOutcome:
    """What became of one paired extraction.

    Attributes:
        filename: The document's file name.
        label: The reading's ``R-nnnn`` when one was written, else ``None``.
        proposal: The proposed assignment, or ``None``.
    """

    filename: str
    label: str | None
    proposal: AssignmentProposal | None


async def _known_investments(
    engine: AsyncEngine, *, tenant_id: UUID, read_by: UUID
) -> list[tuple[str, str | None]] | None:
    """Read ``(name, manager_name)`` of the tenant's investments, or ``None`` on failure."""
    try:
        async with tenant_context(engine, tenant_id, user_id=read_by) as session:
            investments = await InvestmentRepository(session).list_all()
    except Exception as exc:  # noqa: BLE001 - proposals are optional; the readings still go
        logger.warning(
            "reading-desk: investments not read, no proposals for this run (%s)",
            type(exc).__name__,
        )
        return None
    return [(investment.name, investment.manager_name) for investment in investments]


async def _create_with_retry(
    engine: AsyncEngine, *, tenant_id: UUID, read_by: UUID, kwargs: dict[str, Any]
) -> str:
    """Write one reading in its own transaction, retrying a number conflict.

    Returns:
        The reading's label.

    Raises:
        Exception: Whatever the last attempt raised.
    """
    for attempt in range(1, _CREATE_ATTEMPTS + 1):
        try:
            async with tenant_context(engine, tenant_id, user_id=read_by) as session:
                reading = await DocumentReadingRepository(session).create(**kwargs)
        except IntegrityError as exc:
            if attempt < _CREATE_ATTEMPTS and is_reading_number_conflict(exc):
                continue
            raise
        return reading.label
    raise AssertionError("unreachable: the loop returns or raises")


async def _write_one(
    engine: AsyncEngine,
    *,
    tenant_id: UUID,
    read_by: UUID,
    extraction: ReportExtraction,
    document: UploadedDocument,
    result: ScraperResult,
    keywords: Sequence[Keyword],
    model: str,
    now: datetime,
    investments: list[tuple[str, str | None]] | None,
) -> ReadingOutcome:
    """Compose and write one reading; never raises except on cancellation."""
    if extraction.error is not None:
        # The run view shows the error; a failed extraction writes nothing.
        return ReadingOutcome(filename=document.filename, label=None, proposal=None)
    try:
        kwargs = compose_reading(
            extraction,
            document,
            read_by=read_by,
            model=model,
            prompt_version=result.prompt_version,
            keywords=keywords,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - ReadingNotComposable, or a malformed extraction
        _log_not_saved(document, exc)
        return ReadingOutcome(filename=document.filename, label=None, proposal=None)
    proposal = propose_assignment(extraction, investments) if investments is not None else None
    try:
        label = await _create_with_retry(
            engine, tenant_id=tenant_id, read_by=read_by, kwargs=kwargs
        )
    except Exception as exc:  # noqa: BLE001 - per file: one file's failure costs one reading
        _log_not_saved(document, exc)
        return ReadingOutcome(filename=document.filename, label=None, proposal=proposal)
    return ReadingOutcome(filename=document.filename, label=label, proposal=proposal)


def _log_not_saved(document: UploadedDocument, exc: Exception) -> None:
    # The type name only: an exception's text may carry document text.
    logger.warning(
        "reading-desk: reading not saved for %r (%s)",
        sanitise_text(document.filename),
        type(exc).__name__,
    )


async def write_readings(
    engine: AsyncEngine,
    *,
    tenant_id: UUID,
    read_by: UUID,
    documents: Sequence[UploadedDocument],
    result: ScraperResult,
    keywords: Sequence[Keyword],
    model: str,
    now: datetime | None = None,
) -> list[ReadingOutcome]:
    """Write one reading per successfully read document of a run (ADR-0139 §5).

    ``result.extractions[i]`` belongs to ``documents[i]``: a cancelled run
    is a prefix, and file names can repeat, so the pairing is by position.
    Extractions beyond the documents are not written. Each reading is
    written in its own short transaction; a failure is per file, logged
    with the file name and the exception type, and never raised.

    Args:
        engine: The application engine.
        tenant_id: The tenant the readings belong to.
        read_by: The user who started the run.
        documents: The run's uploads, in input order.
        result: The finished (or cancelled) run.
        keywords: The run's ad-hoc keywords.
        model: The model that read the documents.
        now: The ``read_at`` of every reading of the run; the current UTC
            time when omitted.

    Returns:
        One outcome per paired extraction, in order.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    extractions = result.extractions
    if len(extractions) > len(documents):
        logger.warning(
            "reading-desk: %d extractions for %d documents, the surplus is not saved",
            len(extractions),
            len(documents),
        )
    investments = await _known_investments(engine, tenant_id=tenant_id, read_by=read_by)
    return [
        await _write_one(
            engine,
            tenant_id=tenant_id,
            read_by=read_by,
            extraction=extraction,
            document=document,
            result=result,
            keywords=keywords,
            model=model,
            now=now,
            investments=investments,
        )
        for extraction, document in zip(extractions, documents, strict=False)
    ]


async def find_earlier_readings(
    engine: AsyncEngine,
    *,
    tenant_id: UUID,
    user_id: UUID,
    sha256s: Sequence[str],
) -> dict[str, list[DocumentReadingDTO]]:
    """Find the live readings of documents read before (RD-D-9).

    Args:
        engine: The application engine.
        tenant_id: The tenant to look in.
        user_id: The user asking.
        sha256s: The uploads' hashes; a repeated hash is looked up once.

    Returns:
        The live readings per hash, newest first; a hash without a live
        reading is absent.

    Raises:
        ReadingInvalid: If a hash is not 64 lower-case hex characters.
    """
    found: dict[str, list[DocumentReadingDTO]] = {}
    async with tenant_context(engine, tenant_id, user_id=user_id) as session:
        repository = DocumentReadingRepository(session)
        for sha256 in dict.fromkeys(sha256s):
            readings = await repository.find_live_by_sha256(sha256)
            if readings:
                found[sha256] = readings
    return found
