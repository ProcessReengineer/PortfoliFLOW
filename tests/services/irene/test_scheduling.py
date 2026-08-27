# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.irene.scheduling`` (ADR-0086, ADR-0119, ADR-0125).

Pure-function coverage — no DB, no network:

* ``compute_next_due_at`` — daily cadence before/after the preferred
  hour, timezone → UTC conversion, a DST-offset sanity pair (Berlin
  summer vs winter), and the unknown-cadence error.
* ``compute_next_due_at`` under the ADR-0119 §2 anchor semantics — one
  case per sub-daily cadence member, the anchor's candidate grid, the
  strictly-after-now rule, and both DST edges (spring-forward gap,
  fall-back repeat) for a sub-daily cadence.
* ``compute_next_due_at`` on the minute-granular members of vocabulary v2
  (ADR-0125 §1) — the quarter-hour grid measured from the full hour,
  strictness at minute granularity, and both Berlin DST transitions
  walked as a *chain* (each answer fed back as the next ``now``), which
  is how the tick actually advances a schedule.
* ``advisory_lock_key`` — determinism (same UUID → same key), distinctness
  (different UUIDs → different keys, collision-tolerant), and the signed
  64-bit range Postgres accepts for ``pg_try_advisory_xact_lock``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from core.exceptions import IreneCadenceInvalid
from services.irene.scheduling import advisory_lock_key, compute_next_due_at


# ---------------------------------------------------------------------------
# compute_next_due_at — daily cadence
# ---------------------------------------------------------------------------


def test_daily_before_preferred_hour_schedules_same_day() -> None:
    """Now earlier than the preferred hour ⇒ today at the preferred hour."""
    now = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "daily", 6, "UTC")
    assert result == datetime(2026, 7, 2, 6, 0, tzinfo=timezone.utc)


def test_daily_after_preferred_hour_rolls_to_next_day() -> None:
    """Now past the preferred hour ⇒ tomorrow at the preferred hour."""
    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "daily", 6, "UTC")
    assert result == datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)


def test_daily_at_exactly_preferred_hour_rolls_to_next_day() -> None:
    """A beat running *at* the preferred hour schedules the following day."""
    now = datetime(2026, 7, 2, 6, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "daily", 6, "UTC")
    assert result == datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)


def test_daily_none_preferred_hour_uses_midnight() -> None:
    """An unset preferred hour means midnight in the tenant timezone."""
    now = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "daily", None, "UTC")
    assert result == datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)


def test_timezone_conversion_to_utc_summer() -> None:
    """Berlin summer is UTC+2 ⇒ 06:00 local resolves to 04:00 UTC."""
    now = datetime(2026, 7, 2, 1, 0, tzinfo=timezone.utc)  # 03:00 Berlin
    result = compute_next_due_at(now, "daily", 6, "Europe/Berlin")
    assert result == datetime(2026, 7, 2, 4, 0, tzinfo=timezone.utc)


def test_dst_offset_sanity_winter_differs_from_summer() -> None:
    """Berlin winter is UTC+1 ⇒ 06:00 local resolves to 05:00 UTC.

    Paired with the summer case above, this shows the UTC endpoint shifts
    with the DST offset — the conversion is zone-aware, not a fixed skew.
    """
    now = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)  # 04:00 Berlin
    result = compute_next_due_at(now, "daily", 6, "Europe/Berlin")
    assert result == datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)


def test_result_is_timezone_aware_utc() -> None:
    """The returned datetime is always timezone-aware UTC."""
    now = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "daily", 6, "Europe/Berlin")
    assert result.tzinfo is not None
    assert result.utcoffset() == timezone.utc.utcoffset(None)


# ---------------------------------------------------------------------------
# compute_next_due_at — unknown cadence
# ---------------------------------------------------------------------------


def test_unknown_cadence_raises() -> None:
    """A cadence outside the v2 vocabulary raises the typed error.

    ``weekly`` is the invalid example because ``hourly`` — this test's
    original one — joined the vocabulary in ADR-0119 §1.
    """
    now = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
    with pytest.raises(IreneCadenceInvalid):
        compute_next_due_at(now, "weekly", 6, "UTC")


def test_unknown_cadence_message_lists_the_v2_vocabulary() -> None:
    """The error message enumerates the whole vocabulary, v2 members included.

    The list is generated from ``sorted(_SUPPORTED_CADENCES)``, so the two
    ADR-0125 §1 members appear the moment they join the map. Pinning it
    here states both halves: that they *did* join, and that the
    operator-facing message still names the full vocabulary rather than a
    stale subset. ``every_5m`` is the invalid example on purpose — it is
    the cadence ADR-0125 §2 explicitly declines to offer.
    """
    now = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
    with pytest.raises(IreneCadenceInvalid) as excinfo:
        compute_next_due_at(now, "every_5m", 6, "UTC")

    message = str(excinfo.value)
    assert "every_15m" in message
    assert "every_30m" in message


# ---------------------------------------------------------------------------
# compute_next_due_at — vocabulary and anchor semantics (ADR-0119 §2,
# extended to the v2 members by ADR-0125 §1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cadence", "expected_hour", "expected_minute"),
    [
        ("hourly", 10, 0),
        ("every_2h", 10, 0),
        ("every_3h", 11, 0),
        ("every_6h", 14, 0),
        ("daily", 8, 0),
        ("every_30m", 10, 0),
        ("every_15m", 9, 45),
    ],
)
def test_each_cadence_member_steps_from_the_anchor(
    cadence: str, expected_hour: int, expected_minute: int
) -> None:
    """Every vocabulary member resolves, stepping from the 08:00 anchor.

    At 09:30 the candidate grids are: hourly 8,9,10,… ⇒ 10:00; every_2h
    8,10,12,… ⇒ 10:00; every_3h 8,11,14,… ⇒ 11:00; every_6h 2,8,14,20 ⇒
    14:00; daily 8 ⇒ tomorrow. The two minute-granular members of
    vocabulary v2 (ADR-0125 §1) join the same table on the same
    arithmetic: every_30m runs 8:00, 8:30, …, 9:30, 10:00 — 09:30 is a
    candidate falling *on* ``now``, which "strictly after" rules out, so
    the answer is 10:00 — and every_15m runs …, 9:30, 9:45 ⇒ 09:45. Also
    pins that ``daily`` is the 24-hour case of the same arithmetic rather
    than a separate branch.
    """
    now = datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc)
    result = compute_next_due_at(now, cadence, 8, "UTC")
    expected_day = 3 if cadence == "daily" else 2
    assert result == datetime(
        2026, 7, expected_day, expected_hour, expected_minute, tzinfo=timezone.utc
    )


def test_anchor_grid_wraps_backwards_across_midnight() -> None:
    """Candidates below the anchor are real candidates, not rounding losses.

    ``(anchor + k·N) mod 24`` admits negative k: an 08:00 anchor at
    ``every_6h`` puts a candidate at 02:00, so a 01:00 ``now`` is due at
    02:00 — not at the anchor itself. Stepping forward from the anchor
    alone would wrongly answer 08:00.
    """
    now = datetime(2026, 7, 2, 1, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "every_6h", 8, "UTC")
    assert result == datetime(2026, 7, 2, 2, 0, tzinfo=timezone.utc)


def test_sub_daily_last_slot_of_day_rolls_to_the_first_of_the_next() -> None:
    """After the last candidate of a local day, the next is tomorrow's first."""
    now = datetime(2026, 7, 2, 21, 0, tzinfo=timezone.utc)
    # every_6h anchored at 08:00 ⇒ 02:00, 08:00, 14:00, 20:00.
    result = compute_next_due_at(now, "every_6h", 8, "UTC")
    assert result == datetime(2026, 7, 3, 2, 0, tzinfo=timezone.utc)


def test_sub_daily_at_exactly_a_candidate_hour_moves_to_the_next_slot() -> None:
    """The "strictly after now" rule holds per slot, not just per day.

    This is what stops a beat that runs *at* a candidate hour from
    re-firing inside the same tick window.
    """
    now = datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc)
    result = compute_next_due_at(now, "every_6h", 8, "UTC")
    assert result == datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)


def test_sub_daily_none_anchor_uses_midnight_grid() -> None:
    """An unset anchor means the grid starts at local midnight."""
    now = datetime(2026, 7, 2, 7, 15, tzinfo=timezone.utc)
    # every_3h anchored at midnight ⇒ 0,3,6,9,12,15,18,21.
    result = compute_next_due_at(now, "every_3h", None, "UTC")
    assert result == datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)


def test_hourly_anchor_is_inert() -> None:
    """N=1 makes the anchor practically inert (ADR-0119 §2, accepted)."""
    now = datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc)
    assert compute_next_due_at(now, "hourly", 0, "UTC") == compute_next_due_at(
        now, "hourly", 17, "UTC"
    )


def test_sub_daily_is_strictly_after_now_across_a_full_day() -> None:
    """Sweep a whole day: every answer is in the future and on the grid.

    A property check rather than a point assertion — it would catch an
    off-by-one in the step count at any hour of the day, not just the
    ones the cases above happen to probe.
    """
    tz = ZoneInfo("Europe/Berlin")
    for minute in range(0, 24 * 60, 7):
        now = datetime(2026, 7, 2, 0, 0, tzinfo=tz) + timedelta(minutes=minute)
        result = compute_next_due_at(now, "every_3h", 8, "Europe/Berlin")
        assert result > now
        local = result.astimezone(tz)
        assert local.minute == 0
        assert (local.hour - 8) % 3 == 0


# ---------------------------------------------------------------------------
# compute_next_due_at — minute-granular cadences (ADR-0125 §1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now_minute", "expected_utc_hour", "expected_utc_minute"),
    [
        (3, 8, 15),
        (17, 8, 30),
        (31, 8, 45),
        (52, 9, 0),
    ],
)
def test_every_15m_grid_is_measured_from_the_full_hour(
    now_minute: int, expected_utc_hour: int, expected_utc_minute: int
) -> None:
    """``every_15m`` lands on :00 / :15 / :30 / :45 of every local hour.

    This is the test ADR-0125 §1 leans on. The ADR adds no rule about a
    quarter-hour grid — it argues the grid is already a *property of the
    existing arithmetic*: ``anchor + k·step`` with the anchor pinned at
    ``preferred_hour:00`` and a 15-minute step cannot produce anything but
    quarter-hour offsets from a full hour. The four cases walk one local
    hour at :03, :17, :31 and :52 and get :15, :30, :45 and the next :00.

    Anchor 0 and Berlin summer (UTC+2) on a non-DST day, so the local
    grid maps onto UTC by a constant offset and the expected instants
    read as 08:15/08:30/08:45/09:00 UTC for local 10:15/10:30/10:45/11:00.
    """
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 7, 2, 10, now_minute, tzinfo=tz)

    result = compute_next_due_at(now, "every_15m", 0, "Europe/Berlin")

    assert result == datetime(
        2026, 7, 2, expected_utc_hour, expected_utc_minute, tzinfo=timezone.utc
    )
    assert result.astimezone(tz).minute in (0, 15, 30, 45)


def test_every_15m_at_exactly_a_slot_moves_to_the_following_slot() -> None:
    """The strictly-after rule holds at minute granularity too.

    A refresh that runs *at* 10:15 local schedules 10:30, not itself. The
    guard matters more at 15 minutes than it did at an hour: the built-in
    tick scheduler asks "who is due?" every 60 seconds (ADR-0117 §4), so
    a candidate landing on ``now`` would otherwise be re-claimed inside
    the same quarter hour.
    """
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 7, 2, 10, 15, tzinfo=tz)

    result = compute_next_due_at(now, "every_15m", 0, "Europe/Berlin")

    assert result == datetime(2026, 7, 2, 8, 30, tzinfo=timezone.utc)
    assert result.astimezone(tz) == datetime(2026, 7, 2, 10, 30, tzinfo=tz)


def test_sub_hourly_anchor_is_inert() -> None:
    """Both v2 members make the anchor inert, as ``hourly`` already does.

    ADR-0125 §1 accepts this rather than hiding it: a 15- or 30-minute
    grid measured from any full hour is the same grid, so a tenant's
    ``preferred_hour`` stops carrying information once the cadence goes
    sub-hourly. It is also the reasoning behind the ``preferred_hour = 0``
    seed value ADR-0125 §3 specifies — the honest value for an anchor
    nothing reads — which strand M2 lands, not this one.
    """
    now = datetime(2026, 7, 2, 9, 37, tzinfo=timezone.utc)
    for cadence in ("every_30m", "every_15m"):
        assert compute_next_due_at(now, cadence, 0, "UTC") == compute_next_due_at(
            now, cadence, 17, "UTC"
        )


# ---------------------------------------------------------------------------
# compute_next_due_at — DST edges for a sub-daily cadence (ADR-0119 §2)
# ---------------------------------------------------------------------------


def test_sub_daily_spring_forward_keeps_the_local_grid() -> None:
    """Across the Berlin spring-forward the cadence keeps its local hours.

    On 2026-03-29 the local hour 02:00–03:00 does not exist. With an
    ``every_3h`` cadence anchored at 23:00 the grid is 23,02,05,… — so
    the 02:00 slot lands in the gap. :mod:`zoneinfo` shifts it forward
    with the gap (fold=0 ⇒ the pre-transition UTC+1 offset), i.e. 01:00
    UTC, which is 03:00 local; the following slot is the ordinary 05:00
    CEST. The local grid is preserved, not skewed by the offset jump.
    """
    tz = ZoneInfo("Europe/Berlin")
    # 00:30 UTC = 01:30 CET, still before the 02:00 transition.
    now = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)
    gap_slot = compute_next_due_at(now, "every_3h", 23, "Europe/Berlin")
    assert gap_slot == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert gap_slot.astimezone(tz).hour == 3  # the gap pushed 02:00 → 03:00

    after = compute_next_due_at(gap_slot, "every_3h", 23, "Europe/Berlin")
    assert after.astimezone(tz).hour == 5
    assert after == datetime(2026, 3, 29, 3, 0, tzinfo=timezone.utc)


def test_sub_daily_fall_back_beats_the_repeated_hour_once() -> None:
    """Across the Berlin fall-back the repeated local hour beats once.

    On 2026-10-25 the local hour 02:00–03:00 occurs twice (CEST then
    CET). Wall-clock arithmetic with fold=0 takes the first pass, so an
    ``hourly`` cadence resolves 02:00 CEST (00:00 UTC) and then moves to
    03:00 CET (02:00 UTC) — the duplicate 02:00 CET is skipped rather
    than beaten a second time. Answers stay strictly increasing in real
    time, which is what the due read requires.
    """
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)  # 01:30 CEST

    first = compute_next_due_at(now, "hourly", 0, "Europe/Berlin")
    assert first == datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
    assert first.astimezone(tz).hour == 2  # 02:00 CEST, the first pass

    second = compute_next_due_at(first, "hourly", 0, "Europe/Berlin")
    assert second == datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)
    assert second.astimezone(tz).hour == 3  # 03:00 CET — the repeat is skipped
    assert second > first



# ---------------------------------------------------------------------------
# compute_next_due_at — DST chains at 15-minute granularity (ADR-0125 §1)
# ---------------------------------------------------------------------------
#
# The two tests below *chain*: each answer is fed back as the next ``now``,
# which is exactly how the tick advances a schedule (``mark_beat_done``
# writes ``next_due_at`` computed from the run instant). A point assertion
# would miss what matters at a 15-minute cadence — that the sequence stays
# strictly increasing and evenly spaced through a transition, so no slot
# fires twice and none stalls.


def test_every_15m_spring_forward_chain_keeps_fifteen_minute_spacing() -> None:
    """Chaining ``every_15m`` across the Berlin gap neither stalls nor repeats.

    On 2026-03-29 the local hour 02:00–02:59 does not exist. Starting at
    00:45 UTC (01:45 CET) and feeding each answer back, the chain yields,
    in UTC: 01:00, 01:15, 01:30, 01:45, 02:00, 02:15.

    Mechanism: the first candidate is the naive local 02:00 — a
    nonexistent time. :mod:`zoneinfo` resolves it with the
    *pre*-transition offset (fold=0, UTC+1), so it lands on 01:00 UTC,
    which is 03:00 CEST. From there the local wall clock already reads
    03:00, so the grid continues 03:15, 03:30, … The four gap candidates
    02:00–02:45 would collapse onto the same instants as the 03:00–03:45
    CEST slots, but the chain visits each instant exactly once because
    the wall clock has jumped past them. Real-time spacing therefore
    stays exactly 15 minutes across the transition — the ADR-0125
    Consequences statement, pinned.
    """
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 3, 29, 0, 45, tzinfo=timezone.utc)  # 01:45 CET

    results: list[datetime] = []
    for _ in range(6):
        now = compute_next_due_at(now, "every_15m", 0, "Europe/Berlin")
        results.append(now)

    assert results == [
        datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 1, 15, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 1, 45, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 2, 15, tzinfo=timezone.utc),
    ]
    for earlier, later in pairwise(results):
        assert later > earlier
        assert later - earlier == timedelta(minutes=15)

    # The very first answer is already past the gap: 01:00 UTC is 03:00 CEST.
    assert results[0].astimezone(tz).hour == 3
    assert results[0].astimezone(tz).minute == 0


def test_every_15m_fall_back_chain_fires_the_repeated_hour_once() -> None:
    """Chaining ``every_15m`` across the Berlin repeat costs one 75-minute gap.

    On 2026-10-25 the local hour 02:00–02:59 occurs twice (CEST, then
    CET). Starting at 00:30 UTC (02:30 CEST) and feeding each answer
    back, the chain yields, in UTC: 00:45, 02:00, 02:15, 02:30, 02:45.

    Mechanism: 00:45 UTC is 02:45 CEST, the last quarter-hour slot of the
    first pass. The next naive candidate is local 03:00, which is
    unambiguous and localises as CET (UTC+1) — 02:00 UTC. The repeated
    hour's four slots therefore fire once, in the CEST pass, and the four
    CET repeats are skipped, exactly as the ADR-0119 §2 fold rule ("first
    pass wins") already did for ``hourly``.

    The visible consequence is a **75-minute gap in UTC** between the
    first and second answers, after which 15-minute spacing resumes. It
    is accepted, not a defect: the alternative — beating the repeated
    hour twice — would double-fetch and could move ``next_due_at``
    backwards, which the due read cannot tolerate.
    """
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)  # 02:30 CEST

    results: list[datetime] = []
    for _ in range(5):
        now = compute_next_due_at(now, "every_15m", 0, "Europe/Berlin")
        results.append(now)

    assert results == [
        datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 2, 15, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 2, 45, tzinfo=timezone.utc),
    ]

    # Strictly increasing throughout — the property the due read needs.
    for earlier, later in pairwise(results):
        assert later > earlier

    # The one accepted irregularity, and only that one.
    assert results[1] - results[0] == timedelta(minutes=75)
    for earlier, later in pairwise(results[1:]):
        assert later - earlier == timedelta(minutes=15)

    assert results[0].astimezone(tz).hour == 2  # 02:45 CEST — the first pass
    assert results[1].astimezone(tz).hour == 3  # 03:00 CET — the repeat skipped


# ---------------------------------------------------------------------------
# advisory_lock_key
# ---------------------------------------------------------------------------


def test_advisory_lock_key_is_deterministic() -> None:
    """The same UUID always maps to the same key."""
    tenant_id = UUID("11111111-1111-1111-1111-111111111111")
    assert advisory_lock_key(tenant_id) == advisory_lock_key(tenant_id)


def test_advisory_lock_key_differs_across_tenants() -> None:
    """Different UUIDs map to different keys (collision-tolerant sample)."""
    keys = {advisory_lock_key(UUID(int=i)) for i in range(1, 200)}
    # 199 distinct UUIDs should yield 199 distinct 64-bit keys; a stray
    # collision would be a red flag for the derivation, not statistical
    # noise at this tiny sample size.
    assert len(keys) == 199


def test_advisory_lock_key_within_signed_64bit_range() -> None:
    """Keys fit the signed bigint range pg_try_advisory_xact_lock accepts."""
    for _ in range(50):
        key = advisory_lock_key(uuid4())
        assert -(2**63) <= key < 2**63


# ---------------------------------------------------------------------------
# advisory_lock_key — domain separation (ADR-0093 §0.2)
# ---------------------------------------------------------------------------


def test_advisory_lock_key_irene_domain_byte_identical_pin() -> None:
    """The default (Irene) key is byte-identical to the pre-domain form.

    Adding the ``domain`` parameter must not shift any existing Irene lock
    key. Pinned two ways: (1) hard-coded literals computed from the original
    ``md5(str(tenant_id))[:8]`` formula, and (2) the default equals passing
    ``domain="irene"`` explicitly. If either drifts, a running Irene tick
    would silently move to a new lock namespace mid-deployment.
    """
    pins = {
        UUID("11111111-1111-1111-1111-111111111111"): 4091181416664098055,
        UUID("00000000-0000-0000-0000-000000000000"): -6950804328280008906,
    }
    for tenant_id, expected in pins.items():
        assert advisory_lock_key(tenant_id) == expected
        assert advisory_lock_key(tenant_id, domain="irene") == expected


def test_advisory_lock_key_market_data_domain_differs() -> None:
    """The market_data domain salts the key so it never collides with Irene's.

    A market-data refresh and an Irene beat must be able to run the same
    tenant concurrently without one blocking the other, which requires their
    advisory-lock keys to differ.
    """
    for i in range(1, 100):
        tenant_id = UUID(int=i)
        assert advisory_lock_key(tenant_id, domain="market_data") != advisory_lock_key(tenant_id)
        # And the market_data domain stays deterministic + in range.
        key = advisory_lock_key(tenant_id, domain="market_data")
        assert advisory_lock_key(tenant_id, domain="market_data") == key
        assert -(2**63) <= key < 2**63
