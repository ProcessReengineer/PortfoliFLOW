# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Command-palette search endpoint — Sub-stream 6F-2.

A single GET endpoint that returns the catalogue of jumpable
destinations (areas, sections, actions) filtered by a substring
query. The catalogue is static: it is built from :func:`web.shell.all_areas`
plus the per-area section catalogue introduced alongside the section
indicator. No database access — the endpoint stays hot enough to
respond on every keystroke.

The ``actions`` key is reserved for later sub-streams that introduce
imperative palette entries (e.g. *Toggle sidebar*, *Switch theme*).
Today it is always an empty list; the shape lets the client code be
written once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from services.auth.session import SessionDTO
from web.auth import require_session
from web.shell import all_areas, all_sections

router = APIRouter()


def _matches(query: str, *fields: str) -> bool:
    """Return ``True`` when ``query`` is a substring of any field.

    Match is case-insensitive. An empty ``query`` returns ``True`` so
    the empty-input render shows the full catalogue.
    """
    if not query:
        return True
    needle = query.lower()
    return any(needle in field.lower() for field in fields)


def _build_catalogue() -> dict[str, list[dict[str, str]]]:
    """Return the full static catalogue, unfiltered."""
    area_entries: list[dict[str, str]] = []
    section_entries: list[dict[str, str]] = []
    for area in all_areas():
        area_entries.append({"slug": area.slug, "label": area.label, "url": area.url})
        for section in all_sections(area.slug):
            section_entries.append(
                {
                    "slug": section.slug,
                    "label": section.title,
                    "area": area.slug,
                    "url": f"{area.url}#{section.slug}",
                }
            )
    return {
        "areas": area_entries,
        "sections": section_entries,
        "actions": [],
    }


@router.get("/api/cmd-search")
async def cmd_search(
    q: str = "",
    session: SessionDTO = Depends(require_session),
) -> dict[str, Any]:
    """Return the command-palette catalogue filtered by ``q``.

    Args:
        q: Case-insensitive substring filter. Empty string returns the
            full catalogue.
        session: Authenticated session; the dependency raises 401 +
            ``HX-Redirect`` for unauthenticated callers.

    Returns:
        Dict with three keys — ``areas``, ``sections``, ``actions`` —
        each mapping to a list of result entries.
    """
    del session  # Required for the auth dependency; not used in the body.
    catalogue = _build_catalogue()
    return {
        "areas": [
            entry for entry in catalogue["areas"] if _matches(q, entry["label"], entry["slug"])
        ],
        "sections": [
            entry for entry in catalogue["sections"] if _matches(q, entry["label"], entry["slug"])
        ],
        "actions": [
            entry
            for entry in catalogue["actions"]
            if _matches(q, entry.get("label", ""), entry.get("slug", ""))
        ],
    }
