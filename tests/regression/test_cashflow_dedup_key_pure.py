# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the cashflow dedup key is formed by a rule, never a model.

ADR-0092 rests the live cashflow idempotency on a **deterministic**,
rule-based dedup key — the natural companion to Irene's RSS key-forming
guard (``tests/regression/test_irene_key_forming_pure.py``). This test
machine-enforces two properties of
:mod:`services.investments.cashflow_dedup_key`:

1. **Purity of the source.** The module imports nothing beyond the
   standard library — no SQLAlchemy / DB session, no LLM / network client,
   no repository or ORM model. A key formed from a DB row or a model call
   would be neither reproducible nor auditable.

2. **Determinism.** The same logical cashflow always hashes to the same
   key, regardless of keyword ordering; and every identity field
   participates (changing any one changes the key).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from services.investments.cashflow_dedup_key import compute_cashflow_dedup_key

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_KEY_MODULE: Path = _REPO_ROOT / "services" / "investments" / "cashflow_dedup_key.py"

# Tokens that would betray a DB / LLM / network / model dependency inside
# the pure key-former. None may appear as an import in its source.
_FORBIDDEN_IMPORT_TOKENS: tuple[str, ...] = (
    "sqlalchemy",
    "AsyncSession",
    "core.repositories",
    "core.models",
    "AsyncOpenAI",
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "openrouter",
    "OpenRouter",
)

_UUID = UUID("11111111-1111-1111-1111-111111111111")
_TS = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _base_key() -> str:
    return compute_cashflow_dedup_key(
        investment_id=_UUID,
        flow_timestamp=_TS,
        flow_type="dividend",
        flow_kind="actual",
        amount=Decimal("10.50"),
        source="synthetic",
    )


def test_key_module_imports_only_stdlib() -> None:
    """No import line in the key module names a DB / LLM / network dep."""
    offenders: list[str] = []
    for raw in _KEY_MODULE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for tok in _FORBIDDEN_IMPORT_TOKENS:
            if tok in stripped:
                offenders.append(stripped)
    assert not offenders, (
        "ADR-0092: the cashflow dedup key-former must import only the "
        f"standard library; found forbidden import(s): {offenders}."
    )


def test_key_is_deterministic_and_kwarg_order_independent() -> None:
    """Same input → same key; keyword ordering is irrelevant (it is a hash)."""
    first = _base_key()
    # Re-invoke with kwargs in a scrambled order.
    second = compute_cashflow_dedup_key(
        source="synthetic",
        amount=Decimal("10.50"),
        flow_kind="actual",
        flow_type="dividend",
        flow_timestamp=_TS,
        investment_id=_UUID,
    )
    assert first == second
    assert len(first) == 64  # SHA-256 hex
    assert all(c in "0123456789abcdef" for c in first)


def test_every_identity_field_participates_in_the_key() -> None:
    """Changing any one field changes the key (no field is ignored)."""
    base = _base_key()
    variants = [
        compute_cashflow_dedup_key(
            investment_id=UUID("22222222-2222-2222-2222-222222222222"),
            flow_timestamp=_TS,
            flow_type="dividend",
            flow_kind="actual",
            amount=Decimal("10.50"),
            source="synthetic",
        ),
        compute_cashflow_dedup_key(
            investment_id=_UUID,
            flow_timestamp=datetime(2024, 6, 2, 12, 0, tzinfo=timezone.utc),
            flow_type="dividend",
            flow_kind="actual",
            amount=Decimal("10.50"),
            source="synthetic",
        ),
        compute_cashflow_dedup_key(
            investment_id=_UUID,
            flow_timestamp=_TS,
            flow_type="coupon",
            flow_kind="actual",
            amount=Decimal("10.50"),
            source="synthetic",
        ),
        compute_cashflow_dedup_key(
            investment_id=_UUID,
            flow_timestamp=_TS,
            flow_type="dividend",
            flow_kind="plan",
            amount=Decimal("10.50"),
            source="synthetic",
        ),
        compute_cashflow_dedup_key(
            investment_id=_UUID,
            flow_timestamp=_TS,
            flow_type="dividend",
            flow_kind="actual",
            amount=Decimal("11.50"),
            source="synthetic",
        ),
        compute_cashflow_dedup_key(
            investment_id=_UUID,
            flow_timestamp=_TS,
            flow_type="dividend",
            flow_kind="actual",
            amount=Decimal("10.50"),
            source="yahoo",
        ),
    ]
    for v in variants:
        assert v != base
    # All six variants are also distinct from one another.
    assert len(set(variants)) == len(variants)
