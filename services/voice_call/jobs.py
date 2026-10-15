# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The records of a voice call's ``ask_shirley`` jobs (ADR-0137 §3).

The browser's handler for the ``ask_shirley`` client tool starts a job and
polls it. A :class:`CallJob` is the job's record — its state, the cancel flag
and, once it is done, the speakable answer and the display payload — and a
:class:`CallJobStore` holds the records of one process, keyed by session so a
job id from another session never resolves.

The state machine::

    waiting_for_turn ──start()──► running
    waiting_for_turn | running ──complete()──► done
    waiting_for_turn | running ──fail()──────► failed
    waiting_for_turn | running ──cancel()────► cancelled

A job is ``waiting_for_turn`` from its creation until the turn yields its
first event, which proves the turn lock is held (ADR-0137 §6). Any other
transition raises :class:`~services.voice_call.errors.CallJobStateError`,
except :meth:`CallJob.cancel`, which is idempotent: a barge-in may race a
completion, and a cancel that arrives late changes nothing.

Records only: the task that runs a job, and everything asynchronous about
it, belongs to the web layer. Standard library only.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

from services.voice_call.errors import CallJobStateError

logger = logging.getLogger(__name__)

#: The states a job passes through (ADR-0137 §4, ``GET /chat/call/jobs/{id}``).
JobState = Literal["waiting_for_turn", "running", "done", "failed", "cancelled"]

_ACTIVE: frozenset[str] = frozenset({"waiting_for_turn", "running"})
_TERMINAL: frozenset[str] = frozenset({"done", "failed", "cancelled"})


@dataclass
class CallJob:
    """One ``ask_shirley`` job: the question, its state and, when done, its answer.

    Attributes:
        job_id: The job's handle on the wire, a 12-hex slice of a UUID.
        session_id: The owning session's UUID (string form).
        question: The question the call agent asked.
        state: The current :data:`JobState`.
        created_at: Epoch seconds at creation.
        cancel_requested: Set by :meth:`cancel`; the runner checks it at
            every event of the turn.
        speakable: The answer written for the ear (``done`` only).
        prose: Shirley's written answer as appended to the history
            (``done`` only).
        charts: The turn's archived chart records, each
            ``{"artifact_id", "caption", "spec", "oversized"}`` (``done``
            only).
        message_id: The id of the assistant message the answer was appended
            as (``done`` only).
        error: The user-safe failure message (``failed`` only).
    """

    job_id: str
    session_id: str
    question: str
    state: JobState = "waiting_for_turn"
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    speakable: str | None = None
    prose: str | None = None
    charts: list[dict[str, Any]] = field(default_factory=list)
    message_id: str | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether the job is ``done``, ``failed`` or ``cancelled``."""
        return self.state in _TERMINAL

    def _require_active(self, transition: str) -> None:
        """Raise unless the job is ``waiting_for_turn`` or ``running``."""
        if self.state not in _ACTIVE:
            raise CallJobStateError(f"Cannot {transition} a job that is {self.state}.")

    def start(self) -> None:
        """Mark the turn as running: its first event has arrived.

        Raises:
            CallJobStateError: If the job is not ``waiting_for_turn``.
        """
        if self.state != "waiting_for_turn":
            raise CallJobStateError(f"Cannot start a job that is {self.state}.")
        self.state = "running"

    def complete(
        self,
        *,
        speakable: str,
        prose: str,
        charts: list[dict[str, Any]],
        message_id: str,
    ) -> None:
        """Mark the job done and store its answer.

        Args:
            speakable: The answer written for the ear.
            prose: Shirley's written answer.
            charts: The turn's archived chart records.
            message_id: The id of the appended assistant message.

        Raises:
            CallJobStateError: If the job is already terminal.
        """
        self._require_active("complete")
        self.state = "done"
        self.speakable = speakable
        self.prose = prose
        self.charts = list(charts)
        self.message_id = message_id

    def fail(self, error: str) -> None:
        """Mark the job failed with a user-safe message.

        Args:
            error: The message the browser may show.

        Raises:
            CallJobStateError: If the job is already terminal.
        """
        self._require_active("fail")
        self.state = "failed"
        self.error = error

    def cancel(self) -> bool:
        """Request cancellation; idempotent on a terminal job.

        Returns:
            ``True`` when the job was active and is now ``cancelled``;
            ``False`` when it was already terminal, in which case nothing
            changes.
        """
        if self.is_terminal:
            return False
        self.state = "cancelled"
        self.cancel_requested = True
        return True

    def result(self) -> dict[str, Any]:
        """Return the job's wire shape for the poll endpoint.

        Returns:
            Always ``{"job_id", "state"}``; on ``done`` also ``speakable``,
            ``prose``, ``charts`` and ``message_id``; on ``failed`` also
            ``error``.
        """
        payload: dict[str, Any] = {"job_id": self.job_id, "state": self.state}
        if self.state == "done":
            payload.update(
                speakable=self.speakable,
                prose=self.prose,
                charts=list(self.charts),
                message_id=self.message_id,
            )
        elif self.state == "failed":
            payload["error"] = self.error
        return payload


class CallJobStore:
    """The bounded job records of one process, keyed ``(session_id, job_id)``.

    The oldest record is evicted, with a warning, when the store is full, as
    the pending-turn store does. Single-worker only, like every per-session store of the chat
    surface (ADR-0050).
    """

    def __init__(self, limit: int = 100) -> None:
        """Create an empty store holding at most ``limit`` records.

        Args:
            limit: The capacity; adding beyond it evicts the oldest record.
        """
        self.limit = limit
        self._jobs: OrderedDict[tuple[str, str], CallJob] = OrderedDict()

    def add(self, job: CallJob) -> None:
        """Store ``job``, evicting the oldest record when the store is full.

        Args:
            job: The record to store.
        """
        while len(self._jobs) >= self.limit:
            evicted_key, _ = self._jobs.popitem(last=False)
            logger.warning(
                "voice call: evicting job %s (LRU cap %d)",
                evicted_key,
                self.limit,
            )
        self._jobs[(job.session_id, job.job_id)] = job

    def get(self, session_id: str, job_id: str) -> CallJob | None:
        """Return the session's job with ``job_id``, or ``None``.

        Args:
            session_id: The requesting session's UUID (string form).
            job_id: The job id from the URL.

        Returns:
            The record, or ``None`` when it is absent or belongs to another
            session.
        """
        return self._jobs.get((session_id, job_id))
