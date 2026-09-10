# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The provider-channel contract — ADR-0129 Stage A.

**Contract only: no service is built here, and the channel states stay
unreachable.** ADR-0128 defined ``sent`` / ``acknowledged`` / ``executed`` in
the schema from day one and left them unwritable; nothing in this package
changes that. What lands here is the vocabulary the two sides of the channel
must agree on before any of it can be armed: a versioned routing envelope and
structured confirmation (:mod:`~services.provider_channel.schemas`), the
signed provider-directory format and its trust gate
(:mod:`~services.provider_channel.directory`), the publishing key the gate
verifies against (:mod:`~services.provider_channel.publishing_key`), and the
seam that turns a confirmation into proposed booking fields
(:mod:`~services.provider_channel.prefill`).

**Purity contract.** The package imports the standard library and
``cryptography`` (Ed25519 and ``InvalidSignature``) and nothing else. It
reaches no database, no ORM, no HTTP client, no web framework, and — the
structural point — nothing under the transactions package, whose own
``__init__`` would drag the whole ticket-service graph in behind it. Where a
vocabulary is shared with that package (the three channel statuses, the
master-data keys, the ISIN scheme, the ticket kinds) the literal is
re-declared locally beside a comment naming its twin, and the contract test
asserts the two are equal: deliberate duplication of a string, with drift
turned into a test failure. There is no network code, and nothing encrypts
or decrypts.

Decisions of record for this stage:

* **D-sig** — Ed25519 through the existing ``cryptography`` dependency; no new
  dependency, and ``signature_scheme`` is versioned inside the format so a
  scheme can be rotated without a new format version.
* **D-flag** — no feature flag. The channel is *absent*, not *disabled*:
  nothing reads a setting because nothing is wired, and Stage B declares
  ``provider_channel.enabled`` through an ADR-0112 annex ADR.
* **D-clock** — no clock is read here; ``verify_directory`` takes ``now`` as
  an explicit argument.
* **D-amounts** — the pre-fill maps, it never computes; net arithmetic stays
  the ticket layer's single answer.

Stage B (directory service, relay, portal) is a separate project and adds
runtime code to this package or beside it; it does not change the schema
versions declared here without bumping them.
"""

from __future__ import annotations

from services.provider_channel.directory import (
    DIRECTORY_FORMAT_VERSION,
    KEY_TYPE_X25519_SEALED_BOX,
    PROVIDER_TYPES,
    SIGNATURE_SCHEME_ED25519,
    SUPPORTED_ENCRYPTION_KEY_TYPES,
    SUPPORTED_SIGNATURE_SCHEMES,
    Directory,
    DirectoryExpired,
    DirectoryShapeError,
    DirectoryVerificationError,
    InvalidDirectorySignature,
    ProviderEntry,
    PublishingKeyNotConfigured,
    SuccessorKey,
    UnknownDirectoryFormatVersion,
    UnsupportedSignatureScheme,
    canonical_bytes,
    sign_directory,
    verify_directory,
)
from services.provider_channel.prefill import (
    PREFILL_FIELD_CURRENCY,
    PREFILL_FIELD_FEES,
    PREFILL_FIELD_MASTER_DATA,
    PREFILL_FIELD_PRICE_PER_UNIT,
    PREFILL_FIELD_SETTLEMENT_DATE,
    PREFILL_FIELD_TAXES,
    PREFILL_FIELD_TRADE_DATE,
    PREFILL_FIELD_UNITS,
    fill_to_prefill,
)
from services.provider_channel.publishing_key import (
    PUBLISHING_KEY,
    PUBLISHING_KEY_ID,
    PUBLISHING_KEY_PLACEHOLDER,
    PUBLISHING_KEY_RING,
    SUCCESSOR_KEY,
    SUCCESSOR_KEY_ID,
    is_placeholder,
)
from services.provider_channel.schemas import (
    ENVELOPE_SCHEMA_VERSION,
    ENVELOPE_STATUS_ACKNOWLEDGED,
    ENVELOPE_STATUS_DECLINED,
    ENVELOPE_STATUS_EXECUTED,
    ENVELOPE_STATUS_SENT,
    ENVELOPE_STATUSES,
    FILL_SCHEMA_VERSION,
    MESSAGE_TYPES,
    Envelope,
    FillPayload,
    SchemaError,
    UnknownSchemaVersion,
    envelope_to_dict,
    fill_to_dict,
    parse_envelope,
    parse_fill,
)

__all__ = [
    "DIRECTORY_FORMAT_VERSION",
    "ENVELOPE_SCHEMA_VERSION",
    "ENVELOPE_STATUSES",
    "ENVELOPE_STATUS_ACKNOWLEDGED",
    "ENVELOPE_STATUS_DECLINED",
    "ENVELOPE_STATUS_EXECUTED",
    "ENVELOPE_STATUS_SENT",
    "FILL_SCHEMA_VERSION",
    "KEY_TYPE_X25519_SEALED_BOX",
    "MESSAGE_TYPES",
    "PREFILL_FIELD_CURRENCY",
    "PREFILL_FIELD_FEES",
    "PREFILL_FIELD_MASTER_DATA",
    "PREFILL_FIELD_PRICE_PER_UNIT",
    "PREFILL_FIELD_SETTLEMENT_DATE",
    "PREFILL_FIELD_TAXES",
    "PREFILL_FIELD_TRADE_DATE",
    "PREFILL_FIELD_UNITS",
    "PROVIDER_TYPES",
    "PUBLISHING_KEY",
    "PUBLISHING_KEY_ID",
    "PUBLISHING_KEY_PLACEHOLDER",
    "PUBLISHING_KEY_RING",
    "SIGNATURE_SCHEME_ED25519",
    "SUCCESSOR_KEY",
    "SUCCESSOR_KEY_ID",
    "SUPPORTED_ENCRYPTION_KEY_TYPES",
    "SUPPORTED_SIGNATURE_SCHEMES",
    "Directory",
    "DirectoryExpired",
    "DirectoryShapeError",
    "DirectoryVerificationError",
    "Envelope",
    "FillPayload",
    "InvalidDirectorySignature",
    "ProviderEntry",
    "PublishingKeyNotConfigured",
    "SchemaError",
    "SuccessorKey",
    "UnknownDirectoryFormatVersion",
    "UnknownSchemaVersion",
    "UnsupportedSignatureScheme",
    "canonical_bytes",
    "envelope_to_dict",
    "fill_to_dict",
    "fill_to_prefill",
    "is_placeholder",
    "parse_envelope",
    "parse_fill",
    "sign_directory",
    "verify_directory",
]
