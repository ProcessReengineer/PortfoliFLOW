# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for the b023 identifier scheme-set swap (ADR-0096).

Exercises the b023 migration's reversibility as a **CHECK-constraint swap**
(no table create/drop): from ``head`` it downgrades to b022 (narrowing
``ck_investment_identifiers_scheme`` to the ADR-0090 five-scheme set) then
``upgrade head`` again, asserting the constraint definition narrows and then
widens back to the ADR-0096 seven-scheme set.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment a
newer one lands — it then asserts against someone else's schema. Naming
b023's own ``down_revision`` keeps the guard honest for every future head.

The downgrade narrows a value set, so it only succeeds while no
``preqin`` / ``pitchbook`` row exists — this round-trip inserts none, so the
narrowing is clean (the failure-on-existing-rows behaviour is documented in the
migration and is the accepted stance for a value-set-narrowing downgrade). The
test restores the DB to ``head`` in a ``finally`` so that a mid-test failure
cannot strand the schema.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards, so this
guard's downgrade never reaches the shared dev database (see
``tests/regression/conftest.py`` for why that matters — this guard is a case in
point, since a real ``preqin`` row in the dev database would fail the narrowing
by design). The Alembic CLI still runs in a fresh subprocess, so it does not
contend with this test's own connection. If the server is unreachable the test
skips, matching the sibling migration guards.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_CONSTRAINT = "ck_investment_identifiers_scheme"
_PROVIDER_SCHEMES = ("preqin", "pitchbook")
#: The revision immediately below b023 — i.e. b023's own ``down_revision``.
_BELOW = "b022_add_market_data_schedule"


async def _scheme_check_def(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": _CONSTRAINT},
        )
        value = result.scalar_one_or_none()
    assert value is not None, f"{_CONSTRAINT} missing"
    return value


async def test_b023_scheme_check_swap_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head (b023
    # applied), so the CHECK already names the provider-native schemes.
    at_head = await _scheme_check_def(scratch_superuser_engine)
    for scheme in _PROVIDER_SCHEMES:
        assert scheme in at_head, (
            f"{scheme!r} missing from CHECK before round-trip — is the DB at head?\n{at_head}"
        )

    try:
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"

        # After downgrade the CHECK is narrowed back to the five-scheme set.
        narrowed = await _scheme_check_def(scratch_superuser_engine)
        for scheme in _PROVIDER_SCHEMES:
            assert scheme not in narrowed, (
                f"{scheme!r} still present in CHECK after downgrade to {_BELOW}\n{narrowed}"
            )
        assert "isin" in narrowed and "internal" in narrowed

        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"

        # After re-upgrade the provider-native schemes are back.
        restored = await _scheme_check_def(scratch_superuser_engine)
        for scheme in _PROVIDER_SCHEMES:
            assert scheme in restored, (
                f"{scheme!r} missing from CHECK after re-upgrade to head\n{restored}"
            )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
