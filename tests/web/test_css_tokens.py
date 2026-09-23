# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tree-wide token invariants over ``web/static/css`` (P-UX-A0d2).

``tests/web/test_pf_components.py`` pins the *component vocabulary* —
one file, transcribed from the mock. These tests pin the rule that file
established for **every** stylesheet the shell loads:

* **Colour lives in ``theme.css``.** No stylesheet carries a hex literal
  or an ``rgb()``/``rgba()``/``hsl()`` colour; a tint is a ``color-mix``
  of a token, an elevation is ``--ui-shadow-*``. ``login.css`` is the
  one documented exception until A-5 brings the auth surface onto the
  vocabulary.
* **Every token a stylesheet reads is declared.** A ``var(--ui-…)`` name
  no theme publishes either falls back to its literal every time — so
  the theme never reaches it — or, with no fallback, computes to
  nothing. Eleven such names were live before this sweep; the count is
  now zero and stays there.
* **``--ui-button-fg`` is gone.** ``accent.on_accent`` is the single
  source for ink on a filled accent surface, and the ``button`` group
  has left ``config/ui_theme*.json``.

All of it is regex over file sources — DB-free, no app and no browser.
Comments are stripped before counting, so a hex value *quoted in prose*
(``watch_desk.css``, ``planning_desk.css``) is documentation, not a
literal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CSS_DIR = _REPO_ROOT / "web" / "static" / "css"
_THEME = _CSS_DIR / "theme.css"

#: The one stylesheet still allowed a colour literal. ``_auth_base.html``
#: does not link the component vocabulary, so login keeps its own until
#: A-5 migrates the auth surface. One site: the card's box-shadow.
_LITERAL_EXEMPT = {"login.css": 1}

#: Custom properties set from inline ``style=""`` in markup rather than
#: declared in any stylesheet — a per-instance value, not a token.
#: ``--key`` carries a legend swatch's colour on ``.pf-chart__key``.
_SET_FROM_MARKUP = {"--key"}

#: Names added to ``config/ui_theme*.json`` by this sweep. Each one had
#: a consumer that could not reach a theme before it existed.
_REQUIRED_TOKENS = (
    "--ui-font-family-mono",
    "--ui-accessible-control-row-pad",
    "--ui-accessible-border-soft",
    "--ui-shadow-panel",
    "--ui-shadow-scrim",
)

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
_FUNCTIONAL_RE = re.compile(r"\b(?:rgba?|hsla?)\(")
_DECLARED_RE = re.compile(r"(--[a-z0-9-]+)\s*:")
_REFERENCED_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)")


def _stylesheets() -> list[Path]:
    """Every stylesheet the shell owns, newest theme artefact aside.

    Returns:
        Sorted paths under ``web/static/css``, excluding third-party
        ``vendor/`` files and the generated ``theme.css`` (which *is*
        the colour source and therefore holds every literal).
    """
    return sorted(
        path
        for path in _CSS_DIR.rglob("*.css")
        if "vendor" not in path.parts and path.name != "theme.css"
    )


def _source(path: Path) -> str:
    """Read a stylesheet with its comments stripped."""
    return _COMMENT_RE.sub("", path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def declared() -> set[str]:
    """Every custom property ``theme.css`` publishes."""
    return set(_DECLARED_RE.findall(_THEME.read_text(encoding="utf-8")))


def _ids(paths: list[Path]) -> list[str]:
    return [path.name for path in paths]


_SHEETS = _stylesheets()


# ---------------------------------------------------------------------------
# Colour lives in theme.css
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sheet", _SHEETS, ids=_ids(_SHEETS))
def test_no_colour_literals(sheet: Path) -> None:
    """No hex literal outside ``theme.css``; tints are ``color-mix``."""
    hexes = _HEX_RE.findall(_source(sheet))
    assert not hexes, (
        f"{sheet.name} carries colour literals: {sorted(set(hexes))}. "
        "Map each one onto a --ui-* or --chart-* token, or onto a "
        "color-mix() of one. Colour is theme.css's alone (record §2.2.1)."
    )


@pytest.mark.parametrize("sheet", _SHEETS, ids=_ids(_SHEETS))
def test_no_functional_colour_literals(sheet: Path) -> None:
    """No ``rgba()`` either — a shadow is a token, a tint a ``color-mix``."""
    found = _FUNCTIONAL_RE.findall(_source(sheet))
    allowed = _LITERAL_EXEMPT.get(sheet.name, 0)
    assert len(found) <= allowed, (
        f"{sheet.name} carries {len(found)} functional colour literals "
        f"({found}), more than the {allowed} it is allowed. A panel or "
        "drawer shadow is var(--ui-shadow-panel), a modal backdrop is "
        "var(--ui-shadow-scrim), a tint is color-mix(in srgb, "
        "var(--ui-semantic-…) N%, transparent)."
    )


# ---------------------------------------------------------------------------
# Every token a stylesheet reads is declared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sheet", _SHEETS, ids=_ids(_SHEETS))
def test_no_dangling_token_names(sheet: Path, declared: set[str]) -> None:
    """A name no theme publishes renders its fallback, or nothing at all."""
    source = _source(sheet)
    local = set(_DECLARED_RE.findall(source))
    dangling = sorted(
        {
            name
            for name in _REFERENCED_RE.findall(source)
            if name not in declared and name not in local and name not in _SET_FROM_MARKUP
        }
    )
    assert not dangling, (
        f"{sheet.name} reads token names nothing declares: {dangling}. "
        "Either the name is wrong (theme.css publishes the right one) or "
        "the token is missing from config/ui_theme*.json. A fallback "
        "literal is not a fix — it makes the theme unreachable."
    )


# ---------------------------------------------------------------------------
# The theme's side of the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", _REQUIRED_TOKENS)
def test_theme_declares_token(token: str, declared: set[str]) -> None:
    """The five names this sweep added must survive a regeneration."""
    assert token in declared, (
        f"theme.css no longer declares {token}. It has a consumer under "
        "web/static/css; removing it from config/ui_theme*.json leaves "
        "that consumer computing nothing."
    )


def test_theme_does_not_declare_button_fg(declared: set[str]) -> None:
    """The ``button`` group left the JSON; ``accent.on_accent`` wins."""
    assert "--ui-button-fg" not in declared, (
        "config/ui_theme*.json has a `button` group again. Ink on a "
        "filled accent surface is accent.on_accent — one source, not two."
    )


def test_button_fg_is_referenced_nowhere() -> None:
    """No stylesheet reads the retired token."""
    offenders = sorted(
        sheet.relative_to(_REPO_ROOT).as_posix()
        for sheet in _SHEETS
        if "--ui-button-fg" in sheet.read_text(encoding="utf-8")
    )
    assert not offenders, (
        f"These stylesheets still read --ui-button-fg: {offenders}. "
        "The token is gone from theme.css, so each site computes "
        "nothing. Use var(--ui-accent-on-accent)."
    )
