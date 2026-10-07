# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure-projection tests for the Watch Desk monitor (DC2, ADR-0089).

The monitor's DB fan-out is exercised by the route tests in
``test_watch_desk.py``; everything here is the deterministic
projection that sits between the coverage frame and the template — the
gauge arithmetic, Irene's note assembly, and the group summaries. These
functions take plain arguments and need no database.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.exceptions import LimitSetNotEffective
from core.repositories.irene_finding_repository import IreneFindingDTO
from core.repositories.irene_watch_state_repository import IreneWatchStateDTO
from services.analytics.irene_floor import DEFAULT_FLOOR_CONFIG, DEFAULT_WARN_THRESHOLD_PCT
from services.analytics.signal_watch import NoObservation, SignalObservation, SignalResult
from services.watch_desk.overlay import (
    DisabledFamily,
    SignalWatchpoint,
    SubjectOverlay,
    WatchDeskResolution,
    rss_overlay_subject_key,
)
import services.watch_desk.family_status as _monitor_home
from services.watch_desk.family_status import (
    FIGURE_UNIT_DAYS,
    FIGURE_UNIT_PCT,
    FamilyStatus,
    SignalFigure,
    _build_family_group,
    _build_rss_group,
    _fired_this_beat,
    _irene_note,
    _utilisation_pct,
    read_family_status,
)
from services.watch_desk.family_status import build_monitor as _build_monitor
from services.web_research.allowlist import _KNOWN_TAGS

_TENANT: UUID = uuid4()


def _resolution(*overlays: SubjectOverlay) -> WatchDeskResolution:
    """Build a resolution over the code defaults, plus any overlays given.

    The projection takes its calibration as one resolved argument since
    ADR-0116 §5, so these pure tests state it explicitly rather than
    letting the module reach for a default — which it deliberately cannot.
    """
    return WatchDeskResolution(
        config=DEFAULT_FLOOR_CONFIG,
        warn_default_pct=DEFAULT_WARN_THRESHOLD_PCT,
        overlays={overlay.subject_key: overlay for overlay in overlays},
    )


def _overlay(
    subject_key: str,
    *,
    muted: bool = False,
    warn_threshold_pct: str | None = None,
    re_trigger_delta: str | None = None,
) -> SubjectOverlay:
    """Build one sensitivity overlay for a derived subject."""
    return SubjectOverlay(
        watchpoint_id=uuid4(),
        subject_key=subject_key,
        family=subject_key.split(":", 1)[0],
        display_name=subject_key,
        muted=muted,
        warn_threshold_pct=(
            Decimal(warn_threshold_pct) if warn_threshold_pct is not None else None
        ),
        re_trigger_delta=(Decimal(re_trigger_delta) if re_trigger_delta is not None else None),
        notes=None,
    )


def _coverage_frame(rows: list[dict]) -> pd.DataFrame:
    """Build a coverage frame in the engine's long format.

    ``as_of_date`` is datetime64 exactly as ``compute_coverage`` emits it,
    so the projection's Timestamp comparison is exercised for real.
    """
    frame = pd.DataFrame(
        rows,
        columns=[
            "as_of_date",
            "class_key",
            "max_pct",
            "nav_sum_eur",
            "coverage_pct",
            "headroom_eur",
            "status",
        ],
    )
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"])
    return frame


def _row(
    *,
    class_key: str,
    max_pct: str | None,
    coverage_pct: str,
    status: str,
    as_of: date = date(2026, 6, 30),
) -> dict:
    return {
        "as_of_date": as_of,
        "class_key": class_key,
        "max_pct": Decimal(max_pct) if max_pct is not None else None,
        "nav_sum_eur": Decimal("1000000"),
        "coverage_pct": Decimal(coverage_pct),
        "headroom_eur": Decimal("0"),
        "status": status,
    }


def _watch(
    *,
    subject_key: str,
    acknowledged_magnitude: str | None,
    acknowledged_at: datetime | None,
) -> IreneWatchStateDTO:
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    return IreneWatchStateDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        subject_key=subject_key,
        magnitude=None,
        band=None,
        acknowledged_at=acknowledged_at,
        acknowledged_magnitude=(
            Decimal(acknowledged_magnitude) if acknowledged_magnitude is not None else None
        ),
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


def _finding(
    *,
    subject_key: str,
    created_at: datetime,
    payload: dict | None = None,
) -> IreneFindingDTO:
    return IreneFindingDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        subject_key=subject_key,
        payload=payload or {},
        urgency=7,
        band="noteworthy",
        resolution="open",
        resolved_at=None,
        resolved_by=None,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Gauge arithmetic — the shared 0 → ceiling scale
# ---------------------------------------------------------------------------


def test_utilisation_is_coverage_over_ceiling() -> None:
    assert _utilisation_pct(Decimal("18.7"), Decimal("20.0")) == Decimal("93.5")


def test_zero_ceiling_permits_nothing() -> None:
    """A zero ceiling is fully utilised by any positive coverage."""
    assert _utilisation_pct(Decimal("0.4"), Decimal("0")) == Decimal("100")
    assert _utilisation_pct(Decimal("0"), Decimal("0")) == Decimal("0")


def test_missing_ceiling_has_no_utilisation() -> None:
    assert _utilisation_pct(Decimal("12.0"), None) is None


def test_breach_clamps_rendered_fill_but_keeps_the_figure_honest() -> None:
    """A breach must not overflow the bar — while the text stays truthful."""
    group = _build_family_group(
        family="anlv",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="17",
                    max_pct="5.0",
                    coverage_pct="6.5",
                    status="BREACH",
                )
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={},
        effective_label="AnlV 2026",
        resolution=_resolution(),
    )
    (row,) = group["rows"]
    assert row["fill_pct"] == 100.0
    assert row["utilisation"] == pytest.approx(130.0)
    assert row["coverage_pct"] == pytest.approx(6.5)
    assert row["max_pct"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Family group projection
# ---------------------------------------------------------------------------


def test_unconstrained_rows_are_skipped() -> None:
    """NO_LIMIT / UNALLOCATED carry no ceiling, so there is nothing to gauge."""
    group = _build_family_group(
        family="saa",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="equity",
                    max_pct="25.0",
                    coverage_pct="10.0",
                    status="OK",
                ),
                _row(
                    class_key="other",
                    max_pct=None,
                    coverage_pct="3.0",
                    status="NO_LIMIT",
                ),
                _row(
                    class_key="unallocated",
                    max_pct=None,
                    coverage_pct="1.0",
                    status="UNALLOCATED",
                ),
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={},
        effective_label="SAA 2026",
        resolution=_resolution(),
    )
    assert [row["subject_key"] for row in group["rows"]] == ["saa:equity"]
    assert group["subject_count"] == 1


def test_only_the_latest_stichtag_is_projected() -> None:
    group = _build_family_group(
        family="saa",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="equity",
                    max_pct="25.0",
                    coverage_pct="10.0",
                    status="OK",
                    as_of=date(2026, 5, 31),
                ),
                _row(
                    class_key="equity",
                    max_pct="25.0",
                    coverage_pct="23.9",
                    status="WARN",
                    as_of=date(2026, 6, 30),
                ),
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={},
        effective_label="SAA 2026",
        resolution=_resolution(),
    )
    (row,) = group["rows"]
    assert row["status"] == "WARN"
    assert row["coverage_pct"] == pytest.approx(23.9)


def test_group_badges_are_severity_ordered_and_non_zero_only() -> None:
    group = _build_family_group(
        family="saa",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="a",
                    max_pct="10.0",
                    coverage_pct="11.0",
                    status="BREACH",
                ),
                _row(
                    class_key="b",
                    max_pct="10.0",
                    coverage_pct="9.5",
                    status="WARN",
                ),
                _row(
                    class_key="c",
                    max_pct="10.0",
                    coverage_pct="9.6",
                    status="WARN",
                ),
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={},
        effective_label="SAA 2026",
        resolution=_resolution(),
    )
    assert [badge["text"] for badge in group["badges"]] == [
        "1 BREACH",
        "2 WARN",
    ]


def test_group_without_coverage_renders_empty_not_raising() -> None:
    group = _build_family_group(
        family="anlv",
        coverage=None,
        latest_as_of_date=None,
        watch_by_subject={},
        fired={},
        effective_label=None,
        resolution=_resolution(),
    )
    assert group["rows"] == []
    assert group["badges"] == []
    assert group["subject_count"] == 0
    assert group["effective_label"] is None


# ---------------------------------------------------------------------------
# Irene's note — deterministic, no LLM
# ---------------------------------------------------------------------------


def test_calm_note_for_an_ok_subject() -> None:
    note = _irene_note(
        status="OK",
        coverage_pct=Decimal("11.2"),
        max_pct=Decimal("15.0"),
        watch=None,
        fired=False,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=DEFAULT_WARN_THRESHOLD_PCT,
    )
    assert note == "Calm — below the 90% WARN threshold."


def test_fired_note_states_current_status_and_never_a_prior_band() -> None:
    """The FROM→TO edge is not persisted, so the note must not claim one."""
    note = _irene_note(
        status="BREACH",
        coverage_pct=Decimal("5.14"),
        max_pct=Decimal("5.0"),
        watch=None,
        fired=True,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=DEFAULT_WARN_THRESHOLD_PCT,
    )
    assert note == "Fired this beat — now at BREACH."
    assert "rising edge" not in note
    assert "→" not in note


def test_silent_note_uses_the_config_retrigger_delta() -> None:
    """The Δ is the Floor Config value (5.0), not the mock's illustrative 1.5."""
    note = _irene_note(
        status="WARN",
        coverage_pct=Decimal("23.9"),
        max_pct=Decimal("25.0"),
        watch=_watch(
            subject_key="saa:equity",
            acknowledged_magnitude="23.8",
            acknowledged_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        ),
        fired=False,
        re_trigger_delta=DEFAULT_FLOOR_CONFIG.re_trigger_delta["saa"],
        warn_threshold_pct=DEFAULT_WARN_THRESHOLD_PCT,
    )
    assert note == (
        "Silent — WARN acknowledged at 23.80% on 14 Jul 2026; +0.10 pp is "
        "below the 5.0 pp re-trigger."
    )


def test_move_at_or_above_the_retrigger_delta_is_not_silent() -> None:
    """Past the Δ the silence is no longer explained by the acknowledgement."""
    note = _irene_note(
        status="WARN",
        coverage_pct=Decimal("29.0"),
        max_pct=Decimal("30.0"),
        watch=_watch(
            subject_key="saa:equity",
            acknowledged_magnitude="23.8",
            acknowledged_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        ),
        fired=False,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=DEFAULT_WARN_THRESHOLD_PCT,
    )
    assert note == "WARN — not yet reviewed."


def test_non_benign_without_acknowledgement_invents_nothing() -> None:
    note = _irene_note(
        status="BREACH",
        coverage_pct=Decimal("6.0"),
        max_pct=Decimal("5.0"),
        watch=None,
        fired=False,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=DEFAULT_WARN_THRESHOLD_PCT,
    )
    assert note == "BREACH — not yet reviewed."
    assert "acknowledged" not in note


def test_subject_missing_from_watch_state_does_not_raise() -> None:
    group = _build_family_group(
        family="saa",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="equity",
                    max_pct="25.0",
                    coverage_pct="24.0",
                    status="WARN",
                )
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={},
        effective_label="SAA 2026",
        resolution=_resolution(),
    )
    (row,) = group["rows"]
    assert row["note"] == "WARN — not yet reviewed."


def test_fired_row_carries_the_card_anchor() -> None:
    group = _build_family_group(
        family="anlv",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="17",
                    max_pct="5.0",
                    coverage_pct="5.14",
                    status="BREACH",
                )
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={"anlv:17": "abc-123"},
        effective_label="AnlV 2026",
        resolution=_resolution(),
    )
    (row,) = group["rows"]
    assert row["fired_this_beat"] is True
    assert row["finding_anchor"] == "#dc-card-abc-123"


# ---------------------------------------------------------------------------
# Fired-this-beat split
# ---------------------------------------------------------------------------


def test_no_beat_means_nothing_fired() -> None:
    findings = [
        _finding(
            subject_key="saa:equity",
            created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        )
    ]
    assert _fired_this_beat(findings, None) == ({}, {})


def test_only_findings_at_or_after_the_beat_count_as_fired() -> None:
    beat = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    stale = _finding(subject_key="saa:old", created_at=beat - timedelta(days=1))
    fresh = _finding(subject_key="saa:new", created_at=beat)
    by_subject, _by_tag = _fired_this_beat([fresh, stale], beat)
    assert set(by_subject) == {"saa:new"}
    assert by_subject["saa:new"] == str(fresh.id)


def test_rss_tag_is_taken_from_the_finding_payload() -> None:
    beat = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    corroborated = _finding(
        subject_key="saa:private_equity",
        created_at=beat,
        payload={"tag": "private_markets"},
    )
    unknown_tag = _finding(
        subject_key="saa:equity",
        created_at=beat,
        payload={"tag": "not_a_curated_tag"},
    )
    _by_subject, by_tag = _fired_this_beat([corroborated, unknown_tag], beat)
    assert by_tag == {"private_markets": str(corroborated.id)}


# ---------------------------------------------------------------------------
# RSS group — three columns, no silent-cluster claims (D8)
# ---------------------------------------------------------------------------


def test_rss_group_lists_every_curated_tag_sorted() -> None:
    group = _build_rss_group({}, resolution=_resolution())
    assert [row["tag"] for row in group["rows"]] == sorted(_KNOWN_TAGS)


def test_broad_tags_corroborate_nothing() -> None:
    rows = {row["tag"]: row for row in _build_rss_group({}, resolution=_resolution())["rows"]}
    assert rows["macro"]["corroborates"] == []
    assert rows["regulator"]["corroborates"] == []
    assert rows["swiss_finance"]["corroborates"] == []
    assert rows["real_estate"]["corroborates"] == ["real_estate"]


def test_a_tag_without_a_corroborating_finding_carries_no_note() -> None:
    """Empty is the honest v1 output — never a silent-cluster claim (D8)."""
    for row in _build_rss_group({}, resolution=_resolution())["rows"]:
        assert row["note"] == ""
        assert row["fired_this_beat"] is False
        assert row["finding_anchor"] is None


def test_a_corroborating_finding_folds_into_the_tag_note() -> None:
    group = _build_rss_group({"private_markets": "fid-9"}, resolution=_resolution())
    rows = {row["tag"]: row for row in group["rows"]}
    assert rows["private_markets"]["note"] == ("Folded into an open finding as corroboration.")
    assert rows["private_markets"]["finding_anchor"] == "#dc-card-fid-9"
    assert rows["macro"]["note"] == ""


# ---------------------------------------------------------------------------
# Sensitivity overlays in the projection (ADR-0116 §3, §6)
# ---------------------------------------------------------------------------


def _saa_group(*overlays: SubjectOverlay, coverage_pct: str, status: str) -> dict:
    """One SAA row against a 25% ceiling, under the overlays given."""
    return _build_family_group(
        family="saa",
        coverage=_coverage_frame(
            [
                _row(
                    class_key="equity",
                    max_pct="25.0",
                    coverage_pct=coverage_pct,
                    status=status,
                )
            ]
        ),
        latest_as_of_date=date(2026, 6, 30),
        watch_by_subject={},
        fired={},
        effective_label="SAA 2026",
        resolution=_resolution(*overlays),
    )


def test_the_warn_mark_sits_at_the_subject_effective_threshold() -> None:
    """Per-subject *positioned*, never per-row rescaled (ADR-0116 §6)."""
    default_row = _saa_group(coverage_pct="10.0", status="OK")["rows"][0]
    assert default_row["warn_threshold_pct"] == pytest.approx(90.0)

    overridden = _saa_group(
        _overlay("saa:equity", warn_threshold_pct="70"),
        coverage_pct="10.0",
        status="OK",
    )["rows"][0]
    assert overridden["warn_threshold_pct"] == pytest.approx(70.0)
    # The scale itself is untouched: the gauge still runs 0 → ceiling, and
    # the printed figures are the same on both rows.
    assert overridden["max_pct"] == default_row["max_pct"]
    assert overridden["utilisation"] == default_row["utilisation"]


def test_a_warn_override_reclassifies_the_row_the_beat_would_reclassify() -> None:
    """The engine classified at the tenant default; the override corrects it."""
    # 20% of a 25% ceiling is 80% utilisation: OK at the 90% default…
    assert _saa_group(coverage_pct="20.0", status="OK")["rows"][0]["status"] == "OK"
    # …and WARN once this subject's own threshold drops to 70%.
    warned = _saa_group(
        _overlay("saa:equity", warn_threshold_pct="70"),
        coverage_pct="20.0",
        status="OK",
    )["rows"][0]
    assert warned["status"] == "WARN"
    assert warned["modifier"] == "warn"
    assert warned["note"] == "WARN — not yet reviewed."


def test_a_muted_row_stays_visible_tagged_and_counted() -> None:
    """Mute suppresses findings, not truth — the row and its status remain."""
    group = _saa_group(
        _overlay("saa:equity", muted=True),
        coverage_pct="24.0",
        status="WARN",
    )
    (row,) = group["rows"]
    assert row["muted"] is True
    # Live status still rendered, figures untouched.
    assert row["status"] == "WARN"
    assert row["coverage_pct"] == pytest.approx(24.0)
    assert group["subject_count"] == 1
    assert group["muted_count"] == 1
    # The status badge is unchanged by the mute: the group still reports it.
    assert {badge["text"] for badge in group["badges"]} == {"1 WARN"}


def test_the_mute_toggle_is_locked_at_breach_and_free_below_it() -> None:
    """The UI mirror of the beat-side rule (ADR-0116 §3), never its enforcer."""
    breached = _saa_group(coverage_pct="26.0", status="BREACH")["rows"][0]
    assert breached["status"] == "BREACH"
    assert breached["mute_locked"] is True

    calm = _saa_group(coverage_pct="10.0", status="OK")["rows"][0]
    assert calm["mute_locked"] is False


def test_a_customised_subject_is_flagged_in_the_row() -> None:
    plain = _saa_group(coverage_pct="10.0", status="OK")["rows"][0]
    assert plain["warn_customised"] is False
    assert plain["delta_customised"] is False

    tuned = _saa_group(
        _overlay("saa:equity", warn_threshold_pct="70", re_trigger_delta="1.5"),
        coverage_pct="10.0",
        status="OK",
    )["rows"][0]
    assert tuned["warn_customised"] is True
    assert tuned["delta_customised"] is True
    # The note quotes the subject's own Δ, not the family default.
    assert "5.0 pp re-trigger" not in tuned["note"]


def test_a_muted_press_dimension_is_tagged_and_counted() -> None:
    """An rss overlay carries mute alone, keyed by the tag (ADR-0116 §3)."""
    muted_tag = rss_overlay_subject_key("macro")
    group = _build_rss_group({}, resolution=_resolution(_overlay(muted_tag, muted=True)))
    rows = {row["tag"]: row for row in group["rows"]}

    assert rows["macro"]["muted"] is True
    assert rows["macro"]["subject_key"] == "rss:macro"
    # A press cluster has no ceiling to violate, so nothing locks the toggle.
    assert rows["macro"]["mute_locked"] is False
    assert rows["equities"]["muted"] is False
    assert group["muted_count"] == 1


# ---------------------------------------------------------------------------
# Characterization pin — the whole monitor context (P-WD-B1, D-WD-14)
#
# Captured on the tree *before* the family-status lift and compared against
# a literal, so the lift is proven to move code and change no value. The
# three DB collaborators are replaced by fixed stand-ins on the module that
# hosts the monitor; everything between them and the context — the fired
# split, both internal groups, the RSS group, the signal grouping and the
# footer — runs for real.
# ---------------------------------------------------------------------------

_PIN_NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
_PIN_BEAT = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


def _pin_id(n: int) -> UUID:
    return UUID(int=n)


def _pin_overlay(
    subject_key: str,
    n: int,
    *,
    muted: bool = False,
    warn_threshold_pct: str | None = None,
    re_trigger_delta: str | None = None,
) -> SubjectOverlay:
    return SubjectOverlay(
        watchpoint_id=_pin_id(n),
        subject_key=subject_key,
        family=subject_key.split(":", 1)[0],
        display_name=subject_key,
        muted=muted,
        warn_threshold_pct=(
            Decimal(warn_threshold_pct) if warn_threshold_pct is not None else None
        ),
        re_trigger_delta=(Decimal(re_trigger_delta) if re_trigger_delta is not None else None),
        notes=None,
    )


def _pin_watchpoint(
    family: str,
    subject_key: str,
    n: int,
    *,
    threshold_pct: str,
    window_days: int | None = None,
    max_age_days: int | None = None,
    muted: bool = False,
) -> SignalWatchpoint:
    return SignalWatchpoint(
        watchpoint_id=_pin_id(n),
        subject_key=subject_key,
        family=family,
        display_name=subject_key,
        muted=muted,
        warn_threshold_pct=None,
        re_trigger_delta=None,
        instrument_id=_pin_id(n + 100) if family == "price" else None,
        currency_pair="USD/EUR" if family == "fx" else None,
        threshold_pct=Decimal(threshold_pct),
        window_days=window_days,
        notes=None,
        max_age_days=max_age_days,
    )


def _pin_observation(
    subject_key: str, *, magnitude: str, status: str, threshold_pct: str, window_days: int
) -> SignalObservation:
    return SignalObservation(
        subject_key=subject_key,
        magnitude=Decimal(magnitude),
        status=status,
        threshold_pct=Decimal(threshold_pct),
        window_days=window_days,
        reference_value=Decimal("100"),
        reference_date=date(2026, 7, 9),
        latest_value=Decimal("92"),
        latest_date=date(2026, 7, 14),
    )


def _pin_finding(subject_key: str, n: int, created_at: datetime, payload: dict) -> IreneFindingDTO:
    return IreneFindingDTO(
        id=_pin_id(n),
        tenant_id=_pin_id(1),
        subject_key=subject_key,
        payload=payload,
        urgency=7,
        band="noteworthy",
        resolution="open",
        resolved_at=None,
        resolved_by=None,
        created_at=created_at,
    )


def _pin_scenario(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install the stand-ins and return the keyword arguments of one render."""
    price = _pin_watchpoint("price", "price:00000000", 11, threshold_pct="5.0", window_days=5)
    fx = _pin_watchpoint("fx", "fx:USD/EUR", 12, threshold_pct="3.0", window_days=10)
    freshness = _pin_watchpoint(
        "freshness", "freshness:*", 13, threshold_pct="45", max_age_days=45, muted=True
    )
    resolution = WatchDeskResolution(
        config=DEFAULT_FLOOR_CONFIG,
        warn_default_pct=DEFAULT_WARN_THRESHOLD_PCT,
        overlays={
            overlay.subject_key: overlay
            for overlay in (
                _pin_overlay("saa:equity", 21, muted=True),
                _pin_overlay("anlv:fonds", 22, warn_threshold_pct="70", re_trigger_delta="1.5"),
                _pin_overlay(rss_overlay_subject_key("macro"), 23, muted=True),
            )
        },
        signals={wp.subject_key: wp for wp in (price, fx, freshness)},
    )

    saa = _coverage_frame(
        [
            _row(class_key="equity", max_pct="25", coverage_pct="10.0", status="OK"),
            _row(class_key="bonds", max_pct="25", coverage_pct="23.0", status="WARN"),
            _row(class_key="real_estate", max_pct="10", coverage_pct="12.5", status="BREACH"),
            _row(class_key="cash", max_pct=None, coverage_pct="4.0", status="NO_LIMIT"),
            _row(
                class_key="equity",
                max_pct="25",
                coverage_pct="30.0",
                status="BREACH",
                as_of=date(2026, 5, 31),
            ),
        ]
    )
    anlv = _coverage_frame(
        [
            _row(class_key="fonds", max_pct="20", coverage_pct="15.0", status="OK"),
            _row(class_key="zero", max_pct="0", coverage_pct="0", status="OK"),
        ]
    )
    bundle = SimpleNamespace(
        saa=SimpleNamespace(coverage=saa),
        anlv=SimpleNamespace(coverage=anlv),
        latest_as_of_date=date(2026, 6, 30),
    )

    class _CoverageService:
        async def get_coverage(self, *, warn_threshold_pct: Decimal) -> SimpleNamespace:
            assert warn_threshold_pct == resolution.warn_default_pct
            return bundle

    watch_rows = [
        IreneWatchStateDTO(
            id=_pin_id(31),
            tenant_id=_pin_id(1),
            subject_key="saa:bonds",
            magnitude=None,
            band=None,
            acknowledged_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
            acknowledged_magnitude=Decimal("22.4"),
            last_seen_at=_PIN_BEAT,
            created_at=_PIN_BEAT,
            updated_at=_PIN_BEAT,
        ),
        IreneWatchStateDTO(
            id=_pin_id(32),
            tenant_id=_pin_id(1),
            subject_key="fx:USD/EUR",
            magnitude=None,
            band=None,
            acknowledged_at=datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc),
            acknowledged_magnitude=Decimal("1.0"),
            last_seen_at=_PIN_BEAT,
            created_at=_PIN_BEAT,
            updated_at=_PIN_BEAT,
        ),
    ]

    class _WatchStates:
        def __init__(self, session: object) -> None:
            assert session is _PIN_SESSION

        async def list_all(self) -> list[IreneWatchStateDTO]:
            return watch_rows

    observed: list[tuple[SignalWatchpoint, SignalResult]] = [
        (
            price,
            _pin_observation(
                "price:00000000",
                magnitude="8.0",
                status="BREACH",
                threshold_pct="5.0",
                window_days=5,
            ),
        ),
        (fx, NoObservation(subject_key="fx:USD/EUR", reason="no rate observations at all")),
        (
            _pin_watchpoint("freshness", "freshness:" + str(_pin_id(41)), 13, threshold_pct="45"),
            _pin_observation(
                "freshness:" + str(_pin_id(41)),
                magnitude="12",
                status="OK",
                threshold_pct="45",
                window_days=0,
            ),
        ),
        (
            _pin_watchpoint("freshness", "freshness:" + str(_pin_id(42)), 13, threshold_pct="45"),
            _pin_observation(
                "freshness:" + str(_pin_id(42)),
                magnitude="40",
                status="WARN",
                threshold_pct="45",
                window_days=0,
            ),
        ),
    ]

    async def _observe(
        session: object, *, as_of: date, resolution: WatchDeskResolution
    ) -> list[tuple[SignalWatchpoint, SignalResult]]:
        assert session is _PIN_SESSION
        assert as_of == _PIN_NOW.date()
        assert resolution is resolution_given
        return observed

    resolution_given = resolution
    monkeypatch.setattr(_monitor_home, "_build_coverage_service", lambda _s: _CoverageService())
    monkeypatch.setattr(_monitor_home, "IreneWatchStateRepository", _WatchStates)
    monkeypatch.setattr(_monitor_home, "observe_signal_families", _observe)

    findings = [
        _pin_finding("saa:real_estate", 51, _PIN_NOW - timedelta(minutes=30), {}),
        _pin_finding("rss:cluster", 52, _PIN_NOW - timedelta(minutes=20), {"tag": "macro"}),
        _pin_finding("price:00000000", 53, _PIN_NOW - timedelta(minutes=10), {}),
        _pin_finding("saa:bonds", 54, _PIN_BEAT - timedelta(hours=1), {}),
    ]
    return {
        "findings": findings,
        "schedule": SimpleNamespace(last_beat_at=_PIN_BEAT),
        "effective_labels": {"saa": "SAA 2026"},
        "resolution": resolution,
        "now": _PIN_NOW,
    }


_PIN_SESSION = object()

#: Captured from the unchanged tree; see the section comment above.
_PIN_EXPECTED: dict[str, Any] = {
    "coverage_available": True,
    "groups": [
        {
            "family": "saa",
            "name": "SAA limits",
            "badges": [
                {"text": "1 BREACH", "modifier": "breach"},
                {"text": "1 WARN", "modifier": "warn"},
                {"text": "1 OK", "modifier": "ok"},
            ],
            "subject_count": 3,
            "muted_count": 1,
            "effective_label": "SAA 2026",
            "rows": [
                {
                    "subject_key": "saa:equity",
                    "family": "saa",
                    "status": "OK",
                    "modifier": "ok",
                    "coverage_pct": 10.0,
                    "max_pct": 25.0,
                    "utilisation": 40.0,
                    "fill_pct": 40.0,
                    "warn_threshold_pct": 90.0,
                    "muted": True,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-overlay-saa-equity",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "Calm — below the 90% WARN threshold.",
                },
                {
                    "subject_key": "saa:bonds",
                    "family": "saa",
                    "status": "WARN",
                    "modifier": "warn",
                    "coverage_pct": 23.0,
                    "max_pct": 25.0,
                    "utilisation": 92.0,
                    "fill_pct": 92.0,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-overlay-saa-bonds",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "Silent — OK acknowledged at 22.40% on 10 Jul 2026; +0.60 "
                    "pp is below the 5.0 pp re-trigger.",
                },
                {
                    "subject_key": "saa:real_estate",
                    "family": "saa",
                    "status": "BREACH",
                    "modifier": "breach",
                    "coverage_pct": 12.5,
                    "max_pct": 10.0,
                    "utilisation": 125.0,
                    "fill_pct": 100.0,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": True,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-overlay-saa-real-estate",
                    "fired_this_beat": True,
                    "finding_anchor": "#dc-card-00000000-0000-0000-0000-000000000033",
                    "note": "Fired this beat — now at BREACH.",
                },
            ],
        },
        {
            "family": "anlv",
            "name": "AnlV quotas",
            "badges": [{"text": "1 WARN", "modifier": "warn"}, {"text": "1 OK", "modifier": "ok"}],
            "subject_count": 2,
            "muted_count": 0,
            "effective_label": None,
            "rows": [
                {
                    "subject_key": "anlv:fonds",
                    "family": "anlv",
                    "status": "WARN",
                    "modifier": "warn",
                    "coverage_pct": 15.0,
                    "max_pct": 20.0,
                    "utilisation": 75.0,
                    "fill_pct": 75.0,
                    "warn_threshold_pct": 70.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": True,
                    "delta_customised": True,
                    "editor_id": "dc-overlay-anlv-fonds",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "WARN — not yet reviewed.",
                },
                {
                    "subject_key": "anlv:zero",
                    "family": "anlv",
                    "status": "OK",
                    "modifier": "ok",
                    "coverage_pct": 0.0,
                    "max_pct": 0.0,
                    "utilisation": 0.0,
                    "fill_pct": 0.0,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-overlay-anlv-zero",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "Calm — below the 90% WARN threshold.",
                },
            ],
        },
    ],
    "rss": {
        "name": "Press dimensions (RSS)",
        "meta": "7 curated tags · corroboration only, never a source of figures",
        "muted_count": 1,
        "rows": [
            {
                "tag": "credit",
                "subject_key": "rss:credit",
                "family": "rss",
                "corroborates": ["listed_bonds", "private_debt"],
                "muted": False,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-credit",
                "fired_this_beat": False,
                "finding_anchor": None,
                "note": "",
            },
            {
                "tag": "equities",
                "subject_key": "rss:equities",
                "family": "rss",
                "corroborates": ["equities", "listed_equity"],
                "muted": False,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-equities",
                "fired_this_beat": False,
                "finding_anchor": None,
                "note": "",
            },
            {
                "tag": "macro",
                "subject_key": "rss:macro",
                "family": "rss",
                "corroborates": [],
                "muted": True,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-macro",
                "fired_this_beat": True,
                "finding_anchor": "#dc-card-00000000-0000-0000-0000-000000000034",
                "note": "Folded into an open finding as corroboration.",
            },
            {
                "tag": "private_markets",
                "subject_key": "rss:private_markets",
                "family": "rss",
                "corroborates": ["private_equity", "private_debt", "real_estate", "infra_equity"],
                "muted": False,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-private-markets",
                "fired_this_beat": False,
                "finding_anchor": None,
                "note": "",
            },
            {
                "tag": "real_estate",
                "subject_key": "rss:real_estate",
                "family": "rss",
                "corroborates": ["real_estate"],
                "muted": False,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-real-estate",
                "fired_this_beat": False,
                "finding_anchor": None,
                "note": "",
            },
            {
                "tag": "regulator",
                "subject_key": "rss:regulator",
                "family": "rss",
                "corroborates": [],
                "muted": False,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-regulator",
                "fired_this_beat": False,
                "finding_anchor": None,
                "note": "",
            },
            {
                "tag": "swiss_finance",
                "subject_key": "rss:swiss_finance",
                "family": "rss",
                "corroborates": [],
                "muted": False,
                "mute_locked": False,
                "editor_id": "dc-overlay-rss-swiss-finance",
                "fired_this_beat": False,
                "finding_anchor": None,
                "note": "",
            },
        ],
    },
    "signal_groups": [
        {
            "family": "price",
            "name": "Price moves",
            "measure": "adverse move over each watchpoint's window",
            "badges": [{"text": "1 Triggered", "modifier": "triggered"}],
            "subject_count": 1,
            "muted_count": 0,
            "rows": [
                {
                    "subject_key": "price:00000000",
                    "family": "price",
                    "watchpoint_id": "00000000-0000-0000-0000-00000000000b",
                    "display_name": "price:00000000",
                    "status": "Triggered",
                    "modifier": "triggered",
                    "has_data": True,
                    "calm": False,
                    "value": "8.00%",
                    "threshold": "5.00%",
                    "fill_pct": 100.0,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-watchpoint-price-00000000",
                    "fired_this_beat": True,
                    "finding_anchor": "#dc-card-00000000-0000-0000-0000-000000000035",
                    "note": "Fired this beat — now Triggered.",
                }
            ],
            "collapsed_rows": [],
            "collapsed_summary": None,
            "can_add": True,
        },
        {
            "family": "fx",
            "name": "FX moves",
            "measure": "absolute move over each watchpoint's window",
            "badges": [{"text": "1 no data", "modifier": "nodata"}],
            "subject_count": 1,
            "muted_count": 0,
            "rows": [
                {
                    "subject_key": "fx:USD/EUR",
                    "family": "fx",
                    "watchpoint_id": "00000000-0000-0000-0000-00000000000c",
                    "display_name": "fx:USD/EUR",
                    "status": "No data",
                    "modifier": "nodata",
                    "has_data": False,
                    "calm": False,
                    "value": "—",
                    "threshold": "—",
                    "fill_pct": 0.0,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-watchpoint-fx-USD-EUR",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "No data — no rate observations at all.",
                }
            ],
            "collapsed_rows": [],
            "collapsed_summary": None,
            "can_add": True,
        },
        {
            "family": "freshness",
            "name": "NAV freshness",
            "measure": "age of the newest actual NAV",
            "badges": [
                {"text": "1 Approaching", "modifier": "approaching"},
                {"text": "1 Calm", "modifier": "calm"},
            ],
            "subject_count": 2,
            "muted_count": 0,
            "rows": [
                {
                    "subject_key": "freshness:00000000-0000-0000-0000-00000000002a",
                    "family": "freshness",
                    "watchpoint_id": "00000000-0000-0000-0000-00000000000d",
                    "display_name": "freshness:00000000-0000-0000-0000-00000000002a",
                    "status": "Approaching",
                    "modifier": "approaching",
                    "has_data": True,
                    "calm": False,
                    "value": "40 days",
                    "threshold": "45 days",
                    "fill_pct": 88.88888888888889,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-watchpoint-freshness-00000000-0000-0000-0000-00000000002a",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "Approaching — not yet reviewed.",
                }
            ],
            "collapsed_rows": [
                {
                    "subject_key": "freshness:00000000-0000-0000-0000-000000000029",
                    "family": "freshness",
                    "watchpoint_id": "00000000-0000-0000-0000-00000000000d",
                    "display_name": "freshness:00000000-0000-0000-0000-000000000029",
                    "status": "Calm",
                    "modifier": "calm",
                    "has_data": True,
                    "calm": True,
                    "value": "12 days",
                    "threshold": "45 days",
                    "fill_pct": 26.666666666666668,
                    "warn_threshold_pct": 90.0,
                    "muted": False,
                    "mute_locked": False,
                    "warn_customised": False,
                    "delta_customised": False,
                    "editor_id": "dc-watchpoint-freshness-00000000-0000-0000-0000-000000000029",
                    "fired_this_beat": False,
                    "finding_anchor": None,
                    "note": "Calm — below the 90% Approaching mark.",
                }
            ],
            "collapsed_summary": "1 fresh — show all",
            "can_add": False,
        },
    ],
    "signal_footer": [
        {
            "family": "liquidity",
            "name": "Cash coverage",
            "measure": "cash cover of the calls projected inside the horizon",
            "can_add": True,
            "watched": False,
        }
    ],
}


async def test_the_monitor_context_is_pinned_across_the_lift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole ``monitor`` context for one fixed scenario, value for value."""
    context = await _build_monitor(_PIN_SESSION, **_pin_scenario(monkeypatch))
    assert context == _PIN_EXPECTED


# ---------------------------------------------------------------------------
# A switched-off family leaves the monitor (ADR-0133 §2.4)
# ---------------------------------------------------------------------------


async def test_switched_off_families_leave_the_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No group, no press group and no footer line for a family that is off.

    The signal observation is stubbed empty, as the gated observation layer
    returns it for a switched-off family; the footer must still not invite
    an "Add watchpoint" for ``price``.
    """
    session = object()
    switched_off_at = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    disabled = {
        family: DisabledFamily(family=family, effective_from=switched_off_at, changed_by=_TENANT)
        for family in ("saa", "rss", "price")
    }
    resolution = WatchDeskResolution(
        config=DEFAULT_FLOOR_CONFIG,
        warn_default_pct=DEFAULT_WARN_THRESHOLD_PCT,
        overlays={},
        enabled_families=frozenset({"anlv", "fx", "freshness", "liquidity"}),
        disabled_families=disabled,
    )
    bundle = SimpleNamespace(
        saa=SimpleNamespace(
            coverage=_coverage_frame(
                [_row(class_key="equity", max_pct="25", coverage_pct="10.0", status="OK")]
            )
        ),
        anlv=SimpleNamespace(
            coverage=_coverage_frame(
                [_row(class_key="fonds", max_pct="20", coverage_pct="15.0", status="OK")]
            )
        ),
        latest_as_of_date=date(2026, 6, 30),
    )

    class _CoverageService:
        async def get_coverage(self, *, warn_threshold_pct: Decimal) -> SimpleNamespace:
            return bundle

    class _WatchStates:
        def __init__(self, _session: object) -> None:
            pass

        async def list_all(self) -> list[IreneWatchStateDTO]:
            return []

    async def _observe(
        _session: object, *, as_of: date, resolution: WatchDeskResolution
    ) -> list[tuple[SignalWatchpoint, SignalResult]]:
        return []

    monkeypatch.setattr(_monitor_home, "_build_coverage_service", lambda _s: _CoverageService())
    monkeypatch.setattr(_monitor_home, "IreneWatchStateRepository", _WatchStates)
    monkeypatch.setattr(_monitor_home, "observe_signal_families", _observe)

    context = await _build_monitor(
        session,
        findings=[],
        schedule=None,
        effective_labels={"saa": "SAA 2026", "anlv": "AnlV 2026"},
        resolution=resolution,
        now=_PIN_NOW,
    )

    assert [group["family"] for group in context["groups"]] == ["anlv"]
    assert context["rss"] is None
    assert [entry["family"] for entry in context["signal_footer"]] == [
        "fx",
        "freshness",
        "liquidity",
    ]

    # The typed read skips the same families through the same switch.
    status = await read_family_status(session, resolution=resolution, now=_PIN_NOW)
    assert [family_status.family for family_status in status.limits] == ["anlv"]
    assert status.press is None
    assert status.signals == ()


# ---------------------------------------------------------------------------
# Typed family status (ADR-0133 §1.2)
#
# The briefing writer's Part 2 reads the same observation and the same
# classifying helpers the monitor renders from. Each assertion below derives
# its expectation from the pinned monitor context wherever that context holds
# the value, so a typed status that disagrees with the watch strip fails here.
# ---------------------------------------------------------------------------


async def test_the_typed_read_is_the_monitors_own_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every status the typed read states is the one the monitor renders."""
    kwargs = _pin_scenario(monkeypatch)
    status = await read_family_status(
        _PIN_SESSION, resolution=kwargs["resolution"], now=kwargs["now"]
    )

    assert isinstance(status, FamilyStatus)
    assert status.evaluated_at == kwargs["now"]
    assert status.coverage_as_of == date(2026, 6, 30)

    # Limits: the same families, subjects, order and figures as the groups.
    expected_groups = _PIN_EXPECTED["groups"]
    assert [family_status.family for family_status in status.limits] == [
        group["family"] for group in expected_groups
    ]
    for family_status, group in zip(status.limits, expected_groups, strict=True):
        assert [subject.subject_key for subject in family_status.subjects] == [
            row["subject_key"] for row in group["rows"]
        ]
        for subject, row in zip(family_status.subjects, group["rows"], strict=True):
            assert subject.status == row["status"]
            assert float(subject.coverage_pct) == row["coverage_pct"]
            assert (float(subject.max_pct) if subject.max_pct is not None else None) == row[
                "max_pct"
            ]
            assert (
                float(subject.utilisation_pct) if subject.utilisation_pct is not None else None
            ) == row["utilisation"]
            assert float(subject.warn_threshold_pct) == row["warn_threshold_pct"]
            assert subject.muted == row["muted"]

    # Signals: the observation order, which the monitor regroups by family
    # and splits exception-first — so the two orders differ on purpose.
    assert [subject.subject_key for subject in status.signals] == [
        "price:00000000",
        "fx:USD/EUR",
        "freshness:" + str(_pin_id(41)),
        "freshness:" + str(_pin_id(42)),
    ]
    signal_rows = {
        row["subject_key"]: row
        for group in _PIN_EXPECTED["signal_groups"]
        for row in (*group["rows"], *group["collapsed_rows"])
    }
    for subject in status.signals:
        row = signal_rows[subject.subject_key]
        assert subject.status_label == row["status"]
        assert subject.display_name == row["display_name"]
        assert subject.muted == row["muted"]

    price, fx, fresh, approaching = status.signals
    assert price.figure == SignalFigure(
        value=Decimal("8.0"), threshold=Decimal("5.0"), unit=FIGURE_UNIT_PCT
    )
    assert approaching.figure == SignalFigure(
        value=Decimal("40"), threshold=Decimal("45"), unit=FIGURE_UNIT_DAYS
    )
    assert fx.status is None
    assert fx.figure is None
    assert fx.status_label == "No data"
    assert fx.no_data_reason == "no rate observations at all"
    assert price.window_days == 5
    assert fresh.window_days == 0
    assert approaching.window_days == 0
    for observed in (price, fresh, approaching):
        assert observed.as_of == date(2026, 7, 14)
    assert fx.window_days is None
    assert fx.as_of is None

    # Press: the same tags, in the same order, with the same mutes.
    assert status.press is not None
    assert [(press_tag.tag, press_tag.muted) for press_tag in status.press] == [
        (row["tag"], row["muted"]) for row in _PIN_EXPECTED["rss"]["rows"]
    ]


async def test_the_typed_read_touches_no_watch_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live status needs no acknowledgement: watch-state is the monitor's alone."""
    kwargs = _pin_scenario(monkeypatch)

    class _NoWatchStates:
        def __init__(self, _session: object) -> None:
            raise AssertionError("the typed read opened the watch-state repository")

    monkeypatch.setattr(_monitor_home, "IreneWatchStateRepository", _NoWatchStates)

    status = await read_family_status(
        _PIN_SESSION, resolution=kwargs["resolution"], now=kwargs["now"]
    )

    assert len(status.limits) == 2


async def test_the_typed_read_keeps_switched_on_families_when_coverage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable coverage empties a family; it does not switch it off."""
    kwargs = _pin_scenario(monkeypatch)

    class _RefusingCoverageService:
        async def get_coverage(self, *, warn_threshold_pct: Decimal) -> SimpleNamespace:
            raise LimitSetNotEffective("no limit set is in force at the evaluation date")

    monkeypatch.setattr(
        _monitor_home, "_build_coverage_service", lambda _s: _RefusingCoverageService()
    )

    status = await read_family_status(
        _PIN_SESSION, resolution=kwargs["resolution"], now=kwargs["now"]
    )

    assert [family_status.family for family_status in status.limits] == ["saa", "anlv"]
    for family_status in status.limits:
        assert family_status.subjects == ()
    assert status.coverage_as_of is None
    assert len(status.signals) == 4


def test_the_typed_read_takes_only_a_session_a_resolution_and_a_clock() -> None:
    """No findings, schedule or labels: those feed the monitor's projection only."""
    assert list(inspect.signature(read_family_status).parameters) == [
        "db_session",
        "resolution",
        "now",
    ]
