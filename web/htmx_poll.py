# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared HTMX post-enqueue poll helpers (ADR-0120, ADR-0125 §5).

Several surfaces share one problem: a button *enqueues* work — an Irene
beat (ADR-0086), a market-data refresh (ADR-0093) — and the run happens ~a
tick later in the scheduler (ADR-0117), by which time the already-rendered
page has no way to learn that it did. ADR-0120 solved it for the Watch
Desk with a 15-second HTMX poll that the **server** terminates; ADR-0125 §5
adopts the same loop, one-for-one, for the market-data surfaces.

This module holds the parts that are identical across those surfaces —
the two bounds and the two request-level primitives. It deliberately holds
**nothing** domain-specific: which row is read, what "landed" means, and
what the 286 carries stay with each surface's own router. It is the third
copy of these four names that this module exists to prevent, not the
second use of the pattern.

The contract every caller implements, in the ADR-0120 branch order:

* **landed** — 286 carrying the re-rendered region. 286 both cancels the
  poll and is swappable, so the one response ends the loop *and* refreshes
  the page; the swap removes the poller with the markup it replaces, so no
  second poller can survive.
* **pending** — 204, which HTMX does not swap: the page stands and the
  poll continues. This is the branch that runs ~4 times a minute, and it
  must stay one indexed row read.
* **stop, no swap** — :func:`poll_stop` when there is nothing left to wait
  for: an unusable ``since``, no schedule row, or a ``since`` older than
  :data:`POLL_HORIZON`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.responses import Response

# --- Post-enqueue poll bounds ----------------------------------------------
#
# These two values bound the poll that closes the enqueue-to-landing gap.
# Both are server-side: the poller carries no timeout of its own, it stops
# when the server says so.

# HTMX's "stop polling" status for an ``every …`` trigger. Not an
# HTTP-registered code — an htmx convention, honoured by the bundled 1.9.12 —
# so it is spelled out here rather than taken from :mod:`http`.
POLL_STOP_STATUS = 286

# How long after the enqueue the poller may keep asking. Caps a tab left open
# on a run that never happens (a stopped scheduler, a credential-gated
# domain) instead of polling until the browser closes. At the template's
# 15-second cadence this is at most 40 requests, each of them one indexed row
# read.
POLL_HORIZON = timedelta(minutes=10)


def poll_stop() -> Response:
    """End the poll without swapping anything.

    The 286 cancels the poll; ``HX-Reswap: none`` is what keeps the empty
    body out of the page. Without it the poller's declared ``outerHTML``
    swap would apply an empty response to its target — deleting the very
    region the poll exists to refresh.
    """
    return Response(status_code=POLL_STOP_STATUS, headers={"HX-Reswap": "none"})


def parse_poll_since(raw: str | None) -> datetime | None:
    """Parse the poller's ``since`` marker; ``None`` when it is unusable.

    Strict on purpose. The value is the server's own enqueue instant
    round-tripped through a query string, so anything else is a truncated
    or hand-edited URL and there is nothing to answer against. A naive
    stamp is rejected rather than assumed to be UTC: guessing a zone here
    would shift the done condition by hours in either direction — the poll
    would terminate on the first tick, or never.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
