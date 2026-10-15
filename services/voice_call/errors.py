# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Local exception hierarchy for the voice call (ADR-0137 §2).

These are plain :class:`Exception` subclasses, for the reason the voice
service gives in :mod:`services.voice.errors`: they are defined locally —
rather than anchored on a project-wide ``PortfoliFlowError`` — to honour the
layering rule for ``services/voice_call/``. The package imports only the
standard library and ``httpx``, and nothing under it imports from ``core/``.

The hierarchy makes "no silent fallback" enforceable: an incomplete call
configuration and a vendor refusal each surface a distinct, catchable error
rather than an empty value.
"""

from __future__ import annotations


class VoiceCallError(Exception):
    """Base class for all voice-call errors."""


class CallConfigurationError(VoiceCallError):
    """The voice call is active but incomplete; the message names the field and the card.

    Raised by :func:`~services.voice_call.resolved.assemble_call` at first
    use, never at import or start-up; a call that is switched off is a valid,
    quiet state and never raises.
    """


class CallProviderError(VoiceCallError):
    """The vendor refused or failed.

    The message carries the operation, the HTTP status and a body excerpt, or
    the transport failure — never the key.
    """


class CallJobStateError(VoiceCallError):
    """An ``ask_shirley`` job was asked for a transition its state does not allow.

    Raised by :class:`~services.voice_call.jobs.CallJob`; cancelling a
    terminal job is not an error and never raises.
    """
