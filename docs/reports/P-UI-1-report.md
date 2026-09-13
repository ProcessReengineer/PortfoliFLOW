# P-UI-1 report — pills retired, Market Data panel styled

## Operator action required

- [ ] Browser walk: Admin → Market Data (owner): grid of four fields, dark
      controls, status meta line with inline Refresh now, stamps in the schedule
      zone; Admin, Assistants, Transactions: no section pills.
- [ ] Decide on two spots where the prompt's description of a sibling rule did
      not match the repo — see **Open questions** (D-UI-4 in particular: the
      flash is tone-only, not border + tint).
- [ ] Commit (suggested message below).

## Verify-first table (before / after)

| # | check | expected | before | after |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty ✓ | 9 modified + 1 new (below) |
| 2 | `grep -rn 'section_pill=' web/templates` | 4 hits (`_admin_body` ×2, `_assistants_body` ×1, `_transactions_body` ×1) | exactly those 4 ✓ | **0** |
| 3 | `grep -rn 'pf-section__pill' tests/` | 1 hit (`test_planning_desk.py`, `== 0`) | 1 ✓ | 4 (the precedent + 3 new assertions) |
| 4 | `ls web/static/css/components/market_data.css` | does not exist | absent ✓ | present |
| 5 | `grep -c 'css/components/' web/templates/base.html` | 19 | 19 ✓ | **20** |
| 6 | `grep -c 'pf-btn' …/market_data_panel.html` | 2 | 2 ✓ | **0** |
| 7 | `grep -c 'strftime' …/market_data_panel.html` | 2 | 2 ✓ | **0** |
| 8 | `grep -c '_display_zone(' web/routes/market_data.py` | 2 | 2 ✓ | 3 (def + poll + `_panel_context`) |
| 9 | `grep -n 'pf-btn' web/templates -r \| grep -v cases` | only check 6's 2 hits | exactly those ✓ | **nothing** |
| 10 | `grep -c 'Next refresh due\|Last run' tests/web/test_market_data_routes.py` | 0 | 0 ✓ | 3 (the new stamp assertions) |

All ten before-values matched; no STOP condition was hit.

## Files touched

- `web/templates/_partials/areas/_admin_body.html` — dropped `section_pill="live
  import"` (Market Data) and `section_pill="registry"` (Investments tile).
- `web/templates/_partials/areas/_assistants_body.html` — dropped
  `section_pill="moved"`.
- `web/templates/_partials/areas/_transactions_body.html` — dropped
  `section_pill="Draft"`. The head comment says nothing about the pill, so it
  stays as written.
- `web/static/css/components/market_data.css` — **new**. The panel's missing
  stylesheet: intro, flashes, the four-field grid, the control rules copied from
  `.pf-users__input` / `.pf-users__select`, the hint, and the status meta line
  with its separator and inline refresh control.
- `web/templates/base.html` — links the new sheet directly after
  `tenant_users.css`.
- `web/templates/_partials/market_data_panel.html` — `pf-btn` → `btn`, the three
  controls gain their block classes, the status block becomes the meta line with
  "Refresh now" as its last item, the two stamps become pre-formatted display
  strings, colons dropped. Head comment records the context change and why the
  formatting left the template.
- `web/routes/market_data.py` — `_panel_context` adds `next_due_display` /
  `last_run_display` in the schedule's own zone via the existing `_display_zone`;
  new `_STAMP_FORMAT` constant; docstring paragraph. `next_due_at` /
  `last_run_at` are untouched and still in the dict.
- `tests/web/test_market_data_routes.py` — `_seed_schedule` gains
  `timezone_name` (default `"Europe/Berlin"`, the literal it always wrote); two
  slicing helpers and five tests.
- `tests/web/test_transactions_area.py` — one pill assertion.
- `tests/web/test_shell_sidebar_and_areas.py` — one parametrized pill assertion
  (`/admin`, `/assistants`) plus a docstring bullet.

Not touched, as specified: `areas/_section.html`, the `.pf-section__pill` rule in
`layout.css`, `provider_credentials_section.html`, the composer's `tx-state` chip.

## Tests

Run: `pytest tests/web/test_market_data_routes.py tests/web/test_transactions_area.py
tests/web/test_shell_sidebar_and_areas.py tests/web/test_planning_desk.py`
→ **153 passed in 318s**, no failures, no skips.

| module | before | after | new |
|---|---|---|---|
| `test_market_data_routes.py` | 21 | 26 | +5 |
| `test_transactions_area.py` | 3 | 4 | +1 |
| `test_shell_sidebar_and_areas.py` | 64 | 66 | +2 (one test, two params) |
| `test_planning_desk.py` | 57 | 57 | — (regression only) |

"After" is `pytest --collect-only` on the working tree; "before" is that minus the
cases added here — the modified tree was never stashed to re-collect HEAD.

New test names:

- `test_the_panel_renders_controls_the_stylesheet_can_reach` — two selects, one
  input, one `btn btn--primary`, zero `pf-btn`, counted over the panel slice.
- `test_the_status_stamps_render_in_the_schedules_own_timezone` — seeded
  `Europe/Berlin` + `2026-01-15T12:00Z` renders `Last run 2026-01-15 13:00 CET`
  and not `12:00 UTC`.
- `test_an_unknown_schedule_timezone_stamps_utc_and_warns` — `Mars/Olympus`
  seeded past the save route's validation renders `… 12:00 UTC` and logs the
  existing warning, proving the fallback is the shared one.
- `test_refresh_now_is_an_item_of_the_status_line` — the form renders inside the
  status element; with `enabled=False` it is gone and the line still stands.
- `test_the_shell_links_the_market_data_stylesheet` — the `<link>` is served.
- `test_the_transactions_area_carries_no_section_pills` and
  `test_area_headers_carry_no_section_pills[admin|assistants]` — `count(…) == 0`,
  the Planning Desk precedent.

`ruff check` and `ruff format --check`: clean on all four touched Python files.
`pyright web/routes/market_data.py`: 0 errors, 0 warnings.

## D-register

- **D-UI-1 — taken as written.** The panel's two status stamps render in the
  schedule's IANA zone with `%Z`, UTC fallback self-naming, through the same
  `_display_zone` the refresh flash uses. The format is `%Y-%m-%d %H:%M %Z`, a
  module constant (`_STAMP_FORMAT`); the poll flash keeps its own `%H:%M %Z`,
  which is a different question (a landed run, not a calendar position).
- **D-UI-2 — taken as written.** "Refresh now" is the last item of the status
  meta line, inside `.pf-market-data-panel__status`, preceded by a middot
  separator, still its own `<form>`. No `form=` attribute tricks.
- **D-UI-3 — taken as written.** Colons dropped; the three empty-state sentences
  ("Next refresh scheduled.", "Not yet configured.", "No run yet.") keep their
  full stops verbatim.
- **D-UI-4 — added.** `__flash` / `__error` are **tone-only** (colour +
  `0.85rem`), not border + tint. The prompt described
  `.pf-credentials__success` as "border + tint", but that rule is colour and
  font-size only — as are the two existing `__flash` rules in `watch_desk.css`
  (`.pf-dc-cadence__flash`, `.pf-dc-calib__flash`), both at exactly the `0.85rem`
  the prompt asks for. Following the named sibling rather than the parenthetical
  keeps the panel from introducing a tinted-box flash idiom that exists nowhere
  else on the Admin surface. Overrule by reply and it is a four-line change.
- **D-UI-5 — added.** The template branches on `current.next_due_display` /
  `current.last_run_display` rather than on the `…_at` datetimes. They are
  `None` in exactly the same cases, and branching on the string is what leaves
  the template with no reference to a datetime at all (check 7 → 0).
- **D-UI-6 — added.** `_seed_schedule`'s new parameter is `timezone_name`, not
  `timezone`: the test module imports `timezone` from `datetime` and uses it
  inside that helper's own body, so the obvious name would shadow it. Same
  spelling the save route already uses for its form alias.

## Open questions / observations

- **`.pf-users__label` does not exist.** §3.1 cites it as the match for
  `__label` (`--ui-text-primary`, `0.9rem`, `600`). The nearest real rule is
  `.pf-users__field-label`, which is `--ui-text-secondary`, `0.78rem`, `600` —
  a quieter label for a control tucked into a user row. The explicit triple in
  the prompt was implemented as given, which reads as a section-level field
  label rather than a row-level one; that is the right weight for a standalone
  form, but it is *not* what the Users section renders. If the two Admin
  surfaces should match exactly, the change is on this side (three values).
- **Check 8 is now 3, not 2.** `_display_zone` gained its third textual
  occurrence, as intended — the §6 after-list does not mention check 8, noting
  it here so the next verify pass is not surprised.
- **`_seed_schedule` still hard-codes `cadence='every_15m'` and
  `preferred_hour=0`.** Only the timezone was parameterised, since only it was
  needed. `_read_schedule_shape`'s assertion in the member-gate test pins all
  four values, so a future caller changing cadence will need the same treatment.
- **The panel's `pf-market-data__panel` / `pf-market-data` wrappers (in
  `market_data_section.html`) are still unstyled** — they carry the HTMX target
  and need no box, so nothing was added for them. Named here only because a
  reader grepping for `pf-market-data` in the new sheet will not find them.
- **The Overview freshness line and this panel now state the same fact in two
  formats** — `%H:%M` there, `%Y-%m-%d %H:%M %Z` here. Deliberate (D-UI-1), but
  if the operator wants one stamp vocabulary across surfaces, that is a separate
  prompt and would touch `web/routes/overview.py`.
- The four retired pills were the last in the codebase outside
  `provider_credentials_section.html`. `.pf-section__pill` now has exactly one
  production caller; `areas/_section.html` keeps the parameter, per scope.

## Suggested commit

```
feat(ui): retire the four remaining section status pills, give the Market Data panel a stylesheet and schedule-zone stamps

Four section headers carried decorative pills that named no state — "live
import" and "registry" on Admin, "moved" on Assistants, "Draft" on
Transactions. They follow the Planning Desk's, with the same count
assertion per area page. The pill mechanism stays for the
provider-credential cards, which use it for a real per-card state.

The Market Data panel had shipped with BEM classes but no stylesheet, and
with `pf-btn` on both buttons — a mockup class no production sheet
defines — so the browser drew its own controls inside a themed page.
Adds components/market_data.css (the tenant_users.css token set), moves
"Refresh now" into the status meta line in the Overview .ov-meta idiom,
and renders both stamps in the schedule's own timezone through the
_display_zone the refresh flash already used.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
```
