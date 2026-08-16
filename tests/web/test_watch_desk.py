# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Watch Desk web surface (Prompt 5, ADR-0089).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_section_navigation.py`` (login helper, superuser-seeded
tenant/user, HTMX header simulation). They cover the four checkpoints:

* Area/nav (direct + HTMX) — the sixth sidebar area.
* Briefing calm state (affirmative, not empty) + feed order + no raw badge.
* Resolution actions (audit columns, card removal, Journal, 422, immutability).
* Request analysis (enqueue-only, no inline synthesis, button gating).
* Journal (resolved-only, read-only).
* Calibration (read-only Floor Config threshold facts + cadence).
* Cadence settings round-trip (next_due_at recomputed, enabled flag).
* Tenant isolation.

The Watch Desk reads/writes only through the Irene repositories built in
Prompt 1; these tests never touch the delta/floor/synthesis layers.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.case_repository import CaseRepository
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.repositories.limits_repository import LimitsRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from services.web_research.allowlist import _KNOWN_TAGS
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_OTHER_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB Watch Desk tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures (live DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


_TRUNCATE = text(
    "TRUNCATE TABLE case_attachments, case_entries, cases, "
    "irene_finding, irene_schedule, irene_watch_state, "
    "limits, limit_sets, login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """App-role engine for seeding/reading cases under RLS (C4 open-case)."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(_TRUNCATE)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(_TRUNCATE)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    """Seed the primary tenant + owner user, and a second isolated tenant."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "dc-owner@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital')"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'other-tenant')"),
            {"id": str(_OTHER_TENANT_ID), "name": "Other Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def web_client(
    seeded_user: tuple[UUID, str, str],
) -> AsyncGenerator[AsyncClient, None]:
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, email: str, password: str) -> None:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


async def _session_csrf(client: AsyncClient, engine: AsyncEngine) -> str:
    """Read the logged-in session's CSRF token straight from the DB."""
    cookie = client.cookies.get("portfoliflow_session")
    assert cookie is not None, "not logged in"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT csrf_token FROM sessions WHERE session_token = :t"),
                {"t": cookie},
            )
        ).first()
    assert row is not None
    return str(row.csrf_token)


async def _seed_finding(
    engine: AsyncEngine,
    *,
    tenant_id: UUID = SENTINEL_TENANT_ID,
    subject_key: str,
    payload: dict,
    urgency: int,
    band: str,
    resolution: str = "open",
    created_at: datetime | None = None,
    finding_id: UUID | None = None,
) -> UUID:
    fid = finding_id or uuid4()
    created = created_at or datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO irene_finding
                    (id, tenant_id, subject_key, payload, urgency, band,
                     resolution, created_at)
                VALUES
                    (:id, :tid, :sk, CAST(:payload AS jsonb), :urgency,
                     :band, :resolution, :created_at)
                """
            ),
            {
                "id": str(fid),
                "tid": str(tenant_id),
                "sk": subject_key,
                "payload": json.dumps(payload),
                "urgency": urgency,
                "band": band,
                "resolution": resolution,
                "created_at": created,
            },
        )
    return fid


async def _seed_schedule(
    engine: AsyncEngine,
    *,
    tenant_id: UUID = SENTINEL_TENANT_ID,
    next_due_at: datetime,
    enabled: bool = True,
    cadence: str = "daily",
    preferred_hour: int = 8,
    timezone_name: str = "Europe/Berlin",
    last_beat_at: datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO irene_schedule
                    (id, tenant_id, user_id, cadence, preferred_hour,
                     timezone, enabled, next_due_at, last_beat_at)
                VALUES
                    (:id, :tid, NULL, :cadence, :hour, :tz, :enabled, :due,
                     :last_beat)
                """
            ),
            {
                "id": str(uuid4()),
                "tid": str(tenant_id),
                "cadence": cadence,
                "hour": preferred_hour,
                "tz": timezone_name,
                "enabled": enabled,
                "due": next_due_at,
                "last_beat": last_beat_at,
            },
        )


async def _seed_limit_set(
    engine: AsyncEngine,
    *,
    tenant_id: UUID,
    created_by: UUID,
    family: str,
    class_keys: dict[str, float],
) -> None:
    set_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO limit_sets
                    (id, tenant_id, family, effective_from, label, created_by)
                VALUES
                    (:id, :tid, :family, DATE '2020-01-01', :label, :cb)
                """
            ),
            {
                "id": str(set_id),
                "tid": str(tenant_id),
                "family": family,
                "label": f"{family} test set",
                "cb": str(created_by),
            },
        )
        for class_key, max_pct in class_keys.items():
            await conn.execute(
                text(
                    """
                    INSERT INTO limits
                        (id, tenant_id, limit_set_id, class_key, max_pct)
                    VALUES
                        (:id, :tid, :sid, :ck, :mp)
                    """
                ),
                {
                    "id": str(uuid4()),
                    "tid": str(tenant_id),
                    "sid": str(set_id),
                    "ck": class_key,
                    "mp": max_pct,
                },
            )


def _card_payload(
    *, trigger: str, finding: str, basis: str, options: list[str] | None = None
) -> dict:
    payload = {
        "trigger": trigger,
        "finding": finding,
        "basis": basis,
        "urgency_suggestion": 4,
    }
    if options is not None:
        payload["options"] = options
    return payload


# ---------------------------------------------------------------------------
# Area / navigation
# ---------------------------------------------------------------------------


async def test_area_direct_nav_returns_full_layout(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/watch-desk", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "<html" in body  # full base layout on direct nav
    assert 'data-area="watch_desk"' in body
    # Sidebar active-state on the Watch Desk entry.
    assert 'data-area="watch_desk"' in body
    assert "pf-sidebar__item is-active" in body


async def test_area_htmx_fragment_has_body_and_oob_sidebar(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(
        "/watch-desk",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body  # fragment, not full layout
    assert 'data-area="watch_desk"' in body
    assert "hx-swap-oob" in body  # OOB sidebar update


# ---------------------------------------------------------------------------
# Briefing — calm state
# ---------------------------------------------------------------------------


async def test_briefing_calm_state_is_affirmative(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """Zero open findings ⇒ the affirmative calm state, not an empty state."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text
    # Affirmative calm markup — the product thesis made visible.
    assert "pf-dc-calm__status" in body
    assert "All monitored limits are calm" in body
    # The collapsed lower-priority strip is present.
    assert "pf-dc-lowpri" in body
    # It is NOT an empty-list / card render.
    assert "pf-dc-card" not in body


# ---------------------------------------------------------------------------
# Briefing — derived status tiles (DC1)
# ---------------------------------------------------------------------------


async def test_tiles_never_ran_state_has_no_dot_and_no_count(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """No beat on record ⇒ the explicit never-ran state, not "surfaced 0"."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    assert "Never ran yet" in body
    assert "No analysis beat has completed for this tenant yet." in body
    # A beat that never ran surfaced nothing — no count, and no green dot.
    assert "surfaced" not in body
    assert 'class="dot"' not in body
    # No schedule at all ⇒ the next-beat tile says so and the button is gone.
    assert "no cadence set" in body


async def test_tiles_last_beat_counts_all_findings_since_beat(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """ "Surfaced N" counts every finding since the beat, resolved included."""
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    beat_at = now - timedelta(hours=3)

    # Predates the beat — must NOT be counted.
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:before_beat",
        payload=_card_payload(trigger="Older", finding="Before the beat.", basis="Old."),
        urgency=5,
        band="noteworthy",
        created_at=beat_at - timedelta(hours=1),
    )
    # Since the beat, still open — counted, and in the feed.
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:after_open",
        payload=_card_payload(trigger="Newer", finding="After the beat.", basis="New."),
        urgency=8,
        band="critical",
        created_at=beat_at + timedelta(minutes=5),
    )
    # Since the beat but already resolved — counted by the tile (the beat
    # did surface it), absent from the open feed.
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:after_resolved",
        payload=_card_payload(trigger="Handled", finding="After the beat.", basis="New."),
        urgency=6,
        band="noteworthy",
        resolution="acted",
        created_at=beat_at + timedelta(minutes=10),
    )

    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=now + timedelta(days=1),
        last_beat_at=beat_at,
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # Surfaced-since-beat = the open one + the resolved one. An open-only
    # count would say 1 and understate what the beat put in front of the PM.
    assert "surfaced 2 findings" in body
    assert 'class="dot"' in body  # a beat ran ⇒ the green dot renders

    # The open-findings tile counts a *different* set of two: the pre-beat
    # noteworthy (still open) and the post-beat critical. The resolved one
    # is absent. The two tiles measure different things by design.
    assert '<span class="pf-dc-tile__value">2</span>' in body
    assert "1 critical" in body
    assert "1 noteworthy" in body


async def test_tiles_subjects_watched_enumerates_limit_sets(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Internal subjects = limit rows across the effective saa/anlv sets."""
    user_id, email, password = seeded_user
    await _seed_limit_set(
        fresh_superuser_engine,
        tenant_id=SENTINEL_TENANT_ID,
        created_by=user_id,
        family="saa",
        class_keys={"equity": 40.0, "credit": 30.0, "real_estate": 30.0},
    )
    await _seed_limit_set(
        fresh_superuser_engine,
        tenant_id=SENTINEL_TENANT_ID,
        created_by=user_id,
        family="anlv",
        class_keys={"anlv_high_yield": 5.0, "anlv_equity": 35.0},
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    rss = len(_KNOWN_TAGS)
    assert f"5 + {rss}" in body
    assert f"5 internal limits · {rss} press dimensions" in body


async def test_tiles_render_with_no_limit_sets(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """Empty book ⇒ zero internal subjects, tile still renders."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    rss = len(_KNOWN_TAGS)
    assert f"0 + {rss}" in body
    assert "pf-dc-tiles" in body


async def test_tiles_next_beat_reflects_schedule(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The next-beat tile mirrors next_due_at / cadence / enabled."""
    _id, email, password = seeded_user
    # Relative to the run, and in the future: a due-or-overdue schedule
    # renders "due now" instead of a stamp, which is the state the
    # request-analysis test below pins.
    due = datetime.now(timezone.utc) + timedelta(days=2)
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=due,
        enabled=True,
        timezone_name="Europe/Berlin",
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # Rendered in the tenant timezone, not UTC — Berlin is never UTC+0, so
    # the two stamps must differ.
    berlin = due.astimezone(ZoneInfo("Europe/Berlin")).strftime("%a %H:%M")
    assert berlin != due.strftime("%a %H:%M")
    assert f"Next beat · {berlin} · daily · enabled" in body
    assert "Request analysis now" in body


async def test_tiles_next_beat_reads_due_now_when_already_due(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A schedule that is already due says so, rather than stamping the past.

    The tile is labelled "Next beat": a wall-clock time that has passed
    states the opposite of what is true. This is also what makes the
    out-of-band refresh after "Request analysis" visible — the enqueue
    moves ``next_due_at`` to now, so the tile flips to this state.
    """
    _id, email, password = seeded_user
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=datetime.now(timezone.utc) - timedelta(hours=3),
        enabled=True,
        timezone_name="Europe/Berlin",
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200

    assert "Next beat · due now · daily · enabled" in response.text


# ---------------------------------------------------------------------------
# Briefing — the "What Irene watches" monitor (DC2)
#
# The monitor's projection arithmetic is covered by
# test_watch_desk_monitor.py; these tests pin the route-level
# behaviour — that it renders inside the Briefing request, degrades rather
# than errors on an empty book, and makes no claim a beat has run when none
# has.
# ---------------------------------------------------------------------------


async def test_monitor_renders_beneath_the_feed(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """The monitor is part of the Briefing body — no second endpoint."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    assert "pf-dc-monitor" in body
    assert "On watch" in body
    # Beneath the cards, per the mock.
    assert body.index("dc-briefing-feed") < body.index("pf-dc-monitor")
    assert '<details class="pf-dc-group" open>' not in body


async def test_monitor_empty_universe_degrades_without_error(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """No valued positions ⇒ an explicit empty state, never a 500."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    assert "No valued positions yet — nothing to monitor" in body
    assert "SAA limits" in body
    assert "AnlV quotas" in body
    # The press group is DB-free, so it renders regardless of coverage.
    assert "Press dimensions (RSS)" in body
    for tag in _KNOWN_TAGS:
        assert tag in body


async def test_monitor_rss_group_has_three_columns_and_no_cluster_claims(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """D8: the mock's cluster column and silent-cluster notes stay deferred."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    body = response.text

    assert "Clusters today" not in body
    assert "Below materiality" not in body
    assert "No cluster formed today" not in body
    assert "corroboration only, never a source of figures" in body


async def test_monitor_never_ran_state_claims_no_beat(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """The head makes no observation claim, so a never-ran tenant reads none.

    UI-024 removed the head's beat stamp outright rather than branching it:
    the honesty rule that held before — never claim an observation time the
    tenant has not earned — is now satisfied structurally. The last-beat
    time still reaches the operator through the "Last beat" tile.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    body = response.text

    assert "On watch" in body
    assert "Irene has not completed a beat yet" not in body
    assert "last saw these subjects at" not in body


async def _seed_valued_universe(
    actor_id: UUID, *, equities_max_pct: Decimal = Decimal("25.0")
) -> None:
    """Seed one constrained SAA class at 24% of the book.

    Against the default 25% ceiling that is 96% utilisation — a WARN row.
    Callers that need a BREACH pass a lower ``equities_max_pct``; the NAVs
    are unchanged, so the only thing that moves is the ceiling.

    An explicit cash position holds the remainder: since ADR-0103 §2 the
    denominator is ``Σ NAV``, so a class only sits at a given percentage if
    something else holds the rest of the book. Cash is deliberately left
    out of the limit set, so it is a ``NO_LIMIT`` row the monitor skips.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    # The evaluation range runs 12 months back from the NAV horizon, and the
    # engine refuses a Stichtag an investment has no NAV at or before — so
    # each position is valued at both ends of the window.
    stichtage = (date(2025, 6, 30), date(2026, 6, 30))
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            nav_repo = InvestmentNavRepository(session)
            for code, name, kind, value in (
                ("equities", "Alpha", "private_equity", Decimal("240000")),
                ("cash", "Cash EUR", "cash", Decimal("760000")),
            ):
                asset_class = await AssetClassRepository(session).create(
                    code=code, display_name=code.title()
                )
                investment = await InvestmentRepository(session).create(
                    name=name,
                    investment_type=kind,
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=actor_id,
                )
                for stichtag in stichtage:
                    await nav_repo.upsert(
                        investment_id=investment.id,
                        as_of_date=stichtag,
                        nav_kind="actual",
                        nav_value=value,
                        currency="EUR",
                        source=None,
                        created_by=actor_id,
                    )
            limits_repo = LimitsRepository(session)
            await limits_repo.create_set_with_limits(
                family="saa",
                effective_from=date(2020, 1, 1),
                label="SAA monitor test",
                notes=None,
                limits={"equities": equities_max_pct},
                created_by=actor_id,
            )
            # The engine evaluates both families and refuses a Stichtag with
            # no set in force, so AnlV needs one too. Neither position carries
            # an anlv_code, so the quota sits at 0% — an OK row.
            await limits_repo.create_set_with_limits(
                family="anlv",
                effective_from=date(2020, 1, 1),
                label="AnlV monitor test",
                notes=None,
                limits={"anlv_1": Decimal("60.0")},
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def test_monitor_renders_live_coverage_rows_with_gauges(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """End-to-end: engine → gauge → note, with no beat having run.

    Equities sit at 24% of a 25% ceiling — 96% utilisation, past the 90%
    WARN threshold — so the row is WARN and, absent any beat, carries the
    "not yet reviewed" note rather than an invented acknowledgement.
    """
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id)
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    assert "saa:equities" in body
    assert "SAA monitor test" in body
    assert "gauge gauge--warn" in body
    assert 'style="width:96.0%"' in body
    assert "WARN — not yet reviewed." in body
    # The WARN tick sits at this subject's effective threshold — the tenant
    # default here, since nothing overrides it. Positioned per subject since
    # ADR-0116 §6, never rescaled: the gauge still runs 0 → ceiling.
    assert (
        'class="gauge__warnmark"\n                                                  style="left:90.0%"'
        in (body)
        or 'style="left:90.0%"' in body
    )
    # Cash carries no ceiling, so it is not a gauged row.
    assert "saa:cash" not in body


async def test_monitor_moves_the_mark_and_tags_a_muted_subject(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A saved overlay is visible in the monitor immediately (ADR-0116 §6)."""
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    saved = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={
            "subject_key": "saa:equities",
            "warn_threshold_pct": "70",
            "muted": "on",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200
    body = saved.text

    # The mark moved to this subject's own WARN fraction…
    assert 'style="left:70.0%"' in body
    # …the row is tagged and counted, and stays visible with a live status…
    assert 'class="tag tag--muted">muted<' in body
    assert "1 muted" in body
    assert "saa:equities" in body
    assert "gauge gauge--warn" in body
    # …and the fill is unchanged: mute and the mark move nothing about the
    # utilisation the gauge encodes.
    assert 'style="width:96.0%"' in body


async def test_monitor_disables_the_mute_toggle_at_breach(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A breach cannot be muted — the toggle says so and says why."""
    user_id, email, password = seeded_user
    # 24% of the book against a 20% ceiling → BREACH.
    await _seed_valued_universe(user_id, equities_max_pct=Decimal("20.0"))
    await _login(web_client, email, password)

    editor = await web_client.get("/api/watch-desk/watchpoints/overlay?subject_key=saa%3Aequities")
    assert editor.status_code == 200
    assert "disabled" in editor.text
    assert "A breach cannot be muted" in editor.text


async def test_a_locked_mute_survives_a_save_of_the_other_fields(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Locked means unchangeable here, never quietly reset.

    A disabled checkbox is not submitted, so without the hidden carrier
    field, saving a WARN override on a breaching-and-muted row would clear
    the mute the operator never touched.
    """
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id, equities_max_pct=Decimal("20.0"))
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Mute it first — legal, because the toggle only locks at BREACH and the
    # seeded ceiling makes it one; the beat, not this endpoint, is the
    # enforcement point, so the mute itself is accepted and simply ignored
    # for breach findings.
    await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "saa:equities", "muted": "on"},
        headers={"X-CSRF-Token": csrf},
    )

    editor = await web_client.get("/api/watch-desk/watchpoints/overlay?subject_key=saa%3Aequities")
    assert 'type="hidden" name="muted" value="on"' in editor.text

    # Now save an unrelated field the way the locked form posts it.
    await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "saa:equities", "muted": "on", "warn_threshold_pct": "80"},
        headers={"X-CSRF-Token": csrf},
    )

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT muted, warn_threshold_pct FROM watchpoints "
                    "WHERE subject_key = 'saa:equities' "
                    "ORDER BY effective_from DESC LIMIT 1"
                )
            )
        ).first()
    assert row is not None
    assert row.muted is True
    assert row.warn_threshold_pct == Decimal("80")


async def test_monitor_leaves_the_mute_toggle_free_below_breach(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The lock is the breach rule's mirror, not a blanket restriction."""
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id)  # WARN, not BREACH
    await _login(web_client, email, password)

    calm = await web_client.get("/api/watch-desk/watchpoints/overlay?subject_key=saa%3Aequities")
    assert calm.status_code == 200
    assert "disabled" not in calm.text
    assert "Suppresses finding creation only" in calm.text


async def test_monitor_reports_the_effective_set_label(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A family with an effective set names it; one without says so."""
    user_id, email, password = seeded_user
    await _seed_limit_set(
        fresh_superuser_engine,
        tenant_id=SENTINEL_TENANT_ID,
        created_by=user_id,
        family="saa",
        class_keys={"equity": 40.0},
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    body = response.text

    assert "saa test set" in body
    # AnlV has no set seeded — the group renders, it does not 500.
    assert "no effective set" in body


# ---------------------------------------------------------------------------
# Briefing — feed order + no raw urgency badge
# ---------------------------------------------------------------------------


async def test_briefing_feed_order_and_no_raw_badge(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    # Two critical (same urgency, different recency) + one informational.
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:critical_old",
        payload=_card_payload(
            trigger="Old breach", finding="Older critical.", basis="Coverage over ceiling."
        ),
        urgency=8,
        band="critical",
        created_at=now - timedelta(hours=2),
    )
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:critical_new",
        payload=_card_payload(
            trigger="New breach", finding="Newer critical.", basis="Coverage over ceiling."
        ),
        urgency=8,
        band="critical",
        created_at=now - timedelta(hours=1),
    )
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="anlv:info",
        payload=_card_payload(
            trigger="Quiet note", finding="Informational only.", basis="Small move."
        ),
        urgency=2,
        band="informational",
        created_at=now - timedelta(hours=3),
    )

    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # Order: urgency desc, then recency desc → new-critical, old-critical, info.
    pos_new = body.index("saa:critical_new")
    pos_old = body.index("saa:critical_old")
    pos_info = body.index("anlv:info")
    assert pos_new < pos_old < pos_info

    # Band drives the CSS class.
    assert "pf-dc-card--critical" in body
    assert "pf-dc-card--informational" in body
    assert 'data-band="critical"' in body

    # No raw 1–10 urgency badge anywhere.
    assert "data-urgency" not in body
    assert "pf-dc-card__urgency" not in body

    # UI-023: the agent persona is never named in rendered copy. The name
    # survives in code (``IreneFindingRepository``, the ``.irene-note``
    # class, ``services/irene/``) — only the surface is de-personified.
    # Every fixture string here is test-authored, so this is stable.
    assert "Irene" not in body


async def test_possible_moves_is_prose_and_absent_on_informational_card(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A carried ``options`` payload renders as one prose paragraph (DC3).

    The band gate lives upstream in the beat, so an informational finding
    reaches the card with ``options`` already dropped — and must therefore
    render no moves block at all.
    """
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:with_moves",
        payload=_card_payload(
            trigger="Ceiling breach",
            finding="Coverage is over the ceiling.",
            basis="Coverage 104.2% of a 100% ceiling.",
            options=[
                "Restoring headroom needs roughly 120 k EUR off the quota.",
                "A partial sale of the most liquid position would clear it.",
            ],
        ),
        urgency=8,
        band="critical",
        created_at=now - timedelta(hours=1),
    )
    # Informational: the beat dropped options upstream, so none are stored.
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="anlv:no_moves",
        payload=_card_payload(
            trigger="Quiet note", finding="Informational only.", basis="Small move."
        ),
        urgency=2,
        band="informational",
        created_at=now - timedelta(hours=2),
    )

    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # The list markup is gone; the prose block replaced it.
    assert "pf-dc-card__options" not in body
    assert "Possible moves" in body

    # Both option strings joined into a single paragraph, in order.
    assert (
        "Restoring headroom needs roughly 120 k EUR off the quota. "
        "A partial sale of the most liquid position would clear it."
    ) in body

    # Exactly one moves block — the informational card carries none.
    assert body.count('class="pf-dc-card__starting"') == 1

    # The Open-case button is armed (ADR-0107, C4): a real control posting to
    # the open-case endpoint, no longer the inert v1 preview.
    assert "pf-dc-opencase" in body
    assert 'hx-post="/api/watch-desk/findings/' in body
    assert "/open-case" in body
    # It sits inside the (single) Possible-moves block, band-gated as before.
    opencase_form = body[body.index("pf-dc-opencase-form") : body.index("Open case")]
    assert "disabled" not in opencase_form
    assert 'name="csrf_token"' in opencase_form


# --- "Open case →" follows the band, not the options presence (ADR-0120) ----


@pytest.mark.parametrize(
    ("band", "urgency"),
    [("critical", 8), ("noteworthy", 5)],
)
async def test_option_less_higher_band_card_renders_footer_case_affordance(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    band: str,
    urgency: int,
) -> None:
    """An option-less noteworthy/critical card still carries the case path.

    ADR-0120 §1/§2: ``options`` is optional in the ADR-0088 contract, so a
    higher-band card may arrive as pure statement. With no Possible-moves
    block to host it, the same form renders standalone in the card footer.
    """
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key=f"saa:no_options_{band}",
        payload=_card_payload(
            trigger="Equities over the SAA ceiling",
            finding="The ceiling is breached.",
            basis="Coverage 26.00% of a 25.00% ceiling.",
        ),
        urgency=urgency,
        band=band,
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # No Possible-moves block — the payload carries no options.
    assert "pf-dc-card__starting" not in body
    # The affordance renders exactly once, in the card footer.
    assert body.count("pf-dc-opencase-form") == 1
    assert 'class="pf-dc-card__foot"' in body

    # Same endpoint and same HTMX semantics as the in-block variant: the card
    # posts to open-case and replaces itself.
    foot = body[body.index('class="pf-dc-card__foot"') :]
    form = foot[: foot.index("</form>")]
    assert f'hx-post="/api/watch-desk/findings/{fid}/open-case"' in form
    assert f'hx-target="#dc-card-{fid}"' in form
    assert 'hx-swap="outerHTML"' in form
    assert 'name="csrf_token"' in form
    assert "disabled" not in form


async def test_card_with_options_keeps_the_affordance_inside_the_moves_block(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """With options present the placement is unchanged — and not duplicated.

    ADR-0120 §2 preserves ADR-0107 binding decision 4 for this case: the
    affordance stays inside the Possible-moves block, never additionally in
    the footer.
    """
    _id, email, password = seeded_user
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:with_options",
        payload=_card_payload(
            trigger="Equities over the SAA ceiling",
            finding="The ceiling is breached.",
            basis="Coverage 26.00% of a 25.00% ceiling.",
            options=["Trimming the most liquid position would clear it."],
        ),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # Exactly one affordance, and it sits between the moves block and the
    # resolution row — i.e. inside the block, not in the footer.
    assert body.count("pf-dc-opencase-form") == 1
    assert 'class="pf-dc-card__foot"' not in body
    moves = body[body.index('class="pf-dc-card__starting"') : body.index("pf-dc-card__actions")]
    assert "pf-dc-opencase-form" in moves


@pytest.mark.parametrize("options", [None, ["Trimming would clear it."]])
async def test_informational_card_renders_no_case_affordance(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    options: list[str] | None,
) -> None:
    """Informational stays acknowledged-only (ADR-0107 D1, ADR-0120 §1).

    Parametrised over the legacy shape too: even a stored ``options`` payload
    (which the beat drops today) buys an informational card no case path.
    """
    _id, email, password = seeded_user
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="anlv:quiet",
        payload=_card_payload(
            trigger="Quiet note",
            finding="Informational only.",
            basis="Small move.",
            options=options,
        ),
        urgency=2,
        band="informational",
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    assert "Quiet note" in body  # the card itself rendered
    assert "pf-dc-opencase" not in body
    assert "/open-case" not in body
    assert 'class="pf-dc-card__foot"' not in body


# ---------------------------------------------------------------------------
# Resolution actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resolution", ["acted", "dismissed", "acknowledged"])
async def test_resolution_writes_audit_and_removes_card(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    resolution: str,
) -> None:
    user_id, email, password = seeded_user
    payload = _card_payload(trigger="Breach", finding="Over ceiling.", basis="102% of a 100% cap.")
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:resolve_me",
        payload=payload,
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/resolve",
        data={"resolution": resolution},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    # The card left the feed (the only finding → calm state returned).
    assert "saa:resolve_me" not in response.text
    assert "pf-dc-calm__status" in response.text

    # The three resolution columns are written; the audit fields are correct
    # and the immutable history fields are untouched.
    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT resolution, resolved_by, resolved_at, urgency, "
                    "band, payload FROM irene_finding WHERE id = :id"
                ),
                {"id": str(fid)},
            )
        ).first()
    assert row is not None
    assert row.resolution == resolution
    assert row.resolved_by == user_id
    assert row.resolved_at is not None
    # Immutable fields unchanged.
    assert row.urgency == 8
    assert row.band == "critical"
    assert row.payload == payload


async def test_resolved_finding_appears_in_journal(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:journal_me",
        payload=_card_payload(trigger="Breach", finding="Over ceiling.", basis="102% of cap."),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    await web_client.post(
        f"/api/watch-desk/findings/{fid}/resolve",
        data={"resolution": "acted"},
        headers={"X-CSRF-Token": csrf},
    )

    journal = await web_client.get("/api/watch-desk/journal")
    assert journal.status_code == 200
    assert "saa:journal_me" in journal.text
    assert "Acted" in journal.text


async def test_invalid_resolution_returns_422_not_500(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:bad_res",
        payload=_card_payload(trigger="x", finding="y", basis="z"),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/resolve",
        data={"resolution": "totally-bogus"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "pf-dc-error" in response.text
    # The row is untouched — still open.
    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT resolution FROM irene_finding WHERE id = :id"),
                {"id": str(fid)},
            )
        ).first()
    assert row is not None
    assert row.resolution == "open"


# ---------------------------------------------------------------------------
# Request analysis — enqueue only, no inline synthesis
# ---------------------------------------------------------------------------


async def test_request_analysis_enqueues_without_inline_synthesis(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    future = datetime.now(timezone.utc) + timedelta(days=3)
    await _seed_schedule(fresh_superuser_engine, next_due_at=future)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    before = datetime.now(timezone.utc)
    response = await web_client.post(
        "/api/watch-desk/request-analysis",
        headers={"X-CSRF-Token": csrf},
    )
    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    # The copy names the scheduler tick, not the next cadence occurrence.
    assert "Queued — the beat runs on the next tick" in response.text

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT next_due_at, last_beat_at FROM irene_schedule WHERE user_id IS NULL")
            )
        ).first()
        finding_count = (
            await conn.execute(text("SELECT COUNT(*) AS n FROM irene_finding"))
        ).scalar_one()
    assert row is not None
    # next_due_at was pulled to ~now (enqueued), not left in the future.
    assert before - timedelta(seconds=5) <= row.next_due_at <= after + timedelta(seconds=5)
    # No beat ran inline: no findings were written and last_beat_at is unset.
    assert finding_count == 0
    assert row.last_beat_at is None


async def test_request_analysis_refreshes_the_next_beat_tile(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The enqueue answers in the tile, not only in the toolbar line.

    The button's main swap targets ``#dc-briefing-toolbar``, so without an
    out-of-band fragment the fourth tile would keep showing the pre-enqueue
    ``next_due_at`` and the action would read as a no-op.
    """
    _id, email, password = seeded_user
    future = datetime.now(timezone.utc) + timedelta(days=3)
    await _seed_schedule(fresh_superuser_engine, next_due_at=future)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # The pre-enqueue briefing stamps a future time, not "due now".
    briefing = await web_client.get("/api/watch-desk/briefing")
    assert briefing.status_code == 200
    assert 'id="dc-briefing-next-beat"' in briefing.text
    assert "Next beat · due now" not in briefing.text

    response = await web_client.post(
        "/api/watch-desk/request-analysis",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.text

    # The OOB fragment carries the tile by id, and reads the enqueued state.
    assert 'id="dc-briefing-next-beat"' in body
    assert 'hx-swap-oob="true"' in body
    assert "Next beat · due now · daily · enabled" in body

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT next_due_at FROM irene_schedule WHERE user_id IS NULL"))
        ).first()
    assert row is not None
    assert row.next_due_at <= datetime.now(timezone.utc)


async def test_request_analysis_without_schedule_sends_no_tile_fragment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Nothing was enqueued, so nothing is swapped — only the operator hint."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/request-analysis",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "hx-swap-oob" not in response.text
    assert "Queued" not in response.text


async def test_request_analysis_button_hidden_without_schedule(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    # No schedule seeded → button absent.
    briefing = await web_client.get("/api/watch-desk/briefing")
    assert briefing.status_code == 200
    assert "Request analysis" not in briefing.text


async def test_request_analysis_button_shown_with_schedule(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    await _login(web_client, email, password)
    briefing = await web_client.get("/api/watch-desk/briefing")
    assert briefing.status_code == 200
    assert "Request analysis" in briefing.text


# ---------------------------------------------------------------------------
# Briefing poll — the post-enqueue refresh: 204 while pending, 286 to stop
# ---------------------------------------------------------------------------


def _poll_url(since: datetime) -> str:
    """The poll URL the confirmation partial builds, for a given instant."""
    return f"/api/watch-desk/briefing/poll?since={quote(since.isoformat(), safe='')}"


async def test_poll_returns_the_refreshed_briefing_when_the_beat_landed(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The done condition: 286 (stop polling) carrying the whole Briefing.

    286 is HTMX's "stop polling" status *and* a swappable one, so this
    single response both ends the poll and refreshes the page. The body has
    to be the full Briefing body — feed and all four tiles — because a beat
    moves both: new cards below, a new "Last beat" stamp above.
    """
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=30)
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=now + timedelta(days=1),
        # A beat landed after the enqueue this poll is waiting on.
        last_beat_at=since + timedelta(seconds=5),
    )
    await _login(web_client, email, password)

    response = await web_client.get(_poll_url(since))

    assert response.status_code == 286
    body = response.text
    # The swapped region is the briefing root, so the poller inside it goes
    # with the markup it replaces.
    assert 'id="dc-briefing"' in body
    assert 'id="dc-briefing-feed"' in body
    # All four status tiles ride along, not just the feed.
    assert "Last beat" in body
    assert "Subjects watched" in body
    assert "Open findings" in body
    assert 'id="dc-briefing-next-beat"' in body
    # A fresh Briefing starts no poll of its own — only an enqueue does.
    assert "pf-dc-request-poll" not in body
    assert "briefing/poll" not in body


async def test_poll_is_204_while_the_beat_is_still_pending(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No beat yet: 204, which HTMX does not swap, so the page stands."""
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    await _seed_schedule(fresh_superuser_engine, next_due_at=now)
    await _login(web_client, email, password)

    response = await web_client.get(_poll_url(now - timedelta(seconds=30)))

    assert response.status_code == 204
    assert response.text == ""


async def test_poll_is_204_when_the_last_beat_predates_the_enqueue(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The condition is "since the enqueue", not "ever".

    A tenant with a beat behind it would otherwise terminate the poll on its
    first tick and refresh nothing new — the exact staleness this closes.
    """
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=30)
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=now,
        last_beat_at=since - timedelta(hours=6),
    )
    await _login(web_client, email, password)

    response = await web_client.get(_poll_url(since))

    assert response.status_code == 204
    assert response.text == ""


@pytest.mark.parametrize(
    "query",
    [
        "",  # no marker at all
        "?since=",
        "?since=not-a-timestamp",
        "?since=2026-08-13T10:00:00",  # naive: no zone to compare against
    ],
    ids=["absent", "empty", "garbage", "naive"],
)
async def test_poll_stops_on_an_unusable_since(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    query: str,
) -> None:
    """A hand-edited marker terminates the poll — it never 500s.

    ``HX-Reswap: none`` is the load-bearing half: the poller declares an
    ``outerHTML`` swap of ``#dc-briefing``, so an empty 286 without it would
    delete the section instead of leaving it alone.
    """
    _id, email, password = seeded_user
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=datetime.now(timezone.utc),
    )
    await _login(web_client, email, password)

    response = await web_client.get(f"/api/watch-desk/briefing/poll{query}")

    assert response.status_code == 286
    assert response.text == ""
    assert response.headers["HX-Reswap"] == "none"


async def test_poll_stops_without_a_schedule_row(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """No row for a beat to advance — nothing can ever land."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(_poll_url(datetime.now(timezone.utc)))

    assert response.status_code == 286
    assert response.text == ""
    assert response.headers["HX-Reswap"] == "none"


async def test_poll_stops_past_the_server_side_horizon(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A beat that never runs must not leave a tab polling for ever.

    The control case pins that it is the *horizon* terminating the poll and
    not the pending branch generally: nine minutes still gets a 204.
    """
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    await _seed_schedule(fresh_superuser_engine, next_due_at=now)
    await _login(web_client, email, password)

    inside = await web_client.get(_poll_url(now - timedelta(minutes=9)))
    assert inside.status_code == 204

    beyond = await web_client.get(_poll_url(now - timedelta(minutes=11)))
    assert beyond.status_code == 286
    assert beyond.text == ""
    assert beyond.headers["HX-Reswap"] == "none"


async def test_poll_pending_branch_resolves_no_watchpoints(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 204 branch is one schedule read — no resolution, no monitor.

    This branch runs about four times a minute per open tab, so what it must
    *not* do is the expensive part of a Briefing render: the watch-desk
    resolution, the batched signal fetch and the coverage computation behind
    it. Pinned by making that resolution explode: the pending poll answers
    204 regardless, and the landed poll (same fixture, same patch) proves the
    explosive is armed rather than unreachable.
    """
    _id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=30)
    await _seed_schedule(fresh_superuser_engine, next_due_at=now)
    await _login(web_client, email, password)

    async def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("the pending branch resolved the watch desk")

    monkeypatch.setattr("web.routes.watch_desk.resolve_watch_desk", _boom)

    response = await web_client.get(_poll_url(since))
    assert response.status_code == 204

    # Control: the done branch *does* render, so it hits the patched
    # resolution. Without this the test above would pass on a route that
    # never resolves at all.
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE irene_schedule SET last_beat_at = :beat WHERE user_id IS NULL"),
            {"beat": since + timedelta(seconds=5)},
        )
    with pytest.raises(RuntimeError):
        await web_client.get(_poll_url(since))


async def test_request_analysis_starts_a_self_terminating_poller(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The confirmation is what starts the poll — nothing else on the page.

    ``since`` has to be the *server's* enqueue instant: a client clock
    minutes off would make the done condition fire on the first tick or
    never. It is asserted inside the request window for that reason, and the
    URL is then replayed verbatim so the percent-encoding is proven too — an
    ISO 8601 offset carries a "+", which a raw query string reads as a space.
    """
    _id, email, password = seeded_user
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    before = datetime.now(timezone.utc)
    response = await web_client.post(
        "/api/watch-desk/request-analysis",
        headers={"X-CSRF-Token": csrf},
    )
    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    body = response.text

    assert 'hx-trigger="every 15s"' in body
    assert 'hx-target="#dc-briefing"' in body
    assert 'hx-swap="outerHTML"' in body

    match = re.search(r'hx-get="(/api/watch-desk/briefing/poll\?since=[^"]+)"', body)
    assert match is not None
    url = match.group(1)
    since = datetime.fromisoformat(unquote(url.split("since=", 1)[1]))
    assert since.tzinfo is not None
    assert before - timedelta(seconds=5) <= since <= after + timedelta(seconds=5)

    # Replayed as rendered: still pending (no beat has run), so 204 — which
    # also proves the encoded marker survives the round trip.
    pending = await web_client.get(url)
    assert pending.status_code == 204

    # And once a beat lands after that instant, the same URL answers 286.
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE irene_schedule SET last_beat_at = :beat WHERE user_id IS NULL"),
            {"beat": since + timedelta(seconds=5)},
        )
    landed = await web_client.get(url)
    assert landed.status_code == 286
    assert 'id="dc-briefing-feed"' in landed.text


async def test_request_analysis_without_schedule_starts_no_poller(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Nothing was enqueued, so there is nothing to wait for."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/request-analysis",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "briefing/poll" not in response.text
    assert "pf-dc-request-poll" not in response.text


async def test_briefing_page_starts_no_poller_on_load(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Polling begins with an enqueue and with nothing else (ADR-0086)."""
    _id, email, password = seeded_user
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    await _login(web_client, email, password)

    briefing = await web_client.get("/api/watch-desk/briefing")
    assert briefing.status_code == 200
    assert "briefing/poll" not in briefing.text
    assert "every 15s" not in briefing.text


# ---------------------------------------------------------------------------
# Journal — resolved-only, read-only
# ---------------------------------------------------------------------------


async def test_journal_lists_only_resolved_and_is_read_only(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:still_open",
        payload=_card_payload(trigger="open", finding="f", basis="b"),
        urgency=8,
        band="critical",
        resolution="open",
    )
    await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:done",
        payload=_card_payload(trigger="done", finding="f", basis="b"),
        urgency=5,
        band="noteworthy",
        resolution="dismissed",
    )
    await _login(web_client, email, password)
    journal = await web_client.get("/api/watch-desk/journal")
    assert journal.status_code == 200
    body = journal.text
    assert "saa:done" in body  # resolved shows
    assert "saa:still_open" not in body  # open does not
    # Read-only: no action buttons / resolve forms.
    assert "pf-dc-btn" not in body
    assert "/resolve" not in body


# ---------------------------------------------------------------------------
# Calibration — the tenant calibration editor + cadence (DC4/D5, ADR-0116 §7)
# ---------------------------------------------------------------------------


async def test_calibration_renders_the_editor_on_effective_values(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Every field shows its effective value, marked "default" when unstored."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/calibration")
    assert response.status_code == 200
    body = response.text

    # The read-only torso is gone: the editor supersedes it (ADR-0116 §7).
    assert "calib-cell" not in body
    assert 'id="dc-calibration-editor"' in body

    # WARN default, band boundaries and the options gate, from the config.
    assert 'name="warn_default_pct" value="90.0"' in body
    assert 'name="band_boundary_0"\n                           value="3"' in body or (
        'name="band_boundary_0" value="3"' in body
    )
    assert 'name="options_min_band"' in body

    # All seven families are calibratable, the four signal ones included —
    # their producers land later and the values simply wait.
    for family in ("saa", "anlv", "rss", "price", "fx", "freshness", "liquidity"):
        assert f'name="re_trigger_delta_{family}"' in body

    # Floors and caps per trigger / source.
    assert 'name="floor_limit_breach" value="7"' in body
    assert 'name="cap_rss" value="3"' in body

    # Provenance: every field carries a marker (8 floors + 10 caps + 7
    # deltas + WARN + boundaries + options gate + the pinned row), and with
    # nothing stored yet not one of them reads "customised".
    assert body.count("pf-dc-calib__marker") >= 29
    assert "customised" not in body

    # The cadence panel is still rendered inline, unchanged.
    assert 'id="dc-cadence-panel"' in body


async def test_calibration_renders_the_pinned_invariants_locked(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Locked means not submittable, not merely styled (ADR-0116 §7)."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    body = (await web_client.get("/api/watch-desk/calibration")).text

    # fund_closure: a locked row with its rationale and NO input element.
    assert "Pinned at 10 — not calibration" in body
    assert 'name="floor_fund_closure"' not in body
    assert 'name="cap_fund_closure"' not in body

    # The three coupling rules render as constraint hints on the fields
    # they bind, so the coupling is visible before a save is refused.
    assert "Lower bound follows the upper band boundary" in body
    assert body.count("band boundary") >= 3


async def test_calibration_no_longer_enumerates_subjects(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The per-subject tables retired — the monitor covers them now."""
    user_id, email, password = seeded_user
    await _seed_limit_set(
        fresh_superuser_engine,
        tenant_id=SENTINEL_TENANT_ID,
        created_by=user_id,
        family="saa",
        class_keys={"equities": 30.0, "real_estate": 20.0},
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/calibration")
    assert response.status_code == 200
    body = response.text

    # Seeded limit subjects are NOT enumerated here any more.
    assert "saa:equities" not in body
    assert "saa:real_estate" not in body
    assert "30.00%" not in body
    # Nor the curated RSS tag inventory.
    for tag in _KNOWN_TAGS:
        assert f">{tag}<" not in body


# ---------------------------------------------------------------------------
# Calibration editor — save round-trip and the pinned-invariant refusal
# ---------------------------------------------------------------------------


def _calibration_form(**overrides: str) -> dict[str, str]:
    """Build a complete desired-effective-values post, defaults included.

    The editor posts every field, not only the changed ones — which is the
    shape ``save_calibration_revision`` takes, because reducing back to
    deviations is its job and must not be duplicated client-side.
    """
    form = {
        "warn_default_pct": "90.0",
        "band_boundary_0": "3",
        "band_boundary_1": "6",
        "options_min_band": "noteworthy",
        "floor_limit_breach": "7",
        "floor_limit_escalation": "5",
        "floor_all_clear": "1",
        "floor_rss_cluster": "1",
        "floor_price_trigger": "4",
        "floor_fx_trigger": "4",
        "floor_freshness_trigger": "3",
        "floor_liquidity_trigger": "6",
        "cap_internal": "10",
        "cap_rss": "3",
        "cap_limit_breach": "10",
        "cap_limit_escalation": "10",
        "cap_all_clear": "3",
        "cap_rss_cluster": "3",
        "cap_price_trigger": "10",
        "cap_fx_trigger": "10",
        "cap_freshness_trigger": "5",
        "cap_liquidity_trigger": "10",
        "re_trigger_delta_saa": "5.0",
        "re_trigger_delta_anlv": "5.0",
        "re_trigger_delta_rss": "0",
        "re_trigger_delta_price": "5.0",
        "re_trigger_delta_fx": "5.0",
        "re_trigger_delta_freshness": "5.0",
        "re_trigger_delta_liquidity": "5.0",
    }
    form.update(overrides)
    return form


async def test_calibration_save_round_trip_flips_the_provenance_markers(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A changed WARN default and floor are stored; the rest stay NULL."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/calibration",
        data=_calibration_form(
            warn_default_pct="85", floor_price_trigger="6", notes="Tightened price watch"
        ),
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "Calibration saved." in response.text

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT warn_default_pct, floor_price_trigger, floor_fx_trigger, "
                    "band_boundary_0, notes FROM floor_calibration"
                )
            )
        ).first()
    assert row is not None
    assert row.warn_default_pct == Decimal("85")
    assert row.floor_price_trigger == 6
    # Deviations only: fields left at their default are stored as NULL, so a
    # later change to a code default still reaches this tenant.
    assert row.floor_fx_trigger is None
    assert row.band_boundary_0 is None
    assert row.notes == "Tightened price watch"

    # The editor now shows the stored values as effective, marked customised.
    body = (await web_client.get("/api/watch-desk/calibration")).text
    assert 'name="warn_default_pct" value="85.000"' in body
    assert 'name="floor_price_trigger" value="6"' in body
    assert "customised" in body


async def test_calibration_save_rejects_a_pinned_invariant_inline(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Raising the upper band boundary past the breach floor is refused.

    ADR-0116 §7 invariant 2: a regulatory breach can never render below the
    critical band. The message is the service's own, verbatim.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/calibration",
        data=_calibration_form(band_boundary_1="8"),
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    body = response.text
    assert "never render below critical" in body
    assert "Nothing was saved." in body
    # The attempted value is reflected back so it can be corrected in place.
    assert 'name="band_boundary_1"' in body and 'value="8"' in body

    async with fresh_superuser_engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM floor_calibration"))).scalar_one()
    assert count == 0


async def test_calibration_save_refuses_a_posted_pinned_key(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """``fund_closure`` has no input; posting it directly is still refused.

    Passed through to the sanctioned write path rather than dropped —
    silently ignoring it would turn a rejected write into one that looks
    successful.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/calibration",
        data=_calibration_form(floor_fund_closure="8"),
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "pinned level" in response.text

    async with fresh_superuser_engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM floor_calibration"))).scalar_one()
    assert count == 0


async def test_calibration_save_requires_csrf(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.post("/api/watch-desk/calibration", data=_calibration_form())
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Sensitivity overlays — create → revise, rss mute-only, CSRF
# ---------------------------------------------------------------------------


async def test_overlay_create_then_revise_writes_two_versions(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """First touch creates the identity; the next edit revises it (§1)."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    created = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "saa:equities", "warn_threshold_pct": "75"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200
    # The whole monitor comes back, so the mark, the tags and the group
    # counts move together.
    assert 'id="dc-monitor"' in created.text

    revised = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={
            "subject_key": "saa:equities",
            "warn_threshold_pct": "65",
            "re_trigger_delta": "2.5",
            "muted": "on",
            "notes": "noisy while we rebalance",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert revised.status_code == 200

    async with fresh_superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT watchpoint_id, warn_threshold_pct, re_trigger_delta, "
                    "muted, notes FROM watchpoints WHERE subject_key = 'saa:equities' "
                    "ORDER BY effective_from"
                )
            )
        ).all()

    assert len(rows) == 2
    # Two immutable versions of ONE identity — never an updated row.
    assert rows[0].watchpoint_id == rows[1].watchpoint_id
    assert rows[0].warn_threshold_pct == Decimal("75")
    assert rows[0].muted is False
    assert rows[1].warn_threshold_pct == Decimal("65")
    assert rows[1].re_trigger_delta == Decimal("2.5")
    assert rows[1].muted is True
    assert rows[1].notes == "noisy while we rebalance"


async def test_overlay_editor_offers_rss_mute_only(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A press dimension is non-scalar, so it carries mute alone (§3)."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    editor = await web_client.get("/api/watch-desk/watchpoints/overlay?subject_key=rss%3Aequities")
    assert editor.status_code == 200
    assert 'name="muted"' in editor.text
    assert 'name="warn_threshold_pct"' not in editor.text
    assert 'name="re_trigger_delta"' not in editor.text

    saved = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "rss:equities", "muted": "on"},
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT family, muted, warn_threshold_pct FROM watchpoints "
                    "WHERE subject_key = 'rss:equities'"
                )
            )
        ).first()
    assert row is not None
    assert row.family == "rss"
    assert row.muted is True
    assert row.warn_threshold_pct is None


async def test_overlay_rejects_a_scalar_field_on_an_rss_subject(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The repository refuses it, and the schema would refuse it under that."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "rss:equities", "warn_threshold_pct": "75"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "mute only" in response.text


async def test_overlay_rejects_a_family_that_carries_no_overlay(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Only the derived families take a sensitivity overlay."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "price:abc", "muted": "on"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "carry no sensitivity overlay" in response.text


async def test_overlay_save_requires_csrf(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "saa:equities", "muted": "on"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Cadence settings — round-trip
# ---------------------------------------------------------------------------


async def test_cadence_round_trip_recomputes_next_due_at(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/cadence",
        data={
            "cadence": "daily",
            "preferred_hour": "9",
            "timezone": "Europe/Berlin",
            "enabled": "on",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "Cadence saved" in response.text

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT cadence, preferred_hour, timezone, enabled, "
                    "next_due_at FROM irene_schedule WHERE user_id IS NULL"
                )
            )
        ).first()
    assert row is not None
    assert row.cadence == "daily"
    assert row.preferred_hour == 9
    assert row.timezone == "Europe/Berlin"
    assert row.enabled is True
    # next_due_at recomputed via compute_next_due_at → the local hour equals
    # the preferred hour, at the top of the hour, in the future.
    local = row.next_due_at.astimezone(ZoneInfo("Europe/Berlin"))
    assert local.hour == 9
    assert local.minute == 0
    assert row.next_due_at > datetime.now(timezone.utc)


async def test_cadence_disable_persists(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=datetime.now(timezone.utc) + timedelta(days=1),
        enabled=True,
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    # Omitting the ``enabled`` checkbox disables the tenant.
    response = await web_client.post(
        "/api/watch-desk/cadence",
        data={
            "cadence": "daily",
            "preferred_hour": "8",
            "timezone": "Europe/Berlin",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    async with fresh_superuser_engine.connect() as conn:
        enabled = (
            await conn.execute(text("SELECT enabled FROM irene_schedule WHERE user_id IS NULL"))
        ).scalar_one()
    # A subsequent tick's find_due_tenants filters on ``enabled`` → skipped.
    assert enabled is False


async def test_cadence_bad_timezone_returns_422(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        "/api/watch-desk/cadence",
        data={
            "cadence": "daily",
            "preferred_hour": "8",
            "timezone": "Mars/Olympus_Mons",
            "enabled": "on",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "pf-dc-cadence__error" in response.text
    # No row written on a rejected save.
    async with fresh_superuser_engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM irene_schedule"))).scalar_one()
    assert count == 0


async def test_cadence_sub_daily_persists_and_renders_its_label(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A sub-daily cadence saves, schedules on its anchor grid, re-renders.

    The ADR-0119 §1 vocabulary is only real if the whole round trip
    accepts it: the router's choices, the shared ``compute_next_due_at``
    validation, and the panel's re-render. The label assertion is the
    §3 half — a ``|capitalize`` would put "Every_6h" in front of the
    operator, so the map is what the response must show.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        "/api/watch-desk/cadence",
        data={
            "cadence": "every_6h",
            "preferred_hour": "8",
            "timezone": "Europe/Berlin",
            "enabled": "on",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "Cadence saved" in response.text
    # Rendered through CADENCE_LABELS, and the raw value never leaks as a
    # label (it still appears as the option's ``value``, hence the check
    # on the capitalised form the old template would have produced).
    assert "Every 6 hours" in response.text
    assert "Every_6h" not in response.text
    # The re-render selects what was saved rather than falling back to daily.
    assert re.search(r'value="every_6h"\s+selected', response.text) is not None

    async with fresh_superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT cadence, preferred_hour, timezone, enabled, "
                    "next_due_at FROM irene_schedule WHERE user_id IS NULL"
                )
            )
        ).first()
    assert row is not None
    assert row.cadence == "every_6h"
    assert row.preferred_hour == 8
    assert row.timezone == "Europe/Berlin"
    assert row.enabled is True
    # next_due_at sits on the anchor grid (08:00 ± k·6h ⇒ 02, 08, 14, 20)
    # at the top of the hour, in the future.
    local = row.next_due_at.astimezone(ZoneInfo("Europe/Berlin"))
    assert (local.hour - 8) % 6 == 0
    assert local.minute == 0
    assert row.next_due_at > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_briefing_is_tenant_isolated(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    _id, email, password = seeded_user
    # A finding for the active tenant and one for a different tenant.
    await _seed_finding(
        fresh_superuser_engine,
        tenant_id=SENTINEL_TENANT_ID,
        subject_key="saa:mine",
        payload=_card_payload(trigger="mine", finding="f", basis="b"),
        urgency=8,
        band="critical",
    )
    await _seed_finding(
        fresh_superuser_engine,
        tenant_id=_OTHER_TENANT_ID,
        subject_key="saa:theirs",
        payload=_card_payload(trigger="theirs", finding="f", basis="b"),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    assert "saa:mine" in response.text
    assert "saa:theirs" not in response.text


# ---------------------------------------------------------------------------
# Empty / edge states across the refreshed area (DC5)
#
# The individual pieces are pinned above; these fill the gaps the refresh
# left open — the schedule-present-but-disabled distinction, the no-cadence
# operator hint after the DC4 rename, the never-ran states cohering in one
# response, and Calibration's independence from tenant data.
# ---------------------------------------------------------------------------


async def test_tiles_disabled_schedule_still_offers_request_analysis(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A disabled schedule is still a schedule — the button gates on existence.

    This pins the distinction the two button-gating tests above do not
    cover: "no schedule" hides the button, "schedule present but disabled"
    keeps it, because the operator can still enqueue an out-of-band beat.
    """
    _id, email, password = seeded_user
    # Future-dated so the tile renders a stamp, not the "due now" state.
    due = datetime.now(timezone.utc) + timedelta(days=2)
    await _seed_schedule(
        fresh_superuser_engine,
        next_due_at=due,
        enabled=False,
        timezone_name="Europe/Berlin",
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    berlin = due.astimezone(ZoneInfo("Europe/Berlin")).strftime("%a %H:%M")
    assert f"Next beat · {berlin} · daily · disabled" in body
    # The schedule row exists, so the action remains available.
    assert "Request analysis now" in body


async def test_request_analysis_without_cadence_points_at_calibration(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No schedule ⇒ the hint names Calibration, not the retired Watchlist."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    briefing = await web_client.get("/api/watch-desk/briefing")
    assert briefing.status_code == 200
    assert "Next beat · no cadence set" in briefing.text

    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        "/api/watch-desk/request-analysis",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    # DC4 renamed the section; the operator hint must follow it.
    assert "Calibration section" in response.text
    assert "Watchlist" not in response.text


async def test_never_ran_states_cohere_in_one_briefing(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """Fresh tenant: tile, calm feed and monitor agree in a single render.

    Each piece is tested in isolation above. This pins that they cohere in
    one response — a never-ran tile that makes no count claim, an
    affirmative calm feed, and a monitor that admits no beat has run — which
    is the exact trust case the "One Glass" refresh was built for.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # Tile: never-ran, no green dot, no "surfaced N".
    assert "Never ran yet" in body
    assert "surfaced" not in body
    assert 'class="dot"' not in body
    # Feed: affirmative calm, not an empty list.
    assert "All monitored limits are calm" in body
    assert "pf-dc-card" not in body
    # Monitor: renders, and claims no observation time (UI-024).
    assert "On watch" in body
    assert "Irene has not completed a beat yet" not in body
    assert "last saw these subjects at" not in body


async def test_calibration_renders_without_any_tenant_data(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """No limit sets, no schedule, no calibration row — still 200.

    An absent ``floor_calibration`` row means code defaults (ADR-0116 §7),
    which is the ordinary state and never an error: the editor renders the
    defaults as effective values, and the cadence panel falls back to its
    not-configured default alongside it.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/calibration")
    assert response.status_code == 200
    body = response.text

    assert 'id="dc-calibration-editor"' in body
    assert 'name="warn_default_pct" value="90.0"' in body
    # The cadence panel degrades to its default rather than erroring.
    assert 'id="dc-cadence-panel"' in body
    assert "Not yet configured." in body


# ---------------------------------------------------------------------------
# Open case (ADR-0107, C4) — the Watch Desk side of the Case workflow
# ---------------------------------------------------------------------------


async def _seed_case(
    app_engine: AsyncEngine,
    *,
    opened_by: UUID,
    title: str,
    opened_at: datetime,
    finding_id: UUID | None = None,
    close_note: str | None = None,
    closed_at: datetime | None = None,
):
    """Seed one case (optionally closed) through the repository under RLS."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        repo = CaseRepository(session)
        case = await repo.create(
            title=title,
            opened_by=opened_by,
            finding_id=finding_id,
            opened_actor="pm",
            now=opened_at,
        )
        if close_note is not None:
            await repo.close(
                case.id,
                closed_by=opened_by,
                closing_note=close_note,
                now=closed_at or (opened_at + timedelta(hours=1)),
            )
    return case


async def _one_case(engine: AsyncEngine) -> dict:
    """Return the single ``cases`` row and its ``opened`` entry payload."""
    async with engine.connect() as conn:
        case_row = (
            (await conn.execute(text("SELECT id, title, finding_id, opened_by FROM cases")))
            .mappings()
            .one()
        )
        opened = (
            (
                await conn.execute(
                    text(
                        "SELECT actor, payload::text AS payload FROM case_entries "
                        "WHERE case_id = :cid AND kind = 'opened'"
                    ),
                    {"cid": str(case_row["id"])},
                )
            )
            .mappings()
            .one()
        )
    return {
        "id": case_row["id"],
        "title": case_row["title"],
        "finding_id": case_row["finding_id"],
        "opened_actor": opened["actor"],
        "opened_lines": json.loads(opened["payload"])
        .get("materiality_at_opening", {})
        .get("lines", None),
    }


async def _resolution_of(engine: AsyncEngine, finding_id: UUID) -> str:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT resolution FROM irene_finding WHERE id = :id"),
                {"id": str(finding_id)},
            )
        ).scalar_one()


async def _case_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT COUNT(*) FROM cases"))).scalar_one()


# --- The Watch Desk resolve endpoint rejects the fifth resolution (decision 5) --


async def test_watch_desk_resolve_rejects_opened_case(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A posted ``opened_case`` is rejected — written only by open-case (C4)."""
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:reject_me",
        payload=_card_payload(trigger="x", finding="y", basis="z"),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/resolve",
        data={"resolution": "opened_case"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "pf-dc-error" in response.text
    # The row is untouched — still open.
    assert await _resolution_of(fresh_superuser_engine, fid) == "open"


# --- The open-case endpoint composes create + resolve, freezing materiality --


async def test_open_case_creates_case_with_frozen_live_materiality(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Open-case creates the case (title = headline, actor system) and freezes
    the *live* coverage — not the finding's stale basis."""
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id)  # saa:equities live at 24% of 25% WARN
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:equities",
        payload=_card_payload(
            trigger="Equities approaching the SAA ceiling",
            finding="Coverage is nearing the equities ceiling.",
            # Deliberately stale — the frozen lines must NOT echo this.
            basis="coverage 5.14% against a 5.00% ceiling",
            options=["Trim the most liquid position."],
        ),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    case = await _one_case(fresh_superuser_engine)
    assert response.headers["HX-Redirect"] == f"/cases/{case['id']}"

    # The case pre-fills its title from the finding headline, is a system
    # opening, and references the finding.
    assert case["title"] == "Equities approaching the SAA ceiling"
    assert case["opened_actor"] == "system"
    assert case["finding_id"] == fid

    # The finding is resolved as the fifth resolution.
    assert await _resolution_of(fresh_superuser_engine, fid) == "opened_case"

    # The frozen lines echo the LIVE figures (24% of 25%, WARN), never the
    # finding's stale 5.14%/5.00% basis.
    lines = case["opened_lines"]
    blob = " || ".join(lines)
    assert "24.00%" in blob
    assert "25.00%" in blob
    assert "WARN" in blob
    # The distinctive stale figure from the finding's basis is absent — the
    # lines are live, not the finding's frozen-at-finding-time basis.
    assert "5.14" not in blob


async def test_open_case_rss_freezes_band_and_evidence_not_numbers(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """An ``rss:*`` finding freezes the band line + evidence, nothing numeric.

    The band here is **synthetic**: the floor pins a standalone press cluster
    to ``informational`` (``cap[rss] <= band_boundaries[0]``, an invariant no
    calibration may relax), and since ADR-0120 §3 the endpoint refuses that
    band — so the ``rss:*`` freeze branch is no longer reachable from a beat-
    produced finding. Seeding ``noteworthy`` keeps the branch under test; the
    realistic RSS shape is covered by
    ``test_open_case_rejects_an_informational_finding``.
    """
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="rss:credit_stress:2026-07-20",
        payload={
            "trigger": "Credit-stress press cluster",
            "finding": "Several outlets flag widening credit spreads.",
            "evidence_refs": ["rss-bucket-credit_stress-2026-07-20"],
        },
        urgency=5,
        band="noteworthy",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    case = await _one_case(fresh_superuser_engine)
    lines = case["opened_lines"]
    blob = " || ".join(lines)
    assert "Noteworthy band at case opening." in lines
    assert "rss-bucket-credit_stress-2026-07-20" in blob
    # Nothing numeric — no ceiling / headroom / percentage figures.
    assert "%" not in blob
    assert "ceiling" not in blob


async def test_open_case_unresolvable_subject_freezes_empty_but_opens(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No live figure (empty universe) ⇒ freeze [], and still open the case."""
    _id, email, password = seeded_user
    # No investments seeded → coverage is unavailable for saa:equities.
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:equities",
        payload=_card_payload(
            trigger="Equities ceiling",
            finding="f",
            basis="coverage 5.14% against a 5.00% ceiling",
        ),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    case = await _one_case(fresh_superuser_engine)
    # The case opened; the frozen lines are empty (never invented).
    assert case["opened_lines"] == []
    assert await _resolution_of(fresh_superuser_engine, fid) == "opened_case"


# --- Atomicity: create + resolve happen together or neither (decision 1) -----


async def test_open_case_failed_resolve_rolls_back_the_case(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If resolve raises after create, no case row and no opened entry survive."""
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:atomic",
        payload=_card_payload(trigger="Atomic", finding="f", basis="b"),
        urgency=8,
        band="critical",
    )

    async def _boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("resolve failed after the case was created")

    monkeypatch.setattr(IreneFindingRepository, "resolve", _boom)

    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    # The endpoint does not swallow an unexpected DB-layer error (consistent
    # with the resolve endpoint); the ASGI transport re-raises it. What matters
    # is that ``tenant_context`` rolled the whole transaction back on the way
    # out — create and resolve are all-or-nothing.
    with pytest.raises(RuntimeError):
        await web_client.post(
            f"/api/watch-desk/findings/{fid}/open-case",
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
    # The whole transaction rolled back: no case, and the finding is still open.
    assert await _case_count(fresh_superuser_engine) == 0
    async with fresh_superuser_engine.connect() as conn:
        entries = (await conn.execute(text("SELECT COUNT(*) FROM case_entries"))).scalar_one()
    assert entries == 0
    assert await _resolution_of(fresh_superuser_engine, fid) == "open"


async def test_open_case_failed_create_leaves_finding_open(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If create raises, the finding is never resolved (the inverse of above)."""
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:atomic2",
        payload=_card_payload(trigger="Atomic2", finding="f", basis="b"),
        urgency=8,
        band="critical",
    )

    async def _boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("create failed")

    monkeypatch.setattr(CaseRepository, "create", _boom)

    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    with pytest.raises(RuntimeError):
        await web_client.post(
            f"/api/watch-desk/findings/{fid}/open-case",
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
    assert await _case_count(fresh_superuser_engine) == 0
    assert await _resolution_of(fresh_superuser_engine, fid) == "open"


# --- Feed removal, redirect, double-submit and the stale card ---------------


async def test_open_case_removes_card_and_second_post_redirects(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The card leaves the feed; a second post lands on the existing case."""
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:handover",
        payload=_card_payload(
            trigger="Hand this over",
            finding="f",
            basis="b",
            options=["Consider trimming."],
        ),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    first = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert first.status_code == 200
    case_id = (await _one_case(fresh_superuser_engine))["id"]
    assert first.headers["HX-Redirect"] == f"/cases/{case_id}"

    # The card is gone from the briefing (resolution moved it out of list_open).
    briefing = await web_client.get("/api/watch-desk/briefing")
    assert "saa:handover" not in briefing.text

    # A second post on the same finding redirects to the SAME case, no dup.
    second = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert second.status_code == 200
    assert second.headers["HX-Redirect"] == f"/cases/{case_id}"
    assert await _case_count(fresh_superuser_engine) == 1


async def test_open_case_stale_acted_finding_gets_calm_error(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A finding already resolved (acted) yields the calm error, no case."""
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:stale",
        payload=_card_payload(trigger="Stale", finding="f", basis="b"),
        urgency=8,
        band="critical",
        resolution="acted",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "pf-dc-error" in response.text
    assert "HX-Redirect" not in response.headers
    assert await _case_count(fresh_superuser_engine) == 0
    # Untouched — still acted.
    assert await _resolution_of(fresh_superuser_engine, fid) == "acted"


@pytest.mark.parametrize(
    "subject_key",
    ["anlv:quiet", "rss:credit_stress:2026-07-20"],
)
async def test_open_case_rejects_an_informational_finding(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    subject_key: str,
) -> None:
    """The band gate holds server-side, whatever a template rendered.

    ADR-0120 §3 (defence in depth): an informational finding is refused with
    the 422 inline error *before* any composition work — no case, no
    resolution, no frozen materiality. The ``rss:*`` case is parametrised in
    because the floor pins a standalone press cluster to this band, so that
    subject family now has no case path at all.
    """
    _id, email, password = seeded_user
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key=subject_key,
        payload=_card_payload(trigger="Quiet note", finding="f", basis="b"),
        urgency=2,
        band="informational",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "pf-dc-error" in response.text
    assert "HX-Redirect" not in response.headers
    # Nothing composed, and the finding is untouched — still open.
    assert await _case_count(fresh_superuser_engine) == 0
    assert await _resolution_of(fresh_superuser_engine, fid) == "open"


async def test_open_case_option_less_noteworthy_finding_composes_and_redirects(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The door ADR-0120 opens: no options, noteworthy band, case composed.

    The composition itself is untouched — create + ``opened_case`` + frozen
    live materiality in one transaction, then the HX-Redirect to the
    pre-filled case.
    """
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id)  # saa:equities live at 24% of 25% WARN
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:equities",
        payload=_card_payload(
            trigger="Equities approaching the SAA ceiling",
            finding="Coverage is nearing the equities ceiling.",
            basis="Coverage 24.00% of a 25.00% ceiling.",
        ),
        urgency=5,
        band="noteworthy",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    response = await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    case = await _one_case(fresh_superuser_engine)
    assert response.headers["HX-Redirect"] == f"/cases/{case['id']}"
    assert case["title"] == "Equities approaching the SAA ceiling"
    assert case["opened_actor"] == "system"
    assert case["finding_id"] == fid
    assert await _resolution_of(fresh_superuser_engine, fid) == "opened_case"
    # Materiality is frozen live, exactly as on a critical finding.
    blob = " || ".join(case["opened_lines"])
    assert "24.00%" in blob
    assert "25.00%" in blob
    assert "WARN" in blob


async def test_open_case_missing_finding_is_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """An unknown finding id is the 404 idiom."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    response = await web_client.post(
        f"/api/watch-desk/findings/{uuid4()}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 404


# --- Journal merge: resolved findings + closed cases, interleaved -----------


async def test_journal_merges_findings_and_closed_cases_newest_first(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    app_engine: AsyncEngine,
) -> None:
    """The Journal interleaves an opened_case hand-over with a closed case."""
    user_id, email, password = seeded_user

    # An older manually-closed case (no finding) — the Gate-C0 gap this closes.
    await _seed_case(
        app_engine,
        opened_by=user_id,
        title="Board preparation — Q3 review",
        opened_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        close_note="Prepared and circulated; nothing outstanding.",
        closed_at=datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc),
    )

    # A finding handed over to a case via the real endpoint (newest event).
    fid = await _seed_finding(
        fresh_superuser_engine,
        subject_key="saa:journal_handover",
        payload=_card_payload(
            trigger="Equities near ceiling",
            finding="f",
            basis="b",
            options=["Trim."],
        ),
        urgency=8,
        band="critical",
    )
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    await web_client.post(
        f"/api/watch-desk/findings/{fid}/open-case",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    # The case the finding was opened as (still open) — the hand-over target.
    async with fresh_superuser_engine.connect() as conn:
        opened_case_id = (
            await conn.execute(
                text("SELECT id FROM cases WHERE finding_id = :f"),
                {"f": str(fid)},
            )
        ).scalar_one()

    journal = await web_client.get("/api/watch-desk/journal")
    assert journal.status_code == 200
    body = journal.text

    # The opened_case finding row: "Opened case" label + tag class, links case.
    assert "saa:journal_handover" in body
    assert "Opened case" in body
    assert "pf-dc-res-tag--opened_case" in body
    assert f'href="/cases/{opened_case_id}"' in body

    # The closed-case row: badge, closer (email fallback), excerpt, link.
    assert "Board preparation — Q3 review" in body
    assert "dc-owner@example.com" in body
    assert "Prepared and circulated" in body
    assert "pf-dc-journal__row--case" in body

    # Newest first: the hand-over (now) precedes the closed case (12 Jul).
    assert body.index("saa:journal_handover") < body.index("Board preparation — Q3 review")


# ---------------------------------------------------------------------------
# Watchpoint add flows and the Calibration watchpoint list (ADR-0116 §6, §7)
# ---------------------------------------------------------------------------


async def _seed_priceable_investment(actor_id: UUID, *, name: str = "MSCI World ETF") -> UUID:
    """Seed one active investment carrying a market identifier.

    The price picker's admission rule, mirrored from the seeder (ADR-0116
    §8): an investment with at least one identifier is one the platform can
    price. Without the identifier the instrument is correctly absent from
    the form.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="listed_equity", display_name="Listed Equity"
            )
            investment = await InvestmentRepository(session).create(
                name=name,
                investment_type="listed_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
            )
            await InvestmentIdentifierRepository(session).add(
                investment_id=investment.id,
                scheme="isin",
                value="IE00B4L5Y983",
                created_by=actor_id,
            )
            return investment.id
    finally:
        await engine.dispose()


async def _live_watchpoints(engine: AsyncEngine, family: str) -> list:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT watchpoint_id, subject_key, display_name, drop_pct, "
                    "move_pct, window_days, max_age_days, retired, effective_from "
                    "FROM watchpoints WHERE family = :f ORDER BY effective_from"
                ),
                {"f": family},
            )
        ).all()


async def test_price_form_lists_only_market_identified_instruments(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The seeding precedent, applied to the picker (ADR-0116 §8)."""
    user_id, email, password = seeded_user
    await _seed_priceable_investment(user_id)
    # A second investment with no identifier: nothing can price it, so
    # offering it would be offering a permanent "no data" subject.
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="private_equity", display_name="Private Equity"
            )
            await InvestmentRepository(session).create(
                name="Alpha Fund II",
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=user_id,
            )
    finally:
        await engine.dispose()

    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/watchpoints/new?family=price")

    assert response.status_code == 200
    assert "MSCI World ETF" in response.text
    assert "Alpha Fund II" not in response.text
    assert 'name="drop_pct"' in response.text
    assert 'name="window_days"' in response.text


async def test_creating_a_price_watchpoint_shows_it_on_the_monitor(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Live derivation: the new row is on the monitor in the same response."""
    user_id, email, password = seeded_user
    investment_id = await _seed_priceable_investment(user_id)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    created = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={
            "family": "price",
            "instrument_id": str(investment_id),
            "drop_pct": "5",
            "window_days": "5",
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert created.status_code == 200
    assert 'id="dc-monitor"' in created.text
    # The group is there with the instrument's own name, derived live.
    assert "Price moves" in created.text
    assert "Price decline — MSCI World ETF" in created.text
    # No prices in the window yet ⇒ an honest no-data row, never a calm one.
    assert "No data" in created.text
    assert "no price observations at all" in created.text
    # The subject key the seeder would have written for the same instrument.
    assert f"price:{investment_id}" in created.text

    rows = await _live_watchpoints(fresh_superuser_engine, "price")
    assert len(rows) == 1
    assert rows[0].subject_key == f"price:{investment_id}"
    assert rows[0].drop_pct == Decimal("5")
    assert rows[0].window_days == 5


async def test_creating_an_fx_watchpoint_and_the_pair_format_refusal(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The repository owns the pair rule; the route renders it inline."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    rejected = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={
            "family": "fx",
            "currency_pair": "USDEUR",
            "move_pct": "3",
            "window_days": "5",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 422
    assert "BASE/QUOTE" in rejected.text
    assert not await _live_watchpoints(fresh_superuser_engine, "fx")

    accepted = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={
            "family": "fx",
            "currency_pair": "usd/eur",
            "move_pct": "3",
            "window_days": "5",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert accepted.status_code == 200
    rows = await _live_watchpoints(fresh_superuser_engine, "fx")
    assert len(rows) == 1
    # Upper-cased on the way in, so the subject key matches the seeder's.
    assert rows[0].subject_key == "fx:USD/EUR"
    assert rows[0].display_name == "FX move USD/EUR"


async def test_a_second_singleton_is_refused_inline(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """One *live* identity per singleton family (ADR-0116 §4)."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    first = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "120"},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200

    second = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "90"},
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 422
    assert "singleton" in second.text
    assert len(await _live_watchpoints(fresh_superuser_engine, "freshness")) == 1


async def test_an_overlay_family_cannot_be_added_as_a_watchpoint(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The asymmetry: saa/anlv/rss subjects are never *defined* here."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    form = await web_client.get("/api/watch-desk/watchpoints/new?family=saa")
    assert form.status_code == 422
    assert "not defined by a watchpoint" in form.text

    posted = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "saa", "drop_pct": "5"},
        headers={"X-CSRF-Token": csrf},
    )
    assert posted.status_code == 422
    assert not await _live_watchpoints(fresh_superuser_engine, "saa")


async def test_revising_a_watchpoint_writes_a_second_version(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Immutable versions of one identity — never an updated row."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "120"},
        headers={"X-CSRF-Token": csrf},
    )
    watchpoint_id = (await _live_watchpoints(fresh_superuser_engine, "freshness"))[0].watchpoint_id

    editor = await web_client.get(f"/api/watch-desk/watchpoints/{watchpoint_id}/edit")
    assert editor.status_code == 200
    assert 'value="120"' in editor.text
    # A singleton says so: editing one row edits the rule for the book.
    assert "One rule for the whole book" in editor.text

    revised = await web_client.post(
        f"/api/watch-desk/watchpoints/{watchpoint_id}/revise",
        data={
            "family": "freshness",
            "max_age_days": "90",
            "muted": "on",
            "return_to": "list",
            "notes": "tightened after the Q2 lag",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert revised.status_code == 200
    assert 'id="dc-watchpoint-list"' in revised.text

    rows = await _live_watchpoints(fresh_superuser_engine, "freshness")
    assert len(rows) == 2
    assert rows[0].watchpoint_id == rows[1].watchpoint_id
    assert rows[0].max_age_days == 120
    assert rows[1].max_age_days == 90


async def test_the_calibration_list_renders_every_family_and_its_parameters(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Signal parameters in native language; overlays as sensitivity."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "liquidity", "horizon_months": "12", "min_coverage_ratio": "1.2"},
        headers={"X-CSRF-Token": csrf},
    )
    await web_client.post(
        "/api/watch-desk/watchpoints/overlay",
        data={"subject_key": "saa:equities", "warn_threshold_pct": "75"},
        headers={"X-CSRF-Token": csrf},
    )

    calibration = await web_client.get("/api/watch-desk/calibration")
    assert calibration.status_code == 200
    body = calibration.text

    assert 'id="dc-watchpoint-list"' in body
    # The operator's own ratio and horizon — never the internal 100-scale.
    assert "1.20× cover over 12 months" in body
    assert "liquidity:cash_coverage" in body
    # The overlay row states what it is: sensitivity over a derived subject.
    assert "saa:equities" in body
    assert "WARN at 75%" in body
    # No add affordance in the list — adding happens on the monitor.
    assert "+ Add watchpoint" not in body


async def test_retire_moves_the_row_and_keeps_the_history(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Retirement is a version, not a deletion (ADR-0116 §1)."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "120"},
        headers={"X-CSRF-Token": csrf},
    )
    watchpoint_id = (await _live_watchpoints(fresh_superuser_engine, "freshness"))[0].watchpoint_id

    retired = await web_client.post(
        f"/api/watch-desk/watchpoints/{watchpoint_id}/retire",
        headers={"X-CSRF-Token": csrf},
    )
    assert retired.status_code == 200
    assert "retired — show" in retired.text

    rows = await _live_watchpoints(fresh_superuser_engine, "freshness")
    assert len(rows) == 2
    assert rows[1].retired is True

    # The freed slot: the singleton rule is about *live* identities.
    again = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "60"},
        headers={"X-CSRF-Token": csrf},
    )
    assert again.status_code == 200


async def test_history_renders_every_version_newest_first(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The historised rows *are* the story — no diff engine."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "120"},
        headers={"X-CSRF-Token": csrf},
    )
    watchpoint_id = (await _live_watchpoints(fresh_superuser_engine, "freshness"))[0].watchpoint_id
    await web_client.post(
        f"/api/watch-desk/watchpoints/{watchpoint_id}/revise",
        data={"family": "freshness", "max_age_days": "90"},
        headers={"X-CSRF-Token": csrf},
    )

    history = await web_client.get(f"/api/watch-desk/watchpoints/{watchpoint_id}/history")
    assert history.status_code == 200
    body = history.text

    assert "NAV no older than 90 days" in body
    assert "NAV no older than 120 days" in body
    # Newest first.
    assert body.index("90 days") < body.index("120 days")


async def test_the_watchpoint_endpoints_require_csrf(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    unknown = uuid4()

    created = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "freshness", "max_age_days": "120"},
    )
    assert created.status_code == 403

    revised = await web_client.post(
        f"/api/watch-desk/watchpoints/{unknown}/revise",
        data={"family": "freshness", "max_age_days": "120"},
    )
    assert revised.status_code == 403

    retired = await web_client.post(f"/api/watch-desk/watchpoints/{unknown}/retire")
    assert retired.status_code == 403


# ---------------------------------------------------------------------------
# "Subjects watched" — the tile counts everything the desk watches (P7)
# ---------------------------------------------------------------------------
#
# The tile sits directly above the monitor and answers the same question, so
# these tests read *both* out of one rendered Briefing and compare them. That
# is the guarantee worth having: not "the tile is 19" but "the tile is what
# the group headers below it add up to", which stays true when the fixture,
# the families or the book change.


#: The subjects tile, anchored on its own label — the other three tiles carry
#: markup inside their value span and must not be matched by accident.
_SUBJECTS_TILE = re.compile(
    r"Subjects watched</span>\s*"
    r'<span class="pf-dc-tile__value">([^<]+)</span>\s*'
    r'<span class="pf-dc-tile__sub">([^<]+)</span>'
)

#: Every monitor group header stating a subject count — the two quota groups
#: and whichever signal families rendered. The press group states "N curated
#: tags" instead, so it is read separately rather than pattern-matched into
#: the same bucket.
_GROUP_SUBJECTS = re.compile(r'class="pf-dc-group__meta">\s*(\d+) subjects?\b')
_GROUP_TAGS = re.compile(r'class="pf-dc-group__meta">\s*(\d+) curated tags\b')


def _subjects_tile(body: str) -> tuple[str, str]:
    """Return the subjects tile's ``(value, sub)`` strings."""
    match = _SUBJECTS_TILE.search(body)
    assert match is not None, "the subjects-watched tile did not render"
    return match.group(1).strip(), match.group(2).strip()


def _tile_total(body: str) -> int:
    """Sum the subjects tile's ``A + B [+ C]`` breakdown."""
    value, _sub = _subjects_tile(body)
    return sum(int(part) for part in value.split("+"))


def _monitor_subject_total(body: str) -> int:
    """Sum what every monitor group header below the tile says it watches."""
    counts = [int(figure) for figure in _GROUP_SUBJECTS.findall(body)]
    tags = [int(figure) for figure in _GROUP_TAGS.findall(body)]
    assert counts, "the monitor rendered no subject-count headers"
    assert tags, "the monitor rendered no press group"
    return sum(counts) + sum(tags)


async def _make_priceable(actor_id: UUID, *, name: str) -> UUID:
    """Give one already-seeded investment a market identifier.

    The picker admits an investment carrying at least one identifier
    (ADR-0116 §8). Attaching one to a position that is *already valued*
    keeps the coverage engine's Stichtag intact — seeding a second,
    NAV-less investment would cost it, and with it the quota groups.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            book = await InvestmentRepository(session).list_active()
            investment = next(row for row in book if row.name == name)
            await InvestmentIdentifierRepository(session).add(
                investment_id=investment.id,
                scheme="isin",
                value="IE00B4L5Y983",
                created_by=actor_id,
            )
            return investment.id
    finally:
        await engine.dispose()


async def _arm_every_signal_family(client: AsyncClient, csrf: str, *, instrument_id: UUID) -> None:
    """Create one watchpoint of each defined family, through the write path."""
    for payload in (
        {
            "family": "price",
            "instrument_id": str(instrument_id),
            "drop_pct": "5",
            "window_days": "5",
        },
        {"family": "fx", "currency_pair": "USD/EUR", "move_pct": "3", "window_days": "5"},
        {"family": "freshness", "max_age_days": "120"},
        {"family": "liquidity", "horizon_months": "12", "min_coverage_ratio": "1.2"},
    ):
        created = await client.post(
            "/api/watch-desk/watchpoints", data=payload, headers={"X-CSRF-Token": csrf}
        )
        assert created.status_code == 200, created.text


async def test_the_subjects_tile_equals_the_monitors_group_headers(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The no-drift guarantee, stated once: tile == sum of the group headers.

    With all four signal families armed the tile is the whole answer to
    "what does the Watch Desk watch", and an operator can add up the group
    headers underneath and land on the same number. Before P7 they could
    not: the tile stopped at limits and press and understated the monitor
    it sits above.
    """
    user_id, email, password = seeded_user
    await _seed_valued_universe(user_id)
    instrument_id = await _make_priceable(user_id, name="Alpha")
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)
    await _arm_every_signal_family(web_client, csrf, instrument_id=instrument_id)

    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    # The fixture is only worth comparing against if every family rendered.
    for group in ("Price moves", "FX moves", "NAV freshness", "Cash coverage"):
        assert group in body

    assert _tile_total(body) == _monitor_subject_total(body)

    # The figures the equality is made of, so a coincidental match cannot
    # pass it: two constrained limit rows, the curated tags, and five signal
    # subjects — 1 price + 1 fx + 1 liquidity singleton + 2 freshness, the
    # latter enumerated one per active investment.
    value, sub = _subjects_tile(body)
    assert value == f"2 + {len(_KNOWN_TAGS)} + 5"
    assert sub == f"2 internal limits · {len(_KNOWN_TAGS)} press dimensions · 5 signal subjects"


async def test_a_signal_free_tenant_reads_the_tile_unchanged(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No signal watchpoints ⇒ exactly the pre-P7 tile, not a "+ 0" term.

    A tenant who watches no signal family gains a clause that states
    nothing. The term is omitted rather than rendered at zero.
    """
    user_id, email, password = seeded_user
    await _seed_limit_set(
        fresh_superuser_engine,
        tenant_id=SENTINEL_TENANT_ID,
        created_by=user_id,
        family="saa",
        class_keys={"equity": 40.0, "credit": 60.0},
    )
    await _login(web_client, email, password)
    response = await web_client.get("/api/watch-desk/briefing")
    assert response.status_code == 200
    body = response.text

    value, sub = _subjects_tile(body)
    assert value == f"2 + {len(_KNOWN_TAGS)}"
    assert sub == f"2 internal limits · {len(_KNOWN_TAGS)} press dimensions"
    assert "signal subject" not in body


async def test_a_muted_and_a_no_data_subject_are_both_counted(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Two pinned rules on one subject: muted counts, no-data counts.

    A muted subject is watched — the monitor's own header counts it
    (ADR-0116 §3), and a tile that dropped it would contradict the row
    immediately below. A subject the producer refused to measure is watched
    too: "cannot be evaluated today" is not "nobody is looking".
    """
    user_id, email, password = seeded_user
    instrument_id = await _seed_priceable_investment(user_id)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    created = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={
            "family": "price",
            "instrument_id": str(instrument_id),
            "drop_pct": "5",
            "window_days": "5",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200

    watchpoint_id = (await _live_watchpoints(fresh_superuser_engine, "price"))[0].watchpoint_id
    muted = await web_client.post(
        f"/api/watch-desk/watchpoints/{watchpoint_id}/revise",
        data={"drop_pct": "5", "window_days": "5", "muted": "on"},
        headers={"X-CSRF-Token": csrf},
    )
    assert muted.status_code == 200

    body = (await web_client.get("/api/watch-desk/briefing")).text
    # Nothing has priced the instrument, so the one subject is a no-data
    # row — and a muted one on top of that. Both states, still watched.
    assert "No data" in body
    assert "1 muted" in body
    assert "1 subject ·" in body

    value, sub = _subjects_tile(body)
    assert value == f"0 + {len(_KNOWN_TAGS)} + 1"
    assert sub.endswith(f"· {len(_KNOWN_TAGS)} press dimensions · 1 signal subject")
    assert _tile_total(body) == _monitor_subject_total(body)


async def test_a_retired_identity_leaves_the_count(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Retirement stops evaluation, rendering and counting alike.

    The tile inherits that for free: a retired identity is absent from the
    resolution the monitor's groups were built from, so it is absent from
    the figure they are summed into.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    created = await web_client.post(
        "/api/watch-desk/watchpoints",
        data={"family": "fx", "currency_pair": "USD/EUR", "move_pct": "3", "window_days": "5"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200
    watchpoint_id = (await _live_watchpoints(fresh_superuser_engine, "fx"))[0].watchpoint_id

    before = (await web_client.get("/api/watch-desk/briefing")).text
    assert _subjects_tile(before)[0] == f"0 + {len(_KNOWN_TAGS)} + 1"
    assert _tile_total(before) == _monitor_subject_total(before)

    retired = await web_client.post(
        f"/api/watch-desk/watchpoints/{watchpoint_id}/retire",
        headers={"X-CSRF-Token": csrf},
    )
    assert retired.status_code == 200

    after = (await web_client.get("/api/watch-desk/briefing")).text
    assert _subjects_tile(after)[0] == f"0 + {len(_KNOWN_TAGS)}"
    # The family is named in the footer as unwatched, never as a live group.
    assert "— not watched:" in after
    assert _tile_total(after) == _monitor_subject_total(after)
