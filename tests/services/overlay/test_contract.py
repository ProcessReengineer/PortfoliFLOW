# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Contract-layer tests: the closed kind set and the re-pacing bounds.

ADR-0104 §2. The bounds are validated at construction, so an out-of-bounds
:class:`RepaceFlows` cannot exist in memory — no executor and no serialiser
has to re-check them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from services.investments.archetype import Archetype
from services.overlay import (
    EMPTY_OVERLAY,
    EXECUTABLE_KINDS,
    FACTOR_MAX,
    FACTOR_MIN,
    FACTOR_NEUTRAL,
    FactorOutOfBoundsError,
    FxShock,
    InsertTransaction,
    MarketShock,
    RepaceFlows,
    TransformationKind,
)


def test_kind_discriminator_is_closed_over_four_kinds() -> None:
    """Exactly the four ADR-0104 §2 kinds exist — no more, no fewer."""
    assert {k.value for k in TransformationKind} == {
        "insert_transaction",
        "repace_flows",
        "market_shock",
        "fx_shock",
    }


def test_executable_kinds_exclude_only_the_fx_shock() -> None:
    """Three of the four execute; ``fx_shock`` waits for its seam (S34.2).

    Membership of the union is not executability. An ``FxShock`` is a complete
    value — it constructs, and it round-trips through the encoding — but it acts
    at the **conversion seam** (ADR-0104 §3, N3) rather than on a value path,
    and that hook does not exist yet. Applying one raises rather than quietly
    returning the baseline.
    """
    assert {
        TransformationKind.INSERT_TRANSACTION,
        TransformationKind.REPACE_FLOWS,
        TransformationKind.MARKET_SHOCK,
    } == EXECUTABLE_KINDS
    assert TransformationKind.FX_SHOCK not in EXECUTABLE_KINDS


def test_transformations_carry_their_kind() -> None:
    """Each dataclass declares its kind, so the pipeline can dispatch on it."""
    insert = InsertTransaction(
        investment_id=uuid4(),
        txn_type="buy",
        trade_date=date(2026, 9, 30),
        units=Decimal("100"),
        price_per_unit=Decimal("12.50"),
        consideration=None,
        currency="EUR",
    )
    repace = RepaceFlows(investment_id=uuid4(), factor=FACTOR_NEUTRAL)
    market = MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-20"))
    fx = FxShock(currency="USD", magnitude=Decimal("10"))

    assert insert.kind is TransformationKind.INSERT_TRANSACTION
    assert repace.kind is TransformationKind.REPACE_FLOWS
    assert market.kind is TransformationKind.MARKET_SHOCK
    assert fx.kind is TransformationKind.FX_SHOCK


def test_a_market_shock_is_scoped_to_an_archetype_not_an_investment() -> None:
    """ADR-0104 §2's kind table: scope "one archetype", dispatch ``resolve_archetype``.

    A market shock is a statement about a *class* of holdings, so it carries no
    ``investment_id``. Asserting the absence is the point: an investment-scoped
    shock would be a different transformation, and one the ADR does not define.
    """
    shock = MarketShock(archetype=Archetype.TOTAL_RETURN_EQUITY, magnitude=Decimal("-15"))
    assert shock.archetype is Archetype.TOTAL_RETURN_EQUITY
    assert not hasattr(shock, "investment_id")


def test_an_fx_shock_is_scoped_to_a_currency_and_is_archetype_blind() -> None:
    """ADR-0104 §2: scope "one currency", dispatch "none (archetype-blind)"."""
    shock = FxShock(currency="USD", magnitude=Decimal("-8.5"))
    assert shock.currency == "USD"
    assert not hasattr(shock, "archetype")
    assert not hasattr(shock, "investment_id")


def test_shock_magnitudes_are_unbounded() -> None:
    """A shock has no bounds — unlike a re-pacing factor, and deliberately.

    ADR-0104 §2 bounds the re-pacing factor to ``[0.5, 2.0]`` as part of the
    contract, and says nothing of the kind about a shock's magnitude. A stress
    test is precisely where an operator may want an extreme one, so inventing a
    bound would be a UI opinion smuggled into the contract.
    """
    for magnitude in (Decimal("-100"), Decimal("-0.01"), Decimal("250")):
        assert (
            MarketShock(archetype=Archetype.FIXED_INCOME, magnitude=magnitude).magnitude
            == magnitude
        )


def test_empty_overlay_is_legal_and_means_baseline() -> None:
    """An empty overlay is the baseline (ADR-0104 §4), not an error."""
    assert EMPTY_OVERLAY == ()
    assert len(EMPTY_OVERLAY) == 0


@pytest.mark.parametrize(
    "factor",
    [FACTOR_MIN, Decimal("0.75"), FACTOR_NEUTRAL, Decimal("1.5"), FACTOR_MAX],
)
def test_repace_flows_accepts_factors_within_bounds(factor: Decimal) -> None:
    """Both bounds are inclusive (ADR-0104 §2: a factor in [0.5, 2.0])."""
    repace = RepaceFlows(investment_id=uuid4(), factor=factor)
    assert repace.factor == factor


@pytest.mark.parametrize(
    "factor",
    [Decimal("0.49"), Decimal("2.01"), Decimal("0"), Decimal("-1")],
)
def test_repace_flows_rejects_factors_outside_bounds(factor: Decimal) -> None:
    """Construction outside [0.5, 2.0] raises the typed error."""
    with pytest.raises(FactorOutOfBoundsError) as excinfo:
        RepaceFlows(investment_id=uuid4(), factor=factor)
    assert str(factor) in str(excinfo.value)


def test_transformations_are_frozen() -> None:
    """A transformation is a value: an overlay is never mutated in place."""
    repace = RepaceFlows(investment_id=uuid4(), factor=FACTOR_NEUTRAL)
    with pytest.raises(AttributeError):
        repace.factor = Decimal("1.2")  # type: ignore[misc]
