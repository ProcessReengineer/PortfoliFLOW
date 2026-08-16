# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Smoke tests for ``scripts/generate_theme_artifacts.py``.

The script is the single seam through which JSON design tokens reach
the browser. Two invariants matter and are exercised here:

1. The expected ``--``-prefixed custom properties land in the output.
2. Regenerating with the same inputs produces byte-identical bytes —
   without that, the pre-commit hook would churn unrelated commits.

The tests run the script in a temp directory with a hand-rolled
fixture JSON, so they are independent of whatever the live
``config/ui_theme*.json`` happens to contain at the moment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.generate_theme_artifacts import main, render_css


@pytest.fixture
def fixture_config(tmp_path: Path) -> Path:
    """Build a minimal ``config/`` directory with the expected files.

    Returns:
        Path to the temp config directory.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    default_ui = {
        "_comment": "fixture",
        "_version": "1.0.0",
        "background": {"primary": "#000000", "secondary": "#101010"},
        "accent": {"primary": "#FF0000"},
        "font": {"family": "Inter, Arial, sans-serif", "size_base": 10},
    }
    (config_dir / "ui_theme.json").write_text(json.dumps(default_ui, indent=2), encoding="utf-8")

    light_variant = {
        "_display_name": "Light",
        "background": {"primary": "#FFFFFF", "secondary": "#EEEEEE"},
        "accent": {"primary": "#0066CC"},
        "font": {"family": "Inter, Arial, sans-serif", "size_base": 10},
    }
    (config_dir / "ui_theme_light.json").write_text(
        json.dumps(light_variant, indent=2), encoding="utf-8"
    )

    chart = {
        "_version": "1.0.0",
        "colours": {"background": "#1E1E1E", "primary": "#FF0000"},
        "line": {"width_primary": 1.5},
    }
    (config_dir / "chart_theme.json").write_text(json.dumps(chart, indent=2), encoding="utf-8")

    return config_dir


def test_generated_css_contains_expected_custom_properties(
    fixture_config: Path,
) -> None:
    """The output must declare the ``--``-prefixed properties for every
    non-metadata token in the source JSON, on the ``:root`` selector.
    """
    css = render_css(fixture_config)

    assert ":root {" in css
    # UI default theme tokens
    assert "--ui-background-primary: #000000;" in css
    assert "--ui-accent-primary: #FF0000;" in css
    assert "--ui-font-family: Inter, Arial, sans-serif;" in css
    # Chart theme tokens
    assert "--chart-colours-background: #1E1E1E;" in css
    assert "--chart-line-width-primary: 1.5;" in css
    # Variant scope
    assert ':root[data-theme="light"] {' in css
    assert "--ui-background-primary: #FFFFFF;" in css


def test_metadata_keys_are_excluded(fixture_config: Path) -> None:
    """Underscore-prefixed JSON keys (``_comment``, ``_display_name``)
    must not leak into the generated CSS — they are documentation, not
    tokens.
    """
    css = render_css(fixture_config)
    assert "--ui--comment" not in css
    assert "--ui--display-name" not in css
    assert "--ui--version" not in css


def test_regeneration_is_byte_identical(fixture_config: Path, tmp_path: Path) -> None:
    """Two consecutive runs against unchanged inputs must produce the
    same bytes — otherwise the pre-commit hook would churn diffs.
    """
    output = tmp_path / "out" / "theme.css"

    rc1 = main(["--config-dir", str(fixture_config), "--output", str(output)])
    assert rc1 == 0
    first = output.read_bytes()

    rc2 = main(["--config-dir", str(fixture_config), "--output", str(output)])
    assert rc2 == 0
    second = output.read_bytes()

    assert first == second


def test_check_mode_passes_when_artefact_is_fresh(fixture_config: Path, tmp_path: Path) -> None:
    """``--check`` must exit 0 immediately after a fresh write."""
    output = tmp_path / "out" / "theme.css"
    assert main(["--config-dir", str(fixture_config), "--output", str(output)]) == 0
    assert (
        main(
            [
                "--config-dir",
                str(fixture_config),
                "--output",
                str(output),
                "--check",
            ]
        )
        == 0
    )


def test_check_mode_fails_when_artefact_is_stale(fixture_config: Path, tmp_path: Path) -> None:
    """``--check`` must exit non-zero when the on-disk file does not
    match the regenerated content.
    """
    output = tmp_path / "out" / "theme.css"
    output.parent.mkdir()
    output.write_text("/* stale */\n", encoding="utf-8")

    assert (
        main(
            [
                "--config-dir",
                str(fixture_config),
                "--output",
                str(output),
                "--check",
            ]
        )
        == 1
    )


def test_array_values_are_emitted_as_joined_and_indexed(
    fixture_config: Path,
) -> None:
    """Arrays in source JSON should expose both a comma-joined value
    (for direct CSS use) and per-index variants (for palette readers).
    """
    chart_json = json.loads((fixture_config / "chart_theme.json").read_text(encoding="utf-8"))
    chart_json["colours"]["series_palette"] = ["#111", "#222", "#333"]
    (fixture_config / "chart_theme.json").write_text(
        json.dumps(chart_json, indent=2), encoding="utf-8"
    )

    css = render_css(fixture_config)
    assert "--chart-colours-series-palette: #111, #222, #333;" in css
    assert "--chart-colours-series-palette-0: #111;" in css
    assert "--chart-colours-series-palette-2: #333;" in css
