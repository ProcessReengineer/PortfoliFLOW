# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural invariants for Shirley as a shell element (P-UX-A0e).

DB-free regex over the sources — no app, no browser, no database. What
these pin is the *shape* that makes one chat instance possible; the
rendered behaviour is ``tests/web/test_shirley_dock.py``'s.

Three things drift easily and break the whole arrangement quietly:

* **One host each.** A second ``#dock-chat-host`` or ``#stage-chat-host``
  would give ``shirley.js`` two places to move the conversation into,
  and a second copy of ``#chat-form`` the moment either one loaded.
* **Three states, named areas.** ``layout.css`` must declare all three
  ``data-shirley`` variants and place its children by ``grid-area``;
  the implicit column order cannot express the stage, which shares the
  middle cell with ``#shell-main``.
* **One keyboard file.** Ctrl J lives beside Ctrl K in
  ``section_nav.js``, so the shell's two shortcuts cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAYOUT_CSS = _REPO_ROOT / "web" / "static" / "css" / "layout.css"
_BASE_HTML = _REPO_ROOT / "web" / "templates" / "base.html"
_DOCK_HTML = _REPO_ROOT / "web" / "templates" / "_partials" / "shirley_dock.html"
_SECTION_NAV_JS = _REPO_ROOT / "web" / "static" / "js" / "section_nav.js"
_SHIRLEY_JS = _REPO_ROOT / "web" / "static" / "js" / "shirley.js"

#: ``id="chat-`` occurrences in the partial. Pinned to the number the
#: pre-move ``shirley_section.html`` carried — brief banner, history,
#: pin dialog, form, input, attach, send — so the move is provably a
#: move and not a rewrite. A new anchor id is a deliberate change to
#: this number, never an accident.
_CHAT_ANCHOR_IDS = 7


def test_layout_declares_all_three_shirley_states() -> None:
    css = _LAYOUT_CSS.read_text()
    for value in ("closed", "docked", "stage"):
        assert f'data-shirley="{value}"' in css, f"no rule for the {value} state"


def test_layout_places_the_shell_children_by_named_area() -> None:
    """The stage shares the ``main`` cell, so order cannot place them."""
    css = _LAYOUT_CSS.read_text()
    assert "grid-template-areas" in css
    for area in ("nav", "main", "side", "status"):
        assert f"grid-area: {area};" in css, f"nothing is placed in the {area} area"


def test_layout_clears_the_statusbar_on_both_sticky_columns() -> None:
    """The baseline bug: a flat ``100vh`` column ran under the status bar."""
    css = _LAYOUT_CSS.read_text()
    assert css.count("height: calc(100vh - var(--pf-statusbar-height));") == 2


def test_base_html_carries_exactly_one_of_each_host() -> None:
    html = _BASE_HTML.read_text()
    assert html.count('id="dock-chat-host"') == 1
    assert html.count('id="stage-chat-host"') == 1
    assert html.count('id="pf-side"') == 1
    assert html.count('id="pf-stage"') == 1
    # The dock fetches itself, once, on the first open.
    assert 'hx-get="/chat/dock"' in html
    assert 'hx-trigger="pf:shirley-open once"' in html


def test_the_chat_partial_kept_its_anchor_ids() -> None:
    assert _DOCK_HTML.read_text().count('id="chat-') == _CHAT_ANCHOR_IDS


def test_the_chat_partial_is_the_only_home_of_the_composer() -> None:
    """``#chat-form`` must be unique in the DOM for chat.js to work."""
    templates = _REPO_ROOT / "web" / "templates"
    homes = [
        path.relative_to(templates).as_posix()
        for path in templates.rglob("*.html")
        if 'id="chat-form"' in path.read_text()
    ]
    assert homes == ["_partials/shirley_dock.html"]


def test_ctrl_j_lives_beside_ctrl_k() -> None:
    js = _SECTION_NAV_JS.read_text()
    assert "isShirleyHotkey" in js
    assert "window.pfShirley.toggle()" in js
    # Both hotkeys read the same modifier pair, in the same file.
    assert js.count("event.metaKey || event.ctrlKey") == 2


def test_shirley_js_exposes_the_three_entry_points() -> None:
    js = _SHIRLEY_JS.read_text()
    assert "window.pfShirley = {" in js
    # One instance, moved — never cloned, and never re-processed by htmx
    # (which would double-bind every hx-* in the moved subtree). The
    # call form, so the file may still explain itself in prose.
    assert "cloneNode(" not in js
    assert "htmx.process(" not in js
