# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Flat-parameter serialisation of an overlay (ADR-0104 §4).

ADR-0104 §4 fixes the *principle* — the parameter set is the entire page
state, serialised as flat request parameters on every HTMX interaction, with
a deterministic field order and no LLM-formed keys — and leaves the exact
encoding to the implementation strand. **This module is that encoding.**

## The encoding

Each transformation occupies an indexed namespace ``t{n}_``, where ``n`` is
its **0-based position in the overlay**. Application order is list order
(ADR-0104 §2), so the index is not decoration: it *is* the order. Indices are
contiguous from zero; a gap is a malformed parameter set, never a silently
compacted one.

Every transformation leads with ``t{n}_kind``, followed by its fields in a
fixed order:

* ``insert_transaction``: ``investment_id``, ``txn_type``,
  ``trade_date``, ``units``, ``price_per_unit``, ``consideration``,
  ``currency``.
* ``repace_flows``: ``investment_id``, ``factor``.
* ``market_shock``: ``archetype``, ``magnitude``.
* ``fx_shock``: ``currency``, ``magnitude``.

Types on the wire: a UUID as its canonical string, a date as ISO-8601
(``YYYY-MM-DD``), an enum as its **value** (an ``Archetype`` travels as
``capital_account``, never as ``Archetype.CAPITAL_ACCOUNT``), a Decimal as its
plain string form — never via ``float``, which would round-trip a money amount
through binary and back. An **optional** Decimal (``price_per_unit``,
``consideration``) serialises as the empty string.

**All four kinds encode**, including ``fx_shock``, whose executor ships with
S34.2. Encoding is not executability: a scenario link must be *parseable* into
the overlay it names, and the fold — not the parser — is where a kind without
an executor fails loudly
(:class:`~services.overlay.errors.ExecutorNotRegisteredError`). Rejecting an
``fx_shock`` at the parser would make a shared URL unreadable rather than
unexecutable, which is the worse of the two errors: the operator would learn
their link was malformed when in fact their scenario was merely early.

## Two laws

* **Round-trip:** ``parse_overlay(serialise_overlay(o)) == o`` for every
  valid overlay, the empty one included. This is what makes ADR-0104 §4's
  "copy scenario link" honest — a scenario is reproducible from
  *(book, URL)*.
* **Closed table:** parsing dispatches over a module-level mapping keyed by
  the four kind strings. No ``eval``, no dynamic attribute access, no
  key-formed-from-input lookup into anything but this table. A request
  parameter can therefore never name code.

## Foreign keys are ignored, stray keys inside ``t{n}_`` are not

A Planning Desk request carries more than the overlay — a CSRF token, the
horizon, the periodisation. Keys outside the ``t{n}_`` namespace are ignored.
Keys *inside* it that no field table defines are rejected: that namespace
belongs to the overlay, and an unrecognised member of it is a bug in the
caller, not a parameter to shrug at.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import cast
from uuid import UUID

from services.investments.archetype import Archetype
from services.overlay.contract import (
    FxShock,
    InsertTransaction,
    MarketShock,
    Overlay,
    RepaceFlows,
    Transformation,
    TransformationKind,
)
from services.overlay.errors import (
    FactorOutOfBoundsError,
    IndexSequenceError,
    KindNotImplementedError,
    MalformedFieldError,
    MissingFieldError,
    UnknownKindError,
)

#: The ``t{n}_<field>`` namespace. ``n`` is checked for canonical form (no
#: leading zeros) after the match, so the encoding stays bijective.
_PARAM_PATTERN = re.compile(r"^t(\d+)_(\w+)$")

#: The discriminator field every transformation leads with.
_KIND_FIELD = "kind"

#: Field order per kind — **the contract**. Serialisation emits these in order
#: after ``t{n}_kind``; the table in the module docstring mirrors it. All four
#: kinds are present: the encoding is complete even where the executor is not
#: (``fx_shock``, S34.2).
_FIELD_ORDER: dict[TransformationKind, tuple[str, ...]] = {
    TransformationKind.INSERT_TRANSACTION: (
        "investment_id",
        "txn_type",
        "trade_date",
        "units",
        "price_per_unit",
        "consideration",
        "currency",
    ),
    TransformationKind.REPACE_FLOWS: (
        "investment_id",
        "factor",
    ),
    TransformationKind.MARKET_SHOCK: (
        "archetype",
        "magnitude",
    ),
    TransformationKind.FX_SHOCK: (
        "currency",
        "magnitude",
    ),
}

#: Fields whose value may legitimately be absent. They serialise as the empty
#: string and parse back to ``None``; their key may also be missing outright.
#: Every other field is required. Neither shock kind has an optional field: a
#: shock with no magnitude is not a shock with a default, it is an unstated one.
_OPTIONAL_FIELDS: dict[TransformationKind, frozenset[str]] = {
    TransformationKind.INSERT_TRANSACTION: frozenset({"price_per_unit", "consideration"}),
    TransformationKind.REPACE_FLOWS: frozenset(),
    TransformationKind.MARKET_SHOCK: frozenset(),
    TransformationKind.FX_SHOCK: frozenset(),
}


def serialise_overlay(overlay: Overlay) -> list[tuple[str, str]]:
    """Serialise an overlay to ordered flat request parameters.

    The output is a list of pairs, not a dict: order is part of the contract
    (ADR-0104 §4, "deterministic field order"), and a dict would leave it to
    the caller's insertion discipline. Pairs are emitted by ascending
    transformation index, and within a transformation by the field order of
    :data:`_FIELD_ORDER` — ``t{n}_kind`` first.

    Args:
        overlay: The ordered transformations. May be empty (the baseline).

    Returns:
        The flat parameters as ``(key, value)`` pairs, ready for a query
        string or an HTMX form body. Empty for the empty overlay.

    Raises:
        KindNotImplementedError: If the overlay holds a transformation whose
            kind has no field table. Unreachable as the contract stands — all
            four kinds encode — and kept as the structural tripwire for a kind
            added to :class:`~services.overlay.contract.TransformationKind`
            without an encoding, which would otherwise serialise to a bare
            ``t{n}_kind`` and round-trip into nothing.
    """
    params: list[tuple[str, str]] = []
    for index, transformation in enumerate(overlay):
        kind = transformation.kind
        field_order = _FIELD_ORDER.get(kind)
        if field_order is None:
            raise KindNotImplementedError(
                f"transformation {index}: kind '{kind.value}' has no "
                "serialisation field table, so it cannot be encoded"
            )
        params.append((_key(index, _KIND_FIELD), kind.value))
        for field in field_order:
            value = getattr(transformation, field)
            params.append((_key(index, field), _serialise_value(value)))
    return params


def parse_overlay(
    params: Mapping[str, str] | Iterable[tuple[str, str]],
) -> Overlay:
    """Parse flat request parameters back into an overlay.

    The inverse of :func:`serialise_overlay`, and the seam every Planning
    Desk request passes through. Keys outside the ``t{n}_`` namespace are
    ignored (a request carries a CSRF token and timeline parameters too);
    unrecognised keys *inside* it are rejected.

    Args:
        params: The request parameters, as a mapping or as ``(key, value)``
            pairs. Pairs are accepted because a query string may repeat a
            key — a repeat is rejected rather than resolved by a
            last-one-wins rule nobody could reproduce from the URL.

    Returns:
        The overlay, in the order the indices state. Empty for a parameter
        set holding no ``t{n}_kind`` — the baseline (ADR-0104 §4).

    Raises:
        MalformedFieldError: On a duplicate key, a non-canonical index, an
            unrecognised field inside the ``t{n}_`` namespace, or a value
            that is not parseable as its type.
        MissingFieldError: On an absent or empty required field.
        IndexSequenceError: If the transformation indices are not contiguous
            from zero.
        UnknownKindError: If a ``t{n}_kind`` names no kind of the contract.
        KindNotImplementedError: If a ``t{n}_kind`` names a kind with no field
            table — the structural tripwire, unreachable as the contract
            stands. An ``fx_shock`` **parses**: it has an encoding and no
            executor, and it is the fold that says so.
        FactorOutOfBoundsError: If a ``repace_flows`` factor lies outside the
            ADR-0104 §2 bounds.
    """
    fields_by_index = _collect_fields(params)
    if not fields_by_index:
        return ()

    _assert_contiguous(fields_by_index.keys())

    transformations: list[Transformation] = []
    for index in sorted(fields_by_index):
        fields = fields_by_index[index]
        kind = _parse_kind(index, fields)
        _assert_no_unknown_fields(index, kind, fields)
        transformations.append(_CONSTRUCTORS[kind](index, fields))
    return tuple(transformations)


# --- parameter collection -------------------------------------------------


def _key(index: int, field: str) -> str:
    """Return the flat parameter key for a transformation field."""
    return f"t{index}_{field}"


def _collect_fields(
    params: Mapping[str, str] | Iterable[tuple[str, str]],
) -> dict[int, dict[str, str]]:
    """Group the ``t{n}_`` parameters by transformation index.

    Args:
        params: The request parameters, as a mapping or as pairs.

    Returns:
        ``{index: {field: value}}`` for every key in the ``t{n}_``
        namespace, values stripped of surrounding whitespace. Keys outside
        the namespace are dropped.

    Raises:
        MalformedFieldError: On a duplicate key or a non-canonical index
            (e.g. ``t01_kind``, which would collide with ``t1_kind``).
    """
    # A Mapping that satisfies the pairs half of the union would key on the
    # pair itself, so pyright widens `.items()` accordingly; every caller
    # passes a str-keyed mapping or plain pairs.
    pairs = cast(
        Iterable[tuple[str, str]],
        params.items() if isinstance(params, Mapping) else params,
    )

    fields_by_index: dict[int, dict[str, str]] = {}
    seen: set[str] = set()
    for key, value in pairs:
        if key in seen:
            raise MalformedFieldError(
                f"duplicate parameter key '{key}': the overlay encoding "
                "assigns every field exactly one key"
            )
        seen.add(key)

        match = _PARAM_PATTERN.match(key)
        if match is None:
            continue
        raw_index, field = match.group(1), match.group(2)
        if str(int(raw_index)) != raw_index:
            raise MalformedFieldError(
                f"non-canonical transformation index in key '{key}': write "
                f"'t{int(raw_index)}_{field}'"
            )
        fields_by_index.setdefault(int(raw_index), {})[field] = value.strip()
    return fields_by_index


def _assert_contiguous(indices: Iterable[int]) -> None:
    """Assert the transformation indices run 0, 1, … without gaps.

    Raises:
        IndexSequenceError: If they do not. Application order is list order
            (ADR-0104 §2), so a gap has no defined meaning.
    """
    present = sorted(indices)
    expected = list(range(len(present)))
    if present != expected:
        raise IndexSequenceError(
            f"transformation indices must be contiguous from 0; got {present}, expected {expected}"
        )


def _assert_no_unknown_fields(
    index: int, kind: TransformationKind, fields: Mapping[str, str]
) -> None:
    """Assert every field of this index is one the kind actually defines.

    Raises:
        MalformedFieldError: On the first unrecognised field.
    """
    known = set(_FIELD_ORDER[kind]) | {_KIND_FIELD}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise MalformedFieldError(
            f"transformation {index} (kind '{kind.value}'): unrecognised "
            f"field(s) {unknown}; the kind's fields are "
            f"{list(_FIELD_ORDER[kind])}"
        )


def _parse_kind(index: int, fields: Mapping[str, str]) -> TransformationKind:
    """Resolve a transformation's ``kind`` field to a contract kind.

    Raises:
        MissingFieldError: If ``t{n}_kind`` is absent or empty — an index
            carrying fields but no kind is an orphan, not a transformation.
        UnknownKindError: If the value names no kind of the contract.
        KindNotImplementedError: If it names a kind carrying no field table.
            The structural tripwire — unreachable as the contract stands, and
            deliberately *not* the seam that refuses an ``fx_shock``: a kind
            without an executor is refused by the fold
            (:class:`~services.overlay.errors.ExecutorNotRegisteredError`), so
            that a shared scenario link stays readable while its scenario is
            still early.
    """
    raw = fields.get(_KIND_FIELD, "")
    if not raw:
        raise MissingFieldError(f"transformation {index}: '{_key(index, _KIND_FIELD)}' is required")
    try:
        kind = TransformationKind(raw)
    except ValueError as exc:
        known = [k.value for k in TransformationKind]
        raise UnknownKindError(
            f"transformation {index}: unknown kind '{raw}'; the overlay "
            f"contract is closed over {known}"
        ) from exc

    if kind not in _FIELD_ORDER:
        raise KindNotImplementedError(
            f"transformation {index}: kind '{kind.value}' belongs to the "
            "ADR-0104 §2 contract but carries no encoding, so it cannot be "
            "parsed"
        )
    return kind


# --- value parsing --------------------------------------------------------


def _serialise_value(value: object) -> str:
    """Render one field value in its wire form.

    ``None`` (an absent optional Decimal) becomes the empty string; a date
    becomes ISO-8601; an enum becomes its **value**, never its ``repr`` — the
    wire carries ``capital_account``, which is exactly what
    :class:`~services.investments.archetype.Archetype` parses back. A Decimal
    or UUID becomes its canonical string, and a Decimal never passes through
    ``float``.
    """
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        # Explicit rather than relying on StrEnum's ``__str__``: the field is
        # typed as the enum, and a later plain-``Enum`` field would otherwise
        # serialise as "Archetype.CAPITAL_ACCOUNT" and never round-trip.
        return str(value.value)
    return str(value)


def _required(index: int, fields: Mapping[str, str], field: str) -> str:
    """Return a required field's raw value.

    Raises:
        MissingFieldError: If the key is absent or its value empty.
    """
    value = fields.get(field, "")
    if not value:
        raise MissingFieldError(f"transformation {index}: '{_key(index, field)}' is required")
    return value


def _parse_uuid(index: int, fields: Mapping[str, str], field: str) -> UUID:
    """Parse a required UUID field.

    Raises:
        MissingFieldError: If the field is absent or empty.
        MalformedFieldError: If the value is not a UUID.
    """
    raw = _required(index, fields, field)
    try:
        return UUID(raw)
    except ValueError as exc:
        raise MalformedFieldError(
            f"transformation {index}: '{_key(index, field)}' is not a UUID: '{raw}'"
        ) from exc


def _parse_date(index: int, fields: Mapping[str, str], field: str) -> date:
    """Parse a required ISO-8601 date field.

    Raises:
        MissingFieldError: If the field is absent or empty.
        MalformedFieldError: If the value is not an ISO-8601 date.
    """
    raw = _required(index, fields, field)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise MalformedFieldError(
            f"transformation {index}: '{_key(index, field)}' is not an ISO-8601 date: '{raw}'"
        ) from exc


def _parse_decimal(index: int, fields: Mapping[str, str], field: str) -> Decimal:
    """Parse a required Decimal field — via ``Decimal(str)``, never ``float``.

    Raises:
        MissingFieldError: If the field is absent or empty.
        MalformedFieldError: If the value is not a Decimal.
    """
    raw = _required(index, fields, field)
    return _to_decimal(index, field, raw)


def _parse_optional_decimal(index: int, fields: Mapping[str, str], field: str) -> Decimal | None:
    """Parse an optional Decimal field.

    An absent key and an empty value both mean "not stated" and yield
    ``None``; the serialiser always emits the key, so a round trip is
    unaffected either way.

    Raises:
        MalformedFieldError: If a non-empty value is not a Decimal.
    """
    raw = fields.get(field, "")
    if not raw:
        return None
    return _to_decimal(index, field, raw)


def _parse_archetype(index: int, fields: Mapping[str, str], field: str) -> Archetype:
    """Parse a required :class:`~services.investments.archetype.Archetype` field.

    Strict by value: an unknown archetype is **rejected**, never resolved
    through :func:`~services.investments.archetype.resolve_archetype`'s
    NAV-only fallback. That fallback exists to keep an unknown *investment
    type* visible in the universe scan; applying it to a request parameter
    would silently retarget a shock the operator aimed at one class of holdings
    onto another, and a scenario nobody asked for is worse than an error.

    Raises:
        MissingFieldError: If the field is absent or empty.
        MalformedFieldError: If the value names no archetype.
    """
    raw = _required(index, fields, field)
    try:
        return Archetype(raw)
    except ValueError as exc:
        known = [archetype.value for archetype in Archetype]
        raise MalformedFieldError(
            f"transformation {index}: '{_key(index, field)}' is not an "
            f"archetype: '{raw}'; the archetypes are {known}"
        ) from exc


def _to_decimal(index: int, field: str, raw: str) -> Decimal:
    """Convert a raw parameter value to a Decimal.

    Raises:
        MalformedFieldError: If ``raw`` is not a Decimal.
    """
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise MalformedFieldError(
            f"transformation {index}: '{_key(index, field)}' is not a decimal number: '{raw}'"
        ) from exc


# --- the closed constructor table -----------------------------------------


def _build_insert_transaction(index: int, fields: Mapping[str, str]) -> InsertTransaction:
    """Construct an :class:`InsertTransaction` from one index's fields."""
    return InsertTransaction(
        investment_id=_parse_uuid(index, fields, "investment_id"),
        txn_type=_required(index, fields, "txn_type"),
        trade_date=_parse_date(index, fields, "trade_date"),
        units=_parse_decimal(index, fields, "units"),
        price_per_unit=_parse_optional_decimal(index, fields, "price_per_unit"),
        consideration=_parse_optional_decimal(index, fields, "consideration"),
        currency=_required(index, fields, "currency"),
    )


def _build_repace_flows(index: int, fields: Mapping[str, str]) -> RepaceFlows:
    """Construct a :class:`RepaceFlows` from one index's fields.

    The bounds check lives in the dataclass (ADR-0104 §2), so it is not
    restated here. The construction-time error is re-raised with the
    offending index and field named, so a parse failure identifies the
    parameter that caused it.

    Raises:
        FactorOutOfBoundsError: If the factor is outside ``[0.5, 2.0]``.
    """
    investment_id = _parse_uuid(index, fields, "investment_id")
    factor = _parse_decimal(index, fields, "factor")
    try:
        return RepaceFlows(investment_id=investment_id, factor=factor)
    except FactorOutOfBoundsError as exc:
        key = _key(index, "factor")
        raise FactorOutOfBoundsError(f"transformation {index}: '{key}' — {exc.message}") from exc


def _build_market_shock(index: int, fields: Mapping[str, str]) -> MarketShock:
    """Construct a :class:`MarketShock` from one index's fields.

    The magnitude is **not** bounds-checked. Unlike a re-pacing factor, whose
    ``[0.5, 2.0]`` range ADR-0104 §2 states as part of the contract, the ADR
    puts no bound on a shock's per-cent magnitude — and a stress test is
    precisely the place an operator may want an extreme one. Inventing a bound
    here would be a UI opinion smuggled into the parser.
    """
    return MarketShock(
        archetype=_parse_archetype(index, fields, "archetype"),
        magnitude=_parse_decimal(index, fields, "magnitude"),
    )


def _build_fx_shock(index: int, fields: Mapping[str, str]) -> FxShock:
    """Construct an :class:`FxShock` from one index's fields.

    Parsing an ``fx_shock`` succeeds even though no executor can apply one yet
    (S34.2): the value is well-formed, and it is
    :func:`services.overlay.pipeline.apply_overlay` that refuses it, by name.
    """
    return FxShock(
        currency=_required(index, fields, "currency"),
        magnitude=_parse_decimal(index, fields, "magnitude"),
    )


#: The closed parse table: kind string → constructor. A request parameter
#: selects a row here and nothing else — no dynamic attribute access, no
#: ``eval``, no import by name. All four kinds are rows: the table mirrors the
#: *encoding*, not the executor registry (:data:`_EXECUTORS`), and the two part
#: company for ``fx_shock`` until S34.2.
_CONSTRUCTORS: dict[TransformationKind, Callable[[int, Mapping[str, str]], Transformation]] = {
    TransformationKind.INSERT_TRANSACTION: _build_insert_transaction,
    TransformationKind.REPACE_FLOWS: _build_repace_flows,
    TransformationKind.MARKET_SHOCK: _build_market_shock,
    TransformationKind.FX_SHOCK: _build_fx_shock,
}


__all__ = [
    "parse_overlay",
    "serialise_overlay",
]
