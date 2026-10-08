# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""IreneBriefingRepository tests against the live compose Postgres.

The ``irene_briefing`` table (migration b036, ADR-0133 §1.1) is
tenant-scoped (RLS-policed), append-only and immutable: the repository
offers one validating ``append`` and three reads.

Coverage
--------
* IB-01: a written briefing round-trips every DTO field; ``finding_ids``
  come back as a tuple, ``inputs`` intact.
* IB-02: a skipped briefing round-trips, ``model_id`` ``None`` for a
  skip that made no call.
* IB-03: ``newest`` follows ``beat_at``, not insertion order.
* IB-04: ``list_between`` is half-open (``start <= beat_at < end``) and
  newest first.
* IB-05: ``list_between`` rejects ``end <= start`` and naive bounds.
* IB-06: every ``append`` shape violation raises
  :class:`IreneBriefingInvalid` with the right ``field`` and persists
  nothing.
* IB-07: ``get`` of an unknown id is ``None``; a tenant-A row is
  invisible to tenant B through ``newest``, ``get`` and ``list_between``.
* IB-08: obligation 5 — the public methods are exactly append and the
  four reads; there is no update and no delete.
* IB-09: ``list_before`` is exclusive on ``before``, capped, bounded by
  ``start``, and refuses a naive bound (the Journal's paged source).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import IreneBriefingInvalid
from core.repositories import IreneBriefingRepository, tenant_context

_BEAT = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
_DAY = timedelta(days=1)


def _written(beat_at: datetime) -> dict[str, object]:
    return {
        "beat_at": beat_at,
        "written_at": beat_at + timedelta(minutes=2),
        "status": "written",
        "inputs": {"families": ["saa"]},
        "finding_ids": [],
        "text_markets": "Markets were calm.",
        "text_watch": "Nothing on the watch.",
        "model_id": "irene-model",
    }


# ---------------------------------------------------------------------------
# IB-01 / IB-02: round trips
# ---------------------------------------------------------------------------


async def test_ib01_written_round_trip(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("IB-01")
    finding_ids = [uuid4(), uuid4()]
    inputs = {"families": ["saa", "fx"], "market": {"eurusd": 1.08}, "open": 2}

    async with tenant_context(app_engine, tenant_id) as session:
        created = await IreneBriefingRepository(session).append(
            beat_at=_BEAT,
            written_at=_BEAT + timedelta(minutes=3),
            status="written",
            inputs=inputs,
            finding_ids=finding_ids,
            text_markets="Rates rose.",
            text_watch="",
            model_id="irene-model",
        )

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await IreneBriefingRepository(session).get(created.id)

    assert fetched == created
    assert fetched.tenant_id == tenant_id
    assert fetched.beat_at == _BEAT
    assert fetched.written_at == _BEAT + timedelta(minutes=3)
    assert fetched.status == "written"
    assert fetched.skip_reason is None
    assert fetched.text_markets == "Rates rose."
    assert fetched.text_watch == ""
    assert fetched.inputs == inputs
    assert fetched.finding_ids == tuple(finding_ids)
    assert fetched.model_id == "irene-model"
    assert fetched.created_at is not None


async def test_ib02_skipped_round_trip(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("IB-02")

    async with tenant_context(app_engine, tenant_id) as session:
        created = await IreneBriefingRepository(session).append(
            beat_at=_BEAT,
            written_at=_BEAT,
            status="skipped",
            skip_reason="nothing_to_write",
            inputs={},
            finding_ids=[],
        )

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await IreneBriefingRepository(session).get(created.id)

    assert fetched is not None
    assert fetched.status == "skipped"
    assert fetched.skip_reason == "nothing_to_write"
    assert fetched.text_markets is None
    assert fetched.text_watch is None
    assert fetched.model_id is None
    assert fetched.finding_ids == ()
    assert fetched.inputs == {}


# ---------------------------------------------------------------------------
# IB-03 / IB-04 / IB-05: reads
# ---------------------------------------------------------------------------


async def test_ib03_newest_follows_beat_at(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("IB-03")

    # The later beat is appended first, in its own transaction.
    async with tenant_context(app_engine, tenant_id) as session:
        later = await IreneBriefingRepository(session).append(**_written(_BEAT + _DAY))
    async with tenant_context(app_engine, tenant_id) as session:
        await IreneBriefingRepository(session).append(**_written(_BEAT))

    async with tenant_context(app_engine, tenant_id) as session:
        newest = await IreneBriefingRepository(session).newest()

    assert newest is not None
    assert newest.id == later.id


async def test_ib04_list_between_is_half_open_and_newest_first(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("IB-04")
    beats = [_BEAT + n * _DAY for n in range(4)]

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneBriefingRepository(session)
        created = [await repo.append(**_written(beat)) for beat in beats]

    async with tenant_context(app_engine, tenant_id) as session:
        listed = await IreneBriefingRepository(session).list_between(start=beats[1], end=beats[3])

    # beats[1] included (inclusive start), beats[3] excluded (exclusive end).
    assert [row.id for row in listed] == [created[2].id, created[1].id]


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [
        pytest.param(_BEAT, _BEAT, "end", id="empty-range"),
        pytest.param(_BEAT + _DAY, _BEAT, "end", id="inverted-range"),
        pytest.param(_BEAT.replace(tzinfo=None), _BEAT + _DAY, "start", id="naive-start"),
        pytest.param(_BEAT, (_BEAT + _DAY).replace(tzinfo=None), "end", id="naive-end"),
    ],
)
async def test_ib05_list_between_rejects_bad_bounds(
    app_engine: AsyncEngine, seed_tenant, start: datetime, end: datetime, field: str
) -> None:
    tenant_id = await seed_tenant("IB-05")
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(IreneBriefingInvalid) as excinfo:
            await IreneBriefingRepository(session).list_between(start=start, end=end)
    assert excinfo.value.field == field


# ---------------------------------------------------------------------------
# IB-06: append validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"beat_at": _BEAT.replace(tzinfo=None)}, "beat_at", id="naive-beat-at"),
        pytest.param(
            {"written_at": _BEAT.replace(tzinfo=None)}, "written_at", id="naive-written-at"
        ),
        pytest.param({"status": "draft"}, "status", id="unknown-status"),
        pytest.param({"skip_reason": "llm_error"}, "skip_reason", id="written-with-reason"),
        pytest.param(
            {"status": "skipped", "text_markets": None, "text_watch": None},
            "skip_reason",
            id="skipped-without-reason",
        ),
        pytest.param(
            {
                "status": "skipped",
                "skip_reason": "timeout",
                "text_markets": None,
                "text_watch": None,
            },
            "skip_reason",
            id="unknown-reason",
        ),
        pytest.param({"text_markets": None}, "text_markets", id="written-missing-markets"),
        pytest.param({"text_watch": None}, "text_watch", id="written-missing-watch"),
        pytest.param(
            {"status": "skipped", "skip_reason": "llm_error", "text_markets": None},
            "text_watch",
            id="skipped-carrying-watch",
        ),
        pytest.param(
            {"status": "skipped", "skip_reason": "llm_error", "text_watch": None},
            "text_markets",
            id="skipped-carrying-markets",
        ),
    ],
)
async def test_ib06_append_validates_shape_and_persists_nothing(
    app_engine: AsyncEngine, seed_tenant, overrides: dict[str, object], field: str
) -> None:
    tenant_id = await seed_tenant("IB-06")
    arguments = {**_written(_BEAT), **overrides}

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(IreneBriefingInvalid) as excinfo:
            await IreneBriefingRepository(session).append(**arguments)  # type: ignore[arg-type]
    assert excinfo.value.field == field

    async with tenant_context(app_engine, tenant_id) as session:
        assert await IreneBriefingRepository(session).newest() is None


# ---------------------------------------------------------------------------
# IB-07: unknown id and RLS
# ---------------------------------------------------------------------------


async def test_ib07_unknown_id_and_cross_tenant_invisibility(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        repo = IreneBriefingRepository(session)
        created = await repo.append(**_written(_BEAT))
        assert await repo.get(uuid4()) is None

    async with tenant_context(app_engine, tenant_b) as session:
        repo = IreneBriefingRepository(session)
        assert await repo.newest() is None
        assert await repo.get(created.id) is None
        assert await repo.list_between(start=_BEAT - _DAY, end=_BEAT + _DAY) == []


# ---------------------------------------------------------------------------
# IB-08: obligation 5 — append and reads only
# ---------------------------------------------------------------------------


def test_ib08_public_surface_is_append_and_reads_only() -> None:
    public = {
        name
        for name, member in inspect.getmembers(IreneBriefingRepository, inspect.isfunction)
        if not name.startswith("_") and name in vars(IreneBriefingRepository)
    }
    assert public == {"append", "newest", "get", "list_between", "list_before"}, (
        "irene_briefing is append-only and immutable (ADR-0133 obligation 5): the "
        f"repository offers append and reads only. Found {sorted(public)}."
    )


# ---------------------------------------------------------------------------
# IB-09: list_before — the Journal's briefing source, paged by beat_at
# (ADR-0133 §5; P-WD-B14). Written with explicit keywords rather than
# ``**_written(...)``: that helper is typed ``dict[str, object]`` and every
# unpacking of it costs nine pyright errors (a pre-existing debt of this
# module, see the P-WD-B14 report).
# ---------------------------------------------------------------------------


async def test_ib09_list_before_is_exclusive_capped_and_bounded(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("IB-09")
    beats = [_BEAT + n * _DAY for n in range(4)]

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneBriefingRepository(session)
        created = [
            await repo.append(
                beat_at=beat,
                written_at=beat + timedelta(minutes=2),
                status="written",
                inputs={"families": ["saa"]},
                finding_ids=[],
                text_markets="Markets were calm.",
                text_watch="Nothing on the watch.",
                model_id="irene-model",
            )
            for beat in beats
        ]

    async with tenant_context(app_engine, tenant_id) as session:
        repo = IreneBriefingRepository(session)
        # Exclusive upper bound, newest first.
        page = await repo.list_before(before=beats[3], limit=10)
        assert [row.id for row in page] == [created[2].id, created[1].id, created[0].id]
        # The cap keeps the newest; the inclusive start bounds the period.
        assert [row.id for row in await repo.list_before(before=beats[3], limit=2)] == [
            created[2].id,
            created[1].id,
        ]
        bounded = await repo.list_before(before=beats[3], limit=10, start=beats[1])
        assert [row.id for row in bounded] == [created[2].id, created[1].id]
        with pytest.raises(IreneBriefingInvalid):
            await repo.list_before(before=beats[3].replace(tzinfo=None), limit=10)
