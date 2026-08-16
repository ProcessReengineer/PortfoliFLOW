# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneFindingRepository tests against the live compose Postgres.

The ``irene_finding`` table is tenant-scoped (RLS-policed, per ADR-0085
/ ADR-0035) and append-only: a row is never mutated except to record
its resolution.

Coverage
--------
* IF-01: ``append`` roundtrip; ``list_open`` / ``list_journal`` read the
  finding back with the DTO fields intact and in the documented order.
* IF-02: after ``resolve`` the immutable history (``payload``,
  ``urgency``, ``band``, ``created_at``) is unchanged; only the
  resolution fields move, and the finding leaves the open feed.
* IF-03: ``resolve`` with a value outside the vocabulary raises the
  typed :class:`IreneResolutionInvalid`.
* IF-04: a finding written under tenant A is invisible from a session
  under tenant B (RLS smoke).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import IreneResolutionInvalid
from core.repositories import tenant_context
from core.repositories.irene_finding_repository import IreneFindingRepository


# ---------------------------------------------------------------------------
# IF-01: append roundtrip + feed ordering
# ---------------------------------------------------------------------------


async def test_if01_append_roundtrip_and_feed_order(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("IF-01")
    payload = {"trigger": "coverage_breach", "finding": "AnlV Nr. 16 over cap"}

    # Append in two separate transactions so created_at genuinely differs:
    # NOW() is the transaction-start timestamp, so two findings appended in
    # one transaction would tie on created_at and the newest-first order
    # would be ambiguous. In production each finding is born on its own beat.
    async with tenant_context(app_engine, tenant_id) as session:
        low = await IreneFindingRepository(session).append(
            subject_key="anlv:16",
            payload=payload,
            urgency=2,
            band="noteworthy",
        )
    async with tenant_context(app_engine, tenant_id) as session:
        high = await IreneFindingRepository(session).append(
            subject_key="saa:equity",
            payload={"trigger": "saa_drift"},
            urgency=5,
            band="critical",
        )

    assert low.resolution == "open"
    assert low.payload == payload
    assert low.resolved_at is None
    assert low.resolved_by is None

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneFindingRepository(session)
        # list_open is ordered urgency desc, then created_at desc.
        open_feed = await repo.list_open()
        assert [f.id for f in open_feed] == [high.id, low.id]

        # list_journal is the full history, newest first.
        journal = await repo.list_journal()
        assert [f.id for f in journal] == [high.id, low.id]


# ---------------------------------------------------------------------------
# IF-02: resolve preserves immutable history
# ---------------------------------------------------------------------------


async def test_if02_resolve_preserves_history(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("IF-02")
    payload = {"trigger": "coverage_breach", "options": ["rebalance"]}
    resolver = uuid4()
    resolved_at = datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneFindingRepository(session)
        created = await repo.append(
            subject_key="anlv:16",
            payload=payload,
            urgency=4,
            band="critical",
        )

        await repo.resolve(
            finding_id=created.id,
            resolution="acted",
            resolved_by=resolver,
            resolved_at=resolved_at,
        )

        # Re-read via raw SQL (gold standard: bypasses the ORM identity map).
        row = (
            (
                await session.execute(
                    text(
                        "SELECT payload, urgency, band, created_at, "
                        "resolution, resolved_at, resolved_by "
                        "FROM irene_finding WHERE id = :fid"
                    ),
                    {"fid": str(created.id)},
                )
            )
            .mappings()
            .one()
        )

        # Immutable history is unchanged.
        assert row["payload"] == payload
        assert row["urgency"] == 4
        assert row["band"] == "critical"
        assert row["created_at"] == created.created_at
        # Only the resolution fields moved.
        assert row["resolution"] == "acted"
        assert row["resolved_at"] == resolved_at
        assert row["resolved_by"] == resolver

        # A resolved finding leaves the open feed.
        assert await repo.list_open() == []


# ---------------------------------------------------------------------------
# IF-03: resolution vocabulary validation
# ---------------------------------------------------------------------------


async def test_if03_resolve_rejects_invalid_resolution(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("IF-03")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneFindingRepository(session)
        created = await repo.append(
            subject_key="anlv:16",
            payload={"trigger": "x"},
            urgency=1,
            band="informational",
        )

        with pytest.raises(IreneResolutionInvalid):
            await repo.resolve(
                finding_id=created.id,
                resolution="bogus",
                resolved_by=None,
                resolved_at=datetime(2026, 7, 3, 9, 30, tzinfo=timezone.utc),
            )


async def test_if03b_opened_case_is_the_fifth_valid_resolution(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The five-member vocabulary (ADR-0107, C4): ``opened_case`` round-trips.

    The fifth resolution is written by the case-opening composition, so the
    repository must accept it (and leave the immutable history untouched, like
    any other resolution).
    """
    tenant_id = await seed_tenant("IF-03b")
    resolver = uuid4()
    resolved_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneFindingRepository(session)
        created = await repo.append(
            subject_key="saa:private_equity",
            payload={"trigger": "PE near its SAA ceiling"},
            urgency=6,
            band="critical",
        )

        # opened_case is accepted (no IreneResolutionInvalid) and round-trips.
        await repo.resolve(
            finding_id=created.id,
            resolution="opened_case",
            resolved_by=resolver,
            resolved_at=resolved_at,
        )

        fetched = await repo.get(created.id)
        assert fetched is not None
        assert fetched.resolution == "opened_case"
        assert fetched.resolved_by == resolver
        assert fetched.resolved_at == resolved_at
        # Immutable history untouched.
        assert fetched.urgency == 6
        assert fetched.band == "critical"
        # A resolved (opened_case) finding leaves the open feed.
        assert await repo.list_open() == []


# ---------------------------------------------------------------------------
# IF-04: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_if04_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        await IreneFindingRepository(session).append(
            subject_key="anlv:16",
            payload={"trigger": "x"},
            urgency=3,
            band="noteworthy",
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = IreneFindingRepository(session)
        assert await repo.list_open() == []
        assert await repo.list_journal() == []
        count = await session.execute(text("SELECT count(*) FROM irene_finding"))
        assert count.scalar_one() == 0


# ---------------------------------------------------------------------------
# IF-05: count_since is inclusive and resolution-blind
# ---------------------------------------------------------------------------


async def test_if05_count_since_is_inclusive_and_counts_resolved(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``count_since`` backs the "surfaced N findings" tile (ADR-0089).

    It must count every finding created at or after the boundary — resolved
    ones included, since a finding the beat surfaced may already have been
    acted on — and must exclude anything created before it.
    """
    tenant_id = await seed_tenant("IF-05")

    # Three findings, each in its own transaction so created_at differs.
    ids = []
    for subject in ("saa:one", "saa:two", "saa:three"):
        async with tenant_context(app_engine, tenant_id) as session:
            created = await IreneFindingRepository(session).append(
                subject_key=subject,
                payload={"trigger": subject},
                urgency=3,
                band="noteworthy",
            )
            ids.append(created)

    first, second, third = ids

    # Resolve the newest — it must still be counted.
    async with tenant_context(app_engine, tenant_id) as session:
        await IreneFindingRepository(session).resolve(
            finding_id=third.id,
            resolution="acted",
            resolved_by=None,
            resolved_at=datetime.now(timezone.utc),
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneFindingRepository(session)
        # Inclusive lower bound: the boundary finding itself counts.
        assert await repo.count_since(since=first.created_at) == 3
        assert await repo.count_since(since=second.created_at) == 2
        # Resolved-but-recent is counted; open-only would say 1, not 2.
        assert len(await repo.list_open()) == 2
        # Nothing after the newest.
        assert await repo.count_since(since=third.created_at + timedelta(seconds=1)) == 0


# ---------------------------------------------------------------------------
# IF-06: get() by id — present, absent, cross-tenant absent (ADR-0107, C3a)
# ---------------------------------------------------------------------------


async def test_if06_get_present_absent_and_cross_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``get`` reads a finding back by id, and is RLS-scoped to the tenant.

    Backs the case origin embed (ADR-0107, C3a): a present id returns the DTO
    with its payload intact, an unknown id returns ``None``, and a finding
    written under tenant A is invisible (``None``) from a tenant-B session.
    """
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        created = await IreneFindingRepository(session).append(
            subject_key="anlv:17",
            payload={"trigger": "HY quota breached", "basis": "5.14% vs 5.00%"},
            urgency=8,
            band="critical",
        )

    # Present: same tenant reads the finding back with fields intact.
    async with tenant_context(app_engine, tenant_a) as session:
        repo = IreneFindingRepository(session)
        fetched = await repo.get(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.subject_key == "anlv:17"
        assert fetched.band == "critical"
        assert fetched.payload["basis"] == "5.14% vs 5.00%"
        # Absent: an unknown id returns None, not an error.
        assert await repo.get(uuid4()) is None

    # Cross-tenant: tenant B cannot see tenant A's finding by id (RLS).
    async with tenant_context(app_engine, tenant_b) as session:
        assert await IreneFindingRepository(session).get(created.id) is None
