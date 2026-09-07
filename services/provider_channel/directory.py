# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The signed provider directory, format v1 (ADR-0129 §2).

The directory is the list of providers an instance may talk to, published by
portfoliflow.com and signed with the key in
:mod:`services.provider_channel.publishing_key`. Everything here is about one
question — *may this document be believed?* — and the answer is produced by
:func:`verify_directory` alone.

**Signature before trust.** The shape parser is private
(``_parse_directory``) and is reached only after the signature, the format
version and the validity window have all passed. A :class:`Directory` that
was parsed but not verified is therefore not constructible through the public
API, which removes the class of bug where a caller means to verify, forgets,
and still ends up holding something that looks authoritative.

**Canonical bytes.** :func:`canonical_bytes` states the signing input once:
sorted keys, no whitespace, no NaN. A document is refused unless it is
*already* in that form, so "same content, other bytes" has no second answer
and a verifier cannot be talked into checking one spelling while a reader
consumes another. The signature travels **beside** the document, never inside
it, so a document never has to be edited to be checked.

**Named, not implemented.** ``encryption_key_type`` names the ADR-0129 D-1
sealed-box family and ``encryption_public_key`` carries its bytes as hex, but
Stage A encrypts nothing: the key's length is checked, its content is never
used. Likewise ``successor_key`` is parsed and handed on; acting on a
successor announcement is a Stage-B client rule.

Pure: no repository, no session, no clock (``now`` is injected — D-clock), no
network. The caller obtains the bytes however it likes and hands them in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from services.provider_channel.publishing_key import is_placeholder

# ---------------------------------------------------------------------------
# Format vocabulary
# ---------------------------------------------------------------------------

#: Wire version of the directory document.
DIRECTORY_FORMAT_VERSION: Final[int] = 1

#: The only signature scheme this build accepts (D-sig). The field is
#: versioned inside the format so Stage B can rotate schemes without a new
#: format version.
SIGNATURE_SCHEME_ED25519: Final[str] = "ed25519"

#: Signature schemes this build accepts.
SUPPORTED_SIGNATURE_SCHEMES: Final[frozenset[str]] = frozenset({SIGNATURE_SCHEME_ED25519})

#: ADR-0129 D-1 family; named, not implemented.
KEY_TYPE_X25519_SEALED_BOX: Final[str] = "x25519-sealed-box"

#: Encryption key types this build accepts in a directory entry.
SUPPORTED_ENCRYPTION_KEY_TYPES: Final[frozenset[str]] = frozenset({KEY_TYPE_X25519_SEALED_BOX})

#: What kind of counterparty an entry describes.
PROVIDER_TYPES: Final[frozenset[str]] = frozenset(
    {"broker", "secondary_desk", "advisory", "legal", "fund_selection", "other"}
)

#: The ticket kinds an entry may accept.
#:
#: mirrors services.transactions.constants.KINDS; pinned by
#: tests/services/provider_channel/test_contract.py
TICKET_KINDS: Final[frozenset[str]] = frozenset({"order", "commitment", "secondary"})

#: The engagement categories an entry may offer (ADR-0129 §5).
ENGAGEMENT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"advisory", "legal", "fund_selection", "second_opinion", "other"}
)

#: Hex length of a 32-byte key.
_KEY_HEX_LENGTH: Final[int] = 64

#: The member a document may never carry: the signature rides beside it.
_SIGNATURE_MEMBER: Final[str] = "signature"

# ---------------------------------------------------------------------------
# Errors — one per reason to refuse, so a caller can tell them apart
# ---------------------------------------------------------------------------


class DirectoryVerificationError(ValueError):
    """A directory document was refused. Base of every refusal below."""


class InvalidDirectorySignature(DirectoryVerificationError):
    """The signature did not verify, or the bytes were not canonical."""


class DirectoryExpired(DirectoryVerificationError):
    """The document is outside its validity window — stale, or not yet valid."""


class UnsupportedSignatureScheme(DirectoryVerificationError):
    """The document was signed with a scheme this build does not accept."""


class UnknownDirectoryFormatVersion(DirectoryVerificationError):
    """The document declares a format version this build does not read."""


class PublishingKeyNotConfigured(DirectoryVerificationError):
    """Verification was attempted against the un-minted placeholder key."""


class DirectoryShapeError(DirectoryVerificationError):
    """The document verified but did not match the v1 shape."""


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """One counterparty in the directory.

    Attributes:
        provider_id: Stable identifier, unique within the document.
        display_name: Human-readable name.
        provider_type: One of :data:`PROVIDER_TYPES`.
        ticket_kinds: Subset of :data:`TICKET_KINDS` this provider accepts.
        engagement_categories: Subset of :data:`ENGAGEMENT_CATEGORIES`
            (ADR-0129 §5).
        asset_classes: Free-form hints; no closed vocabulary, because the
            directory should not have an opinion about a tenant's taxonomy.
        jurisdictions: ISO 3166-1 alpha-2 codes, upper case.
        encryption_key_type: One of :data:`SUPPORTED_ENCRYPTION_KEY_TYPES`.
        encryption_public_key: 64 hex characters (32 bytes). Length-checked
            only; nothing in Stage A encrypts.
    """

    provider_id: str
    display_name: str
    provider_type: str
    ticket_kinds: frozenset[str]
    engagement_categories: frozenset[str]
    asset_classes: frozenset[str]
    jurisdictions: frozenset[str]
    encryption_key_type: str
    encryption_public_key: str


@dataclass(frozen=True, slots=True)
class SuccessorKey:
    """A successor-key announcement inside a still-valid document (ADR-0129 §2).

    Key rotation without a trusted channel is a chicken-and-egg problem, and
    this is the way out: a client that verified *this* document with the
    current key learns the next key from a document it already believes.
    Stage A parses the announcement; the rule for when a client starts using
    it is Stage B's.

    Attributes:
        publishing_key_id: Identifier of the announced key.
        public_key: 64 hex characters (32 bytes), Ed25519 public key.
        valid_from: The day the announced key takes over.
    """

    publishing_key_id: str
    public_key: str
    valid_from: date


@dataclass(frozen=True, slots=True)
class Directory:
    """A verified provider directory.

    Only :func:`verify_directory` produces one.

    Attributes:
        format_version: Always :data:`DIRECTORY_FORMAT_VERSION` for this build.
        directory_version: Monotonic publication counter — the ADR's version
            pin. A client refuses to move backwards; that rule is Stage B's.
        issued_at: First day the document is valid.
        valid_until: Last day the document is valid.
        signature_scheme: One of :data:`SUPPORTED_SIGNATURE_SCHEMES`.
        publishing_key_id: Identifier of the key that signed the document.
        successor_key: The announcement, if the document carries one.
        providers: Entries with unique ids, sorted by ``provider_id``.
    """

    format_version: int
    directory_version: int
    issued_at: date
    valid_until: date
    signature_scheme: str
    publishing_key_id: str
    successor_key: SuccessorKey | None
    providers: tuple[ProviderEntry, ...]


# ---------------------------------------------------------------------------
# Key inventories
# ---------------------------------------------------------------------------

_DIRECTORY_OPTIONAL_KEYS: Final[frozenset[str]] = frozenset({"successor_key"})

_DIRECTORY_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            "format_version",
            "directory_version",
            "issued_at",
            "valid_until",
            "signature_scheme",
            "publishing_key_id",
            "providers",
        }
    )
    | _DIRECTORY_OPTIONAL_KEYS
)

_PROVIDER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "provider_id",
        "display_name",
        "provider_type",
        "ticket_kinds",
        "engagement_categories",
        "asset_classes",
        "jurisdictions",
        "encryption_key_type",
        "encryption_public_key",
    }
)

_SUCCESSOR_KEYS: Final[frozenset[str]] = frozenset(
    {"publishing_key_id", "public_key", "valid_from"}
)


# ---------------------------------------------------------------------------
# Canonical serialisation
# ---------------------------------------------------------------------------


def canonical_bytes(document: Mapping[str, object]) -> bytes:
    """Serialise a directory document to the one byte string that gets signed.

    Sorted keys, no whitespace, UTF-8, no ``NaN``/``Infinity``. This is *the*
    signing input: :func:`verify_directory` refuses any document whose bytes
    do not already round-trip through this function unchanged.

    Args:
        document: The document as JSON-ready primitives, without a
            ``signature`` member.

    Returns:
        The canonical UTF-8 bytes.
    """
    return json.dumps(
        dict(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sign_directory(document: Mapping[str, object], *, private_key: bytes) -> tuple[bytes, bytes]:
    """Sign a directory document, returning what a publisher must distribute.

    One signer beside the verifier, so the two cannot disagree about which
    bytes matter. The production private key is minted and held by the
    operator and never lives in this repository; this function exists for
    tests and for Stage B's publishing tool.

    Args:
        document: The document as JSON-ready primitives, without a
            ``signature`` member.
        private_key: Raw 32-byte Ed25519 private key seed.

    Returns:
        ``(canonical_bytes, signature)`` — distribute both, side by side.
    """
    payload = canonical_bytes(document)
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(payload)
    return payload, signature


# ---------------------------------------------------------------------------
# Shape readers (private — reached only after the signature has verified)
# ---------------------------------------------------------------------------


def _check_keys(
    data: Mapping[str, object], *, where: str, allowed: frozenset[str], required: frozenset[str]
) -> None:
    """Refuse unknown keys and absent required keys, naming both."""
    present = set(data)
    unknown = sorted(present - allowed)
    if unknown:
        raise DirectoryShapeError(f"{where} carries unknown keys {unknown}")
    missing = sorted(required - present)
    if missing:
        raise DirectoryShapeError(f"{where} is missing required keys {missing}")


def _shape_str(data: Mapping[str, object], key: str, *, where: str) -> str:
    """Read a required non-empty string.

    ``.get`` rather than ``[]``: the validity window is read before the
    required-key check runs, so an absent key must surface as a shape error
    like any other, never as a bare ``KeyError``.
    """
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DirectoryShapeError(f"{where}.{key} must be a non-empty string")
    return value


def _shape_int(data: Mapping[str, object], key: str, *, where: str) -> int:
    """Read a required integer, refusing booleans and absent keys."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DirectoryShapeError(f"{where}.{key} must be an integer")
    return value


def _shape_date(data: Mapping[str, object], key: str, *, where: str) -> date:
    """Read a required strict ``YYYY-MM-DD`` date, refusing an absent key."""
    value = data.get(key)
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise DirectoryShapeError(f"{where}.{key} must be a 'YYYY-MM-DD' date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DirectoryShapeError(f"{where}.{key} {value!r} is not a 'YYYY-MM-DD' date") from None


def _shape_hex_key(data: Mapping[str, object], key: str, *, where: str) -> str:
    """Read a 32-byte key stated as 64 hex characters."""
    value = _shape_str(data, key, where=where)
    if len(value) != _KEY_HEX_LENGTH:
        raise DirectoryShapeError(
            f"{where}.{key} must be {_KEY_HEX_LENGTH} hex characters (32 bytes), got {len(value)}"
        )
    try:
        bytes.fromhex(value)
    except ValueError:
        raise DirectoryShapeError(f"{where}.{key} is not hexadecimal") from None
    return value


def _shape_member_set(
    data: Mapping[str, object], key: str, *, where: str, allowed: frozenset[str] | None
) -> frozenset[str]:
    """Read a list of strings, optionally constrained to a closed vocabulary."""
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DirectoryShapeError(f"{where}.{key} must be a list of strings")
    members = frozenset(value)
    if allowed is not None:
        unknown = sorted(members - allowed)
        if unknown:
            raise DirectoryShapeError(
                f"{where}.{key} carries unknown members {unknown}; allowed are {sorted(allowed)}"
            )
    return members


def _parse_provider(data: Mapping[str, object], *, index: int) -> ProviderEntry:
    """Parse one provider entry."""
    where = f"providers[{index}]"
    _check_keys(data, where=where, allowed=_PROVIDER_KEYS, required=_PROVIDER_KEYS)
    provider_type = _shape_str(data, "provider_type", where=where)
    if provider_type not in PROVIDER_TYPES:
        raise DirectoryShapeError(
            f"{where}.provider_type {provider_type!r} is not one of {sorted(PROVIDER_TYPES)}"
        )
    encryption_key_type = _shape_str(data, "encryption_key_type", where=where)
    if encryption_key_type not in SUPPORTED_ENCRYPTION_KEY_TYPES:
        raise DirectoryShapeError(
            f"{where}.encryption_key_type {encryption_key_type!r} is not one of "
            f"{sorted(SUPPORTED_ENCRYPTION_KEY_TYPES)}"
        )
    jurisdictions = _shape_member_set(data, "jurisdictions", where=where, allowed=None)
    for code in sorted(jurisdictions):
        if len(code) != 2 or not code.isalpha() or not code.isupper() or not code.isascii():
            raise DirectoryShapeError(
                f"{where}.jurisdictions {code!r} is not an ISO 3166-1 alpha-2 code"
            )
    return ProviderEntry(
        provider_id=_shape_str(data, "provider_id", where=where),
        display_name=_shape_str(data, "display_name", where=where),
        provider_type=provider_type,
        ticket_kinds=_shape_member_set(data, "ticket_kinds", where=where, allowed=TICKET_KINDS),
        engagement_categories=_shape_member_set(
            data, "engagement_categories", where=where, allowed=ENGAGEMENT_CATEGORIES
        ),
        asset_classes=_shape_member_set(data, "asset_classes", where=where, allowed=None),
        jurisdictions=jurisdictions,
        encryption_key_type=encryption_key_type,
        encryption_public_key=_shape_hex_key(data, "encryption_public_key", where=where),
    )


def _parse_successor(data: Mapping[str, object]) -> SuccessorKey:
    """Parse the successor-key announcement."""
    where = "successor_key"
    _check_keys(data, where=where, allowed=_SUCCESSOR_KEYS, required=_SUCCESSOR_KEYS)
    return SuccessorKey(
        publishing_key_id=_shape_str(data, "publishing_key_id", where=where),
        public_key=_shape_hex_key(data, "public_key", where=where),
        valid_from=_shape_date(data, "valid_from", where=where),
    )


def _parse_directory(document: Mapping[str, object]) -> Directory:
    """Parse a *verified* document into a :class:`Directory`.

    Private, and kept out of ``__all__``, because a parsed-but-unverified
    directory must not be constructible through the public API: the only way
    to hold a :class:`Directory` is to have proved the signature first.
    """
    where = "directory"
    _check_keys(
        document,
        where=where,
        allowed=_DIRECTORY_KEYS,
        required=_DIRECTORY_KEYS - _DIRECTORY_OPTIONAL_KEYS,
    )
    raw_providers = document["providers"]
    if not isinstance(raw_providers, list):
        raise DirectoryShapeError("directory.providers must be a list")
    entries: list[ProviderEntry] = []
    for index, raw_entry in enumerate(raw_providers):
        if not isinstance(raw_entry, dict):
            raise DirectoryShapeError(f"providers[{index}] must be an object")
        entries.append(_parse_provider(raw_entry, index=index))
    identifiers = [entry.provider_id for entry in entries]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        raise DirectoryShapeError(f"directory.providers repeats provider_id {duplicates}")

    raw_successor = document.get("successor_key")
    if raw_successor is None:
        successor = None
    elif isinstance(raw_successor, dict):
        successor = _parse_successor(raw_successor)
    else:
        raise DirectoryShapeError("directory.successor_key must be an object or null")

    signature_scheme = _shape_str(document, "signature_scheme", where=where)
    return Directory(
        format_version=_shape_int(document, "format_version", where=where),
        directory_version=_shape_int(document, "directory_version", where=where),
        issued_at=_shape_date(document, "issued_at", where=where),
        valid_until=_shape_date(document, "valid_until", where=where),
        signature_scheme=signature_scheme,
        publishing_key_id=_shape_str(document, "publishing_key_id", where=where),
        successor_key=successor,
        providers=tuple(sorted(entries, key=lambda entry: entry.provider_id)),
    )


# ---------------------------------------------------------------------------
# The trust gate
# ---------------------------------------------------------------------------


def verify_directory(
    document_bytes: bytes,
    signature: bytes,
    *,
    publishing_key: bytes,
    now: date,
) -> Directory:
    """Verify a signed directory and return it, or refuse it.

    The checks run in a fixed order, each fail-closed and each with its own
    exception, so a caller (and a log line) can say exactly why a document was
    refused:

    1. the publishing key is not the un-minted placeholder;
    2. the bytes are a JSON object in canonical form, carrying no
       ``signature`` member;
    3. the format version is one this build reads;
    4. the signature scheme is one this build accepts;
    5. the signature verifies over ``document_bytes``;
    6. ``now`` lies inside the validity window;
    7. the document matches the v1 shape.

    Steps 3 and 4 deliberately precede the signature check: refusing a
    document this build cannot read is cheaper and clearer than reporting a
    signature failure for it.

    Args:
        document_bytes: The canonical document bytes, exactly as published.
        signature: The detached signature over ``document_bytes``.
        publishing_key: Raw 32-byte Ed25519 public key. No default —
            production callers pass
            :data:`~services.provider_channel.publishing_key.PUBLISHING_KEY`
            explicitly, tests pass their own.
        now: The day to judge validity against; injected, never read from a
            clock (D-clock).

    Returns:
        The verified :class:`Directory`.

    Raises:
        PublishingKeyNotConfigured: If ``publishing_key`` is the placeholder.
        InvalidDirectorySignature: If the bytes are not canonical JSON, carry
            a ``signature`` member, or the signature does not verify.
        UnknownDirectoryFormatVersion: If ``format_version`` is not
            :data:`DIRECTORY_FORMAT_VERSION`.
        UnsupportedSignatureScheme: If ``signature_scheme`` is unsupported.
        DirectoryExpired: If ``now`` is outside the validity window.
        DirectoryShapeError: If the verified document does not match v1.
    """
    # (1) Nothing may be trusted against the un-minted key.
    if is_placeholder(publishing_key):
        raise PublishingKeyNotConfigured(
            "the portfoliflow.com publishing key is still the Stage-A placeholder; "
            "directory verification fails closed until the real key is minted"
        )

    # (2) One document, one byte string.
    try:
        decoded = json.loads(document_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidDirectorySignature("document is not valid UTF-8 JSON") from None
    if not isinstance(decoded, dict):
        raise InvalidDirectorySignature("document is not a JSON object")
    if _SIGNATURE_MEMBER in decoded:
        raise InvalidDirectorySignature(
            "document carries a 'signature' member; the signature travels beside "
            "the document, never inside it"
        )
    if canonical_bytes(decoded) != document_bytes:
        raise InvalidDirectorySignature(
            "document is not in canonical form; only the canonical byte string "
            "is signed, so a re-spelled document cannot be verified"
        )

    # (3) A version this build reads.
    declared_version = decoded.get("format_version")
    if isinstance(declared_version, bool) or declared_version != DIRECTORY_FORMAT_VERSION:
        raise UnknownDirectoryFormatVersion(
            f"unsupported directory format_version {declared_version!r}; "
            f"this build reads version {DIRECTORY_FORMAT_VERSION} only"
        )

    # (4) A scheme this build accepts.
    declared_scheme = decoded.get("signature_scheme")
    if declared_scheme not in SUPPORTED_SIGNATURE_SCHEMES:
        raise UnsupportedSignatureScheme(
            f"unsupported signature_scheme {declared_scheme!r}; "
            f"this build accepts {sorted(SUPPORTED_SIGNATURE_SCHEMES)}"
        )

    # (5) The signature itself.
    try:
        Ed25519PublicKey.from_public_bytes(publishing_key).verify(signature, document_bytes)
    except InvalidSignature:
        raise InvalidDirectorySignature(
            "directory signature does not verify against the publishing key"
        ) from None
    except ValueError:
        raise InvalidDirectorySignature(
            "the supplied publishing key is not a valid Ed25519 public key"
        ) from None

    # (6) Inside its window. A document from the future is as untrustworthy as
    #     one from the past — both mean the client is not seeing the current
    #     publication.
    issued_at = _shape_date(decoded, "issued_at", where="directory")
    valid_until = _shape_date(decoded, "valid_until", where="directory")
    if now < issued_at:
        raise DirectoryExpired(
            f"directory is not yet valid: issued_at {issued_at.isoformat()} is after "
            f"{now.isoformat()} — a document from the future is stale too"
        )
    if now > valid_until:
        raise DirectoryExpired(
            f"directory expired: valid_until {valid_until.isoformat()} is before {now.isoformat()}"
        )

    # (7) Only now is a Directory allowed to exist.
    return _parse_directory(decoded)


__all__ = [
    "DIRECTORY_FORMAT_VERSION",
    "ENGAGEMENT_CATEGORIES",
    "KEY_TYPE_X25519_SEALED_BOX",
    "PROVIDER_TYPES",
    "SIGNATURE_SCHEME_ED25519",
    "SUPPORTED_ENCRYPTION_KEY_TYPES",
    "SUPPORTED_SIGNATURE_SCHEMES",
    "TICKET_KINDS",
    "Directory",
    "DirectoryExpired",
    "DirectoryShapeError",
    "DirectoryVerificationError",
    "InvalidDirectorySignature",
    "ProviderEntry",
    "PublishingKeyNotConfigured",
    "SuccessorKey",
    "UnknownDirectoryFormatVersion",
    "UnsupportedSignatureScheme",
    "canonical_bytes",
    "sign_directory",
    "verify_directory",
]
