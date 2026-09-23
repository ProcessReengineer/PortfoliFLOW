<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0s — Standards document, atlas scenes, test hygiene · end of A-0

**Strand:** UX A-0 · **Date:** 2026-09-23 · **Runs after:** P-UX-A0e
(`8fb91a8`) and the post-A0e atlas run (`docs/ux/atlas/2026-09-23-4/`,
`git_head 8fb91a8`, all five scenes `ok`) · **Scope:**
`docs/ux/ui-standards.md` (new), `docs/ux/atlas-scenes.json`,
`docs/ux/inventory/*`, `base.html`, `super_admin/base.html`,
`test_cases_area.py`, `test_transactions_area.py`,
`test_super_admin_routes.py`, this report. **No `.css`, no `.js`, no route
logic.**

A-0 closes. The standards document the later strands build against is
written; the atlas can now photograph the Cases detail, its four composers,
the three Transactions composers and the wizard's first step; the two
`data-section` dot pins are retired; Shirley's rail is off the super-admin
surface.

---

## OPERATOR ACTION REQUIRED

### 0. Two steps are deferred out of this session

By operator instruction (mid-session, 2026-09-23): **the full suite (§5.1)
is the next prompt's**, not this one's. The atlas end run (§5.4) needs
credentials this session does not hold. Gates 1–5 are settled here; gate 6
and the second half of gate 4 are not.

### 1. The atlas end run (§5.4) — yours

The run needs a live server and the three `PF_ATLAS_*` credentials, which
are environment-only and not in this session.

```bash
portfoliflow bootstrap          # the test fixtures TRUNCATE on teardown,
                                # and the stopped full-suite run left the
                                # dev DB empty — re-bootstrap first
portfoliflow-web &
export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=… PF_ATLAS_PASSWORD=…
python tools/ux_atlas.py --bands 1200
```

Compare against **`docs/ux/atlas/2026-09-23-4/`**. What must differ: the
nine new scenes appear, and the super-admin routes lose the 44 px rail.
What must not: everything else. Gate 4's second half — every new scene
`ok: true` in the end-run manifest — is settled by that run.

**The Cases scenes write to the book.** Each of the five creates a case
titled "Atlas seed case"; five per run, by the prompt's design (the tool
does not chain scenes, so each repeats the seed). They are ordinary open
cases and can be closed or left.

### 2. Commit

Everything is staged. The commit is the operator's.

```bash
git commit -m "docs(ux): ui-standards.md, atlas scenes for Cases and the Transactions composers, dot-test retired, rail off the super-admin surface — end of A-0 (UX A-0, P-UX-A0s)"
```

---

## Verify-first

| # | Check | Result |
|---|---|---|
| 1 | `git status --porcelain` | ✅ empty |
| 2 | `git log --oneline -1` | ✅ `8fb91a8 … (UX A-0, P-UX-A0e)` |
| 3 | neither `ui-standards.md` nor this report exists | ✅ both absent |
| 4 | scene count | ✅ 5 — chooser, blotter, history, shirley-docked, shirley-stage |
| 5 | `data-section=` in `tests/web/*.py`; line 496 | ✅ **2** (≥ 1); `test_cases_area.py:496` exactly |
| 6 | `super_admin/base.html:13`; one `<aside class="pf-side"` | ✅ both exact |
| 7 | `ls docs/ux/` | ✅ `README.md atlas atlas-scenes.json inventory` — no `checklists/` |
| 8 | file count ~1,451; `type="number"` 38 | ⚠️ **drift on the file count**; `type="number"` **38** exactly |

**Check 8.** `find . -type f -not -path './.git/*'` returns **41,013** here,
because `.venv/` is inside the working tree — it is not the command the
kickoff's number came from. The tracked-file count, `git ls-files`, is
**1,530**; with untracked-but-not-ignored files, 1,531. Against a kickoff
baseline of ~1,451 that is +79, which is what A-0 added (the component
sheet, three `.js` files, the vendored Lucide set, seven new test modules,
nine reports). Anchor intact, command corrected — §5.3 states both forms.

`python` is not on `PATH` in this environment; every command in this report
ran under `source .venv/bin/activate`.

The prompt's §1 input is at `~/DC-UX-D-bundle/DC-UX-D_design-parameters.md`,
not `~/DC-UX-D/` — that directory holds the mock sources (`shared.css` and
the five `mock0N.src.html`). Same file, record v4.

---

## §1 — `docs/ux/ui-standards.md`

398 lines, eight sections in the prompt's order. Written from the record's
§2 and §3 and from the product as it stands, not from the mocks.

* **§1 Where things are** — 17 rows, layer → file: token source and
  generator, `layout.css`, `pf_components.css`, chart theme, icons, the
  section catalogue, the shell and view frames, the three `.js` files.
* **§2 Rules** — record §2.1–§2.12, one rule per table row, 76 rows, each
  with its implementation or "**not yet** — strand X". Twelve tables.
* **§3 Tokens** — sixteen `--ui-*` groups with the JSON key path and one
  line of intent; no values restated. The two `--chart-*` names a component
  may read (`plot-area`, `text`) and the `--pf-*` block marked parked.
* **§4 Component catalogue** — 34 rows covering all 58 declared families
  with their modifiers, the record rule each serves, and a **users** column;
  `pf-state--dormant` marked as the Phase-A addition; the two `color-mix`
  derivations named; an explicit "still to build" list.
* **§5 Deviations** — the eleven the reports accumulated, each with its why.
* **§6 Shortcuts** — the two that exist, with predicate, handler and the
  place each is written, by file and line.
* **§7 How to add a view** — the six-step A-1 recipe.
* **§8 Appendix** — the 125 % checklist (§4 of the prompt).

**One thing the document adds that the prompt did not ask for**: the
**users** column in §4. Measuring it was the only way to write the section
honestly, and the answer is the useful part — of 58 declared families,
`pf-btn` has 36 uses across 8 templates and five more families have one use
each. The vocabulary landed whole in A0d; almost nothing is on it yet. A
later strand reading §4 needs that number more than it needs the rule text.

---

## §2 — Atlas scenes

`docs/ux/atlas-scenes.json`: **5 → 14**.

| Name | Start | Section | Steps | Purpose |
|---|---|---|---|---|
| `cases-detail` | `/cases` | — | 8 | seeds its own case, opens the detail |
| `cases-composer-note` | `/cases` | — | 10 | seed + "Add note" |
| `cases-composer-pin` | `/cases` | — | 10 | seed + "Pin document" |
| `cases-composer-decision` | `/cases` | — | 10 | seed + "Record decision" |
| `cases-composer-close` | `/cases` | — | 10 | seed + "Close case…" |
| `transactions-composer-order` | `/transactions` | `new` | 3 | U-BUY / U-SELL |
| `transactions-composer-commitment` | `/transactions` | `new` | 3 | R-COMMIT |
| `transactions-composer-secondary` | `/transactions` | `new` | 3 | R-SEC-BUY |
| `transactions-wizard-step-1` | `/transactions` | `new` | 4 | U-NEW, step 1 |

**The detail exposes four composers, so there are four composer scenes** —
note, pin, decision, close. The two remaining rail actions ("Capture
scenario", "Consult Shirley") are links out to other Areas, not composers,
and are already photographed where they land.

**`"skip"` is not honoured.** `run_scene` (`tools/ux_atlas.py:1669`) reads
`name`, `session`, `area`, `shot`, `start`, `section` and `steps`, and
ignores every other key — a scene carrying `"skip": "…"` would be walked
regardless. Nothing needed skipping, so no scene was left out; the finding
is recorded because the prompt asked for it, and adding the key would be an
edit to `tools/ux_atlas.py`, outside gate 3.

**The empty DB satisfies "New case".** The form takes a title (required) and
a description (optional) and nothing else — no investment, no finding, no
link. The seed is therefore self-sufficient, which is why each composer
scene repeats it rather than depending on a scene before it.

Every selector was checked against the templates before the file was
written: 26 of the 29 resolve statically, and the three that do not are the
runtime text the scene itself creates ("Atlas seed case") and two attribute
selectors whose attributes are present (`type="submit"`,
`name="title"`) — an artefact of the probe, not of the scene.

### Deviations from the prompt's scene table

1. **The Transactions scenes use `"start": "/transactions"` plus
   `"section": "new"`**, not `"start": "/transactions#new"`. Same
   destination; `section` is the tool's own mechanism for it
   (`switch_section` drives the fragment and *waits for the section to come
   into view*, raising a typed `SectionSwitchError` if it never does), and
   it is what the sibling `transactions-flow-chooser` scene already uses. A
   raw fragment in `start` would reach the same view without that wait.
2. **`transactions-composer-secondary` opens the secondary *buy*.** The
   chooser has two secondary flows — R-SEC-BUY and R-SEC-SELL — and the
   prompt names one scene. Buy is the one A1b's before image needs; the sale
   composer is the richer surface and is a candidate for its own scene when
   A-1 gets to it.
3. **Each Cases scene waits for `#cases-open-list` first.** `run_scene`'s
   `prepare()` only injects the stillness CSS — the reveal pass runs inside
   `capture_shot`, *after* the steps. The open-cases body loads on
   `intersect once` and is above the fold at 1440×900, so it arrives on its
   own; the explicit wait makes the post-create out-of-band refresh's target
   a precondition rather than a hope.

---

## §3 — Hygiene

### 1. The retired dots — two tests, not one

`grep -rn 'data-section=' tests/` returned **two** sites, both the same
assertion and both red for the same reason:

| File | Test | Now |
|---|---|---|
| `tests/web/test_cases_area.py:496` | `test_cases_page_renders_three_sections` | pins `data-pf-section="<slug>"` (the body `shell.js` shows or hides) and `data-pf-section-link="<slug>"` (the sidebar's second level) |
| `tests/web/test_transactions_area.py:203` | `test_transactions_page_renders_three_sections` | the same pair, plus the section title it already checked |

Both names still tell the truth — the pages do render three Sections — so
both are kept. Each docstring now says what replaced the dots and why, so
the next reader does not re-derive it. `grep -rn 'data-section=' tests/`
now returns nothing.

`tests/web/test_section_navigation.py` and `test_shell_sections.py` already
pin the *absence* of `pf-section-indicator`; they needed no change and are
the guard that the dots do not come back.

### 2. The rail off the super-admin surface (A0e flag 8)

`base.html` gained `{% block shirley %}` around **both** the `#pf-stage`
block and the `<aside class="pf-side">` column;
`super_admin/base.html` overrides it empty, with a comment citing ADR-0064
§1 and the flag.

Wrapping both was necessary, not tidy: the block carries the stage as well,
and an override that dropped only the column would leave Ctrl J setting
`data-shirley="docked"` on a shell whose third grid column then widens to
380 px around nothing.

**`shirley.js` tolerates the missing hosts — verified, not assumed.**
`armDock` null-checks `#dock-chat-host`; `relocate` returns early without
`#pf-chat`; `setState` null-checks `.pf-shell` and `#pf-stage`;
`labelStageToggle` null-checks its button; the focus calls null-check
`#chat-input`. The file is unchanged.

**New test** — `test_super_admin_surface_carries_no_shirley_rail` in
`tests/web/test_super_admin_routes.py`, DB-bound, over both super-admin
pages: no `id="pf-side"`, no `id="dock-chat-host"`, no `id="pf-stage"`,
while `id="shell-main"` and the shell itself still render.

### 3. The one-line CSS fix — not applicable

§3.3 applies only to a deviation the operator's browser check (a)–(i)
produced. No result from that check reached this session, so no `.css` was
touched and gate 3's "no `.css`" holds unconditionally. If (b) or (i) did
turn up a wrapped composer row, it is A0e flag 5 — `chat-embed` styling in
a 380 px column — which the A0e report already names as the Assistants
strand's, not a one-liner.

---

## §4 — The 125 % checklist

Ships as §8 of `ui-standards.md`, where the operator will look for it,
rather than as a loose file (check 7 confirms `docs/ux/` has no
`checklists/` directory and none was created).

Nine Area rows × seven checks, plus four accessible-mode rows, each with a
tick box and a "found" column. It opens with the two setup lines: 125 %
zoom at 1280 px with Shirley docked, then the console one-liner that sets
`data-a11y` — because nothing writes that attribute yet (§5.9 of the
standards document).

---

## §Strand-end measurements

### 1. Full suite — `pytest -q`

⏳ **Deferred to the next prompt, by operator instruction** (2026-09-23,
mid-session). It was launched at 14:02 and stopped at ~14:10, about 2 % in,
with no failure recorded; nothing of it is reported here, because a partial
run is not a result.

What it still has to settle, carried forward verbatim:

* **A0d2's deferred gate 7** — the one open full-suite obligation of A-0.
* **The two rewritten dot tests and the new super-admin test** in the whole
  suite's company rather than in a targeted selection. Gate 1 ran all three
  files green (218 passed), so what remains is cross-module interference,
  not these tests' own correctness.
* **Any stale pin on the `{% block shirley %}` wrap.** The block changes no
  rendered byte on a tenant surface — the same elements in the same order —
  and `tests/regression/test_shirley_shell_element.py`, which reads
  `base.html` as text and counts the four shell ids, ran green in gate 1.
  The exposure is a super-admin test elsewhere that assumed the rail.

Per `docs/reports/full-suite-2026-09-17-report.md` and the project's own
record the run is ~2h20m, so it is a session of its own: launch it detached
(`setsid` + a PID waiter), then triage each failure as pre-existing /
this strand's / a stale pin.

### 2. `python tools/ux_inventory.py`

Re-run and committed: **218 templates, 1,203 element rows, 169 routes, 44
JS rows from 44 calls** (dynamic route pass via `web.main.create_app`).

The `transactions` row, per the prompt:

| Area | templates | element rows | headings | buttons | HTMX triggers | links | form labels | pills | tiles | routes | js calls | distinct nouns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| transactions | 31 | 212 | 4 | 47 | 49 | 2 | 69 | 7 | 0 | 23 | 0 | 15 |

It is the largest Area in the book by element rows (212 against the
watch desk's 129 and admin's 115) and by form labels (69 against
investments' 32) — which is the case for A-1 going first, stated in
numbers.

Its flag row is the other half of that case:

| Area | abbrev | duplicate-label | dynamic | from:aria-label | from:title | hidden | icon-only | identifier-leak | identifier-leak-loose | inline-html | internal-name | no-accessible-name | synonym-candidate | unlinked |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| transactions | 13 | 43 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 40 | 0 |

43 duplicate labels and 40 synonym candidates across five composers and a
wizard — the vocabulary work A-1 owns.

### 3. Counts

Each with the command that produced it, because three of the prompt's
baselines are three different ways of counting the same thing.

| Measure | Command | Now | Baseline |
|---|---|---:|---|
| Files, tracked | `git ls-files \| wc -l` | 1,530 | ~1,451 at kickoff (+79 = A-0's additions) |
| Files, `find` | `find . -type f -not -path './.git/*' \| wc -l` | 41,013 | includes `.venv/` — not the kickoff's command |
| `btn` files with uses | `grep -rlo 'class="[^"]*\bbtn\b' web/templates \| wc -l` | **74** | 170 at kickoff → 74 today |
| `btn` sites | `grep -ro 'class="[^"]*\bbtn\b' web/templates \| wc -l` | 176 | record §3.1 says ×165 |
| `tx-btn` sites | `grep -ro 'class="[^"]*\btx-btn\b' …` | **40** | 40 ✅ |
| `pf-btn` sites | `grep -ro 'class="[^"]*\bpf-btn\b' …` | **20** | 14 in Cases + 6 added by A0e |
| `pf-btn` raw substring | `grep -ro 'pf-btn' …` | **36** | 36 ✅ — modifiers counted separately |
| `pf-btn` sites in Cases | as above, Cases templates only | **14** | 14 ✅ |
| `pf-dc-btn` sites | `grep -ro 'class="[^"]*\bpf-dc-btn\b' …` | **9** | 9 ✅ |
| `type="number"` | `grep -ro 'type="number"' web/templates \| wc -l` | **38** | 38 ✅ |
| … of those in Transactions | same, `_partials/transactions` | **18** | 18 ✅ |
| `--ui-*` in `theme.css` | `grep -c -- '--ui-' …` | 249 declarations | file is 421 lines ✅ |
| `pf_components.css` | `wc -l` | **379** | — |

The prompt's `pf-btn*` "36 — A0e added 22 to the 14" reconciles as: 14
element sites in Cases plus 6 added by A0e is **20 sites**; counting the
`--primary` / `--quiet` / `--icon` modifiers as separate substring matches
gives **36**. Both numbers are above, with their commands, so the next
strand can pick the one it means.

`btn` is the one that matters for the migration: 170 → 74 files. That fall
is not this strand's — it is the cumulative effect of A0c/A0d/A0d2 — but
A-0 is where it should be recorded, because A-1 measures against it.

### 4. Atlas end run

⏳ **Operator** — §OPERATOR ACTION REQUIRED, item 1. Credentials are
environment-only and absent from this session.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/regression tests/web/test_cases_area.py tests/web/test_transactions_area.py tests/web/test_super_admin_routes.py -q` | ✅ **218 passed** in 264s — 496 fixed, its twin fixed, the new super-admin test present |
| 2 | `ruff check` · `ruff format --check` | ✅ clean / ✅ 940 files formatted (the two rewritten tests were reformatted before the gate) |
| 3 | `git diff --name-only` | ✅ exactly the nine paths plus the new `ui-standards.md` and this report; **no `.css`, no `.js`** |
| 4 | scene count ≥ 10 | ✅ **14**; every new scene's selectors resolve statically. `ok: true` in the end-run manifest is the operator's run |
| 5 | `wc -l docs/ux/ui-standards.md` | ✅ **398** (band 250–400) |
| 6 | full suite | ⏳ **deferred to the next prompt** by operator instruction — §Strand-end measurements 1 |

**Gate 3 — the changed set.** `docs/ux/ui-standards.md` (new),
`docs/reports/P-UX-A0s-report.md` (new), `docs/ux/atlas-scenes.json`,
`docs/ux/inventory/{elements.csv,routes.csv,summary.md}`,
`web/templates/base.html`, `web/templates/super_admin/base.html`,
`tests/web/test_cases_area.py`, `tests/web/test_transactions_area.py`,
`tests/web/test_super_admin_routes.py`.

`test_transactions_area.py` is the one file beyond the prompt's list. The
prompt asked for the `data-section=` grep and for anything else it found to
get the same treatment; it found that file.

---

## Deliberate deviations

1. **Two test files carry the dot fix, not one** — the grep the prompt
   ordered found a second site, and the prompt says it gets the same
   treatment.
2. **The Transactions scenes reach `#new` through `"section"`, not through
   a fragment in `"start"`** — §2, deviation 1.
3. **`transactions-composer-secondary` is the secondary *buy*** — §2,
   deviation 2.
4. **The 125 % checklist ships inside `ui-standards.md`**, as its §8 — the
   prompt's §1 lists it as the appendix, and check 7 shows no
   `checklists/` convention to join.
5. **§4 of the standards document carries a "users" column** the prompt did
   not ask for — §1 above says why.
6. **No `.css` was touched** — §3.3's precondition (a browser-check result)
   never arrived.

---

## §7 Flags

The §1.5 deviation list, minus what this prompt closed. Two closed:
**Cases' scoped `pf-btn`** stays open (A-3), but **the statusbar shortcut
hint** and **the `/chat/dock` lazy load** are settled decisions rather than
open questions and are recorded as such in §5 of the standards document
rather than carried here. What remains open, for the strand that owns it:

1. **No container queries.** `.pf-main` declares no container; the record's
   twelve `@container view` rules are waiting. The sticky view head must be
   re-checked when the declaration lands — `container-type: inline-size`
   establishes a containment context that can change how `position: sticky`
   resolves. **Strand A-5.**
2. **Cases' scoped `pf-btn`.** `components/cases.css` out-specifies the
   global family for 14 sites. **Strand A-3.**
3. **`prefers-contrast: more` reaches the default theme only** — the remap
   writes `:root`, which `:root[data-theme="…"]` out-specifies. The two
   alternate themes keep their own contrast. **Unowned.**
4. **`data-a11y` has no writer.** The remap block is complete and correct;
   nothing sets the attribute. **Strand A-4** ("My settings").
5. **Shirley's state is not persisted** across full page loads. **Strand
   A-7.**
6. **The model line has no home.** **Strand A-7.**
7. **The below-1600-px auto-collapse is not implemented** (A0e flag 3).
   **Strand A-5.**
8. **Ctrl J on the super-admin surface still sets `data-shirley`.** With
   the block empty there is nothing to show, but the shortcut still writes
   the attribute, and `layout.css` would widen the third column to 380 px
   of nothing. Not fixed here: the fix is one guard in `section_nav.js` or
   one selector in `layout.css`, and gate 3 forbids both files. Harmless
   and invisible unless a super-admin presses Ctrl J. **New — this
   strand's find.**
9. **`.pf-rail` is two different components.** Shirley's rail
   (`layout.css`) and the Cases detail's right-hand box
   (`components/cases.css`) share the class name; they do not collide today
   only because the Cases one is scoped under `.pf-case-detail` and
   Shirley's under `.pf-side`. Renaming one is **strand A-3's** call.
10. **`.pf-statusbar__group--right` restates the mono font stack** as a
    literal although `--ui-font-family-mono` now exists (A0d2 added it, and
    26 declarations use it). One-line token swap, deferred with the rest of
    the `--pf-*` parked cleanup.
11. **The atlas creates five cases per run.** By the prompt's design — the
    tool does not chain scenes, so each composer scene repeats the seed.
    If the run becomes routine, a scene-level fixture or a shared seed step
    would be the fix; that is an edit to `tools/ux_atlas.py`.

**`services/…`-side: none.** No Python outside three test modules, no
repository, no service, no migration, no route.
