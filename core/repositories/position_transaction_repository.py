# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PositionTransactionRepository — persistence for the transaction ledger.

Backs the ``position_transactions`` table introduced in migration b024
(per ADR-0097 §2). Shape mirrors the other tenant-scoped repositories: a
tenant-scoped :class:`AsyncSession` is passed in, methods return frozen
DTOs, and ``tenant_id`` is implicit in the session context (RLS WITH
CHECK derives it from ``app.tenant_id`` — the repository never filters on
``tenant_id`` manually).

Scope is deliberately narrow (YAGNI). The validated write path is the
service layer (:meth:`services.investments.InvestmentService.add_position_transaction`,
which enforces currency equality and the non-negativity invariant); this
repository is the raw persistence seam it calls. Sign and price rules are
CHECK-enforced at the DB. There is **no upsert** on the ledger — each
event is its own row — except that the partial unique index
``uq_position_transactions_opening`` makes a second ``opening`` per
investment structurally impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text

from core.models.position_transaction import PositionTransaction
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class PositionTransactionDTO:
    """Plain data-only view of a ``position_transactions`` row.

    Structurally satisfies
    :class:`services.investments.holdings.LedgerTransaction` so holdings
    derivation consumes it directly without the pure module importing this
    class.
    """

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    txn_type: str
    trade_date: _date
    units: Decimal
    price_per_unit: Decimal | None
    consideration: Decimal | None
    currency: str
    note: str | None
    source: str | None
    ingest_origin: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: PositionTransaction) -> PositionTransactionDTO:
    return PositionTransactionDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        txn_type=model.txn_type,
        trade_date=model.trade_date,
        units=model.units,
        price_per_unit=model.price_per_unit,
        consideration=model.consideration,
        currency=model.currency,
        note=model.note,
        source=model.source,
        ingest_origin=model.ingest_origin,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class PositionTransactionRepository(BaseRepository):
    """Read and write ledger rows in the active tenant context."""

    async def add(
        self,
        *,
        investment_id: UUID,
        txn_type: str,
        trade_date: _date,
        units: Decimal,
        currency: str,
        ingest_origin: str,
        created_by: UUID,
        price_per_unit: Decimal | None = None,
        consideration: Decimal | None = None,
        note: str | None = None,
        source: str | None = None,
    ) -> PositionTransactionDTO:
        """Insert one ledger row.

        The raw persistence seam for the service-layer write path (Excel
        opening synthesis in strand S4, web transaction entry in strand
        S5). Currency equality and the non-negativity invariant are the
        service's responsibility (ADR-0097 §4/§5); the DB CHECKs enforce
        the sign rules, the price rules, and the ``ingest_origin`` /
        ``txn_type`` closed sets, and ``uq_position_transactions_opening``
        rejects a second ``opening`` per investment.

        Args:
            investment_id: The investment this transaction belongs to.
            txn_type: One of ``opening`` / ``buy`` / ``sell`` / ``transfer``.
            trade_date: Statement-day date of the event.
            units: Signed unit quantity (sign rules CHECK-enforced per
                ``txn_type``).
            currency: ISO 4217 currency code; must equal the investment's
                currency (validated in the service layer, not here).
            ingest_origin: Producer — ``'excel'`` (synthesised opening) or
                ``'manual'`` (web entry). ``'live'`` is reserved for a
                future execution layer (ADR-0097 §2).
            created_by: UUID of the user attributable for the write.
            price_per_unit: Optional per-unit trade price; required by CHECK
                for ``buy``/``sell``, may be ``None`` for
                ``opening``/``transfer``.
            consideration: Optional signed cash effect.
            note: Optional free-text note.
            source: Optional free-text provenance.

        Returns:
            The newly created :class:`PositionTransactionDTO`.

        Raises:
            sqlalchemy.exc.IntegrityError: If a CHECK, the opening partial
                unique index, or an FK is violated.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = PositionTransaction(
            tenant_id=active_tenant,
            investment_id=investment_id,
            txn_type=txn_type,
            trade_date=trade_date,
            units=units,
            price_per_unit=price_per_unit,
            consideration=consideration,
            currency=currency,
            note=note,
            source=source,
            ingest_origin=ingest_origin,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def list_for_investment(self, investment_id: UUID) -> list[PositionTransactionDTO]:
        """Return every ledger row for one investment, in canonical order.

        Rows are ordered by the ADR-0097 §2 total tiebreak
        ``(trade_date, created_at, id)`` so holdings derivation and
        materialisation (strand S2) consume them without re-sorting.

        Args:
            investment_id: The investment whose ledger to load.

        Returns:
            All ledger rows for the investment in the active tenant
            context, ordered ``(trade_date, created_at, id)``. Empty list
            for an unknown investment or one with no transactions.
        """
        result = await self._session.execute(
            select(PositionTransaction)
            .where(PositionTransaction.investment_id == investment_id)
            .order_by(
                PositionTransaction.trade_date.asc(),
                PositionTransaction.created_at.asc(),
                PositionTransaction.id.asc(),
            )
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def get_opening(self, investment_id: UUID) -> PositionTransactionDTO | None:
        """Return the investment's ``opening`` transaction, if any.

        At most one ``opening`` exists per investment (the partial unique
        index ``uq_position_transactions_opening``), so this returns a
        single row or ``None``. The Excel re-import reconciliation (strand
        S4) uses it to decide insert-vs-update against the opening.

        Args:
            investment_id: The investment to query.

        Returns:
            The single ``opening`` row, or ``None`` if the ledger has no
            opening yet.
        """
        result = await self._session.execute(
            select(PositionTransaction).where(
                PositionTransaction.investment_id == investment_id,
                PositionTransaction.txn_type == "opening",
            )
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_by_id(self, transaction_id: UUID) -> PositionTransactionDTO | None:
        """Return one ledger row by id.

        Args:
            transaction_id: The transaction to load.

        Returns:
            The :class:`PositionTransactionDTO`, or ``None`` if no row with
            this id exists in the active tenant context. A foreign-tenant id
            is invisible under RLS and therefore reads as absence — which is
            what lets the web layer render it as ``404``, never ``403``.
        """
        result = await self._session.execute(
            select(PositionTransaction).where(PositionTransaction.id == transaction_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def update(
        self,
        transaction_id: UUID,
        *,
        trade_date: _date,
        units: Decimal,
        price_per_unit: Decimal | None = None,
        consideration: Decimal | None = None,
        note: str | None = None,
        source: str | None = None,
    ) -> PositionTransactionDTO | None:
        """Update the mutable fields of one ledger row (web edit, strand S5).

        The general-purpose sibling of :meth:`update_opening`, which stays
        the narrow entry point for the Excel reconcile (it restates only what
        a units row can restate). This method serves the operator edit path
        and touches every field the transaction form exposes.

        ``txn_type`` is **immutable** here, alongside ``ingest_origin``,
        ``currency``, and ``created_by``. Retyping a row would silently move
        it in and out of the one-``opening``-per-investment partial unique
        index; the honest operation is a delete plus a create, which the web
        surface offers. ``currency`` is immutable because ADR-0097 §5 pins it
        equal to the investment's currency — there is nothing to choose.

        The sign and price rules (ADR-0097 §2) are CHECK-enforced and so
        apply to this write exactly as to :meth:`add`. The non-negativity
        invariant (ADR-0097 §4) is the service's responsibility, as it is for
        :meth:`add`: only the service can see the rest of the ledger.

        Args:
            transaction_id: The transaction to update.
            trade_date: Restated statement-day date.
            units: Restated signed unit quantity.
            price_per_unit: Restated per-unit trade price, or ``None``.
            consideration: Restated signed cash effect, or ``None``.
            note: Restated free-text note, or ``None``.
            source: Restated free-text provenance, or ``None``.

        Returns:
            The updated :class:`PositionTransactionDTO`, or ``None`` if no
            row with this id exists in the active tenant context.
        """
        result = await self._session.execute(
            select(PositionTransaction).where(PositionTransaction.id == transaction_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        model.trade_date = trade_date
        model.units = units
        model.price_per_unit = price_per_unit
        model.consideration = consideration
        model.note = note
        model.source = source
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def update_opening(
        self,
        transaction_id: UUID,
        *,
        units: Decimal,
        trade_date: _date,
        price_per_unit: Decimal | None = None,
    ) -> PositionTransactionDTO | None:
        """Update an existing ``opening`` row in place (Excel re-import).

        The Excel book-of-record reconciliation (strand S4, ADR-0097 §7)
        updates the single ``excel``-origin opening in place rather than
        duplicating it: the partial unique index
        ``uq_position_transactions_opening`` forbids a second opening, and
        an in-place update preserves the row's identity and audit lineage.
        Only the fields a restated units row can change are touched —
        ``units``, ``trade_date``, and the always-``NULL`` ``price_per_unit``
        of a synthesised opening; ``ingest_origin``, ``currency``, and
        ``created_by`` are immutable here. The non-negativity invariant
        (ADR-0097 §4) is the service's responsibility, exactly as for
        :meth:`add`.

        Args:
            transaction_id: The opening row to update.
            units: The restated positive unit count.
            trade_date: The restated units-as-of / opening date.
            price_per_unit: Kept ``None`` for an Excel-synthesised opening
                (the day-one price derives at materialisation, ADR-0098).

        Returns:
            The updated :class:`PositionTransactionDTO`, or ``None`` if no
            row with this id exists in the active tenant context.
        """
        result = await self._session.execute(
            select(PositionTransaction).where(PositionTransaction.id == transaction_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        model.units = units
        model.trade_date = trade_date
        model.price_per_unit = price_per_unit
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def delete(self, transaction_id: UUID) -> bool:
        """Hard-delete one ledger row.

        Args:
            transaction_id: The transaction to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no transaction with
            this id existed in the active tenant context.
        """
        result = await self._session.execute(
            delete(PositionTransaction).where(PositionTransaction.id == transaction_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
