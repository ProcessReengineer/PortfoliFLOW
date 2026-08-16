# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentIdentifierRepository — persistence for security identifiers.

Backs the ``investment_identifiers`` table introduced in migration
b020 (per ADR-0090). Shape mirrors the other tenant-scoped
repositories: a tenant-scoped :class:`AsyncSession` is passed in,
methods return frozen DTOs, and ``tenant_id`` is implicit in the
session context (RLS WITH CHECK derives it from ``app.tenant_id`` —
the repository never filters on ``tenant_id`` manually).

Scope is deliberately narrow (ADR-0090 §Decision, YAGNI): create,
list-per-investment, lookup-by-(scheme, value), and delete. FIGI
resolution, OpenFIGI calls, the market-linked predicate, and
``is_primary`` reassignment are **not** here — they belong to later
Live-Data-Import slices in the service layer (ADR-0091 for OpenFIGI /
``services/market_data/``).

Normalisation is an application concern (ADR-0090 §Decision): identifier
values are trimmed and upper-cased on write, and the same normalisation
is applied to lookup arguments so a query matches stored rows. The DB
CHECK only guards non-emptiness; an empty-after-trim value is rejected
here with the standard :class:`ValidationError` before it reaches the
database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, text, update

from core.exceptions import ValidationError
from core.models.investment_identifier import InvestmentIdentifier
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class InvestmentIdentifierDTO:
    """Plain data-only view of an ``investment_identifiers`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    scheme: str
    value: str
    is_primary: bool
    source: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: InvestmentIdentifier) -> InvestmentIdentifierDTO:
    return InvestmentIdentifierDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        scheme=model.scheme,
        value=model.value,
        is_primary=model.is_primary,
        source=model.source,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _normalise_value(value: str) -> str:
    """Normalise an identifier value: trim then upper-case.

    Normalisation is an application concern per ADR-0090 §Decision. The
    same transform is applied on write and on lookup so a query matches
    what was stored.

    Args:
        value: The raw identifier value as supplied by the caller.

    Returns:
        The trimmed, upper-cased value.

    Raises:
        ValidationError: If the value is empty after trimming.
    """
    normalised = value.strip().upper()
    if not normalised:
        raise ValidationError(
            "Identifier value must be non-empty after trimming.",
            field="value",
        )
    return normalised


class InvestmentIdentifierRepository(BaseRepository):
    """Read and write investment identifiers in the active tenant context."""

    async def add(
        self,
        *,
        investment_id: UUID,
        scheme: str,
        value: str,
        created_by: UUID,
        is_primary: bool = False,
        source: str | None = None,
    ) -> InvestmentIdentifierDTO:
        """Create one identifier row for an investment.

        ``value`` is normalised (trimmed + upper-cased) before insert;
        an empty-after-trim value is rejected with
        :class:`ValidationError`. ``tenant_id`` is read from
        ``app.tenant_id`` so the session context is the single source of
        truth for tenant binding (RLS WITH CHECK re-validates it).

        Args:
            investment_id: The investment this identifier belongs to.
            scheme: One of the CHECK-allowed schemes (``isin``,
                ``ticker``, ``figi``, ``cusip``, ``internal``). Invalid
                schemes are rejected by the DB CHECK.
            value: The identifier value; normalised on write.
            created_by: UUID of the user attributable for the write.
            is_primary: Whether this is the investment's primary
                identifier. At most one primary per investment is
                enforced by a partial unique index.
            source: Optional free-text provenance (``'excel'``,
                ``'openfigi'``, ``'manual'``).

        Returns:
            The newly created :class:`InvestmentIdentifierDTO`.

        Raises:
            ValidationError: If ``value`` is empty after trimming.
            sqlalchemy.exc.IntegrityError: If a uniqueness rule or the
                ``scheme`` CHECK is violated, or an FK does not resolve.
        """
        normalised_value = _normalise_value(value)

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = InvestmentIdentifier(
            tenant_id=active_tenant,
            investment_id=investment_id,
            scheme=scheme,
            value=normalised_value,
            is_primary=is_primary,
            source=source,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def list_for_investment(self, investment_id: UUID) -> list[InvestmentIdentifierDTO]:
        """Return every identifier row for one investment.

        Args:
            investment_id: The investment whose identifiers to load.

        Returns:
            All identifier rows for the investment in the active tenant
            context, sorted by ``(scheme, value)`` for stable rendering.
            Empty list for an unknown investment or one with no
            identifiers (the illiquid-instrument case).
        """
        result = await self._session.execute(
            select(InvestmentIdentifier)
            .where(InvestmentIdentifier.investment_id == investment_id)
            .order_by(
                InvestmentIdentifier.scheme,
                InvestmentIdentifier.value,
            )
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def get_by_scheme_value(self, scheme: str, value: str) -> InvestmentIdentifierDTO | None:
        """Return the identifier matching ``(scheme, value)`` in the tenant.

        The lookup ``value`` is normalised the same way as on write so it
        matches stored rows. Tenant scoping is implicit in the session
        context (RLS); no manual ``tenant_id`` filter is applied.

        For every real-world scheme (``isin``/``ticker``/``figi``/
        ``cusip``) the partial ``UNIQUE (tenant_id, scheme, value)`` index
        guarantees at most one match per tenant, so this returns a single
        row or ``None`` — the dedup / resolution path relies on that. The
        ``internal`` scheme is a free namespace not covered by that
        index; callers must not use this method to look up ``internal``
        values that may be shared across investments.

        Args:
            scheme: The identifier scheme to match.
            value: The identifier value; normalised before matching.

        Returns:
            The matching :class:`InvestmentIdentifierDTO`, or ``None`` if
            no identifier with this ``(scheme, value)`` exists in the
            active tenant.

        Raises:
            ValidationError: If ``value`` is empty after trimming.
        """
        normalised_value = _normalise_value(value)
        result = await self._session.execute(
            select(InvestmentIdentifier).where(
                InvestmentIdentifier.scheme == scheme,
                InvestmentIdentifier.value == normalised_value,
            )
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def delete(self, identifier_id: UUID) -> bool:
        """Delete one identifier row.

        Args:
            identifier_id: The identifier row to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no identifier with
            this id existed in the active tenant context.
        """
        result = await self._session.execute(
            delete(InvestmentIdentifier).where(InvestmentIdentifier.id == identifier_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0

    async def set_primary(self, identifier_id: UUID, *, is_primary: bool = True) -> bool:
        """Set or clear one identifier row's ``is_primary`` flag.

        Sets ``is_primary`` to the given value on the row (and bumps
        ``updated_at``). The caller is responsible for the
        one-primary-per-investment discipline — the partial unique index
        ``uq_investment_identifiers_primary_per_investment`` rejects a
        second ``is_primary = TRUE`` row for the same investment.

        Two callers exercise this:

        - the Excel-import reconciliation promotes a row (``is_primary``
          default ``True``) only when the investment currently lacks any
          primary (ADR-0090 §"Identifiers enter through both import
          paths");
        - the identifier CRUD surface (ADR-0096) re-primes a chosen row
          by first **demoting** the current primary
          (``is_primary=False``) and then promoting the target within one
          transaction, so the partial unique index sees a single ``TRUE``
          row at every point.

        Args:
            identifier_id: The identifier row to update.
            is_primary: The flag value to write — ``True`` promotes,
                ``False`` demotes. Defaults to ``True`` so existing
                promotion callers are unaffected.

        Returns:
            ``True`` if a row was updated, ``False`` if no identifier
            with this id existed in the active tenant context.
        """
        result = await self._session.execute(
            update(InvestmentIdentifier)
            .where(InvestmentIdentifier.id == identifier_id)
            .values(is_primary=is_primary, updated_at=text("NOW()"))
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
