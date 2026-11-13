# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""P-GP-7 — the sidebar's overflow cue.

The nav scrolls without a visible bar (P-GP-3), so a list longer than the
window ended without a sign: Admin's last Section sat below the footer and
was taken to be missing. The nav now fades at an edge while entries lie
beyond it. These tests pin the three parts at the source level — the mask
rules, the one script that writes the state attribute, and its script tag;
whether the fade *shows* is the operator's browser walk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LAYOUT = _ROOT / "web" / "static" / "css" / "layout.css"
_SCRIPT = _ROOT / "web" / "static" / "js" / "sidebar_overflow.js"
_BASE = _ROOT / "web" / "templates" / "base.html"
_SIDEBAR = _ROOT / "web" / "templates" / "_partials" / "sidebar.html"

_STATES = ("below", "above", "both")


def _rule(css: str, selector: str) -> str:
    """Body of the rule whose selector is exactly ``selector``."""
    pattern = r"^" + re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, css, re.MULTILINE)
    assert match is not None, f"layout.css is missing the {selector} rule"
    return match.group(1)


@pytest.mark.parametrize("state", _STATES)
def test_each_overflow_state_masks_the_nav(state: str) -> None:
    """Every state the script can write has a mask rule on the token scale."""
    css = _LAYOUT.read_text(encoding="utf-8")
    rule = _rule(css, f'.pf-sidebar__nav[data-pf-overflow="{state}"]')
    assert "mask-image: linear-gradient(" in rule
    assert "var(--ui-space-6)" in rule


def test_the_cue_does_not_bring_the_scrollbar_back() -> None:
    """P-GP-3 stands: the cue is a fade, the bar stays hidden."""
    css = _LAYOUT.read_text(encoding="utf-8")
    assert "scrollbar-width: none" in _rule(css, ".pf-sidebar__nav")


def test_script_writes_exactly_the_states_the_stylesheet_knows() -> None:
    js = _SCRIPT.read_text(encoding="utf-8")
    assert '"data-pf-overflow"' in js
    for state in _STATES:
        assert f'"{state}"' in js, state


def test_script_survives_the_out_of_band_sidebar_swap() -> None:
    """Nothing is bound to the nav element, which every Area swap replaces."""
    js = _SCRIPT.read_text(encoding="utf-8")
    assert "capture: true" in js
    assert '"htmx:oobAfterSwap"' in js
    assert '"htmx:afterSettle"' in js


def test_script_issues_no_request_and_sets_no_hx_attribute() -> None:
    js = re.sub(r"/\*.*?\*/", "", _SCRIPT.read_text(encoding="utf-8"), flags=re.DOTALL)
    js = re.sub(r"//[^\n]*", "", js)
    for needle in ("fetch(", "htmx.ajax", "XMLHttpRequest", "EventSource", "hx-"):
        assert needle not in js, needle


def test_base_template_loads_the_script_deferred() -> None:
    base = _BASE.read_text(encoding="utf-8")
    assert '<script src="/static/js/sidebar_overflow.js" defer></script>' in base


def test_the_server_never_renders_the_state() -> None:
    """The attribute is the script's alone — a rendered value would go stale."""
    assert "data-pf-overflow" not in _SIDEBAR.read_text(encoding="utf-8")
