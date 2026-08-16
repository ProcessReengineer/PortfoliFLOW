# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Verify the Fetcher-LLM system prompt loads from its fence block."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.web_research.service import load_fetcher_prompt


def test_prompt_loads_from_default_path() -> None:
    prompt = load_fetcher_prompt()
    assert prompt
    # Spot-check that the content-extraction intent is present.
    assert "content extraction service" in prompt.lower()
    assert "json" in prompt.lower()
    # Injection-handling clause must be present.
    assert "injection" in prompt.lower()


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_fetcher_prompt(tmp_path / "nope.md")


def test_no_fence_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("no fences here at all\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No ``` fence"):
        load_fetcher_prompt(bad)


def test_unclosed_fence_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("```\nonly opening fence\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unclosed"):
        load_fetcher_prompt(bad)


def test_empty_fence_block_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("```\n\n```\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Empty fence"):
        load_fetcher_prompt(bad)
