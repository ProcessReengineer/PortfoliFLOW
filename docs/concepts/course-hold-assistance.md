# Concept Sketch: Course-Hold Assistance

**Status:** Exploration / concept sketch — **not roadmap-ready**, **not an ADR precursor**.
**Filed:** 2026-07-20
**Resumption trigger:** Not before Strand 3 (TA engine). The consequence
assessment described in §4.3 depends on projections that only become
dependable there. Until then this document is latent by design.
**Relation to accepted decisions:** Strictly inside the guardrails of ADR-0088
(Irene synthesis contract), ADR-0089 (Watch Desk, calm by default),
ADR-0106 (options as decision-support prose), ADR-0107 (Cases). It moves
**none** of those lines.

> **How to read this document.** This is a thinking record, not a commission.
> Nothing here is decided. It exists so that a line of reasoning developed over
> one concept conversation is not lost, and so that a future reader — including
> its author — can resume it without reconstructing it from scratch. It must
> not be mistaken for a decision, and it must not be handed to an
> implementation session. Between this sketch and any Claude Code prompt lie at
> least one ADR (probably several, along the delivery stages in §8), a UI mock,
> and a fresh Repomix verification.

---

## 1. Origin

The sketch began as a deliberately half-serious question: could PortfoliFLOW
offer an "autopilot" for institutions too small to staff portfolio management —
very small pension schemes, foundations, endowments where the portfolio manager
role is a side job or an honorary post? Today such organisations have little
choice but to mandate an established consultant who prepares everything, leaving
the nominal PM as a reviewer with a veto.

The first framing of that idea was: PortfoliFLOW analyses the portfolio,
compares it against the target, reads the attribution, proposes decisions, and
the user only clicks "Yes" — having first been given a full explanation. The
appeal is obvious: enormous relief of manual work, and better decisions for
under-resourced institutions.

That first framing was rejected, and the reasoning behind the rejection is the
substance of everything below, so it is recorded here rather than discarded.

---

## 2. Why the first framing was rejected

Two objections, one architectural and one legal.

**Architectural.** A single line runs through ADR-0088 and ADR-0089: *Irene
proposes, the floor decides, the PM acts.* The model suggests an urgency; a
deterministic floor overrides it by rule ("why an 8?" must have a rule-based
answer). The recommendation never stands without its computed figure beside it.
The MaRisk note in ADR-0089 states the boundary explicitly: the UI presents
decision support; the PM acts and records the resolution. A chain of "Yes"
clicks moves that line — regardless of what the UI claims, it turns decision
support into decision making.

**Legal.** The hoped-for consequence — that neither the software, its authors,
nor the model could be held liable, because the human clicked — does not follow.
Liability does not track where the button sits; it tracks **de facto decision
authority**. An honorary PM who skims a three-paragraph rationale and clicks
"Yes" because he does not command the subject matter (which is precisely why he
is using the tool) is not making an investment decision — he is rubber-stamping
one. Under German supervisory law (KAGB/VAG environment, advice versus
management) an advisory or de facto management relationship arises anyway. The
"Yes button as liability shield" ignores exactly the regulatory substance that
constitutes PortfoliFLOW's moat.

The deeper problem is that the framing optimises for **organising responsibility
away**, which is the opposite of what the Irene architecture achieves. That
architecture is auditable precisely because it holds responsibility
**explicitly**: every finding terminates in an attributable resolution with
actor and timestamp.

---

## 3. The reframing: what an autopilot actually is

The aviation metaphor, taken seriously, rescues the idea. A real autopilot is
not a decision maker. It is a control loop with defined operating limits and a
handback choreography. Four properties make it safe:

1. It holds a course **set by the pilot** — it does not invent one.
2. It operates only inside an **envelope**; outside it disengages rather than
   improvising.
3. It **reports what it is doing and why**, continuously and legibly.
4. On limit exceedance it hands control back **actively and audibly** ("I have
   control") rather than flying on silently.

The error in the first framing was ignoring property 1: there the model both
sets the course and flies it. In a real autopilot the human sets the course
once, deliberately, at a high level of abstraction — and the machine merely
holds it.

**Leading idea in one sentence.** The human sets the **course** once and
deliberately (SAA, investment limits, liquidity requirement, tolerance bands);
the deterministic analytics layer **holds** it and reports deviations; the LLM
only **phrases**; every correction is a **documented decision by the human**,
not a dismissed suggestion.

This is not new architecture. It is a reformulation of the existing
Irene/Watch Desk machinery from "abstract materiality" to "deviation from the
self-set course".

**Why this needs PortfoliFLOW at all.** A frontier LLM can offer a plausible
opinion on a portfolio. What it cannot do is compute the actual state
deterministically against a versioned, tenant-specific SAA and a historised
limit set, form cashflow-adjusted returns correctly for drawdown vehicles
(ADR-0066 — the point at which `pct_change()` is simply wrong), and tie every
deviation to an auditable, rule-based materiality threshold. The value of the
platform in this construction is precisely the **DB-free deterministic analytics
layer as envelope definition**. The model phrases; it neither computes nor
decides.

---

## 4. What stays constant across all modes (the envelope)

These four properties are not mode-dependent. They are the foundation.

### 4.1 Set course, not invented course

The reference is always the human-set SAA + limit sets (ADR-0056) + cashflow
plan (Strands 1/2) + tolerance bands. The LLM never sets the course.

### 4.2 Envelope = tolerance bands

An extension of the Watchlist (ADR-0089 — already today the single explicit,
auditable tuning surface: monitored subjects and their thresholds). The PM sets
a band and a handling rule per asset class or limit. Human-set and versioned —
**not** model-learned. Learned suppression remains excluded (ADR-0089), for the
auditability reason given there.

### 4.3 Consequence assessment is deterministic, not generated

"Returning to target requires a reduction of €X; effect on liquidity, on limit
utilisation, on expected return and risk" — computed in the DB-free analytics
layer, including cashflow-adjusted returns (ADR-0066). The model phrases the
figure and never invents it (ADR-0106: never inventing quantities). *This is the
component that depends on Strand 3 and sets the resumption trigger for this
document.*

### 4.4 Announced handback

An explicit class of findings in which the system **deliberately proposes
nothing** and escalates instead: this lies outside what I can classify for you —
here you need human counsel. Candidates: structural market breaks, illiquid
positions without dependable valuation, anything near a regulatory boundary,
anything the analytics layer cannot capture deterministically.

This is, counter-intuitively, the most trust-building feature rather than the
weakest: a system that knows and announces its own limits is more credible than
one with a "Yes" answer for everything. It is the architectural embodiment of
"a venue for the PM's own decisions, never a broker or advisor" (ADR-0107).

---

## 5. The assistance axis — naming and modelling

A later turn in the discussion raised a real problem: a single uniform treatment
serves neither audience. An experienced PM may feel patronised and will often
have better ideas than the proposed course of action; an inexperienced one may
be overwhelmed by the same default.

The intuition is right. The naming is the trap.

**Not "intensity level". Not an autonomy axis.** "Intensity" suggests the system
becomes *more autonomous* at higher settings — the very axis rejected in §2. At
the top of that axis sits the "Yes" click as a fig leaf again. That axis must
not exist.

The correct axis is **assistance mode**, analogous to automation mode selection
in a cockpit. It varies **not** how much the machine decides (always: nothing),
but **how much guidance, preparation and explanation** it offers the human — and
**how deep the check-back** is before a decision is documented.

Two pilots, identical accountability, different assistance:

| | Experienced PM | Non-professional / inexperienced PM |
|---|---|---|
| Wants | Relief, brevity, own judgement | Guidance, context, classification |
| Needs | Signal without condescension | Explanation, consequence assessment, check-back |
| Risk if miscalibrated | Feels not taken seriously | Overload, or blind rubber-stamping |

Both remain **equally authorised and equally accountable**. The mode changes the
*choreography of assistance*, never the *attribution of the decision*.

---

## 6. The three proposed assistance modes

Deliberately few, named rather than numbered — a "7 out of 10" scale would
suggest a precision the system does not have, the same objection that led
ADR-0088 to reject exposing the raw 1–10 urgency as a badge.

### Mode "Advisory" — for the experienced PM

- Finding plus figure, terse. Course deviation visible, tolerance band named.
- **No** elaborated return options unless explicitly requested.
- No consequence assessment by default — the PM computes the implication
  himself or requests it.
- Aviation analogue: the flight director. Shows the deviation; does not fly.

### Mode "Guided" — the default

- Finding plus figure plus return options as prose (ADR-0106).
- Consequence assessment on request.
- Aviation analogue: the autopilot that holds course and prepares the next
  manoeuvre, but hands back at decision height.

### Mode "Guardian" — for the non-professional PM

- Finding plus figure plus options plus the consequence assessment, **always**,
  unrequested.
- **Explanatory check-back before documentation**: before a case is closed, a
  brief comprehension confirmation ("this return reduces the liquidity reserve
  to X — is that consequence clear to you?"). Not a paternalistic gate but a
  *deliberately placed point of friction* against thoughtless rubber-stamping.
- More contextual prose, more reference to the underlying policy.
- Aviation analogue: the autopilot that calls out the next step more loudly and
  explicitly on an approach.

**Invariant across all three modes:** the recommendation never stands without
its deterministic figure (ADR-0088/0089); the model decides nothing; the floor
stays deterministic; every resolution is attributed.

---

## 7. Where "setting the course" comes from

The one-time, deliberate act of setting the course is the regulatory pivot. It
is what turns "tell me what to buy" (advice) into a **target-versus-actual
comparison against a policy the client set himself** (software tool).

Components that already exist:

- **SAA** — present.
- **Investment limits** — historised as limit sets (ADR-0056).
- **Liquidity requirement** — cashflow planning (Strands 1/2, Planning Desk).
- **Tolerance bands** — *new*, as a Watchlist extension.

Essentially missing: the tolerance band definition, and the reformulation of
Irene findings from materiality to course deviation.

---

## 8. Regulatory self-location

The whole construction stands or falls on a principle ADR-0107 already states:
PortfoliFLOW is a venue for the PM's own decisions, never a broker or advisor.
The assistance mode changes the **assistance**, never the **attribution**:

- No mode takes the decision away from the human.
- No mode forwards decisions automatically to execution partners.
- The Guardian friction point is not a displacement of liability but its
  opposite: it ensures the documented decision is an *understood* one, which is
  what makes it cleanly attributable to the human in the first place.
- Every correction ends as a closed case with a mandatory closing note
  (ADR-0107) — the documented decision file, which is the actual moat.

The honorary PM is enabled **to understand and be accountable for** the
proposals — not enabled to *skip* the understanding. That is the single design
decision separating this concept from "autopilot as fig leaf".

**A note on automation surprise.** Aviation knows a class of accidents in which
the autopilot did something the pilot did not understand, because he had lost
the mental model. The lesson drawn was not "less automation" but "automation
whose state is legible at all times". This is why the grounding rule from
ADR-0088/0089 — *the recommendation never stands without its figure* — is not
compliance ballast but the safety principle of the entire construction. The
small PM must never be surprised. He must always see *why* the course counts as
deviating and *what* the correction achieves. As long as that holds,
"course-hold assistance" is an honest description rather than a marketing
euphemism.

---

## 9. Open questions (deliberately unanswered)

- **Where is the mode set?** Tenant default versus per-user (the `user_id` seam
  from ADR-0089 exists but is unused). Probably per-user with a tenant default —
  but that is a later decision.
- **May the mode vary per finding class?** For example Advisory for rebalancing
  but Guardian near limit boundaries. Attractive, but a complexity risk
  (featuritis warning).
- **How does the Guardian friction point relate to the calm-by-default thesis?**
  It must not turn calm into nagging. Calibration open.
- **Interaction with the provider directory / Execution Network.** Deliberately
  out of scope — any automatic forwarding to execution partners is precisely the
  line that must not be crossed. The autopilot ends at the human's documented
  decision.
- **Relation to the TA engine (Strand 3) and active pacing sliders.** The
  consequence assessment needs dependable projections, partly available only
  with Strand 3. This is the resumption trigger recorded in the header.

---

## 10. Possible delivery stages (should this ever become roadmap-ready)

Independently deliverable, in order of increasing assistance — **not**
increasing autonomy:

1. **Course framing** — phrase Irene findings against SAA and limits rather than
   abstract materiality. Pure presentation work on the ADR-0106 basis.
2. **Tolerance bands** — Watchlist extension, human-set.
3. **Consequence assessment** — deterministic "effect of the return" inside the
   case.
4. **Announced handback** — the escalation finding class.
5. **Assistance modes** — the three operating modes as a selection.

Stages 1–2 are the defensible, immediately valuable core: small, regulatorily
unobjectionable, and useful on their own. Stage 5 presupposes 1–4 and is the
actual subject of the "intensity level" intuition — correctly modelled as an
assistance axis rather than an autonomy axis.

---

*End of sketch. The Planning Desk (Strand 2) remains ahead of this; the
document is filed as latent exploration and costs nothing while it lies.*
