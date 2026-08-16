# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentCashflowRepository — persistence for cashflow rows.

Backs the ``investment_cashflows`` table introduced in migration
b006 (per ADR-0043 §1). Unlike ``investment_navs``, there is **no
UNIQUE constraint** — multiple cashflows per investment / timestamp
/ type / kind are permitted, matching the operational reality that
several capital calls or fees can share the same day. Every
:meth:`create` therefore appends a fresh row; there is no upsert
path.

The Excel-import (B1.1) replace-by-investment workflow uses
:meth:`delete_by_investment` to clear an investment's cashflow
history before re-inserting the Excel-derived rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text, update

from core.models.investment_cashflow import InvestmentCashflow
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class InvestmentCashflowDTO:
    """Plain data-only view of an ``investment_cashflows`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    flow_timestamp: datetime
    flow_type: str
    flow_kind: str
    amount: Decimal
    currency: str
    description: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    # b021 / ADR-0092. Defaulted so existing direct constructions of the
    # DTO stay valid. ``source`` is the free-text provenance (the live
    # provider name); ``ingest_origin`` is the producer that wrote the
    # row ('excel' | 'live' | 'manual').
    source: str | None = None
    ingest_origin: str = "excel"


def _to_dto(model: InvestmentCashflow) -> InvestmentCashflowDTO:
    return InvestmentCashflowDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        flow_timestamp=model.flow_timestamp,
        flow_type=model.flow_type,
        flow_kind=model.flow_kind,
        amount=model.amount,
        currency=model.currency,
        description=model.description,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        source=model.source,
        ingest_origin=model.ingest_origin,
    )


class InvestmentCashflowRepository(BaseRepository):
    """Read and write investment cashflow rows in the active tenant."""

    async def get_by_id(self, cashflow_id: UUID) -> InvestmentCashflowDTO | None:
        """Return the cashflow row with the given id, or ``None`` if absent.

        The web CRUD surface (sub-stream 4b) needs an explicit
        existence check on update / delete paths so the route layer
        can also verify that the cashflow belongs to the investment
        identified in the path — RLS handles cross-tenant isolation
        but does not catch an id-mixing attempt within one tenant.

        Args:
            cashflow_id: The cashflow row to look up.

        Returns:
            The matching :class:`InvestmentCashflowDTO`, or ``None``
            if no cashflow with this id exists in the active tenant
            context.
        """
        result = await self._session.execute(
            select(InvestmentCashflow).where(InvestmentCashflow.id == cashflow_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_by_investment(self, investment_id: UUID) -> list[InvestmentCashflowDTO]:
        """Return every cashflow row for an investment.

        Args:
            investment_id: The investment whose cashflow history to
                load.

        Returns:
            All cashflow rows (plan and actual interleaved) sorted
            by ``flow_timestamp`` ascending. Empty list for an
            unknown investment.
        """
        result = await self._session.execute(
            select(InvestmentCashflow)
            .where(InvestmentCashflow.investment_id == investment_id)
            .order_by(InvestmentCashflow.flow_timestamp.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investment_and_kind(
        self, investment_id: UUID, flow_kind: str
    ) -> list[InvestmentCashflowDTO]:
        """Return cashflow rows of a single ``flow_kind``.

        Args:
            investment_id: The investment whose cashflow history to
                load.
            flow_kind: ``"plan"`` or ``"actual"``.

        Returns:
            Matching cashflow rows sorted by ``flow_timestamp``
            ascending.
        """
        result = await self._session.execute(
            select(InvestmentCashflow)
            .where(
                InvestmentCashflow.investment_id == investment_id,
                InvestmentCashflow.flow_kind == flow_kind,
            )
            .order_by(InvestmentCashflow.flow_timestamp.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self, investment_ids: list[UUID]
    ) -> dict[UUID, list[InvestmentCashflowDTO]]:
        """Batch counterpart to :meth:`list_by_investment`.

        See :meth:`InvestmentNavRepository.list_by_investments` for
        the contract and motivation (P6-H).

        Args:
            investment_ids: The investments whose cashflow history
                to load. Empty list returns an empty dict.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            with rows sorted by ``flow_timestamp`` ascending.
        """
        if not investment_ids:
            return {}
        result = await self._session.execute(
            select(InvestmentCashflow)
            .where(InvestmentCashflow.investment_id.in_(investment_ids))
            .order_by(
                InvestmentCashflow.investment_id.asc(),
                InvestmentCashflow.flow_timestamp.asc(),
            )
        )
        grouped: dict[UUID, list[InvestmentCashflowDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def list_by_investments_and_kind(
        self, investment_ids: list[UUID], flow_kind: str
    ) -> dict[UUID, list[InvestmentCashflowDTO]]:
        """Batch counterpart to :meth:`list_by_investment_and_kind`.

        Args:
            investment_ids: The investments whose cashflow history
                to load. Empty list returns an empty dict.
            flow_kind: ``"plan"`` or ``"actual"``.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            with rows sorted by ``flow_timestamp`` ascending.
        """
        if not investment_ids:
            return {}
        result = await self._session.execute(
            select(InvestmentCashflow)
            .where(
                InvestmentCashflow.investment_id.in_(investment_ids),
                InvestmentCashflow.flow_kind == flow_kind,
            )
            .order_by(
                InvestmentCashflow.investment_id.asc(),
                InvestmentCashflow.flow_timestamp.asc(),
            )
        )
        grouped: dict[UUID, list[InvestmentCashflowDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def create(
        self,
        investment_id: UUID,
        flow_timestamp: datetime,
        flow_type: str,
        flow_kind: str,
        amount: Decimal,
        currency: str,
        description: str | None,
        created_by: UUID,
        *,
        source: str | None = None,
        ingest_origin: str = "excel",
    ) -> InvestmentCashflowDTO:
        """Append a new cashflow row.

        ``tenant_id`` is read from ``app.tenant_id`` so the session
        context is the single source of truth for tenant binding;
        RLS WITH CHECK re-validates the value as defence in depth
        (per ADR-0035 §6).

        There is no UNIQUE constraint on the table, so repeated
        invocation with identical arguments creates multiple distinct
        rows by design — this matches the operational reality of
        several cashflows arriving on the same day. Live-ingest
        idempotency is therefore enforced by the caller with the
        deterministic dedup key (ADR-0092,
        :mod:`services.investments.cashflow_dedup_key`), not by this
        method.

        Args:
            investment_id: The investment this cashflow belongs to.
            flow_timestamp: TIMESTAMPTZ of the flow event. The
                operational convention is 12:00 UTC when the precise
                time is unknown.
            flow_type: One of seven allowed values: ``capital_call``,
                ``distribution``, ``fee``, ``carry``, ``dividend``,
                ``coupon``, ``other``.
            flow_kind: ``"plan"`` or ``"actual"``.
            amount: Signed cashflow amount (calls negative,
                distributions positive by convention).
            currency: ISO 4217 currency code.
            description: Optional free-form description.
            created_by: UUID of the user creating the row.
            source: Optional free-text provenance (the live provider
                name); ``None`` for Excel / manual rows.
            ingest_origin: The producer writing the row — ``'excel'``
                (default), ``'live'``, or ``'manual'`` (ADR-0092).

        Returns:
            The newly created :class:`InvestmentCashflowDTO`.

        Raises:
            sqlalchemy.exc.IntegrityError: If ``flow_type`` or
                ``flow_kind`` violates the CHECK constraint, or if
                the FK to ``investments`` does not resolve.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = InvestmentCashflow(
            tenant_id=active_tenant,
            investment_id=investment_id,
            flow_timestamp=flow_timestamp,
            flow_type=flow_type,
            flow_kind=flow_kind,
            amount=amount,
            currency=currency,
            description=description,
            source=source,
            ingest_origin=ingest_origin,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def update(
        self,
        cashflow_id: UUID,
        *,
        flow_timestamp: datetime | None = None,
        flow_type: str | None = None,
        flow_kind: str | None = None,
        amount: Decimal | None = None,
        currency: str | None = None,
        description: str | None = None,
        ingest_origin: str | None = None,
    ) -> InvestmentCashflowDTO | None:
        """Update mutable fields on a cashflow row.

        Only fields whose argument is not ``None`` are modified.
        ``investment_id`` and ``created_by`` are immutable through
        this method — re-attributing a cashflow to a different
        investment goes via delete + create.

        Args:
            cashflow_id: The cashflow row to update.
            flow_timestamp: New flow timestamp.
            flow_type: New flow type discriminator.
            flow_kind: New flow kind discriminator.
            amount: New amount value.
            currency: New currency code.
            description: New description.
            ingest_origin: When given, restamps the row's producer
                (the manual CRUD surface passes ``'manual'`` so a
                hand edit is no longer live-overwritable, ADR-0092).

        Returns:
            The refreshed :class:`InvestmentCashflowDTO`, or ``None``
            if no cashflow with this id exists in the active tenant
            context.
        """
        values: dict[str, object] = {}
        if flow_timestamp is not None:
            values["flow_timestamp"] = flow_timestamp
        if flow_type is not None:
            values["flow_type"] = flow_type
        if flow_kind is not None:
            values["flow_kind"] = flow_kind
        if amount is not None:
            values["amount"] = amount
        if currency is not None:
            values["currency"] = currency
        if description is not None:
            values["description"] = description
        if ingest_origin is not None:
            values["ingest_origin"] = ingest_origin
        if values:
            values["updated_at"] = text("NOW()")
            await self._session.execute(
                update(InvestmentCashflow)
                .where(InvestmentCashflow.id == cashflow_id)
                .values(**values)
            )
            await self._session.flush()

        result = await self._session.execute(
            select(InvestmentCashflow).where(InvestmentCashflow.id == cashflow_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def delete_by_investment(self, investment_id: UUID) -> int:
        """Delete every cashflow row for an investment.

        The Excel-import (B1.1) replace-by-investment workflow uses
        this to clear an investment's cashflow history before
        re-inserting the Excel-derived rows.

        Args:
            investment_id: The investment whose cashflows to delete.

        Returns:
            The number of rows that were deleted.
        """
        result = await self._session.execute(
            delete(InvestmentCashflow).where(InvestmentCashflow.investment_id == investment_id)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete(self, cashflow_id: UUID) -> bool:
        """Hard-delete a single cashflow row.

        Args:
            cashflow_id: The cashflow row to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no cashflow
            with this id existed in the active tenant context.
        """
        result = await self._session.execute(
            delete(InvestmentCashflow).where(InvestmentCashflow.id == cashflow_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
