# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Credential vault — application-level encryption for secret settings.

The encryption half of the scoped-settings architecture (ADR-0112 §2).
``scoped_settings`` rows flagged ``is_secret`` carry a Fernet token in
``value_ciphertext``; this package owns the key handling and the
encrypt/decrypt seam that produces and consumes those tokens.

:mod:`services.credential_vault.taxonomy` sits beside it and declares
*which* providers and fields exist (ADR-0112 §3) — the field set the
resolver's vault sources read and the F3 write path validates against.

Deliberately narrow: stdlib plus ``cryptography`` plus
:mod:`core.exceptions`, plus — in the taxonomy module only — the
market-data port's ``UnsupportedCapabilityError``, so an unknown provider
raises one typed error across the whole credential façade (ADR-0112 §4).
No database, no web, no settings plane — the callers (the F2 resolver
vault source, the F3 admin write path, the ``vault-rotate-key`` CLI)
bring the ciphertext to the cipher, never the other way round.

See ``docs/deploy/credential-vault.md`` for the operator-side key
custody procedure.
"""

from services.credential_vault.fernet import (
    VaultCipher,
    VaultDecryptError,
    VaultKeyMissingError,
    is_vault_configured,
    MASTER_KEY_ENV_VAR,
)
from services.credential_vault.taxonomy import (
    PROVIDER_TAXONOMY,
    SCOPES,
    ProviderDeclaration,
    ProviderField,
    declaration_for,
    is_declared,
    secret_fields,
)

__all__ = [
    "MASTER_KEY_ENV_VAR",
    "PROVIDER_TAXONOMY",
    "SCOPES",
    "ProviderDeclaration",
    "ProviderField",
    "VaultCipher",
    "VaultDecryptError",
    "VaultKeyMissingError",
    "declaration_for",
    "is_declared",
    "is_vault_configured",
    "secret_fields",
]
