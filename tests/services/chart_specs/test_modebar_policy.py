# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""P-GP-2 — Plotly modebar policy.

Interactive figures carry ``"displayModeBar": "hover"`` so the modebar
appears only while the pointer is over the figure; non-interactive
figures carry ``False``. A permanently visible bar (``True``) is not
used anywhere, and the spelling is the one literal form so a grep
finds every site. These tests read the sources, not the built specs,
because the policy is about every builder — including ones no spec
test exercises.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SOURCES = sorted(
    [
        *(_ROOT / "services" / "chart_specs").glob("*.py"),
        _ROOT / "services" / "tools" / "chart_tools.py",
    ]
)

# The quoted key form only — prose mentions (the package docstring
# says ``config.displayModeBar``) are not config literals.
_ANY = re.compile(r'"displayModeBar"')
_ALLOWED = re.compile(r'"displayModeBar": (?:"hover"|False),')


def _lines_with_key(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if _ANY.search(line)]


def test_no_builder_pins_the_modebar_on() -> None:
    offenders = [
        f"{path.relative_to(_ROOT)}: {line.strip()}"
        for path in _SOURCES
        for line in _lines_with_key(path)
        if "True" in line
    ]
    assert offenders == [], 'displayModeBar: True is not used — use "hover":\n' + "\n".join(
        offenders
    )


def test_every_modebar_literal_is_one_of_the_two_spellings() -> None:
    odd = [
        f"{path.relative_to(_ROOT)}: {line.strip()}"
        for path in _SOURCES
        for line in _lines_with_key(path)
        if not _ALLOWED.search(line)
    ]
    assert odd == [], "unexpected displayModeBar spelling:\n" + "\n".join(odd)


def test_the_hover_sites_are_the_interactive_builders() -> None:
    # 24 chart_specs builders + Shirley's render_chart. Update the number
    # together with a new interactive builder — that is the point.
    hover_files = [
        path
        for path in _SOURCES
        if any('"displayModeBar": "hover",' in line for line in _lines_with_key(path))
    ]
    assert len(hover_files) == 25, [p.name for p in hover_files]
