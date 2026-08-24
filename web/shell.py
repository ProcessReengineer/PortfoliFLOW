# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shell-context helpers — sidebar, status bar, area metadata.

Sub-stream 6F-1 introduces a single sidebar-and-status-bar shell that
wraps every web surface. The shell needs a small set of context
variables on every render: which area is active, whether the sidebar
is collapsed, the tenant name, the build SHA, the config-status flag.
This module concentrates that wiring so individual route handlers do
not have to remember the full set.

Two primitives are exported:

* :class:`AreaMeta` — slug + display label per area, in
  ``module_registry.py`` order.
* :func:`is_htmx_request` — FastAPI dependency that returns ``True``
  when the request carries the ``HX-Request: true`` header.

Per ADR-0046 the shell is a presentational concern. This module never
imports from ``modules/``; it only depends on FastAPI and the
read-only environment surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Request


_SIDEBAR_COOKIE_NAME: str = "pf_sidebar_collapsed"


@dataclass(frozen=True)
class AreaMeta:
    """Static metadata for one area in the IA hierarchy."""

    slug: str
    label: str
    url: str


# Sidebar order, operator-confirmed in the pre-release IA review
# (ADR-0122 §1): Front Office → Back Office → Assistants → Planning Desk →
# Investor Communication → Watch Desk → Cases → Admin. It reads as the book
# first (Front Office, Back Office), the assistant as the standing companion
# right behind it — Shirley is the primary interactive surface for
# day-to-day work on that book — then the forward-looking planning surface
# and the outward-facing communication surface, then the
# monitoring-and-exception workflow (Watch Desk raises, Cases closes), which
# is consulted on its own beat rather than navigated to constantly. Admin is
# last as the rare-use configuration surface.
#
# The Watch Desk → Cases adjacency is deliberate and preserved from
# ADR-0107: the Watch Desk raises a question, a Case carries it to a
# documented close.
#
# This supersedes the ADR-0104 §6 order, which grouped the two
# forward-looking surfaces in the middle and put Assistants seventh —
# underselling the assistant's role in everyday work.
_AREAS: tuple[AreaMeta, ...] = (
    AreaMeta(slug="front_office", label="Front Office", url="/front-office"),
    AreaMeta(slug="back_office", label="Back Office", url="/back-office"),
    AreaMeta(slug="assistants", label="Assistants", url="/assistants"),
    # Seventh top-level Area *by order of introduction* (ADR-0104 §6).
    # Projection and simulation over the plan world.
    AreaMeta(
        slug="planning_desk",
        label="Planning Desk",
        url="/planning-desk",
    ),
    AreaMeta(
        slug="investor_communication",
        label="Investor Communication",
        url="/investor-communication",
    ),
    # Sixth top-level Area *by order of introduction* (ADR-0089): Irene's
    # monitoring surface.
    AreaMeta(
        slug="watch_desk",
        label="Watch Desk",
        url="/watch-desk",
    ),
    # Eighth top-level Area (ADR-0107): the Cases workflow — open questions
    # worked to a documented close. It follows the Watch Desk, which raises
    # the question a Case then carries to that close.
    AreaMeta(slug="cases", label="Cases", url="/cases"),
    AreaMeta(slug="admin", label="Admin", url="/admin"),
)


@dataclass(frozen=True)
class SectionMeta:
    """Static metadata for one section (module slot) within an area.

    Mirrors the section markup produced by ``areas/_section.html``:
    ``slug`` is the section's HTML id (also the URL fragment), and
    ``title`` is the human-readable label rendered into the section
    header and the indicator's hover tooltip.
    """

    slug: str
    title: str


# Sub-stream 6F-2 section catalogue. The slug list per area mirrors
# ``module_registry.py`` order — the same order the area body partials
# emit ``<section>`` blocks. Inspection alone let this drift out of
# sync with the partials; alignment is now enforced by
# ``tests/regression/test_section_catalogue_matches_body_partials.py``,
# which is the authoritative check that every area's slugs and order
# match the ``section_slug`` values its body partial renders. ADR-0046
# referenced ``portfolio-analysis`` in older drafting language; the
# authoritative slug is ``portfolio-optimizer``, matching the module's
# ``module_name``.
_SECTIONS_BY_AREA: dict[str, tuple[SectionMeta, ...]] = {
    "front_office": (
        # Overview is the first section (ADR-0067): a portfolio-level
        # headline KPI strip rendered ahead of per-investment Charts.
        SectionMeta(slug="overview", title="Overview"),
        # data-import moved to admin (6F-3 mid-polish):
        # operator-confirmed that the import surface is a
        # rare-use administrative function, not a primary
        # Front Office concern.
        SectionMeta(slug="charts", title="Charts"),
        SectionMeta(slug="statistics", title="Statistics"),
        SectionMeta(slug="portfolio-optimizer", title="Portfolio Analysis"),
    ),
    # Three sections since ADR-0104 §8 retired the ``scenarios`` placeholder
    # anchor; Feature #034 re-anchors on the Planning Desk below.
    "watch_desk": (
        SectionMeta(slug="briefing", title="Briefing"),
        SectionMeta(slug="journal", title="Journal"),
        SectionMeta(slug="calibration", title="Calibration"),
    ),
    # Two stacked sections, one parameter set, two lenses (ADR-0104 §6).
    "planning_desk": (
        SectionMeta(slug="cash-flow-planning", title="Cash Flow Planning"),
        SectionMeta(slug="scenario-analysis", title="Scenario Analysis"),
    ),
    # Three surfaces, in list order (ADR-0107): the open-cases to-do list,
    # the recently-closed reviewer's view, and the closed-case archive
    # search. The case detail view is C3; these are the list experience only.
    "cases": (
        SectionMeta(slug="open-cases", title="Open Cases"),
        SectionMeta(slug="recently-closed", title="Recently Closed"),
        SectionMeta(slug="archive", title="Archive"),
    ),
    "back_office": (
        SectionMeta(slug="saa", title="Strategic Asset Allocation"),
        SectionMeta(slug="benchmarks-attribution", title="Benchmarks & Attribution"),
        SectionMeta(slug="limits", title="Investment Limits"),
    ),
    "admin": (
        SectionMeta(slug="data-import", title="Data Import"),
        SectionMeta(slug="market-data", title="Market Data"),
        # Replaced the ADR-0052 ``ai-settings`` slot when the scoped
        # settings write surface landed (ADR-0112 §6, strand F3).
        SectionMeta(slug="providers-credentials", title="Providers & Credentials"),
        # The tenant-owner user surface (ADR-0121 §6). The catalogue is
        # role-blind by construction — it feeds the section indicator and
        # command search, neither of which carries a user — so this entry
        # is listed for members too, while the section body and its routes
        # are owner-gated. A member therefore sees one indicator dot and
        # one search hit that lead nowhere; the alternative was to let the
        # catalogue drift from the body partial, which
        # tests/regression/test_section_catalogue_matches_body_partials.py
        # exists to prevent.
        SectionMeta(slug="users", title="Users"),
        # A pointer tile, not a section body of its own — it links out to the
        # full-page investment maintenance surface (GET /investments, ADR-0043
        # §5). Listed role-blind like every catalogue entry; the list GET is
        # session-gated, writes stay owner-gated on their own routes. Replaced
        # the never-implemented "application-settings" placeholder slot.
        SectionMeta(slug="investments", title="Investments"),
    ),
    "investor_communication": (SectionMeta(slug="portfolio-review", title="Portfolio Review"),),
    "assistants": (
        SectionMeta(slug="shirley", title="Shirley"),
        SectionMeta(slug="report-scraper", title="Report Scraper"),
        # A "moved" pointer tile, not a second surface — it follows the
        # Admin slug so the two stay in step.
        SectionMeta(slug="providers-credentials", title="Providers & Credentials"),
    ),
}


def all_areas() -> tuple[AreaMeta, ...]:
    """Return the canonical tuple of areas in registry order."""
    return _AREAS


def get_area_meta(slug: str | None) -> AreaMeta | None:
    """Look up area metadata by slug; ``None`` for unknown slugs."""
    if slug is None:
        return None
    for area in _AREAS:
        if area.slug == slug:
            return area
    return None


def all_sections(area_slug: str) -> tuple[SectionMeta, ...]:
    """Return the section catalogue for ``area_slug`` in module order.

    Args:
        area_slug: Area slug from :data:`_AREAS`.

    Returns:
        Tuple of :class:`SectionMeta` in registry order. Empty tuple
        for unknown area slugs.
    """
    return _SECTIONS_BY_AREA.get(area_slug, ())


def section_index_for(area_slug: str) -> list[dict[str, str]]:
    """Project :func:`all_sections` to the template-friendly dict form.

    The Jinja templates iterate over a list of dicts (``slug`` and
    ``title`` keys) rather than dataclass instances, matching the
    pattern used elsewhere in the codebase for shell context.

    Args:
        area_slug: Area slug to look up.

    Returns:
        List of ``{"slug": str, "title": str}`` dicts. Empty list for
        unknown areas.
    """
    return [{"slug": section.slug, "title": section.title} for section in all_sections(area_slug)]


def is_sidebar_collapsed(request: Request) -> bool:
    """Read the sidebar-collapsed flag from the persistent cookie.

    The cookie is set by ``POST /shell/sidebar/toggle``; absence or
    any value other than ``"true"`` means the sidebar is expanded.

    Args:
        request: The current request.

    Returns:
        ``True`` when the sidebar should render in icon-only state.
    """
    raw = request.cookies.get(_SIDEBAR_COOKIE_NAME)
    return raw == "true"


def is_htmx_request(request: Request) -> bool:
    """FastAPI dependency — ``True`` when the request is an HTMX swap.

    HTMX adds the ``HX-Request: true`` header on every request it
    issues. Direct navigation, browser refresh and bookmarks do not
    set the header, so this is a reliable partial-vs-full branch.

    Args:
        request: The current request.

    Returns:
        Boolean flag indicating whether the response should be a
        partial fragment.
    """
    return request.headers.get("HX-Request", "").lower() == "true"


def build_sha() -> str:
    """Read the build SHA from the environment; fallback to ``"dev"``."""
    return os.getenv("BUILD_SHA", "").strip() or "dev"


def config_ok(request: Request) -> bool:
    """Best-effort config-status flag.

    For 6F-1 the heuristic is the engine attached at startup: when
    ``app.state.engine`` is configured, the data store is reachable
    in principle. A live probe would be more accurate but would
    block every page render on a Postgres round-trip; the simpler
    flag is good enough until the dedicated health-readiness
    surface lands.
    """
    return getattr(request.app.state, "engine", None) is not None
