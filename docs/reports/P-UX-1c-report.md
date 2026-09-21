<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2025-2026 Sönke Pinkernelle
-->

# P-UX-1c — Atlas waits for nested loaders and charts

UX overhaul track, hygiene step after P-UX-1b. The first real run on
2026-09-21 showed charts as "Loading…" placeholders. Two causes the reveal
pass cannot see by scrolling — **nested** lazy loaders and **Plotly's**
asynchronous draw — plus a generic backstop. No new dependency.

---

## Operator action

**Nothing is committed.** Staging and the commit are yours.

Proposed commit message:

```
fix(ux): atlas drives nested lazy loaders and waits for Plotly before capture (P-UX-1c)
```

**The two-area proof run is yours.** No server answered on
`minathena-capital.localhost:8000` or `localhost:8000` during this session, and
no credentials were exported. Take it with:

```bash
source .venv/bin/activate
export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=… PF_ATLAS_PASSWORD=…
python tools/ux_atlas.py --bands 1200 --area front-office --area transactions
```

Then open the Front Office full PNG and confirm two things by eye: the
**overview** charts are drawn, and the **per-investment** charts below them are
drawn — those are the ones that came back as placeholders, and they are the
ones F-UX-1c.1 is about. In `manifest.json`, `loading_placeholders` should be
`0` for both areas; `charts_pending` should be `0`; `loaders_hidden` may be
non-zero and is not a defect (see F-UX-1c.4).

In place of that run, the browser-side logic was exercised end to end against a
synthetic page reproducing the app's exact loader and chart shapes — real
htmx 1.9.12, real Plotly 2.35.2, the nested `charts_section` → triplet
structure, the `innerHTML`-swapping portfolio-review article, and a loader
inside a collapsed `<details>`. Numbers in §Proof. That fixture is throwaway
and is not in the repo.

**One decision is yours**, flagged rather than taken silently: F-UX-1c.4 added
a sixth manifest field, `loaders_hidden`, beyond the five the prompt named. It
exists so that a Section hidden behind a disclosure is *reported* without being
called `suspect`. Fold it into `loaders_left` if you would rather have the five.

---

## Verify-first

Anchors are from the 2026-09-19 pre-1b image; the 1b tool is longer, so actual
lines are given.

| # | Check | Result | Mode |
|---|-------|--------|------|
| 1 | Clean tree, 1b in history | `git status --porcelain` empty; one `(P-UX-1b)` commit (`2378788`) | PASS |
| 2 | Reveal pass exists | All five present: `settle` L572, `prepare` L596, `reveal` L633, `capture_route` L826, `run_scene` L910; tool is 1,549 lines | PASS |
| 3 | Nested loaders | `charts_section.html` **L48–52** exactly as anchored: `.ch-article__lazy`, `hx-get="/api/charts/investment/{{ inv.id }}"`, `hx-trigger="revealed"`, `hx-swap="outerHTML"` | PASS |
| 4 | Other nested loaders | Eight non-`_lazy` files match the grep, but only **three** carry a real nested loader *attribute*; the other five only mention the trigger in a comment. See below. | REPORT |
| 5 | Chart render contract | `overview_section.html` **L196–232**: `renderTarget` sets `data-pf-rendered="1"` after `Plotly.newPlot` on `.pf-plotly-target[data-spec]`. `chart_snapshot.js` L67 renders `[data-pf-chart-plot]`. Both confirmed — **but they are two of five conventions**, see F-UX-1c.1 | REPORT |
| 6 | Placeholder pattern | 25 distinct placeholders, all `<p>`/`<span>` whose text starts `Loading ` and ends `&hellip;` → U+2026. Carrier classes: `pf-section__lazy-skeleton` (22), `pr-investment-stack__loading`, `ch-article__skeleton`, `lim-history__detail-skeleton`, `pd-sa__builders` | REPORT |
| 7 | HTMX version and settle delay | htmx **1.9.12** (`base.html` L65), sse ext L68. No `settleDelay` / `defaultSettleDelay` anywhere in `web/` → the default 20 ms stands | REPORT |

**Check 4 in full.** Files matching `hx-trigger="revealed"` outside `*_lazy.html`:

| File | Real nested loader? |
|---|---|
| `charts_section.html` | **Yes** — L48–52, per investment |
| `portfolio_review_section.html` | **Yes** — L120–123, per investment `<article>`, `hx-swap="innerHTML"` |
| `planning_desk_scenario_results.html` | **Yes** — L172–177, composition drill-down (scenario-run surface only) |
| `overview_section.html` | No — comment only |
| `benchmarks_attribution_section.html` | No — comment only |
| `limits_section.html` | No — comment only |
| `portfolio_analysis_section.html` | No — comment only |
| `statistics_section.html` | No — comment only |

No STOP fired.

---

## What changed

All of it in `tools/ux_atlas.py`, plus the README and the 1b test file.

### Debounced quiet (§2.4)

`settle` no longer returns on the first `.htmx-request === 0` reading.
`.htmx-request` must read zero continuously for `QUIET_HOLD_MS = 400`, polled
every 50 ms, inside the unchanged 5 s budget. Between a parent swap landing and
the nested request it triggers there is htmx's 20 ms settle delay and one
`revealed` re-check; undebounced, that gap reads as quiet and the reveal pass
walks straight past the Section about to arrive.

The budget is a real `time.monotonic()` deadline rather than a count of polls,
because each poll also pays a round-trip into the page — counting polls would
quietly stretch 5 s into 15 s on a slow surface, and that budget is what stops
the assistants chat's open stream from holding the run.

The arithmetic is a pure function, `advance_quiet_window`, which is where the
tests are: an off-by-one that satisfies the hold on its first quiet sample
restores exactly the race the debounce exists to close, and no browser would
notice.

### Loader-driven reveal (§2.1)

The 1b geometric walk is kept as the first step — it is what exercises
scroll-spy and sticky headers — and `drive_loaders` now follows it, taking the
unfired loaders themselves as the work list: each is scrolled to its own
centre, the page is allowed to go quiet, and the DOM is re-read, because what
just landed may have brought more. Capped at `MAX_REVEAL_ROUNDS = 12`.

"Unfired" is read off htmx's own `htmx:beforeRequest`, through a **context init
script** that stamps `data-pf-atlas-fired` on the element making the request
and bumps a monotonic `window.__pfAtlasFired`. Two details that are not
optional:

- It must be an *init* script. A listener attached after navigation misses
  every above-the-fold loader, and those would then read as unfired for the
  rest of the capture.
- Disappearance would not work as the marker. Most loaders swap `outerHTML` and
  do vanish — but `portfolio_review_section.html` swaps `innerHTML` into the
  `<article>` that carries the trigger, so that one keeps its `hx-trigger`
  attribute forever and would be chased for all twelve rounds.

The loop stops early when a round dispatches nothing (the tally did not move):
a round that achieved nothing is a round every later round would repeat. This
is what keeps the `<details>` case from eating the whole budget — measured at
12 wasted rounds before the early break went in.

### Chart readiness (§2.2)

`await_charts` waits up to `CHART_TIMEOUT_MS = 15_000`, polling 250 ms, until
no chart target is undrawn; then dispatches `resize`, waits one
`requestAnimationFrame`, and settles once more.

Two departures from the prompt's spec, both forced by what the tree actually
contains — see F-UX-1c.1 and F-UX-1c.2.

### "Still loading" backstop (§2.3)

Immediately before the shutter, `count_loading_placeholders` counts visible
elements whose **own** text (direct text-node children only, whitespace
collapsed) matches `^Loading\b.*…$`. The pattern is one constant handed to
the browser verbatim as the argument to `new RegExp`, so the Python tests of it
test what actually runs; both engines were checked case-for-case against the 25
placeholders in the tree.

### Scenes (§2.5)

No change needed. `capture_shot` was already the one seam both `capture_route`
and `run_scene` go through, so all three waits apply to scenes by construction.

---

## Findings

### F-UX-1c.1 — the prompt's chart selector misses the charts that failed

`charts_investment_triplet.html` L38 uses `class="ch-chart plotly-target"`, not
`pf-plotly-target`. The prompt's §2.2 selector
`.pf-plotly-target[data-spec]:not([data-pf-rendered="1"])` would therefore have
skipped exactly the per-investment charts that prompted this work, and reported
`charts_pending: 0` on a page full of empty boxes.

There are **five** render conventions in the tree:

| Convention | Where |
|---|---|
| `.pf-plotly-target` + `data-spec` | overview, limits, portfolio review (section and per-investment) |
| `.pf-plotly-target` + `data-plotly-spec` | benchmarks & attribution (stages a, b, c) |
| `.plotly-target` + `data-spec` | per-investment triplet, statistics, portfolio analysis, investment detail |
| `.plotly-target`, spec inlined in the script | SAA optimisation (`#saa-frontier-chart` carries no `data-spec` at all) |
| `[data-pf-chart-plot]` | `chart_snapshot.js` — chat history, Cases timeline pins |

Implemented as the union `.pf-plotly-target, .plotly-target,
[data-pf-chart-plot]`, which covers all five including the spec-less SAA
target. **This is a coupling to watch**: a new chart that invents a sixth
convention becomes invisible to the wait. `CHART_TARGET_SELECTOR` carries a
comment saying so, and the README's suspect table points at it.

*A positive alternative, out of scope here (§2, "changing any template →
findings"): one render helper and one target class across all five would delete
this selector.*

### F-UX-1c.2 — `data-pf-rendered` cannot answer "is it drawn"

The prompt keys readiness on `data-pf-rendered="1"`. That flag is a
per-template convention rather than a contract, and in one case it is actively
wrong: `chart_snapshot.js` L64–65 sets `container.dataset.pfChartRendered = "1"`
**before** awaiting `Plotly.newPlot`, so it marks a figure ready that has not
been drawn. Two of the five conventions set no such flag at all.

Readiness is therefore Plotly's own `.js-plotly-plot` marker (or a `.main-svg`
inside), checked on the target and its descendants. The descendant check via
`querySelector` also sidesteps the prompt's `:has()` support question entirely.

### F-UX-1c.3 — `getClientRects()` is the wrong visibility test for `<details>`

Measured, not assumed: Chromium lays out the contents of a collapsed
`<details>` under `content-visibility: hidden` and returns **one rect** for an
element no reader can see. `Element.checkVisibility()` returns `false` for the
same element.

With the prompt's `getClientRects().length > 0`, a placeholder inside any
collapsed `<details>` counts as a visible loading placeholder and flags the
capture `suspect` for a shot that is correct. Both the loader work list and the
placeholder tripwire now use `checkVisibility()`, falling back to the rect
count where it is unavailable.

This is live in the tree: fourteen templates use `<details>`, including
`statistics_section.html` (four sub-blocks) and `limits_section.html`.

### F-UX-1c.4 — an unreachable loader is a design finding, not a capture defect

The prompt makes any `loaders_left > 0` `suspect`. But the two populations are
not the same fact:

- A **visible** loader that never fired is a hole in the shot. `loaders_left`,
  `suspect`, as specified.
- An **off-screen** loader — inside a collapsed `<details>` or an inactive tab
  — is absent from the shot exactly as it is absent from the reader's view. The
  shot is right. Flagging it would make every page carrying a disclosure
  permanently suspect for a reason that is not about the capture.

Split into `loaders_left` (visible, suspect) and `loaders_hidden` (recorded,
never suspect). That is the sixth field flagged in the operator block.

**No loader in the app currently sits inside a `<details>` or a hidden tab** —
all three real nested loaders are in open flow. So this is presently a guard
rather than an active finding, and it is the shape the prompt's §5 anticipated.
It will start reporting the day a Section is put behind a disclosure.

### F-UX-1c.5 — no chart target sits inside a collapsed `<details>`

Checked, because it would have been the expensive case: a chart inside a closed
disclosure whose script only draws on open would be permanently pending and
would cost 15 s of timeout plus a false `suspect` on every run. Walking
`<details>` nesting depth across the four templates that combine disclosures
with charts, no `.plotly-target` or `.pf-plotly-target` element is inside one.
`statistics_section.html` draws all its targets unconditionally at depth 0.

---

## Proof

### Lint, format, typecheck, tests

```
ruff format tools/ux_atlas.py           1 file left unchanged
ruff check  tools/ux_atlas.py           All checks passed!
pyright     tools/ux_atlas.py           0 errors, 0 warnings, 0 informations
ruff format/check tests/tools/…         clean
python -m pytest tests/tools/ -q        40 passed
```

40, up from 19: twenty-one new cases across `TestAdvanceQuietWindow` (7) and
`TestLoadingPlaceholderPattern` (14).

**The DOM parts cannot be unit-tested without a browser** and are not tested
here — `drive_loaders`, `await_charts`, `count_loading_placeholders`, `reveal`
and the capture loop all need a live page. What is unit-tested is the part that
can be silently wrong without one: the debounce arithmetic and the regex.

### Browser-side check against a synthetic page

Since no app server was reachable, the browser-side logic was exercised against
a throwaway fixture served over HTTP, built to the app's shapes: real
htmx 1.9.12 and Plotly 2.35.2 from the same CDNs `base.html` uses; a top-level
`revealed` Section which swaps in two articles each carrying a *second*
`revealed` loader (the `charts_section` → triplet structure); an
`innerHTML`-swapping `revealed` article (the portfolio-review shape); a
`revealed` loader inside a collapsed `<details>`; `.plotly-target` and
`.pf-plotly-target` chart targets drawn by inline post-swap scripts.

Calling `drive_loaders` **alone, with no geometric walk at all**, from a page
that had only been loaded and settled:

| | unfired loaders | triplets | chart targets | requests fired |
|---|---|---|---|---|
| after load, no scrolling | 3 | 0 | 0 | 0 |
| after `drive_loaders` | 1 | 2 | 5 | 4 |

`rounds=2, loaders_left=0, loaders_hidden=1`. Round 1 fired the two visible
top-level loaders; round 2 fired the two nested per-item loaders that round 1
had brought into existence. The one left over is the `<details>` loader, which
never fires and is correctly classed hidden rather than suspect.

`await_charts` then returned `(5, 0)` — all five drawn, cross-checked against
`document.querySelectorAll('.js-plotly-plot').length === 5`, and the five
include the `.plotly-target` shape the prompt's selector would have missed.
`count_loading_placeholders` returned `0`.

Through the full `capture_shot` seam, against a server stalling every lazy
response by 1.2 s to mimic real round-trips:

```
reveal_iterations 12   reveal_complete True    reveal_rounds 0
loaders_left 0         loaders_hidden 1
charts_total 5         charts_pending 0        loading_placeholders 0
scroll_height 8548     png_height 8548
```

`reveal_rounds: 0` there is a result worth naming: with the debounce in place
the geometric walk no longer outruns the swaps, so it fired everything itself
and the loop found nothing left to do. §2.4 does more of the work than expected
and §2.1 is the guarantee behind it. Before the early break went in, the same
page spent all 12 rounds retrying the one `<details>` loader.

**This is not the two-area proof run.** It shows the mechanisms work against
the app's shapes; it does not show the app's own pages come back clean. That
run is in the operator block.

---

## Open questions

1. **Is `loaders_hidden` worth a field?** (F-UX-1c.4.) It reports nothing today
   — no loader in the app is behind a disclosure. Keep it as a guard, or fold
   it into `loaders_left` and take the five fields the prompt named.

2. **Should the five render conventions become one?** (F-UX-1c.1.) The union
   selector works but is a standing coupling: a sixth convention is invisible to
   the chart wait, and nothing fails loudly when that happens. A single render
   helper is a strand-A template change, out of scope here.

3. **Is 15 s the right chart budget?** Untested against the real app, where a
   front-office page may hold twenty-plus per-investment figures. If the proof
   run shows `charts_pending > 0` with figures that do eventually appear, raise
   `CHART_TIMEOUT_MS` rather than accepting the flag.

4. **The resize lands before the viewport change, not after.** §2.2 asks for
   the `resize` because the full-page shot changes the viewport, but the only
   place to dispatch it from is before `page.screenshot`. It makes Plotly
   re-measure against the post-reveal layout, which is the part that matters;
   whether it helps with the screenshot surface itself is unverified. If the
   proof run shows squashed axes in the full PNG, this is the line to revisit.

5. **SSE surfaces are unchanged**, per F-UX-1.9. The assistants chat holds an
   open stream, so it never reaches `networkidle` and pays the full budget at
   every settle — now a little more often, since the loader loop settles once
   per loader. Not measured against that page.
