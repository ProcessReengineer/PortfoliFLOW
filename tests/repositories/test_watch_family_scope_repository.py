# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""WatchFamilyScopeRepository tests against the live compose Postgres.

The ``watch_family_scope`` table (migration b036, ADR-0133 §2) holds one
switch per watchpoint family per tenant, historised on the ADR-0116 §1
revision model: immutable revisions keyed ``effective_from``, the audit
trigger attached.

Coverage
--------
* WS-01: ``effective_revisions`` returns, per family, the latest
  ``effective_from <= as_of``; a future-dated revision is not yet in
  force; a family without a revision is absent; the order follows
  ``WATCHPOINT_FAMILIES``.
* WS-02: obligation 7 (repository half) — a second revision leaves the
  first unchanged, both are listed.
* WS-03: an equal or earlier ``effective_from`` for the same family is
  refused; another family at the same instant succeeds.
* WS-04: validation — unknown family, ``pacing``, naive
  ``effective_from``, preset ``custom``, a preset on ``rss`` — raises
  :class:`WatchFamilyScopeInvalid` with the field and persists nothing.
* WS-05: ``list_revisions`` rejects an unknown family filter and filters
  by family.
* WS-06: ``changed_by`` round-trips; tenant B sees none of tenant A's
  revisions (RLS).
* WS-07: the audit trigger records the insert with the acting user.
* WS-08: the public methods are exactly the insert and the two reads.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import WatchFamilyScopeInvalid
from core.repositories import UserRepository, WatchFamilyScopeRepository, tenant_context

_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
_HOUR = timedelta(hours=1)


async def _seed_user(app_engine: AsyncEngine, tenant_id: UUID, label: str = "owner") -> UUID:
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email=f"{label}-{tenant_id}@example.test", password_hash="x" * 8
        )
    return user.id


# ---------------------------------------------------------------------------
# WS-01: the effective read
# ---------------------------------------------------------------------------


async def test_ws01_effective_revisions(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WS-01")
    actor = await _seed_user(app_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = WatchFamilyScopeRepository(session)
        # Inserted out of vocabulary order, so the returned order is the
        # repository's, not the insertion's or the alphabet's.
        await repo.insert_revision(
            family="liquidity", effective_from=_T0, enabled=False, changed_by=actor
        )
        await repo.insert_revision(family="fx", effective_from=_T0, enabled=False, changed_by=actor)
        await repo.insert_revision(
            family="saa", effective_from=_T0 + _HOUR, enabled=False, changed_by=actor
        )
        await repo.insert_revision(
            family="saa", effective_from=_T0 + 3 * _HOUR, enabled=True, changed_by=actor
        )
        # Future-dated relative to every as_of below.
        await repo.insert_revision(
            family="anlv", effective_from=_T0 + 10 * _HOUR, enabled=False, changed_by=actor
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = WatchFamilyScopeRepository(session)
        at_two = await repo.effective_revisions(_T0 + 2 * _HOUR)
        at_four = await repo.effective_revisions(_T0 + 4 * _HOUR)
        before_any = await repo.effective_revisions(_T0 - _HOUR)

    assert [(row.family, row.enabled) for row in at_two] == [
        ("saa", False),
        ("fx", False),
        ("liquidity", False),
    ]
    assert [(row.family, row.enabled) for row in at_four] == [
        ("saa", True),
        ("fx", False),
        ("liquidity", False),
    ]
    # anlv is future-dated, every other family has no revision: absent.
    assert before_any == []


async def test_ws01b_effective_revisions_rejects_naive_as_of(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WS-01b")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchFamilyScopeInvalid) as excinfo:
            await WatchFamilyScopeRepository(session).effective_revisions(_T0.replace(tzinfo=None))
    assert excinfo.value.field == "as_of"


# ---------------------------------------------------------------------------
# WS-02 / WS-03: immutability and the advancing rule
# ---------------------------------------------------------------------------


async def test_ws02_second_revision_leaves_the_first_unchanged(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WS-02")
    actor = await _seed_user(app_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id) as session:
        first = await WatchFamilyScopeRepository(session).insert_revision(
            family="price",
            effective_from=_T0,
            enabled=True,
            changed_by=actor,
            preset="quiet",
            note="initial",
        )
    async with tenant_context(app_engine, tenant_id) as session:
        second = await WatchFamilyScopeRepository(session).insert_revision(
            family="price", effective_from=_T0 + _HOUR, enabled=False, changed_by=actor
        )

    async with tenant_context(app_engine, tenant_id) as session:
        revisions = await WatchFamilyScopeRepository(session).list_revisions()

    assert revisions == [first, second]
    assert revisions[0].enabled is True
    assert revisions[0].preset == "quiet"
    assert revisions[0].note == "initial"


@pytest.mark.parametrize("offset", [timedelta(0), -_HOUR], ids=["equal", "earlier"])
async def test_ws03_same_family_must_advance(
    app_engine: AsyncEngine, seed_tenant, offset: timedelta
) -> None:
    tenant_id = await seed_tenant("WS-03")
    actor = await _seed_user(app_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = WatchFamilyScopeRepository(session)
        await repo.insert_revision(
            family="saa", effective_from=_T0, enabled=False, changed_by=actor
        )
        with pytest.raises(WatchFamilyScopeInvalid) as excinfo:
            await repo.insert_revision(
                family="saa", effective_from=_T0 + offset, enabled=True, changed_by=actor
            )
        assert excinfo.value.field == "effective_from"

        # Another family may share the instant — a preset applies per family.
        other = await repo.insert_revision(
            family="anlv", effective_from=_T0, enabled=True, changed_by=actor, preset="nervous"
        )
        assert other.effective_from == _T0


# ---------------------------------------------------------------------------
# WS-04 / WS-05: validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"family": "nav"}, "family", id="unknown-family"),
        pytest.param({"family": "pacing"}, "family", id="pacing"),
        pytest.param({"effective_from": _T0.replace(tzinfo=None)}, "effective_from", id="naive"),
        pytest.param({"preset": "custom"}, "preset", id="custom-preset"),
        pytest.param({"family": "rss", "preset": "standard"}, "preset", id="rss-with-preset"),
    ],
)
async def test_ws04_insert_validates_and_persists_nothing(
    app_engine: AsyncEngine, seed_tenant, overrides: dict[str, object], field: str
) -> None:
    tenant_id = await seed_tenant("WS-04")
    actor = await _seed_user(app_engine, tenant_id)
    arguments: dict[str, object] = {
        "family": "saa",
        "effective_from": _T0,
        "enabled": False,
        "changed_by": actor,
        **overrides,
    }

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(WatchFamilyScopeInvalid) as excinfo:
            await WatchFamilyScopeRepository(session).insert_revision(**arguments)  # type: ignore[arg-type]
    assert excinfo.value.field == field

    async with tenant_context(app_engine, tenant_id) as session:
        assert await WatchFamilyScopeRepository(session).list_revisions() == []


async def test_ws05_list_revisions_family_filter(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WS-05")
    actor = await _seed_user(app_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = WatchFamilyScopeRepository(session)
        await repo.insert_revision(family="fx", effective_from=_T0, enabled=False, changed_by=actor)
        await repo.insert_revision(
            family="saa", effective_from=_T0, enabled=False, changed_by=actor
        )
        await repo.insert_revision(
            family="fx", effective_from=_T0 + _HOUR, enabled=True, changed_by=actor
        )

        everything = await repo.list_revisions()
        fx_only = await repo.list_revisions(family="fx")
        with pytest.raises(WatchFamilyScopeInvalid) as excinfo:
            await repo.list_revisions(family="pacing")

    # Oldest first; at a shared instant, vocabulary order (saa before fx).
    assert [(row.family, row.effective_from) for row in everything] == [
        ("saa", _T0),
        ("fx", _T0),
        ("fx", _T0 + _HOUR),
    ]
    assert [row.effective_from for row in fx_only] == [_T0, _T0 + _HOUR]
    assert excinfo.value.field == "family"


# ---------------------------------------------------------------------------
# WS-06: changed_by and RLS
# ---------------------------------------------------------------------------


async def test_ws06_changed_by_round_trip_and_cross_tenant_invisibility(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    actor = await _seed_user(app_engine, tenant_a)

    async with tenant_context(app_engine, tenant_a) as session:
        created = await WatchFamilyScopeRepository(session).insert_revision(
            family="freshness", effective_from=_T0, enabled=False, changed_by=actor
        )
    assert created.changed_by == actor
    assert created.tenant_id == tenant_a

    async with tenant_context(app_engine, tenant_b) as session:
        repo = WatchFamilyScopeRepository(session)
        assert await repo.list_revisions() == []
        assert await repo.effective_revisions(_T0 + _HOUR) == []


# ---------------------------------------------------------------------------
# WS-07: the audit trigger fires with the actor
# ---------------------------------------------------------------------------


async def test_ws07_audit_trigger_records_the_actor(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WS-07")
    actor = await _seed_user(app_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=actor) as session:
        created = await WatchFamilyScopeRepository(session).insert_revision(
            family="liquidity", effective_from=_T0, enabled=False, changed_by=actor
        )

    async with superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT operation, user_id, record_id FROM audit_log "
                    "WHERE table_name = 'watch_family_scope' AND tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).all()

    assert [(row.operation, row.user_id, row.record_id) for row in rows] == [
        ("INSERT", actor, created.id)
    ]


# ---------------------------------------------------------------------------
# WS-08: public surface
# ---------------------------------------------------------------------------


def test_ws08_public_surface_is_insert_and_reads_only() -> None:
    public = {
        name
        for name, member in inspect.getmembers(WatchFamilyScopeRepository, inspect.isfunction)
        if not name.startswith("_") and name in vars(WatchFamilyScopeRepository)
    }
    assert public == {"insert_revision", "effective_revisions", "list_revisions"}, (
        "Scope revisions are immutable (ADR-0133 obligation 7): the repository "
        f"inserts and reads, never updates. Found {sorted(public)}."
    )
