# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneFindingRepository — persistence for Irene's append-only findings.

Backs the ``irene_finding`` table introduced in migration b019 (per
ADR-0085 §``irene_finding``). Shape mirrors the other tenant-scoped
repositories: tenant-scoped :class:`AsyncSession`, a frozen DTO,
``tenant_id`` implicit in the session context (RLS WITH CHECK derives
it from ``app.tenant_id``).

Findings are append-only: a row is never mutated except to record its
resolution. :meth:`IreneFindingRepository.resolve` is the only writer
that touches an existing row, and it validates the resolution value
against the canonical vocabulary before writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text, update

from core.exceptions import IreneResolutionInvalid
from core.models.irene_finding import IreneFinding
from core.repositories.base import BaseRepository

# Canonical lowercase resolution vocabulary (ADR-0085 / ADR-0088 / ADR-0107).
# The column is plain TEXT; the vocabulary is enforced here, not as a SQL
# enum, matching the codebase's TEXT-for-status convention. ``opened_case``
# (ADR-0107, C4) is the fifth member: it records that a finding was handed
# over to a Case. It is written **only** by the case-opening composition in
# ``web/routes/watch_desk.py`` (``CaseRepository.create`` +
# ``resolve(..., 'opened_case')`` in one transaction); the Watch Desk's own
# resolve endpoint never accepts it (its set stays acted/dismissed/
# acknowledged).
_VALID_RESOLUTIONS: frozenset[str] = frozenset(
    {"open", "acted", "dismissed", "acknowledged", "opened_case"}
)


@dataclass(frozen=True)
class IreneFindingDTO:
    """Plain data-only view of an ``irene_finding`` row."""

    id: UUID
    tenant_id: UUID
    subject_key: str
    payload: dict
    urgency: int
    band: str
    resolution: str
    resolved_at: datetime | None
    resolved_by: UUID | None
    created_at: datetime


def _to_dto(model: IreneFinding) -> IreneFindingDTO:
    return IreneFindingDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        subject_key=model.subject_key,
        payload=model.payload,
        urgency=model.urgency,
        band=model.band,
        resolution=model.resolution,
        resolved_at=model.resolved_at,
        resolved_by=model.resolved_by,
        created_at=model.created_at,
    )


class IreneFindingRepository(BaseRepository):
    """Append and read Irene findings in the active tenant context."""

    async def append(
        self,
        *,
        subject_key: str,
        payload: dict,
        urgency: int,
        band: str,
    ) -> IreneFindingDTO:
        """Append a new finding; ``resolution`` defaults to ``'open'``.

        Args:
            subject_key: Reference to the monitored subject. Not an FK.
            payload: The ``surface_finding`` contract (ADR-0088); opaque
                to persistence.
            urgency: The final urgency after the deterministic floor
                (ADR-0088), not Irene's suggestion.
            band: The band derived from the final urgency.

        Returns:
            The newly created :class:`IreneFindingDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = IreneFinding(
            tenant_id=active_tenant,
            subject_key=subject_key,
            payload=payload,
            urgency=urgency,
            band=band,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def get(self, finding_id: UUID) -> IreneFindingDTO | None:
        """Return one finding by id in the active tenant, or ``None``.

        Added for the case origin embed (ADR-0107, C3a): a case *references*
        its originating finding and renders it read-only. This is a read only
        — resolution writes remain :meth:`resolve` alone (ADR-0085); nothing
        else on this module mutates a finding. RLS scopes the read to the
        active tenant, so a foreign-tenant id reads back as ``None`` exactly as
        an unknown id does.

        ``populate_existing`` mirrors the sibling feed reads so a lookup after
        an in-transaction :meth:`resolve` reflects the current resolution
        rather than a stale mapped instance.

        Args:
            finding_id: The finding to load.

        Returns:
            The matching :class:`IreneFindingDTO`, or ``None`` when no such
            finding is visible in the active tenant context.
        """
        result = await self._session.execute(
            select(IreneFinding)
            .where(IreneFinding.id == finding_id)
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_open(self) -> list[IreneFindingDTO]:
        """Return open findings in Briefing-feed order.

        Ordered by ``urgency`` descending then ``created_at`` descending
        (most urgent, then most recent first) — the order the Briefing
        feed consumes (Prompt 5).

        Returns:
            All findings whose ``resolution`` is ``'open'`` in the active
            tenant context.
        """
        # populate_existing so a read after an in-transaction resolve()
        # (a Core UPDATE that bypasses the ORM identity map) reflects the
        # current resolution rather than a stale mapped instance.
        result = await self._session.execute(
            select(IreneFinding)
            .where(IreneFinding.resolution == "open")
            .order_by(
                IreneFinding.urgency.desc(),
                IreneFinding.created_at.desc(),
            )
            .execution_options(populate_existing=True)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def count_since(self, *, since: datetime) -> int:
        """Count findings created at or after ``since`` in the active tenant.

        Backs the Watch Desk "surfaced N findings" tile (ADR-0089): the
        honest claim is "created since the last beat" — ``created_at >= since``
        — and nothing stronger. Counts findings of **every** resolution, not
        just open ones: a finding surfaced by the last beat may already have
        been resolved, and an open-only count would understate what the beat
        put in front of the manager. Tenant-scoped by the active RLS context.

        Args:
            since: The lower bound (inclusive), typically the schedule's
                ``last_beat_at``. Callers with no beat on record do not call
                this — there is no beat to count since.

        Returns:
            The number of matching findings in the active tenant context.
        """
        result = await self._session.execute(
            select(func.count()).select_from(IreneFinding).where(IreneFinding.created_at >= since)
        )
        return int(result.scalar_one())

    async def list_journal(self, limit: int = 100) -> list[IreneFindingDTO]:
        """Return the full finding history, newest first.

        Args:
            limit: Maximum number of rows to return (most recent).

        Returns:
            Findings in the active tenant context ordered by
            ``created_at`` descending, capped at ``limit``.
        """
        result = await self._session.execute(
            select(IreneFinding)
            .order_by(IreneFinding.created_at.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def resolve(
        self,
        *,
        finding_id: UUID,
        resolution: str,
        resolved_by: UUID | None,
        resolved_at: datetime,
    ) -> None:
        """Record the resolution of one finding.

        Writes ``resolution`` / ``resolved_at`` / ``resolved_by`` on a
        single row. The immutable history fields (``payload``,
        ``urgency``, ``band``, ``created_at``) are never touched.

        Args:
            finding_id: The finding to resolve.
            resolution: One of ``open`` / ``acted`` / ``dismissed`` /
                ``acknowledged`` / ``opened_case`` (lowercase). ``opened_case``
                is written only by the case-opening composition (ADR-0107, C4),
                never by the Watch Desk's resolve endpoint.
            resolved_by: The resolving user id when known, else ``None``.
            resolved_at: When the finding was resolved.

        Raises:
            IreneResolutionInvalid: If ``resolution`` is not in the
                canonical vocabulary.
        """
        if resolution not in _VALID_RESOLUTIONS:
            raise IreneResolutionInvalid(
                f"Invalid finding resolution {resolution!r}; expected one of "
                f"{sorted(_VALID_RESOLUTIONS)}.",
                field="resolution",
            )
        await self._session.execute(
            update(IreneFinding)
            .where(IreneFinding.id == finding_id)
            .values(
                resolution=resolution,
                resolved_at=resolved_at,
                resolved_by=resolved_by,
            )
        )
        await self._session.flush()
