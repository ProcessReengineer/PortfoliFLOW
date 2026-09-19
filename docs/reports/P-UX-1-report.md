# P-UX-1 — Screenshot atlas

**Track:** UX overhaul, step 1. Runs after P-UX-2, P-UX-0b, P-UX-H1 and the
inventory re-run.
**Status:** Complete as a build. The tool, the scene format, the README, the
ignore rules and the dependency are in the tree and pass the gate. The
**baseline run is deferred to the operator** — no server, no credentials and no
browser were available in this session (checks 12–14, and Chromium is a manual
download).
**Ran:** at `002a843` (HEAD), tree clean at start.

---

## 1. Operator action block

### 1.1 Review and commit

Proposed message:

```
chore(ux): add re-runnable screenshot atlas tool and scene format; ignore atlas output (P-UX-1)
```

Nothing is staged — `git add` was not run.

### 1.2 Take the baseline (deferred — this is the part that still needs you)

The atlas output is gitignored, so this can happen at any time, before or after
the commit, and produces nothing to review in git. Run, in this order:

```bash
source .venv/bin/activate && playwright install chromium        # once; a browser download
portfoliflow-web &                                              # data-bearing dev tenant
export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
export PF_ATLAS_USER=…  PF_ATLAS_PASSWORD=…
python tools/ux_atlas.py                                        # → docs/ux/atlas/<date>/
```

Then open `docs/ux/atlas/<date>/index.md` — the visual spot-check is yours. The
contact sheet closes with a "Needs a look" section listing suspect captures,
failed scenes and the skipped routes by reason; start there.

**Preconditions.**

- The instance must carry **representative data**. Empty screens are captured
  all the same, but an empty tenant makes a poor baseline. If the web tests have
  run since the last bootstrap, run `portfoliflow bootstrap` first — they tear
  down seeded tenant data.
- `PF_ATLAS_BASE_URL` must name a **tenant host**, not bare `localhost`: tenants
  resolve by subdomain (ADR-0063 §1). Bare `localhost` works only with
  `LOCAL_DEV_TENANT_SUBDOMAIN` set in `.env`.
- `playwright install chromium` is a browser download and was deliberately not
  run in this session. Without it the tool exits 2 and prints that command.

### 1.3 Optionally add a super-admin session

Without the three admin variables, **four** routes are skipped with the reason
`no-admin-session` and the other seventeen are captured normally. To include
them:

```bash
export PF_ATLAS_ADMIN_BASE_URL=http://admin.localhost:8000
export PF_ATLAS_ADMIN_USER=…  PF_ATLAS_ADMIN_PASSWORD=…
```

Four, not ten — see F-UX-1.5; the DoD's captured/skipped arithmetic changes
accordingly.

### 1.4 Note two expected-looking alarms in the first run

Neither is a defect, and both will show in "Needs a look":

- `/admin/providers-credentials/openrouter/models` will almost certainly be
  flagged `suspect` for a sub-8 KB PNG — it renders a `<datalist>`, which is
  invisible by definition (F-UX-1.8).
- The seven **partials** render without shell chrome. That is what a partial is
  (F-UX-1.7).

### 1.5 A new dependency landed

`playwright>=1.47,<2` was added to `[project.optional-dependencies] dev` and
installed into `.venv` (resolved to 1.63.0, a 48 MB wheel). Check 10 had failed,
so §3.0 applied.

---

## 2. Verify-first

No STOP fired. All five STOP checks passed.

| # | Check | Result | Mode |
|---|-------|--------|------|
| 1 | Clean tree, predecessors | porcelain **empty**; both subjects present (grep count **2**), `002a843` at HEAD | STOP — pass |
| 2 | P-UX-0 artefacts | all four present | STOP — pass |
| 3 | `routes.csv` header | all nine named columns, **plus** trailing `js_callers` (P-UX-0b has run) | STOP — pass |
| 4 | Auth values | exactly three: `session` **150**, `super_admin` **10**, `none` **8** (168 rows) | STOP — pass |
| 5 | Eligible routes | `N_eligible` = **21**, `N_param` = **21** | REPORT |
| 6 | Login form | `<form method="post" action="/login">`; `csrf_token` (hidden), `email`, `password` | STOP — pass |
| 7 | Tenant resolution | subdomain hosts confirmed. Tenant: `http://minathena-capital.localhost:8000`. Super-admin: `http://admin.localhost:8000` | REPORT |
| 8 | Serve command | `portfoliflow-web = "web.main:run"` under `[project.scripts]` (`pyproject.toml:56`) | REPORT |
| 9 | Layouts | `base.html` ×16, `super_admin/base.html` ×3, `_auth_base.html` ×1 — as anticipated | REPORT |
| 10 | Playwright | **absent** → §3.0 ran; now 1.63.0 (see F-UX-1.6 on the check command) | REPORT |
| 11 | Dev group | `pytest`, `pytest-httpx`, `pytest-asyncio`, `ruff==0.15.12`, `pyright`, `aiogram` — as anticipated | REPORT |
| 12 | Tenant server | `PF_ATLAS_BASE_URL` **unset**, curl `000` | REPORT — §3.7 deferred |
| 13 | Super-admin server | `PF_ATLAS_ADMIN_BASE_URL` **unset** | REPORT |
| 14 | Credentials | `unset` | REPORT — §3.7 deferred |
| 15 | `.gitignore` | no `docs/ux` entry (now added) | REPORT |

Check 5's composition matches the prompt's split exactly: `/` + nine area pages
+ `/investments` + `/investments/new` + four `/super-admin/*` + five section
partials = 21.

---

## 3. Method

**Sessions.** Two headless Chromium contexts at 1440×900, device scale 1,
`color_scheme="dark"`. Each logs in through the real form (`input[name="email"]`
/ `input[name="password"]`, submit inside `form[action="/login"]`) and is then
*confirmed* by loading a guarded path and asserting HTTP 200 with a final URL
that is not `/login` — `/front-office` for the tenant, `/super-admin/tenants`
for the super-admin. A context that fails confirmation exits 3; the message
names the session and the URL and never the credentials. Routes with
`auth_required = super_admin` are driven from the admin context, everything else
from the tenant context. Credentials are read from the environment only and
appear in no file, manifest entry or log line.

**Page vs partial.** Taken from the templates, not from a list. The `template`
cell is split on `;` and each name's `{% extends %}` chain is followed (with a
cycle guard) to see whether it reaches `base.html`. `super_admin/base.html`
reaches it one hop out — verified. `_auth_base.html` does not, confirming
check 9's reading that it is a third, login-only layout; its routes are excluded
anyway. A route naming no template (`/`, a redirect) counts as a page, because
its redirect target is one. Result on the current baseline: **14 pages, 7
partials**. Partials are captured, filed under `<area>/partials/` and flagged
`partial: true`.

**HTMX-quiet wait.** `goto(wait_until="domcontentloaded")`, then
`wait_for_load_state("networkidle")`, then a poll until
`document.querySelectorAll('.htmx-request').length === 0` (5 s cap), so lazily
composed sections are in the frame. Both waits are best-effort and swallow their
timeout: a surface holding an open stream never reaches `networkidle` (F-UX-1.9),
and a lazy section that never lands is a finding for the atlas rather than a
reason to abandon the run. Motion is then frozen with the injected
`animation/transition/caret-color: none` rule so two runs of an unchanged page
are comparable.

**Tripwires.** A capture is `suspect` when the PNG is under 8 KB, when the final
URL is `/login` (the session did not carry), or on an HTTP status ≥ 400. Non-200
routes are captured regardless — an error surface is a UX surface.

**Accounting.** Every one of the 168 route rows is either captured or in
`skipped` with a reason; the two sets were verified to sum to 168 under both
admin-session settings. Reasons are first-match in the order `method` → `param`
→ path prefix (`api` / `static` / `framework` / `auth-none`) → `auth-none` →
`no-admin-session`.

---

## 4. Baseline numbers — deferred

No screenshots were taken: no reachable server (checks 12–13), no credentials
(check 14), and Chromium not installed. What *was* verified, by exercising the
browser-independent code paths against the real `routes.csv` and the real
templates:

| Quantity | With admin session | Without |
|---|---|---|
| Eligible (would be captured) | **21** | **17** |
| — pages | 14 | 12 |
| — partials | 7 | 5 |
| Skipped `method` (not GET) | 79 | 79 |
| Skipped `param` | 21 | 21 |
| Skipped `api` | 40 | 40 |
| Skipped `framework` | 6 | 6 |
| Skipped `auth-none` | 1 | 1 |
| Skipped `no-admin-session` | 0 | **4** |
| **Total accounted** | **168** | **168** |

Scenes: 1 defined (`transactions-flow-chooser`). Atlas path would be
`docs/ux/atlas/2026-09-18/`. `git_head` would record `002a843`.

**Exit codes demonstrated** (the DoD's "dry invocation"):

- Exit **3** — `PF_ATLAS_BASE_URL` unset: *"No tenant base URL. Pass --base-url
  or export PF_ATLAS_BASE_URL (a tenant host, …)"*.
- Exit **2** — browser missing: *"The Chromium browser Playwright drives is not
  installed. Install it once with: … playwright install chromium"*.

A run that dies on either leaves **no** dated folder behind — the output
directory is resolved early but not created until a session exists, so a failed
run cannot shift the next run's `-2` suffix.

**Gate:**

```
ruff check tools/ux_atlas.py          → All checks passed!
ruff format --check tools/ux_atlas.py → 1 file already formatted
pyright tools/ux_atlas.py             → 0 errors, 0 warnings, 0 informations
```

**Ignore rules verified** with a throwaway `docs/ux/atlas/1999-01-01/` run:
`manifest.json` and a nested PNG were both matched by `.gitignore:42`,
`git status` stayed unchanged, and `git add -n docs/ux/atlas/` resolves to
exactly `add 'docs/ux/atlas/README.md'`.

---

## 5. Findings

Numbered from **.5**: F-UX-1.1 – F-UX-1.4 were raised by the stopped v3 run and
are already folded into the v4 prompt, so reusing those numbers would collide.

**F-UX-1.5 — Only 4 of the 10 `super_admin` routes are eligible, so the
`no-admin-session` skip count is 4, not 10.** §3.7 and the DoD both anticipate
"the ten `super_admin` routes". Ten rows carry that guard, but six are POST or
carry a path parameter and are skipped earlier under `method` / `param`. The
four that reach the admin gate are `/super-admin/tenants`,
`/super-admin/tenants/partial`, `/super-admin/users`, `/super-admin/users/partial`.
Verified both ways: eligible is 21 with an admin session and 17 without. The DoD
arithmetic reads *captured = 21 − 4 = 17* when the admin variables are unset.
No code change needed; the manifest states the reason per route.

**F-UX-1.6 — `import playwright; playwright.__version__` raises
`AttributeError`, so check 10's command reports "missing" even when the package
is installed.** The `playwright` package exposes no `__version__` attribute. The
check is still sound as a *failure* signal (ImportError when genuinely absent),
but its success path never worked. Use
`python -c "import importlib.metadata as m; print(m.version('playwright'))"`, or
`from playwright.sync_api import sync_playwright`. The installed version is
**1.63.0** — inside the prompt's `>=1.47,<2` pin, though the pin was written
when 1.47 was current and now admits a release sixteen minors newer. Worth
confirming at the first real run that nothing in the sync API drifted.

**F-UX-1.7 — Seven of the 21 eligible routes are partials, and two of them are
inside the prompt's "four `/super-admin/*`" rather than its "five section
partials".** The partials are three under `/admin` (`users/section`,
`providers-credentials/section`, `providers-credentials/openrouter/models`), two
under assistants (`/chat/history`, `/scraper/section`), and
`/super-admin/tenants/partial` and `/super-admin/users/partial`. The prompt's
"five" counts only the non-super-admin ones; the split is consistent, but the
1 + 9 + 2 + 4 + 5 grouping is by URL family, not by page/partial. The
page/partial split is 14/7. All seven will render without shell chrome — an
expected observation, not a defect, and the reason the report calls it out ahead
of the operator's first look.

**F-UX-1.8 — `/admin/providers-credentials/openrouter/models` will trip the 8 KB
tripwire as a false positive.** Its template renders a `<datalist>` (invisible by
definition) plus one hint span and a button, and it takes `datalist_id` /
`slot_id` / `reload_url` from query parameters that a bare atlas visit does not
supply — so it renders its degraded state. Expected `suspect` on the first run.
Left as-is deliberately: suppressing it would mean teaching the tool about one
specific route, and a tripwire that is explained is better than one that is
special-cased.

**F-UX-1.9 — `networkidle` is unreachable on any surface holding an open
stream.** The assistants chat uses SSE. Playwright's `networkidle` waits for 500
ms of no network activity, which an open `EventSource` never yields, so
`/assistants` will spend the full 10 s timeout before capturing. Handled — the
wait is best-effort and the capture proceeds — but it makes the assistants
captures the slowest in the run, and if a future surface opens a stream that
mutates the DOM after the timeout the screenshot may catch it mid-composition.
Flagged for the first real run: compare `/assistants` against the live page.

**F-UX-1.10 — `/` is attributed to the `assistants` Area in `routes.csv`, so its
screenshot files under `assistants/index.png`.** The root route redirects and
renders no template of its own, so the inventory's Area propagation had no
template edge to work from and landed it in `assistants`. The atlas records
`final_url`, so the manifest will show where it actually lands, but the file sits
in the wrong Area folder on the contact sheet. This is an inventory attribution
artefact, not an atlas bug — fixing it belongs to `tools/ux_inventory.py`, and
it is a one-route cosmetic issue. Recorded rather than fixed, since §5 puts
route changes out of scope.

**F-UX-1.11 — `routes.csv` is a static artefact; a drift check exists but is not
enforced.** The manifest records `routes_csv_sha256` and `git_head`, so a run can
be traced to the route table and commit it photographed. Nothing compares the CSV
against the live server, so a route added since the last inventory re-run is
invisible to the atlas and a route deleted since would be captured as a 404 (and
flagged `suspect`). The second case self-reports; the first does not. Re-run the
inventory before the atlas whenever routes have moved — the ordering D-UX-1
already prescribes.

---

## 6. Open questions

**OQ-1.** Should the atlas gain a second, mobile-viewport pass as a standing part
of the baseline, or stay a deliberate `--viewport 390x844` re-run? The flag
exists and the output folder is per-run, so a mobile atlas would need its own
folder or a filename suffix; the latter is a small change to `route_slug` and is
better decided before the first baseline than after.

**OQ-2.** Should the four super-admin routes be part of the standing baseline?
They need a second set of credentials and a second `/etc/hosts` entry for one
page, one partial and their two siblings. If the answer is no, the three admin
variables can be dropped from the documented sequence and the routes stay
permanently `no-admin-session`.

**OQ-3.** The tenant session runs as one role. Tenant roles are `owner`, `member`
and `auditor` (ADR-0063 §2), and an auditor's read-only surface is visibly
different. Is a per-role atlas strand A's "role question", or should the tool
grow a `--role` pass that logs in as several users and writes parallel folders?
The tool is structured for it — sessions are a dict keyed by name — but the
decision is a UX-track one, not a tooling one.

**OQ-4.** The 21 parameterised GET routes are skipped as `param`, which excludes
every detail surface — an investment's page, a case's timeline, a trade ticket.
Those are among the most design-sensitive screens in the app. Should scenes cover
them by clicking through from a list page (robust, slow), or should the tool grow
a way to bind a parameter to a sample id (fast, but the id is tenant-specific and
would have to live in the scenes file)?

**OQ-5.** `suspect` currently mixes three quite different conditions — a tiny
PNG, an HTTP error, and a lost session. The last is a run-level failure (if one
route bounces to `/login`, the session has probably expired for all subsequent
ones) while the first is often benign (F-UX-1.8). Should a `/login` bounce abort
the run and exit 3 rather than flagging one route?

---

## 7. Files touched

| File | Change |
|---|---|
| `tools/ux_atlas.py` | new — the tool |
| `docs/ux/atlas-scenes.json` | new — scene format, one worked example |
| `docs/ux/atlas/README.md` | new — regeneration, the six variables, scene format, how to read a run |
| `docs/reports/P-UX-1-report.md` | new — this report |
| `pyproject.toml` | edited — `playwright>=1.47,<2` in the `dev` extra (check 10 failed) |
| `.gitignore` | edited — `docs/ux/atlas/*` + `!docs/ux/atlas/README.md` |
| `docs/ux/README.md` | edited — appended `## Screenshot atlas` |

`git status --porcelain` at the end:

```
 M .gitignore
 M docs/ux/README.md
 M pyproject.toml
?? docs/reports/P-UX-1-report.md
?? docs/ux/atlas-scenes.json
?? docs/ux/atlas/
?? tools/ux_atlas.py
```

`docs/ux/atlas/` is listed only because it holds the force-included
`README.md`; dated run folders inside it are ignored, as verified in §4.
