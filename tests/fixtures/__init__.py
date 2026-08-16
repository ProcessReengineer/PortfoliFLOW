# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared test fixtures for the characterization-test suite.

Currently exposes:

* :mod:`tests.fixtures.openrouter_responses` — canned OpenRouter / OpenAI
  chat-completion SSE event sequences used by streaming tests.
* :mod:`tests.fixtures.sse_helpers` — small helpers to convert event
  sequences into the byte streams ``pytest-httpx`` expects.
* :mod:`tests.fixtures.mock_tools` — mock tool callables and their
  registration helpers for the ``ToolRegistry`` patching pattern used
  by the streaming tool-loop tests.
"""
