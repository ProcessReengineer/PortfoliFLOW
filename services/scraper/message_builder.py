# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Build OpenAI-format messages for a Scraper extraction request.

Separated from the service so message shape can be tested independently,
and so the upcoming DD Support module can reuse it for text attachments
without refactoring.

Never calls ``Conversation.to_openai_messages`` — that method renders
non-image attachments as text placeholders, silently dropping PDFs. This
module builds content blocks directly in the shape required by the target
model's API format.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from services.scraper.models import Attachment, Keyword

logger = logging.getLogger(__name__)


def build_extraction_messages(
    system_prompt: str,
    user_instruction: str,
    keywords: list[Keyword],
    attachments: list[Attachment],
    model_format: str,
) -> list[dict[str, Any]]:
    """Build the messages list for a one-shot extraction.

    Layout of the user message (order matters for extraction quality — the
    instruction must come *before* the attachment so the model reads the
    keywords before the document):

    1. Instruction text (keyword list inlined).
    2. Attachments as content blocks, each in the shape required by
       ``model_format``.

    Args:
        system_prompt: The system prompt (loaded from ``docs/Scraper_Prompt.md``).
        user_instruction: Short instruction text; keyword list is appended.
        keywords: Keywords to include in the user instruction.
        attachments: Files (bytes or pre-extracted text) to attach.
        model_format: One of ``"openrouter_file"`` (Iteration 1).

    Returns:
        A list of two dicts: a system message and a user message.

    Raises:
        ValueError: If ``model_format`` is not recognised.
    """
    keyword_lines = "\n".join(f"- {k.name} ({k.type.value})" for k in keywords)
    instruction_text = f"{user_instruction}\n\nKeywords to extract:\n{keyword_lines}\n"

    user_content: list[dict[str, Any]] = [{"type": "text", "text": instruction_text}]
    for att in attachments:
        user_content.append(_build_attachment_block(att, model_format))

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _build_attachment_block(att: Attachment, model_format: str) -> dict[str, Any]:
    """Build a single content block for one attachment.

    Args:
        att: The attachment.
        model_format: The target format identifier.

    Returns:
        A content-block dict ready for inclusion in a user message.

    Raises:
        ValueError: If the combination of attachment data type and
            ``model_format`` is not supported.
    """
    # Text attachments: always inline as a text block, regardless of model format.
    # This is the path the DD Support module will use for pre-extracted XLSX data.
    if isinstance(att.data, str):
        return {
            "type": "text",
            "text": (
                f"--- Attached document: {att.filename} ---\n"
                f"{att.data}\n"
                f"--- End of {att.filename} ---"
            ),
        }

    # Binary attachments: format-specific shape
    if model_format == "openrouter_file":
        encoded = base64.b64encode(att.data).decode("ascii")
        return {
            "type": "file",
            "file": {
                "filename": att.filename,
                "file_data": f"data:{att.mime_type};base64,{encoded}",
            },
        }

    raise ValueError(
        f"Unknown model_format '{model_format}'. "
        f"Supported formats for binary attachments: 'openrouter_file'."
    )
