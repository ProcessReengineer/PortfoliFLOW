# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``surface_finding`` function-tool definition for Irene synthesis.

Irene's non-streaming synthesis call
(:meth:`services.ai_service_core.AIServiceCore.run_synthesis`) offers the
model exactly one tool, ``surface_finding``, with ``tool_choice="auto"``.
Zero calls to it means *silence* — the "nothing material" outcome falls
out natively (ADR-0086).

This is the full ADR-0088 contract: the tool's parameters *are* the
Watch Desk card schema. Irene's task is to decide **whether and how
often** to call it, to phrase ``trigger`` / ``finding`` / ``basis``, and to
*propose* an ``urgency_suggestion``. She does **not** have the last word on
urgency: the deterministic floor
(:mod:`services.analytics.irene_floor`) computes the final urgency and band
downstream in the beat, and the ``options`` half is band-gated (dropped on
an ``informational`` card). See ADR-0088 for the split between *informing*
(``finding``, always present) and *advising* (``options``, urgency-gated).
"""

from __future__ import annotations

from typing import Any

# The stable tool name. Keep this constant even as the schema grows so
# the beat handler's ``tc["name"] == SURFACE_FINDING_TOOL_NAME`` filter
# and any downstream logging stay valid across prompts.
SURFACE_FINDING_TOOL_NAME = "surface_finding"

# The full ADR-0088 ``surface_finding`` contract. Field names are English
# (ADR-0008). The schema separates *informing* (``finding``, always
# present) from *advising* (``options``, band-gated downstream).
SURFACE_FINDING_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SURFACE_FINDING_TOOL_NAME,
        "description": (
            "Surface one material finding for the tenant's Watch Desk. "
            "Call this ONLY when something is genuinely material and warrants "
            "the portfolio manager's attention. If nothing is material, do "
            "NOT call this tool at all — surfacing nothing (silence) is the "
            "correct and expected outcome on a calm book. Call it once per "
            "material change, reusing the subject_key you were given verbatim. "
            "'finding' informs (always required); 'options' advises in short "
            "connected prose and is kept only on higher-urgency cards — a "
            "low-urgency card is fact-only, so leave 'options' empty unless "
            "there is a genuine, actionable choice. You propose "
            "urgency_suggestion, but a "
            "deterministic floor sets the final urgency and band; you do not "
            "have the last word."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject_key": {
                    "type": "string",
                    "description": (
                        "The stable key you were ASSIGNED for the monitored "
                        "subject this finding is about. Reference it verbatim; "
                        "never mint or alter one. Only surface a subject_key "
                        "that appears in the beat context you were given."
                    ),
                },
                "trigger": {
                    "type": "string",
                    "description": (
                        "A short description of what the beat observed for "
                        "this subject (the change that prompted the finding)."
                    ),
                },
                "finding": {
                    "type": "string",
                    "description": (
                        "The informing statement: one-to-two sentences on "
                        "what is material and why it warrants attention. "
                        "Always present — this is never gated."
                    ),
                },
                "basis": {
                    "type": "string",
                    "description": (
                        "The derivation/grounding: which figures and which "
                        "source support the finding. Interpret the numbers "
                        "the beat gave you; never invent or alter a number. "
                        "The card shows the deterministic figure beside your "
                        "narrative."
                    ),
                },
                "urgency_suggestion": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "Your PROPOSED urgency on a 1–10 scale (higher = more "
                        "urgent). This is a suggestion only: the deterministic "
                        "floor that computes the FINAL urgency and band is "
                        "applied downstream, not by you. It may raise your "
                        "number to a trigger-type minimum or cap it by source."
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional — the 'advise' half. When there is a "
                        "genuine, actionable choice, write 1–3 connected "
                        "sentences that interpret the computed basis: what "
                        "the figures imply for the manager and what the "
                        "realistic moves are, grounded in the numbers you "
                        "were given (never invent quantities). Prose, not a "
                        "bulleted list of imperatives. Provide as one to "
                        "three short strings; they are joined into a single "
                        "paragraph. Leave empty on a low-urgency "
                        "(informational) card — such a card is pure fact, "
                        "not counsel — and these are band-gated downstream "
                        "regardless."
                    ),
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional references to the watch_state entries or "
                        "RSS bucket ids that ground this finding, for the "
                        "audit trail."
                    ),
                },
            },
            "required": [
                "subject_key",
                "trigger",
                "finding",
                "basis",
                "urgency_suggestion",
            ],
        },
    },
}

# Backward-compatible alias: earlier prompts imported the placeholder under
# this name. Kept so imports do not break; new code uses
# ``SURFACE_FINDING_TOOL``.
SURFACE_FINDING_TOOL_V0: dict[str, Any] = SURFACE_FINDING_TOOL


__all__ = [
    "SURFACE_FINDING_TOOL",
    "SURFACE_FINDING_TOOL_NAME",
    "SURFACE_FINDING_TOOL_V0",
]
