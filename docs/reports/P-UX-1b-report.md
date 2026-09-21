<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2025-2026 Sönke Pinkernelle
-->

# P-UX-1b — Atlas captures the whole page

UX overhaul track, hygiene step between P-UX-1 (the tool) and the first
baseline run. Four capture-correctness fixes to `tools/ux_atlas.py`, no new
dependency.

---

## Operator action

**Nothing is committed.** Staging and the commit are yours.

Proposed commit message:

```
fix(ux): atlas reveals lazy sections before capture, detects truncation, writes chat-legible bands, aborts on lost session (P-UX-1b)
```

**The baseline run is yours to take.** A dev server answered at
`http://minathena-capital.localhost:8000/login` (HTTP 200) during this session,
but `PF_ATLAS_USER` / `PF_ATLAS_PASSWORD` were not exported, so no session could
be opened and no baseline was run. Take it with:

```bash
source .venv/bin/activate
export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=… PF_ATLAS_PASSWORD=…
python tools/ux_atlas.py --bands 1200
```

Then open three images and confirm the blotter, cases and charts Sections show
content rather than "Loading…": one full page, one band cut from that same page,
and one partial. Two numbers in `manifest.json` are worth a glance on the first
run — `reveal_iterations` (how much scrolling each page needed) and
`reveal_complete` (whether any page ran out of iterations; see F-UX-1b.2).

**One decision is yours**, flagged below rather than taken silently:
F-UX-1b.2 raised `REVEAL_MAX_ITERATIONS` from the prompt's 30 to 60 on measured
evidence. If you want the prompt's number back, it is one constant.

---

## Verify-first

| # | Check | Result | Mode |
|---|-------|--------|------|
| 1 | Clean tree, P-UX-1 in history | porcelain empty; `grep -c "(P-UX-1)$"` = **1** | STOP — passed |
| 2 | Tool anchors | All 12 at the stated lines, ±0: `DEFAULT_VIEWPORT` 136, `STILLNESS_CSS` 139, `SUSPECT_MIN_BYTES` 151, `EXIT_LOGIN_FAILED` 156, `settle` 461, `prepare` 483, `capture_route` 497, `run_scene` 558, `write_manifest` 696, `write_index` 738, `build_parser` 869, `main` 963. File 1,110 lines | STOP — passed |
| 3 | The two screenshot calls | `full_page=True` at **528** and **602**, count = 2 | STOP — passed |
| 4 | Login-bounce handling today | `suspect` set at **545–546** (prompt said 544–546; 544 is the `if`, 545–546 the body — an off-by-one in the prompt, not a mismatch in the tool) | REPORT |
| 5 | Lazy sections | **31 occurrences across 28 files** — the prompt's 31 is the occurrence count, not the file count (`grep -rl … \| wc -l` = 28). All five named exemplars present | STOP if 0 — passed |
| 6 | Document scrolls, not an inner container | `.pf-shell { min-height: 100vh }` at 25; `overflow-y: auto` only at 77 (sidebar nav) and 492 (palette results); **no `height: 100vh` anywhere in the file**. `.pf-auth-shell` at 652 also uses `min-height`. The document is the scroll container, so `full_page` + `window.scrollTo` is the right primitive | STOP — passed, not the August inner-scroll case |
| 7 | Sticky header | `position: sticky` at **300** (`.pf-section__header`) — confirmed the only one | REPORT |
| 8 | Playwright | **1.63.0**, driving Chromium **153.0.8010.12** | REPORT |

No STOP fired. Baseline before changes: `ruff check`, `ruff format --check` and
`pyright tools/ux_atlas.py` all clean, so every finding below is this step's.

---

## Changes

### 2.1 Reveal pass — new `reveal()`, called from `capture_shot()`

`reveal(page)` walks the document to the bottom in steps of
`REVEAL_STEP_RATIO` (0.8) viewports — smaller than one viewport, because
`revealed` fires on intersection and a full-viewport step can jump a short
Section clean over the observer. Each round re-reads `scrollHeight`, because
every swapped-in Section lengthens the page, and settles for network + HTMX
quiet. It stops when the bottom is reached with the height standing still, then
scrolls back to the top, settles again and waits `REVEAL_QUIET_MS` (250 ms).

`settle()` gained a `network_timeout_ms` keyword so the loop can pay 2 s per
iteration instead of the 10 s a navigation gets — a surface holding an open
stream never reaches `networkidle` at all, and the loop pays that cost once per
step rather than once per page.

It returns a `RevealResult` (`iterations`, `scroll_height`, `complete`). Both
capture paths go through one seam, `capture_shot()`, so route captures and
scene shots reveal, measure and band identically.

### 2.2 Truncation tripwire

`png_height()` reads the PNG's own height from its IHDR (bytes 20–23, `struct`
only). `is_truncated()` compares it to the final `scrollHeight`: a PNG standing
exactly at `CHROMIUM_MAX_AXIS_PX` (16,384), or falling more than
`TRUNCATION_TOLERANCE_PX` (2) short, is a cut. A truncated capture is flagged
`truncated` and `suspect`; `png_height` and `scroll_height` reach the manifest
either way. See F-UX-1b.1 — the tripwire did not fire on Chromium 153.

### 2.3 Bands — `--bands PX` (default 0, off; 1200 recommended)

`band_rects()` cuts the page height into `(top, height)` slices, the last one
short. `write_bands()` writes each as a second `full_page` screenshot narrowed
by Playwright's `clip` — same render, no stitching, and the sticky Section
header appears once where it sits rather than repeated atop every slice. Files
land in a `bands/` folder beside the shot (`<area>/bands/…` for a page,
`<area>/partials/bands/…` for a partial, `<area>/scenes/bands/…` for a scene),
named `<stem>-01.png`, `-02.png`, …

The manifest lists them under the capture as `bands`, and records the run's
`--bands` height in its header so an empty list reads as "not asked for" rather
than "attempted and empty". `index.md` folds them under a `<details>` beneath
the full shot, so the contact sheet keeps its one-image-per-route rhythm.

One deliberate rule, documented in the README: **a page that fits inside a
single band gets none** — that band would be a byte-for-byte second copy of the
full PNG under a different name.

### 2.4 Login bounce aborts the run

`capture_route()` now raises `LoginFailedError` on a bounce to `/login` instead
of flagging one capture. The error carries the `Capture` it died on
(`LoginFailedError(…, capture=…)`), so `main` appends it — with
`reason: "session lost — run aborted"` — before breaking out of the route loop.
Scenes are skipped, the manifest and contact sheet are still written with
everything the run already had, and the exit code is `EXIT_LOGIN_FAILED` (3).
Module docstring and the README's exit-code table updated.

### 2.5 Version probe — no-op

There is no `playwright.__version__` probe in `docs/ux/atlas/README.md` (or
anywhere else in the repo) to replace. The correct probe was added to the
README's troubleshooting note instead, since the run's Playwright version is
worth being able to state. See F-UX-1b.4.

### Beyond the four concerns

Two additions, both consequences of F-UX-1b.2 and both documented:
`reveal_complete` on the capture and scene records, and the `suspect` flag when
it is false. Without them an under-revealed page is indistinguishable in the
manifest from a fully revealed one — which is the original bug wearing a
different hat.

---

## Proof

Lint, types, tests — all from a clean baseline:

| Gate | Result |
|---|---|
| `ruff check tools/ux_atlas.py tests/tools/` | All checks passed |
| `ruff format --check tools/ux_atlas.py tests/tools/` | 3 files already formatted |
| `pyright tools/ux_atlas.py` | 0 errors, 0 warnings, 0 informations |
| `pytest tests/tools/test_ux_atlas_reveal.py -q` | **19 passed** in 0.15 s |
| `python tools/ux_atlas.py --bands 1200` with no env | exit **3**, same message as before |

`tests/tools/test_ux_atlas_reveal.py` is the new file (with
`tests/tools/__init__.py`, matching every other test package). No tool tests
existed before — `tests/scripts/` covers `scripts/`, not `tools/`. `tools/` has
no `__init__.py` and does not need one: `tests/` is a package, so pytest puts
the repo root on `sys.path` and `tools.ux_atlas` imports as a namespace package.

The tests are pure-function only, as the prompt asks: the IHDR reader against
PNGs the tests write themselves with `zlib` + `struct` (1×3, 4×9000, missing,
short, non-PNG), the truncation predicate (whole, 2 px tolerance, 3 px cut,
exactly-at-the-cap, unreadable, zero-height), and the band grid — including the
worked example `band_rects(2900, 1200) == [(0,1200), (1200,1200), (2400,500)]`
and a tiling check that the slices leave no gap or overlap and sum to the page.

### Live proof of the browser-bound half

No session was available, but three things could still be driven against the
real Chromium without credentials. Scripts were run from the scratchpad; nothing
was written into the tree.

**The concern itself, on a synthetic page whose lazy blocks swap on
IntersectionObserver — the mechanism htmx's `revealed` is built on — and grow
when they load:**

```
without reveal: loaded=1/7 placeholders=6 height 5760 -> 5760
   with reveal: loaded=7/7 placeholders=0 height 5760 -> 10560 iterations=8 complete=True
```

Six of seven Sections never load without the pass. With it all seven load, and
the page nearly doubles in height — tracked correctly, because the loop
re-reads `scrollHeight` every round rather than trusting the first one.

**Bands, against the live `/login` page and a 4,520 px synthetic page:**

```
login: scroll_height=900  png_height=900  truncated=False bands=[]          (fits one band)
tall:  scroll_height=4520 png_height=4520 truncated=False bands=[…4 files]
       tall-01..03: 1440x1200   tall-04: 1440x920   (1200*3 + 920 = 4520)
```

**A 22,600 px page, past the assumed cap:** the full PNG came back whole at
1440×22,600 and the 19 bands tiled it exactly (18×1200 + 1000). See F-UX-1b.1
and F-UX-1b.2.

**The abort path and the output writers** were driven server-free against a
duck-typed stub page: the bounce raises `LoginFailedError`, the carried capture
holds `reason='session lost — run aborted'` and `suspect=True`, the manifest
entry carries all six new fields, `index.md` renders the `<details>` band
gallery, and a truncated + under-revealed scene lands under "Scene shots
needing a look".

**Not proven:** a live truncation (Chromium 153 would not produce one — see
F-UX-1b.1), and the reveal pass against the actual app behind a session, which
is the operator's baseline run.

---

## Findings

**F-UX-1b.1 — Chromium 153 does not enforce the 16,384 px cap.** A 22,600 px
page screenshotted whole, at 1440×22,600, with no error and no truncation. The
cap the concern is built on is real history but not this build's behaviour, so
the tripwire is presently a no-op. It stays: the cost is one 24-byte read, the
failure it guards is silent, and a headed run, an older Chromium or a different
platform may still cut. Recorded in the README so nobody later reads the
absence of `truncated: true` as proof the check works.

**F-UX-1b.2 — the prompt's 30-iteration cap was too small, and silently so.**
`REVEAL_MAX_ITERATIONS = 30` at a 720 px step walks 21,600 px. The 22,600 px
test page hit the cap and stopped 1,000 px short — and, as written, reported
nothing: `reveal_iterations=30`, everything else looking healthy. On a real Area
page that is the original bug returning, with the fix's own fingerprints on it.
Two changes, both deviations from §2.1 and both deliberate: the guard is now
**60** (~43,000 px at a 900 px viewport), and `reveal()` reports whether it
actually finished, so a capped reveal is flagged `suspect` with
`reveal_complete: false` rather than passed off as whole. The same page now
completes in 32 iterations. The prompt's 30 was sized for the 16,384 px cap,
which F-UX-1b.1 shows is not enforced — so pages can legitimately run past it.
**Operator call:** the constant is one line if you want 30 back.

**F-UX-1b.3 — bands survive past the cap.** An open question when the work
started, since a band is still a `full_page` render under the hood. Measured:
the 22,600 px page produced all 19 bands, the last correctly 1440×1000. So
bands remain a usable fallback for a page the full shot cannot cover, which is
the premise of the truncation reason string.

**F-UX-1b.4 — §2.5 was a no-op.** No `playwright.__version__` probe exists in
`docs/ux/atlas/README.md`, or anywhere in the repo. The correct probe
(`importlib.metadata.version('playwright')`) was added to the README's
troubleshooting note rather than replacing something.

**F-UX-1b.5 — two verify-first numbers were slightly off, neither material.**
Check 5's "31 files" is 31 *occurrences* across 28 files. Check 4's "lines
544–546" is 545–546 for the body (544 is the `if`). Both anchors were found
where expected.

---

## Open questions

1. **Run duration.** The reveal pass adds a scroll walk per route, each step
   waiting up to 2 s for `networkidle` plus HTMX quiet. On ~60 routes this is
   the difference between a fast run and a slow one, and it cannot be estimated
   without a session. The first baseline run answers it. If it is painful, the
   lever is `REVEAL_NETWORK_IDLE_TIMEOUT_MS` (2 s), not the step ratio.
2. **Whether 1200 px is the right band height.** Chosen from the ~1,568 px
   downscale, not measured against a real Area page. Worth revisiting once
   there are real bands to read.
3. **Assistants chat.** It holds an open stream, so it never reaches
   `networkidle` and will pay the full 2 s on every reveal step. If its page is
   long, it is the slowest route in the run by some distance.
4. **`reveal_complete` on real pages.** If the baseline shows Area pages
   hitting even the 60-iteration guard, the guard is the wrong shape and the
   loop should bound on distance travelled rather than rounds — but that is a
   change to make against evidence, not ahead of it.

---

## Files touched

| File | Change |
|---|---|
| `tools/ux_atlas.py` | The four fixes; 1,110 → 1,549 lines |
| `docs/ux/atlas/README.md` | Reveal, bands, truncation and manifest-schema sections; `--bands` flag; exit codes; version probe |
| `docs/ux/README.md` | Reveal pass and bands in the atlas section; `--bands 1200` in the command |
| `tests/tools/test_ux_atlas_reveal.py` | **New** — 19 pure-function tests |
| `tests/tools/__init__.py` | **New** — test package marker, as every other test package has |
| `docs/reports/P-UX-1b-report.md` | **New** — this report |

Nothing outside this list changed, and no atlas output was written into the
tree.
