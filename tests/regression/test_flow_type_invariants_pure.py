# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the overlay exemption invariant (ADR-0103 §5).

ADR-0103 §5 makes the exemption invariant **binding and regression-testable**:
*no scenario transformation and no TA transformation ever creates, deletes,
re-paces, or re-scales an investor flow.* ADR-0104 §2 owes the enforcement in
the overlay executors; this guard pins the definition they must enforce
against, so the invariant cannot silently change under them.

Three properties are machine-enforced on
:mod:`services.investments.flow_type_invariants`:

1. **Purity of the source.** The module imports nothing beyond the standard
   library — no DB session, no FastAPI, no LLM / network client, no provider
   SDK. An invariant that needed a database to state itself could not be
   consulted inside a pure overlay executor, which is precisely where
   ADR-0104 must consult it. Mirrors
   ``tests/regression/test_cashflow_dedup_key_pure.py``.

2. **Membership pin.** The exempt set is exactly ``{'investor_flow'}``.
   Widening it is an ADR-level act, not an implementation detail — this
   assertion is what makes that true in practice.

3. **Cross-set consistency.** Every exempt member is a flow type the schema
   actually knows: it appears in the ``ck_investment_cashflows_flow_type``
   CHECK on the ORM model *and* in the web layer's ``_VALID_FLOW_TYPES``.
   The invariant can never name a flow type the database would reject.
   Mirrors the three-sources-of-truth idiom of
   ``tests/regression/test_identifier_scheme_set_consistency.py``.

Pure — no DB, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.models.investment_cashflow import InvestmentCashflow
from services.investments.flow_type_invariants import (
    OVERLAY_EXEMPT_FLOW_TYPES,
    is_overlay_exempt,
)
from web.routes.investments import _VALID_FLOW_TYPES

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_INVARIANTS_MODULE: Path = _REPO_ROOT / "services" / "investments" / "flow_type_invariants.py"

# Tokens that would betray a DB / web / LLM / network dependency inside the
# pure invariants module. None may appear as an import in its source.
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

#: The ADR-0103 §5 exempt set, written out explicitly so the guard holds an
#: independent third opinion rather than comparing the live constant to
#: itself.
_EXPECTED_EXEMPT: frozenset[str] = frozenset({"investor_flow"})

#: The eight-member ADR-0103 flow-type set, likewise stated independently.
_EXPECTED_FLOW_TYPES: frozenset[str] = frozenset(
    {
        "capital_call",
        "distribution",
        "fee",
        "carry",
        "dividend",
        "coupon",
        "other",
        "investor_flow",
    }
)


def _model_check_flow_types() -> frozenset[str]:
    """Extract the flow-type members of the ORM model's CHECK constraint."""
    for constraint in InvestmentCashflow.__table__.constraints:
        if constraint.name == "ck_investment_cashflows_flow_type":
            return frozenset(re.findall(r"'([a-z_]+)'", str(constraint.sqltext)))
    raise AssertionError(
        "ck_investment_cashflows_flow_type not found on the InvestmentCashflow "
        "model — has the constraint been renamed?"
    )


def test_invariants_module_imports_only_stdlib() -> None:
    """No import line in the invariants module names a DB / web / net dep."""
    offenders: list[str] = []
    for raw in _INVARIANTS_MODULE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for tok in _FORBIDDEN_IMPORT_TOKENS:
            if tok in stripped:
                offenders.append(stripped)
    assert not offenders, (
        "ADR-0103 §5: the overlay-exemption invariant must be statable "
        "without a database or a web framework — the ADR-0104 executors "
        f"consult it in pure code; found forbidden import(s): {offenders}."
    )


def test_exempt_set_is_exactly_investor_flow() -> None:
    """The exempt set is pinned: widening it is an ADR-level act."""
    assert OVERLAY_EXEMPT_FLOW_TYPES == _EXPECTED_EXEMPT
    assert is_overlay_exempt("investor_flow") is True
    # Every other flow type is portfolio behaviour, and an overlay may
    # legitimately re-pace or re-scale it.
    for flow_type in _EXPECTED_FLOW_TYPES - _EXPECTED_EXEMPT:
        assert is_overlay_exempt(flow_type) is False, (
            f"{flow_type!r} must remain overlay-transformable — only the "
            "investor's own flows are exempt (ADR-0103 §5)."
        )


def test_model_check_carries_the_eight_flow_types() -> None:
    """The ORM CHECK names exactly the eight ADR-0103 flow types."""
    assert _model_check_flow_types() == _EXPECTED_FLOW_TYPES


def test_web_valid_flow_types_match_the_model_check() -> None:
    """The route's accept-set and the schema's CHECK do not drift."""
    assert _model_check_flow_types() == _VALID_FLOW_TYPES
    assert _VALID_FLOW_TYPES == _EXPECTED_FLOW_TYPES


def test_every_exempt_flow_type_is_a_known_flow_type() -> None:
    """The invariant can never name a flow type the schema does not know."""
    model_types = _model_check_flow_types()
    unknown_to_schema = OVERLAY_EXEMPT_FLOW_TYPES - model_types
    assert not unknown_to_schema, (
        "OVERLAY_EXEMPT_FLOW_TYPES names flow type(s) the "
        "ck_investment_cashflows_flow_type CHECK does not allow: "
        f"{sorted(unknown_to_schema)}."
    )
    unknown_to_web = OVERLAY_EXEMPT_FLOW_TYPES - _VALID_FLOW_TYPES
    assert not unknown_to_web, (
        "OVERLAY_EXEMPT_FLOW_TYPES names flow type(s) the web layer rejects: "
        f"{sorted(unknown_to_web)}."
    )
