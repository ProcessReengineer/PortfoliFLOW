# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Argon2id password-hashing seam.

This module is the single source of truth for the Argon2id parameters
used by PortfoliFLOW. All password-hashing call sites — the
``portfoliflow bootstrap`` and ``portfoliflow set-password`` CLI
subcommands (sub-stream 2a) and the runtime authentication backend
(sub-stream 2b) — go through ``hash_password`` and ``verify_password``
so that parameter changes (e.g. raising ``time_cost`` as hardware
improves) land in exactly one place.

Parameters follow the OWASP Password Storage Cheat Sheet recommendation
for Argon2id (2024 revision):

- ``time_cost = 2`` — number of iterations.
- ``memory_cost = 19456`` KiB (19 MiB) — memory the hash function
  uses.
- ``parallelism = 1`` — number of parallel threads.

See ADR-0040 §3 (Password handling) for the decision record.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

# Centralised Argon2id parameters. Do not duplicate these constants
# elsewhere — import the functions below instead.
_TIME_COST: int = 2
_MEMORY_COST: int = 19456  # 19 MiB
_PARALLELISM: int = 1

_HASHER: PasswordHasher = PasswordHasher(
    time_cost=_TIME_COST,
    memory_cost=_MEMORY_COST,
    parallelism=_PARALLELISM,
)


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password with Argon2id.

    Args:
        plaintext: The user-supplied password. Never logged.

    Returns:
        An Argon2id-encoded string (starts with ``$argon2id$``) suitable
        for direct storage in the ``users.password_hash`` column.
    """
    return _HASHER.hash(plaintext)


def verify_password(plaintext: str, hash: str) -> bool:
    """Verify a plaintext password against a stored Argon2id hash.

    Returns ``False`` rather than raising on a mismatch or a malformed
    hash, so that callers (the auth backend, future password-change
    flows) can treat the function as a pure boolean predicate.

    Args:
        plaintext: The candidate password.
        hash: The stored Argon2id hash string.

    Returns:
        True if the password matches, False otherwise (including when
        the hash is malformed or the verification raises any
        argon2-cffi error).
    """
    try:
        return _HASHER.verify(hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
