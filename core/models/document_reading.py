# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Reading Desk ORM models: readings and retention runs.

Backs the ``document_readings`` and ``reading_purges`` tables introduced in
migration b038 (ADR-0139 §2/§5). A :class:`DocumentReading` is the
structured record of one document, kept without the document itself. A
:class:`ReadingPurge` is one retention run.

The tombstone shape (ADR-0139 §5)
---------------------------------
A live reading carries ``findings``, ``note`` and ``search_text``, the
three payload columns, and no removal field. A removed reading stays as a
tombstone: the three payload columns are SQL NULL, ``removed_at`` and
``removed_by`` are set, and either ``removal_reason`` or ``purge_id`` says
why. A CHECK in the database enforces that shape and a second one allows at
most one assignment; both are mirrored in ``__table_args__`` so the ORM
metadata stays a faithful description of the schema.

The audit images of ``document_readings`` leave out the same three payload
columns (ADR-0140), so a removal is not undone by ``audit_log``.

The vocabularies (``origin``, ``document_type``, ``trust`` and those inside
the JSON) are plain TEXT and live in the repository, not in SQL: they
follow the codebase's TEXT-for-status convention, and a new profile must
not need a migration.

No ``relationship()`` traversals are declared: repositories join
explicitly, per the Phase-3/4 convention.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base

#: The tombstone shape, mirrored from the b038 CHECK.
_TOMBSTONE_SHAPE = """
(removed_at IS NULL AND removed_by IS NULL AND removal_reason IS NULL AND purge_id IS NULL
 AND findings IS NOT NULL AND note IS NOT NULL AND search_text IS NOT NULL)
OR
(removed_at IS NOT NULL AND removed_by IS NOT NULL
 AND (removal_reason IS NOT NULL OR purge_id IS NOT NULL)
 AND findings IS NULL AND note IS NULL AND search_text IS NULL)
"""


class ReadingPurge(Base):
    """One retention run over a tenant's readings (ADR-0139 §5)."""

    __tablename__ = "reading_purges"

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
    criterion: Mapped[str] = mapped_column(Text, nullable=False)
    years: Mapped[int] = mapped_column(Integer, nullable=False)
    cutoff: Mapped[date] = mapped_column(Date, nullable=False)
    reading_count: Mapped[int] = mapped_column(Integer, nullable=False)
    run_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentReading(Base):
    """One document read by one tenant, live or a tombstone (ADR-0139 §2)."""

    __tablename__ = "document_readings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "reading_number",
            name="uq_document_readings_tenant_reading_number",
        ),
        CheckConstraint(
            "investment_id IS NULL OR manager_name IS NULL",
            name="ck_document_readings_one_assignment",
        ),
        CheckConstraint(_TOMBSTONE_SHAPE, name="ck_document_readings_tombstone_shape"),
        Index("ix_document_readings_tenant_sha256", "tenant_id", "sha256"),
        Index("ix_document_readings_tenant_investment", "tenant_id", "investment_id"),
        Index("ix_document_readings_tenant_document_date", "tenant_id", "document_date"),
    )

    # --- identity -----------------------------------------------------------
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
    reading_number: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    read_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'upload'"))
    case_attachment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("case_attachments.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # --- classification -----------------------------------------------------
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- subject ------------------------------------------------------------
    fund_name_read: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_name_read: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    investment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investments.id", ondelete="SET NULL"),
        nullable=True,
    )
    manager_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- findings and reading note ------------------------------------------
    # none_as_null: a plain JSONB column persists Python None as the JSON
    # value null, which is not SQL NULL, so the tombstone CHECK would reject
    # every removal. Needed on exactly these two, the JSONB payload columns.
    findings: Mapped[list[dict] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    note: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)

    # --- provenance ---------------------------------------------------------
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    adhoc_keywords: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    trust: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'untrusted'"))

    # --- search -------------------------------------------------------------
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- removal ------------------------------------------------------------
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    removal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    purge_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reading_purges.id", ondelete="RESTRICT"),
        nullable=True,
    )
