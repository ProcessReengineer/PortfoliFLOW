# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``web/`` reads no environment variable itself.

The web surface takes its configuration from :class:`web.settings.WebSettings`,
built once by the app factory and read through ``request.app.state.settings``,
or from the services it calls. A direct ``os.getenv`` or ``os.environ`` read
in ``web/`` bypasses that one typed, validated surface: the voice provider
keys therefore have their one reader in ``services/voice/config.py``, and the
upload size limit and the build SHA are ``WebSettings`` fields.

The scan walks the syntax tree, so a docstring or comment that *mentions* the
environment does not trip it. ``web/settings.py`` reads the environment
through ``pydantic-settings`` and needs no exemption.

If this guard goes red, add a ``WebSettings`` field (or resolve the value in
the service layer) instead of reading the environment in ``web/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_WEB_ROOT: Path = _REPO_ROOT / "web"
_FORBIDDEN_OS_NAMES: frozenset[str] = frozenset(
    {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
)


def _environment_reads(source: str, label: str) -> list[str]:
    """Return one ``label:line: form`` entry per environment access in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in _FORBIDDEN_OS_NAMES
        ):
            found.append(f"{label}:{node.lineno}: os.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            found.extend(
                f"{label}:{node.lineno}: from os import {alias.name}"
                for alias in node.names
                if alias.name in _FORBIDDEN_OS_NAMES
            )
    return found


def test_web_reads_no_environment_variable() -> None:
    """No ``.py`` file under ``web/`` touches ``os.environ`` or ``os.getenv``."""
    assert _WEB_ROOT.is_dir(), f"expected directory missing: {_WEB_ROOT}"
    offenders = [
        hit
        for path in sorted(_WEB_ROOT.rglob("*.py"))
        for hit in _environment_reads(
            path.read_text(encoding="utf-8"), str(path.relative_to(_REPO_ROOT))
        )
    ]
    assert not offenders, (
        "web/ takes its configuration from WebSettings, not the environment. "
        f"Offending reads: {offenders}"
    )


def test_scan_detects_each_forbidden_form() -> None:
    """The scan sees every form it forbids and ignores prose, so green means clean."""
    source = (
        '"""Mentions os.getenv and os.environ in prose only."""\n'
        "import os\n"
        "from os import environ\n"
        '# os.getenv("IN_A_COMMENT")\n'
        'a = os.getenv("A")\n'
        'b = os.environ["B"]\n'
        'c = os.environ.get("C")\n'
        'd = os.path.join("x", "y")\n'
    )
    assert sorted(_environment_reads(source, "sample.py")) == [
        "sample.py:3: from os import environ",
        "sample.py:5: os.getenv",
        "sample.py:6: os.environ",
        "sample.py:7: os.environ",
    ]
