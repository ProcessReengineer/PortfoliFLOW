# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The tenant-scoped Irene beat handler (ADR-0086).

One *beat* is one tenant's scheduled synthesis: read the world Irene
monitors, ask the model (via a single non-streaming ``run_synthesis``
call) whether anything is material, and persist any surfaced findings.
The beat runs **inside** a tenant-scoped session — the same session the
tick opens via ``tenant_context`` — so every ``irene_finding`` write is
RLS-policed for the active tenant.

The beat reads the **real** internal world state.
:func:`services.irene.internal_delta.evaluate_internal_deltas` diffs the
latest limit-coverage snapshot against
``irene_watch_state.acknowledged_*`` and returns the *eligible findings*
— the material changes worth showing Irene — and since ADR-0116 §4
:func:`services.irene.signal_delta.evaluate_signal_deltas` does the same
for the ``price`` and ``fx`` watchpoints the tenant defined. The beat
renders those as grounded, numeric context for synthesis; a calm book
yields an empty list and the "nothing material" context, so silence still
falls out natively.

Three separable stages sit behind a surfaced finding, and the beat is
where the last two meet (ADR-0087/0088): the delta layer decides *what is
worth showing Irene*; Irene decides *how to phrase and whether to surface*
(via ``surface_finding``, proposing an ``urgency_suggestion``); and the
**deterministic floor** (:mod:`services.analytics.irene_floor`) decides the
*final urgency and band*. The model suggests, deterministic rules decide:
``final = clamp(suggestion, floor[trigger_type], cap[source])`` and the
band follows from the final urgency by fixed boundaries. The suggestion is
preserved in the persisted payload so the suggestion↔final discrepancy is
auditable, while ``irene_finding.urgency``/``band`` store the **final**
values.

The floor is applied **here**, not in ``run_synthesis`` (an ADR-0088
erratum): ``run_synthesis`` is delta-agnostic and cannot know a subject's
trigger type or source, whereas the beat has both the model's suggestion
and the eligible findings the trigger/source derive from. See the
:mod:`services.analytics.irene_floor` docstring.

A single tenant's beat never raises out in a way that aborts the whole
tick: an error during synthesis is caught and reported on
:class:`BeatResult`, so the tick can log it and continue to the next
tenant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import FloorCalibrationInvalid
from core.repositories.irene_finding_repository import IreneFindingRepository
from services.analytics.irene_floor import (
    DEFAULT_FLOOR_CONFIG,
    SOURCE_INTERNAL,
    SOURCE_RSS,
    FloorConfig,
    band_from_final_urgency,
    clamp_suggestion,
    derive_trigger_type,
    final_urgency,
    options_allowed,
)
from services.analytics.rss_bucketing import item_identity
from services.irene.correlation import (
    CorrelationResult,
    MergedInternalFinding,
    correlate,
)
from services.irene.internal_delta import (
    EligibleFinding,
    evaluate_internal_deltas,
)
from services.irene.rss_delta import (
    RssEligibleFinding,
    evaluate_rss_deltas,
)
from services.irene.signal_delta import (
    SignalEligibleFinding,
    evaluate_signal_deltas,
)
from services.irene.synthesis_tool import (
    SURFACE_FINDING_TOOL,
    SURFACE_FINDING_TOOL_NAME,
)
from services.watch_desk.overlay import resolve_watch_desk

if TYPE_CHECKING:
    from services.ai_service_core import ResolvedLLM
    from services.irene.embedding import Embedder
    from services.web_research.models import FeedItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BeatResult:
    """The outcome of one tenant's beat.

    Attributes:
        tenant_id: The tenant this beat ran for.
        findings_written: How many ``irene_finding`` rows were appended
            (``0`` on the silence path).
        silence: ``True`` when synthesis surfaced nothing — the
            "nothing material" outcome.
        error: A short error string when the beat failed (synthesis
            raised, or a finding could not be persisted), else ``None``.
            A non-``None`` error means the tick should not advance this
            tenant's schedule and should retry on the next tick.
    """

    tenant_id: UUID
    findings_written: int
    silence: bool
    error: str | None = None


def _render_eligible_finding(finding: EligibleFinding) -> str:
    """Render one eligible finding as grounded, numeric context.

    Every figure is stated explicitly so the model interprets rather than
    invents (ADR-0013 / ADR-0087 grounding rule). The urgency hint is
    labelled non-binding — the deterministic floor decides the final
    urgency, not the model and not this hint. ``edge_band`` is the delta
    layer's coverage-severity band (note/watch/act), not the card's final
    band (which the floor derives from the final urgency).
    """
    coverage = "n/a" if finding.coverage_pct is None else f"{finding.coverage_pct}%"
    ceiling = "n/a" if finding.max_pct is None else f"{finding.max_pct}%"
    headroom = "n/a" if finding.headroom_eur is None else f"{finding.headroom_eur} EUR"
    return (
        f"- subject_key: {finding.subject_key}\n"
        f"  change: {finding.kind} (status={finding.status}, "
        f"edge_band={finding.band})\n"
        f"  coverage: {coverage} against a {ceiling} ceiling; "
        f"headroom {headroom}\n"
        f"  basis: {finding.reason}\n"
        f"  urgency hint (non-binding): "
        f"{finding.provisional_urgency_hint}"
    )


def _render_signal_eligible(finding: SignalEligibleFinding) -> str:
    """Render one signal-family eligible as grounded, numeric context.

    Same grounding rule as the internal block — every figure stated, none
    to invent — with one difference that is not cosmetic: the status is
    rendered as its **human** label (Calm / Approaching / Triggered), never
    as the internal ``OK``/``WARN``/``BREACH`` vocabulary. "Breach" is
    regulatory language reserved for the quota families (ADR-0116 §4), and
    the surest way to keep it out of a card is to keep it out of what Irene
    is shown.
    """
    return (
        f"- subject_key: {finding.subject_key}\n"
        f"  subject: {finding.display_name} ({finding.family} watchpoint)\n"
        f"  change: {finding.kind} (status={finding.status_label}, "
        f"edge_band={finding.band})\n"
        f"  observation: {finding.note}\n"
        f"  basis: {finding.reason}\n"
        f"  urgency hint (non-binding): "
        f"{finding.provisional_urgency_hint}"
    )


def _render_rss_members(members: Sequence[Any], *, prefix: str) -> str:
    """Render a bucket's members as titles + sources — never any number."""
    return "\n".join(f"{prefix}- {m.title} ({m.source_name})" for m in members)


def _render_corroboration(merged: MergedInternalFinding) -> str:
    """Render the RSS item(s) corroborating one internal finding."""
    lines = [f"  corroborating external signal(s) ({len(merged.corroborating)}):"]
    for r in merged.corroborating:
        tag = r.tags[0] if r.tags else "untagged"
        lines.append(f"    [{tag}, {r.day_bucket.isoformat()}] {len(r.bucket_members)} item(s):")
        lines.append(_render_rss_members(r.bucket_members, prefix="      "))
    return "\n".join(lines)


def _render_rss_eligible(r: RssEligibleFinding) -> str:
    """Render one standalone RSS eligible as grounded, number-free context.

    Only titles / sources / tag / day-bucket — an RSS bucket carries no
    scalar magnitude, so there is no number to interpret and none to
    invent. A standalone RSS finding is deterministically capped at the
    ``informational`` band by the floor (source = RSS), so it never
    outranks an internal note.
    """
    tag = r.tags[0] if r.tags else "untagged"
    return (
        f"- subject_key: {r.subject_key}\n"
        f"  change: new RSS cluster (tag={tag}, "
        f"day_bucket={r.day_bucket.isoformat()})\n"
        f"  items:\n{_render_rss_members(r.bucket_members, prefix='    ')}\n"
        f"  basis: {r.reason}"
    )


def _build_delta_context(
    internal: list[EligibleFinding],
    rss: list[RssEligibleFinding],
    now: datetime,
    *,
    tag_asset_class_map: Any,
    signals: list[SignalEligibleFinding] | None = None,
) -> list[dict[str, Any]]:
    """Build the beat context from the three deltas' eligible findings.

    Applies the correlation lift (ADR-0087 §1.5) first: an RSS bucket
    whose tag corresponds to a coincident internal edge is folded into
    that internal card as corroborating basis, and its standalone eligible
    is suppressed — the PM sees one corroborated internal card, not two.
    Signal-family eligibles are not correlated in v1: the lift keys off the
    tag → asset-class correspondence, and a single instrument or currency
    pair is not an asset class.

    An empty internal, signal **and** RSS list is the calm-book path: Irene
    is told nothing material changed and that silence is correct, so a
    calm book yields zero ``surface_finding`` calls end-to-end. Otherwise
    the context renders one block per internal change (with explicit,
    interpret-don't-invent figures and any attached external corroboration),
    then one per watchpoint signal, then one block per standalone RSS
    cluster (titles / sources only — no invented numbers).

    Args:
        internal: The internal delta's eligible findings for this beat.
        rss: The RSS delta's eligible findings for this beat.
        now: The beat's clock, stamped into the message for traceability.
        tag_asset_class_map: The auditable tag → asset-class correspondence
            for the correlation lift.
        signals: The signal delta's eligible findings (ADR-0116 §4), or
            ``None`` for a beat with no watchpoints to evaluate.

    Returns:
        A one-message OpenAI-shaped context list.
    """
    signal_findings = signals or []
    if not internal and not rss and not signal_findings:
        content = (
            f"Scheduled Irene beat at {now.isoformat()}. The world-state "
            "delta (internal limits, watchpoint signals and RSS press "
            "coverage) reported no material change this beat — every "
            "monitored limit and watchpoint is calm or unchanged since it "
            "was last acknowledged, and no new press cluster formed. Only "
            "call surface_finding if something is genuinely material; "
            "nothing is, so do not call it at all — silence is the correct "
            "outcome."
        )
        return [{"role": "user", "content": content}]

    correlation: CorrelationResult = correlate(
        internal, rss, tag_asset_class_map=tag_asset_class_map
    )

    blocks: list[str] = []
    for merged in correlation.merged:
        block = _render_eligible_finding(merged.internal)
        if merged.corroborating:
            block = f"{block}\n{_render_corroboration(merged)}"
        blocks.append(block)
    for signal in signal_findings:
        blocks.append(_render_signal_eligible(signal))
    for r in correlation.standalone_rss:
        blocks.append(_render_rss_eligible(r))

    content = (
        f"Scheduled Irene beat at {now.isoformat()}. The world-state delta "
        "surfaced the following material change(s) since they were last "
        "acknowledged. Internal-limit figures come from the analytics layer "
        "— interpret them, do not invent or alter any number. Watchpoint "
        "signals are the thresholds this tenant set for itself: describe "
        "them as triggered, approaching or eased — never as a breach, which "
        "is a word reserved for a violated regulatory or strategic limit. "
        "RSS clusters carry press-coverage context only (titles and sources) "
        "— treat them as corroboration, never as a source of figures. For "
        "each change that genuinely warrants the portfolio manager's "
        "attention, call surface_finding once, reusing the given subject_key "
        "verbatim. If a change does not warrant attention, do not call it."
        f"\n\n{chr(10).join(blocks)}"
    )
    return [{"role": "user", "content": content}]


async def _active_tenant_id(session: AsyncSession) -> UUID:
    """Read the active tenant id from the session's RLS context."""
    result = await session.execute(text("SELECT current_setting('app.tenant_id')::uuid AS tid"))
    return result.scalar_one()


def _augment_rss_payload(payload: dict[str, Any], eligible: RssEligibleFinding) -> dict[str, Any]:
    """Fold an RSS bucket's membership into a surfaced finding's payload.

    The persisted membership (``member_ids`` / ``tag`` / ``day_bucket``) is
    what :func:`services.irene.rss_clustering._load_frozen_anchors` reads
    back to freeze the bucket: once an RSS cluster is surfaced as an open
    finding, its identity is immutable and a later embedding-model change
    can neither re-form nor re-key it (ADR-0087 §1.4). ``member_ids`` uses
    the same stable identity function the key was formed from.

    Args:
        payload: The model's ``surface_finding`` arguments.
        eligible: The RSS eligible whose ``subject_key`` the model reused.

    Returns:
        A new payload dict enriched with the bucket's membership.
    """
    tag = eligible.tags[0] if eligible.tags else "untagged"
    enriched = dict(payload)
    enriched["subject_key"] = eligible.subject_key
    enriched["tag"] = tag
    enriched["day_bucket"] = eligible.day_bucket.isoformat()
    enriched["member_ids"] = [
        item_identity(published_at=m.published_at, url=m.url) for m in eligible.bucket_members
    ]
    enriched["members"] = [
        {
            "url": m.url,
            "title": m.title,
            "source_name": m.source_name,
            "published_at": m.published_at.isoformat(),
        }
        for m in eligible.bucket_members
    ]
    return enriched


async def run_beat(
    session: AsyncSession,
    ai_core: Any,
    *,
    llm: ResolvedLLM,
    now: datetime,
    defaults: FloorConfig = DEFAULT_FLOOR_CONFIG,
    rss_items: Sequence[FeedItem] | None = None,
    embedder: Embedder | None = None,
) -> BeatResult:
    """Run one tenant's Irene beat inside a tenant-scoped session.

    The session must already be tenant-scoped (opened by the tick via
    ``tenant_context``); the beat's ``irene_watch_state`` and
    ``irene_finding`` writes derive their ``tenant_id`` from
    ``app.tenant_id`` on that session.

    Flow:

    0. Resolve this tenant's effective calibration **once**
       (:func:`services.watch_desk.overlay.resolve_watch_desk`): defaults ⊕
       the tenant's ``floor_calibration`` revision ⊕ the per-subject
       overlays (ADR-0116 §5). Every layer below receives the result as a
       plain argument; nothing downstream reads ``DEFAULT_FLOOR_CONFIG``,
       and the monitor route resolves through the same function, so the two
       cannot disagree. A stored revision that no longer composes raises
       ``FloorCalibrationInvalid`` here and fails this tenant's beat loudly
       — never a silent fallback to the defaults.
    1. Run the internal delta
       (:func:`services.irene.internal_delta.evaluate_internal_deltas`):
       diff the latest coverage snapshot against the acknowledged state,
       writing the resulting acknowledgements / resets. Then run the signal
       delta (:func:`services.irene.signal_delta.evaluate_signal_deltas`):
       evaluate this tenant's ``price`` and ``fx`` watchpoints over the
       same pipeline (ADR-0116 §4). A tenant with no watchpoints — and
       every tenant before this programme — takes the empty path.
    2. If an ``embedder`` and ``rss_items`` are supplied, run the RSS delta
       (:func:`services.irene.rss_delta.evaluate_rss_deltas`): cluster the
       feed items into keyed buckets (freeze-aware) and edge-gate each
       against ``irene_watch_state``. Both deltas decide *what is worth
       showing Irene*.
    3. Render the eligible findings as grounded context — internal and
       watchpoint figures to interpret (never invent), RSS clusters as
       titles/sources only — applying the correlation lift (a coincident
       internal edge absorbs a corresponding RSS bucket as corroboration).
       A calm book with no watchpoint moves and no RSS clusters yields the
       "nothing material" context.
    4. Call :meth:`AIServiceCore.run_synthesis` with the
       ``surface_finding`` tool and ``tool_choice="auto"`` — Irene's
       decision of *how to phrase and whether to surface*.
    5. Zero ``surface_finding`` calls ⇒ silence. For each surfaced call,
       resolve the trigger type and source from the eligible-finding
       lookup, apply the deterministic floor
       (:mod:`services.analytics.irene_floor`) to turn the model's
       ``urgency_suggestion`` into the **final** urgency, derive the
       canonical band from that final urgency, drop ``options`` when the
       band is below the options gate, and append an ``irene_finding`` with
       the final urgency/band (the suggestion is retained in the payload for
       the audit discrepancy). A surfaced RSS finding's payload is enriched
       with its bucket membership so the bucket can be frozen on a later
       beat. A surfaced ``subject_key`` that matches **no** eligible finding
       is a grounding violation (Irene may only surface what the delta layer
       made eligible): it is dropped with a logged warning, never persisted
       at a fabricated urgency.

    All three deltas write ``irene_watch_state`` (upserts +
    acknowledgements) **before** synthesis runs, so those writes commit even
    if synthesis later fails — the edge is "consumed" once shown to Irene,
    independent of whether Irene phrases a finding for it (ADR-0087
    edge-gate semantics).

    Any exception from a delta read or synthesis (or from persisting a
    finding) is caught and returned as :attr:`BeatResult.error`; the beat
    never raises out in a way that would abort the whole tick.

    Args:
        session: A tenant-scoped :class:`AsyncSession`.
        ai_core: The AI service core (duck-typed: needs
            ``get_system_prompt`` and an async ``run_synthesis``). The
            shared :func:`services.ai_service_core.get_ai_service_core`
            singleton in production; a stub in tests. Since ADR-0112 §4b
            it supplies the system prompt and the synthesis entry point
            only — never the credential or the model.
        llm: This tenant's resolved endpoint, credential and model
            (ADR-0112 §4b), resolved by the tick inside the tenant's own
            context and threaded straight into ``run_synthesis``. The one
            source of truth for what Irene synthesises with; there is no
            second ``model`` argument to drift from it.
        now: The beat's clock (timezone-aware UTC).
        defaults: The **composition input** — the code-default Floor Config
            the tenant's stored calibration is composed over (ADR-0116 §5).
            It is deliberately not the config the beat runs on: that is
            resolved per tenant in step 0 and threaded onward. Overridable
            in tests; production always passes ``DEFAULT_FLOOR_CONFIG``.
        rss_items: The harvested feed items to cluster this beat, or
            ``None`` to skip the RSS delta (internal-only beat).
        embedder: The injected vectorisation seam for RSS clustering, or
            ``None`` to skip the RSS delta.

    Returns:
        The :class:`BeatResult` for this tenant.
    """
    tenant_id = await _active_tenant_id(session)

    try:
        # One resolution per run, threaded onward as plain arguments. The
        # pure layers keep receiving one FloorConfig plus resolved
        # per-subject values, so the analytics purity contract is untouched
        # (ADR-0116 §5).
        resolution = await resolve_watch_desk(session, as_of=now, defaults=defaults)
        thresholds = resolution.config

        internal_eligible = await evaluate_internal_deltas(session, now=now, resolution=resolution)
        signal_eligible = await evaluate_signal_deltas(session, now=now, resolution=resolution)
        rss_eligible: list[RssEligibleFinding] = []
        if embedder is not None and rss_items:
            rss_eligible = await evaluate_rss_deltas(
                session,
                embedder,
                rss_items,
                now=now,
                thresholds=thresholds,
                resolution=resolution,
            )
        system_prompt = ai_core.get_system_prompt("irene")
        context_messages = _build_delta_context(
            internal_eligible,
            rss_eligible,
            now,
            tag_asset_class_map=thresholds.tag_asset_class_map,
            signals=signal_eligible,
        )
        result = await ai_core.run_synthesis(
            system_prompt=system_prompt,
            context_messages=context_messages,
            tool=SURFACE_FINDING_TOOL,
            llm=llm,
        )

        surfacing = [tc for tc in result.tool_calls if tc.get("name") == SURFACE_FINDING_TOOL_NAME]
        if not surfacing:
            logger.info("irene-beat: tenant %s — silence.", tenant_id)
            return BeatResult(
                tenant_id=tenant_id,
                findings_written=0,
                silence=True,
                error=None,
            )

        findings = IreneFindingRepository(session)
        # The eligible set: only these subjects may be surfaced. The three
        # namespaces are disjoint by key prefix (saa/anlv vs price/fx vs
        # rss:cluster), so a subject is at most one of internal / signal /
        # RSS.
        internal_by_key = {e.subject_key: e for e in internal_eligible}
        signal_by_key = {s.subject_key: s for s in signal_eligible}
        rss_by_key = {r.subject_key: r for r in rss_eligible}
        written = 0
        for tc in surfacing:
            args = tc.get("arguments") or {}
            subject_key = str(args.get("subject_key", ""))

            # Resolve the floor's axes (trigger type + source) from the
            # eligible-finding lookup. A subject the delta layer did not make
            # eligible is a grounding violation — drop it with a warning
            # rather than persist a fabricated urgency (ADR-0088 §0.3).
            internal_elig = internal_by_key.get(subject_key)
            signal_elig = signal_by_key.get(subject_key)
            rss_elig = rss_by_key.get(subject_key)
            if internal_elig is not None:
                source = SOURCE_INTERNAL
                trigger_type = derive_trigger_type(
                    source=SOURCE_INTERNAL,
                    kind=internal_elig.kind,
                    status=internal_elig.status,
                )
            elif signal_elig is not None:
                # A watchpoint signal is internal-sourced like a limit edge;
                # its family is the third floor axis (ADR-0116 §4), which is
                # what floors a price move to 4 instead of a limit's 5/7.
                source = SOURCE_INTERNAL
                trigger_type = derive_trigger_type(
                    source=SOURCE_INTERNAL,
                    kind=signal_elig.kind,
                    status=signal_elig.status,
                    family=signal_elig.family,
                )
            elif rss_elig is not None:
                source = SOURCE_RSS
                trigger_type = derive_trigger_type(source=SOURCE_RSS, kind=None, status=None)
            else:
                logger.warning(
                    "irene-beat: tenant %s — dropping surfaced subject_key "
                    "%r not in the eligible set (grounding violation).",
                    tenant_id,
                    subject_key,
                )
                continue

            # The model suggests; deterministic rules decide.
            suggestion = clamp_suggestion(int(args.get("urgency_suggestion", 0)))
            urgency = final_urgency(
                suggestion=suggestion,
                trigger_type=trigger_type,
                source=source,
                config=thresholds,
            )
            band = band_from_final_urgency(urgency, thresholds)

            payload: dict[str, Any] = dict(args)
            if rss_elig is not None:
                # RSS finding: enrich the payload with the bucket membership
                # so the bucket can be frozen on a later beat.
                payload = _augment_rss_payload(payload, rss_elig)
            # Options are the advise-half — gated by band. On an
            # informational card they are dropped even if Irene supplied them
            # (a level-1 card is pure fact, never counsel).
            if not options_allowed(band, thresholds):
                payload.pop("options", None)

            await findings.append(
                subject_key=subject_key,
                payload=payload,
                urgency=urgency,
                band=band,
            )
            written += 1

        logger.info("irene-beat: tenant %s — wrote %d finding(s).", tenant_id, written)
        return BeatResult(
            tenant_id=tenant_id,
            findings_written=written,
            silence=False,
            error=None,
        )
    except FloorCalibrationInvalid as exc:
        # ADR-0116 §5: a stored revision that a later change to a code
        # default invalidated fails this tenant's run loudly. Beating on
        # the defaults instead would run a configuration the operator
        # never chose, and would do so silently — the one outcome the
        # composition check exists to prevent. The tick logs this error
        # and leaves next_due_at in the past, so the beat retries once the
        # calibration is corrected.
        logger.error(
            "irene-beat: tenant %s — the stored calibration no longer composes "
            "into a valid Floor Config; refusing to beat on the code defaults. %s",
            tenant_id,
            exc,
        )
        return BeatResult(
            tenant_id=tenant_id,
            findings_written=0,
            silence=False,
            error=f"calibration invalid: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — one beat must not abort the tick
        logger.exception("irene-beat: tenant %s failed.", tenant_id)
        return BeatResult(
            tenant_id=tenant_id,
            findings_written=0,
            silence=False,
            error=str(exc),
        )


__all__ = ["BeatResult", "run_beat"]
