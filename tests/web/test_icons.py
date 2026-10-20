# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the vendored Lucide icon set and the ``pf_icon`` global.

These are DB-free: the icon layer reads SVG files from the tree and the
registration check builds the app without a configured database URL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from markupsafe import Markup

from web.icons import ICONS, _VENDOR_DIR, _inner, pf_icon
from web.main import create_app
from web.settings import WebSettings

#: The vendored set's manifest: a table row per icon, the Lucide stem in its
#: second column, and one paragraph opening with the total.
_MANIFEST = _VENDOR_DIR / "MANIFEST.md"
_MANIFEST_STEM = re.compile(r"^\|\s*`[a-z0-9-]+`\s*\|\s*`([a-z0-9-]+)`\s*\|")
_MANIFEST_TOTAL = re.compile(r"^(\d+) icons in total\.")


def test_every_icon_resolves_to_a_vendored_file() -> None:
    """ICONS and the vendored set must not drift apart."""
    missing = [
        f"{name} -> {stem}.svg"
        for name, stem in ICONS.items()
        if not (_VENDOR_DIR / f"{stem}.svg").is_file()
    ]
    assert not missing, (
        "ICONS points at stems the vendored Lucide set does not ship: "
        f"{missing}. Vendor the file or correct the stem in web/icons.py."
    )


def test_vendor_dir_is_the_tracked_lucide_directory() -> None:
    """The loader reads from the tree, not from an install location."""
    repo_root = Path(__file__).resolve().parents[2]
    assert _VENDOR_DIR.is_dir()
    # relative_to() raises unless the directory really sits in the checkout.
    assert _VENDOR_DIR.relative_to(repo_root) == Path("web/static/vendor/lucide")


def test_pf_icon_renders_one_decorative_svg() -> None:
    """The default form is a single, decorative, 16 px SVG."""
    markup = pf_icon("close")
    assert isinstance(markup, Markup)
    rendered = str(markup)
    assert 'class="pf-icon"' in rendered
    assert 'width="16"' in rendered
    assert 'height="16"' in rendered
    assert 'stroke-width="1.5"' in rendered
    assert 'stroke="currentColor"' in rendered
    assert 'aria-hidden="true"' in rendered
    assert 'focusable="false"' in rendered
    # The vendored wrapper must be unwrapped, not nested inside ours.
    assert rendered.count("<svg") == 1
    assert rendered.count("</svg>") == 1
    assert "<path" in rendered
    # The upstream licence comment sits outside the root element and must
    # not survive into the page.
    assert "<!--" not in rendered


def test_pf_icon_size_is_overridable() -> None:
    """A caller may scale the icon without leaving the helper."""
    rendered = str(pf_icon("close", size=24))
    assert 'width="24"' in rendered
    assert 'height="24"' in rendered
    # The 24-unit viewBox is the coordinate system, not the rendered size.
    assert 'viewBox="0 0 24 24"' in rendered


def test_pf_icon_with_label_is_an_image_with_a_name() -> None:
    """An icon-only control needs an accessible name, not aria-hidden."""
    rendered = str(pf_icon("close", label="Close"))
    assert 'role="img"' in rendered
    assert 'aria-label="Close"' in rendered
    assert "aria-hidden" not in rendered


def test_pf_icon_escapes_the_label() -> None:
    """A label is data; it must not be able to close the attribute."""
    rendered = str(pf_icon("close", label='Close "the" <panel>'))
    assert 'aria-label="Close ' in rendered
    assert "<panel>" not in rendered
    assert "&lt;panel&gt;" in rendered
    assert "&#34;" in rendered or "&quot;" in rendered
    # One element still, despite the angle brackets in the label.
    assert rendered.count("<svg") == 1


def test_pf_icon_rejects_an_unknown_name() -> None:
    """A template typo fails the render rather than rendering a gap."""
    with pytest.raises(LookupError):
        pf_icon("no-such-icon")


def test_inner_loader_is_cached() -> None:
    """The shell re-renders the same icons on every request."""
    pf_icon("close")  # ensure the entry is warm, whatever ran before
    before = _inner.cache_info().hits
    pf_icon("close")
    assert _inner.cache_info().hits == before + 1


def test_pf_icon_is_registered_as_a_jinja_global() -> None:
    """Templates reach the helper as ``pf_icon``, with no import."""
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        database_url=None,
    )
    app = create_app(settings)
    assert app.state.templates.env.globals["pf_icon"] is pf_icon


def test_pf_icon_renders_through_jinja() -> None:
    """The global is usable from template source, unescaped."""
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        database_url=None,
    )
    env = create_app(settings).state.templates.env
    rendered = env.from_string('{{ pf_icon("search") }}').render()
    assert rendered.startswith("<svg")
    assert "&lt;svg" not in rendered


def test_the_voice_call_icons_are_registered() -> None:
    """The call button, the end button and the muted state (ADR-0137 §8) resolve by name."""
    assert (ICONS["call"], ICONS["call-end"], ICONS["mute"]) == ("phone", "phone-off", "mic-off")
    for name in ("call", "call-end", "mute"):
        assert str(pf_icon(name)).startswith("<svg"), name


def test_the_manifest_lists_exactly_the_vendored_icons() -> None:
    """``MANIFEST.md``, the vendored ``*.svg`` files and ``ICONS`` name one set of stems."""
    lines = _MANIFEST.read_text(encoding="utf-8").splitlines()
    manifest_stems = {m.group(1) for m in map(_MANIFEST_STEM.search, lines) if m}
    totals = [int(m.group(1)) for m in map(_MANIFEST_TOTAL.match, lines) if m]
    vendored_stems = {path.stem for path in _VENDOR_DIR.glob("*.svg")}

    assert manifest_stems, "MANIFEST.md names no icon rows."
    assert totals == [len(manifest_stems)], (
        f"MANIFEST.md states {totals} icons in total but lists {len(manifest_stems)} rows."
    )
    assert manifest_stems == vendored_stems, (
        "MANIFEST.md and the vendored files drift: "
        f"manifest only {sorted(manifest_stems - vendored_stems)}, "
        f"files only {sorted(vendored_stems - manifest_stems)}."
    )
    assert vendored_stems == set(ICONS.values()), (
        "ICONS and the vendored files drift: "
        f"files only {sorted(vendored_stems - set(ICONS.values()))}, "
        f"ICONS only {sorted(set(ICONS.values()) - vendored_stems)}."
    )
