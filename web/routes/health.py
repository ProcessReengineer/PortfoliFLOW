# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Health endpoint for the FastAPI variant.

Returns a liveness signal even when the database is unreachable.
Operations workflows depend on ``/health`` never producing a 5xx —
otherwise a brief Postgres outage would mark the web process itself
as unhealthy and trigger restart loops that compound the problem.

The schema revision (Alembic head) is read once at app startup and
cached on ``app.state``; routes do not re-query the database on every
request.

The response also carries the built-in tick scheduler's health
(ADR-0117 §5) — read from in-process state, so it costs no query and
survives the database being down exactly as the rest of the endpoint
does.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from web.tick_scheduler import TickSchedulerView, read_tick_scheduler_view

router = APIRouter()


def _tick_scheduler_payload(view: TickSchedulerView) -> dict[str, object]:
    """Serialise the scheduler view (ADR-0117 §5).

    External mode reports the mode alone: with no in-process task there
    is no liveness to assert and no interval to name — the external timer
    owns the rhythm, and reporting ``null``s would suggest this process
    knew something about it.

    Args:
        view: The view built by
            :func:`web.tick_scheduler.read_tick_scheduler_view`.

    Returns:
        The ``tick_scheduler`` object of the health payload.
    """
    if view.mode == "external":
        return {"mode": "external"}
    return {
        "mode": "internal",
        "alive": view.alive,
        "last_tick_at": view.last_tick_at.isoformat() if view.last_tick_at else None,
        "interval_seconds": view.interval_seconds,
    }


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    """Return liveness, the cached Alembic revision and the tick scheduler.

    Returns:
        A dict with ``status`` (``"ok"`` when the DB reads succeeded
        at startup, ``"degraded"`` otherwise), ``schema_revision``
        (the head revision id, or ``None`` when degraded) and
        ``tick_scheduler`` (see :func:`_tick_scheduler_payload`).
    """
    revision: str | None = getattr(request.app.state, "schema_revision", None)
    scheduler = _tick_scheduler_payload(read_tick_scheduler_view(request.app.state))
    if revision is None:
        return {"status": "degraded", "schema_revision": None, "tick_scheduler": scheduler}
    return {"status": "ok", "schema_revision": revision, "tick_scheduler": scheduler}
