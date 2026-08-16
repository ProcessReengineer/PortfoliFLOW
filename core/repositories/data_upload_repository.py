# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""DataUploadRepository — persistence for the web Excel-import endpoint.

Backs the Phase-2 (sub-stream 2d) ``data_uploads`` and
``data_upload_sheets`` tables (migration b004). The shape mirrors the
other Phase-1/2 repositories: a tenant-scoped :class:`AsyncSession` is
passed in, methods return frozen DTOs, ``tenant_id`` is implicit in
the session context (RLS WITH CHECK derives it from
``app.tenant_id``).

Per ADR-0041 §3, this repository owns the *web* Excel-import write
path. The PyQt6 GUI continues to write to the in-memory
:class:`~core.data_store.DataStore`. The two surfaces deliberately do
not share data during Phase 2 / 3; convergence is Phase-4 work.

JSONB shape
-----------
Each :class:`pandas.DataFrame` is serialised to the
``DataFrame.to_dict('split')`` shape — ``{"index", "columns",
"data"}`` — via :func:`pandas.DataFrame.to_json` so timestamps and
``NaN`` are JSON-safe. The structure round-trips through
:func:`pandas.read_json` / :func:`pandas.DataFrame` without losing the
shape (dtypes are best-effort; this is a Phase-2 transport
representation, not the Phase-4 normalised schema).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy import select, text

from core.models.data_upload import DataUpload, DataUploadSheet
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class DataUploadDTO:
    """Plain data-only view of a ``data_uploads`` row."""

    id: UUID
    tenant_id: UUID
    uploaded_by: UUID
    filename: str
    file_hash: str
    size_bytes: int
    format_version: str
    created_at: datetime


@dataclass(frozen=True)
class DataUploadSheetDTO:
    """Plain data-only view of a ``data_upload_sheets`` row.

    ``data`` is the ``DataFrame.to_dict('split')`` representation —
    keys ``"index"``, ``"columns"``, ``"data"``. Round-trip back to a
    DataFrame with ``pandas.DataFrame(**dto.data)``; date-indexed
    sheets land with string-typed indexes that callers re-coerce as
    needed.
    """

    id: UUID
    upload_id: UUID
    sheet_name: str
    data: dict[str, Any]
    row_count: int
    column_count: int


def _upload_to_dto(model: DataUpload) -> DataUploadDTO:
    return DataUploadDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        uploaded_by=model.uploaded_by,
        filename=model.filename,
        file_hash=model.file_hash,
        size_bytes=model.size_bytes,
        format_version=model.format_version,
        created_at=model.created_at,
    )


def _sheet_to_dto(model: DataUploadSheet) -> DataUploadSheetDTO:
    return DataUploadSheetDTO(
        id=model.id,
        upload_id=model.upload_id,
        sheet_name=model.sheet_name,
        data=model.data,
        row_count=model.row_count,
        column_count=model.column_count,
    )


def _df_to_jsonb_payload(df: pd.DataFrame) -> dict[str, Any]:
    """Serialise a DataFrame to a JSON-safe ``to_dict('split')`` dict.

    ``DataFrame.to_dict('split')`` returns ``Timestamp`` objects in the
    index list and ``NaN`` floats in the data — neither is valid JSON.
    Going via :func:`pandas.DataFrame.to_json` lets pandas handle the
    coercion (timestamps → ISO-8601 strings, ``NaN`` → ``null``) and
    the JSON parse round-trip yields a plain Python dict that the
    asyncpg JSONB codec accepts directly.
    """
    return json.loads(df.to_json(orient="split", date_format="iso"))


class DataUploadRepository(BaseRepository):
    """Read and write upload records in the active tenant context."""

    async def create_upload(
        self,
        uploaded_by: UUID,
        filename: str,
        file_hash: str,
        size_bytes: int,
        format_version: str,
        sheets: dict[str, pd.DataFrame],
    ) -> DataUploadDTO:
        """Create the parent and child rows in a single transaction.

        ``tenant_id`` is read from ``app.tenant_id`` (the active session
        context), matching the pattern established in
        :meth:`UserRepository.create`. ``uploaded_by`` must equal
        ``app.user_id`` — the b004 restrictive policy enforces this at
        the database boundary so a route handler that forgets to wire
        the authenticated user fails loudly rather than writing an
        attributable-to-no-one row.

        Args:
            uploaded_by: UUID of the authenticated user. Must equal the
                value of ``app.user_id`` in the current session
                (enforced by RLS).
            filename: Sanitised display name (basename only, ≤ 255
                chars). Sanitisation is the route handler's
                responsibility; the repository stores whatever it is
                given.
            file_hash: SHA-256 of the file bytes, hex-encoded. The
                ``(tenant_id, file_hash)`` unique constraint provides
                upload deduplication.
            size_bytes: Size of the original file in bytes.
            format_version: Format identifier (currently always ``"v2"``
                — see ADR-0009).
            sheets: Mapping of canonical snake_case sheet name to the
                parsed DataFrame, exactly as returned by
                :func:`modules.front_office.data_import.load_excel`.

        Returns:
            The :class:`DataUploadDTO` for the newly created upload.
        """
        # Read the active tenant from the GUC so the application stays
        # one source of truth — the session context determines the
        # tenant binding, not method arguments. RLS WITH CHECK then
        # re-validates the value (defence in depth, ADR-0035 §6).
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        upload = DataUpload(
            tenant_id=active_tenant,
            uploaded_by=uploaded_by,
            filename=filename,
            file_hash=file_hash,
            size_bytes=size_bytes,
            format_version=format_version,
        )
        self._session.add(upload)
        # flush so upload.id is populated for the FK on each sheet row
        await self._session.flush()

        for sheet_name, df in sheets.items():
            payload = _df_to_jsonb_payload(df)
            sheet = DataUploadSheet(
                tenant_id=active_tenant,
                upload_id=upload.id,
                sheet_name=sheet_name,
                data=payload,
                row_count=int(df.shape[0]),
                column_count=int(df.shape[1]),
            )
            self._session.add(sheet)

        await self._session.flush()
        await self._session.refresh(upload)
        return _upload_to_dto(upload)

    async def get_by_hash(self, file_hash: str) -> DataUploadDTO | None:
        """Return the upload matching ``file_hash`` in the current tenant.

        Used by the route handler to detect duplicate uploads before
        consuming any parsing work. RLS hides hashes from other
        tenants — a collision (extremely unlikely with SHA-256) across
        tenants is invisible to either side.
        """
        result = await self._session.execute(
            select(DataUpload).where(DataUpload.file_hash == file_hash)
        )
        model = result.scalar_one_or_none()
        return _upload_to_dto(model) if model is not None else None

    async def get_by_id(self, upload_id: UUID) -> DataUploadDTO | None:
        """Return the upload with the given id in the current tenant."""
        result = await self._session.execute(select(DataUpload).where(DataUpload.id == upload_id))
        model = result.scalar_one_or_none()
        return _upload_to_dto(model) if model is not None else None

    async def list_recent(self, limit: int = 20) -> list[DataUploadDTO]:
        """Return the most recent uploads, newest first.

        Args:
            limit: Maximum number of rows to return. Defaults to 20 —
                the recent-uploads list on the data-import page is the
                primary caller and shows a small fixed window.
        """
        result = await self._session.execute(
            select(DataUpload).order_by(DataUpload.created_at.desc()).limit(limit)
        )
        return [_upload_to_dto(model) for model in result.scalars().all()]

    async def get_sheets(self, upload_id: UUID) -> list[DataUploadSheetDTO]:
        """Return every sheet belonging to ``upload_id``, ordered by name."""
        result = await self._session.execute(
            select(DataUploadSheet)
            .where(DataUploadSheet.upload_id == upload_id)
            .order_by(DataUploadSheet.sheet_name)
        )
        return [_sheet_to_dto(model) for model in result.scalars().all()]

    async def get_sheet(self, upload_id: UUID, sheet_name: str) -> DataUploadSheetDTO | None:
        """Return one named sheet of an upload, or ``None`` if absent."""
        result = await self._session.execute(
            select(DataUploadSheet).where(
                DataUploadSheet.upload_id == upload_id,
                DataUploadSheet.sheet_name == sheet_name,
            )
        )
        model = result.scalar_one_or_none()
        return _sheet_to_dto(model) if model is not None else None
