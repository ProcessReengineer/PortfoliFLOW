# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression: both tick hosts call the same runner steps (ADR-0117 §2).

The shared tick runner (``services/scheduler/tick_runner.py``) has two
hosts — the built-in in-process scheduler (``web/tick_scheduler.py``) and
the external ``irene-tick`` command (``cli/irene_tick.py``). Neither may
drift from the other: a step that only one host calls is a step a
deployment silently loses when it switches tick source. The case wake-up
(ADR-0138 §4, P-CS-2c) is the step this guard was written for — both hosts
must call it beside the Irene beat.

A source grep, not an import: the web module is read as text, so the guard
neither imports ``web`` nor needs the app's settings.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_HOSTS = (
    ("web", "tick_scheduler.py"),
    ("cli", "irene_tick.py"),
)

_SHARED_STEPS = ("run_case_wake_tick(", "run_irene_tick(")


def test_both_tick_hosts_call_the_case_wake_and_the_irene_step() -> None:
    """Each host calls ``run_case_wake_tick`` and ``run_irene_tick``."""
    for parts in _HOSTS:
        source = _ROOT.joinpath(*parts).read_text(encoding="utf-8")
        for step in _SHARED_STEPS:
            assert step in source, (
                f"{'/'.join(parts)} does not call {step[:-1]}; both tick hosts "
                "must drive the same runner steps (ADR-0117 §2)."
            )
