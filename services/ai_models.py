# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data models for the AIService layer.

These models are intentionally decoupled from PyQt6 and from the OpenAI SDK.
They represent the application's own view of conversations, messages, and
attachments. Serialisation to/from OpenAI wire format is the AIService's
responsibility, not theirs.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ConnectionStatus(Enum):
    """Connection state of the AIService."""

    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    ERROR = "Error"


class MessageRole(Enum):
    """Role of a message in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Attachment:
    """A file attached to a message.

    Attributes:
        filename: Original filename.
        mime_type: MIME type string (e.g. ``"application/pdf"``).
        data: Raw bytes of the file content.
    """

    filename: str
    mime_type: str
    data: bytes


@dataclass
class ToolCall:
    """A tool/function call requested by the model.

    Attributes:
        id: Unique identifier for the tool call.
        name: Name of the tool/function.
        arguments: JSON-serialisable dict of arguments.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """The result of executing a tool call.

    Attributes:
        tool_call_id: The ID of the tool call this result corresponds to.
        name: The tool name that was called.
        result: The string result returned by the tool function.
    """

    tool_call_id: str
    name: str
    result: str


@dataclass
class Message:
    """A single message in a conversation.

    Attributes:
        id: Unique message identifier (auto-generated UUID).
        role: Who sent this message (system, user, assistant, tool).
        content: Text content of the message.
        timestamp: When the message was created.
        attachments: List of file attachments (user messages only).
        tool_calls: List of tool calls (assistant messages only).
        tool_call_id: The id of the tool call this message answers
            (tool-role messages only). Required by OpenAI's protocol
            so a ``tool`` message can be paired with the originating
            ``assistant``-with-``tool_calls`` entry.
        model: Model identifier that generated this message (assistant only).
    """

    role: MessageRole
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: datetime = field(default_factory=datetime.now)
    attachments: list[Attachment] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    model: str | None = None


@dataclass
class Conversation:
    """An ordered sequence of messages forming a dialogue.

    Attributes:
        id: Unique conversation identifier (auto-generated UUID).
        title: Human-readable title (auto-generated from first user message).
        messages: Ordered list of messages.
        created_at: When the conversation was started.
        updated_at: When the last message was added.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = "New Conversation"
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def add_message(self, message: Message) -> None:
        """Append a message and update the timestamp.

        If this is the first user message, auto-generate a title from it.

        Args:
            message: The message to append.
        """
        self.messages.append(message)
        self.updated_at = datetime.now()
        if message.role == MessageRole.USER and self.title == "New Conversation":
            self.title = message.content[:60].strip()

    def to_openai_messages(self) -> list[dict[str, Any]]:
        """Convert to OpenAI API message format.

        Returns:
            List of dicts with the keys required by the OpenAI chat
            completions endpoint. ``assistant`` messages carrying tool
            calls add a ``tool_calls`` key; ``tool`` messages add a
            ``tool_call_id`` key. Both shapes are mandatory for the
            multi-turn replay to round-trip cleanly (ADR-0050).
        """
        result: list[dict[str, Any]] = []
        for msg in self.messages:
            entry: dict[str, Any] = {
                "role": msg.role.value,
                "content": msg.content,
            }
            # Include attachments as multimodal content blocks for user messages
            if msg.attachments and msg.role == MessageRole.USER:
                content_parts: list[dict[str, Any]] = []
                if msg.content:
                    content_parts.append({"type": "text", "text": msg.content})
                for att in msg.attachments:
                    if att.mime_type.startswith("image/"):
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        f"data:{att.mime_type};base64,"
                                        f"{base64.b64encode(att.data).decode()}"
                                    )
                                },
                            }
                        )
                    else:
                        # Non-image files: include as text description.
                        # Full multimodal file support depends on model capabilities.
                        content_parts.append(
                            {
                                "type": "text",
                                "text": (
                                    f"[Attached file: {att.filename} "
                                    f"({att.mime_type}, {len(att.data)} bytes)]"
                                ),
                            }
                        )
                entry["content"] = content_parts
            # Assistant messages whose role was to dispatch tool calls
            # need the ``tool_calls`` key; ``content`` may legitimately
            # be empty in that case.
            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (
                                json.dumps(tc.arguments)
                                if not isinstance(tc.arguments, str)
                                else tc.arguments
                            ),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            # Tool-role messages carry the originating call id so the
            # API can pair them with the assistant entry.
            if msg.role == MessageRole.TOOL and msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            result.append(entry)
        return result
