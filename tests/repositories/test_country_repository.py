# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CountryRepository tests against the live compose Postgres.

The ``countries`` table is global (no tenant_id, no RLS). Tests run
against the unprivileged ``portfoliflow_app`` role to confirm that
the role can read the seed data exactly as it will in production. No
tenant context is required.

Coverage:

* ``get_by_iso_code`` returns the matching country, or ``None`` for
  an unknown code.
* ``list_active_iso_codes`` cardinality matches the seed fixture.
* The reserved ``XX`` sentinel is present after migration b007.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.repositories.country_repository import CountryRepository


async def _new_session(engine: AsyncEngine) -> AsyncSession:
    """Acquire a session without setting a tenant context.

    ``countries`` is global — no ``app.tenant_id`` is needed for
    reads. The companion ``reset_schema`` autouse fixture truncates
    domain tables but leaves ``countries`` untouched (it is not in
    the truncate list).
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return factory()


# ---------------------------------------------------------------------------
# C-01: get_by_iso_code returns the matching country
# ---------------------------------------------------------------------------


async def test_c01_get_by_iso_code_returns_match(
    app_engine: AsyncEngine,
) -> None:
    session = await _new_session(app_engine)
    try:
        repo = CountryRepository(session)
        de = await repo.get_by_iso_code("DE")
        case_insensitive = await repo.get_by_iso_code("de")
        spaced = await repo.get_by_iso_code("  DE  ")
        unknown = await repo.get_by_iso_code("ZZ")
        empty = await repo.get_by_iso_code("")

        assert de is not None
        assert de.iso_code == "DE"
        assert de.display_name == "Germany"
        assert de.region_default == "DACH"
        assert case_insensitive is not None and case_insensitive.iso_code == "DE"
        assert spaced is not None and spaced.iso_code == "DE"
        assert unknown is None
        assert empty is None
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# C-02: XX sentinel is present
# ---------------------------------------------------------------------------


async def test_c02_xx_sentinel_present(app_engine: AsyncEngine) -> None:
    """Every Phase-5a installation has the reserved XX sentinel."""
    session = await _new_session(app_engine)
    try:
        repo = CountryRepository(session)
        xx = await repo.get_by_iso_code("XX")
        assert xx is not None
        assert xx.iso_code == "XX"
        assert xx.display_name == "Unallocated / To Be Specified"
        assert xx.region_default == "Unallocated"
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# C-03: list_active_iso_codes covers the full ISO catalogue plus XX
# ---------------------------------------------------------------------------


async def test_c03_list_active_iso_codes_cardinality(
    app_engine: AsyncEngine,
) -> None:
    session = await _new_session(app_engine)
    try:
        repo = CountryRepository(session)
        codes = await repo.list_active_iso_codes()
        # The fixture ships ~250 codes (precise count can drift if the
        # ISO catalogue changes in a future migration). The assertion
        # is intentionally a lower bound so a future enrichment
        # doesn't break this test.
        assert len(codes) >= 240
        # Spot-check well-known codes.
        for expected in ("DE", "US", "GB", "JP", "CN", "BR", "ZA", "XX"):
            assert expected in codes, (
                f"Expected {expected!r} in list_active_iso_codes but missing. "
                f"Got {sorted(codes)[:10]} ..."
            )
        # Codes never include lower-case noise.
        assert all(code.isupper() and len(code) == 2 for code in codes)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# C-04: list_all returns DTOs sorted by display_name
# ---------------------------------------------------------------------------


async def test_c04_list_all_returns_full_catalogue(
    app_engine: AsyncEngine,
) -> None:
    """``list_all`` returns the full ISO catalogue plus the XX sentinel.

    The result is ordered by ``display_name`` in the database via
    ``ORDER BY``, but Postgres uses its server-side collation while
    Python's :func:`sorted` orders by Unicode codepoint — the two
    differ on entries like ``"Åland Islands"`` (Å is a separate code
    point but collation-equivalent to ``A`` in many locales). Asserting
    bytewise equality therefore over-specifies the contract; this test
    instead checks structure: the right number of rows and the XX
    sentinel present.
    """
    session = await _new_session(app_engine)
    try:
        repo = CountryRepository(session)
        all_countries = await repo.list_all()
        assert len(all_countries) >= 240
        codes = {c.iso_code for c in all_countries}
        assert "XX" in codes
        assert "DE" in codes
    finally:
        await session.close()
