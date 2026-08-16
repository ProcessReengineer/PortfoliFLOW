# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneWatchStateRepository — persistence for Irene's typed world state.

Backs the ``irene_watch_state`` table introduced in migration b019
(per ADR-0085 §``irene_watch_state``). Shape mirrors the other
tenant-scoped repositories: tenant-scoped :class:`AsyncSession`, a
frozen DTO, ``tenant_id`` implicit in the session context (RLS WITH
CHECK derives it from ``app.tenant_id``).

One row per monitored subject is upserted per beat. The upsert must
never clobber the ``acknowledged_*`` fields, which record the state the
user has already seen and are written only by the delta logic
(Prompt 3 / ADR-0086).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.irene_watch_state import IreneWatchState
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class IreneWatchStateDTO:
    """Plain data-only view of an ``irene_watch_state`` row."""

    id: UUID
    tenant_id: UUID
    subject_key: str
    magnitude: Decimal | None
    band: str | None
    acknowledged_at: datetime | None
    acknowledged_magnitude: Decimal | None
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


def _to_dto(model: IreneWatchState) -> IreneWatchStateDTO:
    return IreneWatchStateDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        subject_key=model.subject_key,
        magnitude=model.magnitude,
        band=model.band,
        acknowledged_at=model.acknowledged_at,
        acknowledged_magnitude=model.acknowledged_magnitude,
        last_seen_at=model.last_seen_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class IreneWatchStateRepository(BaseRepository):
    """Read and write Irene watch-state in the active tenant context."""

    async def upsert(
        self,
        *,
        subject_key: str,
        magnitude: Decimal | None,
        band: str | None,
        last_seen_at: datetime,
    ) -> IreneWatchStateDTO:
        """Insert or update the watch-state row for one subject.

        Conflict target is ``(tenant_id, subject_key)``. On conflict
        only ``magnitude``, ``band``, ``last_seen_at`` and ``updated_at``
        are overwritten — the ``acknowledged_*`` fields are left
        untouched, since they record the state the user has already seen
        and are written only by the delta logic (ADR-0086).

        Args:
            subject_key: The stable, rule-formed subject identifier
                (e.g. ``anlv:16``). Never LLM-generated.
            magnitude: The measured quantity, or ``None`` for non-scalar
                subjects.
            band: The deterministically derived band
                (``informational`` / ``noteworthy`` / ``critical``), or
                ``None`` if not yet assigned.
            last_seen_at: The beat timestamp that observed the subject.

        Returns:
            The upserted :class:`IreneWatchStateDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = pg_insert(IreneWatchState).values(
            tenant_id=active_tenant,
            subject_key=subject_key,
            magnitude=magnitude,
            band=band,
            last_seen_at=last_seen_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "subject_key"],
            set_={
                "magnitude": stmt.excluded.magnitude,
                "band": stmt.excluded.band,
                "last_seen_at": stmt.excluded.last_seen_at,
                # pg_insert is not a Core update(), so the model-level
                # onupdate does not fire here — bump updated_at explicitly.
                "updated_at": text("NOW()"),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()

        refreshed = await self.get_by_subject(subject_key)
        # The row exists: we just wrote it in this transaction.
        assert refreshed is not None
        return refreshed

    async def get_by_subject(self, subject_key: str) -> IreneWatchStateDTO | None:
        """Return the watch-state row for ``subject_key``, or ``None``.

        Args:
            subject_key: The subject identifier to look up.

        Returns:
            The matching :class:`IreneWatchStateDTO`, or ``None`` if no
            row exists in the active tenant context.
        """
        # populate_existing so a read after an in-transaction upsert /
        # acknowledge (both bypass the ORM identity map) reflects the
        # freshly written row rather than a stale mapped instance.
        result = await self._session.execute(
            select(IreneWatchState)
            .where(IreneWatchState.subject_key == subject_key)
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[IreneWatchStateDTO]:
        """Return every watch-state row in the active tenant context.

        Backs the Watch Desk monitor's per-subject note (ADR-0089):
        the note is assembled deterministically from the acknowledged
        state each subject carries, so the monitor needs them all in one
        read rather than N per-subject lookups. Tenant-scoped by the
        active RLS context; order is by ``subject_key`` for a stable
        render.

        Returns:
            Every :class:`IreneWatchStateDTO` visible in the active
            tenant context, ordered by ``subject_key``.
        """
        # populate_existing for the same reason get_by_subject uses it:
        # an in-transaction upsert / acknowledge bypasses the identity map.
        result = await self._session.execute(
            select(IreneWatchState)
            .order_by(IreneWatchState.subject_key)
            .execution_options(populate_existing=True)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def acknowledge(
        self,
        *,
        subject_key: str,
        acknowledged_at: datetime,
        acknowledged_magnitude: Decimal | None,
    ) -> None:
        """Record the state the user has seen for one subject.

        Sets the ``acknowledged_*`` fields; the model-level
        ``onupdate`` bumps ``updated_at``. Used by the delta logic
        (Prompt 3) after a finding is surfaced.

        Args:
            subject_key: The subject identifier to acknowledge.
            acknowledged_at: When the state was acknowledged.
            acknowledged_magnitude: The magnitude the user has seen, or
                ``None`` for non-scalar subjects.
        """
        await self._session.execute(
            update(IreneWatchState)
            .where(IreneWatchState.subject_key == subject_key)
            .values(
                acknowledged_at=acknowledged_at,
                acknowledged_magnitude=acknowledged_magnitude,
            )
        )
        await self._session.flush()

    async def reset_acknowledgement(self, subject_key: str) -> None:
        """Null the ``acknowledged_*`` fields for one subject.

        Falling-edge de-escalation (ADR-0086): when a subject drops back
        to a benign band, the acknowledged state is cleared so a later
        rising edge triggers afresh. The model-level ``onupdate`` bumps
        ``updated_at``.

        Args:
            subject_key: The subject identifier to reset.
        """
        await self._session.execute(
            update(IreneWatchState)
            .where(IreneWatchState.subject_key == subject_key)
            .values(acknowledged_at=None, acknowledged_magnitude=None)
        )
        await self._session.flush()
