<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2025-2026 Sönke Pinkernelle
-->

# Screenshot atlas

Dated, local screenshot runs of the web UI, one full-page PNG per
user-visible GET route. Produced by `tools/ux_atlas.py` (P-UX-1) as the
visual companion to the route and element inventory under
`../inventory/`.

Every shot is preceded by a **reveal pass** that scrolls the page so its
lazy Sections actually load (P-UX-1b), and `--bands` cuts the result into
slices a chat can still read.

## Nothing here is committed

Everything this folder holds except this README is generated and
gitignored:

```
docs/ux/atlas/*
!docs/ux/atlas/README.md
```

A run writes `docs/ux/atlas/<YYYY-MM-DD>/` (a same-day re-run gets a
`-2`, `-3`, … suffix rather than overwriting its predecessor), holding
`manifest.json`, the contact sheet `index.md`, and the PNGs under
`<area>/`, `<area>/partials/` and `<area>/scenes/`. With `--bands` on,
each of those folders gains a `bands/` beside its PNGs.

The runs are **regenerable, not archival**: they photograph one commit
against one database, and they go stale the moment either moves. Delete
old ones freely. The manual copies only the images it actually uses into
`docs/manual/img/`, where they *are* committed and are the single
reviewed copy; nothing should ever link into a dated atlas folder from
prose that outlives the run.

## Regenerating

```bash
source .venv/bin/activate
playwright install chromium        # once — a browser download, not a Python package

portfoliflow-web &                 # against a data-bearing dev tenant

export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=…
export PF_ATLAS_PASSWORD=…

python tools/ux_atlas.py --bands 1200
```

`--bands 1200` is the recommended form: it writes the full PNGs *and*
chat-legible slices of them. Drop the flag for a full-PNG-only run.

Tenants resolve by host subdomain (ADR-0063 §1), so `PF_ATLAS_BASE_URL`
must name a tenant host — bare `localhost` works only when
`LOCAL_DEV_TENANT_SUBDOMAIN` is set in `.env`.

Point the run at an instance carrying representative data. Empty screens
are captured all the same, but an empty tenant is a poor baseline; after
a web-test run, `portfoliflow bootstrap` first.

### The six environment variables

| Variable | Required | Meaning |
|---|---|---|
| `PF_ATLAS_BASE_URL` | yes | Tenant base URL, e.g. `http://minathena-capital.localhost:8000` |
| `PF_ATLAS_USER` | yes | Tenant login e-mail |
| `PF_ATLAS_PASSWORD` | yes | Tenant login password |
| `PF_ATLAS_ADMIN_BASE_URL` | no | Super-admin base URL, e.g. `http://admin.localhost:8000` |
| `PF_ATLAS_ADMIN_USER` | no | Super-admin login e-mail |
| `PF_ATLAS_ADMIN_PASSWORD` | no | Super-admin login password |

Credentials are read from the environment only. They never reach a file,
the manifest, the contact sheet or a log line. Without the three admin
variables the `super_admin` routes are skipped with the manifest reason
`no-admin-session` — the rest of the atlas is unaffected.

## Useful flags

| Flag | Effect |
|---|---|
| `--bands 1200` | Also cut every shot into 1200 px slices (default 0, off) |
| `--area transactions` | Restrict the run to one Area; repeatable |
| `--no-scenes` | Route captures only |
| `--viewport 390x844` | A different viewport — the mobile pass |
| `--out <dir>` | Override the dated folder |
| `--routes <csv>` | Select from a different inventory table |

Exit codes: `0` clean, `1` at least one suspect capture, flagged scene
shot or failed scene (the outputs are written either way), `2` the
browser or the Playwright package is missing, `3` login failed — either
at the start, or mid-run when a capture bounces to `/login`. A mid-run
bounce **aborts the remaining routes**: the session is gone, so every one
of them would photograph the login page instead of itself. The manifest
and contact sheet are still written with what the run already had, and
the route it died on carries the reason `session lost — run aborted`.

## Revealing lazy Sections

An Area page renders every one of its Sections and shows one, so the
atlas walks them the way a reader does: by the URL fragment, one
`switch_section` per `data-pf-section` it finds in the DOM. Section
bodies load on `hx-trigger="intersect once"`, which fires when the
element meets the viewport, and `full_page=True` does not scroll — it
lengthens the rendering surface — so a page shot straight after load is a
column of "Loading…" placeholders below the first 900 px. A hidden
Section can never intersect, so the loader driver leaves its loaders out
of the work list rather than spending rounds on something no observer
will see. Each Section gets its own full shot and its own bands, named
`<route>--<section>`, and a scene may name a `section` of its own to
start from.

Before every shot the atlas therefore walks the document to the bottom in
steps of 0.8 viewports (`revealed` fires on intersection, so a
full-viewport step can jump a short Section clean over the observer),
waiting for the network and for HTMX to go quiet after each, re-reading
`scrollHeight` every round because each swapped-in Section lengthens the
page. It stops once the bottom is reached with the height standing still,
scrolls back to the top and lets the last swap paint.

The pass is capped at 60 iterations — roughly 43,000 px at a 900 px
viewport — as a guard against a page that never stops growing. A capture
that hits the guard is flagged `suspect` with `reveal_complete: false`
rather than quietly passed off as whole; if the baseline shows real Area
pages tripping it, raise `REVEAL_MAX_ITERATIONS`, don't ignore the flag.

### Nested loaders

Scroll geometry gets the top-level Sections in, and stops there. A
Section that itself arrived on `revealed` can hold per-item loaders on
the same trigger — `charts_section.html` swaps in one `<article>` per
investment, each carrying a second `hx-get="/api/charts/investment/{id}"`
— and those elements did not exist when the walk passed the height they
now sit at. Nothing scrolls past them afterwards, so they stay
placeholders.

The walk is therefore followed by a loader-driven loop that takes the
loaders as its work list rather than the page's height: every unfired
`[hx-trigger*="revealed"]` is scrolled to its own centre, the page is
allowed to go quiet, and the DOM is re-read — because what just landed
may have brought more. Twelve rounds, far past the two levels the tree
actually nests.

"Unfired" is read off HTMX's own `htmx:beforeRequest`, via a context init
script that stamps `data-pf-atlas-fired` on the element making the
request. It has to be an *init* script: a listener attached after
navigation misses every above-the-fold loader, and those would then read
as unfired for the rest of the capture. Disappearance would not do as the
marker either — most loaders swap `outerHTML` and do vanish, but the
portfolio-review stack swaps `innerHTML` into the `<article>` that carries
the trigger, so that one keeps its `hx-trigger` attribute forever.

Only loaders a reader could see are driven, and the loop stops the moment
a round dispatches nothing — a round that achieved nothing is a round
every later round would repeat.

What is left over splits in two, because the two are not the same fact:

- `loaders_left` — **visible** loaders that never fired. A hole in the
  shot, so the capture is flagged `suspect` with `unfired lazy loaders
  (N)`.
- `loaders_hidden` — unfired loaders that are off-screen anyway, inside a
  collapsed `<details>` or an inactive tab. These are *not* suspect: the
  section is missing from the shot exactly as it is missing from the
  reader's view. They are recorded because a surface that hides a whole
  Section behind a disclosure is worth knowing about — a design finding,
  not a capture defect.

"Visible" is `Element.checkVisibility()`, not `getClientRects()`.
Chromium lays out the contents of a collapsed `<details>` under
`content-visibility: hidden` and hands back a rect for something no
reader can see; `checkVisibility` knows the difference. The rect count is
the fallback for an engine without it.

### Charts

HTMX going quiet says the markup arrived. It says nothing about the
figures: every chart in the tree is drawn by an inline `<script>` that
runs after the swap, and `Plotly.newPlot` is asynchronous, so
`.htmx-request` is long gone before the first trace appears. A page shot
on HTMX-quiet alone is a grid of empty boxes.

After the loaders are done the atlas waits — up to 15 s, polling every
250 ms — for every chart target to have been drawn into. The targets are
`.pf-plotly-target, .plotly-target, [data-pf-chart-plot]`, which is the
union of the five render conventions in the tree, and "drawn" is Plotly's
own `.js-plotly-plot` (or a `.main-svg` inside), on the container or a
descendant. The per-template `data-pf-rendered` flags are deliberately
*not* what is checked: they are conventions rather than a contract, and
`chart_snapshot.js` sets its own before awaiting the draw, so it would
report a figure ready that is not.

Then one `resize` event and a frame to act on it: the figures are
`responsive: true`, and the full-page shot is about to change the viewport
they sized themselves against.

Targets still undrawn are counted into `charts_pending` (out of
`charts_total`) and flag the capture `suspect` with `charts not drawn
(N of M)`.

### The "still loading" backstop

Immediately before the shutter the atlas counts visible elements whose
*own* text reads `Loading …` — the shape every lazy placeholder in the
tree has, from `&hellip;`-terminated `<p>`s and `<span>`s. Own text only,
so an ancestor is not reported alongside the placeholder it wraps; and
visible only, by the same `checkVisibility()` rule as the loaders, so a
placeholder inside a collapsed `<details>` is not reported against a shot
that is right to omit it.

This knows nothing about HTMX or Plotly, which is the point: it is what
catches a loader neither targeted wait recognises. The count lands in
`loading_placeholders` and flags the capture `suspect` with `N loading
placeholders visible`.

### Quiet is debounced

Everywhere the atlas "waits for HTMX", `.htmx-request` must read zero
continuously for 400 ms, polled every 50 ms, inside the same overall 5 s
budget. A single zero reading is not enough: between a parent swap
landing and the nested request it triggers there is HTMX's 20 ms settle
delay and one `revealed` re-check, and during that gap nothing is in
flight and the page is not finished. Undebounced, the reveal pass walks
straight past the section that is about to arrive.

## Bands

A revealed Area page is several thousand pixels tall, and a chat
downscales a long PNG to about 1,568 px on its long edge — which turns
10,000 px of UI into an unreadable smear. `--bands 1200` writes, beside
each full PNG, `bands/<stem>-01.png`, `-02.png`, … : viewport-wide slices
1200 px tall, the last one short.

Bands are the format to upload to a chat. The full PNG is the format for
a human with an image viewer. Both come out of the same run.

Each band is a second `full_page` screenshot narrowed by Playwright's
`clip` to one slice of the same surface, so there is no stitching to go
wrong and the sticky Section header appears once, where it actually sits,
rather than repeated at the top of every slice. A page that fits inside a
single band gets none — that band would be a second copy of the full PNG
under a different name.

## Truncation

Chromium has historically refused a screenshot surface past 16,384 px on
either axis and returned a silently cut image rather than an error. After
each shot the atlas reads the PNG's own IHDR height and compares it to
the `scrollHeight` the reveal pass settled on; a PNG standing exactly at
the cap, or more than 2 px short of the document, is flagged `truncated`
and `suspect` with the reason `page taller than Chromium's 16,384 px cap
— use bands`.

The tripwire did not fire on the Chromium 153 that Playwright 1.63 ships
(a 22,600 px page came back whole). It stays because the cost is one
24-byte read and the failure it guards against is silent — a shorter page
than you asked for, with no error anywhere to say so.

## The manifest

`manifest.json` records the run and every capture. The run header:

| Field | Meaning |
|---|---|
| `generated_at` | UTC timestamp, seconds |
| `git_head` | Short commit the run photographed |
| `base_url`, `admin_base_url` | The hosts visited; never a credential |
| `viewport` | `{width, height}` in CSS pixels |
| `bands` | The `--bands` height used, `0` when off |
| `routes_csv_sha256` | The route table the selection came from |
| `captured`, `scenes`, `skipped` | The three lists below |

Each entry of `captured` carries `area`, `path`, `file`, `status`,
`final_url`, `partial`, `session`, `suspect`, `reason` — and the shot
geometry:

| Field | Meaning |
|---|---|
| `reveal_iterations` | Scroll rounds the reveal pass spent |
| `reveal_complete` | `false` if it ran out of iterations first — the foot of the page may still be placeholders |
| `scroll_height` | The document height the reveal pass settled on, CSS px |
| `png_height` | The PNG's own height from its IHDR, or `null` if unreadable |
| `truncated` | `true` when the PNG does not cover `scroll_height` |
| `reveal_rounds` | Rounds the loader-driven pass spent after the geometric walk; `0` when the page had no `revealed` loaders left to fire |
| `loaders_left` | **Visible** `revealed` loaders still unfired when the last round ended — above zero, the shot shows placeholders |
| `loaders_hidden` | Unfired loaders a reader could not see either; recorded, never `suspect` |
| `charts_total` | Chart targets the page declares |
| `charts_pending` | Of those, how many were still undrawn when the 15 s wait ran out |
| `loading_placeholders` | Visible `Loading …` elements counted immediately before the shutter |
| `bands` | Band paths relative to the run root, top to bottom; `[]` with `--bands` off or on a page that fits in one band |

Entries of `scenes` carry the same twelve fields alongside `name`,
`file`, `ok`, `failed_step` and `reason`.

## Scenes

Sub-surfaces that only exist after a click — a wizard step, an opened
composer — have no URL of their own, so they are captured by walking to
them. `../atlas-scenes.json` is a list of scene objects:

```json
{"name": "transactions-flow-chooser", "area": "transactions",
 "start": "/transactions", "session": "tenant",
 "steps": [{"wait": "#tx-composer-host"}],
 "shot": "flow-chooser"}
```

`session` is `tenant` or `super_admin`. Each step carries exactly one
verb:

| Verb | Form | Effect |
|---|---|---|
| `click` | `{"click": "#id"}` | Click a Playwright selector |
| `fill` | `{"fill": {"selector": "…", "value": "…"}}` | Type into a field |
| `wait` | `{"wait": "#id"}` | Wait until a selector is present |
| `wait_ms` | `{"wait_ms": 250}` | Pause |

After the last step the tool waits for HTMX to go quiet and writes
`<area>/scenes/<shot>.png`. A step whose selector never appears marks the
scene `failed` with the failing step index and the run continues.

The file ships with one worked example. Filling it out for the real
sub-surfaces is strand A's job.

## Reading a run

`index.md` is the contact sheet: one `##` per Area, pages first, then
partials, then scenes, closing with everything that needs a human look —
suspect captures, failed scenes, scene shots that came back truncated or
under-revealed, and the skipped routes by reason. A route's bands, when
there are any, fold away under a `<details>` beneath its full shot so the
sheet keeps its one-image-per-route rhythm.

Two things are expected rather than defects:

- **Partials render without the shell chrome.** They are HTMX fragments;
  a route that returns one has no `base.html` around it. They are
  captured deliberately, filed under `<area>/partials/` and flagged in
  the manifest.
- **Non-200 routes are still captured.** An error surface is a UX
  surface, and it is flagged `suspect` so it surfaces in the contact
  sheet rather than being dropped.

A capture is flagged `suspect` for one reason, the first that applies of:

| Reason | Means |
|---|---|
| `page taller than Chromium's 16,384 px cap — use bands` | The PNG does not cover the document — re-run with `--bands` |
| `reveal hit the 60-iteration guard …` | The geometric walk never reached a standing height; the foot of the page may be placeholders |
| `unfired lazy loaders (N)` | N `revealed` loaders were never dispatched. Almost always they cannot be scrolled to — inside a collapsed `<details>` or an inactive tab. A design finding about that surface, not an atlas bug |
| `charts not drawn (N of M)` | N chart targets never became Plotly plots inside 15 s. Either the figure is genuinely broken, or its render path uses a convention `CHART_TARGET_SELECTOR` does not cover — check the template's `querySelectorAll` against that constant |
| `N loading placeholders visible` | Something still says `Loading …` that neither targeted wait knew about. Find it in the PNG, then work out which loader it belongs to |
| `png is … bytes` | Implausibly small — likely an empty shell or an error card |
| `HTTP 4xx/5xx` | An error surface, captured deliberately |

A "Loading…" placeholder in a shot is always a defect, and now always
reported: `loading_placeholders` is the backstop that says so even when
`loaders_left` and `charts_pending` are both clean.

To check which Playwright the run used:

```bash
python -c "import importlib.metadata as m; print(m.version('playwright'))"
```
