# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``Message.channel`` marks what a voice call appended (ADR-0137 §5).

The attribute defaults to ``None`` and never reaches the model:
``Conversation.to_openai_messages`` does not serialise it.
"""

from __future__ import annotations

from services.ai_models import Conversation, Message, MessageRole


def test_channel_defaults_to_none_and_carries_call() -> None:
    """A message has no channel unless one is given."""
    assert Message(role=MessageRole.ASSISTANT, content="x").channel is None
    assert Message(role=MessageRole.ASSISTANT, content="x", channel="call").channel == "call"


def test_channel_is_not_serialised_to_the_model() -> None:
    """A call message serialises exactly like a typed one, without a channel key."""
    conv = Conversation()
    conv.add_message(Message(role=MessageRole.ASSISTANT, content="spoken", channel="call"))
    conv.add_message(Message(role=MessageRole.ASSISTANT, content="typed"))

    call_entry, typed_entry = conv.to_openai_messages()

    assert "channel" not in call_entry
    assert "channel" not in typed_entry
    assert {**call_entry, "content": None} == {**typed_entry, "content": None}
