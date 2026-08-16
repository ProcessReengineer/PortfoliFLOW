# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""One resolution, one observation, three call sites (ADR-0116 §1, §6).

``effective_watchpoints`` is *the* read the beat and the web surface share,
"so 'what was effective when this finding fired' is the same query in both
places". This guard makes that structural rather than conventional, at all
**three** layers a second answer could enter through:

1. **Resolution** — the watchpoint maps only ever reach a consumer through
   :func:`~services.watch_desk.overlay.resolve_watch_desk`; the beat and the
   monitor route both call it; neither reads ``DEFAULT_FLOOR_CONFIG``
   directly nor restates a WARN threshold constant of its own — which is
   precisely how the two drifted apart before ADR-0116 (``services/irene``
   and ``web/routes`` each carried a private ``Decimal("90.0")``).
2. **Fetch** — the four families' batched reads live in exactly one module,
   :mod:`services.watch_desk.signal_observation`, which the beat and the
   monitor both call. Before P6 they lived inside the beat's delta layer,
   which is why the monitor could only have grown a second copy.
3. **Producer** — the pure producers are called from that one module and
   from nowhere else, so a monitor row is literally the number the next beat
   will classify rather than a recomputation that agrees today.

Structural rather than behavioural, deliberately: two call sites can agree
on every value in every test and still be two paths, and it is the *second
path* that is the defect. A behavioural test would only catch it once the
paths had already disagreed.

The route is allowed to *import* the two repositories — it owns the
Calibration editor, the watchpoint list and the write endpoints — so the
checks below target the effective **reads** on that side, and the one
registry read it does perform is pinned to the editor's inventory rather
than to anything that classifies a subject.

ADR-0116 §4 is the test of the promise the resolution's own docstring made
— "when the producers land they extend the same resolution rather than
growing a second one". All four defined families kept it, and P6's
extraction kept the sibling promise for the fetch beneath it.
"""

from __future__ import annotations

import inspect
import re

import services.analytics.irene_floor as irene_floor_module
import services.analytics.signal_watch as signal_watch_module
import services.irene.beat as beat_module
import services.irene.internal_delta as internal_delta_module
import services.irene.signal_delta as signal_delta_module
import services.watch_desk.overlay as overlay_module
import services.watch_desk.seeding as seeding_module
import services.watch_desk.signal_observation as observation_module
import web.routes.watch_desk as watch_desk_route

#: Modules that consume the resolution but must never build one.
_CONSUMERS = (
    beat_module,
    internal_delta_module,
    signal_delta_module,
    observation_module,
    watch_desk_route,
)

#: Everything behind the web surface. None of these may read the registry
#: itself: they are handed a resolution and evaluate against it.
_BEAT_SIDE = (
    beat_module,
    internal_delta_module,
    signal_delta_module,
    observation_module,
)

#: The two beat-side delta layers. Both take the resolution as a plain
#: argument; neither may reach for a repository or a default.
_DELTA_LAYERS = (internal_delta_module, signal_delta_module)

#: The four pure producers. Called from the observation layer and nowhere
#: else — that is what makes a monitor row and a beat classification the
#: same computation rather than two agreeing ones.
_PRODUCERS = (
    "evaluate_price_watchpoint",
    "evaluate_fx_watchpoint",
    "evaluate_nav_freshness",
    "evaluate_cash_coverage",
)

#: Repositories only the signal fetch has any business holding. Deliberately
#: not ``FxRateRepository``: the route legitimately holds that one for the
#: coverage engine, so it would not distinguish a second fetch path from the
#: wiring that was always there.
_FETCH_ONLY_REPOSITORIES = (
    "InstrumentPriceRepository",
    "InvestmentCashflowRepository",
)

#: A bare 90-ish WARN constant, in either of the forms the two former
#: copies took. Matching the literal is the point.
_HARDCODED_WARN = re.compile(r"_WARN_THRESHOLD_PCT\s*[:=]|Decimal\(\"90")


def test_no_beat_side_consumer_reads_the_effective_watchpoints_itself() -> None:
    """The watchpoint maps arrive resolved, or it is a second resolution."""
    for module in _BEAT_SIDE:
        assert "effective_watchpoints(" not in inspect.getsource(module), (
            f"{module.__name__} reads watchpoints directly; the overlay and "
            "signal maps arrive through resolve_watch_desk (ADR-0116 §1)."
        )


def test_the_routes_only_registry_read_is_the_editors_inventory() -> None:
    """The route may list the registry; it may never classify from it.

    ADR-0116 §7 gives the Calibration section a watchpoint list, which asks
    "what does this tenant watch at all" — including the retired identities
    the resolution deliberately excludes. That is a registry question, not a
    calibration one, and it has exactly one call site. Every figure the
    monitor renders still comes from the resolution.
    """
    source = inspect.getsource(watch_desk_route)
    assert source.count("effective_watchpoints(") == 1, (
        "the Watch Desk route reads the registry more than once; the only "
        "sanctioned direct read is the Calibration list's inventory."
    )
    assert "effective_watchpoints(" in inspect.getsource(watch_desk_route._watchpoint_list_context)


def test_the_beat_side_holds_no_calibration_repository_at_all() -> None:
    """On the beat side the resolver is the only door to the two tables."""
    for module in _BEAT_SIDE:
        namespace = vars(module)
        assert "FloorCalibrationRepository" not in namespace
        assert "WatchpointRepository" not in namespace


def test_beat_and_monitor_both_resolve_through_the_shared_function() -> None:
    assert "resolve_watch_desk(" in inspect.getsource(beat_module)
    assert "resolve_watch_desk(" in inspect.getsource(watch_desk_route)


def test_no_consumer_restates_the_warn_threshold() -> None:
    """The former divergence: two modules, two private 90% constants."""
    for module in _CONSUMERS:
        assert not _HARDCODED_WARN.search(inspect.getsource(module)), (
            f"{module.__name__} restates a WARN threshold; it is resolved "
            "per tenant and per subject since ADR-0116 §3."
        )


def test_only_the_beat_names_the_defaults_and_only_as_composition_input() -> None:
    """``DEFAULT_FLOOR_CONFIG`` is what is composed *over*, never run on."""
    # Import-level, so a mention in a docstring does not count as a read.
    assert "DEFAULT_FLOOR_CONFIG" in vars(beat_module)
    beat_source = inspect.getsource(beat_module)
    assert "defaults: FloorConfig = DEFAULT_FLOOR_CONFIG" in beat_source
    assert "thresholds = resolution.config" in beat_source

    for module in (*_DELTA_LAYERS, observation_module, watch_desk_route):
        assert "DEFAULT_FLOOR_CONFIG" not in vars(module), (
            f"{module.__name__} imports the code defaults; it must take the "
            "tenant's effective config as an argument."
        )


def test_neither_delta_layer_has_a_default_resolution_to_fall_back_on() -> None:
    """A default argument here would be the second path, in disguise."""
    entry_points = (
        internal_delta_module.evaluate_internal_deltas,
        signal_delta_module.evaluate_signal_deltas,
        observation_module.observe_signal_families,
        observation_module.observe_signal_family,
    )
    for entry_point in entry_points:
        resolution = inspect.signature(entry_point).parameters["resolution"]
        assert resolution.default is inspect.Parameter.empty, entry_point.__name__
        assert resolution.annotation in (
            "WatchDeskResolution",
            overlay_module.WatchDeskResolution,
        )


def test_the_beat_and_the_monitor_share_one_observation_path() -> None:
    """P6's extraction, pinned: one fetch layer, two callers, no copy.

    The failure this forbids is the one that would have been easiest to
    write: the monitor re-fetching prices and re-calling the producers "the
    same way" the beat does. It would agree on the day it was written and
    diverge on the first change to either copy — and the divergence would be
    invisible, because both surfaces would still render a number.
    """
    for module in (signal_delta_module, watch_desk_route):
        assert "observe_signal_families(" in inspect.getsource(module), (
            f"{module.__name__} does not observe through the shared path "
            "(services.watch_desk.signal_observation, ADR-0116 §6)."
        )

    for repository in _FETCH_ONLY_REPOSITORIES:
        assert repository in vars(observation_module)
        for module in (signal_delta_module, watch_desk_route):
            assert repository not in vars(module), (
                f"{module.__name__} holds {repository}; the signal families' "
                "fetch belongs to the observation layer alone."
            )


def test_the_producers_are_called_from_the_observation_layer_alone() -> None:
    """The third layer: one call site per producer, shared by both surfaces."""
    for producer in _PRODUCERS:
        assert producer in vars(observation_module), producer
        for module in (signal_delta_module, watch_desk_route, beat_module):
            assert producer not in vars(module), (
                f"{module.__name__} calls {producer} itself; a second producer "
                "call is a second observation (ADR-0116 §6)."
            )


def test_the_observation_layer_writes_nothing() -> None:
    """Rendering a row must never advance a subject's state machine.

    The monitor calls the observation layer on **every** request. If it
    could upsert watch-state, acknowledge, or reset an acknowledgement, then
    merely looking at the Watch Desk would consume an edge the operator was
    never shown — the quietest possible way to lose a finding.
    """
    source = inspect.getsource(observation_module)
    assert "IreneWatchStateRepository" not in vars(observation_module)
    for write in (".upsert(", ".acknowledge(", ".reset_acknowledgement(", "session.add("):
        assert write not in source, (
            f"the observation layer performs {write!r}; it is read-only so the "
            "monitor can call it without advancing any subject's state."
        )


def test_the_resolution_alone_decides_which_families_have_a_producer() -> None:
    """P5 extended one tuple, not a second enumeration (ADR-0116 §4).

    The observation layer asks the resolution for a family's watchpoints; it
    never asks the registry which families exist. That is what let
    ``freshness``/``liquidity`` land by appending to
    ``_RESOLVED_SIGNAL_FAMILIES`` rather than by teaching a second module
    the same list — and the assertion is kept at equality, not membership,
    so a fifth family cannot be resolved without someone reading this.

    The observation layer's own render/evaluation order must *be* that same
    tuple, not a second one that happens to match: it is what fixes the
    order of the beat's findings and of the monitor's groups alike.
    """
    assert overlay_module._RESOLVED_SIGNAL_FAMILIES == (
        "price",
        "fx",
        "freshness",
        "liquidity",
    )
    assert observation_module.SIGNAL_FAMILY_ORDER == overlay_module._RESOLVED_SIGNAL_FAMILIES

    observation_side = inspect.getsource(observation_module)
    assert "WATCHPOINT_FAMILIES" not in observation_side
    assert "DEFINED_FAMILIES" not in observation_side
    assert "resolution.signals_for(" in observation_side

    # And the delta layer, which now sees only results, states no family
    # list of its own beyond the four labels its prose needs.
    beat_side = inspect.getsource(signal_delta_module)
    assert "WATCHPOINT_FAMILIES" not in beat_side
    assert "DEFINED_FAMILIES" not in beat_side


def test_every_resolved_family_has_a_shape_and_a_floor_trigger() -> None:
    """A family reaches the beat only when all three seams know it.

    The resolution decides which families are evaluated; the shape map
    decides how their columns become a threshold; the floor's family map
    decides what urgency their findings floor to. A family present in the
    first and missing from either of the others would be evaluated and then
    silently floored as a *limit* — the one mislabelling ADR-0116 §4 spends
    a whole section forbidding.
    """
    for family in overlay_module._RESOLVED_SIGNAL_FAMILIES:
        assert family in overlay_module._SHAPE_BY_FAMILY, family
        assert family in irene_floor_module._TRIGGER_BY_SIGNAL_FAMILY, family
        assert family in observation_module._EVALUATOR_BY_FAMILY, family


def test_the_freshness_singleton_key_is_stated_once() -> None:
    """The installer writes the key the resolution falls back to.

    ``freshness:{investment_id}`` subjects carry no registry row, so their
    mute and thresholds resolve through the singleton's wildcard key. Two
    copies of that string — one in the seed installer, one in the lookup —
    would fail open: every enumerated subject would silently take the
    tenant defaults, and a mute would appear to do nothing.
    """
    assert (
        seeding_module.FRESHNESS_SUBJECT_KEY is signal_watch_module.FRESHNESS_WILDCARD_SUBJECT_KEY
    )
    assert (
        signal_watch_module.SINGLETON_SUBJECT_KEY_BY_FAMILY["freshness"]
        is seeding_module.FRESHNESS_SUBJECT_KEY
    )


def test_the_add_flow_forms_the_same_subject_keys_the_seeder_does() -> None:
    """A hand-added watchpoint and a seeded one name the subject alike.

    Not cosmetic: the seeder's idempotency is keyed on what each family is
    *about* among the live identities, and the resolution keys its maps on
    ``subject_key``. Two spellings would give one instrument two subjects —
    one of them permanently silent, because only one can win the map.
    """
    assert watch_desk_route._SUBJECT_KEY_BUILDER["price"] is seeding_module.price_subject_key
    assert watch_desk_route._SUBJECT_KEY_BUILDER["fx"] is seeding_module.fx_subject_key
    assert "default_display_name" in vars(watch_desk_route)


def test_the_subjects_watched_tile_counts_the_monitors_own_groups() -> None:
    """The fourth layer, added by P7: the tile is not a second enumeration.

    The Briefing's "Subjects watched" tile sits directly above the monitor
    and answers the same question. It answers it by *summing the monitor's
    group figures* — so a tile and a group header cannot disagree, and the
    counting rules (muted counts, no-data counts, retired does not) are
    stated once, where ``subject_count`` is built.

    The failure this forbids is the plausible one: the tile re-asking the
    resolution how many ``price`` identities are live and re-enumerating
    the book for ``freshness``. It would agree on the day it was written
    and drift on the first change to either side — silently, because both
    surfaces would still render a number.
    """
    counter = inspect.getsource(watch_desk_route._signal_subject_count)
    assert "subject_count" in counter
    for second_path in ("signals_for(", "observe_signal_families(", "list_active("):
        assert second_path not in counter, (
            f"the subjects-watched count reaches for {second_path!r}; it sums the "
            "monitor's own group figures and derives nothing (ADR-0116 §6)."
        )

    # And the tile projection itself is handed the figure: it takes no
    # resolution and no session, so it has nothing to count *with*.
    tile_parameters = inspect.signature(watch_desk_route._build_tiles).parameters
    assert "signal_subject_count" in tile_parameters
    for counted_with in ("resolution", "db_session", "session"):
        assert counted_with not in tile_parameters, (
            f"_build_tiles takes {counted_with!r}; the tiles project figures "
            "the route already derived and derive none of their own."
        )
