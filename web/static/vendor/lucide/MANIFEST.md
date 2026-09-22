# Lucide icon set — vendored subset

**Package:** `lucide-static`
**Release:** v1.47.0
**Licence:** ISC — full text in `LICENSE` beside this file.
**Upstream:** <https://lucide.dev> · npm `lucide-static`

Only the icons the web shell needs are vendored, not the full set. Each
file is the unmodified upstream SVG; `web/icons.py` reads the markup
between the `<svg>` tags and rebuilds the wrapper with the project's own
geometry (16 px, 1.5 px stroke, `currentColor`) and accessibility
attributes. The upstream `package.json` is kept beside the icons so the
release above can be re-read without consulting the lockfile.

Templates address an icon by its **product name** through the `pf_icon`
Jinja global, never by file name, so an upstream rename is absorbed in
`ICONS` rather than at every call site. To add an icon: vendor the file
here and add one row below and in `web/icons.py`.

## Areas

Sidebar order per ADR-0122 §1, plus the platform-operations surface.

| Product name | Lucide stem |
|---|---|
| `front-office` | `layout-dashboard` |
| `back-office` | `landmark` |
| `assistants` | `bot` |
| `planning-desk` | `calendar-range` |
| `investor-communication` | `megaphone` |
| `watch-desk` | `eye` |
| `cases` | `folder-open` |
| `transactions` | `arrow-left-right` |
| `admin` | `settings` |
| `platform-admin` | `shield` |

## Shell chrome

| Product name | Lucide stem |
|---|---|
| `search` | `search` |
| `nav-collapse` | `panel-left-close` |
| `nav-expand` | `panel-left-open` |
| `sign-out` | `log-out` |
| `shirley` | `message-square` |
| `stage` | `maximize-2` |
| `dock` | `minimize-2` |
| `close` | `x` |
| `new` | `plus` |
| `attach` | `paperclip` |
| `voice` | `mic` |
| `send` | `send-horizontal` |
| `back` | `chevron-left` |
| `forward` | `chevron-right` |
| `expand` | `chevron-down` |
| `collapse` | `chevron-up` |
| `done` | `check` |
| `warn` | `triangle-alert` |
| `block` | `octagon-alert` |
| `info` | `info` |
| `menu` | `ellipsis` |
| `refresh` | `refresh-cw` |
| `external` | `external-link` |
| `filter` | `filter` |
| `sort-asc` | `arrow-up` |
| `sort-desc` | `arrow-down` |
| `calendar` | `calendar` |
| `download` | `download` |

38 icons in total. Four of them were renamed upstream before this
release; v1.47.0 ships the newer stem in every case, so `ICONS` records
`send-horizontal`, `triangle-alert`, `octagon-alert` and `ellipsis`
rather than the older `send`, `alert-triangle`, `alert-octagon` and
`more-horizontal`.
