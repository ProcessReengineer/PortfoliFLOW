# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ORM models for the web Excel-import path.

These models back the Phase-2 (sub-stream 2d) ``data_uploads`` and
``data_upload_sheets`` tables introduced in migration b004.

The pair is the *minimum viable* representation that lets the FastAPI
Excel-import endpoint persist a workbook in a way Shirley (Phase 4) can
later query: every upload becomes one parent row plus one child row
per sheet, where the sheet's DataFrame is stashed as a JSONB blob in
the ``DataFrame.to_dict('split')`` shape. Phase 4 introduces the
normalised investment-domain schema; this is intentionally not that.

Both tables are tenant-scoped. ``data_upload_sheets.tenant_id`` is
denormalised (the value is implicit in the parent ``data_uploads``
row) per ADR-0035 §3 — RLS evaluates row-locally without a JOIN against
the parent.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class DataUpload(Base):
    """One Excel workbook uploaded via the web Excel-import endpoint.

    Uploads are immutable in Phase 2 — there is no update, no soft-
    delete, no versioning. Re-uploading the same bytes is rejected by
    the ``(tenant_id, file_hash)`` unique constraint at the database
    boundary; the route handler surfaces the existing record instead
    of creating a duplicate.
    """

    __tablename__ = "data_uploads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "file_hash", name="uq_data_uploads_tenant_file_hash"),
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
    uploaded_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    format_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DataUploadSheet(Base):
    """One sheet (one DataFrame) belonging to a :class:`DataUpload`."""

    __tablename__ = "data_upload_sheets"
    __table_args__ = (
        UniqueConstraint("upload_id", "sheet_name", name="uq_data_upload_sheets_upload_sheet"),
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
    upload_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_uploads.id", ondelete="CASCADE"),
        nullable=False,
    )
    sheet_name: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
