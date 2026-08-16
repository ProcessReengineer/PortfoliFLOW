# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the shared vision-capability gate (ADR-0075).

Cover the allowlist match / no-match behaviour, the blank-id contract,
the explicit-patterns override (so the test never depends on disk), and
the two shared constants (``MAX_IMAGE_BYTES``, ``ALLOWED_IMAGE_MIME_TYPES``).
"""

from __future__ import annotations

from services.vision_capabilities import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    load_vision_patterns,
    supports_vision,
)


def test_anthropic_claude_is_vision_capable() -> None:
    """The shipped ``anthropic/claude-*`` family matches the allowlist."""
    assert supports_vision("anthropic/claude-sonnet-4.5") is True


def test_non_vision_model_is_rejected() -> None:
    """A clearly non-vision id does not match any pattern."""
    assert supports_vision("mistralai/mistral-7b-instruct") is False


def test_empty_model_id_is_rejected() -> None:
    """A blank or whitespace-only model id returns ``False``."""
    assert supports_vision("") is False
    assert supports_vision("   ") is False


def test_explicit_patterns_override_does_not_touch_disk() -> None:
    """Passing ``patterns`` exercises the override path independently of disk."""
    patterns = ["anthropic/claude-*"]
    assert supports_vision("anthropic/claude-opus-4.8", patterns) is True
    assert supports_vision("openai/gpt-4o", patterns) is False


def test_max_image_bytes_is_8_mib() -> None:
    """The size ceiling is exactly 8 MiB."""
    assert MAX_IMAGE_BYTES == 8 * 1024 * 1024


def test_allowed_mime_types_are_exactly_four() -> None:
    """The allowed MIME set is exactly the four supported raster types."""
    assert (
        frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
        == ALLOWED_IMAGE_MIME_TYPES
    )


def test_default_patterns_load_from_disk() -> None:
    """The default config ships the ``anthropic/claude`` family."""
    patterns = load_vision_patterns()
    assert any("anthropic/claude" in p for p in patterns)
