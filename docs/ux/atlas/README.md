<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2025-2026 Sönke Pinkernelle
-->

# Screenshot atlas

Dated, local screenshot runs of the web UI, one full-page PNG per
user-visible GET route. Produced by `tools/ux_atlas.py` (P-UX-1) as the
visual companion to the route and element inventory under
`../inventory/`.

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
`<area>/`, `<area>/partials/` and `<area>/scenes/`.

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

python tools/ux_atlas.py
```

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
| `--area transactions` | Restrict the run to one Area; repeatable |
| `--no-scenes` | Route captures only |
| `--viewport 390x844` | A different viewport — the mobile pass |
| `--out <dir>` | Override the dated folder |
| `--routes <csv>` | Select from a different inventory table |

Exit codes: `0` clean, `1` at least one suspect capture or failed scene
(the outputs are written either way), `2` the browser or the Playwright
package is missing, `3` login failed.

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
suspect captures, failed scenes and the skipped routes by reason.

Two things are expected rather than defects:

- **Partials render without the shell chrome.** They are HTMX fragments;
  a route that returns one has no `base.html` around it. They are
  captured deliberately, filed under `<area>/partials/` and flagged in
  the manifest.
- **Non-200 routes are still captured.** An error surface is a UX
  surface, and it is flagged `suspect` so it surfaces in the contact
  sheet rather than being dropped.
