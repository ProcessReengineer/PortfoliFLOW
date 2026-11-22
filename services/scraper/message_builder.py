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
from collections.abc import Sequence
from typing import Any

from services.scraper.models import Attachment, Keyword
from services.scraper.profiles import Profile, ProfileField

logger = logging.getLogger(__name__)

_CLASSIFY_LEAD = "Document profiles (classify the document as exactly one of them):"
_PRESELECTED_LEAD = "Document profile (preselected: read the document as this type):"


def build_extraction_messages(
    system_prompt: str,
    user_instruction: str,
    keywords: list[Keyword],
    attachments: list[Attachment],
    model_format: str,
    *,
    profiles: Sequence[Profile] = (),
    preselected: bool = False,
) -> list[dict[str, Any]]:
    """Build the messages list for a one-shot extraction.

    Layout of the user message (order matters for extraction quality — the
    instruction must come *before* the attachment so the model reads the
    profiles and keywords before the document):

    1. Instruction text (profiles and keyword list inlined).
    2. Attachments as content blocks, each in the shape required by
       ``model_format``.

    Args:
        system_prompt: The system prompt (loaded from ``prompts/Scraper_Prompt.md``).
        user_instruction: Short instruction text; keyword list is appended.
        keywords: Keywords to include in the user instruction.
        attachments: Files (bytes or pre-extracted text) to attach.
        model_format: One of ``"openrouter_file"`` (Iteration 1).
        profiles: The document profiles to offer, in order (ADR-0139 §3).
            Without profiles the text has no profile block, and an empty
            keyword list stays empty instead of reading ``(none)``.
        preselected: ``True`` if ``profiles`` holds the one profile the
            document is to be read as; the model then does not classify.

    Returns:
        A list of two dicts: a system message and a user message.

    Raises:
        ValueError: If ``model_format`` is not recognised.
    """
    keyword_lines = "\n".join(f"- {k.name} ({k.type.value})" for k in keywords)
    profile_block = ""
    if profiles:
        profile_block = _build_profile_block(profiles, preselected)
        keyword_lines = keyword_lines or "(none)"
    instruction_text = (
        f"{user_instruction}\n\n{profile_block}Keywords to extract:\n{keyword_lines}\n"
    )

    user_content: list[dict[str, Any]] = [{"type": "text", "text": instruction_text}]
    for att in attachments:
        user_content.append(_build_attachment_block(att, model_format))

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _build_profile_block(profiles: Sequence[Profile], preselected: bool) -> str:
    """Render the profiles for the instruction text, ending with an empty line.

    Args:
        profiles: The profiles, in the order they are offered.
        preselected: Whether the lead line names a preselection.

    Returns:
        The lead line and one paragraph per profile.
    """
    lines = [_PRESELECTED_LEAD if preselected else _CLASSIFY_LEAD, ""]
    for profile in profiles:
        lines.append(f"Profile {profile.id} — {profile.label}")
        lines.extend([_field_line(field) for field in profile.fields] or ["(no fields)"])
        lines.append("")
    return "\n".join(lines) + "\n"


def _field_line(field: ProfileField) -> str:
    unit = f", {field.unit}" if field.unit else ""
    hint = f" {field.hint}" if field.hint else ""
    return f"- {field.key} ({field.type.value}{unit}) — {field.label}.{hint}"


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
