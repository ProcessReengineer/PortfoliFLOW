# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Capability-map lookup for model PDF-input support.

Matches model IDs against the patterns in
``config/scraper_model_capabilities.json`` using :func:`fnmatch.fnmatch`.

No silent fallback: if no pattern matches, raises :class:`UnsupportedModelError`
with a clear message. This is deliberate — a silent fallback was the bug shape
we most want to avoid (OpenRouter sometimes strips malformed content parts
rather than rejecting the request).
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CAPABILITIES_PATH = _REPO_ROOT / "config" / "scraper_model_capabilities.json"


class UnsupportedModelError(Exception):
    """Raised when the selected model has no entry in the capability map."""


@dataclass(frozen=True)
class ModelCapability:
    """Describes a model's PDF-input support.

    Attributes:
        pattern: The fnmatch pattern that matched (e.g. ``"anthropic/claude-*"``).
        format: Content-block format name (e.g. ``"anthropic_document"``).
        max_pdf_mb: Maximum PDF file size in megabytes.
        max_pdf_pages: Maximum PDF page count (informational only; not enforced
            client-side because page counting would require parsing the PDF).
    """

    pattern: str
    format: str
    max_pdf_mb: int
    max_pdf_pages: int


def load_capabilities(path: Path | None = None) -> list[ModelCapability]:
    """Load and parse the capability map.

    Args:
        path: Override path (for tests). If ``None``, uses the default location.

    Returns:
        List of :class:`ModelCapability` objects in file order.
    """
    src = path or _CAPABILITIES_PATH
    raw = json.loads(src.read_text(encoding="utf-8"))
    return [
        ModelCapability(
            pattern=entry["pattern"],
            format=entry["format"],
            max_pdf_mb=int(entry["max_pdf_mb"]),
            max_pdf_pages=int(entry["max_pdf_pages"]),
        )
        for entry in raw["supported_models"]
    ]


def lookup_capability(
    model_id: str,
    capabilities: list[ModelCapability] | None = None,
) -> ModelCapability:
    """Find the capability entry matching a model ID.

    Args:
        model_id: The model identifier (e.g. ``"anthropic/claude-sonnet-4.5"``).
        capabilities: Override list (for tests). If ``None``, loads from disk.

    Returns:
        The first matching :class:`ModelCapability`.

    Raises:
        UnsupportedModelError: If no pattern matches.
    """
    caps = capabilities if capabilities is not None else load_capabilities()
    for cap in caps:
        if fnmatch.fnmatch(model_id, cap.pattern):
            logger.debug("lookup_capability: '%s' matched '%s'.", model_id, cap.pattern)
            return cap
    supported = ", ".join(c.pattern for c in caps)
    raise UnsupportedModelError(
        f"Model '{model_id}' does not support PDF extraction. "
        f"Supported model patterns: {supported}. "
        f"Set a compatible model under Admin → Providers & Credentials → "
        f"OpenRouter → Report Scraper model."
    )
