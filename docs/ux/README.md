# `docs/ux/` — UX overhaul track working area

This folder is the working area of the UX overhaul track: the successor to the
UI polish pass, and a separate concern from it. Where the polish pass improved
how the surface *looks*, this track works on what the surface *says* — the
vocabulary, the labels, the element inventory per Area, and what the overhaul
changes about them. Everything here is evidence and working material, not
architecture: decisions that come out of the track belong in an ADR, and the
work itself belongs on `docs/roadmap.md`. The inventory under `inventory/` is
the "before" the track will be measured against, produced at step P-UX-0.

Regenerate the inventory from a clean checkout with:

    source .venv/bin/activate
    python tools/ux_inventory.py --template-root web/templates --out docs/ux/inventory

The tool is standard-library only and re-runnable — it is the before/after
instrument for the whole track, so run it again after every Area's overhaul and
diff the summary. `--no-import` forces the static route fallback for when the
application will not import (no database, missing environment); the element
inventory is identical either way, only the route table is smaller. The tool
exits 1 if any template raised a parse error, and writes its outputs regardless.

Since P-UX-0b the tool also reads JavaScript, because a route whose only
trigger is a script is invisible to a template-only pass. It scans every
`web/static/js/*.js` and every inline `<script>` block, extracts the URL of each
`fetch` / `htmx.ajax` / `EventSource` / `XMLHttpRequest` / `location` call and
each `hx-*` attribute set from script, matches it to a route, and — by looking
back from the call to the binding that reaches it — links it to the template
element whose handler makes the call. Endpoints reached through a local wrapper
(`fetchJson(url, …)`) are resolved at the wrapper's call sites, and a URL read
out of a `data-*` attribute is followed to the template that writes it. The
resulting rows carry `source_kind = js`; `routes.csv` gains a `js_callers`
count. `--js-off` skips the pass entirely and reproduces the pre-JS artefacts
byte-for-byte, which is the guard that the pass stays purely additive.

The three files under `inventory/` — `elements.csv`, `routes.csv` and
`summary.md` — are **generated artefacts. Do not hand-edit them.** A correction
to what they contain is a change to `tools/ux_inventory.py` followed by a
re-run; an edit made in place is lost on the next run and, worse, makes the
before/after diff lie. `summary.md` documents the heuristics it applies
(distinct-noun counting, the flag definitions) in the file itself, so the
numbers can be read without the tool source at hand.
