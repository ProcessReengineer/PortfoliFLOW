# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the presentation-archetype resolver (ADR-0082 §1).

Pure mapping tests: every canonical ``investment_type`` resolves to the
intended archetype, and both ``other`` and any unknown value fall
through to NAV-only.
"""

from __future__ import annotations

import pytest

from services.investments.archetype import Archetype, resolve_archetype


@pytest.mark.parametrize(
    ("investment_type", "expected"),
    [
        ("private_equity", Archetype.CAPITAL_ACCOUNT),
        ("private_debt", Archetype.CAPITAL_ACCOUNT),
        ("real_estate", Archetype.CAPITAL_ACCOUNT),
        ("infra_equity", Archetype.CAPITAL_ACCOUNT),
        ("listed_equity", Archetype.TOTAL_RETURN_EQUITY),
        ("listed_bonds", Archetype.FIXED_INCOME),
        ("other", Archetype.NAV_ONLY),
    ],
)
def test_resolve_archetype_maps_canonical_types(investment_type: str, expected: Archetype) -> None:
    assert resolve_archetype(investment_type) is expected


@pytest.mark.parametrize(
    "investment_type",
    ["", "OTHER", "hedge_fund", "crypto", "listed_equityy", "infra_debt"],
)
def test_resolve_archetype_unknown_falls_through_to_nav_only(
    investment_type: str,
) -> None:
    """Any value outside the canonical seven routes to NAV-only."""
    assert resolve_archetype(investment_type) is Archetype.NAV_ONLY


def test_archetype_is_str_enum() -> None:
    """The members are string-valued for direct template / JSON use."""
    assert Archetype.FIXED_INCOME == "fixed_income"
    assert {a.value for a in Archetype} == {
        "capital_account",
        "total_return_equity",
        "fixed_income",
        "nav_only",
    }
