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

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.repositories.irene_finding_repository import IreneFindingDTO
from core.repositories.irene_watch_state_repository import IreneWatchStateDTO
from services.analytics.irene_floor import DEFAULT_FLOOR_CONFIG, DEFAULT_WARN_THRESHOLD_PCT
from services.watch_desk.overlay import (
    SubjectOverlay,
    WatchDeskResolution,
    rss_overlay_subject_key,
)
from services.web_research.allowlist import _KNOWN_TAGS
from web.routes.watch_desk import (
    _build_family_group,
    _build_rss_group,
    _fired_this_beat,
    _irene_note,
    _utilisation_pct,
)

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
