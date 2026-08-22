# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk web surface — the sixth top-level Area (ADR-0089).

The Watch Desk is a **read/write UI over the Irene layers built in
Prompts 1–4**: it renders the append-only findings those layers compute and
persist, and records the portfolio manager's response. It introduces **no**
materiality, delta, floor, band, or synthesis logic — those are fixed
upstream (Prompts 3–4). Findings are otherwise immutable (ADR-0085); the
only writes this module performs are *resolutions* and *schedule settings*.

Endpoints
---------

* ``GET  /api/watch-desk/briefing`` — the calm-by-default card feed of
  open findings, in ``IreneFindingRepository.list_open()`` order (final
  urgency desc, then recency). An empty feed renders the affirmative calm
  state (a green status line), never an empty/error state.
* ``POST /api/watch-desk/request-analysis`` — enqueue an
  out-of-cadence beat by bringing the tenant schedule due now
  (``enqueue_due_now``). It does **not** run synthesis inline (ADR-0086):
  the next scheduler tick picks it up (ADR-0117).
* ``GET  /api/watch-desk/briefing/poll`` — the time-boxed companion of
  that enqueue: it answers "has the beat landed since ``since``" with
  either 204 (not yet, keep polling) or 286 (stop) — the latter carrying
  the re-rendered Briefing body when a beat did land. Started only by an
  enqueue confirmation and self-terminating; nothing here polls otherwise.
* ``POST /api/watch-desk/findings/{finding_id}/resolve`` — record a
  resolution (``acted`` / ``dismissed`` / ``acknowledged``); the card
  leaves the Briefing feed and appears in the Journal.
* ``GET  /api/watch-desk/journal`` — read-only history of resolved
  findings via ``list_journal()``.
* ``GET  /api/watch-desk/calibration`` — the tuning surface: the tenant
  calibration editor (ADR-0116 §7), the watchpoint list, and the cadence
  settings panel.
* ``POST /api/watch-desk/calibration`` — save a calibration revision.
* ``GET/POST /api/watch-desk/watchpoints/overlay`` — the per-subject
  sensitivity drawer for the derived families (ADR-0116 §3).
* ``GET  /api/watch-desk/watchpoints/new`` — the add form for one defined
  signal family; ``POST /api/watch-desk/watchpoints`` creates it.
* ``GET  /api/watch-desk/watchpoints/{id}/edit`` — the editor for one
  signal watchpoint identity; ``POST .../revise`` writes a new version and
  ``POST .../retire`` a retiring one.
* ``GET  /api/watch-desk/watchpoints/{id}/history`` — the identity's
  version rows, newest first, read-only.
* ``POST /api/watch-desk/cadence`` — edit the tenant ``irene_schedule``
  (cadence / preferred_hour / timezone / enabled), recomputing
  ``next_due_at`` via ``services.irene.scheduling.compute_next_due_at``.

The monitor's four **signal** groups are derived live at request time from
the same resolution, the same batched fetch and the same pure producers the
beat runs on (``services.watch_desk.signal_observation``, ADR-0116 §6).
Nothing here writes watch-state: rendering a row must never advance a
subject's state machine.

All writes take ``session = Depends(require_session)`` and
``_ = Depends(verify_csrf)`` and open a ``tenant_context`` so the write is
RLS-policed and the audit trigger captures the actor.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    FloorCalibrationInvalid,
    IreneCadenceInvalid,
    IreneResolutionInvalid,
    LimitSetNotEffective,
    MissingFxRateError,
    WatchpointInvalid,
    WatchpointNotFound,
)
from core.models.watchpoint import OVERLAY_FAMILIES, SINGLETON_FAMILIES
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_finding_repository import (
    IreneFindingDTO,
    IreneFindingRepository,
)
from core.repositories.irene_schedule_repository import (
    IreneScheduleDTO,
    IreneScheduleRepository,
)
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateDTO,
    IreneWatchStateRepository,
)
from core.repositories.floor_calibration_repository import (
    CAP_COLUMNS,
    DELTA_COLUMNS,
    FLOOR_COLUMNS,
    FloorCalibrationDTO,
    FloorCalibrationRepository,
)
from core.repositories.limits_repository import LimitsRepository
from core.repositories.tenant_repository import TenantRepository
from core.repositories.user_repository import UserRepository
from core.repositories.watchpoint_repository import WatchpointDTO, WatchpointRepository
from services.analytics.irene_floor import (
    BAND_CRITICAL,
    BAND_INFORMATIONAL,
    BAND_NOTEWORTHY,
    TRIGGER_FUND_CLOSURE,
)
from services.analytics.cash_coverage_watch import coverage_ratio, projected_calls_of
from services.analytics.limit_coverage import classify_coverage_status
from services.analytics.signal_watch import (
    FAMILY_FRESHNESS,
    FAMILY_FX,
    FAMILY_LIQUIDITY,
    FAMILY_PRICE,
    FRESHNESS_WILDCARD_SUBJECT_KEY,
    LIQUIDITY_SUBJECT_KEY,
    STATUS_OK,
    STATUS_TRIGGERED,
    STATUS_WARN,
    NoObservation,
    SignalObservation,
    SignalResult,
    signal_status_label,
)
from services.auth.session import SessionDTO
from services.irene.scheduling import compute_next_due_at
from services.limits import LimitsCoverageBundle, LimitsCoverageService
from services.watch_desk.calibration import save_calibration_revision
from services.watch_desk.overlay import (
    SignalWatchpoint,
    SubjectOverlay,
    WatchDeskResolution,
    resolve_watch_desk,
    rss_overlay_subject_key,
)
from services.watch_desk.seeding import (
    default_display_name,
    fx_subject_key,
    price_subject_key,
)
from services.watch_desk.signal_observation import (
    SIGNAL_FAMILY_ORDER,
    observe_signal_families,
)
from services.web_research.allowlist import _KNOWN_TAGS
from web.auth import require_session, verify_csrf
from web.errors import user_safe_error
from web.htmx_poll import (
    POLL_HORIZON,
    POLL_STOP_STATUS,
    parse_poll_since,
    poll_stop,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# Canonical user-action resolutions (ADR-0085 / ADR-0089). Lowercase is the
# persisted vocabulary; the UI labels them capitalised. ``open`` is a valid
# repository value but is NOT a user action, so it is deliberately excluded
# here — the Watch Desk only ever *closes* a finding. ``opened_case`` is
# deliberately absent too (ADR-0107, C4 binding decision 5): the fifth
# resolution is written exclusively by the open-case composition, never by
# this endpoint, so a posted ``opened_case`` is rejected here.
_ALLOWED_RESOLUTIONS: frozenset[str] = frozenset({"acted", "dismissed", "acknowledged"})

# Display labels for the resolution vocabulary — the Watch Desk owns the tag
# family (ADR-0107, C4 rider 2), so this map is the single source both the
# Journal rows and the Cases origin embed read, and the two surfaces can never
# phrase the fifth resolution differently. The existing values keep their
# capitalised forms; ``opened_case`` reads "Opened case" rather than the
# ``|capitalize`` default "Opened_case".
RESOLUTION_LABELS: dict[str, str] = {
    "open": "Open",
    "acted": "Acted",
    "dismissed": "Dismissed",
    "acknowledged": "Acknowledged",
    "opened_case": "Opened case",
}


def resolution_label(resolution: str) -> str:
    """Return the display label for a resolution value.

    Falls back to a ``|capitalize`` of the raw value on any vocabulary drift,
    so an unknown resolution degrades to a legible label rather than a
    ``KeyError``.
    """
    return RESOLUTION_LABELS.get(resolution, resolution.capitalize())


# The Cases side deep-links back to the Journal section here (ADR-0107, C4
# binding decision 2): a navigational link, never an identifier. The section
# is server-rendered as ``<section id="journal">`` (only its body lazy-loads),
# so the fragment anchor lands reliably. Exported so the Cases surface uses one
# target, not a duplicated string.
JOURNAL_DEEP_LINK: str = "/watch-desk#journal"

# The cadence vocabulary offered in the settings panel — the five members
# of ADR-0119 §1. ``services.irene.scheduling`` stays the validator, so a
# posted value is checked there rather than here, but this tuple is a
# *subset* of ``_SUPPORTED_CADENCES`` and no longer mirrors it: ADR-0125 §1
# added ``every_30m`` and ``every_15m`` to the shared vocabulary, and
# ADR-0125 §2 deliberately withheld both from the Watch Desk — an Irene
# beat every 15 minutes is an LLM-cost decision this area has not taken.
# Pinned by ``tests/web/test_watch_desk_cadence_choices.py``.
# Ordered coarsest-first, the way the panel reads. The market-data admin
# surface keeps its own choices and is not driven by this tuple either.
_CADENCE_CHOICES: tuple[str, ...] = ("daily", "every_6h", "every_3h", "every_2h", "hourly")

# Display labels for the cadence vocabulary — same pattern as
# ``RESOLUTION_LABELS`` above, and for the same reason: a ``|capitalize`` in
# the template would render "Every_2h" (ADR-0119 §3).
CADENCE_LABELS: dict[str, str] = {
    "daily": "Daily",
    "every_6h": "Every 6 hours",
    "every_3h": "Every 3 hours",
    "every_2h": "Every 2 hours",
    "hourly": "Every hour",
}


def cadence_label(cadence: str) -> str:
    """Return the display label for a cadence value.

    Falls back to a ``|capitalize``-equivalent of the raw value on any
    vocabulary drift, so an unknown cadence degrades to a legible label
    rather than a ``KeyError``.
    """
    return CADENCE_LABELS.get(cadence, cadence.capitalize())


# Default cadence-panel values when a tenant has no schedule row yet. The
# tenant's own timezone drives ``preferred_hour`` placement; the German
# deployment default keeps the first render sensible before it is saved.
_DEFAULT_TIMEZONE: str = "Europe/Berlin"
_DEFAULT_PREFERRED_HOUR: int = 8

# Bands in severity order — the order the Open-findings tile breaks out its
# non-zero bands in, and the restrained colour keying of the mock (only
# ``critical`` and ``noteworthy`` are tinted; ``informational`` stays neutral).
_BAND_SEVERITY: tuple[tuple[str, str], ...] = (
    ("critical", "crit"),
    ("noteworthy", "note"),
    ("informational", ""),
)

# The two limit families the internal delta forms subject keys from — the
# beat watches exactly these. Also the order the Calibration section states
# its per-family re-trigger deltas in.
_LIMIT_FAMILIES: tuple[str, ...] = ("saa", "anlv")

# --- Briefing poll (post-enqueue refresh) ----------------------------------
#
# "Request analysis" enqueues; the beat runs ~a tick later in the scheduler
# (ADR-0117), and the already-rendered page has no way to learn that. The
# two bounds and the two primitives that close that gap moved to
# ``web/htmx_poll.py`` when ADR-0125 §5 gave the market-data surfaces the
# same loop: one definition, three call sites, no third copy. The four
# private names below are kept as aliases so this module reads exactly as it
# did — the Watch Desk's behaviour is unchanged by the extraction, and its
# tests are the proof.
_POLL_STOP_STATUS = POLL_STOP_STATUS
_POLL_HORIZON = POLL_HORIZON
_poll_stop = poll_stop
_parse_poll_since = parse_poll_since

# Human labels for the monitor's two internal groups, keyed by family.
_FAMILY_GROUP_NAMES: dict[str, str] = {
    "saa": "SAA limits",
    "anlv": "AnlV quotas",
}

# Display abbreviations for the two families, used by the Calibration cells.
# ``AnlV`` is a proper noun (the German Anlageverordnung), so the casing is
# stated deliberately rather than derived with ``.upper()``.
_FAMILY_LABELS: dict[str, str] = {
    "saa": "SAA",
    "anlv": "AnlV",
}

# ---------------------------------------------------------------------------
# Calibration editor vocabulary (ADR-0116 §7)
#
# The editable key sets are taken from the repository's column maps rather
# than restated here, so the editor cannot offer a field the storage layer
# has no column for — and a regression test already pins those maps against
# ``DEFAULT_FLOOR_CONFIG``. ``fund_closure`` is absent from all three by
# design: a pinned level has nowhere to be stored, which is the strongest
# possible way of making it non-editable. It is rendered as a locked row.
# ---------------------------------------------------------------------------

# Human labels for the floor / cap keys. Missing keys fall back to a
# de-underscored title, so a key added to the column maps still renders.
_CALIBRATION_KEY_LABELS: dict[str, str] = {
    "limit_breach": "Limit breach",
    "limit_escalation": "Limit escalation",
    "all_clear": "All-clear",
    "rss_cluster": "Press cluster",
    "price_trigger": "Price move",
    "fx_trigger": "FX move",
    "freshness_trigger": "NAV freshness",
    "liquidity_trigger": "Cash coverage",
    "internal": "Source · internal",
    "rss": "Source · press (RSS)",
}

# Human labels for the seven re-trigger-delta families.
_DELTA_FAMILY_LABELS: dict[str, str] = {
    "saa": "SAA limits",
    "anlv": "AnlV quotas",
    "rss": "Press clusters",
    "price": "Price moves",
    "fx": "FX moves",
    "freshness": "NAV freshness",
    "liquidity": "Cash coverage",
}

# The bands the options gate may be set to, benign → severe.
_OPTIONS_BANDS: tuple[str, ...] = (BAND_INFORMATIONAL, BAND_NOTEWORTHY, BAND_CRITICAL)

# The bands a finding may be handed to a Case from (ADR-0120 §1). Case-
# worthiness follows the *band*, never the presence of ``options`` — the
# latter is an optional member of the ADR-0088 contract, so a critical card
# phrased as pure statement must still carry the case path. Informational
# stays acknowledged-only (ADR-0107 D1). Read by both the card projection
# (what renders) and the open-case endpoint (what is accepted).
_CASE_BANDS: frozenset[str] = frozenset({BAND_NOTEWORTHY, BAND_CRITICAL})

# Inline constraint hints for the fields the pinned invariants couple
# (ADR-0116 §7 invariants 2–4). Rendered beside the field so the coupling is
# visible *before* a save is refused; the write path enforces it regardless,
# and these strings never substitute for that.
_CONSTRAINT_HINTS: dict[str, str] = {
    "floor_limit_breach": (
        "Lower bound follows the upper band boundary: must be at least "
        "(upper boundary + 1), so a regulatory breach never renders below "
        "critical."
    ),
    "cap_rss": (
        "Upper bound follows the lower band boundary: a standalone press "
        "cluster never outranks an internal finding."
    ),
    "cap_all_clear": (
        "Upper bound follows the lower band boundary: an all-clear is never itself urgent."
    ),
}

# The one delta field that is stored but has nothing to measure. Said out
# loud rather than hidden: an RSS cluster carries no scalar magnitude, so
# the value waits for a successor concept it does not have yet.
_RSS_DELTA_HINT: str = (
    "Press clusters are non-scalar, so they never re-trigger by magnitude — "
    "this value is stored but nothing measures against it today."
)

# Every field name the editor actually renders. A validation error whose
# ``field`` is not in this set has no slot to sit in — the pinned
# ``fund_closure`` keys are exactly that case, since the editor offers no
# input for them — so it is surfaced as a form-level notice instead. An
# error with nowhere to render is an error the operator never sees.
_EDITOR_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "warn_default_pct",
        "band_boundaries",
        "band_boundary_0",
        "band_boundary_1",
        "options_min_band",
        "notes",
    }
    | {f"floor_{key}" for key in FLOOR_COLUMNS}
    | {f"cap_{key}" for key in CAP_COLUMNS}
    | {f"re_trigger_delta_{family}" for family in DELTA_COLUMNS}
)

# Hints on the two fields whose meaning is easiest to misread.
_FIELD_HINTS: dict[str, str] = {
    "warn_default_pct": (
        "Coverage above this percentage of a ceiling is WARN. Strictly "
        "between 50 and 100. A subject may override it from the monitor."
    ),
    "band_boundaries": (
        "The two urgency cut points over the 1–10 scale: informational ≤ b0, "
        "noteworthy ≤ b1, critical above. Set together or not at all."
    ),
    "options_min_band": (
        "The lowest band at which a card keeps its options. Below it a card "
        "is pure fact, never counsel."
    ),
}

# Constrained coverage statuses — the only ones carrying a ceiling to gauge
# against. ``NO_LIMIT`` / ``UNALLOCATED`` rows have none and are skipped
# (the same rule the internal delta applies, ADR-0087 §0.2).
_CONSTRAINED_STATUSES: tuple[str, ...] = ("OK", "WARN", "BREACH")

# Status → pill / gauge modifier suffix, matching the mock's class names.
_STATUS_MODIFIER: dict[str, str] = {
    "OK": "ok",
    "WARN": "warn",
    "BREACH": "breach",
}

# Severity order for the group's aggregate badges; only non-zero counts are
# rendered, most severe first.
_STATUS_SEVERITY: tuple[str, ...] = ("BREACH", "WARN", "OK")

# ---------------------------------------------------------------------------
# Signal families on the monitor (ADR-0116 §4, §6)
#
# Four groups beneath the quota ones, rendered from the *same* observation
# the next beat will classify: `observe_signal_families` on the resolution
# the whole render already holds. Nothing is read from `irene_watch_state`
# except the acknowledged figures the note quotes, and nothing is written.
# ---------------------------------------------------------------------------

# Group headings, by family.
_SIGNAL_GROUP_NAMES: dict[str, str] = {
    FAMILY_PRICE: "Price moves",
    FAMILY_FX: "FX moves",
    FAMILY_FRESHNESS: "NAV freshness",
    FAMILY_LIQUIDITY: "Cash coverage",
}

# What each family measures — stated once in the group header so the Value
# and Threshold cells can stay bare figures in the family's own unit. The
# alternative (a unit suffix in every cell) would put the same six words on
# thirty rows and still leave the direction unsaid.
_SIGNAL_GROUP_MEASURES: dict[str, str] = {
    FAMILY_PRICE: "adverse move over each watchpoint's window",
    FAMILY_FX: "absolute move over each watchpoint's window",
    FAMILY_FRESHNESS: "age of the newest actual NAV",
    FAMILY_LIQUIDITY: "cash cover of the calls projected inside the horizon",
}

# Signal status → pill / gauge modifier. Deliberately **not** reusing
# ``_STATUS_MODIFIER``: that map spells ``breach``, which is regulatory
# language reserved for the quota families (ADR-0116 §4). Rendering it as a
# class name would put the word into the markup of a family whose every
# visible string avoids it — and the string assertion reads the markup.
_SIGNAL_MODIFIER: dict[str, str] = {
    STATUS_OK: "calm",
    STATUS_WARN: "approaching",
    STATUS_TRIGGERED: "triggered",
}

# Badge order for a signal group: worst first, then the no-data bucket last
# — absence of data is not a severity, it is a different axis.
_SIGNAL_SEVERITY: tuple[str, ...] = (STATUS_TRIGGERED, STATUS_WARN, STATUS_OK)

# The fourth rendered state, which has no internal status behind it: a
# subject the producer refused to guess about (ADR-0116 §4). Visually
# distinct from Calm, and it draws no gauge at all — a gauge is a claim.
_NO_DATA_LABEL: str = "No data"
_NO_DATA_MODIFIER: str = "nodata"

# The families whose group header offers "+ Add watchpoint". The two
# singleton families are absent by construction: their group only renders
# when a live singleton already exists, and a second one is refused by the
# repository — so the affordance would be an offer the write path declines.
# Adding a singleton happens from the empty-state footer instead.
_ADDABLE_FROM_GROUP_HEADER: tuple[str, ...] = (FAMILY_PRICE, FAMILY_FX)

# Freshness subjects grow one-for-one with the book, so the group lists its
# exceptions openly and collapses the calm remainder behind a summary line
# (ADR-0116 §6 honesty rule: visibility on demand, never omission). The
# other three families are bounded by what the operator defined and are
# listed whole.
_EXCEPTION_FIRST_FAMILIES: tuple[str, ...] = (FAMILY_FRESHNESS,)

# The WARN threshold is no longer a constant here: since ADR-0116 §3 it is
# resolved per tenant and per subject by
# ``services.watch_desk.overlay.resolve_watch_desk`` — the same resolution
# the beat runs on. Every former reader of the old module-level 90% now
# asks the resolution instead.


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Projections — DTO → template-friendly dicts
# ---------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    """Coerce a payload field to a list of strings; ``[]`` for anything else."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _project_card(dto: IreneFindingDTO) -> dict[str, Any]:
    """Project one open finding into the Briefing card context.

    Everything the card renders comes from the immutable ``payload`` (the
    ``surface_finding`` contract) plus the *final* ``band`` (ADR-0088). The
    raw 1–10 ``urgency`` is deliberately **not** projected — the card is
    band-coloured, never urgency-badged (ADR-0089). ``options`` are already
    band-gated upstream (dropped from an informational card by the beat), so
    the card just renders whatever is present — as the list (``options``) and
    as the joined "Possible moves" paragraph (``options_prose``).

    ``case_affordance`` is the "Open case →" gate (ADR-0120 §1): it follows
    the band alone, so an option-less noteworthy/critical card still carries
    a case path. The template decides *where* the affordance sits (inside the
    Possible-moves block when that block renders, in the card footer
    otherwise, ADR-0120 §2) — never *whether* it may.
    """
    payload = dto.payload or {}
    options = _as_str_list(payload.get("options"))
    # The card renders options as a short prose block (ADR-0089). The payload
    # stays a list of strings (ADR-0088 contract, no migration); the synthesis
    # prompt now steers the model to 1–3 connected sentences, so joining on a
    # space yields the intended prose. Legacy multi-bullet payloads join into a
    # single readable paragraph rather than breaking.
    options_prose = " ".join(part.strip() for part in options if part.strip())
    return {
        "id": str(dto.id),
        "subject_key": dto.subject_key,
        "band": dto.band,
        "trigger": payload.get("trigger", ""),
        "finding": payload.get("finding", ""),
        "basis": payload.get("basis", ""),
        "options": options,
        "options_prose": options_prose,
        "case_affordance": dto.band in _CASE_BANDS,
        "evidence_refs": _as_str_list(payload.get("evidence_refs")),
        # RSS enrichment (present only on RSS-sourced findings).
        "tag": payload.get("tag"),
        "members": payload.get("members") if isinstance(payload.get("members"), list) else [],
        "created_at": dto.created_at,
    }


def _resolve_zone(name: str | None) -> tuple[ZoneInfo, bool]:
    """Resolve a schedule's IANA timezone name, falling back to UTC.

    Args:
        name: The schedule's ``timezone`` column, or ``None`` when the tenant
            has no schedule row.

    Returns:
        A ``(zone, is_utc_fallback)`` pair. ``is_utc_fallback`` is ``True``
        when the tenant timezone was unavailable or unparseable, in which
        case the rendered times carry an explicit ``UTC`` suffix rather than
        silently reading as local time.
    """
    if name:
        try:
            return ZoneInfo(name), False
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "watch-desk tiles: unknown schedule timezone %r; rendering beat times in UTC.",
                name,
            )
    return ZoneInfo("UTC"), True


def _stamp(moment: datetime, fmt: str, *, zone: ZoneInfo, utc_fallback: bool) -> str:
    """Render an instant in the schedule's timezone, flagging a UTC fallback.

    Module-level rather than a closure because two tile projections need it:
    :func:`_build_tiles` for the last-beat tile and :func:`_next_beat_tile`,
    which the "Request analysis" route re-renders on its own.

    Args:
        moment: The instant to render (timezone-aware).
        fmt: A :meth:`~datetime.datetime.strftime` format string.
        zone: The resolved display timezone (see :func:`_resolve_zone`).
        utc_fallback: True when the schedule's timezone was unusable and
            ``zone`` is the UTC substitute — the stamp is suffixed "UTC" so
            it never reads as local time.

    Returns:
        The formatted stamp.
    """
    text_value = moment.astimezone(zone).strftime(fmt)
    return f"{text_value} UTC" if utc_fallback else text_value


def _next_beat_tile(
    *,
    schedule: IreneScheduleDTO | None,
    now: datetime,
    zone: ZoneInfo,
    utc_fallback: bool,
) -> dict[str, Any]:
    """Project the fourth Briefing status tile — the next-beat state.

    Split out of :func:`_build_tiles` so the "Request analysis" route can
    re-render *this* tile out of band after an enqueue. One derivation, two
    render sites: the tile the page shows and the tile the enqueue swaps in
    cannot disagree — and the other three tiles, which the enqueue cannot
    change, are not recomputed (tile 2's figure comes off the monitor
    resolution, which is far too much work to repeat for no visible change).

    A schedule that is already due renders "due now" rather than a wall-clock
    stamp: after an enqueue ``next_due_at`` is *now*, and a past time under
    the label "Next beat" states the opposite of what is true.

    Args:
        schedule: The tenant's :class:`IreneScheduleDTO`, or ``None``.
        now: The current instant (timezone-aware UTC).
        zone: The resolved display timezone (see :func:`_resolve_zone`).
        utc_fallback: True when ``zone`` is the UTC substitute.

    Returns:
        The ``tiles.next_beat`` template context.
    """
    if schedule is None:
        return {"configured": False}
    return {
        "configured": True,
        "due": (
            "due now"
            if schedule.next_due_at <= now
            else _stamp(schedule.next_due_at, "%a %H:%M", zone=zone, utc_fallback=utc_fallback)
        ),
        "cadence": schedule.cadence,
        "enabled": schedule.enabled,
    }


def _build_tiles(
    *,
    findings: list[IreneFindingDTO],
    schedule: IreneScheduleDTO | None,
    findings_since_beat: int | None,
    internal_subject_count: int,
    signal_subject_count: int,
    now: datetime,
) -> dict[str, Any]:
    """Project the four Briefing status tiles (ADR-0089).

    Every figure is **derived at request time** from tables that already
    exist — there is no ``last_beat_summary`` row and no new persistence.
    The wording is derivation-honest: "surfaced N findings" states exactly
    what was measured (findings created at or after ``last_beat_at``) and
    claims no causal attribution to the beat.

    Args:
        findings: The open findings already loaded for the feed.
        schedule: The tenant's :class:`IreneScheduleDTO`, or ``None``.
        findings_since_beat: Count of findings created since the last beat,
            or ``None`` when no beat has run (the never-ran state).
        internal_subject_count: Number of limit rows across the effective
            ``saa`` / ``anlv`` sets — the internal subjects the beat watches.
        signal_subject_count: Number of signal-family subjects, summed off
            the monitor's own group figures by
            :func:`_signal_subject_count`. Never enumerated here: the tile
            and the group headers below it are one count rendered twice.
        now: The current instant (timezone-aware UTC).

    Returns:
        The ``tiles`` template context: one entry per tile.
    """
    zone, utc_fallback = _resolve_zone(schedule.timezone if schedule is not None else None)

    # --- Tile 1: last beat -------------------------------------------------
    last_beat_at = schedule.last_beat_at if schedule is not None else None
    if last_beat_at is None:
        # The trust case: never claim a beat ran, and never render
        # "surfaced 0 findings" as if one had.
        last_beat: dict[str, Any] = {"ran": False}
    else:
        local = last_beat_at.astimezone(zone)
        time_text = _stamp(last_beat_at, "%H:%M", zone=zone, utc_fallback=utc_fallback)
        if local.date() == now.astimezone(zone).date():
            time_text = f"{time_text} today"
        count = findings_since_beat or 0
        last_beat = {
            "ran": True,
            "time": time_text,
            "date": local.strftime("%a %d %b %Y"),
            "surfaced": (
                f"surfaced {count} finding" if count == 1 else f"surfaced {count} findings"
            ),
        }

    # --- Tile 2: subjects watched -----------------------------------------
    # Everything the Watch Desk watches, in the tile's own breakdown idiom:
    # the quota subjects, the press dimensions, and — since ADR-0116 §4 — the
    # signal-family subjects. Without the third term the tile understated the
    # monitor it sits directly above.
    #
    # The signals figure is *taken* from that monitor rather than derived
    # again (see :func:`_signal_subject_count`), which is what carries the
    # counting rules over unchanged: a muted subject counts, a subject the
    # producer could not evaluate counts, a retired identity does not.
    #
    # A zero signals term is omitted rather than rendered as "+ 0": a tenant
    # who watches no signal family reads exactly the tile they read before,
    # and "0 signal subjects" is a clause that states nothing.
    rss_subject_count = len(_KNOWN_TAGS)
    figures = [str(internal_subject_count), str(rss_subject_count)]
    breakdown = [
        f"{internal_subject_count} internal {'limit' if internal_subject_count == 1 else 'limits'}",
        f"{rss_subject_count} press dimensions",
    ]
    if signal_subject_count:
        figures.append(str(signal_subject_count))
        breakdown.append(_plural(signal_subject_count, "signal subject"))
    subjects = {
        "internal": internal_subject_count,
        "rss": rss_subject_count,
        "signals": signal_subject_count,
        "value": " + ".join(figures),
        "sub": " · ".join(breakdown),
    }

    # --- Tile 3: open findings by band ------------------------------------
    band_counts = Counter(finding.band for finding in findings)
    bands = [
        {"text": f"{band_counts[band]} {band}", "css": css}
        for band, css in _BAND_SEVERITY
        if band_counts.get(band)
    ]

    # --- Tile 4: next beat ------------------------------------------------
    # Projected by its own helper so the "Request analysis" enqueue can
    # re-render exactly this tile out of band (see :func:`_next_beat_tile`).
    next_beat = _next_beat_tile(schedule=schedule, now=now, zone=zone, utc_fallback=utc_fallback)

    return {
        "last_beat": last_beat,
        "subjects": subjects,
        "open_findings": {"total": len(findings), "bands": bands},
        "next_beat": next_beat,
    }


# ---------------------------------------------------------------------------
# Monitor — "What Irene watches" (ADR-0089)
#
# A pure request-time projection: live coverage from the limits engine, joined
# to the acknowledged watch-state each subject carries. Nothing here is
# persisted and nothing is generated — Irene's per-subject note is *assembled*
# from status + watch-state + Floor Config, never written by a model.
# ---------------------------------------------------------------------------


def _build_coverage_service(db_session: AsyncSession) -> LimitsCoverageService:
    """Compose the coverage service from the six tenant-scoped repos.

    Identical wiring to ``web/routes/limits.py:_build_service`` and
    ``services/irene/internal_delta.py:_build_service``. The monitor
    computes coverage **live** through the same engine the Limits section
    and the beat use, so the three can never disagree about a figure.
    """
    return LimitsCoverageService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        limits=LimitsRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        tenants=TenantRepository(db_session),
        fx_rates=FxRateRepository(db_session),
    )


def _as_decimal(value: Any) -> Decimal | None:
    """Coerce a coverage-frame cell to ``Decimal``; ``None`` for a null."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _utilisation_pct(coverage_pct: Decimal, max_pct: Decimal | None) -> Decimal | None:
    """Return coverage as a percentage of the ceiling, or ``None``.

    The gauge's shared scale is 0 → ceiling, so utilisation is the only
    quantity the bar encodes. A zero ceiling permits no allocation at all:
    any positive coverage is fully utilised (and classified ``BREACH``
    upstream), zero coverage is not.
    """
    if max_pct is None:
        return None
    if max_pct == 0:
        return Decimal("100") if coverage_pct > 0 else Decimal("0")
    return coverage_pct / max_pct * Decimal("100")


def _dom_id(prefix: str, subject_key: str) -> str:
    """Return a CSS-selector-safe DOM id for one subject's row widgets.

    ``subject_key`` carries a colon (``saa:equities``), which is legal in
    an HTML id but has to be escaped in every CSS selector — and HTMX
    targets *are* CSS selectors. Slugging the key here keeps the templates
    free of escaping rules; the mapping is injective enough for one page
    because class keys are ``[a-z0-9_]``.
    """
    slug = "".join(char if char.isalnum() else "-" for char in subject_key)
    return f"{prefix}-{slug}"


def _irene_note(
    *,
    status: str,
    coverage_pct: Decimal,
    max_pct: Decimal | None,
    watch: IreneWatchStateDTO | None,
    fired: bool,
    re_trigger_delta: Decimal,
    warn_threshold_pct: Decimal,
) -> str:
    """Assemble Irene's deterministic per-subject note (ADR-0089).

    Never generated by a model and never parsed out of a finding's prose:
    the note is derived from the live coverage status, the subject's
    acknowledged watch-state, and the Floor Config re-trigger Δ.

    The **fired** note deliberately states the current status only. The
    FROM→TO edge that actually fired lives transiently in the beat's delta
    ``reason`` and is not persisted as structured data, so a "rising edge
    WARN → BREACH" claim cannot be sourced from persistence — the note
    asserts what is certain and no more (see the DC2 closure note).

    Args:
        status: The live coverage status (``OK`` / ``WARN`` / ``BREACH``).
        coverage_pct: Live coverage in percentage points.
        max_pct: The ceiling in percentage points, if any.
        watch: The subject's watch-state row, or ``None`` when the beat
            has never observed it.
        fired: Whether an open finding for this subject was created in
            the current beat.
        re_trigger_delta: This subject's magnitude re-trigger threshold in
            percentage points — its overlay override when it carries one,
            else the family value from the tenant's effective Floor Config.
        warn_threshold_pct: This subject's effective WARN threshold, as a
            percentage of the ceiling. Per-subject since ADR-0116 §3, so
            the note states the threshold the subject was actually
            measured against rather than a house default.

    Returns:
        The rendered note string.
    """
    if fired:
        return f"Fired this beat — now at {status}."

    if status == "OK":
        return f"Calm — below the {float(warn_threshold_pct):.0f}% WARN threshold."

    # Non-benign and did not fire. If the subject carries an acknowledged
    # magnitude close enough to the live one, the silence is *explained* —
    # the delta layer suppressed it below the re-trigger Δ.
    ack_magnitude = watch.acknowledged_magnitude if watch is not None else None
    ack_at = watch.acknowledged_at if watch is not None else None
    if ack_magnitude is not None and ack_at is not None:
        move = coverage_pct - ack_magnitude
        if abs(move) < re_trigger_delta:
            # The acknowledged band is re-derived by re-classifying the
            # acknowledged magnitude against the *current* ceiling — the
            # same rule the delta layer applies, so the note agrees with
            # the mechanism that produced the silence.
            ack_status = (
                classify_coverage_status(ack_magnitude, max_pct, warn_threshold_pct)
                if max_pct is not None
                else status
            )
            return (
                f"Silent — {ack_status} acknowledged at "
                f"{float(ack_magnitude):.2f}% on "
                f"{ack_at.strftime('%d %b %Y')}; {float(move):+.2f} pp is "
                f"below the {float(re_trigger_delta):.1f} pp re-trigger."
            )

    # Non-benign, no acknowledgement, no firing this beat — e.g. the first
    # render before any beat has run. Never invent an acknowledgement.
    return f"{status} — not yet reviewed."


def _build_family_group(
    *,
    family: str,
    coverage: pd.DataFrame | None,
    latest_as_of_date: Any,
    watch_by_subject: dict[str, IreneWatchStateDTO],
    fired: dict[str, str],
    effective_label: str | None,
    resolution: WatchDeskResolution,
) -> dict[str, Any]:
    """Project one family's constrained coverage rows into a monitor group.

    Only constrained rows (``OK`` / ``WARN`` / ``BREACH``) are shown:
    ``NO_LIMIT`` and ``UNALLOCATED`` carry no ceiling, so there is nothing
    to gauge utilisation against.

    The engine classified the frame against the tenant WARN default in one
    pass; a subject carrying an overlay override is re-classified here with
    ``classify_coverage_status`` — the same pure function, and the same
    correction the beat applies, so monitor and beat agree on the status by
    construction rather than by coincidence.

    Args:
        family: ``'saa'`` or ``'anlv'``.
        coverage: The family's long-format coverage frame, or ``None``
            when coverage is unavailable (empty universe / no Stichtag).
        latest_as_of_date: The Stichtag (a ``date``) to slice, or ``None``.
        watch_by_subject: Watch-state rows indexed by ``subject_key``.
        fired: ``subject_key`` → finding id for subjects that fired in
            the current beat.
        effective_label: The effective limit set's label, or ``None``.
        resolution: The tenant's effective calibration and per-subject
            overlays — the same object the beat runs on.

    Returns:
        The group's template context.
    """
    rows: list[dict[str, Any]] = []

    if coverage is not None and latest_as_of_date is not None:
        # The engine stores as_of_date as datetime64, so the Stichtag must
        # be compared as a Timestamp — a bare date silently matches nothing
        # (same conversion as web/routes/limits.py and internal_delta.py).
        slice_ = coverage[coverage["as_of_date"] == pd.Timestamp(latest_as_of_date)]
        for record in slice_.to_dict("records"):
            status = str(record["status"])
            if status not in _CONSTRAINED_STATUSES:
                continue
            coverage_pct = _as_decimal(record["coverage_pct"])
            if coverage_pct is None:
                continue
            max_pct = _as_decimal(record["max_pct"])
            subject_key = f"{family}:{record['class_key']}"

            warn_threshold_pct = resolution.warn_threshold_for(subject_key)
            if max_pct is not None:
                status = classify_coverage_status(coverage_pct, max_pct, warn_threshold_pct)
            overlay = resolution.overlay_for(subject_key)
            muted = overlay is not None and overlay.muted
            finding_id = fired.get(subject_key)
            utilisation = _utilisation_pct(coverage_pct, max_pct)
            rows.append(
                {
                    "subject_key": subject_key,
                    "family": family,
                    "status": status,
                    "modifier": _STATUS_MODIFIER[status],
                    "coverage_pct": float(coverage_pct),
                    "max_pct": float(max_pct) if max_pct is not None else None,
                    "utilisation": (float(utilisation) if utilisation is not None else None),
                    # Rendered width only — clamped so a breach cannot
                    # overflow the bar. The figures above stay honest.
                    "fill_pct": (
                        min(float(utilisation), 100.0) if utilisation is not None else 0.0
                    ),
                    # The mark's position on the shared 0 → ceiling scale.
                    # Per-subject *positioned* since ADR-0116 §6, never
                    # per-subject rescaled: the axis is still the ceiling on
                    # every row, and the mark still sits at the WARN fraction
                    # of it — that fraction is now the subject's own.
                    "warn_threshold_pct": float(warn_threshold_pct),
                    "muted": muted,
                    # A BREACH cannot be muted (ADR-0116 §3). Disabling the
                    # toggle here is the *mirror* of the beat-side rule, for
                    # legibility; the beat enforces it whatever this says.
                    "mute_locked": status == "BREACH",
                    "warn_customised": (
                        overlay is not None and overlay.warn_threshold_pct is not None
                    ),
                    "delta_customised": (
                        overlay is not None and overlay.re_trigger_delta is not None
                    ),
                    "editor_id": _dom_id("dc-overlay", subject_key),
                    "fired_this_beat": finding_id is not None,
                    "finding_anchor": (
                        f"#dc-card-{finding_id}" if finding_id is not None else None
                    ),
                    "note": _irene_note(
                        status=status,
                        coverage_pct=coverage_pct,
                        max_pct=max_pct,
                        watch=watch_by_subject.get(subject_key),
                        fired=finding_id is not None,
                        re_trigger_delta=resolution.re_trigger_delta_for(subject_key),
                        warn_threshold_pct=warn_threshold_pct,
                    ),
                }
            )

    status_counts = Counter(row["status"] for row in rows)
    muted_count = sum(1 for row in rows if row["muted"])
    return {
        "family": family,
        "name": _FAMILY_GROUP_NAMES[family],
        "badges": [
            {
                "text": f"{status_counts[status]} {status}",
                "modifier": _STATUS_MODIFIER[status],
            }
            for status in _STATUS_SEVERITY
            if status_counts.get(status)
        ],
        "subject_count": len(rows),
        # Counted in the header so a muted subject is visible as a choice
        # someone made, never as an absence (ADR-0116 §3).
        "muted_count": muted_count,
        "effective_label": effective_label,
        "rows": rows,
    }


def _build_rss_group(
    fired_tags: dict[str, str], *, resolution: WatchDeskResolution
) -> dict[str, Any]:
    """Project the curated RSS tags into the monitor's third group.

    Three columns only — tag, what it corroborates, Irene's note. The
    mock's "Clusters today" column is deliberately **not** rendered (D8):
    a per-tag cluster count for tags that produced no finding is not
    recoverable from persisted data, and a silent-cluster claim would be
    an invention. A tag with no corroborating finding this beat carries
    no note, which is the honest v1 output.

    Args:
        fired_tags: RSS ``tag`` → finding id for findings created in the
            current beat that carry that tag's enrichment.
        resolution: The tenant's effective calibration, for the ``rss``
            overlays. An ``rss`` overlay carries **mute alone** — the
            schema forbids the rest, because a cluster subject is
            non-scalar and has no threshold to move.

    Returns:
        The RSS group's template context.
    """
    tag_map = resolution.config.tag_asset_class_map
    rows: list[dict[str, Any]] = []
    for tag in sorted(_KNOWN_TAGS):
        finding_id = fired_tags.get(tag)
        subject_key = rss_overlay_subject_key(tag)
        rows.append(
            {
                "tag": tag,
                "subject_key": subject_key,
                "family": "rss",
                "corroborates": list(tag_map.get(tag, ())),
                "muted": resolution.is_muted(subject_key),
                # No breach exception: a press cluster has no ceiling to
                # violate, so a muted dimension is simply silent.
                "mute_locked": False,
                "editor_id": _dom_id("dc-overlay", subject_key),
                "fired_this_beat": finding_id is not None,
                "finding_anchor": (f"#dc-card-{finding_id}" if finding_id is not None else None),
                "note": (
                    "Folded into an open finding as corroboration."
                    if finding_id is not None
                    else ""
                ),
            }
        )
    return {
        "name": "Press dimensions (RSS)",
        "meta": (
            f"{len(_KNOWN_TAGS)} curated tags · corroboration only, never a source of figures"
        ),
        "muted_count": sum(1 for row in rows if row["muted"]),
        "rows": rows,
    }


def _plural(count: int, unit: str) -> str:
    """Render ``count`` of ``unit``, pluralising the ordinary way."""
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _signal_figures(watchpoint: SignalWatchpoint, result: SignalObservation) -> tuple[str, str]:
    """Return one row's ``(value, threshold)`` in the family's own language.

    Each family speaks the unit its operator calibrated (ADR-0116 §6):
    percentage points for a move, whole days for an age, and **ratios** for
    coverage. The 100-scale ``liquidity`` computes on is arithmetic, not
    communication (see :mod:`services.analytics.cash_coverage_watch`), and
    never reaches a cell — the ratio is recovered from the observation's own
    window pair through the two functions the producer exports for it, so
    the figure on the row and the figure in a finding's note come from one
    derivation.

    Args:
        watchpoint: The row's effective watchpoint.
        result: The observation to render.

    Returns:
        The value cell and the threshold cell, as display strings.
    """
    if watchpoint.family == FAMILY_FRESHNESS:
        return _plural(int(result.magnitude), "day"), _plural(int(result.threshold_pct), "day")

    if watchpoint.family == FAMILY_LIQUIDITY:
        floor = watchpoint.min_coverage_ratio
        ratio = coverage_ratio(
            liquid_balance=result.reference_value, calls=projected_calls_of(result)
        )
        # `None` means "nothing to cover", which is a sentence rather than a
        # number: printing 0.00× there would state a shortfall the book does
        # not have, and printing ∞ would state a cover it was never asked for.
        return (
            "no calls projected" if ratio is None else f"{float(ratio):.2f}×",
            "—" if floor is None else f"{float(floor):.2f}×",
        )

    return f"{float(result.magnitude):.2f}%", f"{float(result.threshold_pct):.2f}%"


def _signal_note(
    *,
    watchpoint: SignalWatchpoint,
    result: SignalResult,
    watch: IreneWatchStateDTO | None,
    fired: bool,
    re_trigger_delta: Decimal,
    warn_threshold_pct: Decimal,
) -> str:
    """Assemble one signal row's deterministic note.

    The signal-family sibling of :func:`_irene_note`, on the same four
    branches and with the same rule: route-assembled from the live
    observation, the subject's acknowledged watch-state and the effective
    calibration, never generated and never parsed out of a finding. The
    template renders the string and composes nothing.

    Two departures from the quota note, both deliberate:

    * every branch speaks Calm / Approaching / Triggered — "breach" is
      regulatory language reserved for the quota families (ADR-0116 §4);
    * the *silent* branch states that the subject was acknowledged and when,
      but **not** the acknowledged figure. Three of the four families could
      restate it natively; ``liquidity`` stores it on the internal 100-scale
      and would have to invert a clamped magnitude to reach a ratio. One
      sentence that is true for four families beats four sentences of which
      one is reconstructed.

    Args:
        watchpoint: The row's effective watchpoint.
        result: The live observation, or the producer's refusal to make one.
        watch: The subject's watch-state row, or ``None`` when the beat has
            never observed it.
        fired: Whether an open finding for this subject was created in the
            current beat.
        re_trigger_delta: The subject's effective magnitude re-trigger
            threshold, in the family's own unit.
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of the trigger threshold.

    Returns:
        The rendered note string.
    """
    if isinstance(result, NoObservation):
        # The producer's own words. It is the half that knows *why* nothing
        # could be measured, and a second phrasing here would eventually
        # disagree with the one the beat logged.
        return f"No data — {result.reason}."

    label = signal_status_label(result.status)
    if fired:
        return f"Fired this beat — now {label}."

    if result.status == STATUS_OK:
        return f"Calm — below the {float(warn_threshold_pct):.0f}% Approaching mark."

    # Non-calm and did not fire. If the subject carries an acknowledged
    # magnitude close enough to the live one, the silence is *explained* —
    # the delta layer suppressed it below the re-trigger Δ.
    ack_magnitude = watch.acknowledged_magnitude if watch is not None else None
    ack_at = watch.acknowledged_at if watch is not None else None
    if (
        ack_magnitude is not None
        and ack_at is not None
        and abs(result.magnitude - ack_magnitude) < re_trigger_delta
    ):
        return (
            f"Silent — acknowledged on {ack_at.strftime('%d %b %Y')}; it has "
            "not moved past the re-trigger since."
        )

    # Non-calm, no acknowledgement, no firing this beat — e.g. the first
    # render before any beat has run. Never invent an acknowledgement.
    return f"{label} — not yet reviewed."


def _signal_row(
    *,
    watchpoint: SignalWatchpoint,
    result: SignalResult,
    watch_by_subject: dict[str, IreneWatchStateDTO],
    fired: dict[str, str],
    resolution: WatchDeskResolution,
) -> dict[str, Any]:
    """Project one observed signal subject into a monitor row.

    The gauge obeys the generalised honesty rules verbatim (ADR-0116 §6):
    the scale runs 0 → the subject's **trigger threshold** on every row of
    every family, the mark sits at that subject's effective warn fraction of
    it, and a crossing clamps the rendered fill while the printed Value and
    Threshold stay honest. A no-data row draws no gauge at all — a bar at
    zero would claim calm, which is the one thing the producer refused to
    say.
    """
    subject_key = watchpoint.subject_key
    finding_id = fired.get(subject_key)
    warn_threshold_pct = resolution.warn_threshold_for(subject_key)
    fill_pct = 0.0
    calm = False

    if isinstance(result, SignalObservation):
        value, threshold = _signal_figures(watchpoint, result)
        status_label = signal_status_label(result.status)
        modifier = _SIGNAL_MODIFIER[result.status]
        calm = result.status == STATUS_OK
        # Percent of the way to the trigger threshold — the one quantity the
        # bar encodes, on the same 0 → threshold scale for every row, and
        # clamped so a crossing cannot overflow it. It is deliberately the
        # bar's *geometry* only and is never printed: for ``liquidity`` the
        # proportion and the internal 100-scale magnitude are the same
        # number, and that number is not one the operator ever set.
        fill_pct = min(float(result.magnitude / result.threshold_pct * Decimal("100")), 100.0)
        observed = True
    else:
        value = threshold = "—"
        status_label = _NO_DATA_LABEL
        modifier = _NO_DATA_MODIFIER
        observed = False

    return {
        "subject_key": subject_key,
        "family": watchpoint.family,
        "watchpoint_id": str(watchpoint.watchpoint_id),
        "display_name": watchpoint.display_name,
        "status": status_label,
        "modifier": modifier,
        "has_data": observed,
        "calm": calm,
        "value": value,
        "threshold": threshold,
        "fill_pct": fill_pct,
        # Per-subject *positioned*, never per-subject rescaled.
        "warn_threshold_pct": float(warn_threshold_pct),
        "muted": watchpoint.muted,
        # No lock here: the un-mutable-breach rule is quota-only (ADR-0116
        # §3 as scoped in P4). A watchpoint the operator set themselves may
        # be silenced at any status — no regulatory floor stands behind it.
        "mute_locked": False,
        "warn_customised": watchpoint.warn_threshold_pct is not None,
        "delta_customised": watchpoint.re_trigger_delta is not None,
        "editor_id": _dom_id("dc-watchpoint", subject_key),
        "fired_this_beat": finding_id is not None,
        "finding_anchor": (f"#dc-card-{finding_id}" if finding_id is not None else None),
        "note": _signal_note(
            watchpoint=watchpoint,
            result=result,
            watch=watch_by_subject.get(subject_key),
            fired=finding_id is not None,
            re_trigger_delta=resolution.re_trigger_delta_for(subject_key),
            warn_threshold_pct=warn_threshold_pct,
        ),
    }


def _build_signal_group(
    family: str,
    observations: list[tuple[SignalWatchpoint, SignalResult]],
    *,
    watch_by_subject: dict[str, IreneWatchStateDTO],
    fired: dict[str, str],
    resolution: WatchDeskResolution,
) -> dict[str, Any]:
    """Project one signal family's observations into a monitor group.

    Args:
        family: One of the four defined families.
        observations: The family's ``(watchpoint, result)`` pairs, in the
            order :func:`~services.watch_desk.signal_observation.observe_signal_families`
            returned them — which is the order the beat evaluated them in.
        watch_by_subject: Watch-state rows indexed by ``subject_key``.
        fired: ``subject_key`` → finding id for subjects that fired in the
            current beat.
        resolution: The tenant's effective calibration — the same object the
            beat runs on.

    Returns:
        The group's template context. For an exception-first family the
        calm rows are moved to ``collapsed_rows`` behind a summary line;
        they are **collapsed, never dropped**, and both halves are counted
        in the header.
    """
    rows = [
        _signal_row(
            watchpoint=watchpoint,
            result=result,
            watch_by_subject=watch_by_subject,
            fired=fired,
            resolution=resolution,
        )
        for watchpoint, result in observations
    ]

    if family in _EXCEPTION_FIRST_FAMILIES:
        listed = [row for row in rows if not row["calm"]]
        collapsed = [row for row in rows if row["calm"]]
    else:
        listed, collapsed = rows, []

    status_counts = Counter(row["status"] for row in rows)
    badges: list[dict[str, str]] = []
    for signal_status in _SIGNAL_SEVERITY:
        label = signal_status_label(signal_status)
        if status_counts.get(label):
            badges.append(
                {
                    "text": f"{status_counts[label]} {label}",
                    "modifier": _SIGNAL_MODIFIER[signal_status],
                }
            )
    if status_counts.get(_NO_DATA_LABEL):
        badges.append(
            {
                "text": f"{status_counts[_NO_DATA_LABEL]} {_NO_DATA_LABEL.lower()}",
                "modifier": _NO_DATA_MODIFIER,
            }
        )

    return {
        "family": family,
        "name": _SIGNAL_GROUP_NAMES[family],
        "measure": _SIGNAL_GROUP_MEASURES[family],
        "badges": badges,
        "subject_count": len(rows),
        "muted_count": sum(1 for row in rows if row["muted"]),
        "rows": listed,
        "collapsed_rows": collapsed,
        "collapsed_summary": (f"{len(collapsed)} fresh — show all" if collapsed else None),
        "can_add": family in _ADDABLE_FROM_GROUP_HEADER,
    }


def _signal_subject_count(signal_groups: list[dict[str, Any]]) -> int:
    """Sum what the monitor's signal groups say they watch.

    The Briefing's "Subjects watched" tile sits directly above the monitor,
    so the two must never answer "what is watched" twice. They do not: the
    tile's signals figure is *these* group headers added up — the same
    ``subject_count`` each one renders — and not a second pass over the
    resolution and the book. Adding a fifth family therefore moves both
    numbers at once, and no counting rule has to be restated anywhere.

    That single source is also what carries P7's pinned rules over for
    free, because each is already true of ``subject_count``:

    * a **muted** subject counts — a muted row stays in its group and in
      its header (ADR-0116 §3), and the tile must not disagree with the
      header about that;
    * a **no-data** subject counts — the producer refusing to measure
      something is not the same as nobody watching it;
    * a **retired** identity does not — it is absent from the resolution
      the groups were built from, which is how retirement stops evaluation
      and rendering alike;
    * an **exception-first** family contributes both halves, since
      ``subject_count`` is taken before the calm remainder is collapsed.

    Args:
        signal_groups: The monitor context's ``signal_groups``, as built by
            :func:`_build_monitor`.

    Returns:
        The number of signal-family subjects the monitor is showing.
    """
    return sum(int(group["subject_count"]) for group in signal_groups)


def _signal_footer(rendered: set[str], *, resolution: WatchDeskResolution) -> list[dict[str, Any]]:
    """Describe the families that rendered no group, and what may be done.

    An empty group is noise, so a family nobody watches is skipped entirely
    (ADR-0116 §6) — but skipping it silently would hide the capability, so
    the section footer carries one compact line per absent family. The
    house empty-state idiom: prose in ``.pf-empty-state``, with the action
    beside it (see ``_cases_body.html``).

    The two singleton families are offered an "Add" only when **no live
    identity** exists. ``signals_for`` answers about the *effective* set, so
    a version dated in the future would read as absent here while the
    repository still refuses a second identity — which is exactly why the
    create path renders that refusal inline rather than trusting this.

    Args:
        rendered: The families that produced a group this render.
        resolution: The tenant's effective calibration.

    Returns:
        One entry per absent family, in the shared family order.
    """
    footer: list[dict[str, Any]] = []
    for family in SIGNAL_FAMILY_ORDER:
        if family in rendered:
            continue
        watched = bool(resolution.signals_for(family))
        footer.append(
            {
                "family": family,
                "name": _SIGNAL_GROUP_NAMES[family],
                "measure": _SIGNAL_GROUP_MEASURES[family],
                # A watched singleton with nothing to show is not an
                # invitation to add a second one — say what is true instead.
                "can_add": not watched,
                "watched": watched,
            }
        )
    return footer


def _fired_this_beat(
    findings: list[IreneFindingDTO], last_beat_at: datetime | None
) -> tuple[dict[str, str], dict[str, str]]:
    """Split the open findings created in the current beat by subject and tag.

    Without a completed beat there is no "since" to test against, so the
    fired sets are empty rather than guessed.

    Args:
        findings: The open findings already loaded for the feed.
        last_beat_at: The schedule's last beat instant, or ``None``.

    Returns:
        A ``(by_subject, by_tag)`` pair, each mapping to the finding id
        the monitor anchors to. ``findings`` arrives in urgency-then-
        recency order, so the first hit per key is the one to link.
    """
    by_subject: dict[str, str] = {}
    by_tag: dict[str, str] = {}
    if last_beat_at is None:
        return by_subject, by_tag
    for finding in findings:
        if finding.created_at < last_beat_at:
            continue
        by_subject.setdefault(finding.subject_key, str(finding.id))
        tag = (finding.payload or {}).get("tag")
        if isinstance(tag, str) and tag in _KNOWN_TAGS:
            by_tag.setdefault(tag, str(finding.id))
    return by_subject, by_tag


async def _build_monitor(
    db_session: AsyncSession,
    *,
    findings: list[IreneFindingDTO],
    schedule: IreneScheduleDTO | None,
    effective_labels: dict[str, str],
    resolution: WatchDeskResolution,
    now: datetime,
) -> dict[str, Any]:
    """Build the "What Irene watches" monitor context (ADR-0089).

    Runs inside the caller's ``tenant_context``: one coverage computation,
    one batched fetch per watched signal family, one bulk watch-state read,
    and the findings the feed already loaded — no per-subject lookup loop.
    Coverage is computed **now** (D4); the beat's timestamp feeds only the
    acknowledged-magnitude / fired-this-beat arithmetic — the monitor head
    carries no observation-time claim.

    The signal groups are derived the same way and from the same code the
    beat runs (ADR-0116 §6): one resolution, then
    :func:`~services.watch_desk.signal_observation.observe_signal_families`,
    which is read-only — rendering a row must never advance a subject's
    state machine. Nothing here reads ``irene_watch_state`` for a *status*;
    the acknowledged figures it does read serve the note alone.

    Coverage unavailability is never an error here: an empty universe, a
    range without a month-end Stichtag, and an engine-level refusal all
    degrade to the same empty internal groups. The Briefing is the primary
    surface and must render.

    Args:
        db_session: The tenant-scoped session opened by the caller.
        findings: The open findings already loaded for the feed.
        schedule: The tenant's schedule row, or ``None``.
        effective_labels: ``family`` → effective limit-set label, for the
            families that have one.
        resolution: The tenant's effective calibration and per-subject
            overlays, resolved once by the caller through
            :func:`services.watch_desk.overlay.resolve_watch_desk` — the
            same function the beat resolves through (ADR-0116 §1).
        now: The render instant. Its date is the signal families' evaluation
            date, which is why a row and the next beat's classification of
            the same subject agree by construction.

    Returns:
        The ``monitor`` template context.
    """
    service = _build_coverage_service(db_session)
    bundle: LimitsCoverageBundle | None
    try:
        bundle = await service.get_coverage(warn_threshold_pct=resolution.warn_default_pct)
    except (
        LimitSetNotEffective,
        CoverageInputMissing,
        CoverageInputOutOfRange,
        MissingFxRateError,
    ) as exc:
        logger.info(
            "watch-desk monitor: coverage unavailable (%s) — rendering the empty internal groups.",
            exc,
        )
        bundle = None

    watch_by_subject = {
        row.subject_key: row for row in await IreneWatchStateRepository(db_session).list_all()
    }
    fired_by_subject, fired_by_tag = _fired_this_beat(
        findings, schedule.last_beat_at if schedule is not None else None
    )

    latest_as_of_date = bundle.latest_as_of_date if bundle is not None else None
    coverage_available = bundle is not None and latest_as_of_date is not None

    groups = [
        _build_family_group(
            family=family,
            coverage=(getattr(bundle, family).coverage if coverage_available else None),
            latest_as_of_date=latest_as_of_date,
            watch_by_subject=watch_by_subject,
            fired=fired_by_subject,
            effective_label=effective_labels.get(family),
            resolution=resolution,
        )
        for family in _LIMIT_FAMILIES
    ]

    # Grouped in the order the observation layer returned them, which is the
    # order the beat evaluated them in — so the two surfaces never present
    # the same subjects in two orders, and there is no second family list
    # here to drift from that one.
    observations: dict[str, list[tuple[SignalWatchpoint, SignalResult]]] = {}
    for watchpoint, result in await observe_signal_families(
        db_session, as_of=now.date(), resolution=resolution
    ):
        observations.setdefault(watchpoint.family, []).append((watchpoint, result))

    signal_groups = [
        _build_signal_group(
            family,
            family_observations,
            watch_by_subject=watch_by_subject,
            fired=fired_by_subject,
            resolution=resolution,
        )
        for family, family_observations in observations.items()
    ]

    return {
        "coverage_available": coverage_available,
        "groups": groups,
        "rss": _build_rss_group(fired_by_tag, resolution=resolution),
        "signal_groups": signal_groups,
        "signal_footer": _signal_footer(set(observations), resolution=resolution),
    }


# ---------------------------------------------------------------------------
# Open case — the Watch Desk side of the Case workflow (ADR-0107, C4)
# ---------------------------------------------------------------------------


def _finding_headline(finding: IreneFindingDTO) -> str:
    """Return the case-title pre-fill for a finding (the mock's headline).

    The card's headline is the finding ``trigger`` (``pf-dc-card__trigger``),
    so a from-finding case pre-fills its title from it, falling back to the
    ``finding`` text and finally the ``subject_key`` so the title is never
    empty.
    """
    payload = finding.payload or {}
    trigger = str(payload.get("trigger") or "").strip()
    if trigger:
        return trigger
    finding_text = str(payload.get("finding") or "").strip()
    if finding_text:
        return finding_text
    return finding.subject_key


def _subject_family(subject_key: str) -> tuple[str | None, str | None]:
    """Split ``family:class_key`` into its parts; ``(None, None)`` if unkeyed."""
    prefix, sep, rest = subject_key.partition(":")
    if not sep:
        return None, None
    return prefix, rest


async def _freeze_materiality_lines(
    db_session: AsyncSession, finding: IreneFindingDTO
) -> list[str]:
    """Compute the frozen materiality-at-opening lines for a finding (C4).

    Binding decision 3 (ADR-0107, Gate-C0 decision D): the materiality is
    computed **live at the moment of opening** through the exact coverage
    wiring the monitor / the Limits section / Irene's delta layer share
    (:func:`_build_coverage_service`), so this becomes the fourth site that
    cannot disagree about a figure. The result is the presentation strings the
    C3a origin embed renders verbatim under "At case opening"; nothing
    client-supplied enters them.

    * ``saa:*`` / ``anlv:*`` — resolve the live per-subject coverage row and
      echo the beat's phrasing (coverage vs ceiling, headroom, live status).
      If no live figure resolves (limit since removed, subject unknown, empty
      universe, engine refusal) freeze **no** lines and log a warning — the
      origin embed then shows the finding-time ``basis`` alone; never invent a
      figure.
    * ``rss:*`` — no scalar exists, so freeze the finding's band line and its
      ``evidence_refs`` (calm phrasing), nothing numeric.
    * anything else — no keyed figure; freeze nothing.

    Args:
        db_session: The tenant-scoped session opened by the endpoint.
        finding: The open finding being handed over to a case.

    Returns:
        The frozen presentation strings, possibly empty.
    """
    payload = finding.payload or {}
    family, class_key = _subject_family(finding.subject_key)
    band_title = (finding.band or "informational").capitalize()

    if family == "rss":
        # No scalar to compute — the band line plus the audit-trail evidence
        # refs, calm phrasing, nothing numeric.
        return [
            f"{band_title} band at case opening.",
            *_as_str_list(payload.get("evidence_refs")),
        ]

    if family not in _LIMIT_FAMILIES or not class_key:
        logger.warning(
            "watch-desk open-case: subject %r carries no keyed materiality — freezing no lines.",
            finding.subject_key,
        )
        return []

    # The frozen status must be the one this subject is actually judged by,
    # so the freeze resolves through the same seam the monitor and the beat
    # do rather than restating a threshold (ADR-0116 §1).
    resolution = await resolve_watch_desk(db_session, as_of=_now())
    warn_threshold_pct = resolution.warn_threshold_for(finding.subject_key)

    service = _build_coverage_service(db_session)
    try:
        bundle = await service.get_coverage(warn_threshold_pct=resolution.warn_default_pct)
    except (
        LimitSetNotEffective,
        CoverageInputMissing,
        CoverageInputOutOfRange,
        MissingFxRateError,
    ) as exc:
        logger.warning(
            "watch-desk open-case: coverage unavailable for subject %r (%s) — freezing no lines.",
            finding.subject_key,
            exc,
        )
        return []

    latest = bundle.latest_as_of_date if bundle is not None else None
    if bundle is None or latest is None:
        logger.warning(
            "watch-desk open-case: no coverage Stichtag for subject %r — freezing no lines.",
            finding.subject_key,
        )
        return []

    coverage = getattr(bundle, family).coverage
    slice_ = coverage[
        (coverage["as_of_date"] == pd.Timestamp(latest)) & (coverage["class_key"] == class_key)
    ]
    records = slice_.to_dict("records")
    coverage_pct = _as_decimal(records[0]["coverage_pct"]) if records else None
    max_pct = _as_decimal(records[0]["max_pct"]) if records else None
    if coverage_pct is None or max_pct is None:
        # No live row, or an unconstrained (NO_LIMIT / UNALLOCATED) one with no
        # ceiling to gauge against — the limit was removed or the class fell
        # out of the effective set since the finding fired.
        logger.warning(
            "watch-desk open-case: no live constrained figure for subject %r — freezing no lines.",
            finding.subject_key,
        )
        return []

    record = records[0]
    headroom = _as_decimal(record.get("headroom_eur"))
    # Re-classified against this subject's own WARN threshold, exactly as
    # the monitor and the beat do; the engine classified the whole frame
    # against the tenant default in one pass.
    status = classify_coverage_status(coverage_pct, max_pct, warn_threshold_pct)
    lines = [f"Coverage {coverage_pct:.2f}% against a {max_pct:.2f}% ceiling."]
    if headroom is not None:
        lines.append(f"Headroom {headroom:,.0f} EUR.")
    lines.append(f"Status {status} at case opening.")
    return lines


def _opencase_error(
    request: Request,
    message: str,
    *,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Return the calm inline error that replaces a stale card (ADR-0107, C4).

    Swapped into the card's own slot (``outerHTML``) with a 200 so HTMX
    replaces the stale card with the notice; the next feed refresh clears the
    remainder. Reuses the Watch Desk's inline-error partial — the one idiom.

    Args:
        request: The active request (template context).
        message: The calm, user-facing sentence.
        status_code: Override for a request the UI never issues — the
            informational band gate answers 422, the resolve endpoint's idiom
            for a well-formed request the domain refuses (ADR-0120 §3). A
            *stale* card stays 200: that one is an ordinary race, not a
            malformed ask.
    """
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_resolve_error.html",
            {"message": message},
            status_code=status_code,
        ),
    )


def _project_journal_entry(
    dto: IreneFindingDTO,
    actor_names: dict[UUID, str],
    case_by_finding: dict[UUID, CaseDTO],
) -> dict[str, Any]:
    """Project one resolved finding into a Journal row (ADR-0089 / ADR-0107).

    An ``opened_case`` row shows the hand-over: the "Opened case" label and a
    link to the case the finding was opened as (``case_by_finding``, resolved
    in one batch for the page — never a per-row lookup).
    """
    payload = dto.payload or {}
    actor = actor_names.get(dto.resolved_by) if dto.resolved_by is not None else None
    case = case_by_finding.get(dto.id)
    return {
        "row_type": "finding",
        "id": str(dto.id),
        "subject_key": dto.subject_key,
        "trigger": payload.get("trigger", ""),
        "band": dto.band,
        "resolution": dto.resolution,
        "resolution_label": resolution_label(dto.resolution),
        "actor": actor or (str(dto.resolved_by) if dto.resolved_by else None),
        "created_at": dto.created_at,
        "resolved_at": dto.resolved_at,
        # The event time the merge sorts on (falls back to creation on the
        # defensive chance a resolved row carries no resolved_at).
        "event_at": dto.resolved_at or dto.created_at,
        "case_href": f"/cases/{case.id}" if case is not None else None,
    }


def _project_journal_case(case: CaseDTO, actor_names: dict[UUID, str]) -> dict[str, Any]:
    """Project one closed case into a Journal row (ADR-0107, C4).

    The Journal's second render-time source (Gate-C0 decision B): a sibling of
    the finding rows, not a new species. Case badge + title, the closer's name
    (the actor-name batch idiom), opened/closed dates, a closing-note excerpt
    (truncated in the template, never in the data), and a link to the case.
    """
    closer_id = case.closed_by if case.closed_by is not None else case.opened_by
    closer = actor_names.get(closer_id) or str(closer_id)
    return {
        "row_type": "case",
        "badge": f"CASE-{case.case_number:04d}",
        "title": case.title,
        "closing_note": case.closing_note,
        "actor": closer,
        "created_at": case.opened_at,
        "resolved_at": case.closed_at,
        "event_at": case.closed_at or case.opened_at,
        "case_href": f"/cases/{case.id}",
    }


# ---------------------------------------------------------------------------
# Briefing — GET the calm-by-default card feed
# ---------------------------------------------------------------------------


async def _briefing_context(
    db_session: AsyncSession,
    *,
    csrf_token: str,
    schedule: IreneScheduleDTO | None,
    now: datetime,
) -> dict[str, Any]:
    """Build the Briefing body's template context — one full render.

    Extracted from :func:`get_briefing` so the post-enqueue poll renders
    *the* Briefing rather than a second projection of it: the refresh a
    landed beat triggers has to be indistinguishable from a reload, tiles,
    feed and monitor alike. A second builder would agree on the day it was
    written and drift on the first tile that changed.

    The schedule is passed in rather than read here because both callers
    already need it for a decision of their own — the button's visibility,
    and the poll's done condition — and one read answers both.

    Runs inside the caller's ``tenant_context``: every read below is
    RLS-policed by it, and none of them writes.

    Args:
        db_session: The tenant-scoped session.
        csrf_token: Session CSRF token for the feed's action forms.
        schedule: The tenant's ``irene_schedule`` row, or ``None``.
        now: The current instant (timezone-aware UTC).

    Returns:
        The template context for ``_partials/watch_desk_briefing.html``.
    """
    finding_repo = IreneFindingRepository(db_session)
    findings = await finding_repo.list_open()

    # Only meaningful once a beat has run; without one there is no
    # "since" to count from and the tile shows the never-ran state.
    findings_since_beat: int | None = None
    if schedule is not None and schedule.last_beat_at is not None:
        findings_since_beat = await finding_repo.count_since(since=schedule.last_beat_at)

    # The internal subjects the beat watches. A family with no effective
    # set contributes 0.
    # The set labels are carried on for the monitor's group meta.
    limits_repo = LimitsRepository(db_session)
    internal_subject_count = 0
    effective_labels: dict[str, str] = {}
    for family in _LIMIT_FAMILIES:
        effective = await limits_repo.get_effective_set(family, now.date())
        if effective is None:
            continue
        effective_labels[family] = effective.label
        internal_subject_count += len(await limits_repo.list_limits(effective.id))

    # One resolution for the whole render — the same function the beat
    # resolves through, so a monitor row and the beat's classification
    # of that subject cannot disagree (ADR-0116 §1).
    resolution = await resolve_watch_desk(db_session, as_of=now)
    monitor = await _build_monitor(
        db_session,
        findings=findings,
        schedule=schedule,
        effective_labels=effective_labels,
        resolution=resolution,
        now=now,
    )

    return {
        "csrf_token": csrf_token,
        "cards": [_project_card(finding) for finding in findings],
        "schedule_exists": schedule is not None,
        "tiles": _build_tiles(
            findings=findings,
            schedule=schedule,
            findings_since_beat=findings_since_beat,
            internal_subject_count=internal_subject_count,
            # The monitor's own group figures, summed — so the tile and the
            # group headers beneath it are one count rendered twice.
            signal_subject_count=_signal_subject_count(monitor["signal_groups"]),
            now=now,
        ),
        "monitor": monitor,
    }


@router.get("/api/watch-desk/briefing", response_class=HTMLResponse)
async def get_briefing(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Briefing section body — the calm-by-default card feed.

    An empty feed is rendered as the affirmative calm state (a green status
    line plus a collapsed lower-priority strip), the product thesis made
    visible — never an empty-list or error partial (ADR-0089). A non-empty
    feed renders the cards in ``list_open()`` order (final urgency desc,
    then recency), band-coloured, with no raw 1–10 badge.

    Above the feed sit the four status tiles (last beat / subjects watched /
    open findings / next beat). Every tile figure is derived at request time
    from the reads this route already performs plus one COUNT and the
    effective-limit-set enumeration — no summary table, no new persistence.
    "Subjects watched" counts the signal families too, and takes that figure
    off the monitor's own group headers rather than enumerating what is
    watched a second time (:func:`_signal_subject_count`).

    Beneath the feed sits the "What Irene watches" monitor: the constrained
    SAA / AnlV coverage rows on a shared 0 → ceiling utilisation gauge, the
    curated RSS dimensions, and Irene's deterministic per-subject note.
    Coverage there is computed **now** through the limits engine rather than
    read from the beat's snapshot, so the monitor and the Limits section can
    never disagree; it adds one coverage computation and one bulk watch-state
    read inside this route's single ``tenant_context``.

    The "Request analysis" button is shown only when the tenant has an
    ``irene_schedule`` row to bring due; without one there is nothing to
    enqueue against, so the button is hidden.
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        schedule = await IreneScheduleRepository(db_session).get_for_tenant()
        context = await _briefing_context(
            db_session,
            csrf_token=session.csrf_token,
            schedule=schedule,
            now=now,
        )

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_briefing.html",
            context,
        ),
    )


# ---------------------------------------------------------------------------
# Briefing poll — the post-enqueue refresh, time-boxed and self-terminating
# ---------------------------------------------------------------------------


@router.get("/api/watch-desk/briefing/poll", response_class=HTMLResponse)
async def poll_briefing(
    request: Request,
    since: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> Response:
    """Answer "has a beat landed since ``since``" for the enqueue's poller.

    "Request analysis" only *enqueues* (ADR-0086): the beat runs a tick
    later in the scheduler (ADR-0117), and the page that fired the enqueue
    has no way to learn that it did. This endpoint closes that gap without
    a push channel — the confirmation partial starts a 15-second HTMX poll
    against it, and the poll ends itself:

    * **landed** — ``last_beat_at >= since``: 286 carrying the re-rendered
      Briefing body. 286 cancels the poll, and the ``outerHTML`` swap of
      ``#dc-briefing`` removes the poller with the markup it replaces, so
      the refresh cannot leave a second poller behind.
    * **pending** — the schedule exists and no beat has landed yet: 204,
      which HTMX does not swap, so the page stands and the poll continues.
      This is the branch that runs ~4 times a minute, and it is one indexed
      row read: no resolution, no coverage computation, no render.
    * **stop, no swap** — 286 with an empty body and ``HX-Reswap: none``
      when there is nothing left to wait for: a malformed ``since``, no
      schedule row, or a ``since`` older than :data:`_POLL_HORIZON`. The
      horizon is what caps a tab left open on a beat that never runs; the
      client carries no timeout of its own.

    Read-only throughout, and deliberately on ``require_session`` rather
    than ``require_authenticated_session``: a poll must not keep a session
    alive that the operator has stopped using. A session that expires
    mid-poll gets that dependency's 401 + ``HX-Redirect``, which navigates
    the tab to ``/login`` instead of polling on unauthenticated.

    Args:
        request: The FastAPI request.
        since: The enqueue instant, ISO 8601 and timezone-aware, as written
            into the poller's URL by :func:`request_analysis`.
        session: The authenticated session.

    Returns:
        286 (with or without a body) or 204, per the branches above.
    """
    parsed_since = _parse_poll_since(since)
    if parsed_since is None:
        return _poll_stop()

    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        schedule = await IreneScheduleRepository(db_session).get_for_tenant()
        if schedule is None:
            # The row a beat would advance is gone — nothing can land.
            return _poll_stop()

        landed = schedule.last_beat_at is not None and schedule.last_beat_at >= parsed_since
        if not landed:
            if now - parsed_since > _POLL_HORIZON:
                return _poll_stop()
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        context = await _briefing_context(
            db_session,
            csrf_token=session.csrf_token,
            schedule=schedule,
            now=now,
        )

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_briefing.html",
            context,
            status_code=_POLL_STOP_STATUS,
        ),
    )


# ---------------------------------------------------------------------------
# Request analysis — enqueue an out-of-cadence beat (no inline synthesis)
# ---------------------------------------------------------------------------


@router.post("/api/watch-desk/request-analysis", response_class=HTMLResponse)
async def request_analysis(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Enqueue an out-of-cadence beat by bringing the schedule due now.

    Per ADR-0086 / ADR-0089 this **does not** run synthesis inline: it only
    sets the tenant schedule's ``next_due_at = now`` (via
    :meth:`IreneScheduleRepository.enqueue_due_now`) so the next scheduler
    tick claims the tenant and runs a beat — at most one tick interval away
    (ADR-0117: 60 seconds by default). The response is a small confirmation
    partial. When no schedule row exists there is nothing to enqueue against
    — a defensive branch returns a "set a cadence first" partial (the button
    is normally hidden in that state).

    The confirmation carries an out-of-band fragment for the fourth status
    tile. Without it the tile keeps showing the *pre*-enqueue ``next_due_at``
    — the enqueue would land silently and read as a no-op. The tile is
    re-projected from the row as re-read after the write, not from the value
    this route passed in, so the operator sees persisted state.

    It also carries the poller that waits for the beat this enqueue asked
    for (:func:`poll_briefing`), keyed on ``now`` — **this** route's instant,
    never the client clock, which may sit minutes off and would make the
    done condition fire early or never. Nothing starts a poll but this
    response, and the no-schedule branch starts none: it enqueued nothing,
    so there is nothing to wait for.
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        schedule_repo = IreneScheduleRepository(db_session)
        schedule = await schedule_repo.get_for_tenant()
        if schedule is not None:
            await schedule_repo.enqueue_due_now(now=now)
            schedule = await schedule_repo.get_for_tenant()

    logger.info(
        "watch-desk request-analysis: tenant=%s user=%s enqueued=%s",
        session.tenant_id,
        session.user_id,
        schedule is not None,
    )
    zone, utc_fallback = _resolve_zone(schedule.timezone if schedule is not None else None)
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_request_analysis_result.html",
            {
                "no_schedule": schedule is None,
                # Shaped like the Briefing's own context so the tile partial
                # renders identically in both places.
                "tiles": {
                    "next_beat": _next_beat_tile(
                        schedule=schedule,
                        now=now,
                        zone=zone,
                        utc_fallback=utc_fallback,
                    )
                },
                # The poller's ``since``, percent-encoded because an ISO
                # 8601 offset carries a "+" — read as a space in a query
                # string, which would make the stamp unparseable and stop
                # the poll on its first tick.
                "poll_since": quote(now.isoformat(), safe=""),
            },
        ),
    )


# ---------------------------------------------------------------------------
# Resolution — record the PM's response, move the card to the Journal
# ---------------------------------------------------------------------------


@router.post(
    "/api/watch-desk/findings/{finding_id}/resolve",
    response_class=HTMLResponse,
)
async def resolve_finding(
    request: Request,
    finding_id: UUID,
    resolution: str = Form(...),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Record a finding's resolution and return the refreshed feed fragment.

    ``resolution`` is one of ``acted`` / ``dismissed`` / ``acknowledged``
    (UI labels Acted / Dismissed / Acknowledged; persisted lowercase). On
    success the finding's three resolution columns are written (with
    ``resolved_by`` = the session user), the card leaves the Briefing feed,
    and it will appear in the Journal. Findings are otherwise immutable
    (ADR-0085) — only these columns change. Dismissal is a **pure audit
    resolution**: it trains no suppression in v0 (ADR-0089).

    An invalid ``resolution`` value returns a 422 inline error partial —
    never a 500 — and does not touch the row.
    """
    normalized = resolution.strip().lower()
    if normalized not in _ALLOWED_RESOLUTIONS:
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/watch_desk_resolve_error.html",
                {
                    "message": (
                        f"Invalid resolution {resolution!r}; expected one of "
                        "Acted / Dismissed / Acknowledged."
                    )
                },
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        repo = IreneFindingRepository(db_session)
        try:
            await repo.resolve(
                finding_id=finding_id,
                resolution=normalized,
                resolved_by=session.user_id,
                resolved_at=_now(),
            )
        except IreneResolutionInvalid as exc:
            # Defence in depth: the route already whitelisted the value, so
            # this is only reachable on a vocabulary drift. Surface it as a
            # 422 inline error, never a 500.
            logger.warning(
                "watch-desk resolve: repository rejected %r: %s",
                normalized,
                exc,
            )
            user_msg, _error_id = user_safe_error(exc)
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/watch_desk_resolve_error.html",
                    {"message": user_msg},
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                ),
            )
        findings = await repo.list_open()

    logger.info(
        "watch-desk resolve: tenant=%s user=%s finding=%s resolution=%s",
        session.tenant_id,
        session.user_id,
        finding_id,
        normalized,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_briefing_feed.html",
            {
                "csrf_token": session.csrf_token,
                "cards": [_project_card(finding) for finding in findings],
            },
        ),
    )


# ---------------------------------------------------------------------------
# Open case — compose CaseRepository.create + resolve('opened_case') atomically
# ---------------------------------------------------------------------------


@router.post(
    "/api/watch-desk/findings/{finding_id}/open-case",
    response_class=HTMLResponse,
)
async def open_case(
    request: Request,
    finding_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Open a Case from a finding — the Watch Desk side of ADR-0107, C4.

    Composes the two existing seams in **one** tenant-context transaction
    (binding decision 1): ``CaseRepository.create`` and
    ``IreneFindingRepository.resolve(..., 'opened_case')`` happen together or
    not at all — if either raises, the whole transaction rolls back and
    neither the case nor its ``opened`` entry survives. The materiality is
    frozen server-side at this moment through the shared coverage wiring
    (binding decision 3); nothing client-supplied enters the payload. The
    fifth resolution is written **only** here (binding decision 5).

    The band gate is enforced here too (ADR-0120 §3): only a ``noteworthy``
    or ``critical`` finding may be handed to a case. An informational one is
    refused with a 422 inline error before any composition work — the
    template never offers the affordance, but the endpoint does not rely on
    that.

    Idempotent under double-submit and a stale feed: a finding already
    ``opened_case`` is not re-opened — the PM is redirected to the existing
    case (``get_by_finding``), so a second click, or a click on a card the
    feed had not yet refreshed away, lands on the record rather than creating
    a duplicate. Any other non-open resolution means the card is stale; the
    calm inline error replaces it and the next feed refresh clears the rest. A
    missing finding is the 404 idiom.

    On success the finding leaves the Briefing feed by virtue of its
    resolution — no feed surgery here — and an ``HX-Redirect`` sends the PM to
    the pre-filled case (the investments/close full-navigation idiom).
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        finding_repo = IreneFindingRepository(db_session)
        finding = await finding_repo.get(finding_id)
        if finding is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Finding not found.",
            )

        if finding.band not in _CASE_BANDS:
            # Defence in depth (ADR-0120 §3): the band gate holds server-side,
            # independent of what any template renders. An informational
            # finding is acknowledged, never case-opened (ADR-0107 D1) — and
            # the refusal lands *before* any composition work, so no case,
            # resolution or frozen materiality is written.
            logger.info(
                "watch-desk open-case: finding=%s is band %r — not case-openable.",
                finding_id,
                finding.band,
            )
            return _opencase_error(
                request,
                "Informational findings are acknowledged, not opened as cases. "
                "Open a case manually from the Cases area if this needs one.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        case_repo = CaseRepository(db_session)
        if finding.resolution == "opened_case":
            # Double-submit / stale feed: land on the existing record.
            existing = await case_repo.get_by_finding(finding_id)
            if existing is not None:
                logger.info(
                    "watch-desk open-case: tenant=%s finding=%s already "
                    "opened as case=%s — redirecting.",
                    session.tenant_id,
                    finding_id,
                    existing.id,
                )
                return HTMLResponse("", headers={"HX-Redirect": f"/cases/{existing.id}"})
            # Resolved opened_case but no case row: a torn prior write. Do not
            # silently re-open — surface the calm error, log for follow-up.
            logger.warning(
                "watch-desk open-case: finding=%s is opened_case but has no case row.",
                finding_id,
            )
            return _opencase_error(
                request,
                "This finding was already handed to a case, but the case "
                "could not be found. Refresh the briefing and try again.",
            )
        if finding.resolution != "open":
            logger.info(
                "watch-desk open-case: finding=%s is %s, not open — stale card.",
                finding_id,
                finding.resolution,
            )
            return _opencase_error(
                request,
                "This finding was already resolved — refresh the briefing to update the list.",
            )

        # Freeze materiality live, then compose the two seams in one
        # transaction: create raising (or resolve raising) rolls both back.
        lines = await _freeze_materiality_lines(db_session, finding)
        created = await case_repo.create(
            title=_finding_headline(finding),
            opened_by=session.user_id,
            finding_id=finding_id,
            opened_actor="system",
            opened_payload={"materiality_at_opening": {"lines": lines}},
            now=now,
        )
        await finding_repo.resolve(
            finding_id=finding_id,
            resolution="opened_case",
            resolved_by=session.user_id,
            resolved_at=now,
        )

    logger.info(
        "watch-desk open-case: tenant=%s user=%s finding=%s case=%s materiality_lines=%d",
        session.tenant_id,
        session.user_id,
        finding_id,
        created.id,
        len(lines),
    )
    return HTMLResponse("", headers={"HX-Redirect": f"/cases/{created.id}"})


# ---------------------------------------------------------------------------
# Journal — merged history: resolved findings + closed cases
# ---------------------------------------------------------------------------


@router.get("/api/watch-desk/journal", response_class=HTMLResponse)
async def get_journal(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Journal section body — the merged resolution history.

    Two render-time sources, merged newest-first (ADR-0107, C4 · Gate-C0
    decision B): the **resolved findings** (as before — subject, trigger,
    band, resolution, actor, timestamps — now including ``opened_case``
    hand-overs), and the **closed cases** (badge, title, closer, dates,
    closing-note excerpt). It remains a *projection* — no journal table, no
    entry numbers; a closed case that was opened manually (no finding) appears
    here too, the Gate-C0 gap this design closes.

    Read-only by contract (ADR-0089): no action buttons. Actor ids (finding
    ``resolved_by`` and case ``closed_by``) are resolved to display names in
    one batch, and the ``opened_case`` findings' cases are looked up once for
    the page (never per row), so every row is legible without a per-row DB
    hit.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        finding_repo = IreneFindingRepository(db_session)
        case_repo = CaseRepository(db_session)

        history = await finding_repo.list_journal(limit=100)
        # The Journal is the history of *resolved* findings (ADR-0089); the
        # still-open ones live in the Briefing feed, not here.
        findings = [f for f in history if f.resolution != "open"]
        closed_cases = await case_repo.list_closed(limit=100)

        # The cases the opened_case findings were handed to, resolved once for
        # the page (binding decision 1's hand-over link). A case may still be
        # open, so this is a finding→case lookup, not a slice of closed_cases.
        case_by_finding: dict[UUID, CaseDTO] = {}
        for finding in findings:
            if finding.resolution != "opened_case":
                continue
            case = await case_repo.get_by_finding(finding.id)
            if case is not None:
                case_by_finding[finding.id] = case

        # Actor names — finding resolvers and case closers — one batch.
        actor_ids: set[UUID] = {
            finding.resolved_by for finding in findings if finding.resolved_by is not None
        }
        actor_ids.update(
            case.closed_by if case.closed_by is not None else case.opened_by
            for case in closed_cases
        )
        user_repo = UserRepository(db_session)
        actor_names: dict[UUID, str] = {}
        for actor_id in actor_ids:
            user = await user_repo.get_by_id(actor_id)
            if user is not None:
                actor_names[actor_id] = user.display_name or user.email

    rows = [_project_journal_entry(finding, actor_names, case_by_finding) for finding in findings]
    rows.extend(_project_journal_case(case, actor_names) for case in closed_cases)
    # Newest event first (finding resolved_at vs case closed_at). Stable
    # tie-break: on an exact timestamp tie, findings precede cases, then id —
    # a deterministic order, documented so it never silently reshuffles. The
    # whole key sorts with reverse=True, so the finding rank is the higher (1)
    # to land it first on a tie.
    rows.sort(
        key=lambda row: (
            row["event_at"],
            1 if row["row_type"] == "finding" else 0,
            row.get("id") or row.get("case_href") or "",
        ),
        reverse=True,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_journal.html",
            {"entries": rows},
        ),
    )


# ---------------------------------------------------------------------------
# Calibration — the tuning surface (threshold facts + cadence)
# ---------------------------------------------------------------------------


def _cadence_context(
    schedule: Any, csrf_token: str, *, saved: bool = False, error: str | None = None
) -> dict[str, Any]:
    """Build the cadence-panel context from a schedule DTO (or defaults).

    Shared by the Calibration render and the cadence-save response so the
    panel round-trips identically. When ``schedule`` is ``None`` the panel
    shows sensible defaults for a tenant that has not configured Irene yet.
    """
    if schedule is None:
        current = {
            "cadence": "daily",
            "preferred_hour": _DEFAULT_PREFERRED_HOUR,
            "timezone": _DEFAULT_TIMEZONE,
            "enabled": True,
            "next_due_at": None,
            "configured": False,
        }
    else:
        current = {
            "cadence": schedule.cadence,
            "preferred_hour": (
                schedule.preferred_hour
                if schedule.preferred_hour is not None
                else _DEFAULT_PREFERRED_HOUR
            ),
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
            "next_due_at": schedule.next_due_at,
            "configured": True,
        }
    return {
        "csrf_token": csrf_token,
        "cadence_choices": _CADENCE_CHOICES,
        # Built through the helper so every rendered choice is guaranteed a
        # label — the template can index this map without an Undefined.
        "cadence_labels": {choice: cadence_label(choice) for choice in _CADENCE_CHOICES},
        "hours": list(range(24)),
        "current": current,
        "cadence_saved": saved,
        "cadence_error": error,
    }


def _calibration_label(key: str) -> str:
    """Return a display label for a floor / cap key, with a safe fallback."""
    return _CALIBRATION_KEY_LABELS.get(key, key.replace("_", " ").capitalize())


def _calibration_field(
    *,
    name: str,
    value: Any,
    label: str,
    customised: bool,
    hint: str | None = None,
    errors: Mapping[str, str] | None = None,
    supplied: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble one editor field, with its provenance marker and any error.

    ``value`` is the **effective** value — the editor always shows what the
    beat would run on, never a blank for "not overridden". ``customised``
    carries the provenance separately, derived from the stored revision
    (a NULL column means the code default, ADR-0116 §7).

    On a rejected save the attempted text is reflected back through
    ``supplied`` so the operator can correct it in place; the provenance
    marker deliberately keeps describing what is *stored*, because nothing
    was.
    """
    shown = supplied.get(name) if supplied is not None else None
    return {
        "name": name,
        "label": label,
        "value": str(value) if shown is None else shown,
        "customised": customised,
        "hint": hint,
        "error": (errors or {}).get(name),
    }


def _calibration_context(
    resolution: WatchDeskResolution,
    calibration: FloorCalibrationDTO | None,
    csrf_token: str,
    *,
    saved: bool = False,
    form_error: str | None = None,
    errors: Mapping[str, str] | None = None,
    supplied: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the Calibration editor context (ADR-0116 §7).

    Every field renders its **effective** value — defaults ⊕ the tenant's
    revision — and carries a separate "default / customised" marker taken
    from the sparse stored row. The form therefore posts a complete desired
    configuration, which is exactly what
    :func:`services.watch_desk.calibration.save_calibration_revision` takes;
    reducing it back to deviations (so a field returned to its default is
    stored as NULL again) is that function's job and is deliberately not
    duplicated here or in the browser.

    Args:
        resolution: The tenant's effective calibration.
        calibration: The stored revision behind it, or ``None`` for a
            tenant running on pure defaults.
        csrf_token: The session token the form posts with.
        saved: Render the "saved" flash.
        form_error: A validation message with no field to attach to.
        errors: Field name → message, rendered inline at that field.
        supplied: Field name → attempted text, reflected back on a refusal.

    Returns:
        The editor's template context.
    """
    config = resolution.config
    stored_floor = calibration.floor if calibration is not None else {}
    stored_cap = calibration.cap if calibration is not None else {}
    stored_delta = calibration.re_trigger_delta if calibration is not None else {}

    def field(name: str, value: Any, label: str, customised: bool) -> dict[str, Any]:
        return _calibration_field(
            name=name,
            value=value,
            label=label,
            customised=customised,
            hint=_CONSTRAINT_HINTS.get(name) or _FIELD_HINTS.get(name),
            errors=errors,
            supplied=supplied,
        )

    boundary_0, boundary_1 = config.band_boundaries
    boundaries_customised = calibration is not None and calibration.band_boundaries is not None
    return {
        "csrf_token": csrf_token,
        "calibration_saved": saved,
        "calibration_error": form_error,
        "warn_default": field(
            "warn_default_pct",
            resolution.warn_default_pct,
            "WARN default",
            calibration is not None and calibration.warn_default_pct is not None,
        ),
        # One paired control, both or neither — mirroring the schema CHECK
        # that stores the two boundary columns together (ADR-0116 §7).
        "band_boundaries": {
            "hint": _FIELD_HINTS["band_boundaries"],
            "customised": boundaries_customised,
            "error": (errors or {}).get("band_boundaries"),
            "lower": _calibration_field(
                name="band_boundary_0",
                value=boundary_0,
                label="Informational top",
                customised=boundaries_customised,
                errors=errors,
                supplied=supplied,
            ),
            "upper": _calibration_field(
                name="band_boundary_1",
                value=boundary_1,
                label="Noteworthy top",
                customised=boundaries_customised,
                errors=errors,
                supplied=supplied,
            ),
        },
        "options_gate": {
            **field(
                "options_min_band",
                config.options_min_band,
                "Options gate",
                calibration is not None and calibration.options_min_band is not None,
            ),
            "choices": _OPTIONS_BANDS,
        },
        "floors": [
            field(
                f"floor_{key}",
                config.floor[key],
                _calibration_label(key),
                key in stored_floor,
            )
            for key in FLOOR_COLUMNS
            if key in config.floor
        ],
        "caps": [
            field(
                f"cap_{key}",
                config.cap[key],
                _calibration_label(key),
                key in stored_cap,
            )
            for key in CAP_COLUMNS
            if key in config.cap
        ],
        "deltas": [
            {
                **field(
                    f"re_trigger_delta_{family}",
                    config.re_trigger_delta[family],
                    _DELTA_FAMILY_LABELS.get(family, family),
                    family in stored_delta,
                ),
                "hint": _RSS_DELTA_HINT if family == "rss" else None,
            }
            for family in DELTA_COLUMNS
            if family in config.re_trigger_delta
        ],
        # The pinned level, rendered as a locked row with its rationale and
        # no input at all — not submittable rather than merely styled
        # (ADR-0116 §7 invariant 1). The write path refuses the key too.
        "pinned": {
            "key": TRIGGER_FUND_CLOSURE,
            "label": _calibration_label(TRIGGER_FUND_CLOSURE),
            "value": config.floor[TRIGGER_FUND_CLOSURE],
            "rationale": (
                "Pinned at 10 — a fixed level, not calibration. A fund "
                "closing is the one event that always ranks top, so it has "
                "no column to be stored in and is not a tenant knob under "
                "any framing."
            ),
        },
        "notes": (calibration.notes if calibration is not None else None) or "",
    }


@router.get("/api/watch-desk/calibration", response_class=HTMLResponse)
async def get_calibration(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Calibration section body — the editor plus cadence.

    The tuning surface (ADR-0089), which ADR-0116 §7 grows from a
    three-cell fact display into the tenant calibration **editor**: the
    WARN default, the per-family re-trigger deltas, the band boundaries,
    the trigger-type floors, the source/trigger caps and the options gate,
    each showing its effective value with a default / customised marker.
    The four pinned invariants are not editable — ``fund_closure`` renders
    as a locked row, and the three coupling rules render as constraint
    hints on the fields they bind.

    Beneath it, the **watchpoint list** (ADR-0116 §7): one row per live
    identity of every family, with adjust / retire / history. It is not the
    per-subject inventory that retired with the DC4 rename — that stated
    live *status*, which the monitor owns; this states what is registered,
    which is a different question and the one an editor answers. The
    cadence settings panel is unchanged and still round-trips through
    ``POST .../cadence``.
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        schedule = await IreneScheduleRepository(db_session).get_for_tenant()
        resolution = await resolve_watch_desk(db_session, as_of=now)
        calibration = await FloorCalibrationRepository(db_session).effective_calibration(now)
        watchpoint_list = await _watchpoint_list_context(
            db_session, now=now, csrf_token=session.csrf_token
        )

    context = _calibration_context(resolution, calibration, session.csrf_token)
    context.update(watchpoint_list)
    context.update(_cadence_context(schedule, session.csrf_token))
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_calibration.html",
            context,
        ),
    )


def _form_decimal(raw: str, *, field: str) -> Decimal:
    """Parse one numeric editor field, or raise the typed validation error."""
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise FloorCalibrationInvalid(
            f"{field} must be a number; got {raw.strip()!r}.", field=field
        ) from exc


def _form_int(raw: str, *, field: str) -> int:
    """Parse one integer editor field, or raise the typed validation error."""
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise FloorCalibrationInvalid(
            f"{field} must be a whole number; got {raw.strip()!r}.", field=field
        ) from exc


def _read_calibration_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the editor's posted form into ``save_calibration_revision`` args.

    The form carries the **desired effective values**, including the fields
    left at their defaults — which is precisely the shape the write path
    takes, so nothing is reduced or filtered here.

    A ``fund_closure`` key is deliberately passed **through** rather than
    dropped: it has no input in the editor, so its presence means something
    posted it directly, and the sanctioned write path refuses it with the
    reason written for exactly that case. Silently ignoring it would turn a
    rejected write into a successful-looking one.

    Args:
        form: The parsed request form.

    Returns:
        Keyword arguments for
        :func:`services.watch_desk.calibration.save_calibration_revision`.

    Raises:
        FloorCalibrationInvalid: On any field that is not a number.
    """
    floors: dict[str, int] = {}
    caps: dict[str, int] = {}
    deltas: dict[str, Decimal] = {}

    for key in (*FLOOR_COLUMNS, TRIGGER_FUND_CLOSURE):
        name = f"floor_{key}"
        if name in form:
            floors[key] = _form_int(str(form[name]), field=name)
    for key in (*CAP_COLUMNS, TRIGGER_FUND_CLOSURE):
        name = f"cap_{key}"
        if name in form:
            caps[key] = _form_int(str(form[name]), field=name)
    for family in DELTA_COLUMNS:
        name = f"re_trigger_delta_{family}"
        if name in form:
            deltas[family] = _form_decimal(str(form[name]), field=name)

    notes = str(form.get("notes", "")).strip()
    return {
        "warn_default_pct": _form_decimal(
            str(form.get("warn_default_pct", "")), field="warn_default_pct"
        ),
        "band_boundaries": (
            _form_int(str(form.get("band_boundary_0", "")), field="band_boundary_0"),
            _form_int(str(form.get("band_boundary_1", "")), field="band_boundary_1"),
        ),
        "options_min_band": str(form.get("options_min_band", "")).strip(),
        "floor": floors,
        "cap": caps,
        "re_trigger_delta": deltas,
        "notes": notes or None,
    }


@router.post("/api/watch-desk/calibration", response_class=HTMLResponse)
async def save_calibration(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Persist one calibration revision and return the refreshed editor.

    The form posts the complete desired configuration;
    :func:`services.watch_desk.calibration.save_calibration_revision`
    composes it over the code defaults, runs the ``FloorConfig``
    constructor **and** the pinned invariants, reduces the result to
    deviations, and writes an immutable revision. This route adds no rule
    of its own — a second copy of the invariants here could drift from the
    one the beat composes against, which is the failure ADR-0116 §5 exists
    to prevent.

    A refusal returns 422 with the editor re-rendered: the service's
    message verbatim, inline at ``FloorCalibrationInvalid.field`` when it
    names one and as a form-level notice when it does not (a composition
    failure is about the configuration as a whole, not one field). The
    attempted values are reflected back so they can be corrected in place.

    The cadence panel is a sibling of this editor inside the section and is
    untouched by this endpoint.
    """
    form = getattr(request.state, "form", None) or await request.form()
    engine = _engine(request)
    now = _now()

    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        calibration_repo = FloorCalibrationRepository(db_session)
        try:
            await save_calibration_revision(
                calibration_repo, effective_from=now, **_read_calibration_form(form)
            )
        except FloorCalibrationInvalid as exc:
            resolution = await resolve_watch_desk(db_session, as_of=now)
            stored = await calibration_repo.effective_calibration(now)
            logger.info(
                "watch-desk calibration: rejected for tenant %s (field=%r): %s",
                session.tenant_id,
                exc.field,
                exc.message,
            )
            # The service writes its messages for a human to read, so they
            # are surfaced verbatim rather than re-phrased. Inline when the
            # named field has a slot in the editor; form-level otherwise —
            # a composition failure names no field at all, and a pinned key
            # names one the editor deliberately does not render.
            inline = exc.field in _EDITOR_FIELD_NAMES
            context = _calibration_context(
                resolution,
                stored,
                session.csrf_token,
                form_error=None if inline else exc.message,
                errors={exc.field: exc.message} if inline else {},
                supplied={key: str(value) for key, value in form.items()},
            )
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/watch_desk_calibration_editor.html",
                    context,
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                ),
            )

        resolution = await resolve_watch_desk(db_session, as_of=now)
        stored = await calibration_repo.effective_calibration(now)

    logger.info(
        "watch-desk calibration: tenant=%s user=%s revision saved.",
        session.tenant_id,
        session.user_id,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_calibration_editor.html",
            _calibration_context(resolution, stored, session.csrf_token, saved=True),
        ),
    )


# ---------------------------------------------------------------------------
# Sensitivity overlays — per-subject WARN / delta / mute (ADR-0116 §3, §6)
#
# The overlay half of the asymmetry: a watchpoint on a derived subject
# adjusts *how nervously* it is watched and nothing else. Subject identity
# and the ceiling stay with the limit set, which is why these endpoints
# offer neither, and why the schema would refuse them if they did.
# ---------------------------------------------------------------------------


def _overlay_display_name(family: str, subject_key: str) -> str:
    """Derive a first display name for a newly created overlay."""
    _, _, rest = subject_key.partition(":")
    if family == "rss":
        return f"Press dimension · {rest}"
    return f"{_FAMILY_LABELS.get(family, family)} · {rest}"


def _overlay_subject_error(subject_key: str) -> str | None:
    """Return why ``subject_key`` cannot carry an overlay, or ``None``.

    Only the *shape* is checked: the family must be one of the derived
    families, and an ``rss`` overlay must name a curated tag. The class key
    of a quota subject is deliberately **not** checked against the effective
    limit set — the schema keeps watchpoints and limits unlinked on purpose
    (ADR-0116 §3), and an overlay on a class that later leaves the set is
    legitimate history, not an error.
    """
    family, separator, rest = subject_key.partition(":")
    if not separator or not rest.strip():
        return f"{subject_key!r} is not a subject key of the form 'family:subject'."
    if family not in OVERLAY_FAMILIES:
        return (
            f"{family!r} subjects carry no sensitivity overlay; the derived "
            f"families are {', '.join(OVERLAY_FAMILIES)}."
        )
    if family == "rss" and rest not in _KNOWN_TAGS:
        return f"{rest!r} is not a curated press dimension."
    return None


async def _live_status_for(
    db_session: AsyncSession, subject_key: str, *, resolution: WatchDeskResolution
) -> str | None:
    """Return one quota subject's live coverage status, or ``None``.

    ``None`` covers every "cannot say" case — a press subject (no ceiling
    to gauge), an unavailable coverage computation, an unconstrained or
    absent row. The mute toggle is only *locked* on a definite BREACH, so
    an unknown status never locks it; the beat enforces the rule regardless
    of what this returns.
    """
    family, _, class_key = subject_key.partition(":")
    if family not in _LIMIT_FAMILIES:
        return None

    try:
        bundle = await _build_coverage_service(db_session).get_coverage(
            warn_threshold_pct=resolution.warn_default_pct
        )
    except (
        LimitSetNotEffective,
        CoverageInputMissing,
        CoverageInputOutOfRange,
        MissingFxRateError,
    ) as exc:
        logger.info(
            "watch-desk overlay: coverage unavailable for subject %r (%s).",
            subject_key,
            exc,
        )
        return None

    latest = bundle.latest_as_of_date if bundle is not None else None
    if bundle is None or latest is None:
        return None

    coverage = getattr(bundle, family).coverage
    slice_ = coverage[
        (coverage["as_of_date"] == pd.Timestamp(latest)) & (coverage["class_key"] == class_key)
    ]
    records = slice_.to_dict("records")
    if not records:
        return None
    coverage_pct = _as_decimal(records[0]["coverage_pct"])
    max_pct = _as_decimal(records[0]["max_pct"])
    if coverage_pct is None or max_pct is None:
        return None
    return classify_coverage_status(
        coverage_pct, max_pct, resolution.warn_threshold_for(subject_key)
    )


def _overlay_editor_context(
    *,
    subject_key: str,
    resolution: WatchDeskResolution,
    overlay: SubjectOverlay | None,
    live_status: str | None,
    csrf_token: str,
    return_to: str,
) -> dict[str, Any]:
    """Build the per-row sensitivity editor context."""
    family, _, _rest = subject_key.partition(":")
    return {
        "csrf_token": csrf_token,
        "subject_key": subject_key,
        # The drawer is opened from two surfaces since ADR-0116 §7 — the
        # monitor row and the Calibration watchpoint list — and each swaps
        # its own container. One editor, two targets; never two editors.
        "return_to": return_to,
        "target": _return_target(return_to),
        "family": family,
        # An rss overlay carries mute alone: a cluster subject is
        # non-scalar, so there is nothing for a threshold or a delta to
        # measure against — and the schema CHECK refuses them outright.
        "scalar": family != "rss",
        "editor_id": _dom_id("dc-overlay", subject_key),
        "muted": overlay is not None and overlay.muted,
        # The UI mirror of the beat-side rule (ADR-0116 §3), never its
        # enforcement: a subject muted while calm and breaching later is
        # still raised, and only the beat is present for that.
        "mute_locked": live_status == "BREACH",
        "warn_threshold_pct": (
            str(overlay.warn_threshold_pct)
            if overlay is not None and overlay.warn_threshold_pct is not None
            else ""
        ),
        "re_trigger_delta": (
            str(overlay.re_trigger_delta)
            if overlay is not None and overlay.re_trigger_delta is not None
            else ""
        ),
        "notes": (overlay.notes if overlay is not None else None) or "",
        "warn_default_pct": float(resolution.warn_default_pct),
        "delta_default": float(resolution.config.re_trigger_delta.get(family, Decimal("0"))),
    }


def _overlay_error(request: Request, message: str) -> HTMLResponse:
    """Return the inline error the sensitivity drawer renders in place."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_resolve_error.html",
            {"message": message},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ),
    )


@router.get("/api/watch-desk/watchpoints/overlay", response_class=HTMLResponse)
async def get_overlay_editor(
    request: Request,
    subject_key: str,
    return_to: str = "monitor",
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the sensitivity editor for one derived subject (ADR-0116 §6).

    Read-only: it renders the overlay currently in force (or an empty form
    when the subject has none) beside the tenant defaults the blanks fall
    back to, so "leave it empty" reads as "follow the default" rather than
    as "zero".
    """
    message = _overlay_subject_error(subject_key)
    if message is not None:
        return _overlay_error(request, message)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        resolution = await resolve_watch_desk(db_session, as_of=_now())
        live_status = await _live_status_for(db_session, subject_key, resolution=resolution)

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_sensitivity_editor.html",
            _overlay_editor_context(
                subject_key=subject_key,
                resolution=resolution,
                overlay=resolution.overlay_for(subject_key),
                live_status=live_status,
                csrf_token=session.csrf_token,
                return_to=return_to,
            ),
        ),
    )


def _optional_decimal(raw: str | None, *, field: str) -> Decimal | None:
    """Parse an optional numeric overlay field; blank means "the default".

    Blank is the meaningful case, and it is not zero: an empty field says
    "follow the tenant default", which is what a ``NULL`` column means and
    why the editor renders the default as a placeholder rather than as a
    value.
    """
    text_value = (raw or "").strip()
    if not text_value:
        return None
    try:
        return Decimal(text_value)
    except InvalidOperation as exc:
        raise WatchpointInvalid(
            f"{field} must be a number; got {text_value!r}. Leave the field "
            "empty to follow the default.",
            field=field,
        ) from exc


@router.post("/api/watch-desk/watchpoints/overlay", response_class=HTMLResponse)
async def save_overlay(
    request: Request,
    subject_key: str = Form(...),
    muted: str | None = Form(None),
    warn_threshold_pct: str | None = Form(None),
    re_trigger_delta: str | None = Form(None),
    notes: str | None = Form(None),
    return_to: str = Form("monitor"),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Create or revise one subject's overlay and return the fresh monitor.

    First touch on a subject **creates** the watchpoint identity;
    every later edit **revises** it, so the subject accumulates a version
    history rather than a mutated row (ADR-0116 §1). A revision states the
    complete calibration — which is what this form posts — and takes effect
    from now, never backdated.

    The whole monitor is re-rendered rather than the single row: the mark
    position, the muted tag, the group's muted count and the row's status
    all move together, and re-deriving them from one fresh resolution is
    the only way they cannot disagree.

    An ``rss`` subject may carry mute alone; the repository refuses the
    other two fields, and the schema refuses them under it. A bad value
    returns 422 with the repository's message inline — never a 500.
    """
    message = _overlay_subject_error(subject_key)
    if message is not None:
        return _overlay_error(request, message)

    family, _, _rest = subject_key.partition(":")
    engine = _engine(request)
    now = _now()

    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        repo = WatchpointRepository(db_session)
        try:
            warn_value = _optional_decimal(warn_threshold_pct, field="warn_threshold_pct")
            delta_value = _optional_decimal(re_trigger_delta, field="re_trigger_delta")
        except WatchpointInvalid as exc:
            return _overlay_error(request, exc.message)

        # "Which subjects does this tenant watch at all" — unbounded in
        # time, so a version dated in the future still counts as the
        # identity to revise rather than a second one to create.
        existing: WatchpointDTO | None = next(
            (
                row
                for row in await repo.list_live_identities(family=family)
                if row.subject_key == subject_key
            ),
            None,
        )
        try:
            if existing is None:
                await repo.create(
                    family=family,
                    subject_key=subject_key,
                    display_name=_overlay_display_name(family, subject_key),
                    effective_from=now,
                    muted=muted is not None,
                    warn_threshold_pct=warn_value,
                    re_trigger_delta=delta_value,
                    notes=(notes or "").strip() or None,
                )
            else:
                await repo.revise(
                    existing.watchpoint_id,
                    effective_from=now,
                    display_name=existing.display_name,
                    muted=muted is not None,
                    warn_threshold_pct=warn_value,
                    re_trigger_delta=delta_value,
                    notes=(notes or "").strip() or None,
                )
        except (WatchpointInvalid, WatchpointNotFound) as exc:
            logger.info(
                "watch-desk overlay: rejected for subject %r: %s",
                subject_key,
                exc.message,
            )
            return _overlay_error(request, exc.message)

        response = await _render_return(
            request,
            db_session,
            return_to=return_to,
            now=now,
            csrf_token=session.csrf_token,
        )

    logger.info(
        "watch-desk overlay: tenant=%s user=%s subject=%s muted=%s warn=%s delta=%s (%s)",
        session.tenant_id,
        session.user_id,
        subject_key,
        muted is not None,
        warn_value,
        delta_value,
        "created" if existing is None else "revised",
    )
    return response


async def _refresh_monitor(db_session: AsyncSession, *, now: datetime) -> dict[str, Any]:
    """Re-derive the monitor context after an overlay write.

    Repeats the Briefing route's monitor fan-out on the caller's session:
    the open findings (for the fired-this-beat anchors), the schedule, the
    effective limit-set labels, and one fresh resolution. Deliberately not
    a partial update of the previous render — a changed WARN override moves
    a status, a mark and possibly a group badge at once.
    """
    findings = await IreneFindingRepository(db_session).list_open()
    schedule = await IreneScheduleRepository(db_session).get_for_tenant()

    limits_repo = LimitsRepository(db_session)
    effective_labels: dict[str, str] = {}
    for family in _LIMIT_FAMILIES:
        effective = await limits_repo.get_effective_set(family, now.date())
        if effective is not None:
            effective_labels[family] = effective.label

    return await _build_monitor(
        db_session,
        findings=findings,
        schedule=schedule,
        effective_labels=effective_labels,
        resolution=await resolve_watch_desk(db_session, as_of=now),
        now=now,
    )


# ---------------------------------------------------------------------------
# Watchpoint add / edit / retire / history (ADR-0116 §6, §7)
#
# The *defined* half of the asymmetry: here a watchpoint creates its own
# subject, so these endpoints offer what the overlay ones must never — an
# instrument, a currency pair, a threshold and a window. They are strictly
# the signal families' own; the derived families reach the same drawer
# through `.../watchpoints/overlay`, which refuses every field below.
# ---------------------------------------------------------------------------


# Where a write's fresh render is swapped. Two surfaces open the same
# editor — the Briefing monitor and the Calibration watchpoint list — and
# each must get its own container back, because a write moves a status, a
# muted count and a list row at once and only a whole re-render keeps them
# consistent (the ADR-0116 §6 reason the overlay save already re-renders).
_RETURN_TARGETS: dict[str, str] = {
    "monitor": "#dc-monitor",
    "list": "#dc-watchpoint-list",
}
_DEFAULT_RETURN_TO: str = "monitor"


@dataclass(frozen=True)
class _ParameterField:
    """One family-defining parameter, as the editor renders and parses it.

    Stated once per family so the form and the parser cannot disagree about
    which fields a family has — the failure that would otherwise show up as
    a ``WatchpointInvalid`` naming a column the operator was never offered.

    Attributes:
        name: The form field and the repository keyword argument.
        label: The field's label.
        unit: The unit shown beside the input.
        hint: One line on what the number means.
        integral: Whether the value is a whole number (days / months).
    """

    name: str
    label: str
    unit: str
    hint: str
    integral: bool


# The four families' defining parameters. ``instrument_id`` and
# ``currency_pair`` are deliberately absent: they are the subject's
# *identity*, settable only at creation and inherited by every later
# version (``WatchpointRepository.revise``), so they are handled separately
# and rendered read-only in the editor.
_SIGNAL_PARAMETERS: dict[str, tuple[_ParameterField, ...]] = {
    FAMILY_PRICE: (
        _ParameterField(
            name="drop_pct",
            label="Decline threshold",
            unit="%",
            hint="A fall of at least this much across the window is Triggered.",
            integral=False,
        ),
        _ParameterField(
            name="window_days",
            label="Window",
            unit="days",
            hint="How far back the move is measured from.",
            integral=True,
        ),
    ),
    FAMILY_FX: (
        _ParameterField(
            name="move_pct",
            label="Move threshold",
            unit="%",
            hint="A move of at least this much either way is Triggered.",
            integral=False,
        ),
        _ParameterField(
            name="window_days",
            label="Window",
            unit="days",
            hint="How far back the move is measured from.",
            integral=True,
        ),
    ),
    FAMILY_FRESHNESS: (
        _ParameterField(
            name="max_age_days",
            label="Maximum NAV age",
            unit="days",
            hint="Applies to every active investment — one rule, not one per position.",
            integral=True,
        ),
    ),
    FAMILY_LIQUIDITY: (
        _ParameterField(
            name="horizon_months",
            label="Horizon",
            unit="months",
            hint="How far ahead projected capital calls are counted.",
            integral=True,
        ),
        _ParameterField(
            name="min_coverage_ratio",
            label="Coverage floor",
            unit="×",
            hint="Cover below this multiple of the projected calls is Triggered.",
            integral=False,
        ),
    ),
}

# How the subject key is formed per family, reusing the seeder's own helpers
# so a hand-added watchpoint and a seeded one for the same subject are the
# same string — which is what makes the seeder's idempotency hold against
# rows this surface wrote.
_SUBJECT_KEY_BUILDER: dict[str, Any] = {
    FAMILY_PRICE: price_subject_key,
    FAMILY_FX: fx_subject_key,
}


def _return_target(return_to: str) -> str:
    """Return the HTMX swap target for a ``return_to`` marker."""
    return _RETURN_TARGETS.get(return_to, _RETURN_TARGETS[_DEFAULT_RETURN_TO])


async def _render_return(
    request: Request,
    db_session: AsyncSession,
    *,
    return_to: str,
    now: datetime,
    csrf_token: str,
) -> HTMLResponse:
    """Re-render whichever surface the write was made from.

    Always a whole container, never a patched row: a saved threshold moves
    a status, a gauge, a group badge and a list row together, and
    re-deriving them from one fresh read is the only way they cannot
    disagree.
    """
    if return_to == "list":
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/watch_desk_watchpoint_list.html",
                await _watchpoint_list_context(db_session, now=now, csrf_token=csrf_token),
            ),
        )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_monitor.html",
            {
                "monitor": await _refresh_monitor(db_session, now=now),
                "csrf_token": csrf_token,
            },
        ),
    )


def _watchpoint_error(request: Request, message: str) -> HTMLResponse:
    """Return the inline error the watchpoint forms render in place.

    The same partial the overlay drawer and the resolution actions use —
    one inline-error idiom for the whole section, so a refusal never looks
    like a different kind of event depending on which form produced it.
    """
    return _overlay_error(request, message)


def _parameter_fields(family: str) -> tuple[_ParameterField, ...]:
    """Return one family's defining parameter fields, or ``()``."""
    return _SIGNAL_PARAMETERS.get(family, ())


def _read_parameters(family: str, form: Mapping[str, Any]) -> dict[str, Any]:
    """Parse one family's defining parameters out of a posted form.

    Raises:
        WatchpointInvalid: On a blank or unparseable value, naming the
            field so the inline error lands where the operator can act on
            it. Bounds are **not** checked here — positivity and the
            currency-pair format belong to the repository write path
            (ADR-0116 §3), and a second copy would be a second contract.
    """
    parameters: dict[str, Any] = {}
    for field in _parameter_fields(family):
        raw = str(form.get(field.name, "") or "").strip()
        if not raw:
            raise WatchpointInvalid(
                f"{field.label} is required for a {family} watchpoint.",
                field=field.name,
            )
        try:
            parameters[field.name] = int(raw) if field.integral else Decimal(raw)
        except (InvalidOperation, ValueError) as exc:
            raise WatchpointInvalid(
                f"{field.label} must be a "
                f"{'whole number' if field.integral else 'number'}; got {raw!r}.",
                field=field.name,
            ) from exc
    return parameters


def _parameter_summary(row: WatchpointDTO) -> str:
    """State one watchpoint's defining parameters in its family's language.

    The Calibration list's third column (ADR-0116 §7). Every figure is the
    one the operator typed, in the unit they typed it in — the ``liquidity``
    row reads ``1.20× over 12 months``, never the internal 100-scale its
    magnitude is computed on.
    """
    if row.family == FAMILY_PRICE and row.drop_pct is not None:
        return f"{float(row.drop_pct):.2f}% decline over {_plural(row.window_days or 0, 'day')}"
    if row.family == FAMILY_FX and row.move_pct is not None:
        return f"{float(row.move_pct):.2f}% move over {_plural(row.window_days or 0, 'day')}"
    if row.family == FAMILY_FRESHNESS and row.max_age_days is not None:
        return f"NAV no older than {_plural(row.max_age_days, 'day')}"
    if row.family == FAMILY_LIQUIDITY and row.min_coverage_ratio is not None:
        return (
            f"{float(row.min_coverage_ratio):.2f}× cover over "
            f"{_plural(row.horizon_months or 0, 'month')}"
        )

    # The overlay families define nothing, so what there is to state is the
    # sensitivity they carry — and "follows the tenant default" is a real
    # answer, not an empty cell.
    parts: list[str] = []
    if row.warn_threshold_pct is not None:
        parts.append(f"WARN at {float(row.warn_threshold_pct):.0f}%")
    if row.re_trigger_delta is not None:
        parts.append(f"re-trigger Δ {float(row.re_trigger_delta):.1f}")
    return " · ".join(parts) if parts else "sensitivity only — follows the tenant defaults"


def _watchpoint_row(row: WatchpointDTO, *, retired: bool) -> dict[str, Any]:
    """Project one watchpoint identity into a Calibration list row."""
    family_label = _DELTA_FAMILY_LABELS.get(row.family, row.family)
    return {
        "watchpoint_id": str(row.watchpoint_id),
        "family": row.family,
        "family_label": family_label,
        "display_name": row.display_name,
        "subject_key": row.subject_key,
        "parameters": _parameter_summary(row),
        "muted": row.muted,
        "notes": row.notes or "",
        "effective_from": row.effective_from,
        "retired": retired,
        # An overlay subject has no identity of its own to edit — its
        # drawer is keyed by subject, not by watchpoint (ADR-0116 §3).
        "edit_url": (
            f"/api/watch-desk/watchpoints/overlay?subject_key={quote(row.subject_key)}&return_to=list"
            if row.family in OVERLAY_FAMILIES
            else f"/api/watch-desk/watchpoints/{row.watchpoint_id}/edit?return_to=list"
        ),
        # Keyed on the identity, not the subject: a retired singleton and
        # its replacement share a subject key, and two rows with one DOM id
        # would make the drawer open in the wrong place.
        "slot_id": _dom_id("dc-wplist", str(row.watchpoint_id)),
    }


async def _watchpoint_list_context(
    db_session: AsyncSession,
    *,
    now: datetime,
    csrf_token: str,
    show_retired: bool = False,
) -> dict[str, Any]:
    """Build the Calibration section's watchpoint list (ADR-0116 §7).

    One row per **live identity**, of every family — the derived ones
    included, because "what does this tenant watch, and how nervously" is
    one question and answering half of it in two places is how the two
    halves drift. Retired identities are a separate, collapsed list: their
    history stays readable (that is what keeps a past finding explainable)
    without keeping a dead subject in the working view.

    Args:
        db_session: The tenant-scoped session opened by the caller.
        now: The instant the retired half is resolved at.
        csrf_token: The session token the row actions post with.
        show_retired: Whether the retired list is expanded on render.

    Returns:
        The list's template context.
    """
    repo = WatchpointRepository(db_session)
    live = await repo.list_live_identities()
    live_ids = {row.watchpoint_id for row in live}

    # Everything effective now that is *not* a live identity — i.e. the
    # retired ones. Read this way rather than as a second "retired" query
    # because "retired" is a property of the latest version, not a column
    # to filter a table on.
    retired_rows: list[WatchpointDTO] = []
    seen: set[UUID] = set()
    for row in await repo.effective_watchpoints(now, include_retired=True):
        if row.watchpoint_id in live_ids or row.watchpoint_id in seen:
            continue
        seen.add(row.watchpoint_id)
        retired_rows.append(row)

    return {
        "csrf_token": csrf_token,
        "watchpoints": [_watchpoint_row(row, retired=False) for row in live],
        "retired_watchpoints": [_watchpoint_row(row, retired=True) for row in retired_rows],
        "show_retired": show_retired,
    }


def _watchpoint_form_context(
    *,
    family: str,
    csrf_token: str,
    return_to: str,
    current: WatchpointDTO | None = None,
    instruments: list[dict[str, str]] | None = None,
    resolution: WatchDeskResolution | None = None,
) -> dict[str, Any]:
    """Build the add / edit form context for one signal watchpoint.

    One template serves both modes. In **edit** mode the identity fields —
    the instrument or the pair — render as read-only facts rather than
    inputs, because changing what a watchpoint watches makes it a different
    watchpoint: a create plus a retire, not an edit
    (:meth:`WatchpointRepository.revise` inherits them and would ignore a
    posted value, so offering one would be a lie).

    A revision states the **complete** calibration, so every field is
    pre-filled from the current version — an immutable version row is meant
    to be readable on its own, without replaying its predecessors.
    """
    editing = current is not None
    values: dict[str, str] = {}
    if current is not None:
        for field in _parameter_fields(family):
            value = getattr(current, field.name, None)
            values[field.name] = "" if value is None else str(value)

    warn_default = float(resolution.warn_default_pct) if resolution is not None else 0.0
    delta_default = (
        float(resolution.config.re_trigger_delta.get(family, Decimal("0")))
        if resolution is not None
        else 0.0
    )
    return {
        "csrf_token": csrf_token,
        "mode": "edit" if editing else "create",
        "family": family,
        "family_label": _DELTA_FAMILY_LABELS.get(family, family),
        "singleton": family in SINGLETON_FAMILIES,
        "fields": [
            {
                "name": field.name,
                "label": field.label,
                "unit": field.unit,
                "hint": field.hint,
                "step": "1" if field.integral else "any",
                "value": values.get(field.name, ""),
            }
            for field in _parameter_fields(family)
        ],
        "instruments": instruments or [],
        "currency_pair": current.currency_pair if current is not None else "",
        "display_name": current.display_name if current is not None else "",
        "muted": current.muted if current is not None else False,
        "warn_threshold_pct": (
            str(current.warn_threshold_pct)
            if current is not None and current.warn_threshold_pct is not None
            else ""
        ),
        "re_trigger_delta": (
            str(current.re_trigger_delta)
            if current is not None and current.re_trigger_delta is not None
            else ""
        ),
        "notes": (current.notes if current is not None else None) or "",
        "warn_default_pct": warn_default,
        "delta_default": delta_default,
        "return_to": return_to,
        "target": _return_target(return_to),
        "action": (
            f"/api/watch-desk/watchpoints/{current.watchpoint_id}/revise"
            if current is not None
            else "/api/watch-desk/watchpoints"
        ),
        "slot_id": (
            _dom_id("dc-watchpoint", current.subject_key)
            if current is not None
            else "dc-watchpoint-new"
        ),
    }


async def _priceable_instruments(
    db_session: AsyncSession, *, watched: set[UUID]
) -> list[dict[str, str]]:
    """Return the active investments a ``price`` watchpoint can be set on.

    The seeding precedent (ADR-0116 §8), applied to the picker: an
    investment with at least one market identifier is one the platform can
    price, and a private-markets fund carries none — offering it would be
    offering a subject that can only ever report "no price history". Those
    already carrying a live ``price`` watchpoint are excluded too: a second
    identity on one instrument would be two answers to one question, and
    the resolution would have to pick one and warn.

    Runs one identifier read per active investment, exactly as the seeder
    does. It is paid only when the operator opens the form, never on a
    monitor render.
    """
    investments = await InvestmentRepository(db_session).list_active()
    identifiers = InvestmentIdentifierRepository(db_session)
    priceable: list[dict[str, str]] = []
    for investment in investments:
        if investment.id in watched:
            continue
        if not await identifiers.list_for_investment(investment.id):
            continue
        priceable.append({"id": str(investment.id), "name": investment.name})
    return priceable


@router.get("/api/watch-desk/watchpoints/new", response_class=HTMLResponse)
async def get_watchpoint_form(
    request: Request,
    family: str,
    return_to: str = _DEFAULT_RETURN_TO,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the add form for one defined signal family (ADR-0116 §6).

    Refuses the derived families outright: for ``saa`` / ``anlv`` / ``rss``
    the subject is enumerated elsewhere and a watchpoint is a sensitivity
    overlay only, so there is nothing here for an "add" to create.
    """
    if family not in _SIGNAL_PARAMETERS:
        return _watchpoint_error(
            request,
            f"{family!r} subjects are not defined by a watchpoint; the defined "
            f"families are {', '.join(SIGNAL_FAMILY_ORDER)}.",
        )

    engine = _engine(request)
    now = _now()
    instruments: list[dict[str, str]] = []
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        resolution = await resolve_watch_desk(db_session, as_of=now)
        if family == FAMILY_PRICE:
            watched = {
                watchpoint.instrument_id
                for watchpoint in resolution.signals_for(FAMILY_PRICE)
                if watchpoint.instrument_id is not None
            }
            instruments = await _priceable_instruments(db_session, watched=watched)

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_watchpoint_form.html",
            _watchpoint_form_context(
                family=family,
                csrf_token=session.csrf_token,
                return_to=return_to,
                instruments=instruments,
                resolution=resolution,
            ),
        ),
    )


@router.post("/api/watch-desk/watchpoints", response_class=HTMLResponse)
async def create_watchpoint(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Create one signal watchpoint and return the surface it was added from.

    The new row appears on the monitor **immediately** — the groups are
    derived live from the fresh resolution — and is evaluated on the next
    beat, which is the same sentence the sensitivity drawer already makes
    and is repeated verbatim in the form's footer.

    Every rule the write must satisfy belongs to the repository: the
    currency-pair format, positivity, the per-family shape, and the
    singleton rule. This handler parses, forms the subject key, and renders
    whatever refusal comes back inline — it never re-implements a check,
    because two copies of a rule is how one of them goes stale.
    """
    form = await request.form()
    family = str(form.get("family", ""))
    if family not in _SIGNAL_PARAMETERS:
        return _watchpoint_error(
            request,
            f"{family!r} subjects are not defined by a watchpoint; the defined "
            f"families are {', '.join(SIGNAL_FAMILY_ORDER)}.",
        )
    return_to = str(form.get("return_to", _DEFAULT_RETURN_TO))

    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        try:
            parameters = _read_parameters(family, form)
            subject_key, identity, subject_label = await _resolve_new_subject(
                db_session, family, form
            )
        except WatchpointInvalid as exc:
            return _watchpoint_error(request, exc.message)

        display_name = str(form.get("display_name", "") or "").strip() or default_display_name(
            family, subject=subject_label
        )
        try:
            created = await WatchpointRepository(db_session).create(
                family=family,
                subject_key=subject_key,
                display_name=display_name,
                effective_from=now,
                notes=str(form.get("notes", "") or "").strip() or None,
                **identity,
                **parameters,
            )
        except WatchpointInvalid as exc:
            logger.info("watch-desk watchpoint: create rejected (%s): %s", family, exc.message)
            return _watchpoint_error(request, exc.message)

        response = await _render_return(
            request,
            db_session,
            return_to=return_to,
            now=now,
            csrf_token=session.csrf_token,
        )

    logger.info(
        "watch-desk watchpoint: tenant=%s user=%s created %s %s (%s)",
        session.tenant_id,
        session.user_id,
        family,
        created.watchpoint_id,
        subject_key,
    )
    return response


async def _resolve_new_subject(
    db_session: AsyncSession, family: str, form: Mapping[str, Any]
) -> tuple[str, dict[str, Any], str]:
    """Return ``(subject_key, identity kwargs, label)`` for a new watchpoint.

    The identity half of the create — the instrument or the pair — which
    only ``price`` and ``fx`` have. The two singleton families name their
    one subject with the constant the producers and the seeder already
    share, so the key a hand-added singleton writes is the key the
    sensitivity lookups fall back to.

    Raises:
        WatchpointInvalid: If the identity field is missing or malformed.
            Only *presence* and parseability are checked here; the pair's
            format is the repository's rule and is left to it.
    """
    if family == FAMILY_PRICE:
        raw = str(form.get("instrument_id", "") or "").strip()
        if not raw:
            raise WatchpointInvalid("Choose an instrument to watch.", field="instrument_id")
        try:
            instrument_id = UUID(raw)
        except ValueError as exc:
            raise WatchpointInvalid(
                f"{raw!r} is not an investment id.", field="instrument_id"
            ) from exc
        investment = await InvestmentRepository(db_session).get_by_id(instrument_id)
        if investment is None:
            raise WatchpointInvalid(
                "That investment is not visible in this tenant.", field="instrument_id"
            )
        return (
            _SUBJECT_KEY_BUILDER[FAMILY_PRICE](instrument_id),
            {"instrument_id": instrument_id},
            investment.name,
        )

    if family == FAMILY_FX:
        pair = str(form.get("currency_pair", "") or "").strip().upper()
        if not pair:
            raise WatchpointInvalid(
                "Name the currency pair to watch, as BASE/QUOTE.", field="currency_pair"
            )
        return _SUBJECT_KEY_BUILDER[FAMILY_FX](pair), {"currency_pair": pair}, pair

    if family == FAMILY_FRESHNESS:
        return FRESHNESS_WILDCARD_SUBJECT_KEY, {}, ""
    return LIQUIDITY_SUBJECT_KEY, {}, ""


@router.get("/api/watch-desk/watchpoints/{watchpoint_id}/edit", response_class=HTMLResponse)
async def get_watchpoint_editor(
    request: Request,
    watchpoint_id: UUID,
    return_to: str = _DEFAULT_RETURN_TO,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the editor for one signal watchpoint identity (ADR-0116 §6).

    Reached from a monitor row and from the Calibration list, which is why
    it carries ``return_to``. For a ``freshness`` row it is the **singleton**
    that opens: every enumerated subject shares one identity, so editing one
    row edits the rule for the whole book — the form says so rather than
    letting the operator infer it from a surprise.
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        current = await WatchpointRepository(db_session).get_current(watchpoint_id)
        resolution = await resolve_watch_desk(db_session, as_of=now)

    if current is None:
        return _watchpoint_error(request, "That watchpoint no longer exists in this tenant.")
    if current.family in OVERLAY_FAMILIES:
        return _watchpoint_error(
            request,
            f"{current.family!r} subjects carry a sensitivity overlay only — their "
            "subject and ceiling belong to the limit set.",
        )

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_watchpoint_form.html",
            _watchpoint_form_context(
                family=current.family,
                csrf_token=session.csrf_token,
                return_to=return_to,
                current=current,
                resolution=resolution,
            ),
        ),
    )


@router.post("/api/watch-desk/watchpoints/{watchpoint_id}/revise", response_class=HTMLResponse)
async def revise_watchpoint(
    request: Request,
    watchpoint_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Write a new version of one signal watchpoint.

    Nothing is updated in place: the identity accumulates immutable
    versions, and the history rendered beside it *is* the change record
    (ADR-0116 §1). The posted form states the complete calibration, which
    is what :meth:`WatchpointRepository.revise` requires and why the editor
    pre-fills every field.
    """
    form = await request.form()
    return_to = str(form.get("return_to", _DEFAULT_RETURN_TO))

    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        repo = WatchpointRepository(db_session)
        current = await repo.get_current(watchpoint_id)
        if current is None:
            return _watchpoint_error(request, "That watchpoint no longer exists in this tenant.")
        if current.family in OVERLAY_FAMILIES:
            return _watchpoint_error(
                request,
                f"{current.family!r} subjects carry a sensitivity overlay only — use "
                "the sensitivity drawer.",
            )

        try:
            parameters = _read_parameters(current.family, form)
            await repo.revise(
                watchpoint_id,
                effective_from=now,
                display_name=(
                    str(form.get("display_name", "") or "").strip() or current.display_name
                ),
                muted=form.get("muted") is not None,
                warn_threshold_pct=_optional_decimal(
                    str(form.get("warn_threshold_pct", "") or ""), field="warn_threshold_pct"
                ),
                re_trigger_delta=_optional_decimal(
                    str(form.get("re_trigger_delta", "") or ""), field="re_trigger_delta"
                ),
                notes=str(form.get("notes", "") or "").strip() or None,
                **parameters,
            )
        except (WatchpointInvalid, WatchpointNotFound) as exc:
            logger.info(
                "watch-desk watchpoint: revision rejected (%s): %s",
                watchpoint_id,
                exc.message,
            )
            return _watchpoint_error(request, exc.message)

        response = await _render_return(
            request,
            db_session,
            return_to=return_to,
            now=now,
            csrf_token=session.csrf_token,
        )

    logger.info(
        "watch-desk watchpoint: tenant=%s user=%s revised %s (%s)",
        session.tenant_id,
        session.user_id,
        watchpoint_id,
        current.family,
    )
    return response


@router.post("/api/watch-desk/watchpoints/{watchpoint_id}/retire", response_class=HTMLResponse)
async def retire_watchpoint(
    request: Request,
    watchpoint_id: UUID,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Retire one watchpoint identity and return the refreshed list.

    Retirement is a **version**, not a deletion: the identity and its
    history stay queryable, so a finding it once raised stays explainable
    (ADR-0116 §1). The beat side of that semantic already holds — a retired
    identity is absent from the resolution, so it stops being evaluated on
    the very next beat, and findings it already raised are neither closed
    nor deleted by the retirement. Here the row simply moves to the retired
    list.
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        try:
            await WatchpointRepository(db_session).retire(watchpoint_id, effective_from=now)
        except (WatchpointInvalid, WatchpointNotFound) as exc:
            logger.info(
                "watch-desk watchpoint: retirement rejected (%s): %s",
                watchpoint_id,
                exc.message,
            )
            return _watchpoint_error(request, exc.message)

        context = await _watchpoint_list_context(
            db_session, now=now, csrf_token=session.csrf_token, show_retired=True
        )

    logger.info(
        "watch-desk watchpoint: tenant=%s user=%s retired %s",
        session.tenant_id,
        session.user_id,
        watchpoint_id,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_watchpoint_list.html",
            context,
        ),
    )


@router.get("/api/watch-desk/watchpoints/{watchpoint_id}/history", response_class=HTMLResponse)
async def get_watchpoint_history(
    request: Request,
    watchpoint_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return one identity's version rows, newest first (ADR-0116 §7).

    Read-only, and deliberately without a diff engine: the historised rows
    **are** the story. Each version states the complete calibration in
    force from its ``effective_from``, so reading one row answers "what was
    this set to then" without replaying its predecessors — which is the
    whole reason the table is versioned rather than updated.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        versions = await WatchpointRepository(db_session).list_versions(watchpoint_id)

    if not versions:
        return _watchpoint_error(request, "That watchpoint has no history in this tenant.")

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_watchpoint_history.html",
            {
                "display_name": versions[-1].display_name,
                "versions": [
                    {
                        "effective_from": row.effective_from,
                        "retired": row.retired,
                        "display_name": row.display_name,
                        "parameters": _parameter_summary(row),
                        "muted": row.muted,
                        "warn_threshold_pct": (
                            f"{float(row.warn_threshold_pct):.0f}%"
                            if row.warn_threshold_pct is not None
                            else "default"
                        ),
                        "re_trigger_delta": (
                            f"{float(row.re_trigger_delta):.1f}"
                            if row.re_trigger_delta is not None
                            else "default"
                        ),
                        "notes": row.notes or "",
                    }
                    for row in reversed(versions)
                ],
            },
        ),
    )


# ---------------------------------------------------------------------------
# Cadence settings — edit the tenant irene_schedule
# ---------------------------------------------------------------------------


@router.post("/api/watch-desk/cadence", response_class=HTMLResponse)
async def save_cadence(
    request: Request,
    cadence: str = Form(...),
    preferred_hour: int = Form(...),
    timezone_name: str = Form(..., alias="timezone"),
    enabled: str | None = Form(None),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Persist the tenant cadence settings and return the refreshed panel.

    Recomputes ``next_due_at`` via
    :func:`services.irene.scheduling.compute_next_due_at` (the single source
    of cadence arithmetic — never duplicated here) before upserting through
    :meth:`IreneScheduleRepository.upsert_tenant_schedule`. This is the
    domain calibration interface for the heartbeat (ADR-0086), not a
    deployment artifact. Per-user cadence stays deferred (the ``user_id``
    seam is unused). An unsupported cadence or an unknown timezone returns a
    422 with the panel re-rendered carrying the error, never a 500.
    """
    enabled_bool = enabled is not None
    cleaned_tz = timezone_name.strip()
    now = _now()

    # Validate + compute next_due_at before any DB write. compute_next_due_at
    # raises IreneCadenceInvalid for a bad cadence; ZoneInfo raises for an
    # unknown timezone. Both become a 422 with the panel re-rendered.
    try:
        ZoneInfo(cleaned_tz)  # fail fast on a bad IANA name
        next_due_at = compute_next_due_at(now, cadence, preferred_hour, cleaned_tz)
    except (IreneCadenceInvalid, ZoneInfoNotFoundError, ValueError) as exc:
        logger.info(
            "watch-desk cadence: rejected (cadence=%r tz=%r): %s",
            cadence,
            cleaned_tz,
            exc,
        )
        user_msg, _error_id = user_safe_error(exc)
        context = _cadence_context(None, session.csrf_token, error=user_msg)
        # Reflect the attempted values back so the operator can correct them.
        context["current"].update(
            {
                "cadence": cadence,
                "preferred_hour": preferred_hour,
                "timezone": cleaned_tz,
                "enabled": enabled_bool,
            }
        )
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/watch_desk_cadence_panel.html",
                context,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        schedule = await IreneScheduleRepository(db_session).upsert_tenant_schedule(
            cadence=cadence,
            preferred_hour=preferred_hour,
            timezone=cleaned_tz,
            enabled=enabled_bool,
            next_due_at=next_due_at,
        )

    logger.info(
        "watch-desk cadence: tenant=%s user=%s cadence=%s hour=%s tz=%s enabled=%s next_due_at=%s",
        session.tenant_id,
        session.user_id,
        cadence,
        preferred_hour,
        cleaned_tz,
        enabled_bool,
        next_due_at.isoformat(),
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_cadence_panel.html",
            _cadence_context(schedule, session.csrf_token, saved=True),
        ),
    )
