# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneBriefingRepository — persistence for the beat's briefing rows.

Backs the ``irene_briefing`` table introduced in migration b036 (per
ADR-0133 §1.1). Shape mirrors the other tenant-scoped repositories:
tenant-scoped :class:`AsyncSession`, a frozen DTO, ``tenant_id`` taken
from the session context (RLS WITH CHECK derives it from
``app.tenant_id``).

A briefing row is append-only and immutable, so the repository is a thin
storage half: one validating :meth:`IreneBriefingRepository.append` and
three reads. There is no update and no delete. ``append`` checks the
shape the schema CHECKs pin before any SQL, so a malformed row surfaces
as :class:`~core.exceptions.IreneBriefingInvalid` with a named field
rather than as an ``IntegrityError``. A duplicate ``(tenant, beat_at)``
is left to the unique constraint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text

from core.exceptions import IreneBriefingInvalid
from core.models.irene_briefing import (
    BRIEFING_SKIP_REASONS,
    BRIEFING_STATUSES,
    IreneBriefing,
)
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class IreneBriefingDTO:
    """Plain data-only view of an ``irene_briefing`` row."""

    id: UUID
    tenant_id: UUID
    beat_at: datetime
    written_at: datetime
    status: str
    skip_reason: str | None
    text_markets: str | None
    text_watch: str | None
    inputs: dict
    finding_ids: tuple[UUID, ...]
    model_id: str | None
    created_at: datetime


def _to_dto(model: IreneBriefing) -> IreneBriefingDTO:
    return IreneBriefingDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        beat_at=model.beat_at,
        written_at=model.written_at,
        status=model.status,
        skip_reason=model.skip_reason,
        text_markets=model.text_markets,
        text_watch=model.text_watch,
        inputs=model.inputs,
        finding_ids=tuple(model.finding_ids),
        model_id=model.model_id,
        created_at=model.created_at,
    )


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IreneBriefingInvalid(
            f"{field} must be timezone-aware; the column is TIMESTAMPTZ and "
            "a naive instant has no defined position on it.",
            field=field,
        )


class IreneBriefingRepository(BaseRepository):
    """Append and read briefing rows in the active tenant context."""

    async def append(
        self,
        *,
        beat_at: datetime,
        written_at: datetime,
        status: str,
        inputs: dict,
        finding_ids: Sequence[UUID],
        skip_reason: str | None = None,
        text_markets: str | None = None,
        text_watch: str | None = None,
        model_id: str | None = None,
    ) -> IreneBriefingDTO:
        """Append one briefing row for one beat.

        Args:
            beat_at: The beat this briefing belongs to (timezone-aware).
                One row per tenant and ``beat_at``.
            written_at: When the beat finished writing (timezone-aware).
            status: ``written`` or ``skipped``.
            inputs: What Irene was handed; opaque to persistence.
            finding_ids: The findings the beat appended; plain references.
            skip_reason: Required iff ``status == 'skipped'``: one of
                ``llm_error``, ``invalid_output``, ``nothing_to_write``.
            text_markets: Irene's markets part; required iff written.
            text_watch: Irene's watch part; required iff written. The empty
                string is valid (every family switched off).
            model_id: The model that wrote the briefing, if a call was made.

        Returns:
            The newly created :class:`IreneBriefingDTO`.

        Raises:
            IreneBriefingInvalid: If a timestamp is naive, ``status`` or
                ``skip_reason`` is outside its vocabulary, ``skip_reason``
                is not present exactly when skipped, or the texts are not
                both present when written and both absent when skipped.
        """
        _require_aware(beat_at, field="beat_at")
        _require_aware(written_at, field="written_at")
        if status not in BRIEFING_STATUSES:
            raise IreneBriefingInvalid(
                f"Invalid briefing status {status!r}; expected one of {list(BRIEFING_STATUSES)}.",
                field="status",
            )
        skipped = status == "skipped"
        if skipped and skip_reason is None:
            raise IreneBriefingInvalid(
                "A skipped briefing must name its skip_reason.",
                field="skip_reason",
            )
        if not skipped and skip_reason is not None:
            raise IreneBriefingInvalid(
                f"A {status} briefing carries no skip_reason; got {skip_reason!r}.",
                field="skip_reason",
            )
        if skip_reason is not None and skip_reason not in BRIEFING_SKIP_REASONS:
            raise IreneBriefingInvalid(
                f"Invalid skip_reason {skip_reason!r}; expected one of "
                f"{list(BRIEFING_SKIP_REASONS)}.",
                field="skip_reason",
            )
        for field, value in (("text_markets", text_markets), ("text_watch", text_watch)):
            if not skipped and value is None:
                raise IreneBriefingInvalid(
                    f"A written briefing must carry {field}.",
                    field=field,
                )
            if skipped and value is not None:
                raise IreneBriefingInvalid(
                    f"A skipped briefing carries no {field}.",
                    field=field,
                )

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = IreneBriefing(
            tenant_id=active_tenant,
            beat_at=beat_at,
            written_at=written_at,
            status=status,
            skip_reason=skip_reason,
            text_markets=text_markets,
            text_watch=text_watch,
            inputs=inputs,
            finding_ids=list(finding_ids),
            model_id=model_id,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def newest(self) -> IreneBriefingDTO | None:
        """Return the briefing of the latest beat, or ``None``.

        Ordered by ``beat_at``, not ``created_at``: the briefing shown is
        the one of the newest beat, whatever order the rows were written in.

        Returns:
            The :class:`IreneBriefingDTO` with the largest ``beat_at`` in the
            active tenant context, or ``None`` when there is none.
        """
        result = await self._session.execute(
            select(IreneBriefing)
            .order_by(IreneBriefing.beat_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get(self, briefing_id: UUID) -> IreneBriefingDTO | None:
        """Return one briefing by id in the active tenant, or ``None``.

        RLS scopes the read to the active tenant, so a foreign-tenant id
        reads back as ``None`` exactly as an unknown id does.

        Args:
            briefing_id: The briefing to load.

        Returns:
            The matching :class:`IreneBriefingDTO`, or ``None``.
        """
        result = await self._session.execute(
            select(IreneBriefing)
            .where(IreneBriefing.id == briefing_id)
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_between(self, *, start: datetime, end: datetime) -> list[IreneBriefingDTO]:
        """Return the briefings with ``start <= beat_at < end``, newest first.

        Args:
            start: Inclusive lower bound (timezone-aware).
            end: Exclusive upper bound (timezone-aware), after ``start``.

        Returns:
            The matching briefings in the active tenant context, ordered by
            ``beat_at`` descending.

        Raises:
            IreneBriefingInvalid: If a bound is naive or ``end <= start``.
        """
        _require_aware(start, field="start")
        _require_aware(end, field="end")
        if end <= start:
            raise IreneBriefingInvalid(
                f"end {end.isoformat()} must lie after start {start.isoformat()}.",
                field="end",
            )
        result = await self._session.execute(
            select(IreneBriefing)
            .where(IreneBriefing.beat_at >= start, IreneBriefing.beat_at < end)
            .order_by(IreneBriefing.beat_at.desc())
            .execution_options(populate_existing=True)
        )
        return [_to_dto(model) for model in result.scalars().all()]
