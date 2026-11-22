# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""DocumentReadingRepository — persistence for the Reading Desk (ADR-0139, ADR-0140).

Backs the ``document_readings`` and ``reading_purges`` tables of migration
b038: the reading (ADR-0139 §2), its lifecycle (§5) and its assignment
(§6). Shape mirrors the other tenant-scoped repositories: a tenant-scoped
:class:`AsyncSession` is passed in, methods return frozen DTOs, and
``tenant_id`` is implicit in the session context (RLS derives it from
``app.tenant_id``).

* **The number.** ``reading_number`` is tenant-sequential, allocated in SQL
  as ``COALESCE(MAX(reading_number), 0) + 1`` with one retry on the unique
  constraint (the ``cases.case_number`` idiom). A tombstone keeps its
  number, so numbers are never reused.
* **The search.** A case-insensitive substring match over ``search_text``
  with wildcards escaped (the Cases idiom). It runs over live readings
  only, and there is no content search over documents.
* **The tombstone.** A reading is written once and has no edit path except
  its assignment. A removal purges the three payload columns
  (:data:`PAYLOAD_COLUMNS`) and keeps the rest of the row: file name, the
  names as read, the ad-hoc keywords and the removal reason stay here and
  in the audit images, and excerpts already kept into a case stay there.
  The audit trigger on ``document_readings`` excludes the same three
  columns from both row images (ADR-0140).
* **The retention run.** :meth:`DocumentReadingRepository.run_purge`
  records one ``reading_purges`` row and tombstones every live reading
  whose effective date lies before the cutoff, in the caller's transaction.
  The effective date is the document date, or the UTC read date where
  there is none.

A reading holds untrusted GP text, so no error message raised here repeats
a value it was given: the message names the field and the rule.

This module is FastAPI-free and knows nothing of ``services/scraper/``:
``core/`` imports nothing from the project, so the profile, confidence and
field-type vocabularies below are mirrored. P-RD-1b adds the parity test
against the profile files.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Date,
    Select,
    and_,
    any_,
    cast,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.exc import IntegrityError

from core.exceptions import ReadingInvalid, ReadingNotFound, ReadingRemoved
from core.models.case import CaseAttachment
from core.models.document_reading import DocumentReading, ReadingPurge
from core.models.investment import Investment
from core.repositories.base import BaseRepository

#: The payload columns (ADR-0139 §5, ADR-0140 Decision 2): what a tombstone
#: purges and what the audit trigger on document_readings excludes. One list —
#: remove() and run_purge() null exactly these, migration b038 passes the same
#: names to audit_trigger_function_excluding, and a test holds the two equal.
PAYLOAD_COLUMNS: tuple[str, ...] = ("findings", "note", "search_text")
#: Reading origins (ADR-0139 §2, §10).
_VALID_ORIGINS: frozenset[str] = frozenset({"upload", "case_attachment"})
#: Profile ids (ADR-0139 §3). Mirrored from config/scraper_profiles/;
#: P-RD-1b pins the parity with the profile files.
_VALID_DOCUMENT_TYPES: frozenset[str] = frozenset(
    {"quarterly_report", "capital_notice", "pitch_deck", "other"}
)
#: Finding origins (ADR-0139 §2).
_VALID_FINDING_ORIGINS: frozenset[str] = frozenset({"profile", "adhoc"})
#: Confidence levels — the services/scraper Confidence values.
_VALID_CONFIDENCES: frozenset[str] = frozenset({"High", "Medium", "Low", "Not_Found"})
#: Field types — the services/scraper KeywordType values.
_VALID_FIELD_TYPES: frozenset[str] = frozenset({"Number", "Percentage", "Date", "Text", "List"})
#: The reading note's keys (ADR-0139 §2).
_NOTE_KEYS: frozenset[str] = frozenset({"highlights", "quotes", "events_and_risks", "outlook"})
#: The one retention criterion: document date, read date where none (§5).
_PURGE_CRITERION = "document_date"
#: Every reading is untrusted GP text (ADR-0139 §2, §8).
_TRUST = "untrusted"
_READING_NUMBER_CONSTRAINT = "uq_document_readings_tenant_reading_number"

_SHA256 = re.compile(r"[0-9a-f]{64}")
#: What ``str.strip`` removes in ASCII; SQL ``btrim`` alone removes only spaces.
_WHITESPACE = " \t\n\r\f\v"

_READING_COLUMNS = tuple(DocumentReading.__table__.columns)
_PURGE_COLUMNS = tuple(ReadingPurge.__table__.columns)
_NEWEST_FIRST = (DocumentReading.read_at.desc(), DocumentReading.reading_number.desc())


@dataclass(frozen=True)
class DocumentReadingDTO:
    """Plain data-only view of a ``document_readings`` row."""

    id: UUID
    tenant_id: UUID
    reading_number: int
    filename: str
    sha256: str
    size_bytes: int
    page_count: int | None
    read_at: datetime
    read_by: UUID
    origin: str
    case_attachment_id: UUID | None
    document_type: str
    profile_version: int
    language: str | None
    fund_name_read: str | None
    manager_name_read: str | None
    document_date: date | None
    period_label: str | None
    currency: str | None
    investment_id: UUID | None
    manager_name: str | None
    assigned_by: UUID | None
    assigned_at: datetime | None
    findings: list[dict] | None
    note: dict | None
    model: str
    prompt_version: str
    adhoc_keywords: list
    trust: str
    search_text: str | None
    removed_at: datetime | None
    removed_by: UUID | None
    removal_reason: str | None
    purge_id: UUID | None

    @property
    def label(self) -> str:
        """The shown form of the number, ``R-nnnn``; made here and nowhere else."""
        return f"R-{self.reading_number:04d}"

    @property
    def is_removed(self) -> bool:
        """Whether the reading is a tombstone."""
        return self.removed_at is not None


@dataclass(frozen=True)
class ReadingPurgeDTO:
    """Plain data-only view of a ``reading_purges`` row."""

    id: UUID
    tenant_id: UUID
    criterion: str
    years: int
    cutoff: date
    reading_count: int
    run_by: UUID
    run_at: datetime


@dataclass(frozen=True)
class ReadingPurgePreview:
    """What a retention run would tombstone, before it runs."""

    years: int
    cutoff: date
    reading_count: int


def _is_reading_number_conflict(exc: IntegrityError) -> bool:
    """Return True when ``exc`` is the tenant reading-number unique violation.

    Reads the driver's ``constraint_name`` off the wrapped error, falling
    back to a substring match on the driver's own message. The match is not
    made on ``str(exc)``: that text includes the statement's parameters,
    which here are document text.
    """
    constraint = getattr(exc.orig, "constraint_name", None)
    if constraint == _READING_NUMBER_CONSTRAINT:
        return True
    return _READING_NUMBER_CONSTRAINT in str(exc.orig)


def _escape_like(term: str) -> str:
    """Escape LIKE/ILIKE wildcards so user input matches literally.

    Backslash first (it is the escape character), then ``%`` and ``_``.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _one_of(value: object, vocabulary: frozenset[str]) -> bool:
    """Whether ``value`` is a string of ``vocabulary``; parsed JSON may be unhashable."""
    return isinstance(value, str) and value in vocabulary


def _validate_sha256(sha256: str) -> None:
    # fullmatch: ``$`` would accept a trailing newline.
    if not _SHA256.fullmatch(sha256):
        raise ReadingInvalid(
            "sha256 must be 64 lower-case hexadecimal characters.",
            field="sha256",
        )


def _validate_document_type(document_type: str) -> None:
    if not _one_of(document_type, _VALID_DOCUMENT_TYPES):
        raise ReadingInvalid(
            f"document_type must be one of {sorted(_VALID_DOCUMENT_TYPES)}.",
            field="document_type",
        )


def _validate_origin(origin: str, case_attachment_id: UUID | None) -> None:
    if not _one_of(origin, _VALID_ORIGINS):
        raise ReadingInvalid(f"origin must be one of {sorted(_VALID_ORIGINS)}.", field="origin")
    if origin == "case_attachment" and case_attachment_id is None:
        raise ReadingInvalid(
            "A reading of a case attachment requires case_attachment_id.",
            field="case_attachment_id",
        )
    if origin == "upload" and case_attachment_id is not None:
        raise ReadingInvalid(
            "A reading of an upload carries no case_attachment_id.",
            field="case_attachment_id",
        )


def _validate_findings(findings: list[dict]) -> None:
    if not isinstance(findings, list):
        raise ReadingInvalid("findings must be a list.", field="findings")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ReadingInvalid(f"findings[{index}] must be a dict.", field="findings")
        key = finding.get("key")
        if not isinstance(key, str) or not key:
            raise ReadingInvalid(
                f"findings[{index}] requires a non-empty string key.",
                field="findings",
            )
        for name, vocabulary in (
            ("origin", _VALID_FINDING_ORIGINS),
            ("confidence", _VALID_CONFIDENCES),
            ("type", _VALID_FIELD_TYPES),
        ):
            if not _one_of(finding.get(name), vocabulary):
                raise ReadingInvalid(
                    f"findings[{index}].{name} must be one of {sorted(vocabulary)}.",
                    field="findings",
                )


def _validate_note(note: dict) -> None:
    if not isinstance(note, dict) or not note.keys() <= _NOTE_KEYS:
        raise ReadingInvalid(
            f"note must be a dict whose keys are among {sorted(_NOTE_KEYS)}.",
            field="note",
        )


def _cutoff(*, years: int, today: date) -> date:
    """Return ``today`` minus ``years`` calendar years, 29 February falling to the 28th."""
    if years < 1:
        raise ReadingInvalid("years must be at least 1.", field="years")
    year = today.year - years
    if year < date.min.year:
        raise ReadingInvalid("years reaches back before the calendar begins.", field="years")
    day = today.day
    if today.month == 2 and day == 29 and not calendar.isleap(year):
        day = 28
    return date(year, today.month, day)


def _effective_date() -> ColumnElement[date]:
    """The document date, or the UTC read date where there is none."""
    # timezone('UTC', x) is the function form of x AT TIME ZONE 'UTC'.
    read_date = cast(func.timezone("UTC", DocumentReading.read_at), Date)
    return func.coalesce(DocumentReading.document_date, read_date)


def _is_live() -> ColumnElement[bool]:
    return DocumentReading.removed_at.is_(None)


def _purgeable(cutoff: date) -> ColumnElement[bool]:
    """Live and strictly older than the cutoff: what preview and run both select."""
    return and_(_is_live(), _effective_date() < cutoff)


def _tombstone_values(
    *,
    removed_at: datetime,
    removed_by: UUID,
    removal_reason: str | None,
    purge_id: UUID | None,
) -> dict[str, Any]:
    """The values of a tombstone: the removal fields plus every payload column NULL."""
    values: dict[str, Any] = dict.fromkeys(PAYLOAD_COLUMNS)
    values.update(
        removed_at=removed_at,
        removed_by=removed_by,
        removal_reason=removal_reason,
        purge_id=purge_id,
    )
    return values


class DocumentReadingRepository(BaseRepository):
    """Write, find, assign and tombstone readings in the active tenant context."""

    async def _active_tenant(self) -> UUID:
        """Return the tenant bound to the active session (``app.tenant_id``)."""
        row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        return row.scalar_one()

    async def _fetch(self, stmt: Select[Any]) -> list[DocumentReadingDTO]:
        result = await self._session.execute(stmt)
        return [DocumentReadingDTO(**row._mapping) for row in result.all()]

    async def _lock_live(self, reading_id: UUID, *, action: str) -> None:
        """Lock a reading for a write, refusing an unknown id and a tombstone.

        The row lock is what keeps a concurrent removal from being followed
        by a write that still saw the reading live.
        """
        row = (
            await self._session.execute(
                select(DocumentReading.removed_at)
                .where(DocumentReading.id == reading_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ReadingNotFound(f"Cannot {action}: no reading {reading_id} in this tenant.")
        if row.removed_at is not None:
            raise ReadingRemoved(f"Cannot {action}: reading {reading_id} is removed.")

    async def _update_one(self, reading_id: UUID, values: dict[str, Any]) -> DocumentReadingDTO:
        row = (
            await self._session.execute(
                update(DocumentReading)
                .where(DocumentReading.id == reading_id)
                .values(**values)
                .returning(*_READING_COLUMNS)
            )
        ).one()
        return DocumentReadingDTO(**row._mapping)

    async def _attempt_create(
        self,
        *,
        active_tenant: UUID,
        values: dict[str, Any],
    ) -> DocumentReadingDTO:
        """One creation attempt.

        Wrapped in a SAVEPOINT so a number collision rolls back just this
        attempt, leaving the caller's transaction intact for the retry. The
        scalar subquery runs under RLS, so the MAX is tenant-scoped.
        """
        async with self._session.begin_nested():
            next_number = select(
                func.coalesce(func.max(DocumentReading.reading_number), 0) + 1
            ).scalar_subquery()
            stmt = (
                insert(DocumentReading)
                .values(tenant_id=active_tenant, reading_number=next_number, **values)
                .returning(*_READING_COLUMNS)
            )
            row = (await self._session.execute(stmt)).one()
        return DocumentReadingDTO(**row._mapping)

    async def create(
        self,
        *,
        filename: str,
        sha256: str,
        size_bytes: int,
        page_count: int | None,
        read_by: UUID,
        origin: str = "upload",
        case_attachment_id: UUID | None = None,
        document_type: str,
        profile_version: int,
        language: str | None,
        fund_name_read: str | None,
        manager_name_read: str | None,
        document_date: date | None,
        period_label: str | None,
        currency: str | None,
        findings: list[dict],
        note: dict,
        model: str,
        prompt_version: str,
        adhoc_keywords: list,
        search_text: str,
        now: datetime,
    ) -> DocumentReadingDTO:
        """Write one reading (ADR-0139 §2).

        The reading starts live and unassigned, with ``trust`` fixed to
        ``untrusted``. Its number is allocated tenant-sequentially; on a
        collision with a concurrent create the allocation is retried once
        and a second collision propagates.

        Args:
            filename: The document's file name, stored as given.
            sha256: The document's hash, 64 lower-case hex characters.
            size_bytes: The document's size.
            page_count: The page count the model reported, or ``None``.
            read_by: The user who started the reading.
            origin: ``upload`` or ``case_attachment``.
            case_attachment_id: The attachment read; required for and only
                allowed with ``case_attachment``.
            document_type: The profile id.
            profile_version: The profile's version, at least 1.
            language: The document's language as read.
            fund_name_read: The fund name as read.
            manager_name_read: The manager name as read.
            document_date: The document or reporting date, or ``None``.
            period_label: The reporting period as read.
            currency: The currency as read.
            findings: One dict per field, each with ``key``, ``origin``,
                ``confidence`` and ``type``; other keys are free.
            note: The reading note; keys among ``highlights``, ``quotes``,
                ``events_and_risks`` and ``outlook``.
            model: The model that read the document.
            prompt_version: The version of the prompt it was read with.
            adhoc_keywords: The ad-hoc keywords of the run.
            search_text: The plain text the search matches.
            now: The timestamp for ``read_at``.

        Returns:
            The new :class:`DocumentReadingDTO`.

        Raises:
            ReadingInvalid: If a value violates a vocabulary or shape rule,
                or the attachment is not visible in the active tenant.
        """
        if not filename.strip():
            raise ReadingInvalid("filename must not be empty.", field="filename")
        _validate_sha256(sha256)
        if size_bytes < 0:
            raise ReadingInvalid("size_bytes must not be negative.", field="size_bytes")
        if page_count is not None and page_count < 1:
            raise ReadingInvalid("page_count must be None or at least 1.", field="page_count")
        _validate_origin(origin, case_attachment_id)
        _validate_document_type(document_type)
        if profile_version < 1:
            raise ReadingInvalid("profile_version must be at least 1.", field="profile_version")
        if not model.strip():
            raise ReadingInvalid("model must not be empty.", field="model")
        if not prompt_version.strip():
            raise ReadingInvalid("prompt_version must not be empty.", field="prompt_version")
        _validate_findings(findings)
        _validate_note(note)
        if not isinstance(adhoc_keywords, list):
            raise ReadingInvalid("adhoc_keywords must be a list.", field="adhoc_keywords")
        if not isinstance(search_text, str):
            raise ReadingInvalid("search_text must be a string.", field="search_text")

        if case_attachment_id is not None:
            # The FK check bypasses RLS, so it alone does not prove the tenant.
            visible = await self._session.execute(
                select(CaseAttachment.id).where(CaseAttachment.id == case_attachment_id)
            )
            if visible.scalar_one_or_none() is None:
                raise ReadingInvalid(
                    "case_attachment_id names no attachment of this tenant.",
                    field="case_attachment_id",
                )

        values: dict[str, Any] = {
            "filename": filename,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "page_count": page_count,
            "read_at": now,
            "read_by": read_by,
            "origin": origin,
            "case_attachment_id": case_attachment_id,
            "document_type": document_type,
            "profile_version": profile_version,
            "language": language,
            "fund_name_read": fund_name_read,
            "manager_name_read": manager_name_read,
            "document_date": document_date,
            "period_label": period_label,
            "currency": currency,
            "findings": findings,
            "note": note,
            "model": model,
            "prompt_version": prompt_version,
            "adhoc_keywords": adhoc_keywords,
            "trust": _TRUST,
            "search_text": search_text,
        }
        active_tenant = await self._active_tenant()
        for attempt in range(2):
            try:
                return await self._attempt_create(active_tenant=active_tenant, values=values)
            except IntegrityError as exc:
                # Retry once on a number collision; propagate anything else,
                # and propagate a second collision too.
                if attempt == 0 and _is_reading_number_conflict(exc):
                    continue
                raise
        raise AssertionError("unreachable: the loop returns or raises")

    async def get(self, reading_id: UUID) -> DocumentReadingDTO | None:
        """Return one reading by id, tombstones included.

        Args:
            reading_id: The reading's id.

        Returns:
            The reading, or ``None`` if the active tenant has none by this id.
        """
        found = await self._fetch(select(*_READING_COLUMNS).where(DocumentReading.id == reading_id))
        return found[0] if found else None

    async def get_by_number(self, reading_number: int) -> DocumentReadingDTO | None:
        """Return one reading by its number, tombstones included.

        Args:
            reading_number: The tenant-sequential number behind ``R-nnnn``.

        Returns:
            The reading, or ``None`` if the active tenant has none by this number.
        """
        found = await self._fetch(
            select(*_READING_COLUMNS).where(DocumentReading.reading_number == reading_number)
        )
        return found[0] if found else None

    async def list_readings(
        self,
        *,
        document_type: str | None = None,
        investment_id: UUID | None = None,
        manager_name: str | None = None,
        assigned: bool | None = None,
        document_date_from: date | None = None,
        document_date_to: date | None = None,
        include_removed: bool = False,
        limit: int | None = None,
    ) -> list[DocumentReadingDTO]:
        """List the tenant's readings, newest read first (the library's filters).

        Args:
            document_type: A profile id, or ``None`` for all.
            investment_id: Only readings assigned to this investment.
            manager_name: Matches the assigned manager name or the name as
                read, case-insensitively and ignoring surrounding whitespace.
                A blank name matches nothing.
            assigned: ``True`` for assigned, ``False`` for unassigned,
                ``None`` for both.
            document_date_from: Inclusive lower bound on the document date.
            document_date_to: Inclusive upper bound on the document date. A
                reading without a document date drops out under either bound.
            include_removed: Include tombstones.
            limit: The most rows to return, or ``None`` for all.

        Returns:
            The readings, ``read_at`` descending, then number descending.

        Raises:
            ReadingInvalid: If ``document_type`` is outside the vocabulary.
        """
        stmt = select(*_READING_COLUMNS)
        if document_type is not None:
            _validate_document_type(document_type)
            stmt = stmt.where(DocumentReading.document_type == document_type)
        if investment_id is not None:
            stmt = stmt.where(DocumentReading.investment_id == investment_id)
        if manager_name is not None:
            wanted = manager_name.strip()
            if not wanted:
                return []
            # Both sides are folded by Postgres: Python's lower() differs from it.
            folded = func.lower(literal(wanted))
            stmt = stmt.where(
                or_(
                    func.lower(DocumentReading.manager_name) == folded,
                    func.lower(func.btrim(DocumentReading.manager_name_read, _WHITESPACE))
                    == folded,
                )
            )
        if assigned is True:
            stmt = stmt.where(
                or_(
                    DocumentReading.investment_id.is_not(None),
                    DocumentReading.manager_name.is_not(None),
                )
            )
        elif assigned is False:
            stmt = stmt.where(
                DocumentReading.investment_id.is_(None),
                DocumentReading.manager_name.is_(None),
            )
        if document_date_from is not None:
            stmt = stmt.where(DocumentReading.document_date >= document_date_from)
        if document_date_to is not None:
            stmt = stmt.where(DocumentReading.document_date <= document_date_to)
        if not include_removed:
            stmt = stmt.where(_is_live())
        return await self._fetch(stmt.order_by(*_NEWEST_FIRST).limit(limit))

    async def search(self, query: str, *, limit: int | None = None) -> list[DocumentReadingDTO]:
        """Search live readings by a substring of ``search_text`` (ADR-0139 §2).

        Args:
            query: The search text; ``%`` and ``_`` match literally.
            limit: The most rows to return, or ``None`` for all.

        Returns:
            The matches, newest read first. An empty or whitespace-only
            query returns an empty list.
        """
        stripped = query.strip()
        if not stripped:
            return []
        pattern = f"%{_escape_like(stripped)}%"
        stmt = (
            select(*_READING_COLUMNS)
            .where(_is_live(), DocumentReading.search_text.ilike(pattern, escape="\\"))
            .order_by(*_NEWEST_FIRST)
            .limit(limit)
        )
        return await self._fetch(stmt)

    async def find_live_by_sha256(self, sha256: str) -> list[DocumentReadingDTO]:
        """Return the live readings of one document, the dedup lookup (ADR-0139 §5).

        Args:
            sha256: The document's hash, 64 lower-case hex characters.

        Returns:
            The live readings with that hash, newest read first.

        Raises:
            ReadingInvalid: If ``sha256`` is not a well-formed hash.
        """
        _validate_sha256(sha256)
        stmt = (
            select(*_READING_COLUMNS)
            .where(_is_live(), DocumentReading.sha256 == sha256)
            .order_by(*_NEWEST_FIRST)
        )
        return await self._fetch(stmt)

    async def assign(
        self,
        reading_id: UUID,
        *,
        investment_id: UUID | None = None,
        manager_name: str | None = None,
        assigned_by: UUID,
        now: datetime,
    ) -> DocumentReadingDTO:
        """Assign a live reading, or clear its assignment (ADR-0139 §6).

        Args:
            reading_id: The reading's id.
            investment_id: The investment, if the fund is held.
            manager_name: Otherwise a manager name, stored stripped. With
                neither given the assignment is cleared.
            assigned_by: Who set or cleared the assignment.
            now: When.

        Returns:
            The updated reading.

        Raises:
            ReadingNotFound: If the reading is not visible in the active tenant.
            ReadingRemoved: If the reading is a tombstone.
            ReadingInvalid: If both targets are given, the manager name is
                empty, or the investment is not visible in the active tenant.
        """
        await self._lock_live(reading_id, action="assign")
        if investment_id is not None and manager_name is not None:
            raise ReadingInvalid(
                "A reading is assigned to an investment or a manager name, not both.",
                field="assignment",
            )
        if manager_name is not None:
            manager_name = manager_name.strip()
            if not manager_name:
                raise ReadingInvalid("manager_name must not be empty.", field="manager_name")
        if investment_id is not None:
            # The FK check bypasses RLS, so it alone does not prove the tenant.
            visible = await self._session.execute(
                select(Investment.id).where(Investment.id == investment_id)
            )
            if visible.scalar_one_or_none() is None:
                raise ReadingInvalid(
                    "investment_id names no investment of this tenant.",
                    field="investment_id",
                )
        return await self._update_one(
            reading_id,
            {
                "investment_id": investment_id,
                "manager_name": manager_name,
                "assigned_by": assigned_by,
                "assigned_at": now,
            },
        )

    async def remove(
        self,
        reading_id: UUID,
        *,
        reason: str,
        removed_by: UUID,
        now: datetime,
    ) -> DocumentReadingDTO:
        """Turn a live reading into a tombstone (RD-D-8).

        Purges the three payload columns. Identity, classification, subject,
        provenance and assignment stay, here and in the audit images.

        Args:
            reading_id: The reading's id.
            reason: Why; required, stored stripped.
            removed_by: Who removed it.
            now: When.

        Returns:
            The tombstone.

        Raises:
            ReadingNotFound: If the reading is not visible in the active tenant.
            ReadingRemoved: If the reading already is a tombstone.
            ReadingInvalid: If the reason is empty.
        """
        await self._lock_live(reading_id, action="remove")
        stripped = reason.strip()
        if not stripped:
            raise ReadingInvalid("A removal requires a reason.", field="reason")
        return await self._update_one(
            reading_id,
            _tombstone_values(
                removed_at=now,
                removed_by=removed_by,
                removal_reason=stripped,
                purge_id=None,
            ),
        )

    async def preview_purge(self, *, years: int, today: date) -> ReadingPurgePreview:
        """Count what a retention run would tombstone; writes nothing.

        Args:
            years: The age in calendar years, at least 1.
            today: The day the cutoff is counted back from.

        Returns:
            The cutoff and the number of live readings strictly before it.

        Raises:
            ReadingInvalid: If ``years`` is out of range.
        """
        cutoff = _cutoff(years=years, today=today)
        result = await self._session.execute(
            select(func.count()).select_from(DocumentReading).where(_purgeable(cutoff))
        )
        return ReadingPurgePreview(years=years, cutoff=cutoff, reading_count=result.scalar_one())

    async def run_purge(
        self,
        *,
        years: int,
        today: date,
        run_by: UUID,
        now: datetime,
    ) -> ReadingPurgeDTO:
        """Run retention in the caller's transaction (ADR-0139 §5).

        Records one ``reading_purges`` row, also when nothing matches, and
        purges the three payload columns of every live reading whose
        effective date is strictly before the cutoff.

        Args:
            years: The age in calendar years, at least 1.
            today: The day the cutoff is counted back from.
            run_by: Who ran it.
            now: When.

        Returns:
            The recorded run.

        Raises:
            ReadingInvalid: If ``years`` is out of range.
        """
        cutoff = _cutoff(years=years, today=today)
        locked = await self._session.execute(
            select(DocumentReading.id).where(_purgeable(cutoff)).with_for_update()
        )
        ids = list(locked.scalars().all())
        purge_stmt = (
            insert(ReadingPurge)
            .values(
                tenant_id=await self._active_tenant(),
                criterion=_PURGE_CRITERION,
                years=years,
                cutoff=cutoff,
                reading_count=len(ids),
                run_by=run_by,
                run_at=now,
            )
            .returning(*_PURGE_COLUMNS)
        )
        purge = ReadingPurgeDTO(**(await self._session.execute(purge_stmt)).one()._mapping)
        if ids:
            # One array parameter: IN (...) would hit asyncpg's 32767-parameter cap.
            locked_ids = literal(ids, ARRAY(PG_UUID(as_uuid=True)))
            await self._session.execute(
                update(DocumentReading)
                .where(DocumentReading.id == any_(locked_ids))
                .values(
                    **_tombstone_values(
                        removed_at=now,
                        removed_by=run_by,
                        removal_reason=None,
                        purge_id=purge.id,
                    )
                )
            )
        return purge

    async def list_for_investment(self, investment_id: UUID) -> list[DocumentReadingDTO]:
        """Return an investment's live readings in document chronology.

        Args:
            investment_id: The investment the readings are assigned to.

        Returns:
            The readings, effective date descending, then number descending.
        """
        stmt = (
            select(*_READING_COLUMNS)
            .where(_is_live(), DocumentReading.investment_id == investment_id)
            .order_by(_effective_date().desc(), DocumentReading.reading_number.desc())
        )
        return await self._fetch(stmt)

    async def list_purges(self) -> list[ReadingPurgeDTO]:
        """Return the tenant's retention runs, newest first.

        Returns:
            The runs, ``run_at`` descending.
        """
        result = await self._session.execute(
            select(*_PURGE_COLUMNS).order_by(ReadingPurge.run_at.desc(), ReadingPurge.id)
        )
        return [ReadingPurgeDTO(**row._mapping) for row in result.all()]
