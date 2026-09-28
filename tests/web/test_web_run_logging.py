# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for logging configuration under ``portfoliflow-web`` (P-TG-H1).

``web.main.run`` is the serve command's whole body, and until P-TG-H1 it
configured no logging: every ``cli/`` command calls
:func:`core.logging_setup.configure_logging` for itself, and this one had
no equivalent, so every INFO line the app emitted was swallowed by
Python's last-resort handler — which passes WARNING and above only. The
Telegram bot's ``dispatcher registered`` lines are the ones the deploy doc
tells operators to look for, and they were among the casualties.

Two things are pinned here, both of them cheap to break by accident:

* the call happens, with the configured level, **before** uvicorn takes
  over the process, and
* ``LOG_LEVEL`` is validated at settings load rather than silently
  falling back, so a typo cannot masquerade as a working configuration.

Nothing here starts a server: ``uvicorn.run`` is replaced by a recorder.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import web.main
from web.settings import WebSettings


def test_run_configures_logging_before_uvicorn_takes_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run()`` configures the app's loggers, then hands off to uvicorn.

    The order is the point: uvicorn's own logging config leaves existing
    loggers alone, so configuring after the hand-off would be configuring
    after the process had already started emitting.
    """
    calls: list[tuple[str, object]] = []

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(
        web.main,
        "configure_logging",
        lambda level: calls.append(("configure_logging", level)),
    )
    monkeypatch.setattr(
        web.main.uvicorn,
        "run",
        lambda *args, **kwargs: calls.append(("uvicorn.run", kwargs.get("port"))),
    )

    web.main.run()

    assert [name for name, _ in calls] == ["configure_logging", "uvicorn.run"]
    assert calls[0][1] == "DEBUG"


def test_an_unknown_log_level_is_refused_at_settings_load() -> None:
    """Rejected, never quietly degraded to INFO — with the reason named."""
    with pytest.raises(ValidationError) as excinfo:
        WebSettings(log_level="verbose")

    assert "LOG_LEVEL" in str(excinfo.value)


def test_a_log_level_is_normalised_to_upper_case() -> None:
    """``logging`` names its levels in upper case; the operator need not."""
    assert WebSettings(log_level="debug").log_level == "DEBUG"
