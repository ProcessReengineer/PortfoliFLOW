# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the credential-vault Fernet helpers (ADR-0112 §2).

Pure tests — no database, no network. They pin the four properties the
vault's callers rely on:

* CV-01: round-trip. What ``encrypt`` produces, ``decrypt`` returns
  verbatim, including non-ASCII and empty values; the ciphertext is not
  the plaintext.
* CV-02: ``from_env`` — a missing or blank ``CREDENTIAL_VAULT_MASTER_KEY``
  raises :class:`VaultKeyMissingError` (the vault is disabled *loudly*,
  never silently in plaintext); a malformed key raises
  :class:`~core.exceptions.ConfigurationError`, a different operator
  situation that must read differently.
* CV-03: a token written under one key does not decrypt under another —
  :class:`VaultDecryptError`, the condition ``vault-rotate-key`` rolls
  back on.
* CV-04: nothing leaks. ``repr``/``str`` of a cipher, and the error
  messages, are free of key and value material.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from core.exceptions import ConfigurationError
from services.credential_vault import (
    MASTER_KEY_ENV_VAR,
    VaultCipher,
    VaultDecryptError,
    VaultKeyMissingError,
    is_vault_configured,
)

_KEY_A = Fernet.generate_key().decode()
_KEY_B = Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# CV-01: round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plaintext",
    [
        "sk-or-v1-0123456789abcdef",
        "",
        "ümläut-Ω-🔐",
        "x" * 4096,
    ],
)
def test_cv01_encrypt_decrypt_round_trip(plaintext: str) -> None:
    cipher = VaultCipher(_KEY_A)
    token = cipher.encrypt(plaintext)

    assert isinstance(token, bytes)
    if plaintext:
        assert plaintext.encode("utf-8") not in token, "ciphertext must not embed the plaintext"
    assert cipher.decrypt(token) == plaintext


def test_cv01_two_encryptions_differ_but_both_decrypt() -> None:
    """Fernet tokens carry a random IV — equal inputs give unequal tokens."""
    cipher = VaultCipher(_KEY_A)
    first = cipher.encrypt("same-secret")
    second = cipher.encrypt("same-secret")

    assert first != second
    assert cipher.decrypt(first) == cipher.decrypt(second) == "same-secret"


def test_cv01_bytes_key_is_accepted() -> None:
    """The key may arrive as ``bytes`` (Fernet's own output) or ``str``."""
    assert VaultCipher(_KEY_A.encode()).decrypt(VaultCipher(_KEY_A).encrypt("v")) == "v"


# ---------------------------------------------------------------------------
# CV-02: from_env and the missing-key contract
# ---------------------------------------------------------------------------


def test_cv02_from_env_reads_the_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _KEY_A)
    assert VaultCipher.from_env().decrypt(VaultCipher(_KEY_A).encrypt("v")) == "v"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_cv02_from_env_without_a_key_raises_key_missing(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, value)

    with pytest.raises(VaultKeyMissingError) as excinfo:
        VaultCipher.from_env()
    assert MASTER_KEY_ENV_VAR in str(excinfo.value)


def test_cv02_malformed_key_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present-but-invalid is a different situation from absent."""
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, "not-a-fernet-key")

    with pytest.raises(ConfigurationError) as excinfo:
        VaultCipher.from_env()
    assert not isinstance(excinfo.value, VaultKeyMissingError)
    # The offending value is never echoed back.
    assert "not-a-fernet-key" not in str(excinfo.value)


def test_cv02_empty_key_argument_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        VaultCipher("")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("   ", False), (_KEY_A, True), ("anything", True)],
)
def test_cv02_is_vault_configured_is_a_presence_check(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    """Presence only — format is validated when a cipher is constructed."""
    if value is None:
        monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, value)
    assert is_vault_configured() is expected


def test_cv02_is_vault_configured_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The caller owns the single WARNING, not this helper (ADR-0112 §2)."""
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
    with caplog.at_level("DEBUG"):
        assert is_vault_configured() is False
    assert caplog.records == []


# ---------------------------------------------------------------------------
# CV-03: wrong key / corrupt token
# ---------------------------------------------------------------------------


def test_cv03_wrong_key_raises_decrypt_error() -> None:
    token = VaultCipher(_KEY_A).encrypt("tenant-api-key")

    with pytest.raises(VaultDecryptError):
        VaultCipher(_KEY_B).decrypt(token)


def test_cv03_corrupt_token_raises_decrypt_error() -> None:
    cipher = VaultCipher(_KEY_A)
    token = bytearray(cipher.encrypt("tenant-api-key"))
    token[-1] ^= 0xFF

    with pytest.raises(VaultDecryptError):
        cipher.decrypt(bytes(token))


def test_cv03_garbage_token_raises_decrypt_error() -> None:
    with pytest.raises(VaultDecryptError):
        VaultCipher(_KEY_A).decrypt(b"not-a-token")


# ---------------------------------------------------------------------------
# CV-04: nothing leaks
# ---------------------------------------------------------------------------


def test_cv04_repr_and_str_leak_nothing() -> None:
    cipher = VaultCipher(_KEY_A)

    for rendered in (repr(cipher), str(cipher), f"{cipher}"):
        assert rendered == "VaultCipher(<key hidden>)"
        assert _KEY_A not in rendered
        # Not even a prefix of the key survives.
        assert _KEY_A[:8] not in rendered


def test_cv04_decrypt_error_message_names_no_value() -> None:
    secret = "sk-or-v1-supersecret"
    token = VaultCipher(_KEY_A).encrypt(secret)

    with pytest.raises(VaultDecryptError) as excinfo:
        VaultCipher(_KEY_B).decrypt(token)

    message = str(excinfo.value)
    assert secret not in message
    assert _KEY_A not in message
    assert _KEY_B not in message
    assert token.decode("utf-8") not in message
