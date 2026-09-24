# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the component vocabulary, ``components/pf_components.css``.

The file is transcribed from the DC-UX-D mock's ``shared.css`` onto the
generated ``--ui-*`` tokens (P-UX-A0d). These tests are regexes over the
file source and the base template — DB-free, no app and no browser.

What they pin:

* the file exists, and ``base.html`` links it after ``layout.css`` and
  before every other ``components/*.css`` — the load order is what lets
  a scoped legacy rule keep its look while the surfaces migrate;
* it carries no colour literal and no reference to the mock's private
  ``--pf-*`` namespace;
* every token it reads is declared in ``theme.css``;
* every class the transcription is supposed to define is present. The
  expected list is written out here rather than parsed from the mock, so
  the suite does not depend on ``~/DC-UX-D`` being on disk;
* the accessible-mode remap and its collapsed-sidebar guard;
* the statusbar shortcut hint is gone from ``layout.css`` (P-UX-A0d §6);
* no container query survives the transcription — ``.pf-main`` declares
  no container yet, so one would be inert;
* **R10's number entry** in the one Area that has swept itself (P-UX-A1d):
  no ``type="number"`` left under ``_partials/transactions/``, and every
  ``inputmode="decimal"`` input paired with the ``pf-read`` slot the
  recalculation fills. Template source, same regex posture as the rest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CSS_DIR = _REPO_ROOT / "web" / "static" / "css"
_COMPONENTS = _CSS_DIR / "components" / "pf_components.css"
_THEME = _CSS_DIR / "theme.css"
_LAYOUT = _CSS_DIR / "layout.css"
_BASE_HTML = _REPO_ROOT / "web" / "templates" / "base.html"

#: Every class the §1 transcription table says the file defines. Taken
#: from the prompt's table, not from the mock, so the test is a contract
#: rather than a mirror of an external file.
_EXPECTED_CLASSES = (
    # Kbd
    "pf-kbd",
    # The one button family — the bare class *is* the default; there is
    # no --default modifier to assert.
    "pf-btn",
    "pf-btn--primary",
    "pf-btn--quiet",
    "pf-btn--danger",
    "pf-btn--icon",
    "pf-btn--sm",
    # Chart frame
    "pf-chart",
    "pf-chart__title",
    "pf-chart__legend",
    "pf-chart__key",
    "pf-chart__plot",
    # Crumb
    "pf-crumb",
    "pf-crumb__back",
    "pf-crumb__sep",
    "pf-crumb__here",
    # Functional state
    "pf-state",
    "pf-state--draft",
    "pf-state--unsaved",
    "pf-state--proposed",
    "pf-state--approved",
    "pf-state--booked",
    "pf-state--reversed",
    "pf-state--cancelled",
    "pf-state--dormant",
    # Notice and message
    "pf-note",
    "pf-note--info",
    "pf-note--warn",
    "pf-note--block",
    "pf-note__lead",
    "pf-note__sub",
    "pf-note__inline",
    "pf-neg",
    # Disclosure
    "pf-more",
    # Table
    "pf-table",
    "pf-table__row",
    "pf-table__slot",
    "pf-table__chev",
    "pf-table__id",
    "pf-table__sub",
    "pf-table__unit",
    "pf-table__actions",
    "pf-table__creating",
    # Menu
    "pf-menu",
    "pf-menu__list",
    "pf-menu__item",
    "pf-menu__item--danger",
    # Slot panel
    "pf-panel",
    "pf-panel__head",
    "pf-panel__titles",
    "pf-panel__title",
    "pf-panel__sub",
    "pf-panel__foot",
    "pf-panel__chart",
    "pf-facts",
    # Form
    "pf-form",
    "pf-form__main",
    "pf-field",
    "pf-field--full",
    "pf-field--half",
    "pf-field--wide",
    "pf-label",
    "pf-input",
    "pf-input--num",
    "pf-select",
    "pf-textarea",
    "pf-check",
    "pf-hint",
    "pf-optional",
    # Stepper (R4). Authored in P-UX-A1c rather than transcribed: the record
    # specifies the family and its three states but ships no rules for it,
    # and `pf-step` is the setup checklist's below.
    "pf-stepper",
    "pf-stepper__step",
    "pf-stepper__dot",
    "pf-stepper__label",
    # Summary rail
    "pf-rail-sum",
    "pf-rail-sum__title",
    "pf-sum",
    "pf-sum__formula",
    "pf-sum__total",
    "pf-leg",
    "pf-leg__type",
    "pf-leg__amount",
    # Action bar
    "pf-actionbar",
    "pf-actionbar__hint",
    # Flow list, filters, empty
    "pf-flows",
    "pf-flow",
    "pf-flow__name",
    "pf-flow__hint",
    "pf-filters",
    "pf-filters__field",
    "pf-empty",
    "pf-empty__lead",
    # As-of, figures, sources, activity, skeleton
    "pf-asof",
    "pf-asof__pair",
    "pf-figures",
    "pf-sources",
    "pf-activity",
    "pf-activity__btn",
    "pf-activity__panel",
    "pf-skel",
    "pf-skel--plot",
    "pf-spin",
    "pf-sr",
    # Setup checklist, switch/settings, number-entry feedback
    "pf-setup",
    "pf-setup__head",
    "pf-setup__title",
    "pf-setup__count",
    "pf-step",
    "pf-step__mark",
    "pf-step__name",
    "pf-step__sub",
    "pf-step__tag",
    "pf-step__action",
    "pf-switch",
    "pf-settings",
    "pf-settings__group",
    "pf-settings__title",
    "pf-setting",
    "pf-setting__name",
    "pf-setting__sub",
    "pf-setting__control",
    "pf-read",
    # Tabs, toolbar, sortable header
    "pf-tabs",
    "pf-tab",
    "pf-toolbar",
    "pf-toolbar__spacer",
    "pf-pos",
    # Reading width, action groups, invalid field, self marker
    "pf-narrow",
    "pf-scroll-x",
    "pf-acts",
    "pf-act",
    "pf-act__row",
    "pf-act__warn",
    "pf-act__text",
    "pf-self",
)


@pytest.fixture(scope="module")
def css() -> str:
    """The component file's source."""
    assert _COMPONENTS.is_file(), f"{_COMPONENTS} does not exist"
    return _COMPONENTS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_base_html_links_the_component_file_first() -> None:
    """Linked after layout.css and before every other components/*.css."""
    html = _BASE_HTML.read_text(encoding="utf-8")
    hrefs = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)

    assert "/static/css/components/pf_components.css" in hrefs, (
        "base.html does not link the component vocabulary. Add the <link> "
        "before the first components/*.css sheet."
    )
    ours = hrefs.index("/static/css/components/pf_components.css")
    assert ours > hrefs.index("/static/css/layout.css"), (
        "pf_components.css must load after layout.css — the shell grid "
        "owns the tokens the components read."
    )

    others = [
        (index, href)
        for index, href in enumerate(hrefs)
        if href.startswith("/static/css/components/") and index != ours
    ]
    too_early = [href for index, href in others if index < ours]
    assert not too_early, (
        "These component sheets load before the vocabulary: "
        f"{too_early}. The vocabulary must come first so a scoped legacy "
        "rule out-specifies it rather than being overridden by it."
    )


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_no_colour_literals(css: str) -> None:
    """Colour belongs to theme.css; this file only references it."""
    hexes = re.findall(r"#[0-9A-Fa-f]{3,8}\b", css)
    assert not hexes, (
        f"pf_components.css carries colour literals: {sorted(set(hexes))}. "
        "Map each one onto a --ui-* or --chart-* token."
    )
    functional = re.findall(r"\b(?:rgb|hsl)\(", css)
    assert not functional, f"pf_components.css carries functional colour literals: {functional}."


def test_no_mock_token_namespace(css: str) -> None:
    """The mock's private --pf-* tokens do not exist in the product."""
    leaked = sorted(set(re.findall(r"var\(\s*(--pf-[a-z0-9-]+)", css)))
    assert not leaked, (
        f"pf_components.css references the mock's token namespace: {leaked}. "
        "Map each one onto its --ui-* equivalent."
    )


def test_every_referenced_token_is_declared(css: str) -> None:
    """A var(--ui-…) or var(--chart-…) that theme.css does not declare."""
    referenced = set(re.findall(r"var\(\s*(--(?:ui|chart)-[a-z0-9-]+)", css))
    declared = set(
        re.findall(r"^\s*(--(?:ui|chart)-[a-z0-9-]+)\s*:", _THEME.read_text(encoding="utf-8"), re.M)
    )
    unknown = sorted(referenced - declared)
    assert not unknown, (
        f"pf_components.css reads tokens theme.css does not declare: {unknown}. "
        "Add them to config/ui_theme*.json and regenerate, or correct the name."
    )


def test_no_container_queries(css: str) -> None:
    """.pf-main declares no container, so a container query would be inert."""
    assert "@container" not in css, (
        "pf_components.css contains a container query. .pf-main must declare "
        "`container: view / inline-size` before any of them can land "
        "(P-UX-A0d §6)."
    )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("class_name", _EXPECTED_CLASSES)
def test_class_is_defined(css: str, class_name: str) -> None:
    """Every class the transcription table names carries at least one rule."""
    # Word boundary on the right so `.pf-btn` does not match `.pf-btn--sm`.
    pattern = rf"\.{re.escape(class_name)}(?![\w-])"
    assert re.search(pattern, css), (
        f".{class_name} is not defined in pf_components.css. The §1 transcription table lists it."
    )


def test_accessible_mode_remap_and_collapsed_guard(css: str) -> None:
    """The a11y attribute remaps the tokens and survives a collapsed nav."""
    assert '.pf-shell[data-a11y="on"]' in css
    assert '.pf-shell[data-a11y="on"][data-sidebar-collapsed="true"]' in css, (
        "The accessible mode's wider nav must not defeat the collapsed "
        "state. The product's attribute is data-sidebar-collapsed."
    )
    # The remap reads the accessible ladder rather than restating sizes.
    for token in (
        "--ui-accessible-font-scale-xs",
        "--ui-accessible-text-secondary",
        "--ui-accessible-border-default",
        "--ui-accessible-control-height",
        "--ui-accessible-motion-duration",
        "--ui-accessible-layout-nav-width",
    ):
        assert token in css, f"accessible mode does not read {token}"


def test_hidden_containers_stay_hidden(css: str) -> None:
    """A class that sets `display` needs its own [hidden] guard.

    An author `display` declaration beats the user-agent's
    ``[hidden] { display: none }`` whatever the specificity — the same
    trap ``.pf-section`` documents in layout.css. P-UX-A0d §5 places two
    empty ``hidden`` containers, and ``.pf-sources`` sets
    ``display: flex``, so without the guard the empty sources line would
    draw its top border as a stray rule under every answer.
    """
    for selector in (".pf-sources[hidden]", ".pf-activity__panel[hidden]"):
        assert selector in css, f"{selector} is missing its display: none guard"
    # .pf-activity itself must not set display, or it needs a guard too.
    activity = re.search(r"^\.pf-activity \{([^}]*)\}", css, re.M)
    assert activity is not None
    assert "display" not in activity.group(1), (
        ".pf-activity now sets display; the hidden statusbar container "
        "needs a .pf-activity[hidden] guard."
    )


def test_primary_button_reads_on_accent(css: str) -> None:
    """accent.on_accent wins over the legacy button.fg (Phase A)."""
    assert "--ui-accent-on-accent" in css
    assert "--ui-button-fg" not in css, (
        "pf-btn--primary must read --ui-accent-on-accent; --ui-button-fg is "
        "the legacy token A0d2 and A-3 remove."
    )


# ---------------------------------------------------------------------------
# The retired statusbar shortcut hint (P-UX-A0d §6)
# ---------------------------------------------------------------------------


def test_layout_css_has_no_statusbar_shortcut_rules() -> None:
    """The view header's search field is the one shortcut affordance."""
    layout = _LAYOUT.read_text(encoding="utf-8")
    assert "pf-statusbar__shortcut" not in layout, (
        "layout.css still styles the retired statusbar shortcut hint."
    )


# ---------------------------------------------------------------------------
# R10 number entry, in the Area that has swept itself (P-UX-A1d)
# ---------------------------------------------------------------------------

_TX_PARTIALS = _REPO_ROOT / "web" / "templates" / "_partials" / "transactions"

#: How many `inputmode="decimal"` inputs each template draws. Written out so
#: a field silently losing its slot — or a new amount field arriving without
#: one — fails here rather than in a browser.
_DECIMAL_INPUTS: dict[str, int] = {
    "_order_composer.html": 4,
    "_wizard_order.html": 4,
    "_secondary_buy_composer.html": 3,
    "_secondary_sale_composer.html": 3,
    "_commitment_composer.html": 1,
    "_settlement.html": 1,
}

_INPUT = re.compile(r"<input\b[^>]*>", re.DOTALL)
_ATTR = re.compile(r'(\w[\w-]*)="([^"]*)"')


def test_the_transactions_partials_draw_no_native_number_field() -> None:
    """R10: the Area's 18 `type="number"` fields are gone, all of them.

    Two notations cannot both reach a `type="number"` input — the browser
    decides which one it accepts, by locale, and silently discards the other.
    The whole rule rests on the control being plain text.
    """
    offenders = sorted(
        path.name for path in _TX_PARTIALS.glob("*.html") if 'type="number"' in path.read_text()
    )
    assert not offenders, f"R10 is not held in {offenders}"


@pytest.mark.parametrize(("name", "expected"), sorted(_DECIMAL_INPUTS.items()))
def test_every_amount_input_has_its_reading_slot(name: str, expected: int) -> None:
    """Each `inputmode="decimal"` input is followed by its own `pf-read` slot.

    The slot is a **sibling** of the input and never its content: the
    recalculation refreshes the reading on every keystroke, and re-rendering
    the input itself would move the caret (see `_order_recalc.html`). The id
    is `tx-read-{name}` — keyed on the posted name, not on the input's own id,
    so the route can address the slot without knowing the template.
    """
    source = (_TX_PARTIALS / name).read_text(encoding="utf-8")
    fields = [
        dict(_ATTR.findall(tag)) for tag in _INPUT.findall(source) if 'inputmode="decimal"' in tag
    ]

    assert len(fields) == expected, f"{name} draws {len(fields)} amount inputs, expected {expected}"
    for field in fields:
        assert field.get("type") == "text", field
        assert "step" not in field, f"{field.get('name')} still carries a number-field step"
        assert f'id="tx-read-{field["name"]}"' in source, f"{field['name']} has no reading slot"


def test_the_vintage_year_is_a_count_and_not_an_amount() -> None:
    """Two `inputmode="numeric"` fields, and neither of them echoes.

    A year takes no grouping rule and no decimal separator, so `_int_or_none`
    still reads it and there is nothing to state back.
    """
    numeric = {
        path.name: path.read_text(encoding="utf-8").count('inputmode="numeric"')
        for path in _TX_PARTIALS.glob("*.html")
    }
    assert {name: count for name, count in numeric.items() if count} == {
        "_secondary_buy_composer.html": 1,
        "_commitment_composer.html": 1,
    }
    assert 'id="tx-read-md_vintage_year"' not in "".join(
        path.read_text(encoding="utf-8") for path in _TX_PARTIALS.glob("*.html")
    )
