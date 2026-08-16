# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Minimal set-time password strength policy.

A single validator, :func:`validate_password_strength`, enforced on the
paths that *set* a password (bootstrap, super-admin creation, and the
``portfoliflow set-password`` CLI). It is **never** applied on the
verify/login path — existing credentials that predate the policy keep
working, and the login surface must not leak "your stored password is
weak" as a distinguishing signal.

The policy is deliberately small (no dictionary check, no zxcvbn
dependency): a length floor plus a character-class-diversity floor. It
raises :class:`core.exceptions.ValidationError` (a
:class:`core.exceptions.PortfoliFlowError` subclass) with a clear,
user-facing message so CLI callers can surface it and exit non-zero.
"""

from __future__ import annotations

from core.exceptions import ValidationError

#: Minimum accepted password length, in characters.
MIN_LENGTH: int = 12

#: Minimum number of distinct character classes a password must draw
#: from. Rejects a password confined to a single class (all-lowercase,
#: all-uppercase, all-digit, or all-symbol).
MIN_CHARACTER_CLASSES: int = 2


def _count_character_classes(password: str) -> int:
    """Count how many of the four character classes appear in *password*.

    The four classes are lowercase letters, uppercase letters, decimal
    digits, and "symbols" (any character that is none of the former —
    punctuation, whitespace, and other non-alphanumeric characters).

    Args:
        password: The candidate password.

    Returns:
        The number of distinct classes present, in ``0..4``.
    """
    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_symbol = any(not c.isalnum() for c in password)
    return sum((has_lower, has_upper, has_digit, has_symbol))


def validate_password_strength(password: str) -> None:
    """Raise :class:`ValidationError` if *password* is too weak.

    Policy (set-time only, never verify-time):

    - minimum length :data:`MIN_LENGTH` (12) characters;
    - at least :data:`MIN_CHARACTER_CLASSES` (2) distinct character
      classes among lowercase, uppercase, digits, and symbols — a
      password confined to a single class (all-lower, all-upper,
      all-digit, all-symbol) is rejected.

    Args:
        password: The plaintext password about to be set.

    Raises:
        ValidationError: If the password is shorter than
            :data:`MIN_LENGTH`, or draws from fewer than
            :data:`MIN_CHARACTER_CLASSES` character classes. The
            message is safe to surface to the operator.
    """
    if len(password) < MIN_LENGTH:
        raise ValidationError(
            f"Password too weak: must be at least {MIN_LENGTH} characters (got {len(password)}).",
            field="password",
        )
    if _count_character_classes(password) < MIN_CHARACTER_CLASSES:
        raise ValidationError(
            "Password too weak: must combine at least "
            f"{MIN_CHARACTER_CLASSES} character classes "
            "(lowercase, uppercase, digits, symbols).",
            field="password",
        )
