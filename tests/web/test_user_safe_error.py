# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :func:`web.errors.user_safe_error`.

The helper masks foreign exception detail behind a generic, correlation-id
message while letting the project's own :class:`PortfoliFlowError`
hierarchy — which carries deliberately user-facing text — pass through
verbatim. Both paths log at ERROR so operators can trace the id.
"""

from __future__ import annotations

import logging
import re

from core.exceptions import DataImportError
from web.errors import user_safe_error

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def test_foreign_exception_is_masked(caplog):
    """A non-PortfoliFlowError is masked; its detail lands only in the log."""
    secret = "secret /etc/path leaked"
    caplog.set_level(logging.ERROR, logger="web.errors")

    try:
        raise RuntimeError(secret)
    except RuntimeError as exc:
        user_message, error_id = user_safe_error(exc)

    # The user never sees the raw exception text.
    assert secret not in user_message
    # The correlation id is a short 8-hex string, echoed in the message.
    assert _HEX8.match(error_id)
    assert error_id in user_message

    # The full detail and a traceback are captured for operators at ERROR.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "expected an ERROR log record"
    assert secret in caplog.text
    assert error_id in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "RuntimeError" in caplog.text


def test_portfoliflow_error_passes_through(caplog):
    """A PortfoliFlowError message survives verbatim in the user message."""
    message = "Sheet 'NAVs' missing column 'as_of_date'."
    caplog.set_level(logging.ERROR, logger="web.errors")

    user_message, error_id = user_safe_error(DataImportError(message))

    # The deliberately user-facing diagnostic is returned unchanged.
    assert user_message == message
    # Still traceable: a short id and an ERROR log record.
    assert _HEX8.match(error_id)
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert error_id in caplog.text


def test_empty_portfoliflow_error_returns_empty_message():
    """An empty PortfoliFlowError yields an empty message (fallback preserved).

    Call sites keep an ``or "..."`` literal fallback for this case; the
    helper must not manufacture a generic message for a pass-through
    error, or that fallback would never fire.
    """
    user_message, error_id = user_safe_error(DataImportError(""))

    assert user_message == ""
    assert _HEX8.match(error_id)
