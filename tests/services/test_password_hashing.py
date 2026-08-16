# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the centralised Argon2id password-hashing seam."""

from __future__ import annotations

from services.password_hashing import hash_password, verify_password


def test_hash_password_returns_argon2id_encoded_string() -> None:
    hashed = hash_password("super-secret-passphrase")
    assert isinstance(hashed, str)
    assert hashed.startswith("$argon2id$")


def test_verify_password_accepts_correct_password() -> None:
    plaintext = "correct-horse-battery-staple"
    hashed = hash_password(plaintext)
    assert verify_password(plaintext, hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("right")
    assert verify_password("wrong", hashed) is False


def test_verify_password_returns_false_on_malformed_hash() -> None:
    # A clearly malformed hash must yield False rather than raising —
    # callers treat the function as a pure boolean predicate.
    assert verify_password("anything", "not-an-argon2-hash") is False
    assert verify_password("anything", "") is False
