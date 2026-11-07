# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Proposals — the pure half of the dock's two one-shots (ADR-0138 §8, P-CS-5c).

No database and no model: ``services/cases/proposals.py`` builds the forced
tools and the context of each call and parses the tool calls a
``run_synthesis`` result carries as plain dicts.

Coverage
--------
* PR-01: both tools are OpenAI function-tool dicts; the seven kinds enumerated.
* PR-02: a well-formed call normalises to pending items with their fields, a
  thread title resolved to the live thread's id; the row's words.
* PR-03: a malformed item is dropped with one reason; the rest survive.
* PR-04: past twelve items, the first twelve.
* PR-05: ``conversation_text`` — speakers, no tool turn, oldest cut first,
  never past ``max_chars``.
* PR-06: ``draft_from_tool_calls`` — the four fields, the framing's default,
  no first thread.
* PR-07: the module's source carries none of the package guard's write tokens.
"""

from __future__ import annotations

import inspect
import json
from typing import Any
from uuid import uuid4

import services.cases.proposals as proposals_module
from services.ai_models import Message, MessageRole, ToolCall
from services.cases.proposals import (
    CASE_DRAFT_SYSTEM_PROMPT,
    CASE_DRAFT_TOOL,
    KINDS,
    MAIN_FIELDS,
    MAX_ITEMS,
    PROPOSAL_SYSTEM_PROMPT,
    PROPOSAL_TOOL,
    conversation_text,
    draft_from_tool_calls,
    draft_messages,
    item_headline,
    item_target,
    items_from_tool_calls,
    proposal_messages,
)
from tests.regression.test_cases_stand_reads_only import _WRITES

_SEVEN = [
    "thread",
    "thread_conclusion",
    "figure",
    "point",
    "conversation",
    "generated_document",
    "prose_append",
]


def _call(items: Any, name: str = "propose_for_file") -> dict[str, Any]:
    """One parsed tool call as ``run_synthesis`` hands it back."""
    return {"name": name, "arguments": {"items": items}, "id": "call-1"}


def _validates_as_json(value: Any) -> None:
    json.loads(json.dumps(value))


def test_pr01_both_tools_are_function_tools_with_the_seven_kinds() -> None:
    """PR-01: the request shape ``run_synthesis(tool=…)`` sends, serialisable as JSON."""
    for tool, name, required in (
        (PROPOSAL_TOOL, "propose_for_file", ["items"]),
        (CASE_DRAFT_TOOL, "draft_case", ["title", "framing", "question"]),
    ):
        plain = dict(tool)
        _validates_as_json(plain)
        assert plain["type"] == "function"
        function = plain["function"]
        assert function["name"] == name
        assert function["description"]
        assert function["parameters"]["type"] == "object"
        assert function["parameters"]["required"] == required

    items = PROPOSAL_TOOL["function"]["parameters"]["properties"]["items"]
    assert items["type"] == "array"
    assert items["maxItems"] == MAX_ITEMS == 12
    item = items["items"]
    assert item["required"] == ["kind", "why"]
    assert item["properties"]["kind"]["enum"] == _SEVEN == list(KINDS)
    for field in set(MAIN_FIELDS.values()):
        assert field in item["properties"], field
    framing = CASE_DRAFT_TOOL["function"]["parameters"]["properties"]["framing"]
    assert framing["enum"] == ["question", "task"]
    # The instructions name the rules the build was given (D-CS-B-121).
    assert "never invent a figure" in PROPOSAL_SYSTEM_PROMPT.lower()
    assert f"at most {MAX_ITEMS} items" in PROPOSAL_SYSTEM_PROMPT
    assert "conversation's language" in PROPOSAL_SYSTEM_PROMPT
    assert "conversation's language" in CASE_DRAFT_SYSTEM_PROMPT


def test_pr02_a_well_formed_call_normalises_to_pending_items() -> None:
    """PR-02: every kind's fields, the thread id resolved, the headline and the target."""
    fees, liquidity = uuid4(), uuid4()
    live = {"Fees": fees, "Liquidity terms": liquidity}
    raw = [
        {"kind": "thread", "title": "  Exit\n route ", "prose": "Who buys?", "why": "PM, turn 2"},
        {
            "kind": "thread_conclusion",
            "thread": "fees",
            "conclusion": "They step up in 2027.",
            "status": "confirmed",
            "why": "Shirley, turn 3",
        },
        {"kind": "figure", "label": "Fee", "value": "1.5 %", "note": "the CFO", "why": "t4"},
        {"kind": "point", "text": "Ask the GP", "due_on": "2026-12-01", "why": "t5"},
        {
            "kind": "conversation",
            "held_on": "2026-11-03",
            "with_whom": "Anna Weber (GP)",
            "said": "The gate lifts in 2027.",
            "channel": "meeting",
            "why": "t6",
        },
        {
            "kind": "generated_document",
            "title": "Memo",
            "body": "One.\r\n\r\nTwo.",
            "sources": [{"label": "The case", "href": "/cases/x"}, {"href": "/investments/y"}],
            "why": "t7",
        },
        {"kind": "prose_append", "thread": "Liquidity terms", "text": "Gated.", "why": "t8"},
    ]

    items, reasons = items_from_tool_calls([_call(raw)], live_threads=live)

    assert reasons == []
    assert [item["kind"] for item in items] == _SEVEN
    assert all(item["status"] == "pending" for item in items)
    thread, conclusion, figure, point, conversation, document, append = items
    assert thread == {
        "kind": "thread",
        "status": "pending",
        "fields": {"title": "Exit route", "prose": "Who buys?"},
        "why": "PM, turn 2",
    }
    assert conclusion["fields"] == {
        "thread": "Fees",
        "thread_id": str(fees),
        "conclusion": "They step up in 2027.",
        "status": "confirmed",
    }
    assert figure["fields"] == {"label": "Fee", "value": "1.5 %", "note": "the CFO"}
    assert point["fields"] == {"text": "Ask the GP", "due_on": "2026-12-01"}
    assert conversation["fields"] == {
        "with_whom": "Anna Weber (GP)",
        "said": "The gate lifts in 2027.",
        "held_on": "2026-11-03",
        "channel": "meeting",
    }
    assert document["fields"] == {
        "title": "Memo",
        "body": "One.\n\nTwo.",
        "sources": [
            {"label": "The case", "href": "/cases/x"},
            {"label": "/investments/y", "href": "/investments/y"},
        ],
    }
    assert append["fields"] == {
        "thread": "Liquidity terms",
        "thread_id": str(liquidity),
        "text": "Gated.",
    }
    for item in items:
        assert MAIN_FIELDS[item["kind"]] in item["fields"]
    _validates_as_json(items)  # the shape case_proposals.items stores

    assert [item_headline(item) for item in items] == [
        "New thread",
        'Conclusion for "Fees" · confirmed',
        "Figure · Fee",
        "Open point · due 2026-12-01",
        "Conversation with Anna Weber (GP) · 2026-11-03",
        "Document · Memo",
        'Text for "Liquidity terms"',
    ]
    assert [item_target(item) for item in items] == [
        "Threads",
        'thread "Fees"',
        "Figures",
        "Open points",
        "Conversations",
        "generated document",
        'thread "Liquidity terms"',
    ]


def test_pr03_a_malformed_item_is_dropped_with_one_reason() -> None:
    """PR-03: an unknown kind, no text, a figure without a value, no live thread."""
    live = {"Fees": uuid4()}
    raw = [
        {"kind": "decision", "text": "We hold.", "why": "t1"},
        {"kind": "point", "why": "t2"},
        {"kind": "figure", "label": "Fee", "why": "t3"},
        {"kind": "thread_conclusion", "thread": "Exit route", "conclusion": "No.", "why": "t4"},
        "not an object",
        {"kind": "point", "text": "Ask the GP", "due_on": "03.12.2026", "why": "t6"},
        {"kind": "thread", "title": "Exit route", "why": "t7"},
    ]

    items, reasons = items_from_tool_calls([_call(raw)], live_threads=live)

    assert reasons == [
        "item 1: unknown kind 'decision'",
        "item 2: a point without a text",
        "item 3: a figure without a value",
        "item 4: a thread_conclusion naming no live thread",
        "item 5: not an object",
    ]
    assert [item["kind"] for item in items] == ["point", "thread"]
    # A date that is not YYYY-MM-DD is dropped from the item, not the item.
    assert items[0]["fields"] == {"text": "Ask the GP"}

    # No call at all, and a call whose items are no list: one reason each.
    assert items_from_tool_calls([], live_threads=live) == ([], ["no propose_for_file call"])
    assert items_from_tool_calls([_call("three")], live_threads=live) == (
        [],
        ["a propose_for_file call without an items list"],
    )
    assert items_from_tool_calls([_call([], name="speak")], live_threads=live)[0] == []


def test_pr04_past_twelve_items_the_first_twelve() -> None:
    """PR-04: the package is capped at twelve; the rest leave a reason each."""
    raw = [{"kind": "point", "text": f"Point {n}", "why": "t"} for n in range(1, 16)]

    items, reasons = items_from_tool_calls([_call(raw)], live_threads={})

    assert [item["fields"]["text"] for item in items] == [f"Point {n}" for n in range(1, 13)]
    assert reasons == [f"item {n}: past the first 12 items" for n in range(13, 16)]


def test_pr05_conversation_text_labels_drops_and_cuts_the_oldest_first() -> None:
    """PR-05: PM and Shirley turns only; the oldest go first; never past the bound."""
    messages = [
        Message(role=MessageRole.USER, content="What does the GP say about the gate?"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="get_case", arguments={})],
        ),
        Message(role=MessageRole.TOOL, content="CASE-0001 · raw tool output", tool_call_id="c1"),
        Message(role=MessageRole.ASSISTANT, content="It lifts in 2027."),
        Message(role=MessageRole.SYSTEM, content="never shown"),
        Message(role=MessageRole.USER, content="  Note that.  "),
    ]

    full = conversation_text(messages)
    assert full == (
        "PM: What does the GP say about the gate?\n\nShirley: It lifts in 2027.\n\nPM: Note that."
    )
    assert "tool output" not in full and "never shown" not in full

    newest_two = "Shirley: It lifts in 2027.\n\nPM: Note that."
    assert conversation_text(messages, max_chars=len(newest_two)) == newest_two
    assert conversation_text(messages, max_chars=len(newest_two) + 5) == newest_two
    clipped = conversation_text(messages, max_chars=8)
    assert clipped == "…e that." and len(clipped) == 8
    for bound in range(0, len(full) + 3):
        assert len(conversation_text(messages, max_chars=bound)) <= bound
    assert conversation_text([]) == ""

    (message,) = proposal_messages("CASE-0001 · Fees", newest_two)
    assert message["role"] == "user"
    assert "CASE-0001 · Fees" in message["content"] and newest_two in message["content"]
    assert message["content"].index("CASE-0001") < message["content"].index("Shirley:")
    (draft,) = draft_messages(newest_two)
    assert draft["role"] == "user" and newest_two in draft["content"]


def test_pr06_the_draft_reads_four_fields() -> None:
    """PR-06: title, framing (``question`` by default), the question, the first thread."""
    call = {
        "name": "draft_case",
        "arguments": {
            "title": "  GP X\n AGM ",
            "framing": "task",
            "question": "Walk in knowing each fund's last two months.",
            "first_thread": {"title": "Fund III's hedge", "prose": "Below the floor?"},
        },
        "id": "c1",
    }
    assert draft_from_tool_calls([call]) == {
        "title": "GP X AGM",
        "framing": "task",
        "question": "Walk in knowing each fund's last two months.",
        "first_thread": {"title": "Fund III's hedge", "prose": "Below the floor?"},
    }

    bare = {"name": "draft_case", "arguments": {"title": "Fees", "question": "Do they rise?"}}
    assert draft_from_tool_calls([bare]) == {
        "title": "Fees",
        "framing": "question",
        "question": "Do they rise?",
        "first_thread": None,
    }
    odd = {"name": "draft_case", "arguments": {"title": "Fees", "framing": "memo"}}
    assert draft_from_tool_calls([odd]) == {
        "title": "Fees",
        "framing": "question",
        "question": "",
        "first_thread": None,
    }
    untitled = {"name": "draft_case", "arguments": {"title": " ", "question": "Why?"}}
    assert draft_from_tool_calls([untitled]) is None
    assert draft_from_tool_calls([{"name": "speak", "arguments": {"text": "x"}}]) is None
    assert draft_from_tool_calls([]) is None


def test_pr07_the_module_carries_no_write_token() -> None:
    """PR-07: the package guard walks this module with the readers — prompts included.

    ``tests/regression/test_cases_stand_reads_only.py`` holds it to the
    no-writes rule; this test states the constraint where the module's prompt
    texts live, so a prompt that says ``update(`` fails here first.
    """
    source = inspect.getsource(proposals_module)
    assert [token for token in _WRITES if token in source] == []
    for text in (PROPOSAL_SYSTEM_PROMPT, CASE_DRAFT_SYSTEM_PROMPT, json.dumps(dict(PROPOSAL_TOOL))):
        assert [token for token in _WRITES if token in text] == []
