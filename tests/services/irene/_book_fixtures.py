# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared book-seeding helpers for the Irene delta / beat tests.

A two-investment universe — one classified equity plus an explicit cash
position — with NAVs and both SAA and AnlV limit sets, enough to drive
:func:`services.irene.internal_delta.evaluate_internal_deltas` to a
known coverage status at the latest Stichtag. Shared by
``test_internal_delta.py`` (delta layer) and ``test_beat.py`` (beat
end-to-end) so the two exercise the identical book.

The leading underscore keeps pytest from collecting this as a test
module — it is a helper, mirroring
``tests/services/analytics/_reference_loader.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.repositories import (
    AssetClassRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    tenant_context,
)
from services.ai_service_core import SynthesisResult
from services.watch_desk.overlay import WatchDeskResolution, resolve_watch_desk

# A month-end window > 12 months wide so the coverage service's default
# 12-month grid is fully covered by carry-forward from the anchor.
ANCHOR_DATE = date(2023, 5, 31)
LATEST_DATE = date(2024, 6, 30)

#: The book total. Since ADR-0103 §2 this is not a ``portfolio_aum`` row but a
#: property of the book: the equity NAV plus an explicit cash position sized to
#: top it up. Holding the float rather than asserting it keeps every coverage
#: percentage below identical to the pre-ADR-0103 fixture — which is the whole
#: claim of the ADR, expressed as a fixture.
AUM_EUR = Decimal("1000000")

#: Name of the cash position that carries the book's float.
CASH_NAME = "Cash EUR"

# Benign latest NAV (10% coverage → OK) vs a breaching one (60% → BREACH).
CALM_NAV = Decimal("100000")
BREACH_NAV = Decimal("600000")

SAA_SUBJECT = "saa:equities"
ANLV_SUBJECT = "anlv:anlv_1"


def D(value: str | int) -> Decimal:
    return Decimal(str(value))


class SurfacingCore:
    """Duck-typed AI core that surfaces every subject it is handed.

    Shared by every beat-level signal test (ADR-0116 §4). The urgency it
    suggests is a test parameter precisely because the deterministic floor
    is what the tests are about: a persisted urgency above the suggestion
    can only have come from the floor, and one below it only from a cap.

    Attributes:
        calls: Every ``run_synthesis`` invocation, so a test can inspect
            the context the model was actually shown.
    """

    def __init__(self, *subject_keys: str, urgency_suggestion: int = 1) -> None:
        """Store the subjects to surface and the urgency to suggest for each."""
        self._subject_keys = subject_keys
        self._urgency = urgency_suggestion
        self.calls: list[dict[str, Any]] = []

    def get_system_prompt(self, prompt_name: str = "irene") -> str:
        """Return a stand-in system prompt."""
        return "You are Irene."

    async def run_synthesis(self, **kwargs: Any) -> SynthesisResult:
        """Surface one finding per configured subject key."""
        self.calls.append(kwargs)
        return SynthesisResult(
            tool_calls=[
                {
                    "name": "surface_finding",
                    "arguments": {
                        "subject_key": subject_key,
                        "trigger": "watchpoint moved",
                        "finding": "The watchpoint moved.",
                        "basis": "See the beat context.",
                        "urgency_suggestion": self._urgency,
                    },
                }
                for subject_key in self._subject_keys
            ],
            raw_text="",
        )


async def resolution(session: AsyncSession) -> WatchDeskResolution:
    """Resolve the tenant's calibration exactly as the beat does.

    The delta layer takes its calibration as one resolved argument since
    ADR-0116 §5 (there is deliberately no default — a default would be the
    second resolution path the ADR forbids). Tests resolve through the real
    function rather than hand-building a resolution, so they exercise the
    composition and the overlay read the beat runs on.
    """
    return await resolve_watch_desk(session, as_of=datetime.now(timezone.utc))


async def seed_user(superuser_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    """Insert an owner user for the tenant (a superuser path)."""
    user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:uid, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)"
            ),
            {
                "uid": str(user_id),
                "tid": str(tenant_id),
                "email": f"u-{user_id}@example.com",
                "hash": "$2b$04$placeholder_hash_for_service_tests_only",
            },
        )
    return user_id


async def seed_book(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    latest_nav: Decimal,
) -> UUID:
    """Seed the equity + cash book; return the **equity** investment id.

    NAVs are anchored at :data:`ANCHOR_DATE` (a benign level that carries
    forward across the whole default grid) plus :data:`LATEST_DATE` (the
    Stichtag the delta inspects), whose NAV is ``latest_nav``. Both SAA and
    AnlV limit sets are seeded so the coverage engine can evaluate both
    families.

    The cash position holds ``AUM_EUR − equity NAV`` at each date, so the
    denominator is :data:`AUM_EUR` and the equity's coverage is the same
    percentage the retired ``portfolio_aum`` row used to produce. Cash sits in
    its own asset class, deliberately *absent* from the SAA set (a NO_LIMIT
    row) and with no ``anlv_code`` (the AnlV unallocated bucket) — neither is
    a constrained class, so Irene does not watch it and the findings under
    test stay exactly the equity's.
    """
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ac = await AssetClassRepository(session).create(code="equities", display_name="Equities")
        inv = await InvestmentRepository(session).create(
            name="Alpha",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=user_id,
        )

        cash_ac = await AssetClassRepository(session).create(code="cash", display_name="Cash")
        cash = await InvestmentRepository(session).create(
            name=CASH_NAME,
            investment_type="cash",
            asset_class_id=cash_ac.id,
            currency="EUR",
            created_by=user_id,
        )

        nav_repo = InvestmentNavRepository(session)
        for as_of, value in (
            (ANCHOR_DATE, CALM_NAV),
            (LATEST_DATE, latest_nav),
        ):
            await nav_repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source=None,
                created_by=user_id,
            )
            await nav_repo.upsert(
                investment_id=cash.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=AUM_EUR - value,
                currency="EUR",
                source=None,
                created_by=user_id,
            )

        limits_repo = LimitsRepository(session)
        await limits_repo.create_set_with_limits(
            family="saa",
            effective_from=date(2020, 1, 1),
            label="SAA test",
            notes=None,
            limits={"equities": D("50.0")},
            created_by=user_id,
        )
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV test",
            notes=None,
            limits={"anlv_1": D("50.0")},
            created_by=user_id,
        )
        return inv.id


async def set_latest_nav(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    investment_id: UUID,
    value: Decimal,
) -> None:
    """Overwrite the latest-Stichtag NAV to move the coverage status.

    The cash position absorbs the change, so the book total stays
    :data:`AUM_EUR` and only the *equity's* share of it moves. Without this
    the denominator would move with the numerator (ADR-0103 §2: the book is
    the denominator) and the coverage percentage would barely budge — the
    fixture would silently stop testing what it claims to.
    """
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        nav_repo = InvestmentNavRepository(session)
        await nav_repo.upsert(
            investment_id=investment_id,
            as_of_date=LATEST_DATE,
            nav_kind="actual",
            nav_value=value,
            currency="EUR",
            source=None,
            created_by=user_id,
        )
        cash = next(
            inv
            for inv in await InvestmentRepository(session).list_active()
            if inv.name == CASH_NAME
        )
        await nav_repo.upsert(
            investment_id=cash.id,
            as_of_date=LATEST_DATE,
            nav_kind="actual",
            nav_value=AUM_EUR - value,
            currency="EUR",
            source=None,
            created_by=user_id,
        )
