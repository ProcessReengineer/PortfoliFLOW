# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""System lines — templates, the NAV diff and the writers (ADR-0138 §7, P-CS-2b).

Runs against the test container on the Irene book fixture (one equity
"Alpha" at :data:`CALM_NAV` of a :data:`AUM_EUR` book, SAA ceiling 50 %).

Coverage
--------
* SL-01: the four template texts, exact, and their payloads (D-CS-B-20).
* SL-02: ``nav_changes`` — new, changed, unchanged, deleted, order, scale.
* SL-03: the ``subject_within_ceiling`` writer — targets, waiting stays
  waiting, dedup.
* SL-04: the ``nav_restated`` writer — linked cases only, dedup, closed
  cases skipped.
* SL-05: the beat seam — a falling edge writes one line; a calm beat none.

SL-06 (the NAV routes), SL-07 (the import) and SL-08 (D-CS-M-10, the
booking) sit beside their seams' own tests.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.repositories import (
    CaseInvestmentRepository,
    CaseRepository,
    IreneFindingRepository,
    tenant_context,
)
from core.repositories.case_repository import CaseDTO
from services.ai_service_core import ResolvedLLM
from services.cases import (
    EVENT_NAV_RESTATED,
    EVENT_SUBJECT_WITHIN_CEILING,
    nav_changes,
    nav_restated,
    subject_within_ceiling,
)
from services.cases.system_lines import (
    limit_within_ceiling_line,
    nav_restated_line,
    signal_within_threshold_line,
)
from services.irene.beat import run_beat
from tests.services.irene._book_fixtures import (
    ANLV_SUBJECT,
    BREACH_NAV,
    CALM_NAV,
    SAA_SUBJECT,
    SurfacingCore,
    seed_book,
    seed_user,
    set_latest_nav,
)

_LLM = ResolvedLLM(base_url="https://openrouter.test/api/v1", api_key="sk-sl", model="test-model")
_AS_OF = date(2026, 11, 2)
_T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _finding_case(
    session: AsyncSession,
    user_id: UUID,
    subject_key: str,
    *,
    resolution: str = "opened_case",
    title: str | None = None,
) -> CaseDTO:
    """Open a case from a finding on ``subject_key``, resolved as given."""
    finding = await IreneFindingRepository(session).append(
        subject_key=subject_key, payload={"trigger": "x"}, urgency=5, band="critical"
    )
    if resolution != "open":
        await IreneFindingRepository(session).resolve(
            finding_id=finding.id, resolution=resolution, resolved_by=user_id, resolved_at=_T0
        )
    return await CaseRepository(session).create(
        title=title or f"Case on {subject_key}",
        opened_by=user_id,
        finding_id=finding.id,
        opened_actor="pm",
        now=_T0,
    )


async def _manual_case(session: AsyncSession, user_id: UUID, title: str = "Manual") -> CaseDTO:
    return await CaseRepository(session).create(
        title=title, opened_by=user_id, opened_actor="pm", now=_T0
    )


# ---------------------------------------------------------------------------
# SL-01: the templates
# ---------------------------------------------------------------------------


def test_sl01_templates_are_fixed_strings() -> None:
    finding_id = uuid4()
    subject, line, payload = limit_within_ceiling_line(
        subject_key="saa:equities",
        family="saa",
        status="OK",
        coverage_pct=Decimal("42.04"),
        max_pct=Decimal("50"),
        as_of=_AS_OF,
        finding_id=finding_id,
    )
    assert subject == "saa:equities"
    assert line == (
        "saa:equities is back within its ceiling: 42.0 % of the 50.0 % ceiling (OK) "
        "as of 2026-11-02."
    )
    assert payload == {
        "family": "saa",
        "status": "OK",
        "coverage_pct": "42.04",
        "max_pct": "50",
        "as_of": "2026-11-02",
        "finding_id": str(finding_id),
    }

    _, no_ceiling, payload = limit_within_ceiling_line(
        subject_key="anlv:16",
        family="anlv",
        status="WARN",
        coverage_pct=Decimal("7.25"),
        max_pct=None,
        as_of=_AS_OF,
        finding_id=None,
    )
    assert no_ceiling == "anlv:16 is back within its ceiling: 7.2 % (WARN) as of 2026-11-02."
    assert payload["max_pct"] is None and payload["finding_id"] is None

    subject, line, payload = signal_within_threshold_line(
        subject_key="fx:EUR/USD",
        family="fx",
        display_name="EUR/USD",
        status="Calm",
        as_of=_AS_OF,
        finding_id=finding_id,
        value=Decimal("1.5"),
        threshold=Decimal("5"),
        unit="pp",
    )
    assert subject == "fx:EUR/USD"
    assert line == "EUR/USD is back within its threshold (Calm) as of 2026-11-02."
    assert payload == {
        "family": "fx",
        "status": "Calm",
        "value": "1.5",
        "threshold": "5",
        "unit": "pp",
        "as_of": "2026-11-02",
        "finding_id": str(finding_id),
    }

    investment_id = uuid4()
    subject, line, payload = nav_restated_line(
        investment_id=investment_id,
        investment_name="Alpha",
        currency="EUR",
        as_of=_AS_OF,
        previous_value=None,
        nav_value=Decimal("1250000"),
    )
    assert subject == f"{investment_id}:2026-11-02"
    assert line == "NAV reported for Alpha: €1.2M as of 2026-11-02."
    assert payload == {
        "investment_id": str(investment_id),
        "investment_name": "Alpha",
        "as_of": "2026-11-02",
        "nav_value": "1250000",
        "previous_value": None,
        "currency": "EUR",
        "change": "new",
    }

    _, line, payload = nav_restated_line(
        investment_id=investment_id,
        investment_name="Alpha",
        currency="CHF",
        as_of=_AS_OF,
        previous_value=Decimal("1250000"),
        nav_value=Decimal("1300000.50"),
    )
    assert line == "NAV restated for Alpha as of 2026-11-02: CHF 1.2M → CHF 1.3M."
    assert (payload["previous_value"], payload["nav_value"], payload["change"]) == (
        "1250000",
        "1300000.50",
        "restated",
    )


# ---------------------------------------------------------------------------
# SL-02: nav_changes
# ---------------------------------------------------------------------------


def test_sl02_nav_changes() -> None:
    d1, d2, d3, d4 = (date(2026, m, 28) for m in (1, 2, 3, 4))
    before = {d1: Decimal("1.0"), d2: Decimal("2"), d3: Decimal("3")}
    after = {d4: Decimal("4"), d2: Decimal("2.5"), d1: Decimal("1.00")}
    # d1: scale only (1.0 == 1.00) → nothing; d2 changed; d3 deleted → nothing;
    # d4 new. Sorted by as-of.
    assert nav_changes(before, after) == [
        (d2, Decimal("2"), Decimal("2.5")),
        (d4, None, Decimal("4")),
    ]
    assert nav_changes(after, after) == []
    assert nav_changes({}, {}) == []
    assert nav_changes(before, {}) == []


# ---------------------------------------------------------------------------
# SL-03: the subject_within_ceiling writer
# ---------------------------------------------------------------------------


async def test_sl03_subject_within_ceiling_writer(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-03")
    user_id = await seed_user(superuser_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        open_case = await _finding_case(session, user_id, SAA_SUBJECT, title="open")
        waiting_case = await _finding_case(session, user_id, SAA_SUBJECT, title="waiting")
        await repo.wait(waiting_case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0)
        closed_case = await _finding_case(session, user_id, SAA_SUBJECT, title="closed")
        await repo.close(closed_case.id, closed_by=user_id, closing_note="done", now=_T0)
        other_case = await _finding_case(session, user_id, ANLV_SUBJECT, title="other")

        written = await subject_within_ceiling(
            session,
            subject_key=SAA_SUBJECT,
            family="saa",
            status="OK",
            as_of=_AS_OF,
            now=_T0 + timedelta(hours=1),
            coverage_pct=Decimal("10.0"),
            max_pct=Decimal("50.0"),
        )
        assert sorted(e.case_id for e in written) == sorted([open_case.id, waiting_case.id])
        expected = (
            "saa:equities is back within its ceiling: 10.0 % of the 50.0 % ceiling (OK) "
            "as of 2026-11-02."
        )
        for case in (open_case, waiting_case):
            (line,) = await repo.list_system_lines(case.id)
            assert (line.kind, line.actor, line.actor_user_id) == ("system_line", "system", None)
            assert line.payload["event"] == EVENT_SUBJECT_WITHIN_CEILING
            assert line.payload["subject"] == SAA_SUBJECT
            assert line.payload["text"] == expected
            assert line.payload["finding_id"] == str(case.finding_id)
            assert line.payload["coverage_pct"] == "10.0"
        assert await repo.list_system_lines(closed_case.id) == []
        assert await repo.list_system_lines(other_case.id) == []

        # The lift on an event is P-CS-2c's: the waiting case is still waiting.
        still = await repo.get(waiting_case.id)
        assert still is not None and still.state == "waiting"

        # Dedup: the same event and subject again writes nothing.
        again = await subject_within_ceiling(
            session,
            subject_key=SAA_SUBJECT,
            family="saa",
            status="OK",
            as_of=_AS_OF,
            now=_T0 + timedelta(hours=2),
            coverage_pct=Decimal("9.0"),
            max_pct=Decimal("50.0"),
        )
        assert again == []
        assert len(await repo.list_system_lines(open_case.id)) == 1


# ---------------------------------------------------------------------------
# SL-04: the nav_restated writer
# ---------------------------------------------------------------------------


async def test_sl04_nav_restated_writer(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-04")
    user_id = await seed_user(superuser_engine, tenant_id)
    equity_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)
    d_new, d_old = date(2024, 7, 31), date(2024, 6, 30)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        links = CaseInvestmentRepository(session)
        linking = await _manual_case(session, user_id, "linking")
        await links.link(linking.id, investment_id=equity_id, linked_by=user_id, now=_T0)
        unlinked = await _manual_case(session, user_id, "unlinked")
        closed = await _manual_case(session, user_id, "closed")
        await links.link(closed.id, investment_id=equity_id, linked_by=user_id, now=_T0)
        await repo.close(closed.id, closed_by=user_id, closing_note="done", now=_T0)

        changes = [
            (d_old, Decimal("100000"), Decimal("120000")),
            (d_new, None, Decimal("130000")),
        ]
        written = await nav_restated(session, investment_id=equity_id, changes=changes, now=_T0)
        assert len(written) == 2
        lines = {e.payload["subject"]: e.payload for e in await repo.list_system_lines(linking.id)}
        assert set(lines) == {f"{equity_id}:2024-06-30", f"{equity_id}:2024-07-31"}
        restated = lines[f"{equity_id}:2024-06-30"]
        assert restated["event"] == EVENT_NAV_RESTATED
        assert restated["text"] == "NAV restated for Alpha as of 2024-06-30: €100k → €120k."
        assert restated["change"] == "restated"
        reported = lines[f"{equity_id}:2024-07-31"]
        assert reported["text"] == "NAV reported for Alpha: €130k as of 2024-07-31."
        assert reported["change"] == "new"
        assert reported["currency"] == "EUR"
        assert await repo.list_system_lines(unlinked.id) == []
        assert await repo.list_system_lines(closed.id) == []

        # Dedup: the same investment and as-of is written once.
        again = await nav_restated(session, investment_id=equity_id, changes=changes, now=_T0)
        assert again == []
        assert len(await repo.list_system_lines(linking.id)) == 2


# ---------------------------------------------------------------------------
# SL-05: the beat seam
# ---------------------------------------------------------------------------


async def _entry_count(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT count(*) FROM case_entries"))).scalar_one()


async def test_sl05_beat_writes_the_falling_edge(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-05")
    user_id = await seed_user(superuser_engine, tenant_id)
    equity_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)
    core: Any = SurfacingCore()

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id, SAA_SUBJECT)
        manual = await _manual_case(session, user_id)

    # Beat 1: the breach rises — no line (a rising edge is no system line).
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        before = await _entry_count(session)
        result = await run_beat(session, core, llm=_LLM, now=_now())
        assert result.error is None
        assert await _entry_count(session) == before

    # Beat 2: back under the ceiling — the falling edge is one line.
    await set_latest_nav(app_engine, tenant_id, user_id, equity_id, CALM_NAV)
    beat_two = _now()
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        result = await run_beat(session, core, llm=_LLM, now=beat_two)
        assert result.error is None
        repo = CaseRepository(session)
        (line,) = await repo.list_system_lines(case.id)
        assert line.payload["event"] == EVENT_SUBJECT_WITHIN_CEILING
        assert line.payload["subject"] == SAA_SUBJECT
        assert line.payload["text"] == (
            "saa:equities is back within its ceiling: 10.0 % of the 50.0 % ceiling (OK) "
            f"as of {beat_two.date().isoformat()}."
        )
        assert line.payload["status"] == "OK"
        assert await repo.list_system_lines(manual.id) == []
        after_two = await _entry_count(session)

    # Beat 3: calm and unchanged — no edge, no line, no entry at all.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        result = await run_beat(session, core, llm=_LLM, now=_now())
        assert result.error is None
        assert await _entry_count(session) == after_two
