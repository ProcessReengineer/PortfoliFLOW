# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``write_briefing`` tool and its validator (ADR-0133 §1.3).

Pure: no database and no model. The schema is pinned as the model is
offered it, the forced choice as ``run_synthesis`` receives it, and
:func:`parse_write_briefing` against every shape the briefing step must
refuse — so a refused output is ``invalid_output`` and never a row. The
length guard pins the Soul and the tool to one word range per part.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import services.irene.briefing_step as briefing_step_module
from core.models.irene_briefing import BRIEFING_SKIP_REASONS, BRIEFING_STATUSES
from services.irene.briefing_tool import (
    WRITE_BRIEFING_TOOL,
    WRITE_BRIEFING_TOOL_CHOICE,
    WRITE_BRIEFING_TOOL_NAME,
    parse_write_briefing,
)


def _call(name: str = WRITE_BRIEFING_TOOL_NAME, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments, "id": "call_brief"}


def test_the_tool_offers_two_required_string_parameters() -> None:
    function = WRITE_BRIEFING_TOOL["function"]
    assert WRITE_BRIEFING_TOOL["type"] == "function"
    assert function["name"] == "write_briefing"
    parameters = function["parameters"]
    assert set(parameters["properties"]) == {"text_markets", "text_watch"}
    for prop in parameters["properties"].values():
        assert prop["type"] == "string"
    assert parameters["required"] == ["text_markets", "text_watch"]
    assert parameters["additionalProperties"] is False


def test_the_forced_choice_names_the_tool() -> None:
    assert WRITE_BRIEFING_TOOL_CHOICE == {
        "type": "function",
        "function": {"name": WRITE_BRIEFING_TOOL_NAME},
    }


def test_one_valid_call_yields_both_texts_stripped() -> None:
    texts = parse_write_briefing(
        [_call(text_markets="  Markets were quiet.\n", text_watch="\tAll calm. ", extra=1)]
    )
    assert texts == ("Markets were quiet.", "All calm.")


def test_an_empty_watch_text_is_valid() -> None:
    texts = parse_write_briefing([_call(text_markets="Markets were quiet.", text_watch="")])
    assert texts == ("Markets were quiet.", "")


def test_no_call_or_two_calls_are_invalid() -> None:
    valid = _call(text_markets="Markets were quiet.", text_watch="All calm.")
    assert parse_write_briefing([]) is None
    assert parse_write_briefing([valid, valid]) is None


def test_a_foreign_name_or_a_missing_or_non_string_argument_is_invalid() -> None:
    assert (
        parse_write_briefing(
            [_call("surface_finding", text_markets="Markets were quiet.", text_watch="")]
        )
        is None
    )
    # What run_synthesis yields for an argument string that is not JSON.
    assert parse_write_briefing([_call()]) is None
    assert parse_write_briefing([_call(text_markets="Markets were quiet.")]) is None
    assert parse_write_briefing([_call(text_markets=3, text_watch="All calm.")]) is None


def test_a_blank_markets_text_is_invalid_and_the_vocabulary_is_the_models() -> None:
    assert parse_write_briefing([_call(text_markets=" \n\t ", text_watch="All calm.")]) is None

    source = inspect.getsource(briefing_step_module)
    for status in ("written", "skipped"):
        assert f'"{status}"' in source
        assert status in BRIEFING_STATUSES
    for reason in ("llm_error", "invalid_output", "nothing_to_write"):
        assert f'"{reason}"' in source
        assert reason in BRIEFING_SKIP_REASONS


_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"

#: The briefing's length, per part (P-GP-5) — stated alike in the Soul and
#: in both tool descriptions, because the model reads both.
_LENGTH = "about 60 to 120 words"


def _briefing_block() -> str:
    soul = (_PROMPTS / "Soul_Irene.md").read_text(encoding="utf-8")
    return soul.split("## Briefing Prompt", 1)[1].split("```", 2)[1]


def test_the_briefing_length_is_one_word_range_in_prompt_and_tool() -> None:
    """The Soul and the tool name the same word range per part, and no sentence count."""
    properties = WRITE_BRIEFING_TOOL["function"]["parameters"]["properties"]
    for name in ("text_markets", "text_watch"):
        assert _LENGTH in properties[name]["description"]
        assert "sentences" not in properties[name]["description"]
    block = _briefing_block()
    assert block.count(_LENGTH) == 2
    assert "two to four sentences" not in block
    assert "short briefing" not in block
