# P-UX-A0a — Token set on the dark palette, neutral text ladder, vendored IBM Plex Sans, chart font line

**Strand:** UX A-0 (shell and tokens). **Date:** 2026-09-22. **Prompt:** P-UX-A0a v2
(re-keyed to run after P-UX-A0v).

One concern, as scoped: the design tokens. Ten tracked files changed; no template
body, no component sheet, no route, no chart spec. The visible effect is that
everything reading `--ui-text-primary` turns from brand red to neutral
`#EDEDED`, and text renders in IBM Plex Sans.

---

## OPERATOR ACTION REQUIRED

### 1. Stage and commit

```
git add config/ui_theme.json config/ui_theme_light.json config/ui_theme_corporate_blue.json \
        config/chart_theme.json web/static/css/theme.css web/static/css/base.css \
        web/templates/base.html web/templates/_auth_base.html tools/ux_atlas.py \
        web/static/vendor/README.md web/static/vendor/fonts/ibm-plex-sans \
        docs/reports/P-UX-A0a-report.md
git commit -m "feat(ui): token set on the dark palette, neutral text ladder, vendored IBM Plex Sans, chart font line (UX A-0, P-UX-A0a)"
```

The `web/static/vendor/fonts/ibm-plex-sans` path in that line is now a **no-op**:
the three `woff2` files and `LICENSE.txt` were committed with P-UX-A0v (see the
check-1 deviation below). Leaving it in the line is harmless.

### 2. Take the atlas

```
python tools/ux_atlas.py --bands 1200
```

Then zip the `bands/` folder for DC-UX-A. **The run folder is to be taken by the
operator after the commit** — no atlas run was made from this session.

DC-UX-A compares the Front Office Overview/Charts and Back Office bands against
the 2026-09-21 baseline for:

* chart canvas, series colours, grid and axes **unchanged**;
* chart text now in IBM Plex Sans;
* headings, labels and inputs neutral grey instead of red.

### 3. `portfoliflow bootstrap` — not required

`tests/web` did run (gate 6), but the single selected test
(`test_stepper_theme_tokens_removed_from_generated_css`) is DB-free by design —
its file's own docstring says "structural checks that do not need a live
database", and the run took 0.28 s without opening a connection. The dev
database was not touched. Run `portfoliflow bootstrap` only if you want the
belt-and-braces reset; nothing here needs it.

---

## Verify-first values observed

| # | Expected | Observed | Verdict |
|---|---|---|---|
| 1 | one line, `?? web/static/vendor/fonts/` | **clean tree, no output** | ⚠️ deviation — continued, see below |
| 2 | line 1 ends `(UX A-0, P-UX-A0v)`; line 2 `5328291 … (P-UX-1c)` | `e2fd660 build(web): vendor HTMX, Plotly, Tabulator and the Lucide icon set locally; pf_icon Jinja global (UX A-0, P-UX-A0v)` / `5328291 fix(ux): atlas drives nested lazy loaders and waits for Plotly before capture (P-UX-1c)` | ✅ |
| 3 | four files; `Web Open Font Format (Version 2)`; each > 40 KB | `LICENSE.txt` present (`SIL Open Font License` ×1); Regular 63 020 B, Medium 66 740 B, SemiBold 67 060 B, all `Web Open Font Format (Version 2), TrueType, version 3.327` | ✅ |
| 4 | `7:version = "2026.09.0"` | `7:version = "2026.09.0"` | ✅ |
| 5 | `"family": ["Arial", "Liberation Sans", "DejaVu Sans", "sans-serif"],` | byte-identical | ✅ |
| 6 | line 7 in each variant, same value | `chart_theme_print.json:7` and `chart_theme_light.json:7`, both identical to check 5 | ✅ |
| 7 | `23:` | `23:        "primary":    "#E8304A",` | ✅ |
| 8 | `37` in each of three | 37 / 37 / 37 | ✅ |
| 9 | `52` in three UI files, `155` in chart file | `ui_theme.json:52`, `ui_theme_light.json:52`, `ui_theme_corporate_blue.json:52`, `chart_theme.json:155` | ✅ |
| 10 | exit `0` | exit `0` | ✅ |
| 11 | `106` | `106` | ✅ |
| 12 | `base.html:27`, `_auth_base.html:33` | `base.html:27`, `_auth_base.html:33` | ✅ |
| 13 | `24:` | `24:    font-family: var(--ui-font-family);` | ✅ |
| 14 | `987:` and `1020:` | `987:def await_charts(page: Page) -> tuple[int, int]:` and `1020:    total, pending = 0, 0` | ✅ |
| 15 | no hits | no hits | ✅ |
| 16 | exactly the three `ui_theme*.json` files | `ui_theme.json`, `ui_theme_light.json`, `ui_theme_corporate_blue.json` | ✅ |

### Check 1 — the one deviation, and why the run continued

`git status --porcelain` printed **nothing**: the tree was clean, not carrying
the expected `?? web/static/vendor/fonts/`.

The check's own stated STOP conditions are "A0v is not committed yet, or
something else is dirty". **Neither holds.** The P-UX-A0v report recommended
option (a) — stage the four packages, leave the fonts untracked — but the
operator staged `web/static/vendor` wholesale, so the fonts went in with that
commit:

```
$ git log --oneline -1 -- web/static/vendor/fonts/
e2fd660 build(web): vendor HTMX, Plotly, Tabulator and the Lucide icon set locally; pf_icon Jinja global (UX A-0, P-UX-A0v)

$ git show --stat HEAD -- web/static/vendor/fonts/
 .../fonts/ibm-plex-sans/IBMPlexSans-Medium.woff2   |  Bin 0 -> 66740 bytes
 .../fonts/ibm-plex-sans/IBMPlexSans-Regular.woff2  |  Bin 0 -> 63020 bytes
 .../fonts/ibm-plex-sans/IBMPlexSans-SemiBold.woff2 |  Bin 0 -> 67060 bytes
 web/static/vendor/fonts/ibm-plex-sans/LICENSE.txt  |   92 +
```

So A0v **is** committed (check 2 ✅), the font drop **is** present and valid
(check 3 ✅), and the tree is clean. The observed state is strictly safer than
the expected one — the only consequence is that gate 2's "plus untracked
`web/static/vendor/fonts/ibm-plex-sans/` (4 files)" cannot appear, and the
operator's `git add` line for that path is a no-op. Continued on that basis.

`LICENSE.txt` was already in place; the `curl` fallback in §0 was not needed and
was not run.

---

## Diff and gates

```
 config/chart_theme.json             |   2 +-
 config/ui_theme.json                | 102 ++++++++++++++++++--
 config/ui_theme_corporate_blue.json | 102 ++++++++++++++++++--
 config/ui_theme_light.json          | 100 +++++++++++++++++--
 tools/ux_atlas.py                   |  11 ++-
 web/static/css/base.css             |  10 ++
 web/static/css/theme.css            | 186 ++++++++++++++++++++++++++++++++++--
 web/static/vendor/README.md         |   1 +
 web/templates/_auth_base.html       |   4 +
 web/templates/base.html             |   4 +
 10 files changed, 490 insertions(+), 32 deletions(-)
```

`theme.css`: **243 lines before → 409 after.**

| Gate | Result |
|---|---|
| 1 — artefact fresh, four tokens present, no `--pf-stepper` | ✅ `--check` exit `0`. `--ui-text-primary: #EDEDED;` ×2 (`:root` + corporate_blue; light keeps `#1A1A1A`), `--ui-space-1: 4px;` ×3, `--ui-layout-nav-width: 252px;` ×3, `--chart-font-family-0: IBM Plex Sans;` ×1, `--pf-stepper` ×0 |
| 2 — changed set | ✅ `git diff --numstat config/chart_theme.json` → `1	1	config/chart_theme.json`. Changed set is exactly the ten files above. `services/chart_specs/`, `chart_theme_light.json`, `chart_theme_print.json`, `web/static/css/components/`, `web/static/css/layout.css` → 0 hits each. Untracked: none (see check 1) |
| 3 — JSON parses | ✅ all six `ui_theme*.json` + `chart_theme*.json` load |
| 4 — theme / generator / tools suites | ✅ **60 passed in 0.45s** (DB-free) |
| 5 — `tests/services/chart_specs` | ✅ **259 passed in 420.93s (7:00)** with the dev Postgres up. Substitute proof also holds: `git diff --name-only \| grep -c 'services/chart_specs'` → `0` |
| 6 — stepper guard | ✅ **1 passed, 9 deselected in 0.28s** |
| 7 — lint / typecheck on `tools/ux_atlas.py` | ✅ `ruff check` → "All checks passed!"; `ruff format --check` → "1 file already formatted". Advisory `pyright` → **0 errors, 0 warnings, 0 informations** |
| 8 — check 11 unchanged | ✅ `106` before and after — the redefinition changes the value, not the uses |
| 9 — no new hard-coded hex in `base.css` | ✅ `0` before, `0` after (the `@font-face` block carries no colours) |
| 10 — no `confirm(` / `alert(` / `hx-confirm` added | ✅ trivially: no template body was touched. Confirmed anyway — added lines matching those three patterns: `0` |

### What landed, file by file

* **`config/ui_theme.json`** — `_version` → `1.1.0`; `background.chrome`,
  `accent.on_accent`/`logo`, `text.tertiary`, `border.soft`, `font.scale`/
  `font.weight`, `semantic.positive`/`negative`/`warn`, and the new `space`,
  `radius`, `control`, `motion`, `focus`, `layout`, `accessible` groups.
  `text.primary` `#E8304A` → `#EDEDED`, with the §3.2 `_comment` recording why.
  `_comment`, `_display_name`, `button`, the ten existing `semantic` keys and
  the whole `pf` block are untouched.
* **`config/ui_theme_light.json`, `config/ui_theme_corporate_blue.json`** — same
  leaf paths including both `_comment` lines, per §2.2 values. corporate_blue's
  `text.primary` `#4A9BD9` → `#EDEDED`; light's stays `#1A1A1A`.
  `test_all_alternative_themes_have_same_schema` passes (gate 4).
* **`config/chart_theme.json`** — line 7 only, `1	1` in numstat.
* **`web/static/css/theme.css`** — regenerated, never hand-edited. The generator
  needed **no change**: `_emit_pairs` already handled the nested dicts and the
  numeric `space` keys, exactly as §2.4 predicted. `--ui-space-1: 4px;` and
  `--ui-accessible-control-height-sm: 36px;` both emit cleanly; the two variant
  blocks gained the same `--ui-*` keys; `--chart-font-family-*` indices shifted
  by one as expected.
* **`web/static/css/base.css`** — the three `@font-face` rules after the header
  comment (now lines 9–17). The token line moved from 24 to **34**, unchanged in
  content.
* **`web/templates/base.html` / `_auth_base.html`** — the four-line preload
  block; every other line untouched; the A0v vendored `<link>`/`<script>` lines
  shifted by exactly four.
* **`tools/ux_atlas.py`** — the `document.fonts.ready` wait before
  `total, pending = 0, 0`, plus one sentence on the readiness paragraph.
  `contextlib` was already imported (line 113), so no import change.
* **`web/static/vendor/README.md`** — one row, nothing else.

### One placement judgement in `base.html`

§2.6 says to insert "immediately **before** the `theme.css` `<link>`". In
`base.html` that link (line 27) is introduced by its own two-line Jinja comment
at 25–26, so a literal reading would have stacked two comments above two links
and orphaned the "Theme tokens" comment from the link it describes. The preload
block went **above that comment** instead, so each comment stays attached to its
own link. The insertion is still exactly four lines, still ahead of every
stylesheet, and the A0v lines still shift by four. `_auth_base.html` has no such
comment, so the block sits directly above line 33 as written.

---

## Open questions

1. **Font version in the README.** The Version column reads `3.327 (font
   revision)` — the value `file(1)` reports from the `woff2` header. The
   upstream release tag could not be read: `fontTools` cannot open a `woff2`
   without the `brotli` extension, which is not in the venv, and no provenance
   note was left by the pre-fetch. §0's licence URL
   (`unpkg.com/@fontsource/ibm-plex-sans/LICENSE`) suggests the files came from
   the `@fontsource/ibm-plex-sans` package; if DC-UX-A or the operator knows the
   package version, pinning it in that cell would match how the other four rows
   are written.
2. **A stale sentence in the vendor README.** Lines 22–23 still read "The
   `fonts/` subdirectory is vendored and documented separately, as part of the
   shell's typography work" — written by A0v in anticipation of this row, and
   now redundant, since the row directly above documents it. Left in place
   because §2.8 says "Nothing else in the file changes". Two lines to delete
   whenever DC-UX-A wants them gone.
3. **`accent.on_accent` in corporate_blue is `#000000`** per §2.2's "identical to
   §2.1 for every new key", while that variant's existing `button.fg` is
   `#FFFFFF`. Black on `#4A9BD9` clears 7:1, so this is not a contrast problem —
   but the two keys now disagree about what sits on the accent, and whichever
   component layer consumes them should be told which one wins. No web route
   sets `data_theme`, so nothing renders from it today.
4. **`accessible.font_scale` has no `hero` step** (xs–xl only), by §2.1. If the
   a11y remapping in the component layer expects a `hero` override, it will fall
   through to the base `40px`.
