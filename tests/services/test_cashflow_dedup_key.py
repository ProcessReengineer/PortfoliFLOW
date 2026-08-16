# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the pure cashflow dedup key (ADR-0092).

Covers the canonicalisation rules the module documents, exercised as
explicit behaviour:

* determinism and SHA-256 hex shape;
* ``source=None`` vs ``source=''`` vs a real value are three distinct keys;
* Decimal canonicalisation — ``10`` / ``10.0`` / ``1E+1`` collapse to the
  **same** key (numerically-equal amounts dedup), while ``10`` and ``10.01``
  do not;
* timezone normalisation — a naive-UTC and an explicit-UTC stamp for the
  same instant collide, and a different zone for the same instant collides.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID

from services.investments.cashflow_dedup_key import compute_cashflow_dedup_key

_INV = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TS = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _key(**overrides) -> str:
    kwargs = dict(
        investment_id=_INV,
        flow_timestamp=_TS,
        flow_type="dividend",
        flow_kind="actual",
        amount=Decimal("10.00"),
        source="synthetic",
    )
    kwargs.update(overrides)
    return compute_cashflow_dedup_key(**kwargs)


def test_deterministic_hex_shape() -> None:
    assert _key() == _key()
    k = _key()
    assert len(k) == 64
    assert all(c in "0123456789abcdef" for c in k)


def test_source_none_empty_and_value_are_three_distinct_keys() -> None:
    k_none = _key(source=None)
    k_empty = _key(source="")
    k_value = _key(source="synthetic")
    assert len({k_none, k_empty, k_value}) == 3


def test_decimal_equal_amounts_collapse_to_same_key() -> None:
    """10, 10.0, 1E+1 are numerically equal → identical dedup key."""
    k_int = _key(amount=Decimal("10"))
    k_one_dp = _key(amount=Decimal("10.0"))
    k_exp = _key(amount=Decimal("1E+1"))
    k_four_dp = _key(amount=Decimal("10.0000"))
    assert k_int == k_one_dp == k_exp == k_four_dp


def test_decimal_different_amounts_differ() -> None:
    assert _key(amount=Decimal("10")) != _key(amount=Decimal("10.01"))
    # Sign matters — a −10 call is not a +10 distribution.
    assert _key(amount=Decimal("10")) != _key(amount=Decimal("-10"))


def test_naive_timestamp_assumed_utc_collides_with_explicit_utc() -> None:
    naive = datetime(2024, 6, 1, 12, 0)  # no tzinfo — assumed UTC
    assert _key(flow_timestamp=naive) == _key(flow_timestamp=_TS)


def test_same_instant_other_zone_collides() -> None:
    """The same instant expressed in another zone canonicalises to UTC."""
    berlin = timezone(timedelta(hours=2))
    same_instant = datetime(2024, 6, 1, 14, 0, tzinfo=berlin)  # == 12:00Z
    assert _key(flow_timestamp=same_instant) == _key(flow_timestamp=_TS)


def test_different_instant_differs() -> None:
    other = datetime(2024, 6, 1, 13, 0, tzinfo=timezone.utc)
    assert _key(flow_timestamp=other) != _key(flow_timestamp=_TS)


# ---------------------------------------------------------------------------
# ADR-0103 §5: the eighth flow type participates without a module change
# ---------------------------------------------------------------------------


def test_investor_flow_participates_without_a_module_change() -> None:
    """``investor_flow`` keys stably and distinctly — no enumeration inside.

    ADR-0103 §5 asserts that "the rule-based dedup key already composes over
    ``flow_type``; the new member participates without mechanical change".
    That claim holds because the key-former treats ``flow_type`` as an opaque
    canonical string and never enumerates its members — this test is what
    documents it. No change to
    :mod:`services.investments.cashflow_dedup_key` accompanies b028.
    """
    key = _key(flow_type="investor_flow")
    # Stable: the same logical investor flow always hashes identically.
    assert key == _key(flow_type="investor_flow")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)

    # Distinct from every one of the seven pre-existing types on an
    # otherwise-identical row: an investor flow is never confused for a
    # distribution of the same amount on the same day.
    seven = [
        "capital_call",
        "distribution",
        "fee",
        "carry",
        "dividend",
        "coupon",
        "other",
    ]
    others = {_key(flow_type=ft) for ft in seven}
    assert key not in others
    assert len(others) == len(seven)

    # And the kind still participates: a plan investor flow is not the
    # actual one (the §6 plan path must not dedup against the book).
    assert _key(flow_type="investor_flow", flow_kind="plan") != key
