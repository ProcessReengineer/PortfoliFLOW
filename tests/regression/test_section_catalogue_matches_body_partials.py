# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``web.shell`` section catalogue matches body partials.

``web/shell.py`` holds ``_SECTIONS_BY_AREA``, a hand-maintained
catalogue of the sections rendered per area. It feeds two consumers:
``section_index_for()`` (the sidebar's second level) and
``sections_for()`` (command search). If the catalogue omits a section
the partial renders, that section becomes undiscoverable via section
nav and command search; if it lists a section the partial does not
render, search shows a phantom.

Since P-UX-A0c the catalogue is role-aware: an entry may carry
``owner_only``, and the two consumers above drop those entries for a
member. That flag is a *mirror* of the partial's
``{% if is_tenant_owner %}`` blocks, never the gate itself — the gate
lives on the routes (ADR-0121 §6, ADR-0126). A mirror that has slipped
is worse than no mirror, so the guards below pin it in both
directions, and pin the invariant the landing view depends on: a
landing section is never owner-only.

Historically the catalogue was kept "aligned by inspection" with the
area body partials under
``web/templates/_partials/areas/_<area>_body.html``. Inspection
drifted (``back_office`` lost ``benchmarks-attribution`` and
``limits``). This guard replaces inspection: for every area in
:data:`web.shell._AREAS` it scans the matching body partial for the
ordered ``section_slug="..."`` values it renders and asserts that list
equals ``[s.slug for s in all_sections(area_slug)]`` — same slugs, same
order.

The area-to-partial mapping is derived from ``web.shell`` (not
hardcoded here) so a renamed partial or a new area fails visibly. The
scan is a plain regex over template source — no Jinja import, no
template rendering — matching the house pattern of the other
template-scanning guards (see
``test_audit_engine_only_writes_login_audit.py`` and
``test_no_matplotlib_in_web.py``).

Since P-UX-2 the catalogue feeds a third consumer: the rendered
``<h2>`` itself. ``areas/_section.html`` resolves its heading through
``web.shell.section_title(active_area, section_slug)``, so the 27
``section_title="..."`` literals the body partials used to carry are
gone and a title is declared exactly once. The guard below
(``test_body_partials_carry_no_title_literals``) keeps them gone; the
unit tests at the end of this module pin the helper itself, including
the pair-keyed lookup that ``providers-credentials`` — a slug shared
by ``admin`` and ``assistants`` — requires.

If this guard goes red, the fix is to reconcile ``_SECTIONS_BY_AREA``
with the body partial (or vice versa) so the two agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from web.shell import (
    _AREAS,
    all_areas,
    all_sections,
    landing_section_for,
    section_index_for,
    section_title,
)

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_AREAS_PARTIALS_DIR: Path = _REPO_ROOT / "web" / "templates" / "_partials" / "areas"

# Captures the slug from ``section_slug="..."`` / ``section_slug='...'``
# as emitted by the ``{% with %}`` blocks that feed
# ``areas/_section.html``. ``findall`` preserves render order.
_SECTION_SLUG_RE: re.Pattern[str] = re.compile(r"""section_slug\s*=\s*["']([^"']+)["']""")


# Matches any resurrected ``section_title=`` assignment in a body
# partial. Deliberately looser than the slug pattern above: it fires on
# the assignment alone, whatever the value's quoting.
_SECTION_TITLE_RE: re.Pattern[str] = re.compile(r"section_title\s*=")


# The flag the body partials condition an owner-only section on.
_OWNER_FLAG: str = "is_tenant_owner"

# One pass over the template source that yields ``{% if %}`` opens,
# ``{% endif %}`` closes and ``section_slug`` assignments *in source
# order*, so :func:`_owner_conditional_slugs` can pair them by depth.
# Alternation in a single pattern is what preserves that order;
# scanning for each separately would lose it.
_IF_OR_SLUG_RE: re.Pattern[str] = re.compile(
    r"""{%-?\s*(?P<tag>if|endif)\b(?P<expr>[^%]*?)-?%}"""
    r"""|section_slug\s*=\s*["'](?P<slug>[^"']+)["']"""
)


def _owner_conditional_slugs(partial: Path) -> set[str]:
    """Return the slugs a partial renders inside ``{% if is_tenant_owner %}``.

    Depth-matched rather than matched to the first ``{% endif %}``: a
    nested conditional inside the owner block (none today, but the
    partials are edited by hand) would otherwise close the owner block
    early and quietly shrink the set this guard compares.

    Args:
        partial: Path to an area body partial.

    Returns:
        The set of ``section_slug`` values enclosed by an owner
        conditional at any depth.

    Raises:
        AssertionError: If the partial's ``{% if %}``/``{% endif %}``
            blocks are unbalanced — the scan would be meaningless, and
            a real Jinja render would fail anyway.
    """
    source = partial.read_text(encoding="utf-8")
    stack: list[bool] = []
    enclosed: set[str] = set()
    for match in _IF_OR_SLUG_RE.finditer(source):
        tag = match.group("tag")
        if tag == "if":
            stack.append(match.group("expr").strip() == _OWNER_FLAG)
        elif tag == "endif":
            assert stack, f"{partial.name}: {{% endif %}} with no open {{% if %}}"
            stack.pop()
        elif any(stack):
            enclosed.add(match.group("slug"))
    assert not stack, f"{partial.name}: {len(stack)} unclosed {{% if %}} block(s)"
    return enclosed


def _partial_path(area_slug: str) -> Path:
    """Return the body-partial path for ``area_slug``.

    The mapping is a convention (``_<area_slug>_body.html``) rather than
    a lookup table, so a renamed partial trips the ``exists`` assertion
    in the tests below.
    """
    return _AREAS_PARTIALS_DIR / f"_{area_slug}_body.html"


def _rendered_slugs(partial: Path) -> list[str]:
    """Extract the ordered ``section_slug`` values a body partial renders."""
    return _SECTION_SLUG_RE.findall(partial.read_text(encoding="utf-8"))


def test_every_area_has_a_body_partial() -> None:
    """Each area in :data:`web.shell._AREAS` must have a body partial."""
    missing = [area.slug for area in all_areas() if not _partial_path(area.slug).exists()]
    assert not missing, (
        "Every area needs a body partial at "
        "web/templates/_partials/areas/_<area>_body.html. "
        f"Missing partials for: {missing}"
    )


def test_section_catalogue_matches_body_partials() -> None:
    """Catalogue slugs and order must match each area's rendered sections."""
    drift: list[str] = []
    for area in _AREAS:
        partial = _partial_path(area.slug)
        assert partial.exists(), f"expected body partial missing: {partial}"
        rendered = _rendered_slugs(partial)
        catalogue = [section.slug for section in all_sections(area.slug)]
        if rendered != catalogue:
            drift.append(f"area '{area.slug}': catalogue={catalogue} rendered={rendered}")
    assert not drift, (
        "web.shell._SECTIONS_BY_AREA has drifted from the area body "
        "partials. Reconcile the catalogue (or the partial) so slugs and "
        "order match:\n" + "\n".join(drift)
    )


def test_body_partials_carry_no_title_literals() -> None:
    """No body partial may re-introduce a ``section_title=`` literal.

    The heading is the catalogue's to state (P-UX-2). A literal here
    would render a title that the section indicator and command search
    do not know about — the exact two-sources drift this module exists
    to prevent, one level deeper than slugs.
    """
    offenders: list[str] = []
    for area in _AREAS:
        partial = _partial_path(area.slug)
        assert partial.exists(), f"expected body partial missing: {partial}"
        hits = _SECTION_TITLE_RE.findall(partial.read_text(encoding="utf-8"))
        if hits:
            offenders.append(f"{partial.relative_to(_REPO_ROOT)}: {len(hits)} occurrence(s)")
    assert not offenders, (
        "Area body partials must not declare section titles. The title "
        "belongs in web.shell._SECTIONS_BY_AREA, which areas/_section.html "
        "reads via pf_section_title(active_area, section_slug). Remove the "
        "literal(s) in:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# web.shell.section_title — the lookup the template calls
# ---------------------------------------------------------------------------


def test_section_title_resolves_a_catalogue_pair() -> None:
    """A known ``(area, section)`` pair resolves to its catalogue title."""
    assert section_title("transactions", "blotter") == "Blotter"


def test_section_title_is_keyed_on_the_pair_not_the_slug() -> None:
    """``providers-credentials`` lives in two areas and must stay distinct.

    Both owning areas resolve it; a third area does not. A helper
    reduced to a slug-only scan would answer for ``transactions`` too,
    and this test would fail — which is the point.
    """
    assert section_title("admin", "providers-credentials") == "Providers & Credentials"
    assert section_title("assistants", "providers-credentials") == "Providers & Credentials"
    with pytest.raises(LookupError):
        section_title("transactions", "providers-credentials")


def test_section_title_raises_for_an_unknown_pair() -> None:
    """An unlisted slug is drift, and drift must not render silently."""
    with pytest.raises(LookupError):
        section_title("front_office", "no-such-section")


# ---------------------------------------------------------------------------
# owner_only — the catalogue mirrors the partial's owner conditional
# ---------------------------------------------------------------------------


def test_owner_only_sections_sit_inside_the_owner_conditional() -> None:
    """``owner_only`` and ``{% if is_tenant_owner %}`` must name the same set.

    Both directions are drift. A slug flagged in the catalogue but
    rendered unconditionally hides a section from a member who can in
    fact reach it; a slug inside the conditional that the catalogue does
    not flag puts a link in the member's sidebar and command palette
    that lands on a section their page never rendered — the bug
    P-UX-A0c exists to remove.
    """
    drift: list[str] = []
    for area in _AREAS:
        partial = _partial_path(area.slug)
        assert partial.exists(), f"expected body partial missing: {partial}"
        rendered = _owner_conditional_slugs(partial)
        flagged = {section.slug for section in all_sections(area.slug) if section.owner_only}
        if rendered != flagged:
            drift.append(
                f"area '{area.slug}': owner_only={sorted(flagged)} "
                f"inside-conditional={sorted(rendered)}"
            )
    assert not drift, (
        "web.shell._SECTIONS_BY_AREA's owner_only flags have drifted from "
        "the {% if is_tenant_owner %} blocks in the area body partials. "
        "The flag mirrors the partial; reconcile the two:\n" + "\n".join(drift)
    )


def test_member_index_keeps_order_and_omits_owner_only() -> None:
    """The member index is the owner index minus the flagged entries.

    Nothing is reordered and nothing else is dropped: the projection is
    a filter over the catalogue, so the sidebar's second level reads the
    same for both roles apart from the missing entries.
    """
    for area in _AREAS:
        owner_index = section_index_for(area.slug, is_tenant_owner=True)
        member_index = section_index_for(area.slug, is_tenant_owner=False)
        flagged = {section.slug for section in all_sections(area.slug) if section.owner_only}
        expected = [entry for entry in owner_index if entry["slug"] not in flagged]
        assert member_index == expected, (
            f"area '{area.slug}': member index {[e['slug'] for e in member_index]} "
            f"is not the owner index minus {sorted(flagged)}"
        )

    assert [entry["slug"] for entry in section_index_for("admin", is_tenant_owner=False)] == [
        "data-import",
        "providers-credentials",
        "investments",
    ]


def test_landing_section_is_never_owner_only() -> None:
    """A landing section has to be in every role's index.

    ``landing_section_for`` is deliberately role-blind — it reads the
    full catalogue — and the server renders that section visible. Were
    it flagged ``owner_only``, a member's page would render without it
    while the shell still tried to land on it, leaving no visible
    section at all. This guard, not the function, is what keeps that
    from being reachable.
    """
    offenders: list[str] = []
    for area in _AREAS:
        landing = landing_section_for(area.slug)
        for section in all_sections(area.slug):
            if section.slug == landing and section.owner_only:
                offenders.append(f"area '{area.slug}': landing section '{landing}' is owner_only")
    assert not offenders, (
        "A landing section must be visible to every role. Move the "
        "landing flag to a section the catalogue does not gate:\n" + "\n".join(offenders)
    )


def test_section_index_for_requires_the_role() -> None:
    """Forgetting the role is a ``TypeError``, not a silent owner render.

    The keyword has no default on purpose: the sidebar's second level is
    the one place a member could be handed a link to a section their
    page does not contain, so a caller that omits the role fails at the
    call rather than quietly rendering the owner list to everyone.
    """
    with pytest.raises(TypeError):
        section_index_for("admin")  # type: ignore[call-arg]
