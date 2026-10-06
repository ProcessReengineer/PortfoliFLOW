# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``irene_briefing`` and ``watch_family_scope`` CHECKs, proven in SQL.

Every insert here goes through **raw SQL**, bypassing
:class:`~core.repositories.irene_briefing_repository.IreneBriefingRepository`
and :class:`~core.repositories.watch_family_scope_repository.WatchFamilyScopeRepository`.
The repositories validate the same rules and would reject these rows with
a named field, but ADR-0133 places the guarantee in the schema (migration
b036). A test through the repositories would prove the repositories work,
not that the schema does.

The rules under test:

* ``irene_briefing`` — the status vocabulary; ``skip_reason`` present
  exactly when skipped and from its vocabulary; both texts present exactly
  when written (``text_watch = ''`` is a present text); one row per tenant
  and ``beat_at``.
* ``watch_family_scope`` — the seven-family vocabulary (no ``pacing``);
  the preset vocabulary (``custom`` is never stored, D-WD-21); no preset
  on ``rss`` (D-WD-13); one revision per tenant, family and
  ``effective_from``; ``changed_by`` names a real user.

Each rule gets both directions: a well-formed row is accepted (so a CHECK
that rejected everything would fail the test rather than pass it), and
each rejection asserts the constraint by name, so a row refused by the
wrong rule does not count. Every rejected row violates exactly one rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import UserRepository, tenant_context

_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

_FAMILIES = ("saa", "anlv", "rss", "price", "fx", "freshness", "liquidity")

_INSERT_BRIEFING = text(
    """
    INSERT INTO irene_briefing (
        tenant_id, beat_at, written_at, status, skip_reason,
        text_markets, text_watch, inputs, finding_ids, model_id
    ) VALUES (
        :tenant_id, :beat_at, :written_at, :status, :skip_reason,
        :text_markets, :text_watch, CAST(:inputs AS jsonb),
        CAST(:finding_ids AS uuid[]), NULL
    )
    """
)

_INSERT_SCOPE = text(
    """
    INSERT INTO watch_family_scope (
        tenant_id, family, effective_from, enabled, preset, changed_by, note
    ) VALUES (
        :tenant_id, :family, :effective_from, :enabled, :preset, :changed_by, NULL
    )
    """
)

_WRITTEN: dict[str, object] = {
    "status": "written",
    "skip_reason": None,
    "text_markets": "Markets were calm.",
    "text_watch": "Nothing on the watch.",
}


async def _insert_briefing(
    engine: AsyncEngine, tenant_id: UUID, *, beat_at: datetime = _NOW, **fields: object
) -> None:
    """Insert one raw briefing row, RLS-scoped like the application."""
    payload = {
        "tenant_id": tenant_id,
        "beat_at": beat_at,
        "written_at": beat_at + timedelta(minutes=1),
        "inputs": json.dumps({"families": []}),
        "finding_ids": [uuid4()],
        **fields,
    }
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(_INSERT_BRIEFING, payload)


async def _insert_scope(
    engine: AsyncEngine,
    tenant_id: UUID,
    changed_by: UUID,
    family: str,
    *,
    enabled: bool = False,
    preset: str | None = None,
    effective_from: datetime = _NOW,
) -> None:
    """Insert one raw scope revision, RLS-scoped like the application."""
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(
            _INSERT_SCOPE,
            {
                "tenant_id": tenant_id,
                "family": family,
                "effective_from": effective_from,
                "enabled": enabled,
                "preset": preset,
                "changed_by": changed_by,
            },
        )


async def _seed_user(app_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    """Create the user a scope revision names as ``changed_by``."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email=f"scope-{tenant_id}@example.test", password_hash="x" * 8
        )
    return user.id


# ---------------------------------------------------------------------------
# irene_briefing — positive controls.
# ---------------------------------------------------------------------------


async def test_written_briefing_with_empty_watch_text_is_accepted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``text_watch = ''`` is a present text: every family switched off."""
    tenant_id = await seed_tenant("IB-CHECK-WRITTEN")
    await _insert_briefing(app_engine, tenant_id, **{**_WRITTEN, "text_watch": ""})


@pytest.mark.parametrize("reason", ["llm_error", "invalid_output", "nothing_to_write"])
async def test_skipped_briefing_is_accepted_for_each_reason(
    app_engine: AsyncEngine, seed_tenant, reason: str
) -> None:
    tenant_id = await seed_tenant(f"IB-CHECK-SKIP-{reason}"[:40])
    await _insert_briefing(
        app_engine,
        tenant_id,
        status="skipped",
        skip_reason=reason,
        text_markets=None,
        text_watch=None,
    )


# ---------------------------------------------------------------------------
# irene_briefing — rejections, each by its named constraint.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        pytest.param(
            {"status": "draft", "skip_reason": None, "text_markets": None, "text_watch": None},
            "ck_irene_briefing_status_vocabulary",
            id="unknown-status",
        ),
        pytest.param(
            {**_WRITTEN, "skip_reason": "llm_error"},
            "ck_irene_briefing_skip_reason_iff_skipped",
            id="written-with-skip-reason",
        ),
        pytest.param(
            {"status": "skipped", "skip_reason": None, "text_markets": None, "text_watch": None},
            "ck_irene_briefing_skip_reason_iff_skipped",
            id="skipped-without-reason",
        ),
        pytest.param(
            {
                "status": "skipped",
                "skip_reason": "timeout",
                "text_markets": None,
                "text_watch": None,
            },
            "ck_irene_briefing_skip_reason_vocabulary",
            id="unknown-reason",
        ),
        pytest.param(
            {**_WRITTEN, "text_watch": None},
            "ck_irene_briefing_texts_iff_written",
            id="written-missing-text-watch",
        ),
        pytest.param(
            {**_WRITTEN, "text_markets": None},
            "ck_irene_briefing_texts_iff_written",
            id="written-missing-text-markets",
        ),
        pytest.param(
            {
                "status": "skipped",
                "skip_reason": "llm_error",
                "text_markets": "Stray text.",
                "text_watch": None,
            },
            "ck_irene_briefing_texts_iff_written",
            id="skipped-carrying-text",
        ),
    ],
)
async def test_malformed_briefing_is_rejected(
    app_engine: AsyncEngine, seed_tenant, fields: dict[str, object], constraint: str
) -> None:
    tenant_id = await seed_tenant("IB-CHECK-REJECT")
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_briefing(app_engine, tenant_id, **fields)
    assert constraint in str(excinfo.value)


async def test_second_briefing_for_the_same_beat_is_rejected(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("IB-CHECK-DUP")
    await _insert_briefing(app_engine, tenant_id, **_WRITTEN)
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_briefing(app_engine, tenant_id, **_WRITTEN)
    assert "uq_irene_briefing_tenant_beat_at" in str(excinfo.value)


# ---------------------------------------------------------------------------
# watch_family_scope — positive controls.
# ---------------------------------------------------------------------------


async def test_every_family_accepts_a_revision_enabled_and_disabled(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WS-CHECK-OK")
    user_id = await _seed_user(app_engine, tenant_id)
    for family in _FAMILIES:
        await _insert_scope(app_engine, tenant_id, user_id, family, enabled=False)
        await _insert_scope(
            app_engine,
            tenant_id,
            user_id,
            family,
            enabled=True,
            effective_from=_NOW + timedelta(hours=1),
        )


async def test_preset_on_a_scalar_family_is_accepted(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WS-CHECK-PRESET")
    user_id = await _seed_user(app_engine, tenant_id)
    await _insert_scope(app_engine, tenant_id, user_id, "saa", enabled=True, preset="standard")


# ---------------------------------------------------------------------------
# watch_family_scope — rejections, each by its named constraint.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "preset", "constraint"),
    [
        pytest.param("pacing", None, "ck_watch_family_scope_family_vocabulary", id="pacing"),
        pytest.param("nav", None, "ck_watch_family_scope_family_vocabulary", id="unknown-family"),
        pytest.param(
            "saa", "custom", "ck_watch_family_scope_preset_vocabulary", id="custom-preset"
        ),
        pytest.param(
            "rss", "standard", "ck_watch_family_scope_rss_has_no_preset", id="rss-with-preset"
        ),
    ],
)
async def test_malformed_scope_revision_is_rejected(
    app_engine: AsyncEngine,
    seed_tenant,
    family: str,
    preset: str | None,
    constraint: str,
) -> None:
    tenant_id = await seed_tenant("WS-CHECK-REJECT")
    user_id = await _seed_user(app_engine, tenant_id)
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_scope(app_engine, tenant_id, user_id, family, preset=preset)
    assert constraint in str(excinfo.value)


async def test_second_revision_at_the_same_instant_is_rejected(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("WS-CHECK-DUP")
    user_id = await _seed_user(app_engine, tenant_id)
    await _insert_scope(app_engine, tenant_id, user_id, "fx")
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_scope(app_engine, tenant_id, user_id, "fx", enabled=True)
    assert "uq_watch_family_scope_tenant_family_effective_from" in str(excinfo.value)


async def test_changed_by_must_name_a_user(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("WS-CHECK-FK")
    with pytest.raises(IntegrityError) as excinfo:
        await _insert_scope(app_engine, tenant_id, uuid4(), "saa")
    assert "watch_family_scope_changed_by_fkey" in str(excinfo.value)
