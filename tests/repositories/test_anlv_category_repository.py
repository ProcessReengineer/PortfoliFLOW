# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AnlVCategoryRepository tests against the live compose Postgres.

Same shape as ``test_country_repository.py``. The ``anlv_categories``
table is global (no tenant_id, no RLS); tests run against the
unprivileged ``portfoliflow_app`` role with no tenant context.

Coverage
--------
* AC-01: ``list_all`` returns rows ordered by ``sort_order``.
* AC-02: ``get_by_code`` returns the expected row for a known code,
  case-insensitively, and ``None`` for an unknown code.
* AC-03: ``list_codes`` returns the full catalogue set.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.repositories.anlv_category_repository import AnlVCategoryRepository


async def _new_session(engine: AsyncEngine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return factory()


# ---------------------------------------------------------------------------
# AC-01: list_all is sorted by sort_order
# ---------------------------------------------------------------------------


async def test_ac01_list_all_returns_rows_sorted_by_sort_order(
    app_engine: AsyncEngine,
) -> None:
    session = await _new_session(app_engine)
    try:
        repo = AnlVCategoryRepository(session)
        rows = await repo.list_all()
    finally:
        await session.close()

    assert rows, "anlv_categories should be seeded by migration b010"
    sort_orders = [r.sort_order for r in rows]
    assert sort_orders == sorted(sort_orders), "list_all() must return rows ordered by sort_order"


# ---------------------------------------------------------------------------
# AC-02: get_by_code resolves known codes and reports unknown codes as None
# ---------------------------------------------------------------------------


async def test_ac02_get_by_code_known_and_unknown(
    app_engine: AsyncEngine,
) -> None:
    session = await _new_session(app_engine)
    try:
        repo = AnlVCategoryRepository(session)
        anlv_13 = await repo.get_by_code("anlv_13")
        anlv_13_upper = await repo.get_by_code("ANLV_13")
        unknown = await repo.get_by_code("anlv_999")
        empty = await repo.get_by_code("")
    finally:
        await session.close()

    assert anlv_13 is not None
    assert anlv_13.code == "anlv_13"
    # Corrected to the § 2 Abs. 1 Nr. 13 statute label (ADR-0083): Nr. 13
    # is *Beteiligungen* (nicht notierte Anteile + geschlossene PE-AIF),
    # not the old mislabelled "Unternehmensbeteiligungen".
    assert anlv_13.display_name == "Beteiligungen (nicht notierte Anteile und geschlossene PE-AIF)"
    assert anlv_13.paragraph_label.startswith("§ 2 Abs. 1 Nr. 13")
    assert anlv_13_upper is not None and anlv_13_upper.code == "anlv_13"
    assert unknown is None
    assert empty is None


# ---------------------------------------------------------------------------
# AC-03: list_codes covers the full catalogue
# ---------------------------------------------------------------------------


async def test_ac03_list_codes_cardinality(
    app_engine: AsyncEngine,
) -> None:
    session = await _new_session(app_engine)
    try:
        repo = AnlVCategoryRepository(session)
        codes = await repo.list_codes()
    finally:
        await session.close()

    # The v21 testdata references 13, 14, 15; the fixture installs
    # every numbered category referenced by the v21 limit-set sheet
    # (1, 4, 8, 13, 14, 15, 16, 17) plus the rest of § 2 Abs. 1.
    for expected in (
        "anlv_1",
        "anlv_4",
        "anlv_8",
        "anlv_13",
        "anlv_14",
        "anlv_15",
        "anlv_16",
        "anlv_17",
    ):
        assert expected in codes, f"Expected {expected!r} in list_codes"
    # ADR-0083 correction: the catalogue now holds 20 codes — the 18
    # numbered § 2 Abs. 1 categories plus the two regulatory buckets
    # anlv_oeffnungsklausel (§ 2 Abs. 2) and anlv_genehmigung (§ 2 Abs. 3).
    assert "anlv_oeffnungsklausel" in codes
    assert "anlv_genehmigung" in codes
    # The fabricated anlv_19 ("Edelmetalle", no statutory Nr. 19) is gone.
    assert "anlv_19" not in codes
    assert len(codes) >= 19
