# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.irene.scheduling`` (ADR-0086, ADR-0119).

Pure-function coverage — no DB, no network:

* ``compute_next_due_at`` — daily cadence before/after the preferred
  hour, timezone → UTC conversion, a DST-offset sanity pair (Berlin
  summer vs winter), and the unknown-cadence error.
* ``compute_next_due_at`` under the ADR-0119 §2 anchor semantics — one
  case per sub-daily cadence member, the anchor's candidate grid, the
  strictly-after-now rule, and both DST edges (spring-forward gap,
  fall-back repeat) for a sub-daily cadence.
* ``advisory_lock_key`` — determinism (same UUID → same key), distinctness
  (different UUIDs → different keys, collision-tolerant), and the signed
  64-bit range Postgres accepts for ``pg_try_advisory_xact_lock``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    """A cadence outside the v1 vocabulary raises the typed error.

    ``weekly`` is the invalid example because ``hourly`` — this test's
    original one — joined the vocabulary in ADR-0119 §1.
    """
    now = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
    with pytest.raises(IreneCadenceInvalid):
        compute_next_due_at(now, "weekly", 6, "UTC")


# ---------------------------------------------------------------------------
# compute_next_due_at — v1 vocabulary and anchor semantics (ADR-0119 §2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cadence", "expected_hour"),
    [
        ("hourly", 10),
        ("every_2h", 10),
        ("every_3h", 11),
        ("every_6h", 14),
        ("daily", 8),
    ],
)
def test_each_cadence_member_steps_from_the_anchor(cadence: str, expected_hour: int) -> None:
    """Every vocabulary member resolves, stepping from the 08:00 anchor.

    At 09:30 the candidate grids are: hourly 8,9,10,… ⇒ 10:00; every_2h
    8,10,12,… ⇒ 10:00; every_3h 8,11,14,… ⇒ 11:00; every_6h 2,8,14,20 ⇒
    14:00; daily 8 ⇒ tomorrow. Also pins that ``daily`` is the N=24 case
    of the same arithmetic rather than a separate branch.
    """
    now = datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc)
    result = compute_next_due_at(now, cadence, 8, "UTC")
    expected_day = 3 if cadence == "daily" else 2
    assert result == datetime(2026, 7, expected_day, expected_hour, 0, tzinfo=timezone.utc)


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
