# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the briefing writer's material (ADR-0133 §1.2, §1.3).

Eleven pure tests drive :func:`compose_briefing_material` (and, with the
three reads faked, :func:`gather_briefing_material`) without a database.
One live-DB test round-trips a gathered ``inputs`` through an
``irene_briefing`` row.

The status records handed to the composer are kept consistent with the
resolution — no record for a switched-off family and ``press=None`` while
``rss`` is off — because that is what ``read_family_status`` guarantees.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import services.irene.briefing_material as material_module
from core.models.watchpoint import WATCHPOINT_FAMILIES
from core.repositories import tenant_context
from core.repositories.irene_briefing_repository import IreneBriefingRepository
from core.repositories.irene_finding_repository import IreneFindingDTO
from core.repositories.watch_family_scope_repository import WatchFamilyScopeRepository
from services.analytics.irene_floor import DEFAULT_FLOOR_CONFIG, DEFAULT_WARN_THRESHOLD_PCT
from services.irene.briefing_material import (
    BriefingMaterial,
    compose_briefing_material,
    gather_briefing_material,
)
from services.irene.rss_clustering import UNTAGGED_DIMENSION, RssBucket, RssItemRef
from services.watch_desk.family_status import (
    FIGURE_UNIT_PCT,
    FamilyStatus,
    LimitFamilyStatus,
    LimitSubjectStatus,
    PressTagStatus,
    SignalFigure,
    SignalSubjectStatus,
)
from services.watch_desk.market_read import (
    AssetClassMove,
    FxMove,
    InstrumentMove,
    MarketRead,
)
from services.watch_desk.overlay import (
    DisabledFamily,
    SubjectOverlay,
    WatchDeskResolution,
    resolve_watch_desk,
    rss_overlay_subject_key,
)
from tests.services.irene._book_fixtures import seed_user

_NOW = datetime(2026, 10, 7, 6, 30, tzinfo=timezone.utc)
_AS_OF = _NOW.date()
_WINDOW_START = date(2026, 10, 2)
_DAY = date(2026, 10, 6)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _overlay(subject_key: str, *, muted: bool) -> SubjectOverlay:
    """Build one sensitivity overlay for a derived subject."""
    return SubjectOverlay(
        watchpoint_id=uuid4(),
        subject_key=subject_key,
        family=subject_key.split(":", 1)[0],
        display_name=subject_key,
        muted=muted,
        warn_threshold_pct=None,
        re_trigger_delta=None,
        notes=None,
    )


def _resolution(
    *, disabled: Iterable[str] = (), muted_tags: Iterable[str] = ()
) -> WatchDeskResolution:
    """Build a resolution over the code defaults with a family scope and press mutes."""
    off = frozenset(disabled)
    overlays = [_overlay(rss_overlay_subject_key(tag), muted=True) for tag in muted_tags]
    return WatchDeskResolution(
        config=DEFAULT_FLOOR_CONFIG,
        warn_default_pct=DEFAULT_WARN_THRESHOLD_PCT,
        overlays={overlay.subject_key: overlay for overlay in overlays},
        enabled_families=frozenset(WATCHPOINT_FAMILIES) - off,
        disabled_families=MappingProxyType(
            {
                family: DisabledFamily(
                    family=family,
                    effective_from=_NOW - timedelta(days=1),
                    changed_by=uuid4(),
                )
                for family in off
            }
        ),
    )


def _status(
    resolution: WatchDeskResolution,
    *,
    limits: tuple[LimitFamilyStatus, ...] = (),
    signals: tuple[SignalSubjectStatus, ...] = (),
    coverage_as_of: date | None = None,
) -> FamilyStatus:
    """Build a family status consistent with the resolution's press switch."""
    press = (
        tuple(
            PressTagStatus(
                tag=tag,
                subject_key=rss_overlay_subject_key(tag),
                muted=resolution.is_muted(rss_overlay_subject_key(tag)),
            )
            for tag in ("macro", "equities")
        )
        if resolution.is_family_enabled("rss")
        else None
    )
    return FamilyStatus(
        evaluated_at=_NOW,
        coverage_as_of=coverage_as_of,
        limits=limits,
        signals=signals,
        press=press,
    )


def _market(
    *,
    instruments: tuple[InstrumentMove, ...] = (),
    fx: tuple[FxMove, ...] = (),
    asset_classes: tuple[AssetClassMove, ...] = (),
) -> MarketRead:
    """Build a market read over the fixed window ending at the check's date."""
    return MarketRead(
        as_of=_AS_OF,
        window_start=_WINDOW_START,
        window_days=5,
        instruments=instruments,
        fx=fx,
        asset_classes=asset_classes,
    )


def _instrument(value: str = "1.5000", *, investment_id: UUID | None = None) -> InstrumentMove:
    """Build one instrument move."""
    investment = investment_id or uuid4()
    return InstrumentMove(
        id=f"instrument:{investment}",
        label="Global Equity Fund",
        value=Decimal(value),
        unit="pct",
        as_of=_AS_OF,
        window_start=_WINDOW_START,
        window_days=5,
        investment_id=investment,
        asset_class_id=uuid4(),
        currency="EUR",
        reference_date=_WINDOW_START,
        reference_price=Decimal("100"),
        latest_date=_AS_OF,
        latest_price=Decimal("101.5"),
    )


def _fx(value: str = "-0.4000") -> FxMove:
    """Build one FX leg."""
    return FxMove(
        id="fx:USD/EUR",
        label="USD/EUR",
        value=Decimal(value),
        unit="pct",
        as_of=_AS_OF,
        window_start=_WINDOW_START,
        window_days=5,
        base_currency="USD",
        quote_currency="EUR",
        reference_date=_WINDOW_START,
        reference_rate=Decimal("0.92"),
        latest_date=_AS_OF,
        latest_rate=Decimal("0.91632"),
    )


def _asset_class(asset_class_id: UUID) -> AssetClassMove:
    """Build one value-weighted asset-class move."""
    return AssetClassMove(
        id=f"asset_class:{asset_class_id}",
        label="Equities",
        value=Decimal("0.7500"),
        unit="pct",
        as_of=_AS_OF,
        window_start=_WINDOW_START,
        window_days=5,
        asset_class_id=asset_class_id,
        instrument_count=3,
        weight_total=Decimal("1250000"),
        functional_currency="EUR",
    )


def _item(url: str, title: str, source: str, hour: int) -> RssItemRef:
    """Build one clustered feed item."""
    return RssItemRef(
        url=url,
        title=title,
        source_name=source,
        published_at=datetime(2026, 10, 6, hour, tzinfo=timezone.utc),
    )


def _bucket(tag: str, *members: RssItemRef) -> RssBucket:
    """Build one press cluster on a tag."""
    return RssBucket(
        tag=tag,
        day_bucket=_DAY,
        members=members or (_item(f"https://example.test/{tag}", f"{tag} story", "ECB", 8),),
        subject_key=f"rss:cluster:{tag}",
    )


def _limit_subject(family: str, class_key: str) -> LimitSubjectStatus:
    """Build one classified limit subject."""
    return LimitSubjectStatus(
        subject_key=f"{family}:{class_key}",
        family=family,
        class_key=class_key,
        status="WARN",
        coverage_pct=Decimal("18.5"),
        max_pct=Decimal("20.0"),
        utilisation_pct=Decimal("92.5"),
        warn_threshold_pct=Decimal("90.0"),
        muted=False,
    )


def _signal(
    family: str, subject_key: str, *, threshold: Decimal = Decimal("2.0")
) -> SignalSubjectStatus:
    """Build one observed signal subject."""
    return SignalSubjectStatus(
        subject_key=subject_key,
        family=family,
        display_name=subject_key,
        status="OK",
        status_label="Calm",
        figure=SignalFigure(value=Decimal("0.4"), threshold=threshold, unit=FIGURE_UNIT_PCT),
        window_days=5,
        as_of=_AS_OF,
        no_data_reason=None,
        muted=False,
    )


def _finding(payload: dict[str, Any]) -> IreneFindingDTO:
    """Build one finding this check appended."""
    return IreneFindingDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        subject_key="saa:equities",
        payload=payload,
        urgency=3,
        band="elevated",
        resolution="open",
        resolved_at=None,
        resolved_by=None,
        created_at=_NOW,
    )


def _compose(
    resolution: WatchDeskResolution,
    *,
    market: MarketRead | None = None,
    status: FamilyStatus | None = None,
    rss_buckets: tuple[RssBucket, ...] = (),
    findings: tuple[IreneFindingDTO, ...] = (),
    open_counts: dict[str, int] | None = None,
) -> BriefingMaterial:
    """Compose with empty defaults for whatever a test does not state."""
    return compose_briefing_material(
        market=market or _market(),
        status=status or _status(resolution),
        rss_buckets=rss_buckets,
        findings=findings,
        open_counts=open_counts or {},
        resolution=resolution,
        now=_NOW,
    )


def _cluster_ids(material: BriefingMaterial) -> list[str]:
    """Return the ids of the press clusters Part 1 carries."""
    return [cluster["id"] for cluster in material.inputs["markets"]["press_clusters"]]


# ---------------------------------------------------------------------------
# Part 1 — press clusters
# ---------------------------------------------------------------------------


def test_macro_and_regulator_clusters_reach_part_one_with_press_coverage_off() -> None:
    """D-WD-2: the two policy tags are briefed even with Press coverage off."""
    resolution = _resolution(disabled=("rss",))
    buckets = (_bucket("regulator"), _bucket("equities"), _bucket("macro"))

    material = _compose(resolution, rss_buckets=buckets)

    assert _cluster_ids(material) == ["rss:cluster:regulator", "rss:cluster:macro"]


def test_macro_and_regulator_clusters_reach_part_one_when_their_tag_is_muted() -> None:
    """A mute suppresses findings, never the two policy tags' briefing."""
    resolution = _resolution(muted_tags=("macro", "regulator"))
    buckets = (_bucket("macro"), _bucket("regulator"))

    material = _compose(resolution, rss_buckets=buckets)

    assert _cluster_ids(material) == ["rss:cluster:macro", "rss:cluster:regulator"]


def test_other_tags_reach_part_one_only_while_press_is_on_and_the_tag_unmuted() -> None:
    """D-WD-19: every other curated tag needs Press on and no mute."""
    buckets = (_bucket("equities"), _bucket("credit"))

    on = _compose(_resolution(muted_tags=("credit",)), rss_buckets=buckets)
    assert _cluster_ids(on) == ["rss:cluster:equities"]

    off = _compose(_resolution(disabled=("rss",), muted_tags=("credit",)), rss_buckets=buckets)
    assert _cluster_ids(off) == []


def test_untagged_clusters_follow_the_press_switch() -> None:
    """D-B-103: the never-mutable residue is briefed exactly while Press is on."""
    buckets = (_bucket(UNTAGGED_DIMENSION),)

    assert _cluster_ids(_compose(_resolution(), rss_buckets=buckets)) == [
        f"rss:cluster:{UNTAGGED_DIMENSION}"
    ]
    assert _cluster_ids(_compose(_resolution(disabled=("rss",)), rss_buckets=buckets)) == []


def test_a_cluster_carries_its_key_tag_day_and_items() -> None:
    """The cluster record: key, tag, ISO day, members in order, ``rss`` source."""
    bucket = _bucket(
        "macro",
        _item("https://ecb.test/a", "Rates held", "ECB", 7),
        _item("https://ft.test/b", "ECB holds rates", "FT", 9),
    )

    material = _compose(_resolution(), rss_buckets=(bucket,))

    assert material.inputs["markets"]["press_clusters"] == [
        {
            "id": "rss:cluster:macro",
            "tag": "macro",
            "day": "2026-10-06",
            "source": "rss",
            "items": [
                {
                    "title": "Rates held",
                    "source": "ECB",
                    "url": "https://ecb.test/a",
                    "published_at": "2026-10-06T07:00:00+00:00",
                },
                {
                    "title": "ECB holds rates",
                    "source": "FT",
                    "url": "https://ft.test/b",
                    "published_at": "2026-10-06T09:00:00+00:00",
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Part 1 — market figures
# ---------------------------------------------------------------------------


def test_market_figures_carry_value_unit_window_date_and_source() -> None:
    """D-B-105: label, value, unit, window and date — moves, never levels."""
    investment_id = uuid4()
    asset_class_id = uuid4()
    market = _market(
        instruments=(_instrument("1.5000", investment_id=investment_id),),
        fx=(_fx("-0.4000"),),
        asset_classes=(_asset_class(asset_class_id),),
    )

    markets = _compose(_resolution(), market=market).inputs["markets"]

    window = {"as_of": "2026-10-07", "window_start": "2026-10-02", "window_days": 5}
    assert {key: markets[key] for key in window} == window
    assert markets["instruments"] == [
        {
            "id": f"instrument:{investment_id}",
            "label": "Global Equity Fund",
            "value": "1.5000",
            "unit": "pct",
            **window,
            "source": "instrument_prices",
        }
    ]
    assert markets["currencies"] == [
        {
            "id": "fx:USD/EUR",
            "label": "USD/EUR",
            "value": "-0.4000",
            "unit": "pct",
            **window,
            "source": "fx_rates",
        }
    ]
    assert markets["asset_classes"] == [
        {
            "id": f"asset_class:{asset_class_id}",
            "label": "Equities",
            "value": "0.7500",
            "unit": "pct",
            **window,
            "source": "derived",
            "instrument_count": 3,
        }
    ]
    for figure in (*markets["instruments"], *markets["currencies"], *markets["asset_classes"]):
        assert not any(key.startswith(("reference_", "latest_")) for key in figure)


# ---------------------------------------------------------------------------
# Part 2 — watch
# ---------------------------------------------------------------------------


def _family_values(node: Any) -> list[str]:
    """Collect every value of a ``family`` key anywhere in the material."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "family" and isinstance(value, str):
                found.append(value)
            found.extend(_family_values(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_family_values(value))
    return found


def test_part_two_omits_switched_off_families() -> None:
    """ADR-0133 §1.2: a switched-off family is absent, not marked."""
    resolution = _resolution(disabled=("saa", "rss", "price"))
    status = _status(
        resolution,
        limits=(LimitFamilyStatus(family="anlv", subjects=(_limit_subject("anlv", "equities"),)),),
        signals=(_signal("fx", "fx:USD/EUR"),),
        coverage_as_of=_AS_OF,
    )
    counts = {family: index + 1 for index, family in enumerate(WATCHPOINT_FAMILIES)}

    watch = _compose(resolution, status=status, open_counts=counts).inputs["watch"]

    assert watch["families_on"] == ["anlv", "fx", "freshness", "liquidity"]
    assert [group["family"] for group in watch["limits"]] == ["anlv"]
    assert "press_tags" not in watch
    assert watch["open_findings"] == {
        "anlv": counts["anlv"],
        "fx": counts["fx"],
        "freshness": counts["freshness"],
        "liquidity": counts["liquidity"],
    }
    assert list(watch["open_findings"]) == ["anlv", "fx", "freshness", "liquidity"]
    for off in ("saa", "rss", "price"):
        assert off not in _family_values(watch)
        assert off not in watch["open_findings"]


def test_findings_of_this_check_carry_band_and_their_own_words_only() -> None:
    """D-B-107: band, urgency and the finding's own words — nothing else."""
    finding = _finding(
        {
            "trigger": "Equities crossed the WARN threshold",
            "finding": "Equities at 92.5 % of the ceiling.",
            "basis": ["coverage"],
            "options": ["rebalance"],
            "members": [{"url": "https://example.test"}],
            "urgency_suggestion": 4,
        }
    )
    unworded = _finding({"trigger": 7, "finding": "Kept."})

    records = _compose(_resolution(), findings=(finding, unworded)).inputs["watch"][
        "findings_this_check"
    ]

    assert records[0] == {
        "id": str(finding.id),
        "subject_key": "saa:equities",
        "band": "elevated",
        "urgency": 3,
        "trigger": "Equities crossed the WARN threshold",
        "finding": "Equities at 92.5 % of the ceiling.",
    }
    assert records[1]["trigger"] is None
    assert records[1]["finding"] == "Kept."


# ---------------------------------------------------------------------------
# Serialisation and the nothing-to-write case
# ---------------------------------------------------------------------------


def _assert_json_native(node: Any) -> None:
    """Walk the material; every node is a JSON-native type."""
    assert node is None or isinstance(node, (dict, list, str, int, bool)), type(node)
    if isinstance(node, dict):
        for key, value in node.items():
            assert isinstance(key, str)
            _assert_json_native(value)
    elif isinstance(node, list):
        for value in node:
            _assert_json_native(value)


def test_the_material_is_json_native_and_states_decimals_exactly() -> None:
    """D-B-108: fixed-point strings, ISO dates, string ids — JSON round-trips."""
    resolution = _resolution()
    limit = LimitSubjectStatus(
        subject_key="saa:cash",
        family="saa",
        class_key="cash",
        status="OK",
        coverage_pct=Decimal("3.0"),
        max_pct=None,
        utilisation_pct=None,
        warn_threshold_pct=Decimal("90.0"),
        muted=False,
    )
    status = _status(
        resolution,
        limits=(LimitFamilyStatus(family="saa", subjects=(limit,)),),
        signals=(_signal("price", "price:fund", threshold=Decimal("1E+1")),),
        coverage_as_of=_AS_OF,
    )

    material = _compose(
        resolution,
        market=_market(instruments=(_instrument("-1.2340"),), fx=(_fx(),)),
        status=status,
        rss_buckets=(_bucket("macro"),),
        findings=(_finding({"trigger": "t", "finding": "f"}),),
        open_counts={"saa": 1},
    )
    inputs = material.inputs

    assert json.loads(json.dumps(inputs)) == inputs
    assert inputs["markets"]["instruments"][0]["value"] == "-1.2340"
    assert inputs["watch"]["signals"][0]["threshold"] == "10"
    subject = inputs["watch"]["limits"][0]["subjects"][0]
    assert subject["max_pct"] is None
    assert subject["utilisation_pct"] is None
    _assert_json_native(inputs)


def test_nothing_to_write_only_when_every_source_is_empty() -> None:
    """ADR-0133 §1.3: no move, no cluster and no switched-on family — no call."""
    all_off = _resolution(disabled=WATCHPOINT_FAMILIES)

    assert _compose(all_off).nothing_to_write is True

    assert _compose(all_off, market=_market(instruments=(_instrument(),))).nothing_to_write is False
    assert _compose(all_off, market=_market(fx=(_fx(),))).nothing_to_write is False
    assert _compose(all_off, rss_buckets=(_bucket("macro"),)).nothing_to_write is False

    one_on = _resolution(disabled=tuple(f for f in WATCHPOINT_FAMILIES if f != "freshness"))
    assert _compose(one_on, status=_status(one_on)).nothing_to_write is False


# ---------------------------------------------------------------------------
# The gatherer
# ---------------------------------------------------------------------------


async def test_gather_reads_each_source_once_on_the_beats_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three reads, each once, on the beat's ``now`` and resolution."""
    resolution = _resolution()
    market = _market(instruments=(_instrument(),))
    status = _status(resolution)
    counts = {family: 2 for family in WATCHPOINT_FAMILIES}
    buckets = (_bucket("macro"),)
    findings = (_finding({"trigger": "t", "finding": "f"}),)
    session = cast(AsyncSession, object())

    market_calls: list[dict[str, Any]] = []
    status_calls: list[dict[str, Any]] = []
    count_calls: list[AsyncSession] = []

    async def fake_read_market(called_with: AsyncSession, *, as_of: date) -> MarketRead:
        market_calls.append({"session": called_with, "as_of": as_of})
        return market

    async def fake_read_family_status(
        called_with: AsyncSession, *, resolution: WatchDeskResolution, now: datetime
    ) -> FamilyStatus:
        status_calls.append({"session": called_with, "resolution": resolution, "now": now})
        return status

    class FakeFindingRepository:
        def __init__(self, called_with: AsyncSession) -> None:
            self._session = called_with

        async def count_open_by_family(self) -> dict[str, int]:
            count_calls.append(self._session)
            return counts

    monkeypatch.setattr(material_module, "read_market", fake_read_market)
    monkeypatch.setattr(material_module, "read_family_status", fake_read_family_status)
    monkeypatch.setattr(material_module, "IreneFindingRepository", FakeFindingRepository)

    material = await gather_briefing_material(
        session, resolution=resolution, now=_NOW, rss_buckets=buckets, findings=findings
    )

    assert market_calls == [{"session": session, "as_of": _NOW.date()}]
    assert len(status_calls) == 1
    assert status_calls[0]["session"] is session
    assert status_calls[0]["resolution"] is resolution
    assert status_calls[0]["now"] == _NOW
    assert count_calls == [session]
    assert material == compose_briefing_material(
        market=market,
        status=status,
        rss_buckets=buckets,
        findings=findings,
        open_counts=counts,
        resolution=resolution,
        now=_NOW,
    )


# ---------------------------------------------------------------------------
# Live DB — the JSONB round trip
# ---------------------------------------------------------------------------


async def test_material_round_trips_through_a_briefing_row(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """A gathered ``inputs`` persists verbatim as ``irene_briefing.inputs``."""
    tenant_id = await seed_tenant("briefing-material")
    user_id = await seed_user(superuser_engine, tenant_id)
    now = datetime.now(timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        scope = WatchFamilyScopeRepository(session)
        for family in WATCHPOINT_FAMILIES:
            await scope.insert_revision(
                family=family,
                effective_from=now - timedelta(hours=1),
                enabled=False,
                changed_by=user_id,
            )

        resolution = await resolve_watch_desk(session, as_of=now)
        material = await gather_briefing_material(
            session, resolution=resolution, now=now, rss_buckets=[], findings=[]
        )
        assert material.nothing_to_write is True

        briefings = IreneBriefingRepository(session)
        await briefings.append(
            beat_at=now,
            written_at=now,
            status="skipped",
            skip_reason="nothing_to_write",
            inputs=material.inputs,
            finding_ids=[],
        )
        newest = await briefings.newest()
        assert newest is not None
        assert newest.inputs == material.inputs
