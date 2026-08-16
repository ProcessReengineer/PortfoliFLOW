# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Beat-level tests for the deterministic floor wiring (ADR-0088, Prompt 4).

Live-DB tests (compose Postgres via the shared ``app_engine`` /
``superuser_engine`` / ``seed_tenant`` fixtures) with a stubbed AI core.
Where ``test_irene_floor.py`` pins the pure floor arithmetic, these assert
that :func:`services.irene.beat.run_beat` *applies* it: the model's
``urgency_suggestion`` becomes the floored final urgency, the band is
derived from that final urgency, ``options`` are band-gated, a standalone
RSS finding is capped, a corroborated RSS is persisted as the internal
finding (uncapped), and a surfaced subject outside the eligible set is
dropped rather than persisted at a fabricated urgency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_finding_repository import IreneFindingRepository
from services.ai_service_core import ResolvedLLM, SynthesisResult
from services.irene.beat import run_beat
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS
from services.irene.rss_clustering import build_rss_buckets
from tests.services.irene._book_fixtures import (
    BREACH_NAV,
    SAA_SUBJECT,
    seed_book,
    seed_user,
)
from tests.services.irene._rss_fixtures import StubEmbedder, make_item


#: The per-tenant resolution the tick threads into every beat since
#: ADR-0112 §4b. The beat no longer takes a bare model string.
_TEST_LLM = ResolvedLLM(
    base_url="https://openrouter.test/api/v1",
    api_key="sk-beat-test",
    model="test-model",
)


class _StubCore:
    """Duck-typed AIServiceCore stub returning canned surface_finding calls."""

    def __init__(self, *, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self._tool_calls = tool_calls or []
        self.calls: list[dict[str, Any]] = []

    def get_system_prompt(self, prompt_name: str = "irene") -> str:
        return "You are Irene."

    async def run_synthesis(
        self,
        *,
        system_prompt: str,
        context_messages: list[dict[str, Any]],
        tool: dict[str, Any],
        model: str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        llm: Any = None,
    ) -> SynthesisResult:
        self.calls.append({"tool": tool, "llm": llm, "context": context_messages})
        return SynthesisResult(tool_calls=self._tool_calls, raw_text="")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _surface(subject_key: str, **args: Any) -> dict[str, Any]:
    return {
        "name": "surface_finding",
        "arguments": {"subject_key": subject_key, **args},
        "id": "call",
    }


# ---------------------------------------------------------------------------
# Internal breach → floor raises the suggestion; band derived; options kept
# ---------------------------------------------------------------------------


async def test_seeded_breach_floors_suggestion_and_preserves_discrepancy(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """A limit breach floors a suggestion of 5 up to 7 (critical band).

    The suggestion is retained in the payload so the suggestion↔final
    discrepancy is auditable; the critical band keeps the ``options``.
    """
    tenant_id = await seed_tenant("floor-breach")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    core = _StubCore(
        tool_calls=[
            _surface(
                SAA_SUBJECT,
                trigger="Equities coverage crossed its ceiling",
                finding="Equities is at 60% against a 50% ceiling.",
                basis="coverage 60% vs 50% ceiling",
                urgency_suggestion=5,
                options=["Trim equities", "Raise the ceiling"],
            )
        ]
    )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=_now())

        assert result.findings_written == 1
        assert result.error is None

        openf = await IreneFindingRepository(session).list_open()
        assert len(openf) == 1
        finding = openf[0]
        assert finding.subject_key == SAA_SUBJECT
        # Floor raised 5 → 7 (limit_breach floor); band derived from 7.
        assert finding.urgency == 7
        assert finding.band == "critical"
        # The suggestion survives in the payload for the audit discrepancy.
        assert finding.payload["urgency_suggestion"] == 5
        # Critical is at/above the options gate — advice is kept.
        assert finding.payload["options"] == ["Trim equities", "Raise the ceiling"]


# ---------------------------------------------------------------------------
# Informational band → options stripped even when the model supplied them
# ---------------------------------------------------------------------------


async def test_informational_band_strips_options(app_engine: AsyncEngine, seed_tenant) -> None:
    """A standalone RSS finding is capped at informational; options dropped.

    Source = RSS caps a high suggestion (9) at the informational top (3),
    and an informational card is pure fact — the ``options`` the stub
    supplied are dropped from the persisted payload.
    """
    tenant_id = await seed_tenant("floor-info")
    published = datetime(2026, 6, 30, 9, tzinfo=timezone.utc)
    items = [
        make_item("https://m/1", "POLICY: one", published, tags=("macro",), source="ECB"),
        make_item("https://m/2", "POLICY: two", published, tags=("macro",), source="FT"),
    ]
    now = _now()
    buckets = await build_rss_buckets(
        None, StubEmbedder(), items, now=now, thresholds=DEFAULT_DELTA_THRESHOLDS
    )
    assert len(buckets) == 1
    key = buckets[0].subject_key

    core = _StubCore(
        tool_calls=[
            _surface(
                key,
                trigger="Policy press cluster",
                finding="A cluster of policy coverage formed.",
                basis="2 items from ECB, FT",
                urgency_suggestion=9,
                options=["Review rates exposure"],
            )
        ]
    )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            core,
            llm=_TEST_LLM,
            now=now,
            rss_items=items,
            embedder=StubEmbedder(),
        )
        assert result.findings_written == 1

        finding = (await IreneFindingRepository(session).list_open())[0]
        # Capped: suggestion 9 → informational top 3.
        assert finding.urgency == 3
        assert finding.band == "informational"
        # Options dropped — a level-1 card is pure fact.
        assert "options" not in finding.payload
        # Suggestion still preserved for audit.
        assert finding.payload["urgency_suggestion"] == 9
        # RSS membership persisted for a later freeze.
        assert len(finding.payload["member_ids"]) == 2


# ---------------------------------------------------------------------------
# Corroborated RSS → persisted as the internal finding, not RSS-capped
# ---------------------------------------------------------------------------


async def test_corroborated_rss_persists_as_internal_and_is_not_capped(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """An RSS bucket corroborating an internal breach rides along, uncapped.

    The correlation lift merges the equities-tagged RSS bucket into the
    coincident ``saa:equities`` breach, so the beat surfaces the *internal*
    subject. It reaches the floor as source=internal (the RSS was already
    merged upstream), so it floors to the breach level 7 — not the RSS cap
    of 3 — and the RSS rides along as corroboration in the beat context.
    """
    tenant_id = await seed_tenant("floor-corroborated")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    published = datetime(2026, 6, 30, 9, tzinfo=timezone.utc)
    items = [
        make_item("https://e/1", "EQ: selloff", published, tags=("equities",), source="FT"),
        make_item("https://e/2", "EQ: rout", published, tags=("equities",), source="RTRS"),
    ]
    now = _now()

    core = _StubCore(
        tool_calls=[
            _surface(
                SAA_SUBJECT,
                trigger="Equities breach with corroborating press",
                finding="Equities is at 60% against a 50% ceiling.",
                basis="coverage 60% vs 50% ceiling; equities press selloff",
                urgency_suggestion=5,
            )
        ]
    )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            core,
            llm=_TEST_LLM,
            now=now,
            rss_items=items,
            embedder=StubEmbedder(),
        )
        assert result.findings_written == 1

        # The beat context folded the RSS into the internal card as
        # corroboration — not a standalone RSS card.
        ctx = core.calls[0]["context"][0]["content"]
        assert "subject_key: saa:equities" in ctx
        assert "corroborating external signal(s)" in ctx
        assert "EQ: selloff (FT)" in ctx
        assert "rss:cluster" not in ctx

        openf = await IreneFindingRepository(session).list_open()
        # Only the internal finding is persisted (the RSS was merged).
        assert len(openf) == 1
        finding = openf[0]
        assert finding.subject_key == SAA_SUBJECT
        # Floored to the breach level, NOT capped at the RSS informational 3.
        assert finding.urgency == 7
        assert finding.band == "critical"


# ---------------------------------------------------------------------------
# Grounding guard → a surfaced subject outside the eligible set is dropped
# ---------------------------------------------------------------------------


async def test_surfaced_subject_outside_eligible_set_is_dropped(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant, caplog
) -> None:
    """Even with a non-empty eligible set, an off-list subject is dropped.

    The breach makes ``saa:equities`` eligible, but the model surfaces a
    different, never-eligible key. That is a grounding violation: it is
    dropped with a warning and nothing is persisted at a fabricated urgency.
    """
    tenant_id = await seed_tenant("floor-drop")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)

    core = _StubCore(
        tool_calls=[
            _surface(
                "saa:phantom_class",
                trigger="hallucinated",
                finding="Something not in the beat context.",
                basis="invented",
                urgency_suggestion=9,
            )
        ]
    )

    async with tenant_context(app_engine, tenant_id) as session:
        with caplog.at_level(logging.WARNING, logger="services.irene.beat"):
            result = await run_beat(session, core, llm=_TEST_LLM, now=_now())

        assert result.silence is False
        assert result.findings_written == 0

        count = (await session.execute(text("SELECT count(*) FROM irene_finding"))).scalar_one()
        assert count == 0

    assert any(
        "not in the eligible set" in rec.message and "saa:phantom_class" in rec.message
        for rec in caplog.records
    )
