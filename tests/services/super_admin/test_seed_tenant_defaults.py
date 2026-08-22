# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-tenant seed-parity tests for ``seed_tenant_defaults`` (ADR-0077).

``portfoliflow create-tenant`` (and the super-admin web route) provision
a tenant and then call :func:`services.super_admin.seed_tenant_defaults`
for its default catalogue. ADR-0077 brings that routine to full parity
with :mod:`cli.bootstrap`: every provisioned tenant must end up with the
``unclassified`` asset class (the ADR-0043 Excel-import fallback) and the
Phase-7 default asset-class catalogue, exactly as the bootstrapped
primary tenant does.

These tests exercise that contract end-to-end against the live compose
Postgres, mirroring the engine the CLI uses (the superuser engine).

Coverage:

* STD-01: After ``seed_tenant_defaults``, the target tenant has the
  ``unclassified`` asset class and every code from the default
  asset-class fixture resolves.
* STD-02: Re-running ``seed_tenant_defaults`` is idempotent — no error,
  and no duplicate asset-class rows per code.
* STD-03 / STD-04: the (disabled) market-data schedule and the
  never-authenticating system actor (ADR-0093).
* STD-05: the (enabled) Irene schedule row (ADR-0119 §4), its computed
  ``next_due_at``, and its idempotency — including that a cadence the
  tenant already saved survives a re-seed untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.irene_schedule_repository import IreneScheduleRepository
from core.repositories.market_data_schedule_repository import (
    MarketDataScheduleRepository,
)
from services.investments.live_refresh import MARKET_DATA_SYSTEM_ACTOR_EMAIL
from services.password_hashing import hash_password
from services.super_admin import seed_tenant_defaults

# Repo root is parents[3] from tests/services/super_admin/<file>.
_DEFAULT_ASSET_CLASSES_FIXTURE_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "data_normalization"
    / "fixtures"
    / "default_asset_classes.json"
)

_UNCLASSIFIED_CODE: str = "unclassified"


def _expected_default_codes() -> list[str]:
    """Return the asset-class codes the Phase-7 fixture ships."""
    with _DEFAULT_ASSET_CLASSES_FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return [str(entry["code"]) for entry in payload]


async def _seed_tenant_and_owner(
    superuser_engine: AsyncEngine,
) -> tuple[UUID, UUID]:
    """Create a fresh tenant plus an owner user; return their ids.

    The owner is the actor attributed for the seed writes — the audit
    trigger on each seeded row records ``app.user_id``, so the user must
    exist to satisfy the FK.
    """
    tenant_id = uuid4()
    user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:tid, :name, :subdomain)"),
            {
                "tid": str(tenant_id),
                "name": "Seed-parity tenant",
                "subdomain": f"std-{tenant_id.hex[:12]}",
            },
        )
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:uid, :tid, :email, :hash, "
                "ARRAY['owner']::text[], TRUE)"
            ),
            {
                "uid": str(user_id),
                "tid": str(tenant_id),
                "email": f"owner-{user_id.hex[:12]}@example.com",
                "hash": hash_password("doesntmatter"),
            },
        )
    return tenant_id, user_id


async def _asset_class_codes(
    superuser_engine: AsyncEngine, tenant_id: UUID, user_id: UUID
) -> list[str]:
    """Return the codes of every asset class visible in the tenant."""
    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        rows = await AssetClassRepository(session).list_all()
    return [row.code for row in rows]


# ---------------------------------------------------------------------------
# STD-01: seed_tenant_defaults installs the full asset-class catalogue
# ---------------------------------------------------------------------------


async def test_std01_seed_installs_unclassified_and_default_catalogue(
    superuser_engine: AsyncEngine,
) -> None:
    tenant_id, user_id = await _seed_tenant_and_owner(superuser_engine)

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    # The unclassified fallback is resolvable via the same case-
    # insensitive path the Excel importer uses (ADR-0043).
    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        assert await repo.get_by_code(_UNCLASSIFIED_CODE) is not None, (
            "seed_tenant_defaults must install the 'unclassified' asset "
            "class so create-tenant tenants can import Excel data."
        )
        missing = [
            code for code in _expected_default_codes() if await repo.get_by_code(code) is None
        ]

    assert not missing, (
        "seed_tenant_defaults must install the full Phase-7 default "
        f"asset-class catalogue; missing codes: {missing!r}"
    )


# ---------------------------------------------------------------------------
# STD-02: re-running is idempotent (no error, no duplicate codes)
# ---------------------------------------------------------------------------


async def test_std02_seed_is_idempotent_on_asset_classes(
    superuser_engine: AsyncEngine,
) -> None:
    tenant_id, user_id = await _seed_tenant_and_owner(superuser_engine)

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)
    # A second run must not raise and must not duplicate any row.
    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    codes = await _asset_class_codes(superuser_engine, tenant_id, user_id)

    # Every expected code (the unclassified fallback plus the full
    # default catalogue) is present exactly once after two runs.
    for code in [_UNCLASSIFIED_CODE, *_expected_default_codes()]:
        assert codes.count(code) == 1, (
            f"asset class {code!r} appears {codes.count(code)} times after "
            "two seed runs; the installers must be idempotent on code."
        )


# ---------------------------------------------------------------------------
# STD-03: seed installs the (disabled) market-data schedule (ADR-0093 §1)
# ---------------------------------------------------------------------------


async def test_std03_seed_installs_disabled_market_data_schedule(
    superuser_engine: AsyncEngine,
) -> None:
    tenant_id, user_id = await _seed_tenant_and_owner(superuser_engine)

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)
    # A second run must not create a duplicate schedule row (idempotency).
    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        schedule = await MarketDataScheduleRepository(session).get_for_tenant()

    assert schedule is not None, "seed_tenant_defaults must install a market_data_schedule row."
    assert schedule.enabled is False, (
        "a freshly seeded tenant must not silently start fetching — the "
        "schedule lands disabled (ADR-0093)."
    )
    assert schedule.cadence == "every_15m", (
        "the seeded cadence is the finest the vocabulary offers so opting a "
        "tenant in is one checkbox, not a cadence decision (ADR-0125 §3)."
    )
    assert schedule.preferred_hour == 0, (
        "the anchor is inert at a sub-hourly cadence — the quarter-hour grid "
        "runs from the full hour — so 0 is the honest seed (ADR-0125 §3)."
    )


# ---------------------------------------------------------------------------
# STD-04: seed installs the never-authenticating system actor (ADR-0093 §0.1)
# ---------------------------------------------------------------------------


async def test_std04_seed_installs_inactive_system_actor(
    superuser_engine: AsyncEngine,
) -> None:
    tenant_id, user_id = await _seed_tenant_and_owner(superuser_engine)

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)
    # Idempotent: a second run must not create a duplicate actor.
    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        actor = await UserRepository(session).get_by_email(MARKET_DATA_SYSTEM_ACTOR_EMAIL)

    assert actor is not None, (
        "seed_tenant_defaults must install the market-data system actor so "
        "live writes have a created_by principal (ADR-0093 §0.1)."
    )
    assert actor.is_active is False, (
        "the system actor must be inactive — it can never authenticate."
    )


# ---------------------------------------------------------------------------
# STD-05: seed installs the (enabled) Irene schedule (ADR-0119 §4)
# ---------------------------------------------------------------------------


async def test_std05_seed_installs_enabled_irene_schedule(
    superuser_engine: AsyncEngine,
) -> None:
    """The Watch Desk gets a cadence out of the box, deliberately enabled.

    The asymmetry with STD-03's disabled market-data row is the decision
    under test: an enabled market-data schedule would fetch from external
    providers immediately, whereas the Irene domain sits behind the tick
    scheduler's credential gate and is skipped quietly until the tenant
    configures credentials.
    """
    tenant_id, user_id = await _seed_tenant_and_owner(superuser_engine)

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        schedule = await IreneScheduleRepository(session).get_for_tenant()

    assert schedule is not None, (
        "seed_tenant_defaults must install an irene_schedule row so a fresh "
        "tenant's Watch Desk has a cadence (ADR-0119 §4)."
    )
    assert schedule.cadence == "daily"
    assert schedule.preferred_hour == 8
    assert schedule.timezone == "Europe/Berlin"
    assert schedule.enabled is True, (
        "the Irene schedule seeds enabled — the credential gate, not a "
        "disabled flag, is what keeps a credential-less tenant quiet."
    )

    # next_due_at is *computed*, not a raw ``now`` placeholder: the row is
    # live, so a placeholder would make it instantly due and beat at an
    # arbitrary hour. It lands on the next 08:00 Europe/Berlin.
    local_due = schedule.next_due_at.astimezone(ZoneInfo("Europe/Berlin"))
    assert (local_due.hour, local_due.minute) == (8, 0)
    now = datetime.now(timezone.utc)
    assert schedule.next_due_at > now
    assert schedule.next_due_at - now <= timedelta(days=1)


async def test_std05_irene_schedule_seed_is_idempotent(
    superuser_engine: AsyncEngine,
) -> None:
    """A re-seed neither duplicates the row nor overwrites a saved cadence.

    Existing tenants receive the row through this same idempotent path
    (ADR-0119 §4) — no migration, no backfill script — so the no-op on an
    existing row is what protects a tenant that has already configured its
    own cadence from being reset to the default on the next seed run.
    """
    tenant_id, user_id = await _seed_tenant_and_owner(superuser_engine)

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    # The tenant then saves its own cadence, as the Watch Desk panel would.
    saved_due = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        await IreneScheduleRepository(session).upsert_tenant_schedule(
            cadence="every_6h",
            preferred_hour=15,
            timezone="Europe/Berlin",
            enabled=False,
            next_due_at=saved_due,
        )

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_id, actor_user_id=user_id)

    async with superuser_engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT COUNT(*) FROM irene_schedule WHERE tenant_id = :tid"),
                {"tid": str(tenant_id)},
            )
        ).scalar_one()
    assert count == 1, f"the Irene schedule seed duplicated the row ({count} rows after two runs)."

    async with tenant_context(superuser_engine, tenant_id, user_id=user_id) as session:
        schedule = await IreneScheduleRepository(session).get_for_tenant()

    assert schedule is not None
    assert schedule.cadence == "every_6h", (
        "a re-seed must not overwrite a cadence the tenant already saved."
    )
    assert schedule.preferred_hour == 15
    assert schedule.enabled is False
    assert schedule.next_due_at == saved_due
