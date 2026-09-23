<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0c — Role-aware section catalogue

**Strand:** UX A-0 · **Date:** 2026-09-23 · **Runs after:** P-UX-A0t
(`88739c3`) · **Scope:** `web/shell.py`, `web/routes/areas.py`,
`web/routes/cmd_search.py`, two template comments, one regression test,
two web tests. No CSS, no JS, no markup beyond comments.

The catalogue in `web/shell.py` learns `owner_only`; the shell flag
`is_tenant_owner` moves from `admin_view` to `_render_area` so every
area render carries it; the sidebar's second level and `/api/cmd-search`
both filter on it. A member no longer sees two navigation entries
(Users, Market Data) that land on nothing.

---

## OPERATOR ACTION REQUIRED

### 1. Commit

All seven gates are green. One file is in the diff that the prompt's
gate 3 did not list — see §Deliberate deviations before committing.

```bash
git add web/shell.py web/routes/areas.py web/routes/cmd_search.py \
        web/templates/_partials/areas/_admin_body.html \
        tests/regression/test_section_catalogue_matches_body_partials.py \
        tests/web/test_tenant_users_routes.py tests/web/test_shell_catalogue.py \
        docs/reports/P-UX-A0c-report.md
git commit -m "feat(shell): role-aware section catalogue — owner-only sections leave the navigation level and the command search for members (UX A-0, P-UX-A0c)"
```

### 2. The dev database is bootstrapped, but has no member user

Gate 6's fixtures `TRUNCATE` on teardown (A0b report §OPERATOR ACTION 2),
so the database was emptied by the run and **re-bootstrapped afterwards**
as the prompt requires. `portfoliflow status` now reports 2 tenants and
2 users, Sentinel owner `ProcessReengineer@minathena-capital.com`,
schema head `b034_add_trade_tickets`, no pending migrations.

`bootstrap` creates an **owner** only. The §Operator browser check below
needs a member as well:

```bash
portfoliflow create-user --tenant minathena-capital \
    --email member@minathena-capital.com --roles member --password-stdin
```

### 3. The atlas baseline

Per the prompt, the post-A0b **and** post-A0c atlas baseline is taken
**after this commit**, not after A0b — A0c changes what the sidebar
renders, so a baseline taken before it would not match A0d…A0s. That run
was **not** performed here; it is the operator's, as it was at A0t.

```bash
source .venv/bin/activate
export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=…  PF_ATLAS_PASSWORD=…
python tools/ux_atlas.py --bands 1200
RUN=docs/ux/atlas/$(date +%F)      # or the suffixed folder the run prints
zip -r ~/atlas-bands-$(date +%F).zip "$RUN"/*/bands "$RUN"/*/scenes
```

Run it as the **owner**: the atlas walks every section by fragment, and
a member session would now legitimately omit two Admin sections, which
would read as missing evidence rather than as the feature working.

---

## Verify-first

| # | Check | Expected | Found | Verdict |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty | ✅ |
| 2 | `git log --oneline -1` | ends `(UX A-0, P-UX-A0t)` | `88739c3 chore(ux): …(UX A-0, P-UX-A0t)` | ✅ |
| 3 | `ls docs/reports/P-UX-A0c-report.md` | absent | absent | ✅ |
| 4 | four anchors in `web/shell.py` | 107, 137, 279, 312 | 107, 137, 279, 312 | ✅ |
| 5 | `landing: bool = False` | one hit, 124 | one hit, 124 | ✅ |
| 6 | `role-blind` | two hits | 190 (`users`), 201 (`investments`) | ✅ |
| 7 | three anchors in `areas.py` | 141, 357, 395 | 141, 357, 395 | ✅ |
| 8 | `grep -c is_tenant_owner areas.py` | 2 | 2 (378 docstring, 408 context key) | ✅ |
| 9 | `wc -l cmd_search.py`; `del session` | 96; one hit | **97**; one hit at 83 | ⚠️ reported, continued |
| 10 | `grep -c 'fetch(\|cmd-search' shell.js` | 0 | 0 | ✅ |
| 11 | `{% if is_tenant_owner %}` in `_admin_body.html` | 24, 42 | 24, 42 | ✅ |
| 12 | regression `wc -l`; two test anchors | 160; 913; 771 | **161**; 913; 771 | ⚠️ reported, continued |
| 13 | `portfoliflow status` | note counts | 2 tenants, 2 users (bootstrapped since A0t); head `b034` | ✅ |

Checks 9 and 12 are off-by-one line counts whose **anchors matched
exactly**, so under the prompt's own rule they were reported and the run
continued. Both are a trailing-newline convention difference in the
prompt author's count, not a content difference: the `del session` line
and both test definitions sit precisely where the prompt says.

This is the second issue of A0c. The first stopped on checks 1 and 2 —
P-UX-A0t was complete but uncommitted — and is recorded in
`~/P-UX-A0c-STOP.md`. Nothing was edited in that attempt; `88739c3` is
the commit that cleared it.

---

## Diff

| File | Before | After | Δ |
|---|---|---|---|
| `web/shell.py` | 398 | 459 | +61 |
| `web/routes/areas.py` | 472 | 488 | +16 |
| `web/routes/cmd_search.py` | 97 | 124 | +27 |
| `web/templates/_partials/areas/_admin_body.html` | 57 | 63 | +6 |
| `tests/regression/test_section_catalogue_matches_body_partials.py` | 161 | 318 | +157 |
| `tests/web/test_tenant_users_routes.py` | 925 | 1,041 | +116 |
| `tests/web/test_shell_catalogue.py` | 76 | 88 | +12 |

`git diff --stat`: 459 insertions, 64 deletions across seven files. **No
`.css`, no `.js`.**

### `web/shell.py`

| Change | What |
|---|---|
| `SectionMeta.owner_only: bool = False` | New field after `landing`, with the prompt's docstring paragraph — cosmetic mirroring of the route gate (ADR-0121 §6, ADR-0126), never the gate itself |
| `market-data`, `users` | The only two entries flagged `owner_only=True`; both "role-blind by construction" comments replaced by one that names the flag and the regression guard that pins it to the partial |
| `investments` | Keeps its entry for every role; the comment loses its "Listed role-blind like every catalogue entry" clause and now says plainly that it carries no flag because the list GET is session-gated |
| `sections_for(area_slug, *, is_tenant_owner)` | New, after `all_sections`. Owner → the full catalogue unchanged; anyone else → the entries without `owner_only`. A filter, never a re-sort |
| `all_sections` | Unchanged behaviour; docstring now states it is the role-blind **full** catalogue and names its two role-blind consumers (the regression guard, `section_title`) |
| `section_index_for` | Signature gains keyword-only `is_tenant_owner` with **no default**; projects `sections_for` |
| `landing_section_for` | No logic change. Docstring records the invariant the regression guard now pins: a landing section is never `owner_only` |
| `section_title` | Untouched, still role-blind — a member never renders an owner-only `<h2>` because the partial omits the section entirely |

### `web/routes/areas.py`

`_render_area` derives the flag itself from `request.state.user` and puts
`is_tenant_owner` in the base `context` alongside
`section_index_for(area_slug, is_tenant_owner=is_owner)`. Both branches —
full page and HTMX fragment with the OOB sidebar — render from that one
dict, so the second level is role-aware on a direct load and on an area
swap alike. `admin_view` keeps its local `is_owner` for the Market Data
pre-read (a DB-cost decision, not a shell one) and no longer puts the flag
in `extra_context`. Its docstring's two flag-supplying sentences now point
at `_render_area`; `_render_area`'s docstring gains the paragraph on the
flag and on why `extra_context` must not carry a competing
`is_tenant_owner` (merged after the base context, it would win the
`update` and silently overrule the derivation).

### `web/routes/cmd_search.py`

Dependency is now `user: UserDTO = Depends(get_authenticated_user)`; the
`del session` line is gone. `_build_catalogue(*, is_tenant_owner)` reads
`sections_for`; areas and actions are untouched. The module docstring no
longer claims "No database access" — see §7 Flags.

---

## What does *not* change — verified, then left alone

- **`web/static/js/shell.js`** — unchanged, and correct as it stands.
  `sections()` reads `#shell-main [data-pf-section]`
  ([shell.js:14-15](../../web/static/js/shell.js#L14-L15)); `resolve()`
  ([shell.js:37-56](../../web/static/js/shell.js#L37-L56)) matches the
  fragment against the sections actually on the page and, finding none,
  falls back to the one the server left visible
  ([:48-54](../../web/static/js/shell.js#L48-L54)). A member typing
  `/admin#users` therefore lands on Data Import with **no request and no
  console error**. There is no catalogue in the script to filter.
- **`web/static/js/section_nav.js`** — renders whatever `/api/cmd-search`
  returns; the filtering happens server-side.
- **`web/templates/_partials/sidebar.html`** — iterates `section_index`,
  which is now the filtered list. The second level renders only for the
  *active* area ([sidebar.html:55](../../web/templates/_partials/sidebar.html#L55)),
  which is what lets the tests count `data-pf-section-link` over the whole
  page and get the Admin count.
- **`_admin_body.html`** — both `{% if is_tenant_owner %}` blocks stay
  exactly where they were. Only the two Jinja comments changed (gate 4
  proves it), now pointing at `_render_area` and naming the matching
  `owner_only=True` catalogue entry.
- **`areas/_section.html`**, `base.html`, `area_fragment.html` — untouched.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/regression/…body_partials.py -q` | ✅ **10 passed in 0.23s**, DB-free; the four new tests present |
| 2 | `ruff check` · `ruff format --check` · `pyright` | ✅ All checks passed · 936 files already formatted · **0 errors, 0 warnings, 0 informations** |
| 3 | `git diff --name-only` | ⚠️ **8 files, not 7** — one addition, see §Deliberate deviations. No `.css`, no `.js` |
| 4 | changed lines in `_admin_body.html` | ✅ every one inside a `{# … #}` comment |
| 5 | banned patterns in added lines | ✅ **0** |
| 6 | DB-bound web tests | ✅ **142 passed in 257.75s**; the cmd-search tests pass **unedited** |
| 7 | `grep -c is_tenant_owner` | ⚠️ areas.py **5** (expected 3–4), shell.py **8** (≥3 ✅), cmd_search.py **4** (≥2 ✅) |

### Gate 1 — the four new regression tests

All DB-free, regex over template source, no Jinja import.

| Test | What it pins |
|---|---|
| `test_owner_only_sections_sit_inside_the_owner_conditional` | Per area, the slugs enclosed by `{% if is_tenant_owner %}` equal `{s.slug for s in all_sections(area) if s.owner_only}` — **both directions**. A flagged-but-unconditional slug hides a section a member can reach; an unflagged slug inside the conditional is the bug A0c removes |
| `test_member_index_keeps_order_and_omits_owner_only` | Per area, the member index is the owner index minus the flagged entries, nothing reordered; `admin` is exactly `data-import · providers-credentials · investments` |
| `test_landing_section_is_never_owner_only` | Per area, the entry `landing_section_for` names is not `owner_only` |
| `test_section_index_for_requires_the_role` | `section_index_for("admin")` raises `TypeError` |

The conditional scan matches `{% endif %}` **by depth**, not by first
occurrence: a single alternation regex yields `if` / `endif` / `section_slug`
in source order and a stack pairs them, so a nested `{% if %}` inside the
owner block cannot close it early and quietly shrink the compared set.
The scan also asserts the blocks balance.

### Gate 6 — the three new DB-bound tests

Appended to `tests/web/test_tenant_users_routes.py`, reusing its
`client_factory` (which seeds one owner and one member).

| Test | Member | Owner |
|---|---|---|
| `test_the_navigation_level_omits_owner_only_sections_for_a_member` | no `href="#users"`, no `href="#market-data"`; the other three present; **3** `data-pf-section-link` in the `pf-sidebar__sections` block | **5** |
| `test_the_htmx_area_fragment_navigation_is_role_aware_too` | the same with `HX-Request: true`, asserting on the OOB sidebar fragment | **5** |
| `test_cmd_search_omits_owner_only_sections_for_a_member` | `q=` yields no admin `users`/`market-data` entry; `q=users` → `sections == []` | both present; `q=users` → exactly one hit, `url == "/admin#users"` |

The four cmd-search auth tests at
`tests/web/test_section_navigation.py` 391–415 pass **without edits**, as
required — `get_authenticated_user` chains
`require_authenticated_session → require_session`, so the 303/401
contract is unchanged. Run on its own, `-k cmd_search` over that file is
**5 passed, 59 deselected** (the two auth tests plus the three catalogue
and filter tests). The owner-seeded
`test_cmd_search_returns_full_catalogue_on_empty_query` was **not**
widened: its fixture seeds `ARRAY['owner']`, so its expected count is the
owner catalogue and stays correct.

---

## Judgement calls

**The command search filters on `sections_for`, not on a second copy of
the rule.** `_build_catalogue` could have filtered `all_sections` inline
on `section.owner_only`. Routing it through `sections_for` means the
sidebar and the palette cannot disagree about what "owner-only" projects
to, which is the whole failure mode A0c exists to close — one consumer
fixed and the other left behind is how the original bug arose.

**`all_sections` keeps its name and its role-blindness.** It would have
been tidier to rename it `full_catalogue` now that a second accessor
exists. It has three callers that genuinely want every entry — the
regression guard, `section_title`, `landing_section_for` — and renaming
it would have touched test files outside this prompt's scope for no
behavioural gain. Its docstring now says plainly which it is.

**`section_index_for` keeps `landing_section_for` role-blind.** The
resolved landing is computed from the full catalogue and then compared
against the filtered list. For today's catalogue that is identical to
resolving it from the filtered list, because no landing entry is
`owner_only` — and `test_landing_section_is_never_owner_only` is what
keeps it identical. Resolving from the filtered list instead would have
silently *moved* a member's landing view if someone ever flagged a
landing section, hiding the drift rather than failing on it.

**The two DB-bound navigation tests assert on a sliced `<ul>`, not on the
whole page.** `data-pf-section-link` appears only in the second level,
and only for the active area, so a page-wide count would work today. It
would also silently start counting a second area's entries if the sidebar
ever rendered more than one expanded level. Slicing the block by hand
(rather than adding a parser dependency) matches this module's existing
substring-assertion style.

**Gate 6 was run as one invocation of all four files.** 142 tests, 4m18s.
The suite is DB-bound and the fixtures truncate, so serialising them in
one process is what keeps them from racing each other on the shared dev
database (project memory: full gate ≈2h20m, run targeted selections).

---

## Deliberate deviations

**`tests/web/test_shell_catalogue.py` is in the diff; gate 3 did not list
it.** This is the one deviation, and it is forced by the prompt's own
§1.4. That file calls `section_index_for(area)` positionally at its old
lines 55 and 66. Making the keyword mandatory with no default — which is
exactly what §1.4 asks for, so that "a caller that forgets the role must
fail at import/call time" — turns both into `TypeError`. It is a caller
that forgot the role, found by the change working as designed. The
alternatives were to give the keyword a default (defeating §1.4's stated
purpose) or to knowingly commit two red tests; both are worse than a
two-line mechanical fix. Both call sites now pass `is_tenant_owner=True`,
preserving each test's original intent — they assert on the full
catalogue — and the unknown-area test additionally asserts the empty
projection for `False`, since that path is now reachable two ways. The
file is **12 lines** larger, all of it those two call sites and the
docstring sentences explaining the role choice. `pytest
tests/web/test_shell_catalogue.py -q` → **30 passed**.

**Gate 7's `areas.py` count is 5, not the expected 3–4.** No deviation in
the work — the gate's own arithmetic under-counted what the prompt
mandates elsewhere. The floor is: §2.1's context key (1) **plus** §2.1's
`is_tenant_owner=is_owner` keyword on `section_index_for` (1) **plus**
§2.3's required docstring paragraph, which cannot state both the flag and
the competing-key hazard without naming it twice (2) **plus** §2.2's
trimmed `admin_view` docstring, which must still point at `_render_area`
(1) = 5. The count could be brought to 4 only by dropping the flag's name
from `admin_view`'s docstring, which would make that sentence vaguer for
no benefit.

---

## §7 Flags

1. **`/api/cmd-search` is now DB-bound, one primary-key read per call.**
   It was explicitly "No database access — the endpoint stays hot enough
   to respond on every keystroke"; that promise is withdrawn, and the
   module docstring now says so rather than leaving a stale claim.
   `get_authenticated_user` opens its own short `tenant_context`,
   commits and closes immediately (ADR-0065 §1b), so it holds no
   connection across the request. It is affordable because the palette
   input is debounced client-side
   ([section_nav.js:240](../../web/static/js/section_nav.js#L240)) rather
   than fired per keystroke. **No cache was added**, per the prompt: a
   cached role is a stale role, and this is an authorization-shaped
   filter. The better long-term answer is to carry the role on
   `SessionDTO`, which would remove the read entirely — that is the
   handover's §2 fact ("`SessionDTO` has no role") and is out of this
   prompt's scope. Worth a decision at DC-UX-A2: this is the first
   per-keystroke-shaped endpoint in the app to acquire a DB dependency.
2. **The `investments` entry stays in every role's index.** It is a
   pointer tile to `/investments`, whose list GET is session-gated, not
   owner-gated — so a member following it reaches a real page. Record
   §2.7.2 hides only the *write* controls there, which is **A-6's**
   business, not A0c's. Flagging it `owner_only` here would have hidden a
   surface a member can legitimately use.
3. **`landing_section_for` is role-blind by design, and the regression
   test is what keeps that safe.** It reads the full catalogue and the
   server renders that section visible. Flag a landing entry `owner_only`
   and a member's page would render without it while the shell still
   tried to land on it — no visible section at all. Nothing in the
   function prevents that; `test_landing_section_is_never_owner_only`
   does. If a future prompt ever wants an owner-only landing section,
   that guard is the thing to argue with first.
4. **The flag is cosmetic mirroring, in both new places.** The sidebar
   and the palette now *look* like an authorization boundary. They are
   not. The gates remain on the routes — `web/routes/tenant_users.py`
   (ADR-0121 §6) and `web/routes/market_data.py` (ADR-0126) — and were
   not touched. A member who types `/admin/users/section` still gets 403.
5. **`_render_area` now depends on `_resolve_user_email` having run.**
   Every area handler calls it first, so the flag is correct today, and a
   degraded render fails safe to the member catalogue. But the coupling
   is implicit — a future area handler that calls `_render_area` without
   it would silently render the member list to an owner. Nothing enforces
   the ordering; the docstring states it.

---

## Operator browser check

After `portfoliflow bootstrap`, the `create-user` from §OPERATOR ACTION 2,
and `portfoliflow-web`, with an owner and a member session in parallel
tabs at `http://minathena-capital.localhost:8000`:

- [ ] member `/admin`: second navigation level shows **three** entries
      (Data Import, Providers & Credentials, Investments); owner: **five**
- [ ] member `/admin#users` in the address bar → **Data Import** is shown,
      no error in the console, **no request** in the network tab
- [ ] member Ctrl K, type `users` → **no section hit**; owner → **one
      hit** that opens `/admin#users`
- [ ] switch area to Admin via the sidebar (HTMX swap) as member → still
      **three** entries

The first, third and fourth are covered by the three new DB-bound tests;
the browser check confirms them against a real session and a real
browser. The second is the one the tests **cannot** reach — it asserts the
absence of a request and of a console error, which is `shell.js`
behaviour in a browser, not something an ASGI test client observes. That
one is the point of this list.

---

## Handover — DC-UX-A2

Run complete, all gates green. One deviation: `tests/web/test_shell_catalogue.py`
joins the diff, because §1.4's no-default keyword made its two positional
`section_index_for` calls a `TypeError` — a caller that forgot the role,
found by the change working as intended. Gate 7's `areas.py` range was
under-counted by the prompt (5 is the floor its own §2.1–§2.3 require).
The atlas baseline is the operator's to run **after** this commit, as the
post-A0b **and** post-A0c baseline for A0d…A0s.
