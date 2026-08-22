# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cross-tenant due evaluation, cadence arithmetic, and lock keys.

Per ADR-0086 the *act* of ticking is dumb, tenant-blind infrastructure;
the interesting logic — "who is due now" and "when is the next beat" —
lives here in the domain layer, agnostic to what triggered the tick.

Three concerns, deliberately kept thin:

- :func:`find_due_tenants` — the **cross-tenant** due read. It runs on a
  superuser connection with RLS intentionally bypassed (a platform-level
  scheduler read, analogous to ``inspect-tenant``'s superuser metadata
  reads), *before* entering any single tenant's context. That is why it
  does NOT live on the tenant-scoped
  :class:`~core.repositories.irene_schedule_repository.IreneScheduleRepository`
  (Prompt 1 left it out on purpose). The scoped beat-completion *write*
  (``mark_beat_done``) does live on the repository.
- :func:`compute_next_due_at` — a pure cadence function (no DB): given a
  clock, a cadence, an anchor hour, and a tenant timezone, return the
  next beat time in UTC. The v2 vocabulary (ADR-0125 §1, extending
  ADR-0119 §1) is ``daily``, ``every_6h``, ``every_3h``, ``every_2h``,
  ``hourly``, ``every_30m`` and ``every_15m``.
- :func:`advisory_lock_key` — a deterministic 64-bit signed key derived
  from a tenant UUID, used with ``pg_try_advisory_xact_lock`` to claim a
  tenant's beat so two overlapping ticks (or a future multi-worker tick)
  cannot double-run one tenant.

Layering: imports only stdlib, SQLAlchemy, and :mod:`core.exceptions`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from core.exceptions import IreneCadenceInvalid

# Cadence vocabulary v2 (ADR-0125 §1, extending ADR-0119 §1), mapped to
# its step as a :class:`~datetime.timedelta`. Every step divides 24 hours,
# so the candidate grid of a cadence repeats identically each local day and
# ``preferred_hour`` stays a meaningful anchor. ``daily`` is simply the
# 24-hour case, which is why extending the vocabulary — first to hourly
# intervals, then to minute-granular steps — needed no second arithmetic
# path: for a 15-minute step the candidates are the ``:00/:15/:30/:45``
# grid measured from the full hour, which falls out of ``anchor + k·step``
# rather than being a rule of its own. For the sub-hourly members the
# anchor hour is practically inert (as it already is for ``hourly``); that
# is accepted (ADR-0125 §1).
#
# Kept as a module constant so the error message, the candidate arithmetic
# and any future validation share one source of truth. The Watch Desk and
# the market-data admin surface keep **separate** choice tuples by decision
# (ADR-0125 §2), and neither tuple is derived from this map — which
# cadences a domain offers is that domain's own call, and the two have
# taken different ones.
#
# Accepted debt (ADR-0125, Consequences): this vocabulary is
# domain-neutral, yet it still lives under ``services/irene/`` and reports
# a bad value as :class:`~core.exceptions.IreneCadenceInvalid`. Relocating
# both is deferred to a successor ADR, deliberately not bundled here.
_CADENCE_STEP: dict[str, timedelta] = {
    "daily": timedelta(hours=24),
    "every_6h": timedelta(hours=6),
    "every_3h": timedelta(hours=3),
    "every_2h": timedelta(hours=2),
    "hourly": timedelta(hours=1),
    "every_30m": timedelta(minutes=30),
    "every_15m": timedelta(minutes=15),
}

_SUPPORTED_CADENCES: frozenset[str] = frozenset(_CADENCE_STEP)

# Default advisory-lock domain. Irene was the first (and, pre-slice-5, only)
# caller, so its keys are computed from the bare tenant UUID; keeping this
# the default makes every existing Irene key byte-identical after the domain
# parameter was added (proven by a pinning test). A second domain
# (``"market_data"``, ADR-0093) salts the same tenant into a disjoint key so
# a market-data refresh can never block an Irene beat or vice versa.
_DEFAULT_LOCK_DOMAIN: str = "irene"


@dataclass(frozen=True)
class DueTenant:
    """One tenant whose Irene beat is due, from the cross-tenant read.

    Attributes:
        tenant_id: The tenant to beat (scoping key for the beat's
            ``tenant_context``).
        schedule_id: The ``irene_schedule`` row id, so the beat can call
            ``mark_beat_done`` without re-reading the schedule.
        cadence: The schedule's cadence — one of
            :data:`_SUPPORTED_CADENCES` (ADR-0119 §1, extended by
            ADR-0125 §1).
        timezone: The tenant's IANA timezone name (e.g.
            ``Europe/Berlin``), used to place ``preferred_hour``.
        preferred_hour: The anchor hour of day (0–23), or ``None``.
    """

    tenant_id: UUID
    schedule_id: UUID
    cadence: str
    timezone: str
    preferred_hour: int | None


async def find_due_tenants(conn: AsyncConnection) -> list[DueTenant]:
    """Return every enabled tenant whose next beat is due, DB-clock based.

    Runs ``SELECT ... WHERE enabled AND next_due_at <= now()`` on a
    **superuser** connection: RLS is bypassed intentionally because this
    is a platform-level scheduler read spanning all tenants, run before
    any tenant context exists (ADR-0086). ``now()`` is evaluated
    DB-side, so every tenant is compared against one clock.

    Args:
        conn: A superuser :class:`AsyncConnection` (RLS-bypassing).

    Returns:
        A list of :class:`DueTenant`, one per due schedule row. Empty
        when nothing is due — the common, near-free case.
    """
    result = await conn.execute(
        text(
            "SELECT tenant_id, id AS schedule_id, cadence, timezone, "
            "preferred_hour "
            "FROM irene_schedule "
            "WHERE enabled AND next_due_at <= now()"
        )
    )
    return [
        DueTenant(
            tenant_id=row.tenant_id,
            schedule_id=row.schedule_id,
            cadence=row.cadence,
            timezone=row.timezone,
            preferred_hour=row.preferred_hour,
        )
        for row in result
    ]


def compute_next_due_at(
    now: datetime,
    cadence: str,
    preferred_hour: int | None,
    timezone_name: str,
) -> datetime:
    """Return the next beat time in UTC for a schedule's cadence.

    Pure function — no DB, no I/O — so it is unit-tested directly with
    fixed inputs. ``preferred_hour`` is the **anchor** (ADR-0119 §2): for
    a cadence of step S the candidate local instants are ``anchor + k·S``
    for integer k, evaluated in the tenant's ``timezone_name``, and the
    return value is the next candidate occurrence *strictly after*
    ``now``. Strictness is what keeps a beat that runs *at* a candidate
    instant from re-firing within the same tick window — it schedules the
    following slot instead.

    ``daily`` is the S=24h case and therefore unchanged from the v0
    behaviour: the next occurrence of ``preferred_hour``, rolling to
    tomorrow when today's has passed. ``hourly`` (S=1h) makes the anchor
    practically inert, which is accepted — and so do the minute-granular
    members ``every_30m`` and ``every_15m``, whose candidates are the
    half- and quarter-hour grids measured from the full hour (ADR-0125
    §1). That grid is a consequence of the arithmetic below, not a
    separate branch: no sub-hourly cadence takes a different path.

    The arithmetic runs on **wall-clock** local time and the result is
    localised afterwards, so a cadence keeps its local hours across a DST
    transition rather than drifting by the offset. :mod:`zoneinfo`
    resolves the edges: a nonexistent local time (spring-forward gap)
    shifts forward with the gap, and an ambiguous one (fall-back repeat)
    follows fold rules, taking the first pass — so the repeated hour
    beats once, not twice.

    Args:
        now: The current instant. Must be timezone-aware; naive input is
            a programming error at the call sites (the tick passes an
            aware UTC ``now``).
        cadence: The schedule's cadence — one of
            :data:`_SUPPORTED_CADENCES`.
        preferred_hour: Anchor hour of day (0–23), or ``None`` for
            midnight in the tenant timezone.
        timezone_name: The tenant's IANA timezone name (e.g.
            ``Europe/Berlin``). Placement of the candidate hours happens
            in this zone, then the result is converted to UTC, so DST
            offsets are handled by :mod:`zoneinfo`.

    Returns:
        The next beat time as a timezone-aware UTC datetime.

    Raises:
        IreneCadenceInvalid: If ``cadence`` is outside the supported
            vocabulary.
    """
    if cadence not in _SUPPORTED_CADENCES:
        raise IreneCadenceInvalid(
            f"Unsupported Irene cadence {cadence!r}; "
            f"supported cadences are {sorted(_SUPPORTED_CADENCES)}.",
            field="cadence",
        )

    tz = ZoneInfo(timezone_name)
    anchor_hour = preferred_hour if preferred_hour is not None else 0
    step = _CADENCE_STEP[cadence]

    # Wall-clock arithmetic: strip the zone so subtraction and stepping
    # count *local* time rather than elapsed instants (an aware
    # subtraction would net out the DST offset and pull the candidate off
    # its anchor). Today's anchor occurrence is the reference point; the
    # candidates are that point plus any integer number of steps, in
    # either direction, which is why ``k`` may be negative or zero.
    local_naive_now = now.astimezone(tz).replace(tzinfo=None)
    anchor = local_naive_now.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)

    # Smallest integer k with ``anchor + k·step > local_naive_now``.
    # timedelta floor division floors toward negative infinity, so this
    # holds for a ``now`` on either side of the anchor, and the ``+ 1``
    # is what makes the comparison strict on an exact hit.
    k = (local_naive_now - anchor) // step + 1
    candidate = anchor + k * step

    return candidate.replace(tzinfo=tz).astimezone(timezone.utc)


def advisory_lock_key(tenant_id: UUID, *, domain: str = _DEFAULT_LOCK_DOMAIN) -> int:
    """Derive a stable 64-bit signed advisory-lock key from a tenant UUID.

    Used with ``pg_try_advisory_xact_lock(key)`` to claim a tenant's
    periodic job. Two overlapping tick firings (or a future multi-worker
    tick) map the same ``(tenant, domain)`` to the same key, so only one can
    hold the lock at a time; the other's ``pg_try_...`` returns false and it
    skips.

    Deriving the key in Python (rather than the SQL
    ``('x' || substr(md5(:tid), 1, 16))::bit(64)::bigint`` idiom) lets the
    tick pass the value as a bind parameter — no SQL-cast subtlety — and
    makes the mapping directly unit-testable. The computation mirrors that
    SQL form: the first 8 bytes of ``md5(material)`` reinterpreted as a
    big-endian two's-complement signed 64-bit integer, which is exactly what
    ``bit(64)::bigint`` yields.

    Domain separation (ADR-0093 §0.2): different periodic jobs must not share
    a lock key, so a market-data refresh can never block an Irene beat. The
    ``domain`` salts the hashed material — but the **default** domain
    (``"irene"``) hashes the bare ``str(tenant_id)``, byte-identical to the
    pre-domain form, so every existing Irene key is unchanged (a pinning test
    proves this). A non-default domain hashes ``f"{domain}:{tenant_id}"``,
    yielding a disjoint key for the same tenant.

    Args:
        tenant_id: The tenant to derive a lock key for.
        domain: The job domain sharing the lock namespace. Defaults to
            ``"irene"`` (the original caller); the market-data tick passes
            ``"market_data"``.

    Returns:
        A deterministic signed 64-bit integer in
        ``[-2**63, 2**63)`` — the range ``pg_try_advisory_xact_lock``
        accepts for its ``bigint`` argument.
    """
    material = str(tenant_id) if domain == _DEFAULT_LOCK_DOMAIN else f"{domain}:{tenant_id}"
    digest = hashlib.md5(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


__all__ = [
    "DueTenant",
    "advisory_lock_key",
    "compute_next_due_at",
    "find_due_tenants",
]
