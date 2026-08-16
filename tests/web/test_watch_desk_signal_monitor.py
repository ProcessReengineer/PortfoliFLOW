# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The signal-family monitor groups: projection and rendering (ADR-0116 §6).

Two halves, both DB-free:

* the deterministic projection between an observation and the template —
  the native-language Value/Threshold cells, the gauge arithmetic, the note
  assembly, the exception-first split and the group summaries;
* the rendered fragment itself, which is where the two **vocabulary** rules
  are pinned. A signal family renders Calm / Approaching / Triggered and
  never "breach" (regulatory language, reserved for the quota families),
  and ``liquidity`` renders **ratios** and never the internal 100-scale its
  magnitude is computed on.

The vocabulary assertions are made against ``watch_desk_monitor_signals``
rather than the whole monitor, which is exactly why that fragment exists:
the quota groups legitimately say "breach", so an assertion over the whole
page could only ever be a fuzzy one.

The DB fan-out behind these projections is exercised by the ASGI route
tests in ``test_watch_desk.py``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.repositories.irene_watch_state_repository import IreneWatchStateDTO
from services.analytics.cash_coverage_watch import COVERAGE_THRESHOLD_PCT
from services.analytics.irene_floor import DEFAULT_FLOOR_CONFIG, DEFAULT_WARN_THRESHOLD_PCT
from services.analytics.signal_watch import (
    NoObservation,
    SignalObservation,
    SignalResult,
)
from services.watch_desk.overlay import SignalWatchpoint, WatchDeskResolution
from web.routes.watch_desk import (
    _build_signal_group,
    _signal_figures,
    _signal_footer,
    _signal_note,
)

_TENANT: UUID = uuid4()
_AS_OF = date(2026, 8, 11)


# ---------------------------------------------------------------------------
# Fixtures — plain values, no session
# ---------------------------------------------------------------------------


def _templates() -> Environment:
    return Environment(
        loader=FileSystemLoader("web/templates"),
        autoescape=select_autoescape(["html"]),
    )


def _visible_text(html: str) -> str:
    """Strip tags so an assertion can speak about what a human reads.

    The 100-scale rule is about figures the operator *reads*, so the gauge's
    CSS width — which is the bar's geometry and encodes exactly that
    proportion by design — is deliberately outside it. The "breach" rule is
    the opposite: it is asserted against the raw markup, because a class
    name like ``tag--breach`` would be a reintroduction of the vocabulary
    even though nobody sees the word.
    """
    return re.sub(r"<[^>]+>", " ", html)


def _resolution(*watchpoints: SignalWatchpoint) -> WatchDeskResolution:
    return WatchDeskResolution(
        config=DEFAULT_FLOOR_CONFIG,
        warn_default_pct=DEFAULT_WARN_THRESHOLD_PCT,
        overlays={},
        signals={watchpoint.subject_key: watchpoint for watchpoint in watchpoints},
    )


def _watchpoint(
    *,
    family: str,
    subject_key: str,
    display_name: str = "Subject",
    threshold_pct: str = "5.0",
    window_days: int | None = 5,
    muted: bool = False,
    warn_threshold_pct: str | None = None,
    re_trigger_delta: str | None = None,
    max_age_days: int | None = None,
    horizon_months: int | None = None,
    min_coverage_ratio: str | None = None,
) -> SignalWatchpoint:
    return SignalWatchpoint(
        watchpoint_id=uuid4(),
        subject_key=subject_key,
        family=family,
        display_name=display_name,
        muted=muted,
        warn_threshold_pct=(
            Decimal(warn_threshold_pct) if warn_threshold_pct is not None else None
        ),
        re_trigger_delta=(Decimal(re_trigger_delta) if re_trigger_delta is not None else None),
        instrument_id=None,
        currency_pair="USD/EUR" if family == "fx" else None,
        threshold_pct=Decimal(threshold_pct),
        window_days=window_days,
        notes=None,
        max_age_days=max_age_days,
        horizon_months=horizon_months,
        min_coverage_ratio=(
            Decimal(min_coverage_ratio) if min_coverage_ratio is not None else None
        ),
    )


def _observation(
    *,
    subject_key: str,
    magnitude: str,
    status: str,
    threshold_pct: str,
    window_days: int = 5,
    reference_value: str = "100",
    latest_value: str = "94",
) -> SignalObservation:
    return SignalObservation(
        subject_key=subject_key,
        magnitude=Decimal(magnitude),
        status=status,
        threshold_pct=Decimal(threshold_pct),
        window_days=window_days,
        reference_value=Decimal(reference_value),
        reference_date=date(2026, 8, 6),
        latest_value=Decimal(latest_value),
        latest_date=_AS_OF,
    )


def _watch(
    *, subject_key: str, acknowledged_magnitude: str, acknowledged_at: datetime
) -> IreneWatchStateDTO:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
    return IreneWatchStateDTO(
        id=uuid4(),
        tenant_id=_TENANT,
        subject_key=subject_key,
        magnitude=None,
        band=None,
        acknowledged_at=acknowledged_at,
        acknowledged_magnitude=Decimal(acknowledged_magnitude),
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


def _group(
    family: str,
    pairs: list[tuple[SignalWatchpoint, SignalResult]],
    *,
    watch: dict[str, IreneWatchStateDTO] | None = None,
    fired: dict[str, str] | None = None,
) -> dict:
    return _build_signal_group(
        family,
        pairs,
        watch_by_subject=watch or {},
        fired=fired or {},
        resolution=_resolution(*[watchpoint for watchpoint, _result in pairs]),
    )


# ---------------------------------------------------------------------------
# Native language: each family's Value and Threshold cells
# ---------------------------------------------------------------------------


def test_price_and_fx_cells_speak_percentage_points() -> None:
    watchpoint = _watchpoint(family="price", subject_key="price:x", threshold_pct="5.0")
    result = _observation(
        subject_key="price:x", magnitude="6.2", status="BREACH", threshold_pct="5.0"
    )
    assert _signal_figures(watchpoint, result) == ("6.20%", "5.00%")


def test_freshness_cells_speak_whole_days_and_pluralise() -> None:
    watchpoint = _watchpoint(
        family="freshness",
        subject_key="freshness:*",
        threshold_pct="120",
        window_days=120,
        max_age_days=120,
    )
    result = _observation(
        subject_key="freshness:a",
        magnitude="134",
        status="BREACH",
        threshold_pct="120",
        window_days=120,
    )
    assert _signal_figures(watchpoint, result) == ("134 days", "120 days")

    one_day = _observation(
        subject_key="freshness:a",
        magnitude="1",
        status="OK",
        threshold_pct="120",
        window_days=120,
    )
    assert _signal_figures(watchpoint, one_day)[0] == "1 day"


def test_liquidity_cells_speak_ratios_never_the_hundred_scale() -> None:
    """The operator calibrated a ratio and must read one back (ADR-0116 §4)."""
    watchpoint = _watchpoint(
        family="liquidity",
        subject_key="liquidity:cash_coverage",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        window_days=None,
        horizon_months=12,
        min_coverage_ratio="1.2",
    )
    # 12,000,000 available against 10,000,000 of projected calls → 1.20×.
    result = _observation(
        subject_key="liquidity:cash_coverage",
        magnitude="100",
        status="BREACH",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        reference_value="12000000",
        latest_value="2000000",
    )
    value, threshold = _signal_figures(watchpoint, result)
    assert value == "1.20×"
    assert threshold == "1.20×"


def test_liquidity_with_nothing_to_cover_states_a_sentence_not_a_number() -> None:
    """A ratio over zero calls is undefined; 0.00× would claim a shortfall."""
    watchpoint = _watchpoint(
        family="liquidity",
        subject_key="liquidity:cash_coverage",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        window_days=None,
        horizon_months=12,
        min_coverage_ratio="1.2",
    )
    result = _observation(
        subject_key="liquidity:cash_coverage",
        magnitude="0",
        status="OK",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        reference_value="5000000",
        latest_value="5000000",
    )
    assert _signal_figures(watchpoint, result)[0] == "no calls projected"


# ---------------------------------------------------------------------------
# The note — four branches, signal vocabulary throughout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [("OK", "Calm — below the 90% Approaching mark."), ("BREACH", "Triggered — not yet reviewed.")],
)
def test_note_states_calm_or_an_unreviewed_status(status: str, expected: str) -> None:
    watchpoint = _watchpoint(family="price", subject_key="price:x")
    result = _observation(
        subject_key="price:x", magnitude="6.2", status=status, threshold_pct="5.0"
    )
    note = _signal_note(
        watchpoint=watchpoint,
        result=result,
        watch=None,
        fired=False,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=Decimal("90"),
    )
    assert note == expected


def test_note_explains_a_silence_below_the_re_trigger() -> None:
    watchpoint = _watchpoint(family="price", subject_key="price:x")
    result = _observation(
        subject_key="price:x", magnitude="6.2", status="BREACH", threshold_pct="5.0"
    )
    note = _signal_note(
        watchpoint=watchpoint,
        result=result,
        watch=_watch(
            subject_key="price:x",
            acknowledged_magnitude="6.0",
            acknowledged_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
        ),
        fired=False,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=Decimal("90"),
    )
    assert note.startswith("Silent — acknowledged on 04 Aug 2026")
    assert "re-trigger" in note


def test_a_no_data_note_carries_the_producers_own_reason() -> None:
    """One phrasing, shared with what the beat logged for the same subject."""
    watchpoint = _watchpoint(family="price", subject_key="price:x")
    result = NoObservation(subject_key="price:x", reason="no price observations at all")
    note = _signal_note(
        watchpoint=watchpoint,
        result=result,
        watch=None,
        fired=False,
        re_trigger_delta=Decimal("5.0"),
        warn_threshold_pct=Decimal("90"),
    )
    assert note == "No data — no price observations at all."


# ---------------------------------------------------------------------------
# Group projection: gauges, badges, muted counts, the exception-first split
# ---------------------------------------------------------------------------


def test_the_gauge_runs_zero_to_the_trigger_threshold_and_clamps() -> None:
    """The generalised honesty rule (ADR-0116 §6), on a signal row."""
    watchpoint = _watchpoint(family="price", subject_key="price:x", threshold_pct="5.0")
    below = _observation(subject_key="price:x", magnitude="2.5", status="OK", threshold_pct="5.0")
    crossed = _observation(
        subject_key="price:x", magnitude="7.5", status="BREACH", threshold_pct="5.0"
    )

    calm_row = _group("price", [(watchpoint, below)])["rows"][0]
    assert calm_row["fill_pct"] == pytest.approx(50.0)
    assert calm_row["warn_threshold_pct"] == pytest.approx(90.0)

    crossed_row = _group("price", [(watchpoint, crossed)])["rows"][0]
    # Clamped fill, honest figures.
    assert crossed_row["fill_pct"] == pytest.approx(100.0)
    assert crossed_row["value"] == "7.50%"
    assert crossed_row["threshold"] == "5.00%"


def test_the_mark_moves_per_subject_but_the_scale_does_not() -> None:
    tight = _watchpoint(
        family="price", subject_key="price:x", threshold_pct="5.0", warn_threshold_pct="60"
    )
    result = _observation(
        subject_key="price:x", magnitude="2.5", status="WARN", threshold_pct="5.0"
    )
    row = _group("price", [(tight, result)])["rows"][0]
    assert row["warn_threshold_pct"] == pytest.approx(60.0)
    # Same magnitude, same threshold → same fill. Only the mark moved.
    assert row["fill_pct"] == pytest.approx(50.0)
    assert row["warn_customised"] is True


def test_a_no_data_row_draws_no_gauge_and_is_not_calm() -> None:
    watchpoint = _watchpoint(family="price", subject_key="price:x")
    result = NoObservation(subject_key="price:x", reason="no price observations at all")
    row = _group("price", [(watchpoint, result)])["rows"][0]

    assert row["has_data"] is False
    assert row["calm"] is False
    assert row["status"] == "No data"
    assert row["modifier"] == "nodata"
    assert row["fill_pct"] == 0.0
    assert row["value"] == "—"


def test_group_badges_and_muted_count_cover_every_row() -> None:
    triggered = _watchpoint(family="price", subject_key="price:a", display_name="A")
    approaching = _watchpoint(family="price", subject_key="price:b", display_name="B", muted=True)
    blind = _watchpoint(family="price", subject_key="price:c", display_name="C")
    group = _group(
        "price",
        [
            (
                triggered,
                _observation(
                    subject_key="price:a", magnitude="6", status="BREACH", threshold_pct="5.0"
                ),
            ),
            (
                approaching,
                _observation(
                    subject_key="price:b", magnitude="4.6", status="WARN", threshold_pct="5.0"
                ),
            ),
            (blind, NoObservation(subject_key="price:c", reason="no price observations at all")),
        ],
    )

    assert group["subject_count"] == 3
    assert group["muted_count"] == 1
    assert [badge["text"] for badge in group["badges"]] == [
        "1 Triggered",
        "1 Approaching",
        "1 no data",
    ]
    assert group["can_add"] is True


def test_freshness_lists_exceptions_and_collapses_the_calm_remainder() -> None:
    """Subjects grow with the book, so the calm ones collapse — never drop."""
    singleton = _watchpoint(
        family="freshness",
        subject_key="freshness:*",
        threshold_pct="120",
        window_days=120,
        max_age_days=120,
    )
    pairs: list[tuple[SignalWatchpoint, SignalResult]] = []
    for index in range(3):
        subject = f"freshness:{index}"
        pairs.append(
            (
                _watchpoint(
                    family="freshness",
                    subject_key=subject,
                    display_name=f"Fund {index}",
                    threshold_pct="120",
                    window_days=120,
                    max_age_days=120,
                ),
                _observation(
                    subject_key=subject,
                    magnitude="10",
                    status="OK",
                    threshold_pct="120",
                    window_days=120,
                ),
            )
        )
    pairs.append(
        (
            singleton,
            _observation(
                subject_key="freshness:stale",
                magnitude="134",
                status="BREACH",
                threshold_pct="120",
                window_days=120,
            ),
        )
    )

    group = _group("freshness", pairs)
    assert group["subject_count"] == 4
    assert len(group["rows"]) == 1
    assert len(group["collapsed_rows"]) == 3
    assert group["collapsed_summary"] == "3 fresh — show all"
    # Not an add family: the singleton already exists if the group rendered.
    assert group["can_add"] is False


def test_the_other_families_list_every_row_openly() -> None:
    watchpoint = _watchpoint(family="fx", subject_key="fx:USD/EUR", threshold_pct="3.0")
    group = _group(
        "fx",
        [
            (
                watchpoint,
                _observation(
                    subject_key="fx:USD/EUR", magnitude="0.4", status="OK", threshold_pct="3.0"
                ),
            )
        ],
    )
    assert len(group["rows"]) == 1
    assert group["collapsed_rows"] == []
    assert group["collapsed_summary"] is None


# ---------------------------------------------------------------------------
# The footer: which absent families may be added
# ---------------------------------------------------------------------------


def test_the_footer_names_every_absent_family_in_the_shared_order() -> None:
    footer = _signal_footer(set(), resolution=_resolution())
    assert [entry["family"] for entry in footer] == ["price", "fx", "freshness", "liquidity"]
    assert all(entry["can_add"] for entry in footer)


def test_a_watched_singleton_with_nothing_to_show_is_not_an_invitation() -> None:
    """The singleton rule is one *live* identity — the footer must not offer a second."""
    singleton = _watchpoint(
        family="freshness",
        subject_key="freshness:*",
        threshold_pct="120",
        window_days=120,
        max_age_days=120,
    )
    footer = _signal_footer({"price", "fx", "liquidity"}, resolution=_resolution(singleton))
    assert len(footer) == 1
    assert footer[0]["family"] == "freshness"
    assert footer[0]["watched"] is True
    assert footer[0]["can_add"] is False


def test_a_rendered_family_gets_no_footer_line() -> None:
    footer = _signal_footer({"price", "fx", "freshness", "liquidity"}, resolution=_resolution())
    assert footer == []


# ---------------------------------------------------------------------------
# The rendered fragment: columns, gauges, collapse, and the two vocabularies
# ---------------------------------------------------------------------------


def _render(groups: list[dict], footer: list[dict] | None = None) -> str:
    return (
        _templates()
        .get_template("_partials/watch_desk_monitor_signals.html")
        .render(monitor={"signal_groups": groups, "signal_footer": footer or []}, csrf_token="t")
    )


def test_the_fragment_renders_the_six_columns_and_the_gauge() -> None:
    watchpoint = _watchpoint(family="price", subject_key="price:x", display_name="MSCI World ETF")
    group = _group(
        "price",
        [
            (
                watchpoint,
                _observation(
                    subject_key="price:x", magnitude="6.2", status="BREACH", threshold_pct="5.0"
                ),
            )
        ],
    )
    html = _render([group])

    for column in ("Subject", "Status", "Value", "Threshold", "Proximity", "Note"):
        assert f">{column}<" in html
    assert "MSCI World ETF" in html
    assert "Triggered" in html
    assert "6.20%" in html
    assert "gauge--triggered" in html
    assert 'style="width:100.0%"' in html
    assert "/api/watch-desk/watchpoints/" in html


def test_the_fragment_never_says_breach_for_a_signal_family() -> None:
    """Asserted on the raw markup: a class name is a reintroduction too."""
    price = _group(
        "price",
        [
            (
                _watchpoint(family="price", subject_key="price:x"),
                _observation(
                    subject_key="price:x", magnitude="6.2", status="BREACH", threshold_pct="5.0"
                ),
            )
        ],
    )
    warn = _group(
        "fx",
        [
            (
                _watchpoint(family="fx", subject_key="fx:USD/EUR", threshold_pct="3.0"),
                _observation(
                    subject_key="fx:USD/EUR", magnitude="2.8", status="WARN", threshold_pct="3.0"
                ),
            )
        ],
    )
    html = _render([price, warn])

    assert "breach" not in html.lower()
    assert "Triggered" in html
    assert "Approaching" in html


def test_the_liquidity_group_never_prints_the_hundred_scale() -> None:
    """The magnitude is arithmetic; the ratio is what the operator set."""
    watchpoint = _watchpoint(
        family="liquidity",
        subject_key="liquidity:cash_coverage",
        display_name="Cash coverage of projected calls",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        window_days=None,
        horizon_months=12,
        min_coverage_ratio="1.2",
    )
    # 9,000,000 available against 10,000,000 of calls → 0.90× against a
    # 1.20× floor, which is magnitude 133.3333 on the internal scale.
    result = _observation(
        subject_key="liquidity:cash_coverage",
        magnitude="133.3333",
        status="BREACH",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        reference_value="9000000",
        latest_value="-1000000",
    )
    html = _render([_group("liquidity", [(watchpoint, result)])])
    text = _visible_text(html)

    assert "0.90×" in text
    assert "1.20×" in text
    assert "133" not in text
    assert "breach" not in html.lower()


def test_the_collapsed_freshness_remainder_is_present_and_expandable() -> None:
    """Collapsed, never hidden — the honesty rule is visibility on demand."""
    calm = _watchpoint(
        family="freshness",
        subject_key="freshness:a",
        display_name="Quiet Fund",
        threshold_pct="120",
        window_days=120,
        max_age_days=120,
    )
    stale = _watchpoint(
        family="freshness",
        subject_key="freshness:b",
        display_name="Stale Fund",
        threshold_pct="120",
        window_days=120,
        max_age_days=120,
    )
    group = _group(
        "freshness",
        [
            (
                calm,
                _observation(
                    subject_key="freshness:a",
                    magnitude="3",
                    status="OK",
                    threshold_pct="120",
                    window_days=120,
                ),
            ),
            (
                stale,
                _observation(
                    subject_key="freshness:b",
                    magnitude="134",
                    status="BREACH",
                    threshold_pct="120",
                    window_days=120,
                ),
            ),
        ],
    )
    html = _render([group])

    assert "Stale Fund" in html
    # The calm row is in the document, behind the summary — never omitted.
    assert "Quiet Fund" in html
    assert "1 fresh — show all" in html
    assert "<details" in html


def test_a_signal_group_renders_closed_with_its_rows_still_in_the_document() -> None:
    """Closed, never hidden — the summary line is the decision surface."""
    watchpoint = _watchpoint(family="price", subject_key="price:x", display_name="MSCI World ETF")
    group = _group(
        "price",
        [
            (
                watchpoint,
                _observation(
                    subject_key="price:x", magnitude="6.2", status="BREACH", threshold_pct="5.0"
                ),
            )
        ],
    )
    html = _render([group])

    assert '<details class="pf-dc-group pf-dc-group--signal">' in html
    assert 'pf-dc-group--signal" open' not in html
    assert "MSCI World ETF" in html


def test_a_no_data_row_renders_its_reason_and_no_gauge() -> None:
    watchpoint = _watchpoint(
        family="liquidity",
        subject_key="liquidity:cash_coverage",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        window_days=None,
        horizon_months=12,
        min_coverage_ratio="1.2",
    )
    group = _group(
        "liquidity",
        [
            (
                watchpoint,
                NoObservation(
                    subject_key="liquidity:cash_coverage",
                    reason="no materialised cash plan path projects past 2026-08-11",
                ),
            )
        ],
    )
    html = _render([group])

    assert "tag--nodata" in html
    assert "No data" in html
    assert "no materialised cash plan path" in html
    assert "gauge__fill" not in html


def test_an_empty_family_gets_a_footer_line_with_its_add_affordance() -> None:
    footer = _signal_footer({"freshness", "liquidity"}, resolution=_resolution())
    html = _render([], footer)

    assert "Price moves" in html
    assert "FX moves" in html
    assert "+ Add watchpoint" in html
    assert "/api/watch-desk/watchpoints/new?family=price" in html
    assert 'id="dc-watchpoint-new"' in html


def test_a_watched_but_empty_singleton_offers_no_add_button() -> None:
    singleton = _watchpoint(
        family="liquidity",
        subject_key="liquidity:cash_coverage",
        threshold_pct=str(COVERAGE_THRESHOLD_PCT),
        window_days=None,
        horizon_months=12,
        min_coverage_ratio="1.2",
    )
    footer = _signal_footer({"price", "fx", "freshness"}, resolution=_resolution(singleton))
    html = _render([], footer)

    assert "Cash coverage" in html
    assert "watched, but nothing to show yet" in html
    assert "+ Add watchpoint" not in html
