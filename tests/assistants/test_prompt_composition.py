# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Prompt composition split by persona (ADR-0133 §1.6, D-WD-22, D-WD-16).

Test obligation 6 of DC-WD-B. Shirley's composed prompt stays
byte-identical; Irene's finding prompt is the grounding block plus her
System Prompt block, her briefing prompt the grounding block plus the
block under ``## Briefing Prompt`` — neither carries Shirley's tool
inventory or context files.

The voice-call agent's prompt (ADR-0137 §9) is Shirley's System Prompt
block plus the block of ``Soul_Shirley_call.md`` — no grounding, no
inventory — and is pinned by hash, so an edit to either file is a
conscious change.

The exact-equality pins build the expected string from its parts in the
real ``prompts/`` files, so they survive edits to the Soul text but fail
on any change to the composition. The parts are extracted here by line,
independently of the loader's own parsing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import services.ai_service_core as core_mod
from services.ai_models import ConnectionStatus, Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore, _temporal_grounding_block
from services.tool_registry import get_tool_registry
from services.voice_call import load_call_prompt
from tests.fixtures.openrouter_responses import SIMPLE_TEXT_RESPONSE
from tests.fixtures.sse_helpers import encode_sse_events

_PROMPTS = Path(core_mod.__file__).resolve().parents[1] / "prompts"
_CONTEXT_FILES = ("Shirley_AnalysisResults_Context.md", "Shirley_ToolOrchestration_Context.md")
_INVENTORY_HEADING = "## Your currently available tools"

# One sentence unique to each Soul_Irene block (the opening line is shared).
_SYSTEM_ONLY = "### You suggest urgency; you do not set it"
_BRIEFING_ONLY = "You have exactly one tool: write_briefing."

_BASE_URL = "https://openrouter.ai/api/v1"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"


def _fences(text: str) -> list[str]:
    """Return every triple-backtick block of ``text``, by line, stripped."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current).strip())
                current = None
        elif current is not None:
            current.append(line)
    return blocks


def _section(text: str, heading: str) -> str:
    """Return the text of the H2 section ``heading``, up to the next H2."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def _soul(name: str) -> str:
    return (_PROMPTS / f"Soul_{name}.md").read_text(encoding="utf-8")


def _irene_system_block() -> str:
    return _fences(_section(_soul("Irene"), "## System Prompt"))[0]


def _irene_briefing_block() -> str:
    return _fences(_section(_soul("Irene"), "## Briefing Prompt"))[0]


def _grounded(body: str) -> str:
    return _temporal_grounding_block() + "\n\n" + body


# ---------------------------------------------------------------------------
# 1–3: exact composition, built from parts
# ---------------------------------------------------------------------------


def test_shirley_prompt_is_byte_identical_composition() -> None:
    """Shirley: grounding · Soul block · inventory · the two context files."""
    core = AIServiceCore()  # registers the default tools on first construction
    inventory = core._render_tool_inventory()
    assert inventory.startswith(_INVENTORY_HEADING)

    parts = [_fences(_soul("Shirley"))[0], inventory]
    parts += [(_PROMPTS / f).read_text(encoding="utf-8").strip() for f in _CONTEXT_FILES]
    expected = _grounded("\n\n".join(parts))

    assert core.get_system_prompt("shirley") == expected
    assert core.get_system_prompt() == expected


def test_call_prompt_is_soul_block_plus_addendum_block() -> None:
    """The call agent's prompt: Soul block · addendum block — no grounding, no inventory."""
    expected = _fences(_soul("Shirley"))[0] + "\n\n" + _fences(_soul("Shirley_call"))[0]
    composed = load_call_prompt()
    assert composed == expected
    assert _INVENTORY_HEADING not in composed
    assert _temporal_grounding_block() not in composed
    # Shirley's own composition is untouched by the addendum's existence.
    core = AIServiceCore()
    assert _fences(_soul("Shirley_call"))[0] not in core.get_system_prompt()


def test_call_prompt_hash_is_pinned() -> None:
    """An edit to either prompt file is a conscious change: update this hash with it."""
    digest = hashlib.sha256(load_call_prompt().encode("utf-8")).hexdigest()
    assert digest == "e2b2b7603ee228fec92b8b89c029b57026a51453573d2b9be998e834d23ff08b"


def test_irene_finding_prompt_is_grounding_plus_system_block() -> None:
    """Irene's finding prompt: grounding · the System Prompt block, nothing else."""
    prompt = AIServiceCore().get_system_prompt("irene")

    assert prompt == _grounded(_irene_system_block())
    assert "You are Irene, the background analyst" in prompt
    assert _SYSTEM_ONLY in prompt
    assert _BRIEFING_ONLY not in prompt


def test_irene_briefing_prompt_is_grounding_plus_briefing_block() -> None:
    """Irene's briefing prompt: grounding · the Briefing Prompt block, nothing else."""
    prompt = AIServiceCore().get_briefing_prompt()

    assert prompt == _grounded(_irene_briefing_block())
    assert "You are Irene, the background analyst" in prompt
    assert _BRIEFING_ONLY in prompt
    assert _SYSTEM_ONLY not in prompt


def test_briefing_block_found_by_heading_not_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoy fence inserted above the heading does not shift the briefing block."""
    soul = _soul("Irene")
    heading = "## Briefing Prompt"
    decoy = "## Decoy\n\n```\nDECOY_BLOCK\n```\n\n"
    (tmp_path / "Soul_Irene.md").write_text(
        soul.replace(heading, decoy + heading), encoding="utf-8"
    )
    monkeypatch.setattr(core_mod, "_PROMPT_DIR", tmp_path)

    prompt = AIServiceCore().get_briefing_prompt()

    assert prompt == _grounded(_irene_briefing_block())
    assert "DECOY_BLOCK" not in prompt


# ---------------------------------------------------------------------------
# 4: no Shirley material in either Irene prompt
# ---------------------------------------------------------------------------


def test_irene_prompts_carry_no_inventory_or_shirley_context() -> None:
    """Neither Irene prompt holds the inventory or any line of Shirley's context files."""
    core = AIServiceCore()
    assert get_tool_registry().get_tool_definitions(), "inventory must be non-empty here"
    context_lines = {
        line.strip()
        for f in _CONTEXT_FILES
        for line in (_PROMPTS / f).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    # Lines the context files share with Irene's own Soul text are not leaks.
    context_lines -= {line.strip() for line in _soul("Irene").splitlines()}

    for prompt in (core.get_system_prompt("irene"), core.get_briefing_prompt()):
        assert _INVENTORY_HEADING not in prompt
        leaked = [line for line in context_lines if line in prompt]
        assert not leaked, f"Shirley context text in an Irene prompt: {leaked[:3]}"


# ---------------------------------------------------------------------------
# 5: Irene's fallback names Irene and stays grounded
# ---------------------------------------------------------------------------


def test_irene_fallback_names_irene_when_soul_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing Soul_Irene.md yields Irene's grounded fallback on both accessors."""
    monkeypatch.setattr(core_mod, "_PROMPT_DIR", tmp_path)
    core = AIServiceCore()
    expected = _grounded(core_mod._IRENE_FALLBACK)

    for prompt in (core.get_system_prompt("irene"), core.get_briefing_prompt()):
        assert prompt == expected
        assert prompt.startswith(_temporal_grounding_block())
        assert "Irene" in prompt
        assert "Shirley" not in prompt


@pytest.mark.parametrize(
    "soul_text",
    [
        "# Soul_Irene.md\n\n## System Prompt\n\n```\nSYSTEM_BODY\n```\n",
        "# Soul_Irene.md\n\n## Briefing Prompt\n\nno fence here\n\n## Next\n\n```\nX\n```\n",
        "# Soul_Irene.md\n\n## Briefing Prompt\n\n```\nUNCLOSED\n",
        "# Soul_Irene.md\n\n## Briefing Prompt\n\n```\n   \n```\n",
    ],
    ids=["no-heading", "fence-outside-section", "unclosed-fence", "empty-fence"],
)
def test_briefing_prompt_falls_back_on_malformed_soul(
    soul_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing heading or a missing, unclosed or empty fence → Irene's fallback."""
    (tmp_path / "Soul_Irene.md").write_text(soul_text, encoding="utf-8")
    monkeypatch.setattr(core_mod, "_PROMPT_DIR", tmp_path)

    assert AIServiceCore().get_briefing_prompt() == _grounded(core_mod._IRENE_FALLBACK)


# ---------------------------------------------------------------------------
# 6: Shirley's chat call still offers her full registry
# ---------------------------------------------------------------------------


async def test_shirley_stream_offers_full_registry(httpx_mock: Any) -> None:
    """The streaming chat request's ``tools`` field is the whole registry."""
    core = AIServiceCore()
    core.configure(_BASE_URL, "sk-test-key")
    core.set_status(ConnectionStatus.CONNECTED)
    core.set_model("test/model")
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        content=encode_sse_events(SIMPLE_TEXT_RESPONSE),
        headers={"content-type": "text/event-stream"},
    )
    conv = Conversation()
    conv.add_message(Message(role=MessageRole.USER, content="hi"))
    registry = get_tool_registry()

    [e async for e in core.stream_response(conv, core.get_system_prompt())]

    body = json.loads(next(r for r in httpx_mock.get_requests() if r.method == "POST").content)
    offered = [t["function"]["name"] for t in body["tools"]]
    assert offered == [d["function"]["name"] for d in registry.get_tool_definitions()]
    assert len(offered) > 1
    assert body["messages"][0] == {"role": "system", "content": core.get_system_prompt()}
