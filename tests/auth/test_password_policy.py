# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the set-time password strength policy.

Covers the pure validator (:func:`validate_password_strength`) and its
wiring into the CLI *set* paths (``set-password`` and
``create-super-admin``). The verify/login path is deliberately never
exercised — policy applies on set, never on verify, so pre-policy
credentials keep working.

The CLI assertions rely on the validator running *before* any database
work: both commands validate the resolved password ahead of the
``asyncio.run`` that opens an engine, so a weak password is rejected
with no live Postgres required.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli import app
from core.exceptions import ValidationError
from services.auth.password_policy import (
    MIN_LENGTH,
    validate_password_strength,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_rejects_short_password() -> None:
    """A sub-12-character password is rejected even with many classes."""
    with pytest.raises(ValidationError):
        # 4 chars, four character classes — fails purely on length.
        validate_password_strength("Ab1!")


def test_rejects_single_character_class_all_lower() -> None:
    """An all-lowercase password (single class) is rejected."""
    with pytest.raises(ValidationError):
        validate_password_strength("abcdefghijklmnopqrst")


def test_rejects_single_character_class_all_digits() -> None:
    """An all-digit password (single class) is rejected."""
    with pytest.raises(ValidationError):
        validate_password_strength("1234567890123456")


def test_accepts_valid_password() -> None:
    """A 12+ char password spanning two classes is accepted (no raise)."""
    validate_password_strength("correcthorse42")  # lowercase + digits


def test_length_error_message_is_clear() -> None:
    """The too-short message names the policy and the floor length."""
    with pytest.raises(ValidationError) as excinfo:
        validate_password_strength("Ab1!")
    message = excinfo.value.message
    assert "too weak" in message.lower()
    assert str(MIN_LENGTH) in message


def test_class_error_message_is_clear() -> None:
    """The single-class message explains the diversity requirement."""
    with pytest.raises(ValidationError) as excinfo:
        validate_password_strength("abcdefghijklmnopqrst")
    message = excinfo.value.message.lower()
    assert "too weak" in message
    assert "class" in message


# ---------------------------------------------------------------------------
# CLI set paths — rejection happens before any DB access
# ---------------------------------------------------------------------------


def test_set_password_cli_rejects_weak_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``portfoliflow set-password`` exits non-zero on a weak password."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")

    result = runner.invoke(app, ["set-password", "--password-stdin"], input="short\n")

    assert result.exit_code != 0


def test_create_super_admin_cli_rejects_weak_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``portfoliflow create-super-admin`` exits non-zero on a weak password."""
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "short")

    result = runner.invoke(app, ["create-super-admin", "--email", "sa@example.com"])

    assert result.exit_code != 0
