# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Case ORM models — the Cases area persistence (ADR-0107, ADR-0138).

Backs eleven tables. Migration b031 (ADR-0107) introduced ``cases``,
``case_entries`` and ``case_attachments``; migration b037 (ADR-0138)
extended ``cases`` and added the eight working-file tables
``case_threads``, ``case_kept``, ``case_figures``, ``case_conversations``,
``case_points``, ``case_decisions``, ``case_investments`` and
``case_proposals``. A case has two layers (ADR-0138 §2): the **record** —
``case_entries``, append-only, written only at milestones and as system
lines — and the **working file** — the working-file columns of ``cases``
and the eight tables, mutable while the case is open or waiting and
read-only once it is closed. Every milestone freezes the working file into
the record; the working-file rows of a closed case stay in their tables so
that search reads closed cases without unpacking snapshots.

The entities ADR-0107 §2 names ``case`` / ``case_entry`` map to the
**plural** table names ``cases`` / ``case_entries`` / ``case_attachments``:
``case`` is a reserved SQL keyword, and the plural forms follow the
majority convention already in the schema (``users``, ``tenants``,
``data_uploads``, ``fx_rates``). This is the Gate-C0 naming decision; the
ADR text is left unedited.

The finding-payload idiom (ADR-0088 / :mod:`core.models.irene_finding`) is
reused: ``case_entries.payload`` is JSONB and **opaque to persistence** —
the timeline-entry contract lives above this layer, and no state
vocabulary is enforced as a SQL enum (TEXT, application-enforced, matching
the codebase status convention).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from core.models.base import Base


class Case(Base):
    """One tenant-scoped case — a question or task kept open (ADR-0138 §1).

    A case has two layers (ADR-0138 §2): the append-only **record**
    (:class:`CaseEntry`) and the mutable **working file** — the
    working-file columns of this row plus the eight working-file tables.
    Its ``state`` vocabulary is ``open`` · ``waiting`` · ``closed`` (§4) —
    TEXT, enforced in application code, never a SQL enum, matching the
    codebase's status convention. A waiting case carries ``wait_until`` and
    is as mutable as an open one.

    The **permitted mutations** of a ``cases`` row are: the working-file
    columns (``title``, ``description``, ``counterparty``, ``due_on``,
    ``framing``) while the case is open or waiting; the state transitions
    of §4 (``wait_until`` with them); the owner at hand-over (§13); and the
    close trio — ``closed_by``, ``closed_at`` and the mandatory
    ``closing_note`` — once. ``updated_at`` is the working-file stamp,
    bumped by every one of them. ``description`` holds the question's (or
    task's) prose; it is reinterpreted, not renamed. ``owner_user_id`` is
    initially the opener; ``opened_by`` stays as history. ``framing``
    (``question`` · ``task``) is a presentation flag that changes no
    behaviour — there are no case kinds (§3).

    ``case_number`` is a tenant-sequential display number; the
    ``(tenant_id, case_number)`` unique constraint is the race-safety
    guarantee for the number allocation implemented in C1b.

    The case *references* its finding through ``finding_id`` and never
    mutates it (ADR-0085): the finding is an immutable audit record, and
    the case is a separate lifecycle layered on top of it.
    """

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_number", name="uq_cases_tenant_case_number"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Tenant-sequential display number; uniqueness is the C1b allocation's
    # race-safety guarantee (uq_cases_tenant_case_number).
    case_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # The question's (or task's) prose (ADR-0138 Object model); cases
    # opened from a finding start with it empty.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="open",
        server_default=text("'open'"),
    )
    # References the finding, never mutates it (ADR-0085). Nullable:
    # manually opened cases have no finding.
    finding_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("irene_finding.id", ondelete="RESTRICT"),
        nullable=True,
    )
    opened_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # The close-transition trio: all null until the case is closed.
    closed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Mandatory at close, enforced in application code (C1b), not by the
    # schema.
    closing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The date a waiting case returns to open; null unless waiting (§4).
    wait_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The owner (§13); initially the opener, changed only at hand-over.
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Free text, suggested from investment.manager_name (§5).
    counterparty: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The case's own due date.
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # question / task — presentation only (§3); vocabulary in the repository.
    framing: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The working-file stamp, bumped by every write to the working file.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CaseEntry(Base):
    """One append-only record entry belonging to a :class:`Case`.

    ``case_entries`` is **the record** of ADR-0138 §2: entries are
    **append-only — never updated or deleted; a new situation is a new
    entry** (ADR-0107 §2, confirmed for the record). The ``kind`` vocabulary
    is ``opened`` · ``handed_over`` · ``waiting_set`` · ``waiting_lifted`` ·
    ``decision_record`` · ``closed`` · ``frozen`` · ``system_line`` (TEXT,
    application-enforced, never a SQL enum); the b031 kinds ``note`` and
    ``pin`` are retired for new writes and stay readable as legacy rows. A
    ``frozen`` entry carries ``payload.milestone_entry_id`` and the
    whole-file snapshot taken at that milestone; the pair is written in one
    transaction. The ``actor`` vocabulary is ``pm`` / ``shirley`` /
    ``system``, with ``actor_user_id`` set where a user acted; ``shirley``
    stays in the vocabulary for legacy rows — Shirley writes the working
    file, never the record.

    ``payload`` is JSONB and **opaque to persistence** (the finding-payload
    idiom): the per-kind contract lives above this layer.

    ``tenant_id`` is denormalised (its value is implicit in the parent
    ``cases`` row) per ADR-0035 §3 — RLS evaluates row-locally without a
    JOIN against the parent, the same idiom as ``data_upload_sheets``.
    """

    __tablename__ = "case_entries"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # Denormalised from the parent cases row for row-local RLS (ADR-0035 §3).
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    case_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    # Set where a user acted; null for system-authored entries.
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # The per-kind timeline contract; opaque to persistence.
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CaseAttachment(Base):
    """One in-database file attachment belonging to a :class:`Case`.

    Attachments are stored in-database: ``content`` is the raw file bytes
    (BYTEA) with the usual metadata columns (``filename``, ``mime_type``,
    ``size_bytes``, ``sha256``) alongside. They are addressed through
    :class:`CaseKept` rows of class ``document`` / ``document_excerpt``
    (ADR-0138 §5/§9) instead of pin entries; an attachment with no kept
    row is an upload in progress. The DMS boundary holds (ADR-0107 §7,
    ADR-0138 §11): there are no folders, no versioning, and no content
    search. Size and count caps and
    the MIME-type whitelist are configuration enforced at the route layer,
    not schema constraints.

    Unlike ``data_uploads`` there is deliberately **no**
    ``(tenant_id, sha256)`` unique constraint: the same document
    legitimately pinned in two cases is stored twice by design. This is an
    intentional divergence from the ``data_uploads`` dedup idiom — a case's
    attachments are its own curated evidence, not a shared content store.

    ``tenant_id`` is denormalised from the parent ``cases`` row per
    ADR-0035 §3 so RLS evaluates row-locally without a JOIN.
    """

    __tablename__ = "case_attachments"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # Denormalised from the parent cases row for row-local RLS (ADR-0035 §3).
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    case_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # The file bytes (BYTEA). No dedup: the same document pinned in two
    # cases is stored twice by design (see the class docstring).
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


def _id_column() -> MappedColumn[Any]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _tenant_column() -> MappedColumn[Any]:
    # Denormalised from the parent cases row for row-local RLS (ADR-0035 §3).
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _case_column() -> MappedColumn[Any]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cases.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _created_at_column() -> MappedColumn[Any]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


def _user_column(*, nullable: bool) -> MappedColumn[Any]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=nullable,
    )


class CaseThread(Base):
    """One thread of a case's working file — a sub-question or hypothesis.

    ADR-0138 §3: a thread carries prose, kept artefacts, a ``status`` of
    ``open`` · ``confirmed`` · ``parked`` · ``refuted`` (TEXT,
    application-enforced) and a one-line ``conclusion``. Mutable while the
    case is open or waiting. A thread is never deleted: ``removed_at``
    removes it from the file softly, because :class:`CaseKept` rows keep
    referencing it.
    """

    __tablename__ = "case_threads"

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="open", server_default=text("'open'")
    )
    prose: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at_column()
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseKept(Base):
    """One kept artefact in a case's working file (ADR-0138 §9).

    Placed in a thread (``thread_id``) or at file level (null). ``artifact``
    is one of the six kept classes — ``document`` · ``consultation`` ·
    ``scenario_snapshot`` · ``chart_snapshot`` · ``document_excerpt`` ·
    ``generated_document``. The ``payload`` (JSONB, the class contract) is
    **immutable once kept** for every class except ``generated_document``;
    ``payload_sha256`` is its canonical hash, recomputed when a
    ``generated_document`` is edited. Placement (thread, caption, comment,
    removal) is mutable. A kept row is never deleted — ``removed_at``
    removes it from the file, because frozen snapshots reference it by id.
    ``attachment_id`` is set for ``document`` / ``document_excerpt`` only.
    """

    __tablename__ = "case_kept"

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    thread_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("case_threads.id", ondelete="RESTRICT"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("case_attachments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    kept_by: Mapped[UUID] = _user_column(nullable=False)
    via: Mapped[str] = mapped_column(Text, nullable=False)
    kept_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at_column()


class CaseFigure(Base):
    """One named figure in a case's working file (ADR-0138 §3).

    A value and its provenance, never an expression (no formulas).
    ``source`` is ``book`` · ``typed`` · ``document`` · ``kept`` ·
    ``conversation`` · ``external``; a ``book`` row carries no
    ``value_text`` — it is read live from ``source_ref`` (investment and
    metric) and written by value only into snapshots. Mutable while the
    case is open or waiting and **hard-deletable**, because snapshots
    embed figures by value.
    """

    __tablename__ = "case_figures"

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    as_of: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at_column()


class CaseConversation(Base):
    """One call or meeting in a case's working file (ADR-0138 §3).

    ``channel`` is ``call`` · ``meeting`` · ``other``. Mutable while the
    case is open or waiting and hard-deletable (snapshots embed
    conversations by value).
    """

    __tablename__ = "case_conversations"

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    held_on: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str] = mapped_column(
        Text, nullable=False, default="call", server_default=text("'call'")
    )
    with_whom: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    what_was_said: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    what_was_promised: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _created_at_column()


class CasePoint(Base):
    """One open point in a case's working file (ADR-0138 §3, §5).

    A checklist item with its own owner and due date, independent of the
    case owner. ``ticket_id`` is the ticket created from this point (the
    ticket's own ``case_id`` is the back-link, ADR-0128 §7). Mutable while
    the case is open or waiting; hard-deletable **while no ticket is
    linked**.
    """

    __tablename__ = "case_points"

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[UUID | None] = _user_column(nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ticket_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trade_tickets.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at_column()


class CaseDecision(Base):
    """One decision of a case — options, the chosen one, rationale (ADR-0138 §14).

    ``options`` is a JSONB list of ``{key, text}``; ``chosen_key`` names one
    of them. A row with ``recorded_at`` null is **the draft** — at most one
    per case, guaranteed by the partial unique index
    ``uq_case_decisions_one_draft``. Recording sets ``recorded_by``,
    ``recorded_at`` and ``record_entry_id`` (the ``decision_record`` entry);
    a recorded row is **immutable** — a change of mind is a new decision,
    and a case may record more than one over its life. ``decided_by`` is
    free text (the committee), ``decided_at`` a date the PM may set to the
    committee's date.
    """

    __tablename__ = "case_decisions"
    __table_args__ = (
        Index(
            "uq_case_decisions_one_draft",
            "case_id",
            unique=True,
            postgresql_where=text("recorded_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    options: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    chosen_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    recorded_by: Mapped[UUID | None] = _user_column(nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    record_entry_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("case_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at_column()


class CaseInvestment(Base):
    """One link between a case and an investment (ADR-0138 §5).

    Unique per pair (``uq_case_investments_case_investment``). Linked and
    unlinked while the case is open or waiting; Where we stand and the
    system-line writers read through it.
    """

    __tablename__ = "case_investments"
    __table_args__ = (
        UniqueConstraint("case_id", "investment_id", name="uq_case_investments_case_investment"),
    )

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    investment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    linked_by: Mapped[UUID] = _user_column(nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at_column()


class CaseProposal(Base):
    """One proposal package persisted on a case (ADR-0138 §8).

    The landing place of a Shirley job: requested, then landed with
    ``items`` (a JSONB list, each item with a per-item ``status`` of
    ``pending`` · ``accepted`` · ``discarded`` and the created row's id on
    acceptance), then resolved once no item is pending. Nothing reaches
    the working file until an item is accepted. The item shape beyond
    ``kind`` / ``status`` is opaque to persistence.
    """

    __tablename__ = "case_proposals"

    id: Mapped[UUID] = _id_column()
    tenant_id: Mapped[UUID] = _tenant_column()
    case_id: Mapped[UUID] = _case_column()
    requested_by: Mapped[UUID] = _user_column(nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    landed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at_column()
