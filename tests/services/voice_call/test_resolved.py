# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice_call.resolved` (ADR-0137 §7).

The pure assembly returns inactive (``None``), active (a
:class:`ResolvedCall` with every default filled) or raises
:class:`CallConfigurationError` naming the field and the Admin card — and,
because the declaration has no environment link, never a ``.env`` variable.
The value object masks its key on every rendering path.
"""

from __future__ import annotations

import pytest

from services.voice_call import (
    DEFAULT_BASE_URL,
    DEFAULT_CONVERSATION_MODEL,
    DEFAULT_LANGUAGE,
    CallConfigurationError,
    ResolvedCall,
    assemble_call,
)

_KEY = "xi-call-supersecret"
_CARD = "Admin → Providers & Credentials → Voice call"


def test_off_is_none_whatever_else_is_set() -> None:
    """A switched-off call is the quiet inactive state, even fully configured."""
    assert assemble_call(False, _KEY, "agent", "m", "v", "en", "https://x.invalid", "t") is None
    assert assemble_call(False, None, None, None, None, None, None, None) is None


def test_on_with_key_and_voice_fills_every_default() -> None:
    """Model, language and base URL fall back to the package defaults; ``""`` is no agent."""
    call = assemble_call(True, _KEY, "", None, "v1", "", None, None)

    assert call == ResolvedCall(
        api_key=_KEY,
        agent_id=None,
        model=DEFAULT_CONVERSATION_MODEL,
        voice="v1",
        language=DEFAULT_LANGUAGE,
        base_url=DEFAULT_BASE_URL,
    )


def test_on_without_a_key_names_the_card_and_no_env_variable() -> None:
    """A missing key fails loudly, naming the card and the field — no ``.env``."""
    with pytest.raises(CallConfigurationError) as excinfo:
        assemble_call(True, "  ", None, None, "v1", None, None, None)

    message = str(excinfo.value)
    assert _CARD in message
    assert "API key" in message
    assert ".env" not in message


def test_voice_falls_back_to_the_tts_voice_and_own_voice_wins() -> None:
    """Empty own voice → ``voice_tts.voice``; a set own voice outranks it."""
    fallback = assemble_call(True, _KEY, None, None, "", None, None, "v_tts")
    own = assemble_call(True, _KEY, None, None, "v_call", None, None, "v_tts")

    assert fallback is not None and fallback.voice == "v_tts"
    assert own is not None and own.voice == "v_call"


def test_neither_voice_names_both_fields() -> None:
    """No call voice and no persona voice: the message names both places."""
    with pytest.raises(CallConfigurationError) as excinfo:
        assemble_call(True, _KEY, None, None, None, None, None, " ")

    message = str(excinfo.value)
    assert "Voice" in message
    assert "text-to-speech" in message
    assert _CARD in message


def test_values_are_stripped_and_the_trailing_slash_dropped() -> None:
    """Whitespace is stripped from every value; the base URL loses its trailing ``/``."""
    call = assemble_call(
        True, f" {_KEY} ", " agent_x ", " m-x ", " v1 ", " en ", " https://eu.invalid/ ", None
    )

    assert call == ResolvedCall(
        api_key=_KEY,
        agent_id="agent_x",
        model="m-x",
        voice="v1",
        language="en",
        base_url="https://eu.invalid",
    )


def test_repr_masks_the_key() -> None:
    """``repr``, ``str`` and f-strings never render the key."""
    call = ResolvedCall(
        api_key=_KEY,
        agent_id="agent_x",
        model="m",
        voice="v",
        language="de",
        base_url=DEFAULT_BASE_URL,
    )

    for rendered in (repr(call), str(call), f"{call}"):
        assert _KEY not in rendered
        assert "masked" in rendered
        assert "agent_x" in rendered
