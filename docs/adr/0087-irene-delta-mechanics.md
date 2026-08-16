# ADR-0087: Irene Delta Mechanics — Edge Triggering, Magnitude Re-Trigger, and Deterministic RSS Bucketing

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Feature #033 (Decision Console / Irene)
- **Tags:** decision-console, irene, delta, edge-triggering, dedup, rss, determinism, analytics-purity

---

## Context

Irene's value is *suppression*, not generation: on a quiet day the
Console stays quiet. That property lives in the delta layer — the logic
that decides whether anything has *materially changed* since the PM last
saw the world. This ADR fixes that logic. It sits between the persistence
layer (ADR-0085) and the synthesis contract (ADR-0088): the delta layer
decides *what is worth showing Irene*; Irene decides *how to phrase and
whether to surface it*.

"Delta" conflates two unequal problems.

**The internal half is deterministic and easy.** NAV loads, limit
recomputation, and composition drift produce typed numeric states.
`services/analytics/limit_coverage.py` already emits per-class `status`
(OK/WARN/BREACH) with `coverage_pct`, `max_pct`, and `headroom`. The
delta is a deterministic comparison of current `magnitude` against
`irene_watch_state.acknowledged_magnitude` (ADR-0085) using a configured
threshold. No LLM, no semantics, fully fixture-testable — this is what
makes v0 testable before any live feed exists.

**The external half (RSS) is the hard problem.** One market event
produces several headlines across several feeds; that is *one* event, not
several. A byte diff is insufficient — semantic de-duplication is needed.
But the project holds a hard principle: **stable identifiers must never be
LLM-formed** (subject keys, dedup keys must be deterministic/rule-based).
If `subject_key = rss:cluster:<hash>` were LLM-derived, a news item's
identity would be non-deterministic and the entire edge/dedup behaviour
would be unreproducible and unauditable.

Two further requirements come from the design discussion:

- **Edge triggering, not level triggering.** Surface on the *rising edge*
  (a breach begins); stay silent while the state persists. The falling
  edge (all-clear) is itself a message. Deltas are computed against the
  *acknowledged* state (the Journal), not the previous raw heartbeat.
- **Magnitude re-trigger.** A material escalation *within* an existing
  breach (50.5% → 58%) warrants a fresh finding; noise (50.5% → 50.6%)
  does not.

## Decision

### Internal delta: deterministic magnitude comparison

- Per subject, compare current `magnitude`/`band` against
  `irene_watch_state.acknowledged_magnitude`/`band`.
- **Rising edge:** band worsens across a boundary → eligible finding.
- **Falling edge:** band improves → reset `acknowledged_*`; append an
  all-clear finding deterministically capped at `informational` (ADR-0088
  floor rule). Resetting `acknowledged_*` is mandatory, never optional,
  so a later re-entry edge-triggers correctly.
- **Magnitude re-trigger:** within an unchanged band, if
  `|current − acknowledged| ≥ re_trigger_delta[subject_type]` (Floor
  Config, ADR-0088), emit a fresh finding. Otherwise stay silent.
- All thresholds are configuration, not hardcoded constants; the delta
  layer is DB-free and FastAPI-free and must pass
  `tests/regression/test_analytics_layer_pure.py` if placed under
  `services/analytics/`.

### External delta (RSS): deterministic bucket, LLM only for presentation

Adopt the hybrid ("Weg 3"):

1. **Deterministic bucketing forms the key.** A coarse bucket key is
   built from `(time_window, allowlist_tag, topic_token)`:
   - `time_window` — a rolling window (e.g. 24–48h), configuration.
   - `allowlist_tag` — the asset-class/entity tag already carried in
     `config/web_research.yaml`; Irene does not invent it.
   - `topic_token` — derived by **embedding similarity** against existing
     open buckets (chosen over brittle keyword fingerprints for
     robustness).
   - `subject_key = rss:cluster:<hash(time_window, allowlist_tag,
     bucket_membership)>`.
2. **The LLM only phrases the card** within an already-formed bucket. It
   may mark items as narratively related, but that marker lands **only in
   the finding payload**, never in `subject_key` and never in
   `watch_state`.

### Embedding determinism (reconciling similarity with the key invariant)

Embeddings are model-dependent and potentially non-deterministic across
model versions, which is in tension with a deterministic key. The key is
made reproducible by construction:

- A **pinned embedding model** and a **fixed similarity threshold**
  (configuration) govern bucket assignment.
- The hash is taken over **bucket membership** (the set of item
  identities assigned to the bucket), **not** over the embedding vector.
- Bucket assignment (nearest open bucket above threshold, else new
  bucket) is a deterministic function of inputs given the pinned model
  and threshold.
- Changing the pinned model or threshold is a configuration change with
  audit implications and is treated as such.

### Hard invariant: the key precedes the LLM

`subject_key` is computed **before the LLM sees the bucket**. Ordering in
code is the guarantee: the key already exists when Irene is invoked, so
the LLM *cannot* be key-forming by construction. This invariant is
enforced by a regression test analogous to
`test_analytics_layer_pure.py`: the key-formation path must contain no
LLM/model calls.

### Materiality capping and correlation

- **RSS-only findings** are deterministically capped at `informational`
  (ADR-0088 floor). A headline alone never escalates to a higher band, so
  a mis-bucketed pair costs at most "one informative card too many/few",
  never a mis-identified critical alarm.
- **Correlation with internal state** lifts the card off the RSS cap:
  when an internal `watch_state` edge fires, it is an *internal* finding
  with full floor, and the RSS item appears only as *basis/context* on
  that card. This is the denominator case (a public-equity drop reported
  via RSS pushing the internal private-markets ratio over a limit without
  any transaction).

## Consequences

- The internal half is fully deterministic and fixture-testable,
  satisfying the v0 test strategy (replay of a historical stress day as a
  deterministic fixture).
- The RSS half gains semantic clustering without ever letting the LLM
  form identity; the embedding dependency is contained behind a pinned,
  configured, auditable boundary.
- A new regression test enforces the key-forming invariant, making the
  "LLM never key-forming" principle *machine-enforced* for the first time
  rather than only documented.
- A pinned embedding model becomes a new runtime dependency and a
  configuration item with audit weight; model/threshold changes must be
  recorded.
- The delta layer produces *eligible findings*; whether they surface, and
  their final urgency/band, is decided in ADR-0088.

## Alternatives Considered

- **URL/source dedup only ("Weg 2"), clustering deferred to v1.**
  Rejected as the v0 default: honest but weaker; would emit N cards per
  event. Retained conceptually as the degenerate fallback if the
  embedding path is unavailable.
- **Full LLM/event clustering forming the key.** Rejected: violates the
  LLM-never-key-forming principle; non-reproducible identity.
- **Keyword/entity fingerprint for `topic_token`.** Rejected in favour of
  embedding similarity: brittle against differing wording for the same
  event. (Kept as a documented fallback.)
- **Hashing the embedding vector directly.** Rejected: vectors are
  model-version-sensitive; hashing membership instead keeps the key
  stable under a pinned model.
- **Level triggering (surface while breached).** Rejected: produces
  repeated identical alarms; edge triggering against acknowledged state
  is the suppression mechanism.

## Compliance & Audit Relevance

- **Reproducibility & explainability (MaRisk, BAIT/VAIT):** every
  surfaced finding traces to a deterministic trigger — a magnitude
  crossing a configured threshold, or a deterministically-formed RSS
  bucket. "Why did this appear (or not)?" has a rule-based answer, not an
  LLM's discretion.
- **Determinism guarantee:** the key-forming invariant is enforced by
  regression test; the LLM cannot alter the identity of a monitored
  subject or a news cluster.
- **Materiality safety:** RSS-only findings are floor-capped, so
  clustering error cannot manufacture a critical alarm; only corroborated
  internal state escalates.
- **Change control:** the pinned embedding model and similarity threshold
  are configuration with recorded changes, so a shift in clustering
  behaviour is attributable.

## Revision History

- 2026-07-02 — Proposed.
- 2026-07-11 — Accepted against the shipped code. Implemented 2026-07-02:
  `services/irene/internal_delta.py` and `services/irene/rss_delta.py`, with
  the pure delta arithmetic in `services/analytics/irene_delta.py`.
