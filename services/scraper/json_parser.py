# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Defensive JSON parser for LLM extraction responses.

The LLM is asked to return ```json ... ``` but real-world responses sometimes
include prose preamble (e.g. tool-leak artefacts) or drop the language hint.
We try three strategies in order; if all fail, raise :class:`JsonParseError`.

Tier usage is logged at WARNING level so we can monitor how often the
fallbacks are needed in practice.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_FENCED_JSON_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_FENCED_ANY_RE = re.compile(r"```\s*(.*?)```", re.DOTALL)


class JsonParseError(Exception):
    """Raised when no JSON object can be extracted from the response."""


def parse_extraction_response(raw: str) -> dict[str, Any]:
    """Parse an LLM extraction response to a dict.

    Tier 1: ```json ... ``` fenced block (the happy path).
    Tier 2: any ``` ... ``` fenced block, parsed as JSON.
    Tier 3: first ``{`` to last ``}`` in the whole response.

    Args:
        raw: The raw LLM response text.

    Returns:
        The parsed JSON object as a dict.

    Raises:
        JsonParseError: If none of the three tiers yields a valid JSON object.
    """
    # Tier 1
    m = _FENCED_JSON_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError as exc:
            logger.warning("parse_extraction_response: Tier 1 matched but JSON invalid: %s", exc)

    # Tier 2
    m = _FENCED_ANY_RE.search(raw)
    if m:
        try:
            result = json.loads(m.group(1).strip())
            logger.warning("parse_extraction_response: used Tier 2 (any-fence).")
            return result
        except json.JSONDecodeError:
            pass

    # Tier 3
    first = raw.find("{")
    last = raw.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = raw[first : last + 1]
        try:
            result = json.loads(candidate)
            logger.warning("parse_extraction_response: used Tier 3 (brace-to-brace).")
            return result
        except json.JSONDecodeError:
            pass

    raise JsonParseError(
        "Could not extract a JSON object from the LLM response. "
        f"Response preview (first 200 chars): {raw[:200]!r}"
    )
