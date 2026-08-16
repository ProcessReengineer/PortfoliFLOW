# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The built-in in-process tick scheduler — the default tick source (ADR-0117).

One asyncio background task, started by the web lifespan alongside the
Telegram bot, loops: sleep a fixed short interval, then run one
market-data tick and one Irene tick through the shared runner
(:mod:`services.scheduler.tick_runner`). Anyone who starts
``portfoliflow-web`` therefore has a working heartbeat with no OS-level
configuration; ``TICK_SCHEDULER_ENABLED=false`` opts back out to the
external systemd/cron tick sources, and the two may run **simultaneously**
— the advisory locks deduplicate, whichever claimant fires first beats.

Why this module lives in ``web/`` rather than ``services/``: it is wiring,
not orchestration. It binds two web-owned resources — the app's audit
engine (ADR-0117 §3) and :class:`web.settings.WebSettings` — to the
host-neutral runner, which must stay importable from the CLI and must not
import ``web``. The per-tick behaviour (due read, advisory-lock claim,
beat, schedule advance, per-tenant failure isolation, every tick log line)
is the runner's and is shared byte-for-byte with the CLI ticks; what is
decided here is only *when* a tick happens and what happens when one
fails.

Three properties are deliberate and each is pinned by a test:

- **Sleep first.** The first tick fires one full interval after startup,
  so the task never competes with the lifespan still bringing the process
  up (engines, bot, template environment).
- **Per-domain isolation.** Each domain runs in its own ``try``: a raised
  market-data tick must not starve Irene's beat, or the other way round.
  An infrastructure failure is logged with its traceback and the loop
  continues — the next interval retries. (The runner already isolates
  *per tenant* below this.)
- **A quiet credential gate.** Whether any scope can resolve an LLM
  credential is re-evaluated every tick — an operator who configures one
  must not have to restart — but the "nothing can resolve a credential"
  state is reported at task start and on *transitions* only. A deployment
  without an LLM key is an ordinary state under ADR-0117 (zero-config
  self-hosting is the point); it must not write a warning a minute
  forever. The market-data domain runs regardless: it needs no LLM.

Shutdown: the lifespan sets the stop event and waits a bounded grace
period. A sleeping task stops immediately; a task mid-beat is cancelled
when the grace runs out, which rolls its advisory-lock transaction back —
``next_due_at`` stays unadvanced and the beat is retried after restart
(ADR-0117 §1).

Visibility (ADR-0117 §5): the loop keeps an in-process
:class:`TickSchedulerStatus` — never a database row — and
:func:`read_tick_scheduler_view` reduces it, plus the deployment setting,
to the three reportable facts (mode, task alive, last completed tick).
Both reporting surfaces, ``/health`` and the Super Admin platform card,
call that one function, so the JSON and the page can never disagree about
what the scheduler is doing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import State

from services.scheduler.tick_runner import (
    IreneTickSettings,
    IreneTickSummary,
    MarketDataTickSummary,
    irene_credentials_reachable,
    run_irene_tick,
    run_market_data_tick,
)

_LOG = logging.getLogger("portfoliflow.web.tick_scheduler")

# The logger the shared runner emits under — owned by
# :mod:`services.scheduler.tick_runner` and referenced here rather than
# redefined, since its name is an operational contract (ADR-0117
# §Compliance: "the same structured log lines per tick and per tenant beat
# in both hosts"). The anti-spam filter below attaches by this name, so the
# two must be kept in step: see :func:`_credentials_reachable`, which mutes
# exactly one of the runner's lines and nothing else.
_RUNNER_LOG_NAME = "portfoliflow.scheduler"

# How long lifespan shutdown waits for an in-flight tick before cancelling
# it. Long enough for a due read and a schedule advance to finish, short
# enough that a restart is not held hostage by a slow LLM synthesis call.
SHUTDOWN_GRACE_SECONDS = 5.0


class TickSchedulerSettings(IreneTickSettings, Protocol):
    """The deployment settings the scheduler task reads — structurally typed.

    Extends the runner's :class:`~services.scheduler.tick_runner.IreneTickSettings`
    with the one field the loop itself needs.
    :class:`web.settings.WebSettings` satisfies it; so does any test double
    carrying the three attributes.

    Attributes:
        tick_scheduler_interval_seconds: How often to ask "who is due?",
            bounds-checked at settings load (ADR-0117 §4).
    """

    tick_scheduler_interval_seconds: int


@dataclass
class TickSchedulerStatus:
    """What the loop last did — in-process only (ADR-0117 §5).

    Mutable by design: the loop overwrites it after every tick and the
    reporting surfaces read it. Deliberately **not** persisted — ADR-0117
    §5 asks for visibility, and the durable evidence a beat leaves already
    exists per tenant (``last_beat_at`` / ``next_due_at``). A tick-state
    table would be a second, weaker copy of it.

    No locking: everything that touches this object runs on the single
    uvicorn event loop, and each field is rebound in one statement.

    Every field except :attr:`started_at`, :attr:`last_error_at` and
    :attr:`consecutive_failures` describes the **last completed tick**
    alone, so a domain that failed or was skipped reads ``None`` rather
    than leaving an older tick's summary standing.

    Attributes:
        started_at: When the task was created.
        last_tick_at: When both domains last finished — success or logged
            failure. ``None`` until the first interval elapses.
        last_irene_summary: The last tick's Irene summary, or ``None``
            when that tick's Irene domain failed or was gated off for want
            of a resolvable credential.
        last_market_data_summary: The last tick's market-data summary, or
            ``None`` when that tick's market-data domain failed.
        last_error_at: When a domain last raised, or ``None`` if none has.
        consecutive_failures: Ticks in a row in which at least one domain
            raised; reset by the first tick in which neither does.
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_tick_at: datetime | None = None
    last_irene_summary: IreneTickSummary | None = None
    last_market_data_summary: MarketDataTickSummary | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class TickSchedulerHandle:
    """A started scheduler task, the event that stops it, and its status.

    Parked on ``app.state.tick_scheduler`` by the lifespan so shutdown can
    quiesce the task and the reporting surfaces can read it (ADR-0117 §5).

    The handle stays frozen while :attr:`status` is mutable: freezing
    binds *which* task and status this handle names, not the tick counters
    inside them — an ``asyncio.Task`` is mutable state too.

    Attributes:
        task: The background task running :func:`run_tick_scheduler`.
        stop_event: Set to ask the loop to finish; see
            :func:`stop_tick_scheduler`.
        interval_seconds: The interval the task was started with, so a
            reader need not re-resolve the settings to report it.
        status: The live :class:`TickSchedulerStatus` the loop updates.
    """

    task: asyncio.Task[None]
    stop_event: asyncio.Event
    interval_seconds: int
    status: TickSchedulerStatus


@dataclass(frozen=True)
class TickSchedulerView:
    """The three reportable facts of ADR-0117 §5, plus the interval.

    One shape for both surfaces — ``/health`` serialises it, the Super
    Admin platform card renders it — so the JSON and the page state the
    same thing by construction. Built by
    :func:`read_tick_scheduler_view`.

    Attributes:
        mode: ``"internal"`` when the built-in scheduler is running in
            this process, ``"external"`` when a tick source outside it is
            expected (disabled, or enabled but not started).
        alive: Whether the task is still running; ``None`` in external
            mode, where there is no task to ask.
        last_tick_at: When both domains last finished; ``None`` before the
            first tick and in external mode.
        interval_seconds: How often the loop asks "who is due?"; ``None``
            in external mode, where the external timer sets the rhythm.
    """

    mode: Literal["internal", "external"]
    alive: bool | None = None
    last_tick_at: datetime | None = None
    interval_seconds: int | None = None


def read_tick_scheduler_view(app_state: State) -> TickSchedulerView:
    """Report the scheduler's health off ``app.state`` (ADR-0117 §5).

    Total by construction: every read is defensive, because ``/health``
    must answer even when startup left the state partial (no superuser
    URL, an unreachable database, a lifespan that has not run at all).
    "Enabled but no handle" is reported as ``external`` on purpose —
    whatever kept the task down, the deployment now factually depends on
    an external tick source.

    Args:
        app_state: The FastAPI application state.

    Returns:
        The :class:`TickSchedulerView` for this process.
    """
    settings = getattr(app_state, "settings", None)
    handle: TickSchedulerHandle | None = getattr(app_state, "tick_scheduler", None)
    enabled = bool(getattr(settings, "tick_scheduler_enabled", False))
    if not enabled or handle is None:
        return TickSchedulerView(mode="external")
    return TickSchedulerView(
        mode="internal",
        alive=not handle.task.done(),
        last_tick_at=handle.status.last_tick_at,
        interval_seconds=handle.interval_seconds,
    )


class _MuteTheGateWarning(logging.Filter):
    """Drop records emitted from inside the gate — see :func:`_credentials_reachable`.

    Matches on the emitting function rather than on the message text: the
    warning's wording is the runner's to change, its call site is not, and
    a wording-matched filter would silently stop muting the day it is
    reworded. Other code logging to the same logger from another thread
    while the filter is attached keeps its records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Reject records the credential gate itself emitted.

        Args:
            record: The record offered by the logger it is attached to.

        Returns:
            ``False`` for the gate's own line, ``True`` for everything else.
        """
        return record.funcName != irene_credentials_reachable.__name__


def _credentials_reachable(settings: IreneTickSettings, *, quiet: bool) -> bool:
    """Evaluate the runner's credential gate, optionally muting its warning.

    The gate is the runner's, not a copy of it: re-deriving "can any scope
    resolve an LLM credential?" here would be exactly the drift between
    hosts that ADR-0117 §2 forbids. But the gate logs its own operator
    warning whenever the answer is ``False``, and this loop asks once a
    minute forever — so in the steady unreachable state that one line is
    filtered out for the duration of the (synchronous, await-free) call,
    and this module reports the state on transitions instead.

    Args:
        settings: The deployment settings the gate reads.
        quiet: Whether to mute the gate's own warning for this evaluation.
            Pass ``True`` only when the previous evaluation already
            reported the unreachable state.

    Returns:
        ``True`` when some scope could resolve an LLM credential.
    """
    if not quiet:
        return irene_credentials_reachable(settings)

    runner_log = logging.getLogger(_RUNNER_LOG_NAME)
    muted = _MuteTheGateWarning()
    runner_log.addFilter(muted)
    try:
        return irene_credentials_reachable(settings)
    finally:
        runner_log.removeFilter(muted)


def _log_gate_state(*, reachable: bool) -> None:
    """Report the credential gate's state — at task start and on changes only.

    Args:
        reachable: The gate's current answer.
    """
    if reachable:
        _LOG.info(
            "tick-scheduler: an LLM credential is resolvable again — the Irene "
            "domain resumes on the next tick."
        )
    else:
        _LOG.warning(
            "tick-scheduler: no scope can resolve an LLM credential — skipping "
            "the Irene domain until one is configured (a tenant key in Admin → "
            "Providers & Credentials, or OPENROUTER_API_KEY in .env). The "
            "market-data domain keeps ticking. This is reported once, not per "
            "tick."
        )


def _record_completed_tick(status: TickSchedulerStatus, *, failed: bool) -> None:
    """Stamp the tick both domains have just finished onto the status.

    Called once per interval, after both domains ran — a tick counts as
    completed whether or not a domain failed, because "the loop is alive
    and getting through its work" is exactly what the health surface asks
    about. A failure is recorded alongside, not instead.

    Args:
        status: The loop's status object.
        failed: Whether either domain raised during this tick.
    """
    now = datetime.now(timezone.utc)
    status.last_tick_at = now
    if failed:
        status.last_error_at = now
        status.consecutive_failures += 1
    else:
        status.consecutive_failures = 0


async def _sleep_until_next_tick(stop_event: asyncio.Event, interval_seconds: int) -> bool:
    """Wait one interval, or return early when a stop is requested.

    Args:
        stop_event: The loop's stop signal.
        interval_seconds: How long one interval lasts.

    Returns:
        ``True`` when the wait ended because a stop was requested (the
        loop must break), ``False`` when the interval simply elapsed.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
    except TimeoutError:
        return False
    return True


async def run_tick_scheduler(
    engine: AsyncEngine,
    settings: TickSchedulerSettings,
    *,
    stop_event: asyncio.Event | None = None,
    status: TickSchedulerStatus | None = None,
) -> None:
    """Drive both tick domains until asked to stop (ADR-0117 §1).

    Runs one market-data tick and one Irene tick per interval, each
    isolated from the other's failures. Returns cleanly when
    ``stop_event`` is set; a cancellation propagates (rolling back any
    in-flight beat) rather than being swallowed.

    Args:
        engine: The RLS-bypassing engine the runner uses for its
            cross-tenant due reads and per-tenant transactions. The web
            host passes ``app.state.audit_engine`` (ADR-0117 §3); its
            lifecycle stays with the lifespan — a tick never disposes it.
        settings: The deployment settings (see
            :class:`TickSchedulerSettings`).
        stop_event: The stop signal. A fresh event is used when omitted,
            which leaves task cancellation as the only way to stop.
        status: The object the loop records each tick on (ADR-0117 §5).
            A fresh one is used when omitted, which simply leaves nobody
            holding a reference to it.
    """
    interval_seconds = settings.tick_scheduler_interval_seconds
    stop = stop_event if stop_event is not None else asyncio.Event()
    state = status if status is not None else TickSchedulerStatus()

    _LOG.info(
        "tick-scheduler: started — asking who is due every %ds (ADR-0117). "
        "External tick sources may run alongside; the advisory locks "
        "deduplicate.",
        interval_seconds,
    )

    # Evaluate once up front so the "nothing can resolve a credential"
    # state is reported at start rather than a full interval later. The
    # healthy state needs no line of its own — the runner logs per tick.
    reachable = _credentials_reachable(settings, quiet=False)
    if not reachable:
        _log_gate_state(reachable=False)

    while True:
        if await _sleep_until_next_tick(stop, interval_seconds):
            break

        failed = False

        # Market data first, and unconditionally: it needs no LLM, so a
        # deployment with no credential at all still refreshes prices.
        try:
            state.last_market_data_summary = await run_market_data_tick(engine)
        except Exception:  # noqa: BLE001 — one domain must not starve the other
            failed = True
            state.last_market_data_summary = None
            _LOG.exception(
                "tick-scheduler: the market-data tick failed; continuing with "
                "the Irene domain and retrying next interval."
            )

        was_reachable = reachable
        reachable = _credentials_reachable(settings, quiet=not was_reachable)
        if reachable is not was_reachable:
            _log_gate_state(reachable=reachable)
        # Cleared up front so a gated-off tick reports "no Irene summary"
        # rather than the last tick that had one.
        state.last_irene_summary = None
        if reachable:
            try:
                state.last_irene_summary = await run_irene_tick(engine, settings=settings)
            except Exception:  # noqa: BLE001 — a tick failure must not end the loop
                failed = True
                _LOG.exception("tick-scheduler: the Irene tick failed; retrying next interval.")

        _record_completed_tick(state, failed=failed)

    _LOG.info("tick-scheduler: stopped.")


def start_tick_scheduler(
    engine: AsyncEngine,
    settings: TickSchedulerSettings,
) -> TickSchedulerHandle:
    """Start the scheduler task and return its handle.

    Args:
        engine: The RLS-bypassing engine to hand to the runner (see
            :func:`run_tick_scheduler`).
        settings: The deployment settings.

    Returns:
        The :class:`TickSchedulerHandle` for the lifespan to park on
        ``app.state`` and quiesce on shutdown.
    """
    stop_event = asyncio.Event()
    status = TickSchedulerStatus()
    task = asyncio.create_task(
        run_tick_scheduler(engine, settings, stop_event=stop_event, status=status),
        name="portfoliflow-tick-scheduler",
    )
    return TickSchedulerHandle(
        task=task,
        stop_event=stop_event,
        interval_seconds=settings.tick_scheduler_interval_seconds,
        status=status,
    )


async def stop_tick_scheduler(
    handle: TickSchedulerHandle,
    *,
    grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
) -> None:
    """Quiesce the scheduler task — cleanly if it can, forcibly if it must.

    Must run **before** the engines the task uses are disposed. A task
    between ticks stops at once; a task mid-tick gets ``grace_seconds`` to
    finish and is cancelled after that, which rolls its advisory-lock
    transaction back so the beat is simply retried after restart
    (ADR-0117 §1). Never raises: shutdown continues regardless.

    Args:
        handle: The handle returned by :func:`start_tick_scheduler`.
        grace_seconds: How long an in-flight tick may take before it is
            cancelled.
    """
    handle.stop_event.set()
    # asyncio.wait (rather than wait_for) so neither a task exception nor
    # a timeout raises here: the grace period must not turn a shutdown
    # into an error, and the cancellation below has to stay explicit.
    _done, pending = await asyncio.wait({handle.task}, timeout=grace_seconds)

    if pending:
        _LOG.warning(
            "tick-scheduler: still ticking after %.0fs — cancelling. An "
            "in-flight beat rolls back and is retried after restart "
            "(ADR-0117 §1).",
            grace_seconds,
        )
        handle.task.cancel()
        with suppress(asyncio.CancelledError):
            await handle.task
        return

    if handle.task.cancelled():
        _LOG.info("tick-scheduler: task was already cancelled.")
        return
    error = handle.task.exception()
    if error is not None:
        _LOG.error(
            "tick-scheduler: task ended with an unhandled error: %s",
            error,
            exc_info=error,
        )
