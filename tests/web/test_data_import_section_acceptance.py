# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Acceptance regression guards for the Data Import section.

Complements ``test_data_import_section_routes.py`` (HTTP-level
functional coverage) with structural checks that do not need a live
database:

1. None of the section templates / JS contain forbidden German
   strings (ADR-0008 regression guard).
2. The ``--pf-stepper-*`` tokens are **absent** from the generated
   ``web/static/css/theme.css`` — they were retired with the
   single-button refactor (sub-stream 6F).
3. The section template includes the upload-form fragment and the
   ``Upload and Import`` button by default.
4. The sunset templates and assets are gone (including the renamed
   stage1 / stage2 partials).
5. The preview fragment carries the new button vocabulary
   (``Upload and Import`` is on the upload form, ``Apply to
   Investments`` and ``Discard Preview`` on the preview).
6. The legacy ``Import anyway`` confirm-button text never returns.

The DB-backed flow assertions live alongside the route tests; this
file is a static-text guard that runs in milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_DATA_IMPORT_TEXT_SURFACES: tuple[Path, ...] = (
    _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_section.html",
    _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_upload_form.html",
    _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_preview.html",
    _REPO_ROOT / "web" / "static" / "js" / "data_import_section.js",
)

# Verbatim strings from the (sunset) German-language UI. Listed here
# rather than scanned heuristically because the surface is small and
# the exact strings are the regression contract.
_GERMAN_STRINGS_FORBIDDEN: tuple[str, ...] = (
    "In Investments importieren",
    "Trotzdem importieren",
    "Hochladen",
    "Importieren",
)


@pytest.mark.parametrize("path", _DATA_IMPORT_TEXT_SURFACES)
def test_no_german_strings_in_data_import_surface(path: Path) -> None:
    """ADR-0008 regression guard for the Data Import surface."""
    assert path.exists(), f"{path} is missing — Data Import surface incomplete."
    content = path.read_text(encoding="utf-8")
    for forbidden in _GERMAN_STRINGS_FORBIDDEN:
        assert forbidden not in content, (
            f"{path.relative_to(_REPO_ROOT)} contains forbidden German "
            f"string {forbidden!r}. ADR-0008 violation."
        )


def test_stepper_theme_tokens_removed_from_generated_css() -> None:
    """The ``--pf-stepper-*`` tokens are gone from ``theme.css``.

    The stepper was retired with the single-button Data Import
    refactor (sub-stream 6F); the corresponding ``pf.stepper`` block
    was removed from ``config/ui_theme*.json`` and the regenerated
    ``theme.css`` must not carry the tokens any more. This guard
    fails loudly if the JSON sources re-introduce them.
    """
    css_path = _REPO_ROOT / "web" / "static" / "css" / "theme.css"
    assert css_path.exists(), (
        "web/static/css/theme.css is missing. Run `python -m scripts.generate_theme_artifacts`."
    )
    css = css_path.read_text(encoding="utf-8")
    for token in (
        "--pf-stepper-track",
        "--pf-stepper-active",
        "--pf-stepper-completed",
    ):
        assert token not in css, (
            f"{token} reappeared in theme.css. The stepper palette "
            "was retired with the single-button refactor; restore "
            "the deletion in config/ui_theme*.json and regenerate "
            "with `python -m scripts.generate_theme_artifacts`."
        )


def test_section_template_includes_upload_form_by_default() -> None:
    """The section template starts the panel with the upload form."""
    template = _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_section.html"
    content = template.read_text(encoding="utf-8")
    assert "data_import_upload_form.html" in content
    assert "Upload and Import" not in content, (
        "The button label belongs to the included upload-form fragment, not the section wrapper."
    )
    upload_form = _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_upload_form.html"
    assert "Upload and Import" in upload_form.read_text(encoding="utf-8")


def test_old_data_import_assets_are_gone() -> None:
    """Sunset templates and assets are deleted, not just unreferenced.

    Covers both the original 6F-5 sunset (standalone pages + their JS
    / CSS) and the 6F rename of the stage1 / stage2 partials into the
    upload-form / preview partials.
    """
    for relpath in (
        "web/templates/data_import.html",
        "web/templates/data_import_detail.html",
        "web/static/js/data_import_detail.js",
        "web/static/css/components/data_import.css",
        "web/templates/_partials/data_import_stage1.html",
        "web/templates/_partials/data_import_stage2.html",
    ):
        path = _REPO_ROOT / relpath
        assert not path.exists(), f"{relpath} still on disk; sunset/rename incomplete."


def test_preview_panel_uses_new_button_labels() -> None:
    """The preview fragment uses the single-button vocabulary.

    The pre-preview trigger button (``Import to Investments``) was
    deleted — the dry-run already ran server-side — so the label
    must not appear here. ``Apply to Investments`` (the destructive
    confirm) and ``Discard Preview`` (the cancel link) are required.
    """
    template = _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_preview.html"
    content = template.read_text(encoding="utf-8")
    assert "Apply to Investments" in content
    assert "Discard Preview" in content
    assert "Import anyway" not in content
    # The dry-run trigger button is gone; this label must not appear.
    assert "Import to Investments" not in content


def test_preview_panel_does_not_display_format_field() -> None:
    """The preview metadata block does not render ``format_version``.

    The field stays in the API responses and the ``data_uploads``
    column; only the visual row in the ``<dl>`` is gone.
    """
    template = _REPO_ROOT / "web" / "templates" / "_partials" / "data_import_preview.html"
    content = template.read_text(encoding="utf-8")
    assert "<dt>Format</dt>" not in content
    assert "Format:" not in content
    assert "format_version" not in content


def test_no_legacy_button_strings_in_data_import_surface() -> None:
    """Regression guard: ``Import anyway`` must not reappear.

    Parallel to ``test_no_german_strings_in_data_import_surface``: a
    file-grep that catches accidental reintroduction during future
    refactors.
    """
    for path in _DATA_IMPORT_TEXT_SURFACES:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        assert "Import anyway" not in content, (
            f"{path.relative_to(_REPO_ROOT)} still contains legacy button text 'Import anyway'."
        )
