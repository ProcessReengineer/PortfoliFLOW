# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""P-GP-1 — sidebar footer polish.

The footer used to render the signed-in e-mail address as its own
line above the Collapse and Sign out controls. On short viewports
the un-scrolled nav ran under the footer and the address painted
over the last entries. The address line is gone (redundant with the
welcome header, ADR-0068); the address itself moved into the Sign
out button's ``title`` tooltip, and the nav is now the sidebar's own
scroll region, so the footer can never be overlapped. These tests
pin all three at the source level; the browser walk is the
operator's.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SIDEBAR = _ROOT / "web" / "templates" / "_partials" / "sidebar.html"
_LAYOUT = _ROOT / "web" / "static" / "css" / "layout.css"


def _rule(css: str, selector: str) -> str:
    """Body of the rule whose selector list is exactly ``selector``.

    Anchored at line start: the collapsed-state variants
    (``.pf-shell[data-sidebar-collapsed="true"] .pf-sidebar__footer``)
    precede the base rule in the file and must not be matched.
    """
    pattern = r"^" + re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, css, re.MULTILINE)
    assert match is not None, f"layout.css is missing the {selector} rule"
    return match.group(1)


def test_sidebar_footer_no_longer_renders_the_address_line() -> None:
    html = _SIDEBAR.read_text(encoding="utf-8")
    assert "pf-sidebar__user" not in html
    # The address variable still gates the sign-out form.
    assert html.count("{% if user_email %}") == 1
    assert 'action="/logout"' in html


def test_sign_out_button_carries_the_address_as_tooltip() -> None:
    html = _SIDEBAR.read_text(encoding="utf-8")
    button = re.search(
        r'<button type="submit"\s+class="pf-sidebar__signout-button"\s+'
        r'title="Signed in as \{\{ user_email \}\}">',
        html,
    )
    assert button is not None, "sign-out button lacks the Signed-in-as title"
    # The address appears exactly once in the partial — in that title.
    assert html.count("{{ user_email }}") == 1


def test_layout_dropped_the_user_rules() -> None:
    css = _LAYOUT.read_text(encoding="utf-8")
    assert "pf-sidebar__user" not in css


def test_sidebar_nav_is_its_own_scroll_region() -> None:
    css = _LAYOUT.read_text(encoding="utf-8")
    nav = _rule(css, ".pf-sidebar__nav")
    assert "overflow-y: auto" in nav
    assert "overflow-x: hidden" in nav
    assert "min-height: 0" in nav


def test_sidebar_footer_is_opaque() -> None:
    css = _LAYOUT.read_text(encoding="utf-8")
    footer = _rule(css, ".pf-sidebar__footer")
    assert "background-color: var(--ui-background-chrome)" in footer
