# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the cash unity-price constraint (ADR-0103 §1).

ADR-0103 §1 pins a cash position's prices as **stored** rows of exactly one,
one per statement date, in the position currency. Two writers owe that
constraint — the Cash-sheet importer (§3/§4) and the ADR-0100-row migration
(§9) — and neither exists yet. This guard pins the definition they will
consume, so the contract cannot drift under them between now and then.

Four properties are machine-enforced on
:mod:`services.investments.unity_price`:

1. **Purity of the source.** The module imports nothing beyond the standard
   library — no DB session, no ORM model, no FastAPI, no provider SDK. The
   migration must be able to consult it from inside Alembic and the importer
   from inside a transform, so it cannot need a database to state itself.
   Mirrors ``tests/regression/test_flow_type_invariants_pure.py``.

2. **The value is one — at the stored scale.** Representation is irrelevant
   (``1``, ``1.0``, ``1.0000`` and ``1.00000000`` are the same number, and
   the ADR's "1.0000" is the column's ``Numeric(20, 8)`` unity); any
   deviation the column could actually record is a violation. Decimal
   throughout — a float would defeat the exactness the constraint exists for.

3. **The scale matches the column.** :data:`PRICE_SCALE` tracks
   ``instrument_prices.price``. If migration bNNN ever restates that column,
   this assertion fails rather than letting the quantisation silently
   disagree with the database.

4. **The currency matches the position** — ADR-0097 §5 restated for cash. A
   unity price in a foreign currency is a 1:1 FX conversion hidden in the
   write path, which ADR-0099 forbids outright.

Pure — no DB, no network.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from core.models.instrument_price import InstrumentPrice
from services.investments.unity_price import (
    PRICE_SCALE,
    UNITY_PRICE,
    is_unity_price,
    unity_price_violation,
)

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_UNITY_MODULE: Path = _REPO_ROOT / "services" / "investments" / "unity_price.py"

# Tokens that would betray a DB / web / LLM / network dependency inside the
# pure constraint module. None may appear as an import in its source.
_FORBIDDEN_IMPORT_TOKENS: tuple[str, ...] = (
    "sqlalchemy",
    "AsyncSession",
    "core.repositories",
    "core.models",
    "fastapi",
    "PyQt6",
    "AsyncOpenAI",
    "openai",
    "anthropic",
    "httpx",
    "requests",
)

#: Spellings of one that the column stores identically. All are unity.
_UNITY_SPELLINGS: tuple[Decimal, ...] = (
    Decimal("1"),
    Decimal("1.0"),
    Decimal("1.0000"),
    Decimal("1.00000000"),
    Decimal(1),
)

#: Values a cash price must never take — each one restates the balance.
_NON_UNITY: tuple[Decimal, ...] = (
    Decimal("0.9999"),
    Decimal("1.0001"),
    Decimal("1.00001"),
    Decimal("0"),
    Decimal("100"),
    Decimal("-1"),
)


def test_unity_module_imports_only_stdlib() -> None:
    """No import line in the constraint module names a DB / web / net dep."""
    offenders: list[str] = []
    for raw in _UNITY_MODULE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for tok in _FORBIDDEN_IMPORT_TOKENS:
            if tok in stripped:
                offenders.append(stripped)
    assert not offenders, (
        "ADR-0103 §1: the unity-price constraint must be statable without a "
        "database — the §9 migration consults it from inside Alembic and the "
        f"§3 importer from inside a transform; found forbidden import(s): {offenders}."
    )


def test_price_scale_matches_the_instrument_prices_column() -> None:
    """The quantisation scale tracks ``instrument_prices.price``."""
    column_type = InstrumentPrice.__table__.c.price.type
    assert column_type.scale == PRICE_SCALE, (
        "unity_price.PRICE_SCALE has drifted from the instrument_prices.price "
        f"column (Numeric(_, {column_type.scale})) — the unity comparison "
        "would no longer agree with what the database stores."
    )


def test_unity_price_is_one_at_the_stored_scale() -> None:
    """The constant is one, spelled at the column's scale."""
    assert Decimal(1) == UNITY_PRICE
    # ADR-0103 §1 writes it "1.0000"; the column holds eight fractional
    # digits. Same number, and the constant carries the column's scale.
    assert Decimal("1.0000") == UNITY_PRICE
    assert -UNITY_PRICE.as_tuple().exponent == PRICE_SCALE


def test_every_spelling_of_one_is_unity() -> None:
    """Representation-equivalence: numerically equal ones all pass."""
    for price in _UNITY_SPELLINGS:
        assert unity_price_violation(price, "EUR", "EUR") is None, (
            f"{price!r} is one and must satisfy the ADR-0103 §1 constraint."
        )
        assert is_unity_price(price, "EUR", "EUR") is True


def test_non_unity_prices_are_violations() -> None:
    """Any price the column could record other than one is refused."""
    for price in _NON_UNITY:
        reason = unity_price_violation(price, "EUR", "EUR")
        assert reason is not None, (
            f"{price!r} is not one and must violate the ADR-0103 §1 "
            "constraint — a cash NAV is holdings × price, so any other price "
            "would silently restate the statement balance."
        )
        assert "exactly" in reason
        assert is_unity_price(price, "EUR", "EUR") is False


def test_currency_mismatch_is_a_violation() -> None:
    """ADR-0097 §5 restated for cash: no 1:1 conversion in the write path."""
    reason = unity_price_violation(UNITY_PRICE, "USD", "EUR")
    assert reason is not None
    assert "USD" in reason and "EUR" in reason
    assert not is_unity_price(UNITY_PRICE, "USD", "EUR")


def test_violation_messages_are_english_sentences() -> None:
    """The returned strings are operator-facing copy (ADR-0008)."""
    messages = [
        unity_price_violation(Decimal("1.5"), "EUR", "EUR"),
        unity_price_violation(UNITY_PRICE, "USD", "EUR"),
    ]
    for message in messages:
        assert message is not None
        assert message.strip() == message
        assert len(message) > 20
        assert message[0].isupper()
        assert message.endswith(".")
