<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0t — Atlas on the new shell: walk every section by fragment

**Strand:** UX A-0 · **Date:** 2026-09-22 · **Runs after:** P-UX-A0b
(`9581ed0`) · **Scope:** `tools/ux_atlas.py`, its tests, its scenes file
and its README. No `web/` change.

---

## OPERATOR ACTION REQUIRED

### 1. Gate 4 is not run — the atlas has no session

The dry run could not be executed. The dev database reachable from this
session holds **no tenant and no user rows**, so no login is possible and
there is nothing to photograph:

```
$ portfoliflow status
  Tenants  Total 0   Sentinel exists  no
  Users    Total 0   Sentinel owner   <none>

$ psql -d portfoliflow_dev -c 'select subdomain, name from tenants'
 (0 rows)
$ psql -d portfoliflow_dev -c 'select count(*) from users'   -> 0
$ psql -d portfoliflow_dev -c 'select count(*) from investments' -> 0
```

One container (`portfoliflow-postgres`, healthy, `127.0.0.1:5432`), one
database (`portfoliflow_dev`), named by both `DATABASE_URL` and
`DATABASE_URL_SUPERUSER`. Nothing was written to it: no bootstrap, no
`create-user`, no reset. The dev web server **was** started and is still
running (`portfoliflow-web`, uvicorn on 127.0.0.1:8000, `/login` → 200).

To run gate 4, with a session available:

```bash
source .venv/bin/activate
export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=…  PF_ATLAS_PASSWORD=…
python tools/ux_atlas.py --bands 1200
```

Expected, and what to check against §Gate 4 below: `sections_total` in
`manifest.json`, the per-area section list, `≥ 1` band per section, and a
zero `loading_placeholders` on every section.

**That run is the post-A0b evidence and the new baseline for A0c…A0s.**
Zip its `bands/` folders for DC-UX-A once it exists:

```bash
RUN=docs/ux/atlas/$(date +%F)          # or the suffixed folder the run prints
zip -r ~/atlas-bands-$(date +%F).zip "$RUN"/*/bands "$RUN"/*/scenes
```

### 2. Verify-first check 1 failed and was waived

`git status --porcelain` was **not** empty at start:
`docs/reports/P-UX-A0b-report.md` carried an uncommitted gate-5 addendum
to the previous prompt's report. The table says STOP; the operator was
asked and chose to proceed, since the file is the prior prompt's own
report and is outside this prompt's scope. It is untouched here and
appears in `git diff --name-only` as a pre-existing delta.

### 3. Commit

```bash
git add tools/ux_atlas.py tests/tools/test_ux_atlas_reveal.py docs/ux/atlas-scenes.json \
        docs/ux/atlas/README.md docs/reports/P-UX-A0t-report.md
git commit -m "chore(ux): atlas walks sections by fragment on the one-section shell, per-section shots and scenes (UX A-0, P-UX-A0t)"
```

`docs/reports/P-UX-A0b-report.md` is deliberately left out of that `git
add` — it is the previous prompt's report and its own to commit.

---

## Verify-first

| # | Check | Expected | Found | Verdict |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | `M docs/reports/P-UX-A0b-report.md` | ❌ **waived** — see above |
| 2 | `git log --oneline -1` | ends `(UX A-0, P-UX-A0b)` | `9581ed0 feat(shell): …(UX A-0, P-UX-A0b)` | ✅ |
| 3 | `wc -l tools/ux_atlas.py` | `2026` | `2026` | ✅ |
| 4 | ten `^def` anchors | 830, 891, 938, 993, 1206, 1258, 1357, 1430, 1518, 1615 | identical | ✅ |
| 5 | `UNFIRED_LOADER_SELECTOR` / `MAX_REVEAL_ROUNDS` | 272, 281 | 272, 281 | ✅ |
| 6 | `grep -n 'arrives on'` | one hit near 281 | **three**: 82, 278, 943 | ⚠️ reported, continued |
| 7 | `grep -c 'data-pf-section'` | ≥ 1 each | `shell.js` 7, `areas/_section.html` 1 | ✅ |
| 8 | `hashchange` in `shell.js` | one listener | one, line 65 | ✅ |
| 9 | `docs/ux/atlas-scenes.json` | one scene, `transactions-flow-chooser`, `[{"wait": "#tx-composer-host"}]` | exactly that | ✅ |
| 10 | `grep -n '^def test' … \| wc -l` | the count | **0** — every test is a method inside a class, so `^def test` matches nothing; the file holds **26** tests. `from tools.ux_atlas import (` at line 33 ✅ | ⚠️ reported |
| 11 | `python -c 'import playwright; print(playwright.__version__)'` | prints a version | `AttributeError: module 'playwright' has no attribute '__version__'` — the package *is* installed; it simply carries no `__version__`. `importlib.metadata.version('playwright')` → **1.63.0**, and `from playwright.sync_api import sync_playwright` imports | ⚠️ reported — the check's intent (runtime present) is met |

Check 6's anchor at 278 matched, so the count mismatch was reported and
the run continued per the prompt's own rule. The two extra hits were the
module docstring (82) and `drive_loaders`' docstring (943); all three now
read `intersect`, and only one says "arrives on" — see Gate 4 below.

---

## Diff

| File | Before | After | Δ |
|---|---|---|---|
| `tools/ux_atlas.py` | 2,026 | 2,403 | +377 |
| `tests/tools/test_ux_atlas_reveal.py` | 233 | 558 | +325 |
| `docs/ux/atlas/README.md` | 360 | 368 | +8 |
| `docs/ux/atlas-scenes.json` | 12 | 31 | +19 |

`git diff --stat`: 811 insertions, 64 deletions across the four files
plus the pre-existing `P-UX-A0b-report.md` delta. **`web/` is absent.**

### What landed in `tools/ux_atlas.py`

**Section discovery and switching** (§2.1) — three helpers after
`scroll_height_of`, plus the selectors they read:

| Name | Line | What it does |
|---|---|---|
| `SECTION_SELECTOR` | 190 | `#shell-main [data-pf-section]` |
| `VISIBLE_SECTION_SELECTOR` | 194 | the same, `:not([hidden])` |
| `HIDDEN_SECTION_SELECTOR` | 199 | `[data-pf-section][hidden]` |
| `SECTION_SWITCH_TIMEOUT_MS` | 204 | 5 s |
| `SectionSwitchError` | 501 | typed, so one bad fragment is a finding rather than a crash |
| `section_slugs` | 911 | the slug list in DOM order; `[]` for a surface the shell does not section |
| `visible_section` | 938 | the one slug without `hidden` |
| `switch_section` | 959 | sets `location.hash`, waits for the shell to act, then `settle` |

The Section list is read **off the DOM**, never imported from
`web.shell`: the atlas observes the running product, so a Section added
to the catalogue appears in the next run without touching the tool —
the same rule route selection already follows.

`switch_section` drives the shell's own path (`location.hash` →
`hashchange` → `web/static/js/shell.js`) rather than clearing the
`hidden` attribute by hand. That is deliberate: a fragment that does not
resolve then shows up as a finding, where unhiding the element directly
would have photographed a surface no reader can reach.

**Loader driving respects `hidden`** (§2.2) — `unfired_loaders` now asks
the page for two flags per loader, `el.closest('[data-pf-section][hidden]')
=== null` and the existing visibility test, and drops the ones in a
hidden Section **entirely** rather than counting them as invisible. Both
halves matter: the driver never spends a round scrolling to something
that can never intersect, and `loaders_hidden` keeps its old meaning —
an unfired loader the *shown* Section hides behind a collapsed
`<details>` — instead of becoming "every other Section on the page".

**Per-section capture** (§2.3) — `capture_route` walks
`section_slugs(page)`, switching and shooting each; `Capture` gains
`sections: list[tuple[str, ShotMeta]]`, `ShotMeta` gains the `file` it
wrote. Shots land at `<area>/<route>--<section>.png`, bands at
`<area>/bands/<route>--<section>-NN.png`. A route with no Sections is
untouched: one shot, bands as before, `sections == []`.

**Scenes** (§2.4) — `run_scene` switches to `scene["section"]` after
`start` loads and before the steps run; a section that never comes into
view is a failed scene, not an aborted run.

**Manifest and index** (§2.5) — `manifest_entry` re-shapes the Section
pairs into named objects (`section`, `file`, `bands` count, `truncated`,
`loading_placeholders`); the manifest gains `sections_total`;
`write_index` emits one `####` heading per Section with its own shot and
its own bands, e.g. `Transactions › Blotter — /transactions#blotter`.

### Three judgement calls worth reviewing

1. **`switch_section` then `capture_shot`, not the four calls listed.**
   §2.3 names `switch_section`, `drive_loaders`, `await_charts`,
   `count_loading_placeholders`, then `capture_shot`. `capture_shot`
   already *is* those three in that order (via `reveal`), and calling
   them first would double every section's wall-clock for an identical
   result. Read as describing the sequence, not prescribing five calls.

2. **`await_charts` is scoped to the shown Section** (not asked for).
   A Section already walked keeps its drawn figures in the DOM behind
   `hidden`, so an unscoped probe would make each Section's
   `charts_total` the running sum of the ones before it. The probe now
   roots at `VISIBLE_SECTION_SELECTOR`, falling back to `document` on a
   page the shell does not section — byte-identical behaviour there.

3. **The suspect reason names its Section.** `unfired lazy loaders (3)`
   on a four-Section Area page is a question, not a finding, so the
   ladder (extracted as the pure `shot_reason`) runs per Section and the
   reason reads `charts: unfired lazy loaders (3)`. Route-level fields
   carry the fold of the Sections (`aggregate_sections`): counters sum,
   `truncated` / `reveal_complete` take the worst case, heights take the
   tallest, `file` names the first Section's shot.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/tools -q` | ✅ **63 passed** in 0.11 s (was 26) — DB-free |
| 2 | `ruff check` / `ruff format --check`; `pyright` advisory | ✅ all checks passed / 3 files already formatted; **pyright 0 errors, 0 warnings** |
| 3 | `git diff --name-only` | ✅ the four files + this report; **`web/` absent** (one pre-existing delta, §OPERATOR ACTION 2) |
| 4 | Dry run against the dev server | ❌ **not run** — no session; see §OPERATOR ACTION 1. The `arrives on` half of the gate **is** met: `grep -c 'arrives on' tools/ux_atlas.py` → **1**, reading "a section body that arrives on `intersect`" |
| 5 | No `confirm(` / `alert(` / `hx-confirm` added; no `type="number"` touched | ✅ trivially — `git diff -U0 \| grep -c '^+.*\(confirm(\|alert(\|hx-confirm\|type="number"\)'` → **0**. This prompt adds no markup at all |

### Gate 1 — what the 37 new tests cover

Pure functions only, no browser, matching the file's existing structure
(classes of methods) and its self-written PNG fixtures:

| Class | Covers |
|---|---|
| `TestSectionShotStem` | `<route>--<section>` naming under the `route_slug` rules, including `/` → `index--overview` never colliding with `front-office--overview` |
| `TestSectionHeading` | the contact sheet's heading, and that it ends in the fragment a reader navigates by |
| `TestManifestEntry` | the manifest shape for a two-Section route, DOM order, band **count** not list, and an empty list for a route without Sections |
| `TestWriteIndexSections` | `write_index` over a tmp run root: both headings present, each Section's own shot, a Section's bands under *its* heading only, the header's section count, and the unsectioned fallback |
| `TestScenesCarryASection` | the loader accepts a scene with `section` and one without, and the `--area` filter is unaffected |
| `TestShippedScenesFile` | the three Transactions scenes in the shipped file each name their Section — a scene that lost the key would silently photograph the landing Section three times |

### Gate 4 — the section list, from the catalogue

The dry run could not produce this list, so here is the count it should
match, read from `web/shell.py`'s `_SECTIONS_BY_AREA` and confirmed
against the `areas/_section.html` includes in each area body template
(they agree, one include per catalogue entry):

| Area | Route | Sections | n |
|---|---|---|---|
| Front Office | `/front-office` | overview, charts, statistics, portfolio-optimizer | 4 |
| Back Office | `/back-office` | saa, benchmarks-attribution, limits | 3 |
| Assistants | `/assistants` | shirley, report-scraper, providers-credentials | 3 |
| Planning Desk | `/planning-desk` | cash-flow-planning, scenario-analysis | 2 |
| Investor Communication | `/investor-communication` | portfolio-review | 1 |
| Watch Desk | `/watch-desk` | briefing, journal, calibration | 3 |
| Cases | `/cases` | open-cases, recently-closed, archive | 3 |
| Transactions | `/transactions` | new, **blotter** (landing), history | 3 |
| Admin | `/admin` | data-import, market-data, providers-credentials, users, investments | 5 |
| | | **total** | **27** |

**27, as the prompt states.** One prediction to check against the real
run: the atlas also captures **`/`**, which redirects to `/front-office`
and therefore photographs those four Sections a second time under
`index--*`. `sections_total` should read **31** across **ten** captured
routes, not 27 across nine — 27 is the catalogue, 31 is the run. If the
manifest says 27, `/` stopped redirecting.

`/investments`, `/investments/new`, the four super-admin routes and every
partial carry no `#shell-main [data-pf-section]`, so they keep the
P-UX-1c single-shot path unchanged.

---

## Open questions

1. **Which Sections show a non-zero placeholder count?** Unanswered —
   gate 4 did not run. This is the question the baseline exists to
   answer, and a non-zero count is a **product** finding (a loader that
   does not fire when its Section becomes visible), not an atlas fault.
   The two Sections most worth watching are `front-office#charts` (its
   per-investment `<article>` loaders are the nested case `drive_loaders`
   was written for) and `assistants#shirley` (an open stream never
   reaches `networkidle`).

2. **Does every Section reach ≥ 1 band?** Also unanswered. Note the
   shape of the answer: `write_bands` returns **no** bands for a page
   that fits inside a single band, by design — a lone band would be the
   full PNG under another name. One Section per view is a much shorter
   document than the old long-scroll Area page, so on a lightly-populated
   tenant several Sections will legitimately come back with **zero**
   bands at `--bands 1200`. If the gate is meant to assert "the shot was
   cut into bands", it may need restating as "≥ 1 band **or** a
   single-band page"; on the operator's populated tenant this may not
   arise at all.

3. **Is the `/` duplicate wanted?** It doubles Front Office's four
   captures. Cheap to drop (`PATH_SKIPS` would take `("/", "redirect")`)
   and cheap to keep. Kept, because the atlas's stated contract is to
   account for the whole route table rather than silently narrowing it,
   and because a redirect that lands somewhere unexpected is exactly the
   kind of thing a baseline should catch. Flagged rather than decided.

4. **Section headings are built from slugs, not catalogue titles.**
   `humanise_slug` turns `cash-flow-planning` into "Cash Flow Planning",
   which reads well, but `saa` becomes "Saa" and `portfolio-optimizer`
   becomes "Portfolio Optimizer" where the catalogue says "Strategic
   Asset Allocation" and "Portfolio Analysis". Importing `web.shell`
   would fix the prose and break the rule that the atlas observes the
   product rather than linking against it. Left as slugs; say if the
   contact sheet should read the titles out of the DOM's `h2` instead —
   that would stay observational and is a small change.

5. **The dev database is empty.** Unrelated to this prompt, but it
   blocks the baseline: `portfoliflow status` reports 0 tenants and 0
   users, and `investments` is empty, while the previous atlas run
   (`docs/ux/atlas/2026-09-22-2`, commit `e2fd660`) clearly had data.
   Nothing here wrote to it.
