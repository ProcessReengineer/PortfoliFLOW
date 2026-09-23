# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Command-palette search endpoint — Sub-stream 6F-2.

A single GET endpoint that returns the catalogue of jumpable
destinations (areas, sections, actions) filtered by a substring
query. The catalogue is static: it is built from :func:`web.shell.all_areas`
plus the per-area section catalogue introduced alongside the section
indicator.

Since P-UX-A0c the endpoint is **no longer DB-free**. Sections the
area partial renders only for a tenant owner must not surface here for
a member, and the role is not on the session — so the handler depends
on :func:`web.permissions.get_authenticated_user`, which costs one
primary-key read per call (its own short ``tenant_context``, committed
and closed immediately; ADR-0065 §1b). That is affordable because the
palette input is debounced client-side (``section_nav.js``) rather
than fired on every keystroke, but it is a real change of character
for this route and deliberately not papered over with a cache: a
cached role is a stale role, and this is an authorization-shaped
filter. Carrying the role on :class:`SessionDTO` would remove the read
altogether and is the better long-term answer; it is out of scope
here, since ``SessionDTO`` has no role today.

The ``actions`` key is reserved for later sub-streams that introduce
imperative palette entries (e.g. *Toggle sidebar*, *Switch theme*).
Today it is always an empty list; the shape lets the client code be
written once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from core.repositories.user_repository import UserDTO
from web.permissions import get_authenticated_user
from web.shell import all_areas, sections_for

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


def _build_catalogue(*, is_tenant_owner: bool) -> dict[str, list[dict[str, str]]]:
    """Return the catalogue for one role, unfiltered by the query.

    Areas and actions are role-blind; only the section list narrows,
    via :func:`web.shell.sections_for`, so a member is never offered a
    jump to a section their area page does not contain.

    Args:
        is_tenant_owner: Whether the signed-in user holds the ``owner``
            role.

    Returns:
        Dict with the three catalogue keys, each a list of entries.
    """
    area_entries: list[dict[str, str]] = []
    section_entries: list[dict[str, str]] = []
    for area in all_areas():
        area_entries.append({"slug": area.slug, "label": area.label, "url": area.url})
        for section in sections_for(area.slug, is_tenant_owner=is_tenant_owner):
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
    user: UserDTO = Depends(get_authenticated_user),
) -> dict[str, Any]:
    """Return the command-palette catalogue filtered by ``q`` and by role.

    Args:
        q: Case-insensitive substring filter. Empty string returns the
            caller's full catalogue.
        user: The authenticated user. ``get_authenticated_user`` chains
            ``require_authenticated_session`` → ``require_session``, so
            the unauthenticated contract is unchanged: 303 to ``/login``
            on a plain GET, 401 + ``HX-Redirect`` on an HTMX one.

    Returns:
        Dict with three keys — ``areas``, ``sections``, ``actions`` —
        each mapping to a list of result entries. ``sections`` omits the
        owner-only entries for a caller who is not a tenant owner.
    """
    catalogue = _build_catalogue(is_tenant_owner=user.has_role("owner"))
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
