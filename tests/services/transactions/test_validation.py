# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure deviation-ratio tests — no database (ADR-0128, MD-20).

:mod:`services.transactions.validation` carries two ratios that look alike
and answer different questions. :func:`price_deviation_ratio` is the
*magnitude* the 5 % execution-price warning thresholds against (ADR-0128
Q-4), and :func:`signed_deviation_ratio` is the *direction-carrying* figure
the reported flows show as context — proceeds against the last reported NAV
on R-SEC-SELL, price against the acquired NAV on R-SEC-BUY. MD-20 makes the
second one information and never judgement, so the sign is the whole point:
a secondary that sold below NAV is ordinary economics, and the surface says
by how much rather than whether that was wise.

The identity between them is asserted directly. A signed ratio whose
magnitude ever disagreed with the warning's would put two different numbers
for one comparison on one screen.

Coverage
--------
* TV-01: the sign — below, above, and exactly at the reference.
* TV-02: a zero reference is ``None`` on both, for the same reason.
* TV-03: ``abs(signed) == unsigned``, over both sides of the reference.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.transactions.validation import price_deviation_ratio, signed_deviation_ratio


# ---------------------------------------------------------------------------
# TV-01: the sign
# ---------------------------------------------------------------------------


def test_tv01_signed_ratio_is_negative_below_the_reference() -> None:
    """Proceeds under the last reported NAV read as a negative ratio.

    M-3's R-SEC-SELL shows "−4.3 %" for proceeds of 1,837,500 against a NAV
    of 1,920,000; the sign is what makes that sentence a discount rather
    than an unlabelled distance.
    """
    ratio = signed_deviation_ratio(value=Decimal("1837500"), reference=Decimal("1920000"))

    assert ratio is not None
    assert ratio < 0
    assert f"{ratio * 100:,.1f}" == "-4.3"


def test_tv01_signed_ratio_is_positive_above_the_reference() -> None:
    """A premium is the same derivation with the other sign."""
    ratio = signed_deviation_ratio(value=Decimal("110"), reference=Decimal("100"))

    assert ratio == Decimal("0.1")


def test_tv01_signed_ratio_is_zero_at_the_reference() -> None:
    """Exactly at the reference is zero, not ``None`` — it is a real answer."""
    assert signed_deviation_ratio(value=Decimal("100"), reference=Decimal("100")) == 0


# ---------------------------------------------------------------------------
# TV-02: the undefined case
# ---------------------------------------------------------------------------


def test_tv02_zero_reference_is_none_on_both_ratios() -> None:
    """A zero reference makes the comparison meaningless rather than infinite.

    Both functions answer ``None`` and neither raises: the caller renders a
    dash, exactly as it does for a stake that carries no NAV at all.
    """
    assert signed_deviation_ratio(value=Decimal("500"), reference=Decimal(0)) is None
    assert price_deviation_ratio(price=Decimal("500"), reference=Decimal(0)) is None


# ---------------------------------------------------------------------------
# TV-03: the identity with the unsigned twin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reference"),
    [
        (Decimal("1837500"), Decimal("1920000")),
        (Decimal("2100000"), Decimal("1920000")),
        (Decimal("0"), Decimal("1920000")),
        (Decimal("-50000"), Decimal("1920000")),
    ],
)
def test_tv03_magnitudes_agree_with_the_unsigned_twin(value: Decimal, reference: Decimal) -> None:
    """``abs(signed)`` is the unsigned ratio, on both sides of the reference."""
    signed = signed_deviation_ratio(value=value, reference=reference)
    unsigned = price_deviation_ratio(price=value, reference=reference)

    assert signed is not None and unsigned is not None
    assert abs(signed) == unsigned
