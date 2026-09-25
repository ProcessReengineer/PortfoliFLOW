<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0e2 — Shirley's stage and dock bound to the viewport

**Hotfix, outside strand A-1** (demo imminent) · **Date:** 2026-09-25 ·
**Ran after:** the P-UX-A1s strand end (`7d34336`, `c1e2c80`) ·
**Scope:** `base.html`, `shirley_dock.html`, `layout.css`, `chat.css`,
`chat.js`, two existing test modules, this report. No atlas run, no full
suite, no `ui-standards.md`.

`.pf-stage` carried `height: 100%` inside a shell with only a
`min-height`, so the percentage resolved to `auto`, the stage grew with
the conversation and the document scrolled. `.chat-history` still carried
`min-height: 280px; max-height: 60vh` from the retired Section embed, so
the composer floated mid-column over a dead band. Nothing writes to
`.pf-stage__canvas` yet, so the stage was a 420 px conversation beside an
empty half-screen.

The stage is now viewport-bound exactly as `.pf-side` is; the conversation
has the rail anatomy (head · flexing history · composer at the foot) in
**both** hosts; and while the canvas is empty the conversation takes the
whole work area at a reading width. The composer's UA-default file picker
and the 🎤 emoji became `pf-btn--icon` controls.

---

## OPERATOR ACTION REQUIRED

### 1. Commit

```
git add web/templates/base.html web/templates/_partials/shirley_dock.html \
        web/static/css/layout.css web/static/css/components/chat.css \
        web/static/js/chat.js tests/web/test_shirley_dock.py \
        docs/reports/P-UX-A0e2-report.md
git commit -m "fix(shell): bind Shirley's stage and dock to the viewport, full-width stage while the canvas is empty, icon composer controls (UX hotfix, P-UX-A0e2)"
```

**Add `tests/regression/test_shirley_shell_element.py` to that `git add`** —
its sticky-column pin had to move from 2 to 3 (deviation D1 below).

### 2. Re-bootstrap if a gate wiped the dev DB

The gate ran DB-bound modules, so `tenants`, `users` and `scoped_settings`
are empty:

```bash
portfoliflow bootstrap
```

Then re-enter `voice.enabled = true` and both voice keys in
Admin → Providers & Credentials, and **reload the page** — the dock is
fetched once per page life, so a re-enabled tenant needs a fresh load
before the 🎤 appears.

### 3. Browser round

On `minathena-capital.localhost:8000`, at 100 % **and** 125 % zoom,
Assistants → Shirley:

- the conversation fills the work area, **no document scroll**; head at
  the top, composer at the bottom, history scrolling inside;
- "Nothing on the stage yet" is gone;
- the paperclip opens the picker and the chosen names appear under the
  row;
- 🎤 is a mic icon that highlights when pressed;
- "Back to dock" (the head's toggle) returns to the 380 px dock, where
  the composer now also sits at the bottom;
- Ctrl J still toggles; navigating Areas while docked keeps the
  conversation.

---

## Verify-first

Line numbers are the **pre-edit** working tree.

| # | Check | Found |
|---|---|---|
| 1 | `git status --porcelain` empty | clean |
| 1 | `grep -c "P-UX-A1s"` over `log -40` | **2**, not 1 — see D0 |
| 1 | `grep -c "P-UX-A1e2"` over `log -40` | 1 (`76fea55`) |
| 2 | `.pf-stage {` + `grid-area: main;` / `display: none;` / `height: 100%;` | `layout.css:812–816` |
| 2 | `.pf-shell[data-shirley="stage"] .pf-stage {` + `display: grid;` + `grid-template-columns: var(--ui-layout-stage-conversation) minmax(0, 1fr);` | `layout.css:822–825` |
| 2 | `.pf-side {` with `height: calc(100vh - var(--pf-statusbar-height));` | `layout.css:710`, height at `716` |
| 3 | `.chat-history {` with `min-height: 280px;` / `max-height: 60vh;` | `chat.css:26`, `30`, `31` |
| 3 | `.chat-composer__attach {` · `.chat-composer__voice-toggle {` | `chat.css:242` · `chat.css:277` |
| 4 | `<div class="pf-stage" id="pf-stage" hidden>` | `base.html:105` |
| 5 | `class="chat-composer__attach"` · `title="Voice mode">🎤</button>` · `hx-on::after-request="this.reset()"` | `shirley_dock.html:131` · `144` · `119` |
| 6 | `ICONS["attach"] = "paperclip"`, `ICONS["voice"] = "mic"`; both SVGs on disk | `icons.py:47`, `48`; `paperclip.svg`, `mic.svg` present |
| 7 | document-level `htmx:afterSwap` calling `initVoiceControls(event.target)` | `chat.js:54` (listener), `67` (call) |
| 8 | `grep -rn "chat-composer__attach\|chat-composer__voice-toggle" tests web/static/js` | nothing |

---

## §1 — `layout.css`: the stage bound to the viewport

`.pf-stage` (`812`) trades `height: 100%` for the `.pf-side` formulation —
`position: sticky; top: 0; height: calc(100vh - var(--pf-statusbar-height));
min-width: 0; overflow: hidden;` — with the comment naming the resolved-to-`auto`
bug. The state rule and `#shell-main { display: none; }` are unchanged and
were not duplicated (the latter already sat in this block).

Four new rules follow the state rule (`840`, `844`, `848`, `853`): the
empty canvas collapses the grid to one column, hides
`.pf-stage__canvas`, centres `.pf-stage__chat` without its right border,
and caps `.pf-chat` at `max-width: 880px`. `.pf-stage__chat`,
`__canvas`, `__head`, `__title`, `__note` are untouched.

## §2 — `chat.css`: the rail anatomy, the composer controls

1. `.chat-history` (`30`) — `flex: 1 1 auto; min-height: 0;` in place of the
   two caps, `padding: var(--ui-space-3) var(--ui-space-4)`, comment
   rewritten to the dock/stage column.
2. `.chat-composer` — `flex: none;`, `border-radius: 0;` (it is the
   column's foot, not a card).
3. `.chat-composer__attach` and its comment deleted;
   `.chat-composer__files` + `[hidden]` in its place (`249`, `258`).
4. `.chat-composer__voice-toggle` base rule and the `:hover` half of the
   pressed rule deleted; only `[aria-pressed="true"]` remains (`290`),
   with the ADR-0076 comment updated to `pf-btn--icon`.
5. File header gained one sentence on the embed living in the shell
   column since P-UX-A0e.

No other rule changed. `--ui-space-3`, `--ui-space-4`,
`--ui-font-scale-xs` are all declared in `theme.css`;
`tests/web/test_css_tokens.py` is green.

## §3 — `base.html`

`base.html:109` is now
`<div class="pf-stage" id="pf-stage" data-canvas="empty" hidden>`, and the
stage comment gained a paragraph on `data-canvas` and the absent canvas
writer.

## §4 — `shirley_dock.html`: the composer row

Row order is textarea · attach · voice · Send (`125`–`157`). The native
input keeps `id`, `name`, `accept`, `capture`, `multiple` — the multipart
contract is byte-identical — and gains `hidden`; the paperclip
(`data-pf-attach-trigger`, `141`) opens it. The voice toggle keeps
`data-pf-voice-toggle`, `aria-pressed` and its class, and renders
`pf_icon("voice")`. `<p class="chat-composer__files" data-pf-attach-names
hidden>` sits directly after `.chat-composer__row` (`158`), before the
voice panel. Head, banner, history, sources, pin dialog, voice panel and
hint are byte-identical; the fragment's head comment gained one line.

## §5 — `chat.js`: the attach trigger and the names line

Three delegated document-level handlers plus `renderAttachNames`, inserted
between the composer `keydown` listener and the EventSource bootstrap
(`86`–`130`): `click` on `[data-pf-attach-trigger]` → `input.click()`;
`change` on `#chat-attach` → render; `reset` on `#chat-form` in the
**capture** phase (reset does not bubble), deferred by `setTimeout(…, 0)`
so the files are already cleared. Delegation because the conversation
subtree arrives via `/chat/dock` and moves between hosts; `htmx.process`
is never called. `initVoiceControls` is untouched.

## §6 — Tests

- `test_every_area_page_carries_the_rail_and_the_two_hosts` (`227`):
  `assert 'id="pf-stage" data-canvas="empty"' in body`.
- `test_chat_dock_renders_the_one_conversation_as_a_fragment` (`290`–`292`):
  `data-pf-attach-trigger` present, `id="chat-attach"` and `name="images"`
  still in the form, no `🎤`.
- `test_shirley_shell_element.py` — the sticky-column pin, see D1.

No new test module, no new test function.

---

## Gates

```
$ ruff check web/ tests/web/test_shirley_dock.py tests/regression/test_shirley_shell_element.py
All checks passed!
$ ruff format --check web/ tests/web/test_shirley_dock.py tests/regression/test_shirley_shell_element.py
39 files already formatted
$ pytest tests/web/test_shirley_dock.py tests/web/test_chat_voice.py \
         tests/regression/test_shirley_shell_element.py tests/web/test_css_tokens.py -q
111 passed in 35.52s
```

The full suite was **not** run; the A1s home run
(`docs/reports/full-suite-2026-09-25-report.md`) stands as the baseline
and this hotfix rides on the four modules above. The dev Postgres was
down at first attempt and was started with
`podman start portfoliflow-postgres` (no compose provider in this
environment).

---

## Deliberate deviations

**D0 — the A1s marker appears twice, not once.** The prompt's verify step 1
expects `grep -c "P-UX-A1s"` to be `1`; the working tree returns `2`,
because the operator committed the strand end as two commits —
`c1e2c80` (`ui-standards.md`) and `7d34336` (the full-suite report), both
carrying the marker. The precondition the check exists for — the tree is
post-A1s — holds, twice over, so the run proceeded rather than stopping.

**D1 — a second test module had to move.**
`tests/regression/test_shirley_shell_element.py` pins
`css.count("height: calc(100vh - var(--pf-statusbar-height));") == 2` —
the two sticky columns, `.pf-sidebar` and `.pf-side`. §1 makes `.pf-stage`
a third one *by design and in those exact words*, so the pin went to `3`,
the docstring now names all three, and the function was renamed
`test_layout_clears_the_statusbar_on_both_sticky_columns` →
`…_on_every_sticky_column` (the old name is referenced nowhere else in the
tree). The alternative — spelling the stage's height differently to dodge
the count — would have defeated the pin and diverged from the `.pf-side`
formulation §1 asks for. This module is outside the prompt's Touches list;
it is in the `git add` above.

`_CHAT_ANCHOR_IDS = 7` in the same module held unchanged: the attach input
kept its id and the new paperclip is addressed by data attribute, not id.

---

## Flags — for the DC-UX-A3 closing report and the record

- `max-width: 880px` on the empty-canvas stage is a reading width with no
  token behind it. D-UX-S question: `--ui-layout-stage-reading-width`?
- `.chat-message { width: 60% }` (`chat.css`) is now the only thing
  narrowing bubbles in a 380 px dock and an 880 px stage alike. The rule
  predates both surfaces and wants a look in **A-7**.
- The stage head ("Shirley · Back to dock") lives *inside* the hidden
  canvas while `data-canvas="empty"`; the way back is the conversation
  head's stage toggle, which is labelled "Back to dock" in that state.
  Fine for the demo; **A-7** decides whether the stage wants a head of
  its own.
- The canvas writer does not exist. `data-canvas="figure"` has no author,
  so the two-column split is currently unreachable.
- **A1s ran before this hotfix.** `docs/ux/ui-standards.md` as rewritten
  by A1s describes the Shirley column and the composer as they were
  *before* A0e2 (capped history, native picker, emoji toggle), and its §4
  users column is stale by the two new `pf-btn--icon` users; the A1s
  measurement table and the inventory baseline predate the two new
  buttons. None of that is fixed here — the DC-UX-A3 closing report lists
  A0e2 as an out-of-strand commit and carries these lines; a later docs
  prompt updates the file and re-runs the inventory.
- Voice absent in a tenant is **configuration, not code**: `voice.enabled`
  → `VOICE_ENABLED` → off. DB-bound gates wipe `scoped_settings`, and the
  dock is fetched once per page life, so a re-enabled tenant needs a page
  reload.
