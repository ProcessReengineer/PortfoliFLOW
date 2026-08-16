# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CaseAttachmentRepository — in-database file attachments for cases (ADR-0107).

Backs the ``case_attachments`` table (migration b031), kept separate from
:class:`~core.repositories.case_repository.CaseRepository` so byte handling
stays out of the case timeline's way. Attachments are addressed **only**
through their pin entry (ADR-0107 §7, the DMS boundary): there are no
folders, no versioning, no content search.

The metadata DTO deliberately omits ``content`` — the bytes are loaded only
by :meth:`CaseAttachmentRepository.get_with_content`, and the list / count
reads never touch the BYTEA column. Size / count caps and the MIME-type
whitelist are configuration enforced at the route layer, not here; the only
guard this layer owns is closed-case immutability (ADR-0107 §4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, insert, select

from core.exceptions import CaseClosedError, CaseStateInvalid
from core.models.case import Case, CaseAttachment
from core.repositories.base import BaseRepository

_ATTACHMENT_METADATA_COLUMNS = (
    CaseAttachment.id,
    CaseAttachment.tenant_id,
    CaseAttachment.case_id,
    CaseAttachment.filename,
    CaseAttachment.mime_type,
    CaseAttachment.size_bytes,
    CaseAttachment.sha256,
    CaseAttachment.uploaded_by,
    CaseAttachment.created_at,
)


@dataclass(frozen=True)
class CaseAttachmentDTO:
    """Metadata-only view of a ``case_attachments`` row (no ``content``).

    The file bytes are never carried on this DTO; fetch them explicitly via
    :meth:`CaseAttachmentRepository.get_with_content`.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    uploaded_by: UUID
    created_at: datetime


class CaseAttachmentRepository(BaseRepository):
    """Create and read case attachments in the active tenant context."""

    async def create(
        self,
        case_id: UUID,
        *,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        content: bytes,
        uploaded_by: UUID,
        now: datetime,
    ) -> CaseAttachmentDTO:
        """Store one attachment against an open case.

        The parent case must exist in the active tenant and be open — a
        closed case is immutable in its entirety (ADR-0107 §4). ``tenant_id``
        is denormalised from the parent case row (read alongside its state)
        so the write matches the RLS row-locality idiom without a second
        lookup.

        Args:
            case_id: The case to attach to.
            filename: Display filename (route-sanitised).
            mime_type: The attachment's MIME type.
            size_bytes: Size of ``content`` in bytes.
            sha256: Hex-encoded SHA-256 of ``content``. There is no dedup —
                the same document pinned in two cases is stored twice by
                design (ADR-0107 §7).
            content: The raw file bytes.
            uploaded_by: The uploading user.
            now: The ``created_at`` timestamp.

        Returns:
            The stored :class:`CaseAttachmentDTO` (metadata only).

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is already closed.
        """
        row = (
            await self._session.execute(
                select(Case.state, Case.tenant_id).where(Case.id == case_id)
            )
        ).one_or_none()
        if row is None:
            raise CaseStateInvalid(
                f"No case {case_id} in this tenant to attach to.",
                field="state",
            )
        state, tenant_id = row
        if state != "open":
            raise CaseClosedError(
                f"Case {case_id} is closed; closed cases are immutable (ADR-0107 §4)."
            )

        stmt = (
            insert(CaseAttachment)
            .values(
                tenant_id=tenant_id,
                case_id=case_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                sha256=sha256,
                content=content,
                uploaded_by=uploaded_by,
                created_at=now,
            )
            .returning(*_ATTACHMENT_METADATA_COLUMNS)
        )
        created = (await self._session.execute(stmt)).one()
        return CaseAttachmentDTO(**created._mapping)

    async def get_with_content(self, attachment_id: UUID) -> tuple[CaseAttachmentDTO, bytes] | None:
        """Return one attachment's metadata and bytes, or ``None`` if absent.

        The single read path that loads the BYTEA column. Used by the route
        that streams a pinned document back to the browser.
        """
        model = (
            await self._session.execute(
                select(CaseAttachment).where(CaseAttachment.id == attachment_id)
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        return _attachment_to_dto(model), model.content

    async def list_for_case(self, case_id: UUID) -> list[CaseAttachmentDTO]:
        """Return a case's attachments (metadata only), oldest first.

        The BYTEA ``content`` column is never selected here — the list carries
        no bytes.
        """
        result = await self._session.execute(
            select(*_ATTACHMENT_METADATA_COLUMNS)
            .where(CaseAttachment.case_id == case_id)
            .order_by(CaseAttachment.created_at.asc())
        )
        return [CaseAttachmentDTO(**row._mapping) for row in result.all()]

    async def count_for_case(self, case_id: UUID) -> int:
        """Count a case's attachments.

        The route layer enforces the per-case cap against this count — caps
        are configuration, not schema (ADR-0107 §7).
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(CaseAttachment)
            .where(CaseAttachment.case_id == case_id)
        )
        return int(result.scalar_one())


def _attachment_to_dto(model: CaseAttachment) -> CaseAttachmentDTO:
    return CaseAttachmentDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        case_id=model.case_id,
        filename=model.filename,
        mime_type=model.mime_type,
        size_bytes=model.size_bytes,
        sha256=model.sha256,
        uploaded_by=model.uploaded_by,
        created_at=model.created_at,
    )
