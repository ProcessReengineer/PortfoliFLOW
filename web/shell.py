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

Since P-UX-A0b an area shows one section at a time: the URL fragment
selects it, and :func:`landing_section_for` names the section the area
opens on when the fragment is absent.

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
# (ADR-0122 §1), with Transactions inserted by ADR-0128 §7: Front Office →
# Back Office → Assistants → Planning Desk → Investor Communication →
# Watch Desk → Cases → Transactions → Admin. It reads as the book
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
# documented close. The Cases → Transactions adjacency continues that
# chain (ADR-0128 §7): a trade ticket executes the decision a Case
# carries, so the three surfaces read left to right as raise → decide →
# act.
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
    # Ninth top-level Area *by order of introduction* (ADR-0128 §7): the
    # Transactions Area — a trade ticket executes the decision a Case
    # carries, completing the Watch Desk → Cases → Transactions chain.
    AreaMeta(slug="transactions", label="Transactions", url="/transactions"),
    AreaMeta(slug="admin", label="Admin", url="/admin"),
)


@dataclass(frozen=True)
class SectionMeta:
    """Static metadata for one section (module slot) within an area.

    Mirrors the section markup produced by ``areas/_section.html``:
    ``slug`` is the section's HTML id (also the URL fragment), and
    ``title`` is the human-readable label rendered into the section's
    view header and the sidebar's second level. Since P-UX-2 this
    catalogue is the single source of the rendered heading: the body
    partials pass only the slug, and ``areas/_section.html`` resolves
    the title through :func:`section_title`.

    ``landing`` marks the section shown when the area is opened without
    a fragment; at most one per area, else the first entry.

    ``owner_only`` marks a section whose body the area partial renders
    only for a tenant owner; the navigation level and the command search
    omit it for everyone else. It is cosmetic mirroring of the route gate
    (ADR-0121 §6, ADR-0126), never the gate itself.
    """

    slug: str
    title: str
    landing: bool = False
    owner_only: bool = False


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
    # Three sections, in list order (ADR-0128 §7): the order and
    # record-flow composer, the blotter (draft / proposed / approved), the
    # history (booked / cancelled). Placeholder bodies until S4 / S5.
    "transactions": (
        SectionMeta(slug="new", title="New transaction"),
        SectionMeta(slug="blotter", title="Blotter", landing=True),
        SectionMeta(slug="history", title="History"),
    ),
    "back_office": (
        SectionMeta(slug="saa", title="Strategic Asset Allocation"),
        SectionMeta(slug="benchmarks-attribution", title="Benchmarks & Attribution"),
        SectionMeta(slug="limits", title="Investment Limits"),
    ),
    "admin": (
        SectionMeta(slug="data-import", title="Data Import"),
        # Owner-only (ADR-0126): the schedule save and "Refresh now" both
        # refuse a member, so the navigation level and the command search
        # omit the section rather than pointing at a body the partial does
        # not render. The flag is pinned to that partial's
        # ``{% if is_tenant_owner %}`` block by
        # tests/regression/test_section_catalogue_matches_body_partials.py,
        # so catalogue and template cannot drift apart.
        SectionMeta(slug="market-data", title="Market Data", owner_only=True),
        # Replaced the ADR-0052 ``ai-settings`` slot when the scoped
        # settings write surface landed (ADR-0112 §6, strand F3).
        SectionMeta(slug="providers-credentials", title="Providers & Credentials"),
        # The tenant-owner user surface (ADR-0121 §6). Owner-only: the
        # navigation level and the command search omit it for a member,
        # who would otherwise get an entry leading to a body the partial
        # does not render. The flag is pinned to that partial's
        # ``{% if is_tenant_owner %}`` block by
        # tests/regression/test_section_catalogue_matches_body_partials.py,
        # so catalogue and template cannot drift apart.
        SectionMeta(slug="users", title="Users", owner_only=True),
        # A pointer tile, not a section body of its own — it links out to the
        # full-page investment maintenance surface (GET /investments, ADR-0043
        # §5). Visible to every role — the list GET is session-gated and
        # writes stay owner-gated on their own routes — so it carries no
        # ``owner_only`` flag. Replaced the never-implemented
        # "application-settings" placeholder slot.
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
    """Return the full section catalogue for ``area_slug`` in module order.

    Role-blind by design: this is the catalogue as written, owner-only
    entries included. It is what
    ``tests/regression/test_section_catalogue_matches_body_partials.py``
    compares against the body partial, and what :func:`section_title`
    resolves a heading through — both of which have to see every entry.
    Consumers that render *to a signed-in user* call :func:`sections_for`
    instead.

    Args:
        area_slug: Area slug from :data:`_AREAS`.

    Returns:
        Tuple of :class:`SectionMeta` in registry order. Empty tuple
        for unknown area slugs.
    """
    return _SECTIONS_BY_AREA.get(area_slug, ())


def sections_for(area_slug: str, *, is_tenant_owner: bool) -> tuple[SectionMeta, ...]:
    """Return the sections ``area_slug`` shows to the signed-in role, in catalogue order.

    An owner sees the full catalogue; every other role sees it without
    the ``owner_only`` entries, because the area body partial does not
    render those sections for them. Order is never disturbed — this is
    a filter, not a re-sort.

    Args:
        area_slug: Area slug from :data:`_AREAS`.
        is_tenant_owner: Whether the signed-in user holds the ``owner``
            role. ``False`` is the safe default for a degraded render:
            it yields the member catalogue, which points only at
            sections every role can reach.

    Returns:
        Tuple of :class:`SectionMeta` in catalogue order. Empty tuple
        for unknown area slugs.
    """
    sections = all_sections(area_slug)
    if is_tenant_owner:
        return sections
    return tuple(section for section in sections if not section.owner_only)


def section_title(area_slug: str, section_slug: str) -> str:
    """Return the catalogue title for ``section_slug`` inside ``area_slug``.

    Registered as the ``pf_section_title`` Jinja global so that
    ``areas/_section.html`` renders its ``<h2>`` straight from the
    catalogue; the body partials no longer carry a title literal.

    The lookup is keyed on the *pair* — ``providers-credentials``
    exists under both ``admin`` and ``assistants``, so a slug-only
    scan would be ambiguous.

    Args:
        area_slug: Area slug from :data:`_AREAS`.
        section_slug: Section slug within that area.

    Returns:
        The human-readable section heading.

    Raises:
        LookupError: For an unknown pair: a body partial that renders
            a slug the catalogue does not list is drift, and
            ``tests/regression/test_section_catalogue_matches_body_partials.py``
            exists so that drift never reaches a render.
    """
    for section in all_sections(area_slug):
        if section.slug == section_slug:
            return section.title
    raise LookupError(
        f"No section title for ({area_slug!r}, {section_slug!r}) in the shell "
        "catalogue; add it to _SECTIONS_BY_AREA rather than titling it in the "
        "body partial."
    )


def landing_section_for(area_slug: str) -> str:
    """Return the slug of the section ``area_slug`` opens on.

    Since P-UX-A0b an area shows one section at a time and the URL
    fragment selects it. With no fragment the landing section is
    shown: the catalogue entry flagged ``landing``, or — the common
    case, where no entry is flagged — the first one.

    Deliberately role-blind, and safely so: a landing section is never
    ``owner_only``, so the resolved landing view is present in every
    role's index. That is an invariant of the catalogue rather than of
    this function, and
    ``tests/regression/test_section_catalogue_matches_body_partials.py``
    is what holds it — flagging a landing entry ``owner_only`` would
    land a member on a section their partial never rendered.

    Args:
        area_slug: Area slug from :data:`_AREAS`.

    Returns:
        The landing section's slug.

    Raises:
        LookupError: For an unknown area, in the style of
            :func:`section_title` — an area whose sections the
            catalogue does not list cannot name a landing view, and
            failing here surfaces the drift rather than rendering a
            page with every section hidden.
    """
    sections = all_sections(area_slug)
    if not sections:
        raise LookupError(
            f"No sections for area {area_slug!r} in the shell catalogue; add "
            "it to _SECTIONS_BY_AREA rather than defaulting the landing view."
        )
    for section in sections:
        if section.landing:
            return section.slug
    return sections[0].slug


def section_index_for(area_slug: str, *, is_tenant_owner: bool) -> list[dict[str, str]]:
    """Project :func:`sections_for` to the template-friendly dict form.

    The Jinja templates iterate over a list of dicts (``slug``,
    ``title`` and ``landing`` keys) rather than dataclass instances,
    matching the pattern used elsewhere in the codebase for shell
    context. ``landing`` stays a string — ``"true"`` / ``"false"`` —
    so the dict type is uniform and the sidebar template compares it
    the way it compares every other rendered attribute value.

    ``landing`` carries the *resolved* landing view from
    :func:`landing_section_for`, not the raw ``SectionMeta.landing``
    flag: exactly one entry per area is ``"true"``, including the eight
    areas that flag nothing and fall back to their first section. The
    sidebar's second level marks that entry ``aria-current``, and it has
    to be marked on every area, not only on the one that carries a flag.

    The role is keyword-only and carries **no default**: the sidebar's
    second level is the one place a member could be handed a link to a
    section their page does not contain, so a caller that forgets to
    pass the role fails loudly at the call rather than quietly
    rendering the owner list to everyone.

    Args:
        area_slug: Area slug to look up.
        is_tenant_owner: Whether the signed-in user holds the ``owner``
            role; passed through to :func:`sections_for`.

    Returns:
        List of ``{"slug": str, "title": str, "landing": str}`` dicts.
        Empty list for unknown areas.
    """
    sections = sections_for(area_slug, is_tenant_owner=is_tenant_owner)
    if not sections:
        return []
    landing = landing_section_for(area_slug)
    return [
        {
            "slug": section.slug,
            "title": section.title,
            "landing": "true" if section.slug == landing else "false",
        }
        for section in sections
    ]


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
