# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRepository — persistence for the per-tenant investment catalogue.

Backs the ``investments`` table introduced in migration b006 (per
ADR-0043 §1). The shape mirrors the Phase-3 SAA repositories: a
tenant-scoped :class:`AsyncSession` is passed in, methods return
frozen DTOs, ``tenant_id`` is implicit in the session context (RLS
WITH CHECK derives it from ``app.tenant_id``).

Phase-4 modelling is repository-flavoured: this module deliberately
does not expose ORM ``relationship()`` traversals to NAVs or
cashflows. Cross-table reads are orchestrated in
``services/investments/investment_service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text, update

from core.models.investment import Investment
from core.models.investment_bond_analytics import InvestmentBondAnalytics
from core.models.investment_country_weight import InvestmentCountryWeight
from core.models.investment_maturity_weight import InvestmentMaturityWeight
from core.models.investment_rating_weight import InvestmentRatingWeight
from core.models.investment_region_weight import InvestmentRegionWeight
from core.models.investment_sector_weight import InvestmentSectorWeight
from core.repositories.base import BaseRepository

#: The analytics / weight children of an ``investments`` row, table name
#: first, in the order :meth:`InvestmentRepository.analytics_children_with_rows`
#: reports them.
#:
#: Every one is ``ON DELETE CASCADE``, so none of them *prevents* a delete —
#: which is exactly why the question has to be asked in application code. The
#: caller (trade-ticket reversal, ADR-0128 §6) needs to know whether deleting
#: an investment would silently take somebody's classification work with it.
#: The ledger, cashflow and NAV children are absent from this list on
#: purpose: those have repositories of their own, and the NAV probe needs an
#: ``ingest_origin`` predicate no generic emptiness check could carry.
_ANALYTICS_CHILDREN: tuple[tuple[str, Any], ...] = (
    ("investment_region_weights", InvestmentRegionWeight),
    ("investment_sector_weights", InvestmentSectorWeight),
    ("investment_country_weights", InvestmentCountryWeight),
    ("investment_rating_weight", InvestmentRatingWeight),
    ("investment_maturity_weight", InvestmentMaturityWeight),
    ("investment_bond_analytics", InvestmentBondAnalytics),
)


@dataclass(frozen=True)
class InvestmentDTO:
    """Plain data-only view of an ``investments`` row."""

    id: UUID
    tenant_id: UUID
    name: str
    investment_type: str
    asset_class_id: UUID
    manager_name: str | None
    region: str | None
    currency: str
    vintage_year: int | None
    commitment_amount: Decimal | None
    is_active: bool
    type_specific_data: dict | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    # Phase-7 AnlV classification (ADR-0057). Optional field at the
    # end of the dataclass so existing keyword-arg constructions in
    # tests and pre-Phase-7 callers continue to work without churn.
    anlv_code: str | None = None
    # Position-model write-path discriminator (ADR-0097 §1, column b024):
    # 'reported' (NAV carried directly in investment_navs) or 'unitised'
    # (NAV materialised from holdings × price, ADR-0098). Read-only here —
    # the operator flip is strand S5. Optional with a 'reported' default so
    # existing keyword-arg constructions stay valid (the DB backfilled every
    # row to 'reported'); the computed-NAV materialisation service reads it
    # to enforce its reported-mode no-op.
    valuation_mode: str = "reported"


def _to_dto(model: Investment) -> InvestmentDTO:
    return InvestmentDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        name=model.name,
        investment_type=model.investment_type,
        asset_class_id=model.asset_class_id,
        manager_name=model.manager_name,
        region=model.region,
        currency=model.currency,
        vintage_year=model.vintage_year,
        commitment_amount=model.commitment_amount,
        is_active=model.is_active,
        type_specific_data=model.type_specific_data,
        anlv_code=model.anlv_code,
        valuation_mode=model.valuation_mode,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InvestmentRepository(BaseRepository):
    """Read and write investments in the active tenant context."""

    async def list_all(self) -> list[InvestmentDTO]:
        """Return every investment visible in the active tenant context.

        Returns:
            All investments (active and inactive), sorted by ``name``
            for stable rendering in the CRUD list view.
        """
        result = await self._session.execute(select(Investment).order_by(Investment.name))
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_active(self) -> list[InvestmentDTO]:
        """Return only investments where ``is_active = TRUE``.

        Returns:
            All active investments in the active tenant, sorted by
            ``name``. Soft-deleted investments (``is_active = FALSE``)
            are filtered out.
        """
        result = await self._session.execute(
            select(Investment).where(Investment.is_active.is_(True)).order_by(Investment.name)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def get_by_id(self, investment_id: UUID) -> InvestmentDTO | None:
        """Return the investment with the given id, or ``None`` if absent.

        Cross-tenant rows are invisible (RLS hides them); the
        repository correctly reports absence rather than raising.

        Args:
            investment_id: The investment to look up.

        Returns:
            The matching :class:`InvestmentDTO`, or ``None`` if no
            investment with this id exists in the active tenant.
        """
        result = await self._session.execute(
            select(Investment).where(Investment.id == investment_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_by_name(self, name: str) -> InvestmentDTO | None:
        """Return the investment matching ``name`` in the current tenant.

        This method is the natural-key resolution path the Excel-import
        workflow (sub-stream 4c) uses: investment identity across
        re-imports is resolved on ``(tenant_id, name)``.

        Args:
            name: The investment name to look up. Tenant-unique by the
                ``uq_investments_tenant_name`` constraint.

        Returns:
            The matching :class:`InvestmentDTO`, or ``None`` if no
            investment with this name exists in the active tenant.
        """
        result = await self._session.execute(select(Investment).where(Investment.name == name))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_by_type(self, investment_type: str) -> list[InvestmentDTO]:
        """Return every investment of a given ``investment_type``.

        Args:
            investment_type: One of the eight CHECK-allowed values
                (``private_equity``, ``private_debt``, ``real_estate``,
                ``infra_equity``, ``listed_equity``, ``listed_bonds``,
                ``other``, ``cash``).

        Returns:
            All matching investments (active and inactive) in the
            active tenant, sorted by ``name``.
        """
        result = await self._session.execute(
            select(Investment)
            .where(Investment.investment_type == investment_type)
            .order_by(Investment.name)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def create(
        self,
        name: str,
        investment_type: str,
        asset_class_id: UUID,
        currency: str,
        created_by: UUID,
        *,
        manager_name: str | None = None,
        region: str | None = None,
        vintage_year: int | None = None,
        commitment_amount: Decimal | None = None,
        is_active: bool = True,
        type_specific_data: dict | None = None,
        anlv_code: str | None = None,
        valuation_mode: str = "reported",
    ) -> InvestmentDTO:
        """Create a new investment in the current tenant context.

        ``tenant_id`` is read from ``app.tenant_id`` so the session
        context is the single source of truth for tenant binding;
        RLS WITH CHECK re-validates the value as defence in depth
        (per ADR-0035 §6).

        Args:
            name: Tenant-unique investment name (the natural key for
                Excel-import re-import resolution).
            investment_type: One of the eight CHECK-allowed
                discriminator values.
            asset_class_id: 1:1 FK to the per-tenant asset-class
                catalogue.
            currency: ISO 4217 currency code (free-form text in
                Phase 4; no stammtabelle).
            created_by: UUID of the user creating the investment.
                Stored on the row for audit purposes.
            manager_name: Optional fund-manager / GP name.
            region: Optional geographic region label.
            vintage_year: Optional integer vintage year.
            commitment_amount: Optional total commitment amount.
            is_active: Active flag. Defaults to ``TRUE``; the Excel-
                import soft-delete path overrides this when an
                investment is missing from a fresh upload.
            type_specific_data: Optional Phase-5+ extension JSONB.
                Should remain ``None`` in Phase 4.
            anlv_code: Optional AnlV-category code (FK to
                ``anlv_categories.code``). ``None`` represents the
                "AnlV unallocated" engine-fallback case (ADR-0057).
            valuation_mode: ``'reported'`` (default) or ``'unitised'``.
                Creation is the *only* place a row may start out
                unitised: the mode change on an existing row is a
                deliberate, one-way operator act that goes through
                :meth:`set_valuation_mode` (ADR-0097 §6), and the import
                never flips. The one caller passing ``'unitised'`` is the
                Cash-sheet import, which creates a cash position already
                in its permanent mode (ADR-0103 §3) rather than creating
                it ``'reported'`` and immediately flipping it.

        Returns:
            The newly created :class:`InvestmentDTO`.

        Raises:
            sqlalchemy.exc.IntegrityError: If ``name`` collides with
                an existing investment in the same tenant, the
                investment type fails the CHECK constraint, or the
                FK to ``asset_classes`` does not resolve.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = Investment(
            tenant_id=active_tenant,
            name=name,
            investment_type=investment_type,
            asset_class_id=asset_class_id,
            manager_name=manager_name,
            region=region,
            currency=currency,
            vintage_year=vintage_year,
            commitment_amount=commitment_amount,
            is_active=is_active,
            type_specific_data=type_specific_data,
            anlv_code=anlv_code,
            valuation_mode=valuation_mode,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def update(
        self,
        investment_id: UUID,
        *,
        name: str | None = None,
        investment_type: str | None = None,
        asset_class_id: UUID | None = None,
        manager_name: str | None = None,
        region: str | None = None,
        currency: str | None = None,
        vintage_year: int | None = None,
        commitment_amount: Decimal | None = None,
        type_specific_data: dict | None = None,
        anlv_code: str | None = None,
    ) -> InvestmentDTO | None:
        """Update mutable fields on an investment.

        Only fields whose argument is not ``None`` are modified.
        ``is_active`` is intentionally not updatable here — toggling
        active state goes through :meth:`set_active` so the soft-
        delete-with-reactivation workflow has a single named entry
        point. ``created_by`` is also immutable (the original creator
        is part of the audit story).

        Args:
            investment_id: The investment to update.
            name: New name (keeps the existing value if ``None``).
            investment_type: New type discriminator.
            asset_class_id: New asset-class FK.
            manager_name: New manager-name label.
            region: New region label.
            currency: New currency code.
            vintage_year: New vintage year.
            commitment_amount: New commitment amount.
            type_specific_data: New JSONB payload.

        Returns:
            The refreshed :class:`InvestmentDTO`, or ``None`` if no
            investment with this id exists in the active tenant
            context.
        """
        values: dict[str, object] = {}
        if name is not None:
            values["name"] = name
        if investment_type is not None:
            values["investment_type"] = investment_type
        if asset_class_id is not None:
            values["asset_class_id"] = asset_class_id
        if manager_name is not None:
            values["manager_name"] = manager_name
        if region is not None:
            values["region"] = region
        if currency is not None:
            values["currency"] = currency
        if vintage_year is not None:
            values["vintage_year"] = vintage_year
        if commitment_amount is not None:
            values["commitment_amount"] = commitment_amount
        if type_specific_data is not None:
            values["type_specific_data"] = type_specific_data
        if anlv_code is not None:
            values["anlv_code"] = anlv_code
        if values:
            values["updated_at"] = text("NOW()")
            await self._session.execute(
                update(Investment).where(Investment.id == investment_id).values(**values)
            )
            await self._session.flush()

        return await self.get_by_id(investment_id)

    async def set_active(self, investment_id: UUID, is_active: bool) -> None:
        """Toggle the soft-delete flag on an investment.

        Used by the Excel-import workflow (sub-stream 4c) for the
        soft-delete-with-reactivation pattern:

        - Investments missing from a fresh Excel upload are set to
          ``is_active = FALSE``.
        - Investments reappearing in a subsequent upload are set
          back to ``is_active = TRUE``.

        Args:
            investment_id: The investment to update.
            is_active: New value for the active flag.
        """
        await self._session.execute(
            update(Investment)
            .where(Investment.id == investment_id)
            .values(is_active=is_active, updated_at=text("NOW()"))
        )
        await self._session.flush()

    async def set_valuation_mode(self, investment_id: UUID, valuation_mode: str) -> None:
        """Set the valuation-mode discriminator on an investment.

        Like :meth:`set_active`, and for the same reason, this is a named
        entry point rather than a field on :meth:`update`: the transition
        carries a workflow that a generic field write would let callers skip.
        Per ADR-0097 §6 the flip to ``'unitised'`` is a **one-way** operator
        act with preconditions, and it must be accompanied by the live-row
        cleanup and initial materialisation.

        This method is the mechanism only. The one-way rule and the
        preconditions are **policy**, enforced in
        :meth:`services.investments.InvestmentService.flip_to_unitised`,
        which is the single sanctioned caller — the split keeps the
        repository reusable for the future cash ADR (which unitises cash as
        the degenerate ``price ≡ 1.0000`` case) without that ADR having to
        relax a rule baked into the repository.

        Args:
            investment_id: The investment to update.
            valuation_mode: ``'reported'`` or ``'unitised'``. Values outside
                the pair are rejected by the ``ck_investments_valuation_mode``
                CHECK constraint.
        """
        await self._session.execute(
            update(Investment)
            .where(Investment.id == investment_id)
            .values(valuation_mode=valuation_mode, updated_at=text("NOW()"))
        )
        await self._session.flush()

    async def analytics_children_with_rows(self, investment_id: UUID) -> tuple[str, ...]:
        """Return the analytics / weight child tables that still hold rows.

        A read-only emptiness probe over the six classification children of
        an ``investments`` row — region, sector and country weights, the two
        fixed-income weight tables, and the bond-analytics characteristics.
        All six are ``ON DELETE CASCADE``, so a delete would take them
        silently; a caller that wants to know *before* deciding has to ask,
        and this is where the list of tables to ask about lives so that it is
        stated once rather than assembled at each call site.

        The sole caller is the trade-ticket reversal's shell clean-up
        (:func:`services.transactions.emission.cleanup_new_investment_shell`,
        ADR-0128 §6), which deletes an investment a booking created only when
        nothing but platform artefacts is left on it. Classification work is
        not a platform artefact — somebody typed it — so any row here retains
        the shell instead.

        One statement rather than six: each child contributes an ``EXISTS``
        subquery, and RLS scopes every one of them to the active tenant like
        any other read.

        Args:
            investment_id: The investment whose children to probe.

        Returns:
            The table names that hold at least one row for this investment,
            in :data:`_ANALYTICS_CHILDREN` order. Empty when all six are.
        """
        stmt = select(
            *[
                select(model.id).where(model.investment_id == investment_id).exists().label(table)
                for table, model in _ANALYTICS_CHILDREN
            ]
        )
        row = (await self._session.execute(stmt)).one()
        return tuple(
            table for (table, _), present in zip(_ANALYTICS_CHILDREN, row, strict=True) if present
        )

    async def delete(self, investment_id: UUID) -> bool:
        """Hard-delete an investment.

        ``investment_navs.investment_id`` and
        ``investment_cashflows.investment_id`` carry ``ON DELETE
        CASCADE``, so child NAV and cashflow rows disappear
        automatically. Asset classes referenced by the investment
        are *not* deleted — they remain in the catalogue for use by
        other investments.

        Args:
            investment_id: The investment to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no investment
            with this id existed in the active tenant context.
        """
        result = await self._session.execute(
            delete(Investment).where(Investment.id == investment_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
