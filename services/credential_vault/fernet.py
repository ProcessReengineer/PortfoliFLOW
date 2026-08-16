# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Fernet encrypt/decrypt helpers for the credential vault (ADR-0112 §2).

Application-level symmetric encryption over the ``is_secret`` rows of
``scoped_settings``. The master key lives in the environment
(``CREDENTIAL_VAULT_MASTER_KEY``) and **never** in the database — not in
``scoped_settings``, not in any settings table, not in an audit row. There
is no KMS and no external secret manager at this scale (ADR-0112 §7);
rotation is the documented ``portfoliflow vault-rotate-key`` procedure.

Three rules this module exists to keep:

* **Values are never logged.** Nothing here emits a log record at all —
  not even at DEBUG. Callers log counts, providers and key names; never
  plaintext, never ciphertext, never the master key.
* **``repr`` leaks nothing.** :class:`VaultCipher` renders as a constant
  string, so an exception traceback, a debugger frame or a careless
  f-string cannot surface key material.
* **A missing key is loud, never silent.** Requesting a cipher without a
  configured key raises :class:`VaultKeyMissingError`; the application
  never runs a silent plaintext mode. Callers that must *degrade* rather
  than fail (the F2 resolver's vault source) ask
  :func:`is_vault_configured` first and log their own single WARNING.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from core.exceptions import ConfigurationError, PortfoliFlowError

#: The environment variable holding the Fernet master key. Environment
#: only — see the module docstring and ADR-0112 §2.
MASTER_KEY_ENV_VAR = "CREDENTIAL_VAULT_MASTER_KEY"


class VaultKeyMissingError(PortfoliFlowError):
    """Raised when a cipher is requested but no master key is configured.

    The missing-key contract of ADR-0112 §2: with
    ``CREDENTIAL_VAULT_MASTER_KEY`` unset the vault is **disabled, loudly**
    — reads of secret rows are not attempted and writes fail with a typed,
    operator-readable error. There is never a silent plaintext mode.

    Distinct from :class:`~core.exceptions.ConfigurationError`, which this
    module raises when a key *is* present but is not a valid Fernet key:
    absent and malformed are different operator situations and read
    differently in a log.
    """


class VaultDecryptError(PortfoliFlowError):
    """Raised when a ciphertext cannot be decrypted with the active key.

    Either the token was written under a different master key (a rotation
    that did not complete, or a restored backup paired with the wrong key)
    or it is corrupt. The message names neither the token nor the key —
    the caller identifies the offending row by id.
    """


class VaultCipher:
    """Encrypts and decrypts secret values under one Fernet master key.

    Construct from an explicit key argument, or from the environment via
    :meth:`from_env`. Instances are cheap and stateless beyond the key, so
    a caller may hold one for a request, a turn, or a whole rotation run.

    Args:
        key: A Fernet key — url-safe base64-encoded 32 bytes, as emitted by
            ``portfoliflow vault-generate-key``.

    Raises:
        ConfigurationError: If ``key`` is empty or is not a valid Fernet key.
    """

    __slots__ = ("_fernet",)

    def __init__(self, key: str | bytes) -> None:
        if not key:
            raise ConfigurationError(
                "An empty vault key was supplied. Generate one with "
                f"`portfoliflow vault-generate-key` and set {MASTER_KEY_ENV_VAR}."
            )
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            # Deliberately does not echo the offending value.
            raise ConfigurationError(
                "The supplied vault key is not a valid Fernet key (it must be "
                "url-safe base64-encoded 32 bytes). Generate one with "
                "`portfoliflow vault-generate-key`; see "
                "docs/deploy/credential-vault.md."
            ) from exc

    @classmethod
    def from_env(cls) -> VaultCipher:
        """Construct a cipher from ``CREDENTIAL_VAULT_MASTER_KEY``.

        Returns:
            A cipher bound to the configured master key.

        Raises:
            VaultKeyMissingError: If the variable is unset or empty.
            ConfigurationError: If it is set but is not a valid Fernet key.
        """
        key = os.getenv(MASTER_KEY_ENV_VAR, "").strip()
        if not key:
            raise VaultKeyMissingError(
                f"{MASTER_KEY_ENV_VAR} is not set — the credential vault is "
                f"disabled. Generate a key with `portfoliflow vault-generate-key` "
                f"and place it in the deployment environment; see "
                f"docs/deploy/credential-vault.md."
            )
        return cls(key)

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt ``plaintext`` into a Fernet token.

        Args:
            plaintext: The secret value to protect.

        Returns:
            The Fernet token, ready for a ``value_ciphertext`` column.
        """
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        """Decrypt a Fernet token back into its plaintext.

        Args:
            token: The ciphertext as stored in ``value_ciphertext``.

        Returns:
            The decrypted secret value.

        Raises:
            VaultDecryptError: If the token was written under a different
                key, or is corrupt.
        """
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise VaultDecryptError(
                "Ciphertext could not be decrypted with the active vault key "
                "(wrong key or corrupt token)."
            ) from exc

    def __repr__(self) -> str:
        # Never leak key material — applies to str() too, and to any
        # traceback or debugger frame that renders this object.
        return "VaultCipher(<key hidden>)"

    __str__ = __repr__


def is_vault_configured() -> bool:
    """Return whether a master key is present in the environment.

    The cheap presence check the F2 resolver's vault source uses to decide
    between "resolve from the vault" and "the vault is disabled". It
    deliberately **does not log** — the caller owns the single WARNING the
    missing-key contract calls for (ADR-0112 §2), because only the caller
    knows whether this is the first use.

    Returns:
        ``True`` when :data:`MASTER_KEY_ENV_VAR` is set to a non-empty
        value, ``False`` otherwise. Does not validate the key's format;
        a malformed key surfaces as a :class:`ConfigurationError` when a
        cipher is actually constructed.
    """
    return bool(os.getenv(MASTER_KEY_ENV_VAR, "").strip())
