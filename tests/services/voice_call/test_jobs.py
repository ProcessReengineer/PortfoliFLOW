# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``ask_shirley`` job records and their bounded store (ADR-0137 §3, §4).

Pure: no database, no event loop. What is pinned here is the state machine
the poll endpoint reports — a job waits for the turn, runs, and ends done,
failed or cancelled; a cancel that races a completion changes nothing — and
the store's two promises: it stays bounded, and a job id resolves only under
the session that owns it.
"""

from __future__ import annotations

import pytest

from services.voice_call import CallJob, CallJobStateError, CallJobStore


def _job(job_id: str = "job000000001", session_id: str = "session-a") -> CallJob:
    return CallJob(job_id=job_id, session_id=session_id, question="How is the book?")


def _done(job: CallJob) -> CallJob:
    job.complete(speakable="Fine.", prose="All fine.", charts=[], message_id="m1")
    return job


def test_a_new_job_waits_for_the_turn() -> None:
    """Created: ``waiting_for_turn``, not terminal, and the result names only id and state."""
    job = _job()

    assert job.state == "waiting_for_turn"
    assert job.is_terminal is False
    assert job.result() == {"job_id": "job000000001", "state": "waiting_for_turn"}


def test_start_makes_the_job_running() -> None:
    """The first event of the turn moves the job to ``running``."""
    job = _job()

    job.start()

    assert job.state == "running"
    assert job.is_terminal is False


def test_complete_from_running_is_done_with_the_answer() -> None:
    """``done`` carries the speakable, the prose, the charts and the message id."""
    job = _job()
    job.start()
    chart = {"artifact_id": "a1", "caption": "NAV", "spec": {}, "oversized": False}

    job.complete(speakable="Fine.", prose="All fine.", charts=[chart], message_id="m1")

    assert job.is_terminal is True
    assert job.result() == {
        "job_id": "job000000001",
        "state": "done",
        "speakable": "Fine.",
        "prose": "All fine.",
        "charts": [chart],
        "message_id": "m1",
    }


def test_fail_from_waiting_is_failed_with_the_message() -> None:
    """``failed`` carries the user-safe message."""
    job = _job()

    job.fail("Shirley did not answer.")

    assert job.is_terminal is True
    assert job.result() == {
        "job_id": "job000000001",
        "state": "failed",
        "error": "Shirley did not answer.",
    }


def test_cancel_from_an_active_state_is_cancelled() -> None:
    """From ``waiting_for_turn`` and from ``running``: cancelled, flagged, ``True``."""
    for started in (False, True):
        job = _job()
        if started:
            job.start()

        assert job.cancel() is True
        assert job.state == "cancelled"
        assert job.cancel_requested is True
        assert job.result() == {"job_id": "job000000001", "state": "cancelled"}


def test_a_terminal_job_ignores_cancel_and_refuses_every_transition() -> None:
    """Cancel on a terminal job is ``False`` and a no-op; the transitions raise."""
    for make_terminal in (
        _done,
        lambda job: (job.fail("boom"), job)[1],
        lambda job: (job.cancel(), job)[1],
    ):
        job = make_terminal(_job())
        before = job.result()
        flag_before = job.cancel_requested

        assert job.cancel() is False
        assert job.result() == before
        assert job.cancel_requested is flag_before

        with pytest.raises(CallJobStateError):
            job.start()
        with pytest.raises(CallJobStateError):
            job.complete(speakable="x", prose="x", charts=[], message_id="x")
        with pytest.raises(CallJobStateError):
            job.fail("x")
        assert job.result() == before


def test_the_store_evicts_the_oldest_job_at_its_limit() -> None:
    """Bounded like the pending-turn store: the fourth job pushes out the first."""
    store = CallJobStore(limit=3)
    jobs = [_job(job_id=f"job{index:09d}") for index in range(4)]
    for job in jobs:
        store.add(job)

    assert store.get("session-a", jobs[0].job_id) is None
    for job in jobs[1:]:
        assert store.get("session-a", job.job_id) is job


def test_the_store_resolves_a_job_only_under_its_own_session() -> None:
    """Another session's job id never resolves."""
    store = CallJobStore()
    job = _job(session_id="session-a")
    store.add(job)

    assert store.get("session-a", job.job_id) is job
    assert store.get("session-b", job.job_id) is None
