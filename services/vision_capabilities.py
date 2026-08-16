# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Vision-capability gate for the active Shirley model (ADR-0075).

Answers one question: can the configured model accept raster-image input?
The allowlist of vision-capable model families lives in
``config/vision_capable_models.json`` and is matched with
:func:`fnmatch.fnmatch`, mirroring the "no silent fallback" discipline of
:mod:`services.scraper.capabilities`.

Unlike the scraper's PDF gate, a no-match here does **not** raise — it
returns ``False`` and leaves the user-facing decision to the caller (the
web surface renders an inline error fragment; the Telegram bot sends a
reply). The shared discipline is preserved: a non-vision model is reported
as such instead of letting OpenRouter silently drop the image content
block.

This module imports only the standard library and reads its JSON config;
it must not import from ``web/`` or ``bot/`` and must not import PyQt6
(ADR-0038).
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VISION_CAPABILITIES_PATH = _REPO_ROOT / "config" / "vision_capable_models.json"

#: Raster-image MIME types Shirley accepts as input (ADR-0075).
ALLOWED_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)

#: Per-image size ceiling in bytes (8 MiB), enforced before the ~33 %
#: base64 inflation. Server-side downscaling is deliberately not
#: introduced (no Pillow dependency); oversize images are rejected.
MAX_IMAGE_BYTES: int = 8 * 1024 * 1024


def load_vision_patterns(path: Path | None = None) -> list[str]:
    """Load the vision-capable model fnmatch patterns from disk.

    Args:
        path: Override path (for tests). If ``None``, uses the default
            location ``config/vision_capable_models.json``.

    Returns:
        The fnmatch patterns in file order.
    """
    src = path or _VISION_CAPABILITIES_PATH
    raw = json.loads(src.read_text(encoding="utf-8"))
    return [entry["pattern"] for entry in raw["vision_capable_models"]]


def supports_vision(model_id: str, patterns: list[str] | None = None) -> bool:
    """Report whether ``model_id`` can accept raster-image input.

    Matches the model id against the vision-capability allowlist with
    :func:`fnmatch.fnmatch`. A blank ``model_id`` or a no-match returns
    ``False`` — there is no silent fallback, but how that is surfaced to
    the user is left to the caller (ADR-0075). Text-only turns never
    consult this gate.

    Args:
        model_id: The active model identifier (e.g.
            ``"anthropic/claude-sonnet-4.5"``). Empty or whitespace-only
            ids return ``False``.
        patterns: Override pattern list (for tests). If ``None``, loads
            from disk.

    Returns:
        ``True`` when a pattern matches; ``False`` otherwise.
    """
    if not model_id or not model_id.strip():
        return False
    pats = patterns if patterns is not None else load_vision_patterns()
    for pattern in pats:
        if fnmatch.fnmatch(model_id, pattern):
            logger.debug("supports_vision: '%s' matched '%s'.", model_id, pattern)
            return True
    return False
