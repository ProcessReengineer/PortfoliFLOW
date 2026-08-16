# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared fixtures for the CLI test package.

Several CLI test modules auto-load a local ``.env`` (directly via
``python-dotenv`` and indirectly through ``cli._db``). That can leak a
real ``SUPER_ADMIN_EMAIL`` / ``SUPER_ADMIN_PASSWORD`` pair into tests
that only intend to exercise sentinel bootstrap and seed installation.

Since the set-time password policy now rejects a weak
``SUPER_ADMIN_PASSWORD`` (see :func:`services.auth.password_policy.
validate_password_strength`, ADR-0036 §8), a leaked weak local password
would make an otherwise-unrelated seed test fail during the incidental
super-admin creation step. The seed tests do not care about super-admin
creation at all.

The autouse fixture below clears the ``SUPER_ADMIN_*`` env vars before
every CLI test, making super-admin creation strictly opt-in: a test
that wants it sets both vars explicitly (see
``test_bootstrap_super_admin.py``). This mirrors the per-module
``_isolate_bootstrap_env`` fixtures already used by the unit-level
bootstrap tests.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_super_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``SUPER_ADMIN_*`` env vars so bootstrap skips super-admin
    creation unless a test opts in by setting them explicitly.
    """
    monkeypatch.delenv("SUPER_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_DISPLAY_NAME", raising=False)
