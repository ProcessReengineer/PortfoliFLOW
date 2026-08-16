# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the AIServiceCore stop-token stripper.

Some non-default LLMs leak control tokens like ``<|eom|>`` and
``<|eot_id|>`` into their streaming output. The Phase-2 web variant
exposes the model picker, which makes the bug visible — the stripper
in :mod:`services.ai_service_core` runs at the core level so every
adapter (Qt, SSE, bot) inherits the fix without channel-specific
filtering. See sub-stream 2c, Task 4 (Option A).
"""

from __future__ import annotations

from services.ai_service_core import _StopTokenStripper


def test_stripper_passes_clean_text_through_unchanged() -> None:
    s = _StopTokenStripper()
    assert s.process("hello") == "hello"
    assert s.flush() == ""


def test_stripper_removes_complete_stop_token_in_one_chunk() -> None:
    s = _StopTokenStripper()
    assert s.process("hello<|eom|>world") == "helloworld"
    assert s.flush() == ""


def test_stripper_removes_stop_token_split_across_chunks() -> None:
    """The token may straddle the chunk boundary — the stripper must
    buffer the partial prefix and drop the whole token once the next
    chunk completes it.
    """
    s = _StopTokenStripper()
    out1 = s.process("hello<|")
    out2 = s.process("eom|> world")
    assert "<|eom|>" not in out1 + out2
    assert (out1 + out2).strip() == "hello world"


def test_stripper_releases_held_back_text_when_proven_benign() -> None:
    """If a buffered prefix turns out not to be a stop token, the
    stripper must release it on the next chunk so the user sees the
    real text.
    """
    s = _StopTokenStripper()
    out1 = s.process("hello<|")
    out2 = s.process("not-a-token")
    assert (out1 + out2) == "hello<|not-a-token"


def test_stripper_handles_multiple_distinct_tokens() -> None:
    s = _StopTokenStripper()
    text = "first<|eom|>middle<|eot_id|>last"
    assert s.process(text) == "firstmiddlelast"
    assert s.flush() == ""


def test_stripper_flush_returns_pending_tail_after_clean_match_failed() -> None:
    """``flush`` runs at end-of-stream and must release any buffered
    suffix that turned out to be a non-token.
    """
    s = _StopTokenStripper()
    emitted = s.process("trail<|")
    assert "<|" not in emitted
    flushed = s.flush()
    assert flushed == "<|"
