# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CaseAttachmentRepository tests against the live compose Postgres.

The ``case_attachments`` table stores file bytes in-database (BYTEA),
tenant-scoped and RLS-policed (ADR-0107 §7 / ADR-0035). These tests pin the
byte round-trip, the metadata-only reads (no ``content`` on the list DTO),
the count, the closed-case guard, and cross-tenant invisibility.

Coverage
--------
* CA-01: ``content`` round-trips byte-exact; the DTO omits ``content``.
* CA-02: ``list_for_case`` carries metadata only and is ordered; ``count``
  counts.
* CA-03: a closed (or missing) case rejects ``create``.
* CA-04: cross-tenant invisibility (RLS smoke).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import CaseClosedError, CaseStateInvalid
from core.repositories import UserRepository, tenant_context
from core.repositories.case_attachment_repository import (
    CaseAttachmentRepository,
)
from core.repositories.case_repository import CaseRepository

_T0 = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


async def _seed_user(app_engine: AsyncEngine, tenant_id, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


# ---------------------------------------------------------------------------
# CA-01: byte-exact content round-trip; the metadata DTO omits content
# ---------------------------------------------------------------------------


async def test_ca01_content_round_trips_exactly(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CA-01")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ca01.example")
    # 1 KiB spanning every byte value, NULs and high bytes included.
    blob = bytes(range(256)) * 4

    async with tenant_context(app_engine, tenant_id) as session:
        case = await CaseRepository(session).create(
            title="With attachment",
            opened_by=user_id,
            opened_actor="pm",
            now=_T0,
        )
    async with tenant_context(app_engine, tenant_id) as session:
        att = await CaseAttachmentRepository(session).create(
            case.id,
            filename="doc.pdf",
            mime_type="application/pdf",
            size_bytes=len(blob),
            sha256="deadbeef",
            content=blob,
            uploaded_by=user_id,
            now=_T0,
        )

    assert not hasattr(att, "content")  # the metadata DTO never carries bytes
    assert att.filename == "doc.pdf"
    assert att.size_bytes == len(blob)

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await CaseAttachmentRepository(session).get_with_content(att.id)
    assert fetched is not None
    dto, content = fetched
    assert content == blob  # byte-exact
    assert dto.id == att.id
    assert not hasattr(dto, "content")


# ---------------------------------------------------------------------------
# CA-02: list carries metadata only, ordered; count counts
# ---------------------------------------------------------------------------


async def test_ca02_list_and_count(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CA-02")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ca02.example")

    async with tenant_context(app_engine, tenant_id) as session:
        case = await CaseRepository(session).create(
            title="Multi", opened_by=user_id, opened_actor="pm", now=_T0
        )
        arepo = CaseAttachmentRepository(session)
        await arepo.create(
            case.id,
            filename="a.pdf",
            mime_type="application/pdf",
            size_bytes=2,
            sha256="h1",
            content=b"aa",
            uploaded_by=user_id,
            now=_T0,
        )
        await arepo.create(
            case.id,
            filename="b.png",
            mime_type="image/png",
            size_bytes=3,
            sha256="h2",
            content=b"bbb",
            uploaded_by=user_id,
            now=_T0 + timedelta(minutes=1),
        )
        listed = await arepo.list_for_case(case.id)
        count = await arepo.count_for_case(case.id)

    assert count == 2
    assert [a.filename for a in listed] == ["a.pdf", "b.png"]  # created_at asc
    for a in listed:
        assert not hasattr(a, "content")


# ---------------------------------------------------------------------------
# CA-03: a closed (or missing) case rejects create
# ---------------------------------------------------------------------------


async def test_ca03_closed_or_missing_case_rejects_create(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CA-03")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ca03.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Closing", opened_by=user_id, opened_actor="pm", now=_T0)
        await repo.close(
            case.id,
            closed_by=user_id,
            closing_note="done",
            now=_T0 + timedelta(hours=1),
        )

    async with tenant_context(app_engine, tenant_id) as session:
        arepo = CaseAttachmentRepository(session)
        with pytest.raises(CaseClosedError):
            await arepo.create(
                case.id,
                filename="late.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                sha256="h",
                content=b"x",
                uploaded_by=user_id,
                now=_T0 + timedelta(hours=2),
            )
        with pytest.raises(CaseStateInvalid):
            await arepo.create(
                uuid4(),
                filename="ghost.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                sha256="h",
                content=b"x",
                uploaded_by=user_id,
                now=_T0,
            )


# ---------------------------------------------------------------------------
# CA-04: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_ca04_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@a-ca.example")

    async with tenant_context(app_engine, tenant_a) as session:
        case = await CaseRepository(session).create(
            title="A-doc", opened_by=user_a, opened_actor="pm", now=_T0
        )
        att = await CaseAttachmentRepository(session).create(
            case.id,
            filename="secret.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="h",
            content=b"S",
            uploaded_by=user_a,
            now=_T0,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        arepo = CaseAttachmentRepository(session)
        assert await arepo.get_with_content(att.id) is None
        assert await arepo.list_for_case(case.id) == []
        assert await arepo.count_for_case(case.id) == 0
        count = await session.execute(text("SELECT count(*) FROM case_attachments"))
        assert count.scalar_one() == 0
