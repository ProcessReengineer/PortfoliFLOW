# ADR-0031: Module-Level Threading Lock as Interim Concurrency Control for Bot-Side Turns

- **Status:** Accepted
- **Date:** 2026-04-29
- **Deciders:** PortfoliFLOW project owner
- **Tags:** architecture, security, integration

---

## Context

ADR-0029 introduced `services/headless_shirley.run_turn` as the Qt-free
entry point for non-GUI consumers of Shirley. ADR-0030 introduced the
Telegram bot as the first such consumer, running in a daemon thread on
its own asyncio event loop. As soon as a non-GUI channel sits next to
the existing GUI channel, a concurrency question becomes real: two
threads can now drive a Shirley turn at the same time, and they share
state.

The shared state of concern is the per-turn gating state in
`services/tool_registry.py`. ADR-0022 defined the trust-class taxonomy
and a turn-scoped gating rule: once a `READ_EXTERNAL_UNTRUSTED` tool
has executed in a turn, all `WRITE_INTERNAL` and `EXTERNAL_EFFECT`
tools are locked for the remainder of that turn. The implementation
holds this state in a single instance attribute, `_locked_classes:
set[ToolClass]`, mutated in `begin_turn`, `end_turn`, and
`execute_tool`. None of those methods take a lock. While only one turn
ran at a time (the Qt event loop serialises `_StreamWorker.run`), this
was correct by construction. With a second concurrent driver (the bot),
the assumption no longer holds.

The decision PortfoliFLOW must make is *what concurrency control to
apply now, given that the right long-term answer depends on the
ToolRegistry's lifecycle in the post-refactor world (ADR-0018) — a
question the project does not need to answer today*. The Thursday demo
timebox rules out invasive changes to the ToolRegistry; the bot must
not block GUI use; and the project does not pretend that the gap
introduced is theoretical.

This decision is concurrency-, security-, and audit-relevant: the
gating state in ADR-0022 is the structural defence against
prompt-injection-driven side effects, and any race on that state is a
real (if narrow) integrity exposure.

## Decision

PortfoliFLOW introduces a module-level `threading.Lock` named
`_TURN_LOCK` in `services/headless_shirley.py`. The full body of
`run_turn` runs inside `with _TURN_LOCK:`. This serialises **bot-side
turns against each other**: no two calls to `run_turn` from any thread
can be inside the function at the same time.

The lock explicitly **does not** serialise bot-side turns against
GUI-side turns. The GUI path is `services/ai_service._StreamWorker.run`,
which is a `QThread.run` body and does not acquire `_TURN_LOCK`.
PortfoliFLOW records this as a known limitation, not a feature, and
characterises the actual exposure honestly: a GUI turn running
concurrently with a bot turn touches `ToolRegistry._locked_classes`
without synchronisation. The worst-case observable failure for the
single-operator case is a stale Trust-Gating state for one turn — for
example, the bot's `begin_turn()` reset clobbers the GUI's
in-flight lock state, allowing a `WRITE_INTERNAL` call inside the
GUI turn that would otherwise have been refused. This is a real race
condition. Its blast radius is small in the present deployment — the
operator rarely uses both channels in parallel, and the gating rule is
a defence-in-depth control rather than the only line of defence — but
it is named here rather than ignored.

PortfoliFLOW deliberately rejects, for this iteration, both ends of the
spectrum: doing nothing, and freezing the GUI behind a process-wide
turn lock. The full fix — making `ToolRegistry`'s per-turn state
thread-safe — is bundled with the planned client-server / repository
refactor of ADR-0018, at which point the registry's lifecycle is
reconsidered anyway. Resolving the registry's thread-safety strategy
ahead of that refactor would either prejudge the registry's eventual
shape (singleton vs. per-request) or build a partial answer that has to
be revisited.

## Rationale

- **The ToolRegistry's per-turn state is the correct thing to protect,
  and the wrong thing to redesign today.** ADR-0022 established the
  state and its lifecycle. A change to thread-safety semantics is not a
  local fix: it interacts with how the registry is acquired, scoped,
  and reset in the post-ADR-0018 world. The `_TURN_LOCK` is the
  smallest correct intervention that covers the bot-vs-bot case
  without prejudging that future redesign.
- **A module-level lock at `headless_shirley`'s seam is the right
  scope for the bot path.** Every bot turn enters through `run_turn`,
  so the lock at that boundary covers every bot turn; it does not
  reach across to the GUI path because `_StreamWorker.run` is a
  separate seam. Naming this asymmetry openly is preferable to
  inserting a lock inside `ToolRegistry` whose semantics would need
  to be re-litigated when the registry changes shape.
- **A process-wide turn lock would freeze the GUI behind in-flight
  bot turns.** The simplest implementation of "lock both paths"
  (acquire the lock at the top of both `run_turn` and
  `_StreamWorker.run`) makes the GUI unresponsive whenever a bot
  turn is mid-flight. For a single-operator deployment where the
  GUI is the primary surface, that is a worse user-visible
  regression than the race the lock would close.
- **Honesty about the residual race is part of the decision.**
  The race exists; it is small in the current deployment; it is
  bounded to one turn's worth of stale gating state; and the named
  fix path is recorded. Burying the limitation in a comment instead
  of an ADR would lose the audit trail.
- **Operationally tolerable today.** Simultaneous GUI + bot use is
  rare in practice. Item D.3 in the operator's handover names this
  explicitly as a watch-point during the next weeks of bot use, with
  the fix-becomes-urgent trigger described.

## Alternatives Considered

- **Make `ToolRegistry` thread-safe now (process-wide lock around
  state mutations).** Rejected for this iteration: increases coupling
  of an unrelated subsystem to the bot's needs; the simplest
  implementation freezes the GUI during bot turns; the right design
  depends on whether `ToolRegistry` stays a process singleton or
  becomes per-request in the post-ADR-0018 world. Answering that
  prematurely is wasted work.
- **Use `threading.local` for per-turn state in `ToolRegistry`
  immediately.** Rejected for this iteration: a partial change that
  would have to be revisited when the registry's lifecycle changes;
  recorded as a candidate solution for the eventual fix. The right
  design likely depends on whether per-turn state is per-thread or
  per-request, and that distinction matters in the server world.
- **Add a process-wide turn lock that both the bot and `_StreamWorker`
  honour.** Rejected for the GUI-freeze reason above. A more
  sophisticated variant — a fairness-aware reader/writer lock with
  priority for the GUI — solves the freeze, but reintroduces exactly
  the redesign question the simpler `_TURN_LOCK` defers.
- **Do nothing and accept the race even between bot turns.**
  Rejected — within the bot path the race is easy to close
  (`_TURN_LOCK` is one line) and the failure mode (two bot turns
  racing the gating state) has no operational defence. A small,
  cheap, complete fix for the bot path is worth taking now.
- **Refuse concurrent bot turns at the handler layer (a per-chat
  semaphore in `bot.telegram_bot`).** Rejected — it solves a
  different problem (one user, two messages in flight). The shared
  state we are protecting is process-wide; the lock belongs at the
  process-wide seam.

## Consequences

### Positive

- Two bot turns cannot race the `ToolRegistry` per-turn gating
  state. The most common concurrency case for the bot path is
  closed.
- The lock is cheap and its scope is one function in one file. An
  auditor can verify both its presence and its scope by reading
  `services/headless_shirley.py` end-to-end.
- The decision does not prejudge the right shape of `ToolRegistry`
  in the post-ADR-0018 world.

### Negative

- A bot turn concurrent with a GUI turn still touches
  `ToolRegistry._locked_classes` without synchronisation. The
  failure mode is a stale Trust-Gating state for the duration of
  one turn; it is bounded but real.
- A long-running bot turn blocks all subsequent bot turns. For a
  single operator this is invisible (they only have one chat in
  flight at a time); for a future multi-user bot this would need to
  be revisited together with the multi-user authorisation work
  (ADR-0019).
- The lock lives in the consuming module
  (`services/headless_shirley.py`) rather than at the producer of
  the unsafe state (`services/tool_registry.py`). That asymmetry is
  itself a smell; it is the cost of deferring the registry's
  redesign to a more appropriate moment.

### Neutral / Follow-ups

- **Full thread-safety of `ToolRegistry`.** Bundle with the
  ADR-0018 refactor. Candidate designs at that point include
  `threading.local` per-turn state, a registry-level lock, or
  per-turn registry contexts. The right choice depends on whether
  `ToolRegistry` stays a process-wide singleton (current behaviour)
  or becomes per-request (server world).
- **Watch-point in operation.** If the operator notices noticeable
  delay or GUI freezes when interacting with both channels at once
  (handover item D.3), the fix becomes more urgent than this ADR
  currently sequences.
- **DataStore concurrency.** A related but distinct gap — the
  `DataStore` singleton is also unsynchronised — is not addressed
  by `_TURN_LOCK`. It will be resolved automatically by the move to
  DuckDB-backed DataVault (ADR-0017), or by a discrete RLock if
  needed earlier. Out of scope here; recorded so the linkage is
  visible to a reader of this ADR.

## Implementation Notes

- Lock: `services/headless_shirley.py::_TURN_LOCK = threading.Lock()`
  at module scope. Acquired in `run_turn` via `with _TURN_LOCK:`;
  the body of the protected region is delegated to `_run_turn_locked`
  to keep the acquisition site auditably small.
- Module docstring section "Concurrency" in
  `services/headless_shirley.py` documents the scope and limitation
  of the lock in the same words this ADR uses.
- Unsynchronised state being protected:
  `services/tool_registry.py::ToolRegistry._locked_classes`,
  mutated in `begin_turn`, `end_turn`, and `execute_tool`.
- Counterpart with no lock acquisition:
  `services/ai_service.py::_StreamWorker.run`. This is the GUI-side
  turn entry point.
- Test demonstrating the bot-vs-bot serialisation:
  `tests/services/test_headless_shirley.py::test_lock_serialises_concurrent_turns`
  measures wall-clock time for two concurrent threads with a 0.5s
  mock `create()` and asserts the result is ~1.0s, not ~0.5s.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Reliability (the
  concurrency boundary is named and bounded), Security (the
  Trust-Gating state introduced in ADR-0022 has a known incomplete
  invariant for cross-channel concurrent turns; recorded openly
  rather than implied).
- **Regulatory references:** BAIT AT 7.2 — the lock and its
  documented gap are part of the structural control surface around
  the LLM execution path; an AT 7.2 review can read this ADR plus
  ADR-0022 plus the source of `ToolRegistry.execute_tool` and see
  exactly where the structural defence holds and where it does not.
- **Audit evidence:** the `_TURN_LOCK` source line and its
  docstring explanation in `services/headless_shirley.py`; the
  absence of locking in `services/ai_service._StreamWorker.run`;
  `tests/services/test_headless_shirley.py::test_lock_serialises_concurrent_turns`;
  this ADR.

## References

- ADR-0012 (ToolRegistry as single seam — the registry whose
  per-turn state this lock protects)
- ADR-0018 (Planned Service / Repository layering — the refactor
  that the full thread-safety fix is bundled with)
- ADR-0019 (Planned multi-user readiness — the context in which a
  per-user concurrency model becomes mandatory)
- ADR-0022 (Tool Trust Classes and Gating Policy — the policy whose
  in-memory state the lock protects)
- ADR-0029 (Headless Shirley — the module that owns the lock)
- ADR-0030 (Telegram bot — the consumer that introduced the
  concurrency question this ADR answers)

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-04-29 | PortfoliFLOW project owner   | Initial draft. Records the `_TURN_LOCK` in `services/headless_shirley.py`, what it protects, what it does not protect, and the named fix path bundled with ADR-0018. Code already implemented and in use. |
| 2026-05-03 | PortfoliFLOW project owner   | Phase 1 / Strang A1 (ADR-0038): the GUI-side tool-execution loop has moved from `services/ai_service.py::_StreamWorker.run` to `services.ai_service_core.AIServiceCore.stream_response` (asyncio). The bot-side `_TURN_LOCK` continues to live in `services/headless_shirley.py` unchanged — Strang A1 deliberately did *not* migrate it because that would require revisiting the `ToolRegistry` per-turn state's thread-safety strategy, and the ADR's reasons for deferral remain valid. The cross-channel race characterised in *Negative — A bot turn concurrent with a GUI turn …* is unchanged in scope but now sits between two asyncio loops (the bot's own loop running `headless_shirley.run_turn` and the per-call asyncio loop the Qt adapter spawns inside its `QThread`). The full thread-safety fix remains bundled with ADR-0018. The headless-shirley deletion / lock relocation question is owned by Strang A2 (ADR-0038 §5). |
| 2026-05-03 | PortfoliFLOW project owner   | Phase 1 / Strang A2 (ADR-0038): the lock has been relocated from `services/headless_shirley.py` to `services.ai_service_core` (still a module-level `threading.Lock`, kept thread-based rather than `asyncio.Lock` because consumers dispatch from different event loops — the bot's daemon-thread loop, the Qt adapter's per-call `asyncio.run` inside a `QThread`, and any future FastAPI worker — and a `threading.Lock` is loop-agnostic). `services/headless_shirley.py` was removed in this strang. Because every consumer (Qt adapter, Telegram bot, future FastAPI handler) now routes through `AIServiceCore.stream_response`, the lock additionally closes the bot-vs-GUI cross-channel race that this ADR previously named as a known limitation. The full `ToolRegistry` thread-safety fix bundled with ADR-0018 remains the long-term target; this lock is still the deliberately narrow interim measure, just with a wider serialisation footprint than at the time of the original decision. The retired `services.headless_shirley` lock-serialisation test was ported to `tests/characterization/test_ai_service_core.py::test_C_17a_lock_serialises_concurrent_turns`. Decider: PortfoliFLOW project owner. |
| 2026-05-14 | maintainer + AI              | The `_TURN_LOCK` *acquisition* in `services.ai_service_core` was moved off the event loop — `stream_response` now does `await asyncio.to_thread(_TURN_LOCK.acquire)` inside a `try/finally` instead of `with _TURN_LOCK:`. Under the FastAPI SSE variant, concurrent turns are tasks on the *same* uvicorn loop and all contend for the lock; a synchronous `acquire()` froze the whole loop (every request, plus `SIGINT`) until the lock came free. The lock stays a process-global `threading.Lock`, still held across the turn's `await` points and released in the `finally`. Its process-global scope — it serialises *all* users' turns — is flagged as multi-tenant-conversion work, to be replaced by per-tenant turn isolation bundled with the ADR-0018 `ToolRegistry` thread-safety fix. The same change dispatches each synchronous `ToolRegistry.execute_tool` call via `asyncio.to_thread` (a tool may block on a DB workflow's `Thread.join`). No new ADR — this is interim concurrency control already owned by this ADR; see the `services.ai_service_core` module docstring "Concurrency" section. |
