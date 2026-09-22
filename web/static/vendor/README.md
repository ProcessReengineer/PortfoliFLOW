# Vendored third-party assets

Every third-party asset the web shell loads is served from this tree, not
from a CDN — the local vendor bundle ADR-0037 §9 foresees. Versions stay
pinned by directory name, so an upgrade is a new directory plus the
`<script>`/`<link>` change in `web/templates/base.html`, reviewable as one
diff.

The bundle is tracked in git on purpose: a checkout is deployable without
a package manager, and nothing about a render depends on a third party
being reachable. It is excluded from `repomix.config.json` so repository
slices do not carry several megabytes of minified JavaScript.

| Package | Version | Files | Licence | Upstream |
|---|---|---|---|---|
| htmx | 1.9.12 | `htmx-1.9.12/htmx.js` (162 KB, unminified reference), `htmx-1.9.12/htmx.min.js` (48 KB, loaded), `htmx-1.9.12/ext/sse.js` (10 KB) | BSD Zero Clause — `htmx-1.9.12/LICENSE` | <https://unpkg.com/htmx.org@1.9.12/dist/> |
| Plotly.js | 2.35.2 | `plotly-2.35.2/plotly.min.js` (4.4 MB) | MIT — `plotly-2.35.2/LICENSE` | <https://cdn.plot.ly/plotly-2.35.2.min.js> |
| Tabulator | 5.6.1 | `tabulator-5.6.1/tabulator.min.js` (410 KB), `tabulator-5.6.1/tabulator.min.css` (26 KB) | MIT — `tabulator-5.6.1/LICENSE` | <https://unpkg.com/tabulator-tables@5.6.1/dist/> |
| Lucide icons | 1.47.0 (`lucide-static`) | `lucide/*.svg` — 38 files, the subset the shell uses; see `lucide/MANIFEST.md` | ISC — `lucide/LICENSE` | <https://lucide.dev> |
| IBM Plex Sans (web font) | 3.327 (font revision) | `fonts/ibm-plex-sans/IBMPlexSans-Regular.woff2` (62 KB), `fonts/ibm-plex-sans/IBMPlexSans-Medium.woff2` (65 KB), `fonts/ibm-plex-sans/IBMPlexSans-SemiBold.woff2` (66 KB) | OFL 1.1 — `fonts/ibm-plex-sans/LICENSE.txt` | <https://github.com/IBM/plex> |

The `fonts/` subdirectory is vendored and documented separately, as part
of the shell's typography work.

## Provenance

The htmx files are byte-identical to the CDN copies they replace: their
SHA-384 digests match the `integrity=` attributes that `base.html` carried
before the switch, which is the check that proved no substitution happened
in transit. Plotly and Tabulator carry their own version banners in the
first bytes of the bundle. Re-verify at any time with:

    openssl dgst -sha384 -binary web/static/vendor/htmx-1.9.12/htmx.min.js | openssl base64 -A
    # ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2
    openssl dgst -sha384 -binary web/static/vendor/htmx-1.9.12/ext/sse.js | openssl base64 -A
    # OZrRw8/Zvv0VFGJJF6TN3gABVZvvO60Xz7RWkQKAmoRe+t1dc5J/ySJvXbpL+N+Q

`base.html` no longer sets `integrity=`: SRI guards bytes fetched over the
network, and these are read from the same checkout as the template.

## One runtime caveat

Plotly's own default for `topojsonURL` is `https://cdn.plot.ly/`, consulted
only when a geo trace renders. No chart spec under `services/chart_specs/`
emits one — geographic allocation is drawn as a treemap — so nothing here
reaches the network. A future geo chart would need
`Plotly.setPlotConfig({topojsonURL: "/static/vendor/plotly-2.35.2/"})` and
the matching topojson files vendored beside it.
