# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Serialisation tests: the round-trip law and the parse rejections.

ADR-0104 §4 makes a scenario reproducible from *(book, URL)*. That promise
rests on one law — ``parse_overlay(serialise_overlay(o)) == o`` for every
valid overlay, the empty one included — and on a parser that refuses anything
it cannot reproduce rather than guessing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from services.investments.archetype import Archetype
from services.overlay import (
    EMPTY_OVERLAY,
    FactorOutOfBoundsError,
    FxShock,
    IndexSequenceError,
    InsertTransaction,
    KindNotImplementedError,
    MalformedFieldError,
    MarketShock,
    MissingFieldError,
    Overlay,
    OverlayParseError,
    RepaceFlows,
    UnknownKindError,
    parse_overlay,
    serialise_overlay,
)

_INVESTMENT_A = UUID("11111111-1111-1111-1111-111111111111")
_INVESTMENT_B = UUID("22222222-2222-2222-2222-222222222222")


def _insert(**overrides: object) -> InsertTransaction:
    """Build an ``InsertTransaction`` with sane defaults."""
    fields: dict[str, object] = {
        "investment_id": _INVESTMENT_A,
        "txn_type": "buy",
        "trade_date": date(2026, 9, 30),
        "units": Decimal("100.5"),
        "price_per_unit": Decimal("12.3456"),
        "consideration": Decimal("-1240.73"),
        "currency": "USD",
    }
    fields.update(overrides)
    return InsertTransaction(**fields)  # type: ignore[arg-type]


# --- serialisation --------------------------------------------------------


def test_serialise_empty_overlay_is_empty() -> None:
    """The baseline serialises to no parameters at all."""
    assert serialise_overlay(EMPTY_OVERLAY) == []


def test_serialise_insert_transaction_field_order() -> None:
    """The insert_transaction encoding, key by key, in its fixed order."""
    params = serialise_overlay((_insert(),))
    assert params == [
        ("t0_kind", "insert_transaction"),
        ("t0_investment_id", "11111111-1111-1111-1111-111111111111"),
        ("t0_txn_type", "buy"),
        ("t0_trade_date", "2026-09-30"),
        ("t0_units", "100.5"),
        ("t0_price_per_unit", "12.3456"),
        ("t0_consideration", "-1240.73"),
        ("t0_currency", "USD"),
    ]


def test_serialise_repace_flows_field_order() -> None:
    """The repace_flows encoding, key by key, in its fixed order."""
    params = serialise_overlay((RepaceFlows(investment_id=_INVESTMENT_B, factor=Decimal("1.5")),))
    assert params == [
        ("t0_kind", "repace_flows"),
        ("t0_investment_id", "22222222-2222-2222-2222-222222222222"),
        ("t0_factor", "1.5"),
    ]


def test_optional_decimals_serialise_as_the_empty_string() -> None:
    """An unstated price or consideration is the empty string on the wire."""
    params = dict(serialise_overlay((_insert(price_per_unit=None, consideration=None),)))
    assert params["t0_price_per_unit"] == ""
    assert params["t0_consideration"] == ""


def test_indices_are_contiguous_and_ascending() -> None:
    """Application order is list order — the index carries it (ADR-0104 §2)."""
    overlay: Overlay = (
        _insert(),
        RepaceFlows(investment_id=_INVESTMENT_B, factor=Decimal("0.8")),
        _insert(investment_id=_INVESTMENT_B),
    )
    keys = [key for key, _ in serialise_overlay(overlay)]
    assert keys[0] == "t0_kind"
    assert "t1_kind" in keys
    assert "t2_kind" in keys
    prefixes = [key.split("_", 1)[0] for key in keys]
    assert prefixes == sorted(prefixes, key=lambda p: int(p[1:]))


# --- the round-trip law ---------------------------------------------------


@pytest.mark.parametrize(
    "overlay",
    [
        pytest.param(EMPTY_OVERLAY, id="empty"),
        pytest.param((_insert(),), id="insert_transaction"),
        pytest.param(
            (RepaceFlows(investment_id=_INVESTMENT_A, factor=Decimal("2.0")),),
            id="repace_flows",
        ),
        pytest.param(
            (_insert(price_per_unit=None, consideration=None),),
            id="insert_transaction_without_optionals",
        ),
        pytest.param(
            (
                RepaceFlows(investment_id=_INVESTMENT_A, factor=Decimal("0.5")),
                _insert(investment_id=_INVESTMENT_B, txn_type="sell", units=Decimal("-40")),
                _insert(consideration=None),
            ),
            id="multi_transformation",
        ),
    ],
)
def test_round_trip(overlay: Overlay) -> None:
    """``parse(serialise(o)) == o`` — the law ADR-0104 §4's URL rests on."""
    assert parse_overlay(serialise_overlay(overlay)) == overlay


def test_round_trip_preserves_order() -> None:
    """A re-ordered overlay is a different scenario, and stays one."""
    repace = RepaceFlows(investment_id=_INVESTMENT_A, factor=Decimal("1.25"))
    insert = _insert()

    forward = parse_overlay(serialise_overlay((repace, insert)))
    backward = parse_overlay(serialise_overlay((insert, repace)))

    assert forward == (repace, insert)
    assert backward == (insert, repace)
    assert forward != backward


def test_decimals_never_round_trip_through_float() -> None:
    """A price keeps every digit: Decimal(str), never float (ADR-0104 §4)."""
    exact = Decimal("0.1234567890123456789")
    parsed = parse_overlay(serialise_overlay((_insert(units=exact),)))
    assert parsed[0].units == exact
    assert str(parsed[0].units) == str(exact)


def test_parse_accepts_a_mapping_and_ignores_foreign_keys() -> None:
    """A Planning Desk request carries more than the overlay."""
    params = dict(serialise_overlay((_insert(),)))
    params["csrf_token"] = "abc123"
    params["horizon"] = "8q"
    params["periodisation"] = "quarterly"

    assert parse_overlay(params) == (_insert(),)


def test_parse_of_a_parameterless_request_is_the_baseline() -> None:
    """No ``t{n}_kind`` at all means the empty overlay, not an error."""
    assert parse_overlay({"csrf_token": "abc123"}) == EMPTY_OVERLAY
    assert parse_overlay({}) == EMPTY_OVERLAY


# --- parse rejections -----------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("market_shock", {"archetype": "capital_account", "magnitude": "-20"}),
        ("fx_shock", {"currency": "USD", "magnitude": "-20"}),
    ],
)
def test_both_shock_kinds_now_encode(kind: str, fields: dict[str, str]) -> None:
    """The successor of ``test_shock_kinds_are_recognised_and_rejected_naming_034``.

    That test pinned the S2.1a state: the shock kinds were enum members with no
    field table, and the *parser* rejected them by name against roadmap #034.
    S34.1 gives both an encoding, so the rejection it asserted is gone — but the
    property underneath it is not, and this is where it moved to.

    The parser now **accepts** both kinds, because an encoding is not an
    executor. What still refuses an ``fx_shock`` is the fold
    (:class:`~services.overlay.errors.ExecutorNotRegisteredError`, asserted in
    ``test_pipeline.py`` and ``test_executors.py``), and that is the right seam:
    rejecting it at the parser would make a shared scenario link *unreadable*
    rather than merely unexecutable, which is the worse of the two errors — the
    operator would be told their URL was malformed when in truth their scenario
    was early.
    """
    params = {"t0_kind": kind} | {f"t0_{field}": value for field, value in fields.items()}
    overlay = parse_overlay(params)

    assert len(overlay) == 1
    assert overlay[0].kind.value == kind
    # And it round-trips: the encoding is complete, not merely tolerant.
    assert parse_overlay(serialise_overlay(overlay)) == overlay


def test_kind_not_implemented_stays_in_the_hierarchy() -> None:
    """The error is retired from *these two* kinds, not from the contract.

    All four kinds encode, so nothing raises it today. It remains the structural
    tripwire for a kind added to the discriminator without a field table — which
    would otherwise serialise to a bare ``t{n}_kind`` and round-trip into
    nothing at all.
    """
    assert issubclass(KindNotImplementedError, OverlayParseError)


def test_market_shock_field_order() -> None:
    """``kind`` first, then the ADR-0104 §2 fields; the archetype by value."""
    shock = MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-20"))
    assert serialise_overlay((shock,)) == [
        ("t0_kind", "market_shock"),
        ("t0_archetype", "capital_account"),
        ("t0_magnitude", "-20"),
    ]


def test_fx_shock_field_order() -> None:
    """Likewise for the currency-scoped kind, whose executor is still to come."""
    assert serialise_overlay((FxShock(currency="USD", magnitude=Decimal("7.5")),)) == [
        ("t0_kind", "fx_shock"),
        ("t0_currency", "USD"),
        ("t0_magnitude", "7.5"),
    ]


@pytest.mark.parametrize(
    "magnitude",
    ["-20", "-0.005", "0", "12.345678", "-100", "250"],
)
def test_a_shock_magnitude_round_trips_as_a_decimal_string(
    magnitude: str,
) -> None:
    """Never via ``float``: ``-0.005`` must come back as ``-0.005``, exactly.

    The same law the money fields obey. A magnitude that lost a digit to binary
    would silently restate the scenario the operator asked for.
    """
    overlay: Overlay = (
        MarketShock(archetype=Archetype.FIXED_INCOME, magnitude=Decimal(magnitude)),
    )
    parsed = parse_overlay(serialise_overlay(overlay))

    assert parsed == overlay
    assert parsed[0].magnitude == Decimal(magnitude)  # type: ignore[union-attr]


@pytest.mark.parametrize("archetype", list(Archetype))
def test_every_archetype_round_trips_by_value(archetype: Archetype) -> None:
    """An enum travels as its value — ``capital_account``, never its ``repr``."""
    overlay: Overlay = (MarketShock(archetype=archetype, magnitude=Decimal("-5")),)
    parsed = parse_overlay(serialise_overlay(overlay))

    assert parsed == overlay
    assert parsed[0].archetype is archetype  # type: ignore[union-attr]


def test_an_unknown_archetype_is_rejected_not_silently_defaulted() -> None:
    """No fallback to NAV-only here (contrast ``resolve_archetype``).

    The resolver's NAV-only fallback exists so an unknown *investment type* stays
    visible in the universe scan. Applying it to a request parameter would
    silently retarget a shock the operator aimed at one class of holdings onto
    another — a scenario nobody asked for, which is worse than an error.
    """
    with pytest.raises(MalformedFieldError) as excinfo:
        parse_overlay(
            {
                "t0_kind": "market_shock",
                "t0_archetype": "hedge_fund",
                "t0_magnitude": "-20",
            }
        )
    assert "hedge_fund" in str(excinfo.value)


@pytest.mark.parametrize("field", ["archetype", "magnitude"])
def test_a_shock_field_is_required(field: str) -> None:
    """Neither field is optional: a shock with no magnitude is an unstated one."""
    params = {
        "t0_kind": "market_shock",
        "t0_archetype": "capital_account",
        "t0_magnitude": "-20",
    }
    del params[f"t0_{field}"]
    with pytest.raises(MissingFieldError) as excinfo:
        parse_overlay(params)
    assert f"t0_{field}" in str(excinfo.value)


def test_a_mixed_overlay_of_all_four_kinds_round_trips() -> None:
    """The whole contract, in one parameter set, in order (ADR-0104 §4).

    Including the ``fx_shock``: a scenario link carrying one must survive the
    round trip, because it is the *fold* that will refuse it — not the URL.
    """
    overlay: Overlay = (
        _insert(),
        RepaceFlows(investment_id=_INVESTMENT_B, factor=Decimal("1.75")),
        MarketShock(archetype=Archetype.CAPITAL_ACCOUNT, magnitude=Decimal("-20")),
        FxShock(currency="USD", magnitude=Decimal("-8.5")),
    )
    assert parse_overlay(serialise_overlay(overlay)) == overlay


def test_unknown_kind_is_rejected() -> None:
    """A kind outside the closed four is not a transformation at all."""
    with pytest.raises(UnknownKindError):
        parse_overlay({"t0_kind": "delete_investment"})


def test_index_gap_is_rejected() -> None:
    """A gap in the index sequence has no defined application order."""
    params = dict(serialise_overlay((_insert(),)))
    shifted = {key.replace("t0_", "t1_"): value for key, value in params.items()}
    with pytest.raises(IndexSequenceError):
        parse_overlay(shifted)


def test_missing_required_field_is_rejected_naming_index_and_field() -> None:
    """A missing required field names the parameter that caused the failure."""
    params = dict(serialise_overlay((_insert(),)))
    del params["t0_units"]
    with pytest.raises(MissingFieldError) as excinfo:
        parse_overlay(params)
    assert "t0_units" in str(excinfo.value)


def test_empty_required_field_is_rejected() -> None:
    """An empty value for a required field is a missing field."""
    params = dict(serialise_overlay((_insert(),)))
    params["t0_currency"] = ""
    with pytest.raises(MissingFieldError):
        parse_overlay(params)


def test_orphan_index_without_a_kind_is_rejected() -> None:
    """Fields without a ``t{n}_kind`` are an orphan, not a transformation."""
    with pytest.raises(MissingFieldError):
        parse_overlay({"t0_factor": "1.5"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("t0_investment_id", "not-a-uuid"),
        ("t0_trade_date", "30.09.2026"),
        ("t0_units", "one hundred"),
        ("t0_price_per_unit", "12,50"),
    ],
)
def test_malformed_values_are_rejected(field: str, value: str) -> None:
    """A value that is not its type is rejected, never coerced."""
    params = dict(serialise_overlay((_insert(),)))
    params[field] = value
    with pytest.raises(MalformedFieldError) as excinfo:
        parse_overlay(params)
    assert field in str(excinfo.value)


def test_unknown_field_inside_the_namespace_is_rejected() -> None:
    """The ``t{n}_`` namespace belongs to the overlay; strays are bugs."""
    params = dict(serialise_overlay((_insert(),)))
    params["t0_magnitude"] = "-20"
    with pytest.raises(MalformedFieldError) as excinfo:
        parse_overlay(params)
    assert "magnitude" in str(excinfo.value)


def test_duplicate_key_is_rejected() -> None:
    """A repeated key has no last-one-wins rule anyone could read off a URL."""
    pairs = serialise_overlay((_insert(),))
    with pytest.raises(MalformedFieldError):
        parse_overlay([*pairs, ("t0_units", "999")])


def test_non_canonical_index_is_rejected() -> None:
    """``t01_kind`` would collide with ``t1_kind``: the encoding is 1:1."""
    with pytest.raises(MalformedFieldError):
        parse_overlay({"t01_kind": "repace_flows"})


@pytest.mark.parametrize("factor", ["0.49", "2.01"])
def test_out_of_bounds_factor_is_rejected_naming_index_and_field(
    factor: str,
) -> None:
    """An out-of-bounds factor fails the parse, with the parameter named."""
    with pytest.raises(FactorOutOfBoundsError) as excinfo:
        parse_overlay(
            {
                "t0_kind": "repace_flows",
                "t0_investment_id": str(_INVESTMENT_A),
                "t0_factor": factor,
            }
        )
    assert "t0_factor" in str(excinfo.value)


@pytest.mark.parametrize("factor", ["0.5", "2.0"])
def test_boundary_factors_parse(factor: str) -> None:
    """Both bounds are inclusive."""
    overlay = parse_overlay(
        {
            "t0_kind": "repace_flows",
            "t0_investment_id": str(_INVESTMENT_A),
            "t0_factor": factor,
        }
    )
    assert overlay == (RepaceFlows(investment_id=_INVESTMENT_A, factor=Decimal(factor)),)


def test_every_parse_rejection_shares_one_hierarchy() -> None:
    """One base class for the route seam to catch (S2.3)."""
    for exc_type in (
        UnknownKindError,
        KindNotImplementedError,
        MissingFieldError,
        MalformedFieldError,
        IndexSequenceError,
    ):
        assert issubclass(exc_type, OverlayParseError)


def test_overlay_survives_a_query_string_round_trip() -> None:
    """The overlay travels as ordered pairs — the "copy scenario link" path."""
    overlay: Overlay = (
        _insert(),
        RepaceFlows(investment_id=_INVESTMENT_B, factor=Decimal("1.75")),
    )
    pairs = serialise_overlay(overlay)
    assert parse_overlay(pairs) == overlay
    # The index carries the order, not the pair sequence: a client that
    # reshuffles the parameters still reproduces the same scenario.
    assert parse_overlay(reversed(pairs)) == overlay
