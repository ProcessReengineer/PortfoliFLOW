<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0e — Shirley dock and stage

**Strand:** UX A-0 · **Date:** 2026-09-23 · **Runs after:** P-UX-A0d2
(`c83cd7a`) and the post-A0d2 atlas run (`docs/ux/atlas/2026-09-23-3/`)
· **Scope:** `base.html`, `layout.css`, the renamed chat partial, the
Assistants body, `chat.py`, `areas.py`, three `.js` files (one new),
`atlas-scenes.json`, six test files (two new), this report. **No
`chat.js`, no `chat.css`, no `pf_components.css`.**

Shirley left the Assistants body and became a shell element. Three
states live on `.pf-shell[data-shirley]` — `closed` (a 44 px rail),
`docked` (a 380 px column that *pushes* the work area), `stage` (the
conversation at 420 px plus a canvas, filling it). **One DOM instance**,
moved between `#dock-chat-host` and `#stage-chat-host`, both outside
`#shell-main`: `#chat-history` and `#chat-form` stay unique, and a
running SSE stream survives an area swap. Ctrl J toggles closed ↔
docked; `/assistants#shirley` opens the stage.

The shell grid gained its third column and its named areas, and the
baseline sidebar/statusbar overlap is fixed on the way past.

---

## OPERATOR ACTION REQUIRED

### 1. Gate 8 — the browser check is yours

Nine checks, (a)–(i), listed verbatim in §Browser check below. They
need `portfoliflow bootstrap` plus a member account, and both roles.
**The gate-7 fixtures `TRUNCATE` on teardown, so the dev DB is empty
after this run** — re-bootstrap before opening the app:

```bash
portfoliflow bootstrap
```

### 2. Gate 9 — the atlas *after* run

```bash
python tools/ux_atlas.py --bands 1200
```

Compare against **`docs/ux/atlas/2026-09-23-3/`** (`git_head c83cd7a`).
Two scenes were added to `docs/ux/atlas-scenes.json` and will be
captured by the same run: `shirley-docked` (Front Office, dock open)
and `shirley-stage` (`/assistants#shirley`).

**What must differ:** every area gains the 44 px rail at the right
edge, and "Sign out" is no longer cut off by the status bar.
**What must not:** anything else.

### 3. Commit

Everything is staged. The commit is the operator's.

```bash
git commit -m "feat(shell): Shirley as a shell element — rail, dock and stage on .pf-shell[data-shirley], one chat instance moved between hosts, Ctrl J, /chat/dock fragment; sidebar no longer under the statusbar (UX A-0, P-UX-A0e)"
```

The rename is staged as a rename (`git mv` then edit), so the
`git rm --cached` line in the prompt's block is the no-op it says it is.

---

## Verify-first

Checks 1–3 (the STOP checks) passed: clean tree, HEAD `c83cd7a` with a
subject ending `(UX A-0, P-UX-A0d2)`, none of the three new paths
present, no `/chat/dock` in `chat.py`.

| # | Check | Result |
|---|---|---|
| 1 | `git status --porcelain` | ✅ empty |
| 2 | `git log --oneline -1` | ✅ `c83cd7a … (UX A-0, P-UX-A0d2)` |
| 3 | new paths absent, no `/chat/dock` | ✅ all four |
| 4 | `base.html` anchors | ✅ 81 / 84 / 88 / 102 / 104 / 108 exactly |
| 5 | `layout.css` 26–36, no areas, no `data-shirley` | ✅ two-column grid + collapsed variant; `grid-template-areas` 0, `data-shirley` 0 |
| 6 | `height: 100vh` 47; `grid-column: 1 / -1` 658 | ⚠️ **drift, anchors intact** — see below |
| 7 | four layout tokens | ✅ `44px` / `380px` / `420px` / `28px`, all declared |
| 8 | `wc -l` on five files | ✅ 142 / 26 / 557 / 77 / 278 |
| 9 | `model_id` / `voice_enabled` / `brief_banner` in `areas.py` | ⚠️ **drift, anchor intact** — see below |
| 10 | `chat.js` initialiser lines | ⚠️ **drift, anchor intact** — see below |
| 11 | `section_nav.js` 184 / 195 | ✅ exact |
| 12 | test files carrying the chat markers | ✅ the three named; `test_chat_consultation.py` has **9** `"/assistants` (≥ 8) |
| 13 | `ls -d docs/ux/atlas/*/ \| tail -1` | ⚠️ **drift, anchor intact** — see below |

**Check 6.** The pattern `height: 100vh` matches `min-height: 100vh`
too, so it returns 29 (`.pf-shell`), **47** (`.pf-sidebar` — the
anchor), and 741. `grid-column: 1 / -1` returned 658 exactly.

**Check 9.** All three names are inside `assistants_view` (461–489) as
stated, *plus* the import block at 51–53, which the grep also matches.
Both were rewritten here.

**Check 10.** The grep returns 61, 67, **140**, 379, 519, 554, 555. The
extra 140 is `function attachSseListeners(container) {` — the
definition, which the pattern `attachSseListeners(` matches and the
prompt's list omits. Every anchor the check was reaching for is there.

**Check 13.** `tail -1` returns `docs/ux/atlas/2026-09-23/`, for the
same sort-order reason P-UX-A0d2's check 9 documented: with the
trailing slash, `-` (0x2D) sorts before `/` (0x2F), so `2026-09-23-3/`
lands *before* `2026-09-23/`. By mtime the post-A0d2 run is
`docs/ux/atlas/2026-09-23-3/` (12:54), which is the anchor.

---

## §1 — the shell grid

`.pf-shell` now declares three columns, two rows and named areas, and
each child names its area rather than relying on document order. The
stage is why: `.pf-stage` shares the `main` cell with `#shell-main`,
which implicit placement cannot express.

| State | Third column |
|---|---|
| `closed` (default) | `var(--ui-layout-rail-width)` — 44 px |
| `docked` | `var(--ui-layout-dock-width)` — 380 px |
| `stage` | `0` |

Each state variant swaps **only** the third column, and
`[data-sidebar-collapsed="true"]` swaps only the first, so the two
compose. Four rules result (collapsed × docked, collapsed × stage are
spelled out) rather than a combined selector, because `grid-template-
columns` is a single property and a partial override is not possible.

`transition: grid-template-columns var(--ui-motion-duration) ease` on
`.pf-shell`. The accessible-mode remap at
`components/pf_components.css:309` already maps `--ui-motion-duration`
to `--ui-accessible-motion-duration` (`0ms`), so accessible mode gets
the state change with no animation, unchanged and unextended.

**The overlap fix (baseline flag).** `.pf-sidebar` was `height: 100vh`
inside a shell whose status bar is `position: sticky; bottom: 0`: the
column ended exactly where the bar begins, so the bar rode on top of
the sidebar footer and "Sign out" was unreachable. Both sticky columns
now take `calc(100vh - var(--pf-statusbar-height))`. The token is in
the parked `pf` block in `theme.css`; *using* it is in scope here,
*cleaning the block* remains parked.

`.pf-statusbar`'s `grid-column: 1 / -1` became `grid-area: status`,
and its comment ("the shell grid no longer names its areas") was
corrected — it does again.

---

## Transcription ledger — `shared.css` 167–215

The prompt's line numbers run one ahead of the file from
`.pf-chat__brief` onward; the **named** rule sets are authoritative and
are what was taken. Mapping is the P-UX-A0d table, verified by value.

### Taken (18 rules)

| Source | Rule | Token substitutions |
|---|---|---|
| 168 | `.pf-side` | `--pf-bg-0` → `--ui-background-chrome`; `--pf-line-soft` → `--ui-border-soft`. **Plus** `position: sticky; top: 0` and the calc height — the product's columns are sticky, the mock's shell is viewport-high |
| 169 | `.pf-rail` | `--pf-s2` → `--ui-space-2`. `padding-top: 11px` kept as a literal — it is optical centring against the 56 px head, not a scale step |
| 170 | `.pf-rail__btn` | none |
| 171 | `.pf-presence` | `--pf-pos` → `--ui-semantic-positive` |
| 172 | `.pf-rail__name` | `--pf-s2`, `--pf-text-low` → `--ui-text-tertiary`, `--pf-fs-xs` → `--ui-font-scale-xs` |
| 173 | rail hidden when open | verbatim |
| 174 | dockhost hidden when closed; side hidden on stage | verbatim |
| 175 | `.pf-dockhost` | none |
| 177 | `.pf-chat` | none |
| 178 | `.pf-chat__head` | `--pf-s2/s3/s4` → `--ui-space-2/3/4`. `height: 56px` kept |
| 179 | `.pf-chat__name` | `--pf-text-hi` → `--ui-text-primary`; `font-weight: 600` → `var(--ui-font-weight-semibold)` |
| 180 | `.pf-chat__name .pf-presence` | verbatim |
| 204 | `.pf-stage` | **plus** `grid-area: main` |
| 205 | stage grid | `420px` → `var(--ui-layout-stage-conversation)` |
| 207 | `.pf-stage__chat` | `--pf-bg-0`, `--pf-line-soft` |
| 208 | `.pf-stage__canvas` | `--pf-s5/s6/s7` → `--ui-space-5/6/7` |
| 209 | `.pf-stage__head` | `--pf-s3/s4` |
| 210 | `.pf-stage__title` | `--pf-text-hi`, `--pf-fs-xl` → `--ui-font-scale-xl`, weight token |
| 211 | `.pf-stage__note` | `--pf-text-mid` → `--ui-text-secondary`, `--pf-fs-sm` |

### Added, not in the mock

* `.pf-shell[data-shirley="stage"] #shell-main { display: none; }` —
  the product's equivalent of the mock's `.pf-view` rule (206), which
  targets a class the product does not use.

### Left for the Assistants strand (§Not in scope)

`.pf-chat__brief` and its two children, `.pf-chat__log`, `.pf-msg`
(five rules), `.pf-msg__actions`, `.pf-preview` (three rules),
`.pf-chat__composer`, `.pf-chat__box` (two), `.pf-chat__input` (two),
`.pf-chat__tools` (two), `.pf-chat__hint` — **21 rules**. The moved
instance keeps its `chat-embed` styling from `components/chat.css`,
untouched here.

### Deliberately not transcribed

* The `@container view (max-width: 900px)` stage narrowing (212–214) —
  the prompt excludes it, and the product declares no container
  context.
* `.pf-shell[data-shirley="stage"] .pf-view { display: none; }` (206) —
  superseded by the `#shell-main` rule above.

**Zero literals introduced.** `layout.css` after the transcription:
`hex 0`, `rgb/rgba/hsl 0`, dangling token names `0`.
`tests/web/test_css_tokens.py` stays green at 85 passed.

---

## §4 — `chat.js` was not edited, and did not need to be

`git diff --quiet web/static/js/chat.js` passes. Three existing seams
carry the new arrangement:

* **`chat.js:53–67`** — the `htmx:afterSwap` listener is bound on
  `document` and branches on `event.target`, not on a fixed container.
  Line **67**, `if (event.target) initVoiceControls(event.target);`,
  runs for *any* swap target, so the `/chat/dock` swap into
  `#dock-chat-host` wires the voice toggle and panel. Its own dataset
  guard makes it idempotent.
* **`chat.js:56–60`** — the same listener calls
  `attachSseListeners(event.target)` when the target is, or is inside,
  `#chat-history`. The `#chat-history` div inside the fragment carries
  `hx-trigger="load"`, which htmx fires when it processes the swapped
  subtree, so the history loads itself and its swap arms the SSE
  listeners. Nothing about that changed: the div is byte-identical.
* **`chat.js:519`** — the send path re-attaches listeners against the
  live `#chat-history` it looks up by id at call time, so it follows
  the instance wherever `shirley.js` has moved it.

`htmx.process` is **not** called after a move, as the prompt requires:
`appendChild` relocates a live subtree with its listeners and htmx
bindings intact, and re-processing would double-bind every `hx-*`.
`tests/regression/test_shirley_shell_element.py` pins the absence of
both `htmx.process(` and `cloneNode(` in `shirley.js`.

**One deferred-script ordering hazard found and handled.** HTMX binds
its triggers on `DOMContentLoaded`, which runs *after* every deferred
script — `shirley.js` and `shell.js` included. A state resolved during
that window (a server-rendered `docked`, or `shell.js` reading
`#shirley` at parse time) would dispatch `pf:shirley-open` at a host
that is not listening yet. `shirley.js` therefore holds the **first**
dispatch until `DOMContentLoaded` (`htmxReady` / `openPending`). htmx
is a synchronous `<head>` script, so its listener is registered first
and runs first; the held dispatch lands after it.

---

## §5 — `/assistants#shirley`, and one departure from §4's wording

The prompt says the `shell.js` hook fires "when the resolved section is
`assistants`' `shirley`". **Implemented on the explicit fragment
instead**, because `shirley` is also the Assistants *landing* section
(`web/shell.py:220`): `resolve()` names it for a bare `/assistants`
too, via its "fall back to the section the server left visible" branch.
Firing on the resolved slug would open the stage on every `/assistants`
visit — and would take `/assistants?case=…` (which must arrive
*docked*, with its banner, per §3.3 and browser check (g)) off the dock
the operator asked for.

`openStageForShirley` therefore requires **both** the resolved slug and
`window.location.hash` to be `shirley`, plus an `[data-area="assistants"]`
element on the page. Everything else about the hook is as specified:
navigating away does nothing, and the stage is left by its own "Back to
dock", which restores the view beneath — hidden, never removed.

---

## Deliberate deviations

1. **The lazy `/chat/dock` fragment** — *this prompt's stated design,
   not a deviation from it.* The operator did not veto it before the
   run, so the specified branch was taken: the empty hosts render on
   every page, and the chat instance is fetched once, on first open.
   The alternative (inline `_partials/shirley_dock.html` in `base.html`
   from a `shirley` context dict built by `_render_area`) would cost
   two tenant-scoped DB reads — `_resolve_voice_enabled` and
   `resolve_active_brief_banner` — on **every** area render of all
   nine Areas. Recorded here so the choice is legible later.

2. **The `shell.js` hook gates on the explicit fragment**, not the
   resolved slug — §5 above. Without it the prompt's own browser check
   (g) fails.

3. **The atlas "docked" scene clicks the rail button rather than
   pressing Ctrl J.** `apply_step` in `tools/ux_atlas.py` knows four
   verbs — `click`, `fill`, `wait`, `wait_ms` — and no key verb. Adding
   one would edit `tools/ux_atlas.py`, which is outside this prompt's
   gate-3 file list. `{"click": ".pf-rail__btn"}` reaches the identical
   state through the product's own affordance; the Ctrl J *binding* is
   pinned by `tests/regression/test_shirley_shell_element.py` and
   verified in browser check (b)/(f).

4. **`shirley_dock.html` is 165 lines, not 142 + the head.** The head
   is 16 lines and the docstring grew by 14; the old `.chat-controls`
   row (10 lines) was removed and the model line (3) deleted.
   `grep -c 'id="chat-'` is **7**, as the prompt requires, and
   everything below the head is byte-identical apart from indentation —
   which did not change either, since the wrapper went from `<div>` to
   `<section>` at the same depth.

5. **`data-shirley="docked"` is passed when a marker *and* a live brief
   are both present**, not when the marker specifically named an open
   case. `resolve_active_brief_banner` sets the stash and returns the
   banner in one call, and distinguishing "this marker set it" from
   "an earlier one did" would mean reaching into the private stash
   helpers. The looser test's only extra case — a malformed marker
   arriving on top of an already-stashed case — opens the dock showing
   the true banner, which is the harmless way round. Documented in the
   handler's docstring.

---

## Tests

### Rewritten — `tests/web/test_assistants_embedding.py`

| Was | Now |
|---|---|
| `test_assistants_renders_chat_shell_inside_shirley_section` | `test_assistants_shirley_section_is_the_pointer` — the section keeps its slug and heading, its body is the pointer prose + `data-set-shirley="stage"`, and carries **no** `chat-form` / `chat-history` / `chat-input` |
| `test_assistants_renders_model_status_line_when_set` | **deleted** (record §2.10.3) |
| `test_assistants_omits_model_line_when_unset` | **deleted** (record §2.10.3) |
| `test_assistants_htmx_request_returns_fragment_with_chat_shell` | `test_assistants_htmx_fragment_carries_neither_chat_nor_column` — no chat ids, no `id="pf-side"`, no hosts |
| `test_assistants_provider_credentials_section_links_to_admin` | unchanged |

`assert "pf-side" not in body` had to become `assert 'id="pf-side"'
not in body`: `pf-sidebar` contains `pf-side` as a prefix, and the
sidebar OOB fragment is part of every area swap.

### New — `tests/web/test_shirley_dock.py` (6 tests, DB-bound)

1. `test_every_area_page_carries_the_rail_and_the_two_hosts` —
   `/front-office` has exactly one of each host, one `#pf-side`,
   `data-shirley="closed"`, `title="Open Shirley  Ctrl J"`, and no
   `id="chat-form"`.
2. `test_the_area_fragment_carries_none_of_the_hosts` — the `HX-Request`
   fragment carries none of the three.
3. `test_chat_dock_renders_the_one_conversation_as_a_fragment` — 200,
   one each of `#pf-chat` / `#chat-history` / `#chat-form` /
   `#chat-input`, the head's three controls, no `Model: `, no `<html`.
4. `test_chat_dock_is_session_gated_like_chat_history` — 303 →
   `/login`; HTMX → 401 + `HX-Redirect: /login`.
5. `test_a_case_marker_opens_the_dock_and_banners_in_it` —
   `/assistants?case=<open>` → `data-shirley="docked"` and **no**
   banner in the page body; the following `/chat/dock` carries
   "Consulting for", the badge and the title.
6. `test_assistants_without_a_marker_stays_closed` → `closed`.

### New — `tests/regression/test_shirley_shell_element.py` (8 tests, DB-free)

Three `data-shirley` variants and `grid-template-areas` in
`layout.css`; all four grid areas placed; the calc height on **both**
sticky columns; one each of the four shell ids in `base.html` plus the
`hx-get` / `hx-trigger` pair; `id="chat-` count **7** (hardcoded from
the pre-move partial, as the prompt requires — the old file is gone);
`#chat-form` present in exactly one template; the Ctrl J branch beside
Ctrl K; and `shirley.js` free of `cloneNode(` / `htmx.process(`.

### Re-pointed, URLs only

| File | Test | Change |
|---|---|---|
| `test_chat_consultation.py` | `_login_and_csrf` (helper, feeds every test in the file) | reads the CSRF token off `/chat/dock` instead of `/assistants` — the composer moved |
| `test_chat_consultation.py` | `test_a_stale_closed_since_stash_clears_and_runs_unbriefed` | the closing banner-absence assertion moves to `/chat/dock` |
| `test_chat_consultation.py` | `test_marker_hygiene_banner_dismiss_and_replace` | each `/assistants?case=` still **sets** the stash; every banner assertion (three absent, two present, one after dismiss) moves to the `/chat/dock` render that follows it. The dismiss CSRF is read from the dock too |
| `test_chat_voice.py` | `_login_and_get_csrf` (helper) | `/chat/dock` |
| `test_chat_voice.py` | `test_disabled_service_404s_and_hides_controls` | toggle asserted on `/chat/dock` |
| `test_chat_voice.py` | `test_enabled_service_renders_controls` | toggle asserted on `/chat/dock` |
| `test_chat_voice_resolution.py` | `_login` (helper) | `/chat/dock` |
| `test_chat_voice_resolution.py` | `test_a_tenant_row_enables_voice_with_the_environment_silent` | both off/on assertions on `/chat/dock` |

Every assertion's substance is unchanged; `test_chat_consultation.py`'s
tests that only use `/assistants?case=` to set the stash (`386`, `410`)
were left exactly as they were.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/regression -q` | ✅ **164 passed** in 159s |
| 2 | `ruff check` · `ruff format --check` · `pyright` on `chat.py` + `areas.py` | ✅ clean / ✅ formatted / ⚠️ one **pre-existing** pyright error, `chat.py:2019` (`StreamingResponse` content type) — proven pre-existing by `git stash` + re-run on clean `main` |
| 3 | changed-file list; `chat.js` untouched | ✅ exactly the 16 paths; `chat.js`, `chat.css`, `pf_components.css` all byte-identical |
| 4 | `data-shirley` counts | ✅ 11 / 3 / 1 (≥ 3 / ≥ 3 / 1) |
| 5 | inventory | ✅ `hex 0`, `rgb/rgba 1` (`login.css`, out of scope), `dangling 0`; `test_css_tokens.py` 85 passed |
| 6 | `id="chat-form"` across templates | ✅ one file, `shirley_dock.html`, count 1 |
| 7 | the ten DB-bound web files | ✅ **153 passed** — 9 in 18s (`test_shirley_dock.py` + `test_assistants_embedding.py`), 144 in 249s (the other eight) |
| 8 | operator browser check | ⏳ **operator** — §Browser check |
| 9 | atlas *after* | ⏳ **operator** — two new scenes shipped |

**Gate 3 — the staged set (16 paths).**
`docs/reports/P-UX-A0e-report.md`, `docs/ux/atlas-scenes.json`,
`tests/regression/test_shirley_shell_element.py`,
`tests/web/test_assistants_embedding.py`,
`tests/web/test_chat_consultation.py`, `tests/web/test_chat_voice.py`,
`tests/web/test_chat_voice_resolution.py`,
`tests/web/test_shirley_dock.py`, `web/routes/areas.py`,
`web/routes/chat.py`, `web/static/css/layout.css`,
`web/static/js/section_nav.js`, `web/static/js/shell.js`,
`web/static/js/shirley.js`, `web/templates/base.html`,
`web/templates/_partials/areas/_assistants_body.html`, and the rename
`_partials/shirley_section.html → _partials/shirley_dock.html`.

---

## Browser check (gate 8) — operator

Run after `portfoliflow bootstrap` and a member account, in both roles.

| | Check | Result |
|---|---|---|
| (a) | any area: rail visible; the status bar no longer covers "Sign out" | ⏳ |
| (b) | Ctrl J → dock opens, content **pushed** not overlaid, history loads, voice toggle present if enabled | ⏳ |
| (c) | send a message, switch area via the sidebar **while it streams** → stream continues, dock stays | ⏳ |
| (d) | "Open on stage" → conversation at 420 px, canvas with the empty state, view hidden; "Back to dock" → view returns where it was | ⏳ |
| (e) | `/assistants#shirley` in the address bar → stage | ⏳ |
| (f) | Ctrl J closes, Ctrl J reopens to the last state | ⏳ |
| (g) | `/assistants?case=<id>` from a case's "Consult Shirley" → arrives docked, with the banner | ⏳ |
| (h) | collapse the sidebar with the dock open → three columns still consistent | ⏳ |
| (i) | 125 % zoom: dock still usable | ⏳ |

(c) is the one that matters most: it is the whole reason the hosts live
outside `#shell-main`.

---

## §7 Flags

1. **The model line has no home yet.** Record §2.10.3 demoted it and
   names Providers & Credentials as its destination; nothing renders it
   today. The Assistants strand (A-?) owns putting it there. The two
   tests that pinned it are deleted, so there is no test guarding the
   gap — this flag is the record.
2. **Shirley's state is not persisted across full page loads.** Every
   full load starts `closed` unless the server says otherwise
   (`/assistants?case=…` → `docked`). A reader who docks Shirley and
   then hard-navigates loses the dock. Cheapest fix when it is wanted:
   a session value or a `localStorage` read in `shirley.js`'s
   initialiser — deliberately not done here.
3. **The below-1600-px auto-collapse presentation rule is not
   implemented.** At narrow widths the dock still takes its full 380 px
   out of the work area rather than collapsing the sidebar for it.
4. **The stage canvas is an empty state.** "Open on stage" from a chart
   preview, and any content beyond the empty state, belong to the
   Assistants strand.
5. **`chat-embed` styling in a 380 px column** — the moved instance
   keeps its `components/chat.css` rules, written for a full-width
   section body. `.chat-history` carries `max-height: 60vh`, which in
   a flex column of its own height is now redundant at best; the
   composer row, the voice panel and the brief banner are the likely
   wrap/clip sites. **Gate 8 (b) and (i) are the read**; a band image
   goes here from the atlas run. The `pf-chat__*` rules that would
   replace them are explicitly the Assistants strand's (§Not in scope),
   so this is a *known* gap, not a discovered one.
6. **`tests/web/test_cases_area.py:496` is still red.** The A0b
   pre-existing failure the A0d2 report triaged (`data-section=` dots,
   retired by the one-section shell). Untouched here, as instructed;
   it goes to A0s with the other test hygiene.
7. **`.chat-controls` / `.chat-controls__new` in `chat.css` are now
   dead rules** — the "New chat" button moved into `pf-chat__head` and
   took its `pf-btn` classes. `chat.css` is out of scope for this
   prompt (gate 3 pins it unchanged), so the two rules are left in
   place for the Assistants strand to sweep with the rest.
8. **The rail renders on the super-admin surface too.**
   `web/templates/super_admin/base.html` extends `base.html`, so a
   super-admin session now sees Shirley's rail. Opening it works —
   `/chat/dock` takes `require_session` like `/chat/history`, and the
   System Tenant simply has no voice row and no cases, so the fragment
   renders bare — but `POST /chat/messages` is `require_role("owner",
   "member")`, so nothing can actually be sent. That is an affordance
   leading nowhere, not a data leak (ADR-0064 §1 is intact: the
   fragment reads the *session's* tenant under RLS and the System
   Tenant holds no domain data). Out of scope here — the prompt exempts
   `_auth_base.html` and nothing else — and the fix is one `is_super_admin`
   guard around the `<aside class="pf-side">` block. Flagged for A0s.

9. **`services/…`-side: none.** No Python outside two route modules,
   no repository, no service, no migration.
