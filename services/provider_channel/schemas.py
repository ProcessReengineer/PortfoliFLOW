# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Versioned envelope and confirmation shapes for the provider channel (ADR-0129 §3).

Four wire shapes live here, each carrying its own ``schema_version``:

* :class:`Envelope` — the ADR-0129 §3 *routing* envelope. It names who sends
  to whom, about which correlated object, and what the relay currently knows
  about the message. It deliberately carries **no payload field**: the
  ciphertext is Stage B's concern, and a Stage-A reader that cannot decrypt
  should also be unable to hold a decrypted body by accident.
* :class:`FillPayload` — the ADR-0129 §3 structured confirmation. A fill is
  data, not prose, so the numbers arrive as fields rather than as something a
  human retypes. The free-text ``message`` (ADR-0129 §4) rides along for the
  human and is mapped nowhere.
* :class:`ExportPayload` — the plaintext a sealed export carries (ADR-0129 §6).
  The order's own parameters and nothing else: what the provider needs in
  order to act, with no tenant, no user, no ticket number and no holdings
  riding along behind it.
* :class:`ExportEnvelope` — the downloadable artefact a sealed export becomes:
  routing plus ciphertext, and **no plaintext member of any kind**. The same
  reasoning that keeps a body off :class:`Envelope` keeps one off this.

Parsing is strict and fail-closed on purpose. Four rules earn their keep:

* an absent or unrecognised ``schema_version`` is refused loudly
  (:class:`UnknownSchemaVersion`) rather than guessed at;
* unknown keys are refused and named — a forward-compatible reader is a
  Stage-B decision with its own compatibility story, not a Stage-A default
  that silently drops fields it was never taught;
* money arrives as JSON **strings** and becomes :class:`~decimal.Decimal`;
  a JSON number for a money field is refused, because binary floating point
  has no place between a provider's fill and a ticket;
* timestamps are offset-bearing; a naive datetime is refused.

Pure: no repository, no session, no clock, no network. Every value is
supplied by the caller (D-clock).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from services.provider_channel.directory import SUPPORTED_ENCRYPTION_KEY_TYPES, TICKET_KINDS

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

#: Wire version of the routing envelope. Bump when a field is added, removed
#: or given a new meaning; see docs/concepts/provider-directory-format.md.
ENVELOPE_SCHEMA_VERSION: Final[int] = 1

#: Wire version of the structured confirmation.
FILL_SCHEMA_VERSION: Final[int] = 1

#: Wire version of the sealed export. The plaintext :class:`ExportPayload` and
#: the :class:`ExportEnvelope` that carries it share this one number and bump
#: together: an envelope is only ever a sealed payload of its own generation,
#: and a portal that can read one can read the other.
EXPORT_SCHEMA_VERSION: Final[int] = 1

# ---------------------------------------------------------------------------
# Message vocabulary
# ---------------------------------------------------------------------------

#: A message about a trade ticket (ADR-0128 §1).
MESSAGE_TYPE_ORDER: Final[str] = "order"

#: A message about an engagement (ADR-0129 §5). The literal exists so the
#: envelope can already carry the distinction; no engagement object is built
#: in Stage A.
MESSAGE_TYPE_ENGAGEMENT: Final[str] = "engagement"

#: The two message types.
MESSAGE_TYPES: Final[frozenset[str]] = frozenset({MESSAGE_TYPE_ORDER, MESSAGE_TYPE_ENGAGEMENT})

# ---------------------------------------------------------------------------
# Envelope statuses
#
# The first three mirror services.transactions.constants.STATUS_SENT /
# _ACKNOWLEDGED / _EXECUTED; pinned by
# tests/services/provider_channel/test_contract.py. The literals are
# re-declared rather than imported because importing that package pulls the
# whole ticket-service graph in behind them (verify item 3) — deliberate
# duplication of a string, with the pin making drift a test failure.
# ---------------------------------------------------------------------------

#: mirrors services.transactions.constants.STATUS_SENT
ENVELOPE_STATUS_SENT: Final[str] = "sent"

#: mirrors services.transactions.constants.STATUS_ACKNOWLEDGED
ENVELOPE_STATUS_ACKNOWLEDGED: Final[str] = "acknowledged"

#: mirrors services.transactions.constants.STATUS_EXECUTED
ENVELOPE_STATUS_EXECUTED: Final[str] = "executed"

#: ADR-0129 §3 — envelope-only. A declined message has no ticket status: the
#: ticket stays where the operator left it, and declining is a fact about the
#: *message*, not a station in the ADR-0128 lifecycle.
ENVELOPE_STATUS_DECLINED: Final[str] = "declined"

#: The four envelope statuses. Note that no book state appears here — the
#: channel informs, and booking stays a reviewed act on this side.
ENVELOPE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        ENVELOPE_STATUS_SENT,
        ENVELOPE_STATUS_ACKNOWLEDGED,
        ENVELOPE_STATUS_EXECUTED,
        ENVELOPE_STATUS_DECLINED,
    }
)

# ---------------------------------------------------------------------------
# Export directions
#
# The same deliberate duplication as the statuses above: these mirror
# services.transactions.constants.DIRECTION_BUY / _SELL and are re-declared
# rather than imported, because importing that package would pull the whole
# ticket-service graph in behind them. The contract test asserts the two
# vocabularies are equal, so drift is a test failure rather than a silent
# mismatch between what a ticket says and what a provider is told.
# ---------------------------------------------------------------------------

#: mirrors services.transactions.constants.DIRECTION_BUY
EXPORT_DIRECTION_BUY: Final[str] = "buy"

#: mirrors services.transactions.constants.DIRECTION_SELL
EXPORT_DIRECTION_SELL: Final[str] = "sell"

#: The two directions an exported order may state.
EXPORT_DIRECTIONS: Final[frozenset[str]] = frozenset({EXPORT_DIRECTION_BUY, EXPORT_DIRECTION_SELL})

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaError(ValueError):
    """A document did not match the shape this build reads."""


class UnknownSchemaVersion(SchemaError):
    """A document declared a ``schema_version`` this build does not read.

    Attributes:
        shape: Which shape was being parsed — ``'envelope'``, ``'fill'``,
            ``'export'`` or ``'export_envelope'``.
        version: The offending value, or ``None`` when the key was absent.
    """

    def __init__(self, message: str, *, shape: str, version: object | None) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description, naming the offending version.
            shape: ``'envelope'``, ``'fill'``, ``'export'`` or
                ``'export_envelope'``.
            version: The declared value, or ``None`` if the key was missing.
        """
        super().__init__(message)
        self.shape = shape
        self.version = version


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Envelope:
    """The ADR-0129 §3 routing envelope.

    Carries routing and state, never content. There is no payload member by
    design: Stage A neither encrypts nor decrypts, so a body it could hold
    would be a body it could leak (ADR-0129 §6).

    Attributes:
        schema_version: Always :data:`ENVELOPE_SCHEMA_VERSION` for this build.
        sender_tenant_handle: The sending tenant's channel handle. Opaque
            here; the relay resolves it.
        provider_id: The recipient's directory id (:class:`~services.provider_channel.directory.ProviderEntry`).
        message_type: One of :data:`MESSAGE_TYPES`.
        correlation_id: The correlated object's id as text — a ticket or an
            engagement. Opaque: this package does not know about UUIDs, and
            an envelope must stay readable without a database.
        status: One of :data:`ENVELOPE_STATUSES`.
        created_at: Offset-bearing creation timestamp.
        updated_at: Offset-bearing timestamp of the last state change.
    """

    schema_version: int
    sender_tenant_handle: str
    provider_id: str
    message_type: str
    correlation_id: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FillPayload:
    """The ADR-0129 §3 structured confirmation.

    "The confirmation is structured": the provider states the fill as fields,
    so the instance can pre-fill a booking step rather than ask a human to
    retype a PDF. What it does *not* carry is as deliberate as what it does —
    there is no status, and no derived total (D-amounts).

    Attributes:
        schema_version: Always :data:`FILL_SCHEMA_VERSION` for this build.
        units: Filled quantity, unsigned; direction lives on the ticket.
        price_per_unit: Execution price in ``currency``.
        fees: Fees charged, in ``currency``.
        taxes: Taxes charged, in ``currency``.
        currency: Three upper-case letters.
        trade_date: Execution date.
        settlement_date: Settlement date if stated, else ``None``. When
            present it may not precede ``trade_date``.
        isin: The instrument's ISIN if the provider stated one, else ``None``.
        message: Free text for the human (ADR-0129 §4). Never mapped onto a
            ticket field.
    """

    schema_version: int
    units: Decimal
    price_per_unit: Decimal
    fees: Decimal
    taxes: Decimal
    currency: str
    trade_date: date
    settlement_date: date | None
    isin: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class ExportPayload:
    """The plaintext a sealed export carries (ADR-0129 §6).

    The order's own parameters, and deliberately nothing more. No tenant, no
    user, no ticket number and no holdings ride along: a provider needs to
    know what to trade, not who the portfolio belongs to or what else is in
    it, and an export that cannot carry those facts cannot leak them.

    ``gross_amount`` is the ticket's stored field, mapped across and **never
    computed here** (D-amounts) — net arithmetic stays the ticket layer's
    single answer, which is also why there is no ``net_amount`` member.

    ``message`` is free text for the human at the provider. The caller fills
    it deliberately and never from the ticket's ``note`` column, which may
    hold tenant-internal remarks that have no business leaving the instance.

    Attributes:
        schema_version: Always :data:`EXPORT_SCHEMA_VERSION` for this build.
        message_type: One of :data:`MESSAGE_TYPES`.
        correlation_id: The correlated object's id as text. Opaque here, and
            the one identifier that does travel — without it a returning
            confirmation matches nothing.
        ticket_kind: One of
            :data:`~services.provider_channel.directory.TICKET_KINDS`.
        direction: One of :data:`EXPORT_DIRECTIONS`, or ``None`` where the
            kind does not take one.
        identifier_scheme: Master-data identifier scheme, e.g. ``'isin'``.
            Present exactly when ``identifier_value`` is.
        identifier_value: The identifier itself.
        instrument_name: The instrument as text, for an instrument no
            identifier names. A payload must name the instrument one way or
            the other.
        units: Quantity, if stated.
        price_per_unit: Limit or indicative price in ``currency``, if stated.
        gross_amount: The ticket's stored gross amount, if stated.
        fees: Fees in ``currency``, if stated.
        taxes: Taxes in ``currency``, if stated.
        currency: Three upper-case letters.
        trade_date: Intended trade date, if stated.
        settlement_date: Intended settlement date, if stated. When present it
            may not precede ``trade_date``.
        message: Free text for the human at the provider, or ``None``.
    """

    schema_version: int
    message_type: str
    correlation_id: str
    ticket_kind: str
    direction: str | None
    identifier_scheme: str | None
    identifier_value: str | None
    instrument_name: str | None
    units: Decimal | None
    price_per_unit: Decimal | None
    gross_amount: Decimal | None
    fees: Decimal | None
    taxes: Decimal | None
    currency: str
    trade_date: date | None
    settlement_date: date | None
    message: str | None


@dataclass(frozen=True, slots=True)
class ExportEnvelope:
    """The downloadable artefact a sealed export becomes.

    Routing plus ciphertext. **There is no plaintext member of any kind, by
    design**: the whole point of sealing an export is that the artefact on
    disk says who it is for and nothing about what it asks for, and a
    convenience field holding the payload would undo that in one line.

    The routing members that do appear are the ones a human or a portal needs
    before it can decrypt anything — which provider, which key, which
    correlated object — and each is either public already (the recipient's
    own key) or meaningless without the private key.

    Attributes:
        schema_version: Always :data:`EXPORT_SCHEMA_VERSION` for this build.
        provider_id: The recipient's directory id.
        encryption_key_type: One of
            :data:`~services.provider_channel.directory.SUPPORTED_ENCRYPTION_KEY_TYPES`.
        recipient_public_key: 64 lower-case hex characters (32 bytes) — the
            key the ciphertext was sealed to, restated so a portal can tell
            at a glance whether it holds the matching private key.
        sender_tenant_handle: The sending tenant's channel handle, or
            ``None``. Optional in version 1 because no handle is issued
            before B-2 and the export leaves the instance by hand; a later
            version may require it.
        correlation_id: The correlated object's id as text, copied from the
            payload so a returning confirmation can be matched without
            decrypting anything.
        message_type: One of :data:`MESSAGE_TYPES`, likewise copied.
        exported_at: Offset-bearing timestamp, supplied by the caller and
            never read from a clock here (D-clock).
        ciphertext: The sealed box, standard-alphabet base64.
    """

    schema_version: int
    provider_id: str
    encryption_key_type: str
    recipient_public_key: str
    sender_tenant_handle: str | None
    correlation_id: str
    message_type: str
    exported_at: datetime
    ciphertext: str


# ---------------------------------------------------------------------------
# Key inventories
# ---------------------------------------------------------------------------

_KEY_SCHEMA_VERSION: Final[str] = "schema_version"

_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        _KEY_SCHEMA_VERSION,
        "sender_tenant_handle",
        "provider_id",
        "message_type",
        "correlation_id",
        "status",
        "created_at",
        "updated_at",
    }
)

_FILL_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"settlement_date", "isin", "message"})

_FILL_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            _KEY_SCHEMA_VERSION,
            "units",
            "price_per_unit",
            "fees",
            "taxes",
            "currency",
            "trade_date",
        }
    )
    | _FILL_OPTIONAL_KEYS
)

_EXPORT_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "direction",
        "identifier_scheme",
        "identifier_value",
        "instrument_name",
        "units",
        "price_per_unit",
        "gross_amount",
        "fees",
        "taxes",
        "trade_date",
        "settlement_date",
        "message",
    }
)

_EXPORT_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            _KEY_SCHEMA_VERSION,
            "message_type",
            "correlation_id",
            "ticket_kind",
            "currency",
        }
    )
    | _EXPORT_OPTIONAL_KEYS
)

_EXPORT_ENVELOPE_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"sender_tenant_handle"})

_EXPORT_ENVELOPE_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            _KEY_SCHEMA_VERSION,
            "provider_id",
            "encryption_key_type",
            "recipient_public_key",
            "correlation_id",
            "message_type",
            "exported_at",
            "ciphertext",
        }
    )
    | _EXPORT_ENVELOPE_OPTIONAL_KEYS
)

_SHAPE_ENVELOPE: Final[str] = "envelope"
_SHAPE_FILL: Final[str] = "fill"
_SHAPE_EXPORT: Final[str] = "export"
_SHAPE_EXPORT_ENVELOPE: Final[str] = "export_envelope"

#: Hex length of a 32-byte key, as carried by a directory entry.
_KEY_HEX_LENGTH: Final[int] = 64

#: The lower-case hex alphabet. Upper case is refused rather than folded: one
#: spelling of a key means one set of bytes to compare, here and in a portal.
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")

#: Shortest byte string a sealed box can be — the ephemeral public key plus
#: the Poly1305 MAC, with an empty plaintext. Re-declared here as a literal
#: rather than imported from :data:`sealed_box.SEALED_BOX_OVERHEAD`, because
#: this module parses documents and must not pull ``nacl`` in behind it: a
#: reader that cannot decrypt should not need libsodium to check a shape.
_SEALED_BOX_OVERHEAD: Final[int] = 48


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------


def _check_version(data: Mapping[str, object], *, shape: str, expected: int) -> None:
    """Refuse a document whose declared version this build does not read."""
    if _KEY_SCHEMA_VERSION not in data:
        raise UnknownSchemaVersion(
            f"{shape} document has no {_KEY_SCHEMA_VERSION}; "
            f"this build reads version {expected} only",
            shape=shape,
            version=None,
        )
    version = data[_KEY_SCHEMA_VERSION]
    if isinstance(version, bool) or not isinstance(version, int) or version != expected:
        raise UnknownSchemaVersion(
            f"unsupported {shape} {_KEY_SCHEMA_VERSION} {version!r}; "
            f"this build reads version {expected} only",
            shape=shape,
            version=version,
        )


def _check_keys(
    data: Mapping[str, object], *, shape: str, allowed: frozenset[str], required: frozenset[str]
) -> None:
    """Refuse unknown keys and absent required keys, naming both."""
    present = set(data)
    unknown = sorted(present - allowed)
    if unknown:
        raise SchemaError(
            f"{shape} document carries unknown keys {unknown}; "
            "this build refuses what it was not taught to read"
        )
    missing = sorted(required - present)
    if missing:
        raise SchemaError(f"{shape} document is missing required keys {missing}")


def _as_str(data: Mapping[str, object], key: str, *, shape: str) -> str:
    """Read a required non-empty string field."""
    value = data[key]
    if not isinstance(value, str):
        raise SchemaError(f"{shape}.{key} must be a string, got {type(value).__name__}")
    if not value:
        raise SchemaError(f"{shape}.{key} must not be empty")
    return value


def _as_optional_str(data: Mapping[str, object], key: str, *, shape: str) -> str | None:
    """Read an optional string field; absent and ``null`` both mean ``None``."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{shape}.{key} must be a string or null, got {type(value).__name__}")
    return value


def _as_member(data: Mapping[str, object], key: str, *, shape: str, allowed: frozenset[str]) -> str:
    """Read a string field constrained to a closed vocabulary."""
    value = _as_str(data, key, shape=shape)
    if value not in allowed:
        raise SchemaError(f"{shape}.{key} {value!r} is not one of {sorted(allowed)}")
    return value


def _as_decimal(data: Mapping[str, object], key: str, *, shape: str) -> Decimal:
    """Read a money field stated as a JSON string.

    A JSON number is refused: the point of the string encoding is that the
    provider's digits reach the ticket unrounded, and a float would already
    have lost them by the time this function saw the value.
    """
    value = data[key]
    if not isinstance(value, str):
        raise SchemaError(
            f"{shape}.{key} must be a decimal string such as '123.45', "
            f"got {type(value).__name__} — JSON numbers are refused for money fields"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise SchemaError(f"{shape}.{key} {value!r} is not a decimal") from None
    if not parsed.is_finite():
        raise SchemaError(f"{shape}.{key} {value!r} is not a finite decimal")
    return parsed


def _as_optional_decimal(data: Mapping[str, object], key: str, *, shape: str) -> Decimal | None:
    """Read an optional money field; absent and ``null`` both mean ``None``.

    A value that *is* present obeys :func:`_as_decimal` in full — optional
    means "may be omitted", never "may be sloppy".
    """
    if data.get(key) is None:
        return None
    return _as_decimal(data, key, shape=shape)


def _as_hex_key(data: Mapping[str, object], key: str, *, shape: str, length: int) -> str:
    """Read a fixed-length lower-case hex key.

    Upper case is refused rather than folded: a key has one spelling on the
    wire, so two documents naming the same recipient are byte-identical and a
    portal comparing them needs no normalisation step of its own.
    """
    value = _as_str(data, key, shape=shape)
    if len(value) != length or any(character not in _HEX_DIGITS for character in value):
        raise SchemaError(f"{shape}.{key} must be exactly {length} lower-case hex characters")
    return value


def _as_base64(
    data: Mapping[str, object], key: str, *, shape: str, minimum_decoded_length: int
) -> str:
    """Read a standard-alphabet base64 field and check what it decodes to.

    The decoded length is checked rather than the encoded one: the point is
    that the field could plausibly *be* a sealed box, and a ciphertext too
    short to hold an ephemeral key and a MAC is not one however well-formed
    its base64 is.
    """
    value = _as_str(data, key, shape=shape)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise SchemaError(f"{shape}.{key} is not standard-alphabet base64") from None
    if len(decoded) < minimum_decoded_length:
        raise SchemaError(
            f"{shape}.{key} decodes to {len(decoded)} bytes; "
            f"at least {minimum_decoded_length} are required"
        )
    return value


def _as_date(data: Mapping[str, object], key: str, *, shape: str) -> date:
    """Read a required ``YYYY-MM-DD`` date."""
    value = data[key]
    return _parse_date_value(value, field=f"{shape}.{key}")


def _as_optional_date(data: Mapping[str, object], key: str, *, shape: str) -> date | None:
    """Read an optional ``YYYY-MM-DD`` date; absent and ``null`` mean ``None``."""
    value = data.get(key)
    if value is None:
        return None
    return _parse_date_value(value, field=f"{shape}.{key}")


def _parse_date_value(value: object, *, field: str) -> date:
    """Parse one strict ISO ``YYYY-MM-DD`` calendar date.

    ``date.fromisoformat`` accepts more spellings than the format allows
    (``20260907`` among them), so the shape is checked before it is parsed.
    """
    if not isinstance(value, str):
        raise SchemaError(f"{field} must be a 'YYYY-MM-DD' string, got {type(value).__name__}")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise SchemaError(f"{field} {value!r} is not a 'YYYY-MM-DD' date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SchemaError(f"{field} {value!r} is not a 'YYYY-MM-DD' date") from None


def _as_datetime(data: Mapping[str, object], key: str, *, shape: str) -> datetime:
    """Read a required offset-bearing ISO-8601 timestamp."""
    value = data[key]
    if not isinstance(value, str):
        raise SchemaError(f"{shape}.{key} must be an ISO-8601 string, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SchemaError(f"{shape}.{key} {value!r} is not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError(
            f"{shape}.{key} {value!r} is naive; the envelope crosses time zones "
            "and every timestamp on it must carry an offset"
        )
    return parsed


def _check_currency(value: str, *, field: str) -> str:
    """Refuse anything that is not three upper-case letters."""
    if len(value) != 3 or not value.isalpha() or not value.isupper() or not value.isascii():
        raise SchemaError(f"{field} {value!r} must be a three-letter upper-case currency code")
    return value


# ---------------------------------------------------------------------------
# Parsers — the only constructors callers should use
# ---------------------------------------------------------------------------


def parse_envelope(data: Mapping[str, object]) -> Envelope:
    """Parse a routing envelope, refusing anything this build does not read.

    Args:
        data: The decoded JSON object.

    Returns:
        The parsed :class:`Envelope`.

    Raises:
        UnknownSchemaVersion: If ``schema_version`` is absent or is not
            :data:`ENVELOPE_SCHEMA_VERSION`.
        SchemaError: If a key is unknown or missing, a value has the wrong
            type, ``message_type`` or ``status`` is outside its vocabulary,
            or a timestamp is naive.
    """
    _check_version(data, shape=_SHAPE_ENVELOPE, expected=ENVELOPE_SCHEMA_VERSION)
    _check_keys(data, shape=_SHAPE_ENVELOPE, allowed=_ENVELOPE_KEYS, required=_ENVELOPE_KEYS)
    return Envelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        sender_tenant_handle=_as_str(data, "sender_tenant_handle", shape=_SHAPE_ENVELOPE),
        provider_id=_as_str(data, "provider_id", shape=_SHAPE_ENVELOPE),
        message_type=_as_member(data, "message_type", shape=_SHAPE_ENVELOPE, allowed=MESSAGE_TYPES),
        correlation_id=_as_str(data, "correlation_id", shape=_SHAPE_ENVELOPE),
        status=_as_member(data, "status", shape=_SHAPE_ENVELOPE, allowed=ENVELOPE_STATUSES),
        created_at=_as_datetime(data, "created_at", shape=_SHAPE_ENVELOPE),
        updated_at=_as_datetime(data, "updated_at", shape=_SHAPE_ENVELOPE),
    )


def parse_fill(data: Mapping[str, object]) -> FillPayload:
    """Parse a structured confirmation, refusing anything this build does not read.

    Args:
        data: The decoded JSON object.

    Returns:
        The parsed :class:`FillPayload`.

    Raises:
        UnknownSchemaVersion: If ``schema_version`` is absent or is not
            :data:`FILL_SCHEMA_VERSION`.
        SchemaError: If a key is unknown or missing, a money field is not a
            decimal string, the currency is malformed, or a stated
            ``settlement_date`` precedes ``trade_date``.
    """
    _check_version(data, shape=_SHAPE_FILL, expected=FILL_SCHEMA_VERSION)
    _check_keys(
        data,
        shape=_SHAPE_FILL,
        allowed=_FILL_KEYS,
        required=_FILL_KEYS - _FILL_OPTIONAL_KEYS,
    )
    trade_date = _as_date(data, "trade_date", shape=_SHAPE_FILL)
    settlement_date = _as_optional_date(data, "settlement_date", shape=_SHAPE_FILL)
    if settlement_date is not None and settlement_date < trade_date:
        raise SchemaError(
            f"fill.settlement_date {settlement_date.isoformat()} precedes "
            f"fill.trade_date {trade_date.isoformat()}"
        )
    return FillPayload(
        schema_version=FILL_SCHEMA_VERSION,
        units=_as_decimal(data, "units", shape=_SHAPE_FILL),
        price_per_unit=_as_decimal(data, "price_per_unit", shape=_SHAPE_FILL),
        fees=_as_decimal(data, "fees", shape=_SHAPE_FILL),
        taxes=_as_decimal(data, "taxes", shape=_SHAPE_FILL),
        currency=_check_currency(
            _as_str(data, "currency", shape=_SHAPE_FILL), field="fill.currency"
        ),
        trade_date=trade_date,
        settlement_date=settlement_date,
        isin=_as_optional_str(data, "isin", shape=_SHAPE_FILL),
        message=_as_optional_str(data, "message", shape=_SHAPE_FILL),
    )


def parse_export(data: Mapping[str, object]) -> ExportPayload:
    """Parse a sealed export's plaintext, refusing anything this build does not read.

    This is the parser a portal runs on what it decrypted, and the one this
    build runs on its own artefact before handing it out, so a payload that
    could not be read back is never produced in the first place.

    Args:
        data: The decoded JSON object.

    Returns:
        The parsed :class:`ExportPayload`.

    Raises:
        UnknownSchemaVersion: If ``schema_version`` is absent or is not
            :data:`EXPORT_SCHEMA_VERSION`.
        SchemaError: If a key is unknown or missing, a value has the wrong
            type, ``message_type`` / ``ticket_kind`` / ``direction`` is
            outside its vocabulary, the identifier scheme and value are not
            both present or both absent, no instrument is named at all, or a
            stated ``settlement_date`` precedes a stated ``trade_date``.
    """
    _check_version(data, shape=_SHAPE_EXPORT, expected=EXPORT_SCHEMA_VERSION)
    _check_keys(
        data,
        shape=_SHAPE_EXPORT,
        allowed=_EXPORT_KEYS,
        required=_EXPORT_KEYS - _EXPORT_OPTIONAL_KEYS,
    )

    direction = _as_optional_str(data, "direction", shape=_SHAPE_EXPORT)
    if direction is not None and direction not in EXPORT_DIRECTIONS:
        raise SchemaError(
            f"export.direction {direction!r} is not one of {sorted(EXPORT_DIRECTIONS)}"
        )

    identifier_scheme = _as_optional_str(data, "identifier_scheme", shape=_SHAPE_EXPORT)
    identifier_value = _as_optional_str(data, "identifier_value", shape=_SHAPE_EXPORT)
    if (identifier_scheme is None) != (identifier_value is None):
        raise SchemaError(
            "export.identifier_scheme and export.identifier_value must be present "
            "together or absent together; half an identifier names nothing"
        )

    instrument_name = _as_optional_str(data, "instrument_name", shape=_SHAPE_EXPORT)
    if identifier_scheme is None and instrument_name is None:
        raise SchemaError(
            "export payload names no instrument: give an export.identifier_scheme "
            "with its export.identifier_value, an export.instrument_name, or both"
        )

    trade_date = _as_optional_date(data, "trade_date", shape=_SHAPE_EXPORT)
    settlement_date = _as_optional_date(data, "settlement_date", shape=_SHAPE_EXPORT)
    if trade_date is not None and settlement_date is not None and settlement_date < trade_date:
        raise SchemaError(
            f"export.settlement_date {settlement_date.isoformat()} precedes "
            f"export.trade_date {trade_date.isoformat()}"
        )

    return ExportPayload(
        schema_version=EXPORT_SCHEMA_VERSION,
        message_type=_as_member(data, "message_type", shape=_SHAPE_EXPORT, allowed=MESSAGE_TYPES),
        correlation_id=_as_str(data, "correlation_id", shape=_SHAPE_EXPORT),
        ticket_kind=_as_member(data, "ticket_kind", shape=_SHAPE_EXPORT, allowed=TICKET_KINDS),
        direction=direction,
        identifier_scheme=identifier_scheme,
        identifier_value=identifier_value,
        instrument_name=instrument_name,
        units=_as_optional_decimal(data, "units", shape=_SHAPE_EXPORT),
        price_per_unit=_as_optional_decimal(data, "price_per_unit", shape=_SHAPE_EXPORT),
        gross_amount=_as_optional_decimal(data, "gross_amount", shape=_SHAPE_EXPORT),
        fees=_as_optional_decimal(data, "fees", shape=_SHAPE_EXPORT),
        taxes=_as_optional_decimal(data, "taxes", shape=_SHAPE_EXPORT),
        currency=_check_currency(
            _as_str(data, "currency", shape=_SHAPE_EXPORT), field="export.currency"
        ),
        trade_date=trade_date,
        settlement_date=settlement_date,
        message=_as_optional_str(data, "message", shape=_SHAPE_EXPORT),
    )


def parse_export_envelope(data: Mapping[str, object]) -> ExportEnvelope:
    """Parse a sealed export envelope, refusing anything this build does not read.

    Note what is *not* checked: whether the ciphertext opens. That needs a
    private key this side does not hold, so the envelope is judged on its
    shape alone and the recipient finds out the rest.

    Args:
        data: The decoded JSON object.

    Returns:
        The parsed :class:`ExportEnvelope`.

    Raises:
        UnknownSchemaVersion: If ``schema_version`` is absent or is not
            :data:`EXPORT_SCHEMA_VERSION`.
        SchemaError: If a key is unknown or missing, ``message_type`` or
            ``encryption_key_type`` is outside its vocabulary, the recipient
            key is not 64 lower-case hex characters, ``exported_at`` is
            naive, or the ciphertext is not base64 decoding to at least
            :data:`_SEALED_BOX_OVERHEAD` bytes.
    """
    _check_version(data, shape=_SHAPE_EXPORT_ENVELOPE, expected=EXPORT_SCHEMA_VERSION)
    _check_keys(
        data,
        shape=_SHAPE_EXPORT_ENVELOPE,
        allowed=_EXPORT_ENVELOPE_KEYS,
        required=_EXPORT_ENVELOPE_KEYS - _EXPORT_ENVELOPE_OPTIONAL_KEYS,
    )
    return ExportEnvelope(
        schema_version=EXPORT_SCHEMA_VERSION,
        provider_id=_as_str(data, "provider_id", shape=_SHAPE_EXPORT_ENVELOPE),
        encryption_key_type=_as_member(
            data,
            "encryption_key_type",
            shape=_SHAPE_EXPORT_ENVELOPE,
            allowed=SUPPORTED_ENCRYPTION_KEY_TYPES,
        ),
        recipient_public_key=_as_hex_key(
            data,
            "recipient_public_key",
            shape=_SHAPE_EXPORT_ENVELOPE,
            length=_KEY_HEX_LENGTH,
        ),
        sender_tenant_handle=_as_optional_str(
            data, "sender_tenant_handle", shape=_SHAPE_EXPORT_ENVELOPE
        ),
        correlation_id=_as_str(data, "correlation_id", shape=_SHAPE_EXPORT_ENVELOPE),
        message_type=_as_member(
            data, "message_type", shape=_SHAPE_EXPORT_ENVELOPE, allowed=MESSAGE_TYPES
        ),
        exported_at=_as_datetime(data, "exported_at", shape=_SHAPE_EXPORT_ENVELOPE),
        ciphertext=_as_base64(
            data,
            "ciphertext",
            shape=_SHAPE_EXPORT_ENVELOPE,
            minimum_decoded_length=_SEALED_BOX_OVERHEAD,
        ),
    )


# ---------------------------------------------------------------------------
# Serialisers — the parser's mirror, so Stage B has one wire format
# ---------------------------------------------------------------------------


def envelope_to_dict(envelope: Envelope) -> dict[str, object]:
    """Serialise an envelope to JSON-ready primitives.

    Args:
        envelope: The envelope to serialise.

    Returns:
        A dict that :func:`parse_envelope` reads back unchanged.
    """
    return {
        "schema_version": envelope.schema_version,
        "sender_tenant_handle": envelope.sender_tenant_handle,
        "provider_id": envelope.provider_id,
        "message_type": envelope.message_type,
        "correlation_id": envelope.correlation_id,
        "status": envelope.status,
        "created_at": envelope.created_at.isoformat(),
        "updated_at": envelope.updated_at.isoformat(),
    }


def fill_to_dict(fill: FillPayload) -> dict[str, object]:
    """Serialise a confirmation to JSON-ready primitives.

    Money is emitted as strings and dates as ISO days, so a round trip
    through JSON changes no digit.

    Args:
        fill: The confirmation to serialise.

    Returns:
        A dict that :func:`parse_fill` reads back unchanged. Optional fields
        are emitted explicitly as ``null`` rather than omitted.
    """
    return {
        "schema_version": fill.schema_version,
        "units": str(fill.units),
        "price_per_unit": str(fill.price_per_unit),
        "fees": str(fill.fees),
        "taxes": str(fill.taxes),
        "currency": fill.currency,
        "trade_date": fill.trade_date.isoformat(),
        "settlement_date": None
        if fill.settlement_date is None
        else fill.settlement_date.isoformat(),
        "isin": fill.isin,
        "message": fill.message,
    }


def _optional_decimal_text(value: Decimal | None) -> str | None:
    """Emit a money field as a string, or ``null`` when it is absent."""
    return None if value is None else str(value)


def _optional_date_text(value: date | None) -> str | None:
    """Emit a date as an ISO day, or ``null`` when it is absent."""
    return None if value is None else value.isoformat()


def export_to_dict(payload: ExportPayload) -> dict[str, object]:
    """Serialise a sealed export's plaintext to JSON-ready primitives.

    These are the primitives that become the sealed bytes, so the round trip
    has to be exact: money as strings and dates as ISO days, with every
    optional emitted explicitly as ``null`` rather than omitted, so the
    document a portal decrypts has the same keys whatever the caller left out.

    Args:
        payload: The plaintext to serialise.

    Returns:
        A dict that :func:`parse_export` reads back unchanged.
    """
    return {
        "schema_version": payload.schema_version,
        "message_type": payload.message_type,
        "correlation_id": payload.correlation_id,
        "ticket_kind": payload.ticket_kind,
        "direction": payload.direction,
        "identifier_scheme": payload.identifier_scheme,
        "identifier_value": payload.identifier_value,
        "instrument_name": payload.instrument_name,
        "units": _optional_decimal_text(payload.units),
        "price_per_unit": _optional_decimal_text(payload.price_per_unit),
        "gross_amount": _optional_decimal_text(payload.gross_amount),
        "fees": _optional_decimal_text(payload.fees),
        "taxes": _optional_decimal_text(payload.taxes),
        "currency": payload.currency,
        "trade_date": _optional_date_text(payload.trade_date),
        "settlement_date": _optional_date_text(payload.settlement_date),
        "message": payload.message,
    }


def export_envelope_to_dict(envelope: ExportEnvelope) -> dict[str, object]:
    """Serialise a sealed export envelope to JSON-ready primitives.

    Args:
        envelope: The envelope to serialise.

    Returns:
        A dict that :func:`parse_export_envelope` reads back unchanged.
        ``sender_tenant_handle`` is emitted explicitly as ``null`` when absent.
    """
    return {
        "schema_version": envelope.schema_version,
        "provider_id": envelope.provider_id,
        "encryption_key_type": envelope.encryption_key_type,
        "recipient_public_key": envelope.recipient_public_key,
        "sender_tenant_handle": envelope.sender_tenant_handle,
        "correlation_id": envelope.correlation_id,
        "message_type": envelope.message_type,
        "exported_at": envelope.exported_at.isoformat(),
        "ciphertext": envelope.ciphertext,
    }


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "ENVELOPE_STATUSES",
    "ENVELOPE_STATUS_ACKNOWLEDGED",
    "ENVELOPE_STATUS_DECLINED",
    "ENVELOPE_STATUS_EXECUTED",
    "ENVELOPE_STATUS_SENT",
    "EXPORT_DIRECTIONS",
    "EXPORT_DIRECTION_BUY",
    "EXPORT_DIRECTION_SELL",
    "EXPORT_SCHEMA_VERSION",
    "FILL_SCHEMA_VERSION",
    "MESSAGE_TYPES",
    "MESSAGE_TYPE_ENGAGEMENT",
    "MESSAGE_TYPE_ORDER",
    "Envelope",
    "ExportEnvelope",
    "ExportPayload",
    "FillPayload",
    "SchemaError",
    "UnknownSchemaVersion",
    "envelope_to_dict",
    "export_envelope_to_dict",
    "export_to_dict",
    "fill_to_dict",
    "parse_envelope",
    "parse_export",
    "parse_export_envelope",
    "parse_fill",
]
