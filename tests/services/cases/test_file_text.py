# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The working file as text — Shirley's brief and ``get_case`` (ADR-0138 §10, P-CS-5a).

Runs against the test container. The cases are seeded through the
repositories; the book, where a test needs one, is the Irene book fixture
(one equity, ``Alpha``, at :data:`CALM_NAV`).

Coverage
--------
* FT-01: every section filled — the seven headings in ADR order, then Record.
* FT-02: empty sections are absent — a bare case is The question, Where we
  stand and the Record.
* FT-03: a waiting case reads *waiting until d*.
* FT-04: a closed case renders *At close* from the frozen snapshot, and
  ``StandContext.load`` is not called; the stand's headline, which repeats
  the first investment's NAV, is not a line of its own (P-CS-H1).
* FT-05: the bounds — a cut section says *… n more*, a long line is clipped
  to :data:`LINE_CHARS`, the whole text to :data:`MAX_CHARS`.
* FT-06: kept rows as caption lines — never a chart spec; an excerpt's
  passage; a generated document's title and first line.
* FT-07: a ``book`` figure carries the live value through ``book_values_for``.
* FT-08: names, never ids.
* FT-09: a stand that raises — in the composition, or on the database —
  reads *Where we stand: not available — …*, and the rest of the file follows
  (5a F-4, D-CS-B-120).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

import services.cases.file_text as file_text_module
import services.cases.stand as stand_module
from core.repositories import (
    CaseFigureRepository,
    CaseInvestmentRepository,
    CaseRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.case_attachment_repository import CaseAttachmentRepository
from core.repositories.case_repository import CaseDTO
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseDecisionRepository,
    CaseKeptRepository,
    CasePointRepository,
    CaseThreadRepository,
)
from services.cases import StandContext, book_values_for, compose, to_snapshot
from services.cases.file_text import (
    LINE_CHARS,
    MAX_CHARS,
    file_user_ids,
    render_case_file_text,
)
from services.money_format import format_money_compact
from tests.services.irene._book_fixtures import CALM_NAV, LATEST_DATE, seed_book

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_ALL_HEADINGS = [
    "The question",
    "Where we stand",
    "Threads",
    "Figures",
    "Conversations",
    "Open points",
    "Decision",
    "Record",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _headings(text: str) -> list[str]:
    """The section headings: the lines the composition does not indent."""
    return [line for line in text.splitlines() if line and not line.startswith(" ")]


async def _people(app_engine: AsyncEngine, seed_tenant, name: str) -> tuple[UUID, UUID, UUID]:
    """A tenant with two named users; return ``(tenant, pm, colleague)``."""
    tenant_id = await seed_tenant(name)
    slug = name.lower().replace(" ", "-")
    async with tenant_context(app_engine, tenant_id) as session:
        users = UserRepository(session)
        pm = await users.create(
            email=f"pm@{slug}.example",
            password_hash="x" * 8,
            roles=("owner",),
            display_name="S. Behrens",
        )
        colleague = await users.create(
            email=f"colleague@{slug}.example", password_hash="x" * 8, display_name="A. Keller"
        )
    return tenant_id, pm.id, colleague.id


async def _render(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    case_id: UUID,
    *,
    names: dict[UUID, str] | None = None,
) -> str:
    """Render the file as the callers do: the named users resolved, then the text."""
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await CaseRepository(session).get(case_id)
        assert case is not None
        if names is None:
            users = UserRepository(session)
            names = {}
            for uid in await file_user_ids(session, case):
                user = await users.get_by_id(uid)
                if user is not None:
                    names[uid] = user.display_name or user.email
        return await render_case_file_text(session, case, now=_now(), user_names=names)


async def _open(session: Any, pm: UUID, title: str = "Manual case", **kwargs: Any) -> CaseDTO:
    return await CaseRepository(session).create(
        title=title, opened_by=pm, opened_actor="pm", now=_now(), **kwargs
    )


# ---------------------------------------------------------------------------
# FT-01
# ---------------------------------------------------------------------------


async def test_ft01_every_section_renders_in_adr_order(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, pm, colleague = await _people(app_engine, seed_tenant, "FT 01")
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(
            session,
            pm,
            "Fund II continuation vehicle",
            description="Why now: the GP proposes a continuation vehicle.\nWhat is asked: roll or sell.",
            counterparty="Nordic Partners",
            due_on=date(2026, 12, 15),
        )
        threads = CaseThreadRepository(session)
        thread = await threads.add(
            case.id, title="Fee step-up", prose="The step-up applies from year three.", now=_now()
        )
        await threads.update(
            thread.id, now=_now(), status="confirmed", conclusion="Applies from year three"
        )
        await CaseKeptRepository(session).keep(
            case.id,
            artifact="consultation",
            payload={"excerpt": "Hedged at 80 % of NAV, page 23."},
            kept_by=pm,
            via="keep",
            thread_id=thread.id,
            caption="Hedged at 80 % of NAV",
            now=_now(),
        )
        await CaseFigureRepository(session).add(
            case.id,
            label="Management fee",
            source="typed",
            value_text="1.5 %",
            as_of="Q2 2026",
            now=_now(),
        )
        await CaseConversationRepository(session).add(
            case.id,
            held_on=date(2026, 10, 1),
            with_whom="Nordic Partners",
            what_was_said="They will extend the term.",
            what_was_promised="Q3 numbers by Friday",
            duration_minutes=20,
            now=_now(),
        )
        await CasePointRepository(session).add(
            case.id,
            text="Get the Q2 statement",
            owner_user_id=colleague,
            due_on=date(2026, 11, 15),
            now=_now(),
        )
        decisions = CaseDecisionRepository(session)
        await decisions.upsert_draft(
            case.id,
            now=_now(),
            options=[
                {"key": "a", "text": "Roll into the CV"},
                {"key": "b", "text": "Sell at the tender"},
            ],
            chosen_key="a",
            rationale="Better long-term value.",
            decided_by="IC",
        )
        await CaseRepository(session).record_decision(case.id, recorded_by=pm, now=_now())
        await decisions.upsert_draft(
            case.id, now=_now(), options=[{"key": "x", "text": "Revisit in Q1"}]
        )

    text = await _render(app_engine, tenant_id, pm, case.id)

    assert _headings(text) == _ALL_HEADINGS
    for expected in (
        "  CASE-0001 — Fund II continuation vehicle",
        "  State: open",
        "  Owner: S. Behrens · opened manually",
        "  Counterparty: Nordic Partners · Due: 15 Dec 2026",
        "  Why now: the GP proposes a continuation vehicle.",
        "  Fee step-up · Confirmed · conclusion: Applies from year three",
        "    The step-up applies from year three.",
        "    kept: consultation · Hedged at 80 % of NAV",
        "  Management fee · 1.5 % · typed · as of Q2 2026",
        "  01 Oct 2026 · Nordic Partners · Call · 20 min",
        "    said: They will extend the term.",
        "    promised: Q3 numbers by Friday",
        "  Get the Q2 statement · owner A. Keller · due 15 Nov 2026 · open",
        "    chosen: Roll into the CV",
        "    option: Sell at the tender",
        "    rationale: Better long-term value.",
        "  Draft",
        "    option: Revisit in Q1",
        "  Investments: none linked.",
    ):
        assert expected in text, expected
    record = text.split("\nRecord\n", 1)[1]
    assert "· Case opened · Opened manually." in record
    assert "· Decision record · Decision recorded: Roll into the CV — Better long-term value." in (
        record
    )
    assert len(text) <= MAX_CHARS


# ---------------------------------------------------------------------------
# FT-02
# ---------------------------------------------------------------------------


async def test_ft02_a_bare_case_renders_three_blocks(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 02")
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(session, pm, "Bare case")

    text = await _render(app_engine, tenant_id, pm, case.id)

    assert _headings(text) == ["The question", "Where we stand", "Record"]
    assert "  Investments: none linked." in text
    assert "  Tickets: none." in text
    assert "  Since opening: nothing happened on this case's subject." in text


# ---------------------------------------------------------------------------
# FT-03
# ---------------------------------------------------------------------------


async def test_ft03_a_waiting_case_reads_waiting_until(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 03")
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(session, pm, "Waits for the statement")
        await CaseRepository(session).wait(
            case.id, wait_until=date(2099, 1, 1), by=pm, now=_now(), note="the Q2 statement"
        )

    text = await _render(app_engine, tenant_id, pm, case.id)

    assert "  State: waiting until 01 Jan 2099" in text
    assert "Where we stand" in _headings(text)
    assert "· Waiting set · Waiting until 01 Jan 2099 — the Q2 statement" in text


# ---------------------------------------------------------------------------
# FT-04
# ---------------------------------------------------------------------------


async def test_ft04_a_closed_case_renders_at_close_without_composing(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 04")
    equity_id = await seed_book(app_engine, tenant_id, pm, latest_nav=CALM_NAV)
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        repo = CaseRepository(session)
        case = await _open(session, pm, "Sold at the tender")
        await CaseInvestmentRepository(session).link(
            case.id, investment_id=equity_id, linked_by=pm, now=_now()
        )
        figures = await CaseFigureRepository(session).list_for_case(case.id)
        ctx = await StandContext.load(session, now=_now())
        stand = await compose(ctx, case, figures=figures)
        await repo.close(
            case.id,
            closed_by=pm,
            closing_note="Sold at the tender; the CV was rejected.",
            now=_now(),
            stand=to_snapshot(stand),
            book_values=book_values_for(stand, figures),
        )
        unfrozen = await _open(session, pm, "Closed without a stand")
        await repo.close(unfrozen.id, closed_by=pm, closing_note="Dropped.", now=_now())

    calls = {"load": 0}
    real_load = stand_module.StandContext.load

    async def counting_load(session: Any, *, now: datetime) -> StandContext:
        calls["load"] += 1
        return await real_load(session, now=now)

    monkeypatch.setattr(stand_module.StandContext, "load", counting_load)

    text = await _render(app_engine, tenant_id, pm, case.id)
    other = await _render(app_engine, tenant_id, pm, unfrozen.id)

    assert calls["load"] == 0
    assert _headings(text) == ["The question", "At close", "Record"]
    assert "Where we stand" not in _headings(text)
    at_close = text.split("\nAt close\n", 1)[1].split("\n\n", 1)[0]
    assert at_close.splitlines()[0].startswith("  Frozen ")
    assert "  Investment: Alpha · EUR" in at_close
    assert f"NAV {format_money_compact(float(CALM_NAV), 'EUR')}" in at_close
    assert stand.headline and "Headline:" not in text  # the NAV once, on its investment
    assert "  State: closed" in text
    assert "  Closing note: Sold at the tender; the CV was rejected." in text
    assert "· Case closed · Sold at the tender; the CV was rejected." in text
    assert "  Where we stand was not frozen when this case closed." in other


# ---------------------------------------------------------------------------
# FT-05
# ---------------------------------------------------------------------------


async def test_ft05_bounds_cut_sections_lines_and_the_whole(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 05")
    long_line = "x" * 400
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        many = await _open(session, pm, "Thirteen threads")
        threads = CaseThreadRepository(session)
        for i in range(13):
            await threads.add(
                many.id, title=f"Thread {i:02d}", prose=long_line if i == 0 else "", now=_now()
            )
        huge = await _open(session, pm, "A very long file")
        paragraph = "\n".join(f"{i} " + "y" * 200 for i in range(6))
        for i in range(12):
            await threads.add(huge.id, title=f"Long {i:02d}", prose=paragraph, now=_now())

    text = await _render(app_engine, tenant_id, pm, many.id)

    titles = [line for line in text.splitlines() if line.startswith("  Thread ")]
    assert len(titles) == 12
    assert "  Thread 12 · Open" not in text
    threads_block = text.split("\nThreads\n", 1)[1].split("\n\n", 1)[0]
    assert threads_block.splitlines()[-1] == "  … 1 more"
    clipped = next(line for line in text.splitlines() if "xxxx" in line)
    assert len(clipped) == LINE_CHARS
    assert clipped.endswith("…")
    assert max(len(line) for line in text.splitlines()) <= LINE_CHARS

    cut = await _render(app_engine, tenant_id, pm, huge.id)
    assert len(cut) <= MAX_CHARS
    assert cut.endswith("\n… [file truncated]")
    assert "Record" not in _headings(cut)


# ---------------------------------------------------------------------------
# FT-06
# ---------------------------------------------------------------------------


async def test_ft06_kept_rows_render_as_caption_lines_only(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 06")
    spec = {"data": [{"type": "scatter", "name": "SPEC-SERIES-TOKEN"}], "layout": {}}
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(session, pm, "Kept things")
        thread = await CaseThreadRepository(session).add(case.id, title="The LPA terms", now=_now())
        attachment = await CaseAttachmentRepository(session).create(
            case.id,
            filename="lpa-2026.pdf",
            mime_type="application/pdf",
            size_bytes=13,
            sha256="SHA-TOKEN",
            content=b"BYTES-TOKEN",
            uploaded_by=pm,
            now=_now(),
        )
        kept = CaseKeptRepository(session)
        await kept.keep(
            case.id,
            artifact="chart_snapshot",
            payload={"caption": "NAV trajectory", "spec": spec},
            kept_by=pm,
            via="keep",
            thread_id=thread.id,
            caption="NAV trajectory",
            now=_now(),
        )
        await kept.keep(
            case.id,
            artifact="document_excerpt",
            payload={"filename": "lpa-2026.pdf", "passage": "The key-person clause.", "page": 12},
            kept_by=pm,
            via="keep",
            thread_id=thread.id,
            caption="Key person",
            attachment_id=attachment.id,
            now=_now(),
        )
        await kept.keep(
            case.id,
            artifact="generated_document",
            payload={
                "title": "Memo to the IC",
                "body": "The CV is priced at 92 % of NAV.\n\nSECOND-PARAGRAPH-TOKEN",
                "sources": [{"label": "SOURCE-TOKEN", "href": "/cases"}],
            },
            kept_by=pm,
            via="shirley",
            caption="Memo to the IC",
            now=_now(),
        )
        await kept.keep(
            case.id,
            artifact="document",
            payload={
                "filename": "lpa-2026.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 13,
                "sha256": "SHA-TOKEN",
                "attachment_id": str(attachment.id),
            },
            kept_by=pm,
            via="keep",
            caption="lpa-2026.pdf",
            attachment_id=attachment.id,
            now=_now(),
        )

    text = await _render(app_engine, tenant_id, pm, case.id)

    assert "    kept: chart snapshot · NAV trajectory" in text
    assert "    kept: document excerpt · Key person · “The key-person clause.”" in text
    assert "  Kept in the file" in text
    assert "    kept: generated document · Memo to the IC · The CV is priced at 92 % of NAV." in (
        text
    )
    assert "  Document: lpa-2026.pdf" in text
    for leaked in (
        "SPEC-SERIES-TOKEN",
        "scatter",
        "SECOND-PARAGRAPH-TOKEN",
        "SOURCE-TOKEN",
        "SHA-TOKEN",
        "BYTES-TOKEN",
        "application/pdf",
    ):
        assert leaked not in text, leaked


# ---------------------------------------------------------------------------
# FT-07
# ---------------------------------------------------------------------------


async def test_ft07_a_book_figure_reads_live(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 07")
    equity_id = await seed_book(app_engine, tenant_id, pm, latest_nav=CALM_NAV)
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(session, pm, "Book figure")
        await CaseFigureRepository(session).add(
            case.id,
            label="Alpha NAV",
            source="book",
            source_ref={"investment_id": str(equity_id), "metric": "nav"},
            note="read live",
            now=_now(),
        )

    text = await _render(app_engine, tenant_id, pm, case.id)

    nav = format_money_compact(float(CALM_NAV), "EUR")
    assert (
        f"  Alpha NAV · {nav} · book (Alpha · NAV) · as of {LATEST_DATE:%d %b %Y} · note: read live"
    ) in text


# ---------------------------------------------------------------------------
# FT-08
# ---------------------------------------------------------------------------


async def test_ft08_names_never_ids(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, pm, colleague = await _people(app_engine, seed_tenant, "FT 08")
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(session, pm, "Handed on")
        await CasePointRepository(session).add(
            case.id, text="Call the GP", owner_user_id=pm, now=_now()
        )
        await CaseRepository(session).hand_over(
            case.id, to_user_id=colleague, by=pm, now=_now() + timedelta(seconds=1)
        )
        closing = await _open(session, pm, "Closed by the PM")
        await CaseRepository(session).close(
            closing.id, closed_by=pm, closing_note="Done.", now=_now()
        )

    text = await _render(app_engine, tenant_id, pm, case.id)
    closed = await _render(app_engine, tenant_id, pm, closing.id)
    unnamed = await _render(app_engine, tenant_id, pm, case.id, names={})

    assert "  Owner: A. Keller · opened manually" in text
    assert "by S. Behrens" in text
    assert "  Call the GP · owner S. Behrens · open" in text
    assert "· Handed over · Handed over to A. Keller" in text
    assert "  State: closed" in closed and "by S. Behrens" in closed
    assert "  Owner: unknown user" in unnamed
    for body in (text, closed, unnamed):
        assert _UUID.search(body) is None, _UUID.search(body)
        assert str(pm) not in body and str(colleague) not in body


# ---------------------------------------------------------------------------
# FT-09
# ---------------------------------------------------------------------------


async def test_ft09_a_failing_stand_never_fails_the_text(
    app_engine: AsyncEngine, seed_tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, pm, _ = await _people(app_engine, seed_tenant, "FT 09")
    equity_id = await seed_book(app_engine, tenant_id, pm, latest_nav=CALM_NAV)
    async with tenant_context(app_engine, tenant_id, user_id=pm) as session:
        case = await _open(session, pm, "The stand is down")
        await CaseThreadRepository(session).add(case.id, title="Liquidity terms", now=_now())
        await CaseFigureRepository(session).add(
            case.id,
            label="Alpha NAV",
            source="book",
            source_ref={"investment_id": str(equity_id), "metric": "nav"},
            now=_now(),
        )
        await CasePointRepository(session).add(case.id, text="Call the GP", now=_now())

    async def raising_compose(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the Watch Desk did not answer\nsecond line")

    monkeypatch.setattr(file_text_module, "compose", raising_compose)
    text = await _render(app_engine, tenant_id, pm, case.id)

    assert "Where we stand: not available — the Watch Desk did not answer" in text
    assert "second line" not in text
    assert "\nWhere we stand\n" not in text
    for heading in ("The question", "Threads", "Figures", "Open points", "Record"):
        assert heading in _headings(text), heading
    # The book figure reads no live value without the stand, and says so.
    assert "  Alpha NAV · not available · book (Alpha · NAV)" in text

    # A database error inside the stand rolls back to its savepoint: the
    # investment read that follows still runs on the same transaction.
    monkeypatch.undo()

    async def failing_load(session: Any, *, now: datetime) -> StandContext:
        await session.execute(sql_text("SELECT * FROM no_such_table_ft09"))
        raise AssertionError("unreachable")

    monkeypatch.setattr(file_text_module.StandContext, "load", failing_load)
    on_the_database = await _render(app_engine, tenant_id, pm, case.id)

    assert "Where we stand: not available — " in on_the_database
    assert "  Alpha NAV · not available · book (Alpha · NAV)" in on_the_database
    assert "Record" in _headings(on_the_database)
