# ADR-0122: Sidebar Area Order v3

Status: Accepted (2026-08-15)
Date: 2026-08-15
Supersedes: the sidebar-order decision recorded in ADR-0104 §6 (ADR-0104 remains Accepted and unedited; only its area-order statement is superseded)
Related: ADR-0089 (Watch Desk as sixth Area; its placement was already superseded by ADR-0104 §6), ADR-0107 (Cases as eighth Area; Watch Desk → Cases pairing), ADR-0046 (shell as presentational concern), ADR-0051 (Shirley embedded in Assistants area)

## Context

ADR-0104 §6 fixed the sidebar order, operator-confirmed when the Planning
Desk landed:

```
Front Office → Back Office → Watch Desk → Cases → Planning Desk →
Investor Communication → Assistants → Admin
```

The rationale read the sidebar as the working day: the book first, then the
two forward-looking surfaces grouped in the middle (the Watch Desk watches
and raises, the Planning Desk projects and simulates, with Cases between
them carrying a raised question to a documented close), then the
outward-facing and assistive areas, with Admin last.

During the pre-release UI polish pass (Chat E2 context) the operator
reviewed the information architecture as a whole and decided on a different
reading: the assistive surface belongs directly behind the book, not near
the end. Shirley is the primary interactive companion for day-to-day work
on Front Office and Back Office data, and burying the Assistants area in
seventh position undersold that role. The monitoring-and-exception workflow
(Watch Desk raising, Cases closing) moves toward the end of the list — it
is consulted on its own beat rather than navigated to constantly — while
keeping the two areas adjacent, preserving the ADR-0107 pairing.

The order is duplicated by design in two places (`web/shell.py` `_AREAS`
carrying the rationale, and the hardcoded `_areas` list in
`web/templates/_partials/sidebar.html`), with one hard order pin in the
test suite (the glyph-sequence assert in
`tests/web/test_sidebar_glyph_and_auth_polish.py`). All other consumers —
status bar label resolution, the command-search catalogue,
`tests/web/test_shell_sidebar_and_areas.py`, `tests/web/test_statusbar.py`
— derive from `all_areas()` and follow automatically.

## Decision

### §1 New sidebar order

```
Front Office → Back Office → Assistants → Planning Desk →
Investor Communication → Watch Desk → Cases → Admin
```

It reads: the book first (Front Office, Back Office), the assistant as the
standing companion right behind it, then the forward-looking planning
surface and the outward-facing communication surface, then the
monitoring-and-exception workflow (Watch Desk → Cases, adjacency
deliberately preserved per ADR-0107), with Admin last as the rare-use
configuration surface.

### §2 What does not change

* **Labels, slugs, and URLs are untouched.** This is a pure reordering.
* **Section catalogues** (`_SECTIONS_BY_AREA`) and area body partials are
  out of scope.
* **ADR-0104** remains Accepted and unedited; corrections travel in this
  successor per the immutability discipline.

### §3 Considered and rejected: renaming "Assistants" to "Shirley"

A rename of the Assistants area label to "Shirley" was considered alongside
the reorder and rejected (operator decision, 2026-08-15): the area is the
home for assistive functions in general — it already hosts the Report
Scraper and the Providers & Credentials pointer tile besides the Shirley
chat — and future assistive functions may join it. The generic label keeps
that door open. Should a rename be revisited later, it is a label-only
change (slug `assistants` and URL `/assistants` would remain), plus the
area body `<h1>`, the page `<title>`, and the label list in
`tests/web/test_auth_surface_layout.py`.

## Consequences

* `web/shell.py` — `_AREAS` tuple reordered; the order-rationale comment is
  rewritten to cite this ADR (the previous comment cited ADR-0104 §6 and
  its superseding of ADR-0089).
* `web/templates/_partials/sidebar.html` — the hardcoded `_areas` list is
  reordered identically; its header comment is updated to reference this
  ADR.
* `tests/web/test_sidebar_glyph_and_auth_polish.py` — the glyph-sequence
  pin changes from `["F", "B", "W", "C", "P", "I", "A", "A"]` to
  `["F", "B", "A", "P", "I", "W", "C", "A"]`; the accompanying order
  comment is updated.
* `tests/web/test_shell_sidebar_and_areas.py` — assertions derive from
  `all_areas()` and need no change; the stale order enumeration in its
  docstring is corrected as a comment-only edit.
* No migration, no route change, no behavioural change beyond navigation
  order.
