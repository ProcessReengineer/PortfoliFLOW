# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Case-workflow ORM models — the Cases area persistence (ADR-0107).

Backs the three tables introduced in migration b031 (per ADR-0107):
``cases``, ``case_entries`` and ``case_attachments``. A case is a
tenant-scoped unit of decision work: it may be opened manually or from
an Irene finding, accumulates an append-only timeline of entries, and is
eventually closed with a mandatory note. Closed cases become a second
projection source for the Watch Desk Journal in a later sub-strand;
nothing journal-related is persisted here (Gate-C0 decision).

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

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class Case(Base):
    """One tenant-scoped case — a unit of decision work (ADR-0107 §2).

    A case is opened either manually (with a ``description``) or from an
    Irene finding (``finding_id`` set, ``description`` may be null). Its
    ``state`` vocabulary is ``open`` / ``closed`` — TEXT, enforced in
    application code (C1b), never a SQL enum, matching the codebase's
    status convention.

    The close transition — setting ``state`` to ``closed`` together with
    ``closed_by``, ``closed_at`` and the mandatory ``closing_note`` — is
    the **only permitted mutation** of a case row (ADR-0107 §2). Every
    other change to the case's world is a new :class:`CaseEntry`. The
    ``closing_note`` is mandatory at close but enforced in application
    code (C1b), not by the schema.

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
    # The manual-creation description; open-from-finding cases may leave
    # this null.
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


class CaseEntry(Base):
    """One append-only timeline entry belonging to a :class:`Case`.

    Entries are **append-only — never updated or deleted; a new situation
    is a new entry** (ADR-0107 §2). The ``kind`` vocabulary is ``opened``
    · ``note`` · ``pin`` · ``decision_record`` · ``closed`` (TEXT,
    application-enforced, never a SQL enum). The ``actor`` vocabulary is
    ``pm`` / ``shirley`` / ``system``, with ``actor_user_id`` set where a
    user acted.

    ``payload`` is JSONB and **opaque to persistence** (the finding-payload
    idiom): the per-kind contract lives above this layer. A ``pin`` entry's
    payload carries an artifact of class ``document`` / ``consultation`` /
    ``scenario_snapshot`` plus the mandatory curation comment — all inside
    ``payload``, invisible here. Materiality-at-opening is frozen into the
    ``opened`` entry's payload by the route layer in a later sub-strand;
    this layer neither reads nor validates it.

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
    ``size_bytes``, ``sha256``) alongside. They are addressed **only**
    through their pin entry (ADR-0107 §7, the DMS boundary): there are no
    folders, no versioning, and no content search. Size and count caps and
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
