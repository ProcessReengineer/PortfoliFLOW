#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Re-runnable screenshot atlas of every user-visible GET route in the web UI.

The second instrument of the UX overhaul track (P-UX-1), downstream of the
inventory (P-UX-0, ``tools/ux_inventory.py``). Where the inventory says *what
exists*, the atlas says *what it looks like*: it logs into a running instance,
visits one route at a time and writes one full-page screenshot per route into a
dated, per-area folder with a manifest and a contact sheet.

Route selection is not hand-maintained — it is read from the inventory's
``routes.csv``, so a route added to the app appears in the next atlas run
without touching this file. A route is captured when it is a ``GET``, carries no
path parameter, is not an API/static/framework path, and is guarded by a
session (``session`` or ``super_admin``). Everything else lands in the
manifest's ``skipped`` list with a reason, so the atlas accounts for the whole
route table rather than silently narrowing it.

Pages and partials
------------------
A route is a **page** when any template it renders transitively ``{% extends %}``
``base.html`` (``super_admin/base.html`` does, one hop further out); otherwise it
is a **partial**. Partials are captured too — they are HTMX fragments, so they
render without the shell chrome, and seeing that unstyled is the point rather
than a defect. They land in ``<area>/partials/`` and are flagged in the
manifest. A route that renders no template of its own (``/``, a redirect) counts
as a page: the redirect target is one.

Scenes
------
Sub-surfaces that only exist after a click — a wizard step, an opened composer —
are unreachable by URL. They are captured through ``docs/ux/atlas-scenes.json``,
a list of scene objects:

.. code-block:: json

    {"name": "transactions-flow-chooser", "area": "transactions",
     "start": "/transactions", "session": "tenant",
     "steps": [{"wait": "#tx-composer-host"}],
     "shot": "flow-chooser"}

``session`` is ``tenant`` or ``super_admin``. Each step carries exactly one verb:
``click`` (selector), ``fill`` (``{"selector": …, "value": …}``), ``wait``
(selector becomes present) or ``wait_ms`` (integer pause). A step whose selector
never appears marks the scene ``failed`` with the failing step index and the run
continues to the next scene. This file ships with one worked example; filling it
out for the real sub-surfaces is strand A's job.

Credentials
-----------
Read from the environment only — ``PF_ATLAS_USER`` / ``PF_ATLAS_PASSWORD`` for
the tenant session and, optionally, ``PF_ATLAS_ADMIN_USER`` /
``PF_ATLAS_ADMIN_PASSWORD`` for a super-admin session on the ``admin`` subdomain.
They never reach a file, a manifest entry, a log line or an error message. With
no admin session configured, ``super_admin`` routes are skipped with the reason
``no-admin-session``.

Because tenants resolve by host subdomain (ADR-0063 §1), the base URL must name
a tenant host — ``http://minathena-capital.localhost:8000``, not bare
``localhost`` unless ``LOCAL_DEV_TENANT_SUBDOMAIN`` is set.

Usage
-----
    source .venv/bin/activate
    playwright install chromium            # once; a browser download, not a package
    export PF_ATLAS_BASE_URL=http://minathena-capital.localhost:8000
    export PF_ATLAS_USER=… PF_ATLAS_PASSWORD=…
    python tools/ux_atlas.py

Revealing, truncation and bands
-------------------------------
Most Areas load their heavy Sections on ``hx-trigger="revealed"``, which fires
on intersection with the viewport. A page rendered without ever scrolling is
therefore a column of "Loading…" placeholders below the fold, whatever
``full_page`` does afterwards. Every shot is preceded by a reveal pass that
walks the document to the bottom in sub-viewport steps, waiting for HTMX after
each, until the page stops growing — then back to the top.

Scroll geometry alone is not enough for two things it cannot see. A Section
that arrives on ``revealed`` may itself hold per-item loaders on the same
trigger, which did not exist when the walk passed their eventual position, so
the walk is followed by a loader-driven loop that takes the unfired loaders
themselves as its work list and re-reads the DOM after each round. And Plotly
draws asynchronously from an inline script after the swap, long after
``.htmx-request`` has gone, so the shutter waits on Plotly's own
``.js-plotly-plot`` marker rather than on HTMX. A last sweep counts anything
still reading "Loading …" — the backstop for whatever neither wait knows about.

A revealed page gets long, and two things follow. Chromium refuses a screenshot
surface past 16,384 px and returns a silently truncated image, so the PNG's own
IHDR height is compared against the final ``scrollHeight``. And a 10,000 px tall
PNG is unreadable the moment a chat downscales it to ~1,568 px on the long edge,
so ``--bands 1200`` cuts the same full-page render into viewport-wide slices
with Playwright's ``clip``. Bands are the format to upload to a chat; the full
PNG is the format for a human with a viewer.

Output goes to ``docs/ux/atlas/<YYYY-MM-DD>/`` (gitignored — the atlas is a
local, regenerable artefact, never a committed one).

Exit codes: 0 clean; 1 at least one suspect capture, flagged scene shot or
failed scene (the outputs are written either way); 2 the Chromium browser or the
Playwright package is missing; 3 login failed, or a session lost mid-run — that
aborts the remaining routes, because every one of them would photograph the
login page, but the manifest and contact sheet are still written with what the
run already had.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from playwright.sync_api import Page

# Imported defensively so that ``--help`` and the argument parsing still work in
# a checkout where the dev extra has not been installed; the capture path turns
# a missing package into the same exit 2 as a missing browser.
playwright_api: ModuleType | None
try:
    import playwright.sync_api as _playwright_api

    playwright_api = _playwright_api
except ImportError:  # pragma: no cover - exercised only on a bare checkout
    playwright_api = None

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = REPO_ROOT / "web" / "templates"
DEFAULT_ROUTES = REPO_ROOT / "docs" / "ux" / "inventory" / "routes.csv"
DEFAULT_SCENES = REPO_ROOT / "docs" / "ux" / "atlas-scenes.json"
DEFAULT_OUT_PARENT = REPO_ROOT / "docs" / "ux" / "atlas"

#: The layout every full page reaches, directly or through one more hop.
ROOT_LAYOUT = "base.html"

#: Session-guard values that mean "a logged-in human can see this".
CAPTURABLE_AUTH = ("session", "super_admin")

#: Path prefixes that are never user-visible surfaces, mapped to their manifest
#: skip reason. ``/login`` is excluded because it is the door, not a room — and
#: its own guard is ``none`` anyway.
PATH_SKIPS: tuple[tuple[str, str], ...] = (
    ("/api/", "api"),
    ("/static/", "static"),
    ("/docs", "framework"),
    ("/redoc", "framework"),
    ("/openapi", "framework"),
    ("/health", "framework"),
    ("/favicon", "framework"),
    ("/login", "auth-none"),
)

DEFAULT_VIEWPORT = (1440, 900)

#: Kills motion so two runs of the same unchanged page are byte-comparable.
STILLNESS_CSS = (
    "* { animation: none !important; transition: none !important; "
    "caret-color: transparent !important; }"
)

NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_TIMEOUT_MS = 10_000
HTMX_QUIET_TIMEOUT_MS = 5_000
STEP_TIMEOUT_MS = 5_000

#: A full-page PNG smaller than this is almost certainly an error card or an
#: empty shell rather than a rendered surface.
SUSPECT_MIN_BYTES = 8 * 1024

#: Chromium refuses a screenshot surface past this on either axis and hands back
#: a silently cut image rather than an error.
CHROMIUM_MAX_AXIS_PX = 16_384

#: A PNG may round a fractional CSS pixel; a bigger shortfall than this is a cut.
TRUNCATION_TOLERANCE_PX = 2

TRUNCATION_REASON = f"page taller than Chromium's {CHROMIUM_MAX_AXIS_PX:,} px cap — use bands"

SESSION_LOST_REASON = "session lost — run aborted"

#: The reveal pass steps by less than one viewport because ``revealed`` fires on
#: intersection with it: a full-viewport step can jump a short section clean over
#: the observer and leave it a placeholder.
REVEAL_STEP_RATIO = 0.8

#: The guard against a page that never stops growing. Sized from measurement,
#: not from the 16,384 px cap: Chromium 153 does not enforce that cap, so a
#: revealed Area page can genuinely run past it, and at a 720 px step this walks
#: ~43,000 px. A reveal that hits the guard is reported rather than assumed
#: complete — see ``REVEAL_INCOMPLETE_REASON``.
REVEAL_MAX_ITERATIONS = 60

#: Shorter than the settle after a navigation: the reveal loop pays this once per
#: iteration, and a surface holding an open stream never reaches networkidle at
#: all, so the full timeout would be spent waiting for something that cannot come.
REVEAL_NETWORK_IDLE_TIMEOUT_MS = 2_000

#: Lets the last lazy swap paint before the shutter.
REVEAL_QUIET_MS = 250
REVEAL_INCOMPLETE_REASON = (
    f"reveal hit the {REVEAL_MAX_ITERATIONS}-iteration guard — the foot of the page "
    "may still be placeholders"
)

#: How long ``.htmx-request`` must stay at zero before the page counts as quiet.
#: A single-sample check calls the gap between a parent swap landing and the
#: nested request it triggers (HTMX's 20 ms settle delay, then the ``revealed``
#: re-check) "quiet", and the reveal pass walks on past a section that has not
#: finished arriving.
QUIET_HOLD_MS = 400

#: Poll interval of the debounced quiet wait.
QUIET_POLL_MS = 50

#: Marks a loader the page has actually dispatched. Set from an init script
#: listening on ``htmx:beforeRequest``, so it is in place before the first
#: above-the-fold loader fires; without it a loader that swaps ``innerHTML``
#: keeps its ``hx-trigger`` attribute and would read as unfired forever.
FIRED_ATTR = "data-pf-atlas-fired"

#: A monotonic count of dispatched requests, kept on ``window`` by the same
#: init script. The attribute alone cannot say whether a *round* achieved
#: anything — an ``outerHTML`` swap takes its marked element away with it — and
#: a round that fires nothing is a round every later round will repeat.
FIRED_TALLY = "__pfAtlasFired"

#: Installed on the context, so it is present from document start on every page
#: the session opens. Capture phase, so a listener that stops propagation
#: cannot hide a request from it.
LOADER_MARKER_SCRIPT = f"""
window.{FIRED_TALLY} = 0;
document.addEventListener("htmx:beforeRequest", function (evt) {{
    var el = evt.target;
    if (el && el.setAttribute) el.setAttribute({FIRED_ATTR!r}, "1");
    window.{FIRED_TALLY} = (window.{FIRED_TALLY} || 0) + 1;
}}, true);
"""

#: What counts as on-screen, for both the loader work list and the placeholder
#: tripwire. ``getClientRects()`` alone is not enough: Chromium lays out the
#: contents of a collapsed ``<details>`` under ``content-visibility: hidden``
#: and hands back a rect for something no reader can see. ``checkVisibility``
#: knows about that; the rect count is the fallback for an engine without it.
VISIBLE_FN = """
    function pfAtlasVisible(el) {
        if (typeof el.checkVisibility === "function") return el.checkVisibility();
        return el.getClientRects().length > 0;
    }
"""

#: An ``hx-trigger="revealed"`` element that has not dispatched its request.
#: ``*=`` because a trigger may list more than one event.
UNFIRED_LOADER_SELECTOR = f'[hx-trigger*="revealed"]:not([{FIRED_ATTR}="1"])'

#: The loader-driven reveal re-reads the DOM after each round because a section
#: that arrives on ``revealed`` may itself contain per-item loaders on the same
#: trigger (``charts_section.html`` → ``/api/charts/investment/{id}``). Twelve
#: rounds is far past the two levels the tree actually nests.
MAX_REVEAL_ROUNDS = 12

UNFIRED_LOADERS_REASON = "unfired lazy loaders"

#: Every Plotly render path in the tree, as one selector. The conventions are
#: not uniform — ``.pf-plotly-target`` with ``data-spec`` (overview, limits,
#: portfolio review), ``.pf-plotly-target`` with ``data-plotly-spec``
#: (benchmarks), ``.plotly-target`` with ``data-spec`` (per-investment charts,
#: statistics, portfolio analysis), ``.plotly-target`` with the spec inlined in
#: its script (SAA), and ``[data-pf-chart-plot]`` (``chart_snapshot.js``) — so
#: the union is what makes the wait cover them all rather than the three the
#: first draft of this check knew about.
CHART_TARGET_SELECTOR = ".pf-plotly-target, .plotly-target, [data-pf-chart-plot]"

#: Plotly stamps this class on the container it draws into. It is the one
#: marker every path shares: the ``data-pf-rendered`` flags are per-template
#: conventions, and ``chart_snapshot.js`` sets its own *before* the async draw,
#: so neither proves a figure is on screen.
PLOTLY_DRAWN_SELECTOR = ".js-plotly-plot, .main-svg"

#: Plotly draws asynchronously from an inline script after the swap, long after
#: ``.htmx-request`` has gone. Generous, because it is only ever paid in full on
#: a page where a chart genuinely never lands.
CHART_TIMEOUT_MS = 15_000
CHART_POLL_MS = 250

CHARTS_PENDING_REASON = "charts not drawn"

#: A lazy placeholder's own text: "Loading charts…", "Loading {name}…". The
#: templates write ``&hellip;``, which reaches the DOM as U+2026. The pattern is
#: handed to the browser as-is (``new RegExp``), so the pure test of it below
#: tests what actually runs.
LOADING_PLACEHOLDER_PATTERN = r"^Loading\b.*\u2026$"
LOADING_PLACEHOLDER_RE = re.compile(LOADING_PLACEHOLDER_PATTERN)

#: The band height that survives a chat's ~1,568 px downscale. ``--bands``
#: defaults to off; this is the number to pass when it is on.
RECOMMENDED_BAND_PX = 1200

EXIT_OK = 0
EXIT_SUSPECT = 1
EXIT_NO_BROWSER = 2
EXIT_LOGIN_FAILED = 3

INSTALL_HINT = (
    "The Chromium browser Playwright drives is not installed. Install it once with:\n"
    "    source .venv/bin/activate && playwright install chromium"
)
PACKAGE_HINT = (
    "The Playwright package is not installed. Install the dev extra with:\n"
    '    source .venv/bin/activate && pip install -e ".[dev]"'
)


@dataclass
class RouteRow:
    """One row of the inventory's ``routes.csv``, reduced to what the atlas needs."""

    area: str
    methods: str
    path: str
    template: str
    auth_required: str


@dataclass
class Capture:
    """One screenshot attempt, whatever its outcome."""

    area: str
    path: str
    file: str
    status: int | None
    final_url: str
    partial: bool
    session: str
    suspect: bool = False
    reason: str | None = None
    reveal_iterations: int | None = None
    #: ``True`` until a reveal pass says otherwise, so a capture that never got
    #: as far as revealing is not reported as under-revealed.
    reveal_complete: bool = True
    scroll_height: int | None = None
    png_height: int | None = None
    truncated: bool = False
    reveal_rounds: int | None = None
    loaders_left: int | None = None
    loaders_hidden: int | None = None
    charts_total: int | None = None
    charts_pending: int | None = None
    loading_placeholders: int | None = None
    bands: list[str] = field(default_factory=list)


@dataclass
class SceneResult:
    """One scene walk, whatever its outcome."""

    name: str
    file: str | None
    ok: bool
    failed_step: int | None = None
    reason: str | None = None
    reveal_iterations: int | None = None
    reveal_complete: bool = True
    scroll_height: int | None = None
    png_height: int | None = None
    truncated: bool = False
    reveal_rounds: int | None = None
    loaders_left: int | None = None
    loaders_hidden: int | None = None
    charts_total: int | None = None
    charts_pending: int | None = None
    loading_placeholders: int | None = None
    bands: list[str] = field(default_factory=list)


@dataclass
class RevealResult:
    """What the reveal pass did before the shutter opened."""

    iterations: int
    scroll_height: int
    complete: bool
    #: Rounds of the loader-driven pass that followed the geometric walk.
    reveal_rounds: int = 0
    #: Visible loaders still unfired when the last round ended. Anything above
    #: zero is a section the shot will show as a placeholder.
    loaders_left: int = 0
    #: Unfired loaders a reader could not see either — inside a collapsed
    #: ``<details>`` or an inactive tab. Recorded, never ``suspect``.
    loaders_hidden: int = 0


@dataclass
class ShotMeta:
    """What one full-page screenshot turned out to be, past the file itself."""

    reveal_iterations: int
    reveal_complete: bool
    scroll_height: int
    png_height: int | None
    truncated: bool
    reveal_rounds: int = 0
    loaders_left: int = 0
    loaders_hidden: int = 0
    charts_total: int = 0
    charts_pending: int = 0
    loading_placeholders: int = 0
    bands: list[str] = field(default_factory=list)


@dataclass
class Skip:
    """One route the atlas deliberately did not visit."""

    path: str
    reason: str


@dataclass
class Session:
    """A logged-in browser context plus the base URL it is bound to."""

    name: str
    base_url: str
    context: Any
    pages: list[Any] = field(default_factory=list)


class BrowserMissingError(RuntimeError):
    """Raised when Playwright cannot find the Chromium it is asked to drive."""


class LoginFailedError(RuntimeError):
    """Raised when a session never reaches, or stops reaching, an authenticated page.

    Raised both at login time and mid-run, when a capture bounces to ``/login``:
    the session is gone, and every later route would photograph the login page,
    so the run ends rather than filling the atlas with them.

    The message never carries the credentials that were tried.
    """

    def __init__(self, message: str, *, capture: Capture | None = None) -> None:
        """Store the capture the bounce was seen on, when there was one.

        Args:
            message: The failure message, credential-free.
            capture: The route capture that bounced, so the manifest can still
                account for the route the run died on. ``None`` at login time,
                where no route was being captured.
        """
        super().__init__(message)
        self.capture = capture


# --------------------------------------------------------------------------- #
# Route selection
# --------------------------------------------------------------------------- #


def read_routes(path: Path) -> list[RouteRow]:
    """Read ``routes.csv`` by column name.

    Columns are addressed by name rather than position because P-UX-0b appended
    ``js_callers`` after ``auth_required``; a positional read would have silently
    picked up the wrong column.

    Args:
        path: Path to the inventory's ``routes.csv``.

    Returns:
        One :class:`RouteRow` per data row, in file order.

    Raises:
        SystemExit: If a required column is missing.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"area", "methods", "path", "template", "auth_required"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path}: missing column(s): {', '.join(sorted(missing))}")
        return [
            RouteRow(
                area=row["area"],
                methods=row["methods"],
                path=row["path"],
                template=row["template"],
                auth_required=row["auth_required"],
            )
            for row in reader
        ]


def skip_reason(route: RouteRow, *, admin_session: bool) -> str | None:
    """Return why this route is not captured, or ``None`` if it is eligible.

    Args:
        route: The inventory row.
        admin_session: Whether a super-admin session is available this run.

    Returns:
        A manifest skip reason, or ``None`` when the route should be captured.
    """
    if "GET" not in {verb.strip().upper() for verb in route.methods.split("|")}:
        return "method"
    if "{" in route.path:
        return "param"
    for prefix, reason in PATH_SKIPS:
        if route.path == prefix or route.path.startswith(prefix):
            return reason
    if route.auth_required not in CAPTURABLE_AUTH:
        return "auth-none"
    if route.auth_required == "super_admin" and not admin_session:
        return "no-admin-session"
    return None


def extends_target(template_name: str) -> str | None:
    """Return the template ``template_name`` extends, if it extends one.

    Args:
        template_name: Template path relative to the template root.

    Returns:
        The parent template path, or ``None`` when the file declares no
        ``{% extends %}`` or cannot be read.
    """
    source = TEMPLATE_ROOT / template_name
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None
    # The tag is always the first one in a Jinja template, so a cheap scan of the
    # head beats pulling in a parser for a single directive.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{%"):
            continue
        if "extends" not in stripped:
            continue
        for quote in ('"', "'"):
            if quote in stripped:
                parts = stripped.split(quote)
                if len(parts) >= 2:
                    return parts[1]
        return None
    return None


def reaches_root_layout(template_name: str) -> bool:
    """Whether ``template_name`` transitively extends :data:`ROOT_LAYOUT`.

    Args:
        template_name: Template path relative to the template root.

    Returns:
        ``True`` when following ``{% extends %}`` reaches ``base.html``.
    """
    seen: set[str] = set()
    current: str | None = template_name
    while current and current not in seen:
        if current == ROOT_LAYOUT:
            return True
        seen.add(current)
        current = extends_target(current)
    return False


def is_page(route: RouteRow) -> bool:
    """Whether a route renders a full page rather than an HTMX fragment.

    A route that names no template is a page: it renders nothing of its own
    because it redirects, and the redirect target is a page.

    Args:
        route: The inventory row.

    Returns:
        ``True`` for a page, ``False`` for a partial.
    """
    templates = [name.strip() for name in route.template.split(";") if name.strip()]
    if not templates:
        return True
    return any(reaches_root_layout(name) for name in templates)


def route_slug(path: str) -> str:
    """Turn a URL path into a flat, filesystem-safe screenshot stem.

    ``/`` becomes ``index``; every other separator becomes ``__`` with the
    leading pair stripped, so ``/admin/users/section`` becomes
    ``admin__users__section``.

    Args:
        path: The route path.

    Returns:
        The screenshot stem, without an extension.
    """
    slug = path.replace("/", "__")
    slug = slug.removeprefix("__")
    return slug or "index"


def area_dir(area: str) -> str:
    """Return the folder name for an inventory area value.

    A route attributed to two Areas carries a ``|``-joined value; the separator
    is not filesystem-safe, so it becomes a dash while the manifest keeps the
    raw value.

    Args:
        area: The raw ``area`` cell.

    Returns:
        A directory name.
    """
    return area.replace("|", "-") or "unassigned"


# --------------------------------------------------------------------------- #
# Browser sessions
# --------------------------------------------------------------------------- #


def open_session(
    browser: Any,
    *,
    name: str,
    base_url: str,
    user: str,
    password: str,
    confirm_path: str,
    viewport: tuple[int, int],
) -> Session:
    """Log in and return a confirmed, authenticated browser context.

    Args:
        browser: The launched Chromium browser.
        name: Session label used in the manifest (``tenant`` / ``super_admin``).
        base_url: Scheme and host the session is bound to.
        user: Login e-mail, from the environment.
        password: Login password, from the environment.
        confirm_path: A guarded path that proves the session took.
        viewport: Width and height in CSS pixels.

    Returns:
        The ready :class:`Session`.

    Raises:
        LoginFailedError: If the confirmation page is not reached. The message
            names the session and the URL, never the credentials.
    """
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=1,
        color_scheme="dark",
    )
    # On the context, not the page, and as an init script: it has to be
    # listening before the first above-the-fold loader fires, or that loader
    # reads as unfired for the rest of the capture.
    context.add_init_script(LOADER_MARKER_SCRIPT)
    page = context.new_page()
    page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.fill('input[name="email"]', user)
    page.fill('input[name="password"]', password)
    page.click(
        'form[action="/login"] button[type="submit"], form[action="/login"] input[type="submit"]'
    )
    settle(page)

    response = page.goto(
        f"{base_url}{confirm_path}", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
    )
    settle(page)
    status = response.status if response is not None else None
    if status != 200 or page.url.rstrip("/").endswith("/login"):
        raise LoginFailedError(
            f"{name} session did not authenticate against {base_url}{confirm_path} "
            f"(status {status}, landed on {page.url})"
        )
    page.close()
    return Session(name=name, base_url=base_url, context=context)


def advance_quiet_window(
    quiet: bool, quiet_since_ms: float | None, now_ms: float, *, hold_ms: int = QUIET_HOLD_MS
) -> tuple[float | None, bool]:
    """Advance the debounced quiet wait by one poll.

    The whole of the debounce's arithmetic, kept here as a pure function
    because it is the part that can be wrong without a browser noticing: an
    off-by-one that returns on the first quiet sample restores exactly the race
    the debounce exists to close.

    Args:
        quiet: Whether this poll saw the page quiet.
        quiet_since_ms: When the current run of quiet polls began, or ``None``
            if the previous poll was not quiet.
        now_ms: This poll's clock reading, in milliseconds on any monotonic
            scale — only differences are used.
        hold_ms: How long quiet must hold before the wait is satisfied.

    Returns:
        The new ``quiet_since_ms`` (``None`` once a poll is not quiet, which
        restarts the hold from scratch) and whether the hold is satisfied.
    """
    if not quiet:
        return None, False
    started = now_ms if quiet_since_ms is None else quiet_since_ms
    return started, (now_ms - started) >= hold_ms


def htmx_quiet(page: Page) -> bool:
    """Report whether no HTMX request is in flight on the page.

    Args:
        page: The page to sample.

    Returns:
        ``True`` when nothing carries ``.htmx-request``, and on any evaluation
        error — a page that cannot be sampled is photographed as it stands
        rather than waited out.
    """
    try:
        return bool(page.evaluate("() => document.querySelectorAll('.htmx-request').length === 0"))
    except Exception:  # noqa: BLE001 - an unsamplable page is not a reason to stall
        return True


def settle(page: Page, *, network_timeout_ms: int = NETWORK_IDLE_TIMEOUT_MS) -> None:
    """Wait for the network to go quiet, then for HTMX to stay finished.

    ``networkidle`` is best-effort: a surface holding an open stream (the
    assistants chat) never reaches it, and waiting out the timeout there is
    cheaper than not waiting at all elsewhere.

    The HTMX wait is **debounced**: ``.htmx-request`` must read zero for
    :data:`QUIET_HOLD_MS` continuously. A single sample is not enough, because
    a parent swap and the nested ``revealed`` loader it brings with it are
    separated by HTMX's settle delay and one intersection re-check — a gap in
    which nothing is in flight and the page is not finished.

    Args:
        page: The page to settle.
        network_timeout_ms: How long to give ``networkidle``. The reveal pass
            passes a shorter budget because it pays this once per scroll step.
    """
    # Both waits are best-effort: whatever goes wrong, the right answer is to
    # photograph the surface as it stands. A lazy section that never lands is a
    # finding for the report, not a reason to abandon the run.
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=network_timeout_ms)

    # A real clock, not a count of polls: each poll also pays one round-trip
    # into the page, so counting them would quietly stretch the budget on a
    # slow surface — and the budget is what keeps the chat's open stream from
    # holding the run.
    quiet_since: float | None = None
    deadline = time.monotonic() + HTMX_QUIET_TIMEOUT_MS / 1000
    while True:
        now_ms = time.monotonic() * 1000
        quiet_since, held = advance_quiet_window(htmx_quiet(page), quiet_since, now_ms)
        if held or time.monotonic() >= deadline:
            return
        with contextlib.suppress(Exception):
            page.wait_for_timeout(QUIET_POLL_MS)


def prepare(page: Page) -> None:
    """Freeze motion on a page so screenshots are reproducible.

    Args:
        page: The page to style.
    """
    page.add_style_tag(content=STILLNESS_CSS)


def viewport_of(page: Page) -> tuple[int, int]:
    """Return the page's viewport in CSS pixels.

    Args:
        page: The page to measure.

    Returns:
        Width and height, falling back to :data:`DEFAULT_VIEWPORT` for a context
        opened without an explicit viewport.
    """
    size = page.viewport_size
    if size is None:
        return DEFAULT_VIEWPORT
    return int(size["width"]), int(size["height"])


def scroll_height_of(page: Page) -> int:
    """Return the document's current scroll height in CSS pixels.

    Args:
        page: The page to measure.

    Returns:
        ``document.documentElement.scrollHeight``.
    """
    return int(page.evaluate("() => document.documentElement.scrollHeight"))


def reveal(page: Page) -> RevealResult:
    """Scroll the document top to bottom so every lazy Section loads.

    Sections triggered by ``hx-trigger="revealed"`` load on intersection with
    the viewport, so ``full_page=True`` on a page that was never scrolled
    photographs their placeholders: the renderer lengthens the surface, it does
    not scroll it. The pass walks down in sub-viewport steps, re-reading the
    height each round because each newly swapped Section lengthens the page, and
    stops only once the bottom is reached with the height standing still.

    The document is the scroll container (``.pf-shell`` sets ``min-height``, not
    ``height`` + ``overflow``), so scrolling the window is all this needs.

    The walk is the first of two passes, and it is kept because it is the one
    that exercises the page the way a reader does — scroll-spy, sticky headers,
    anything else hung on scroll position. What it cannot guarantee is that
    every loader fired, so :func:`drive_loaders` follows it and finishes the
    job from the loaders' own side.

    Args:
        page: The page to reveal, already settled after its navigation.

    Returns:
        The :class:`RevealResult`; ``complete`` is ``False`` when the walk ran
        out of iterations before the page stopped growing, which means the foot
        of the page was never intersected and may still hold placeholders, and
        ``loaders_left`` is what the second pass could not fire.
    """
    _, viewport_height = viewport_of(page)
    step = max(1, int(viewport_height * REVEAL_STEP_RATIO))

    offset = 0
    previous_height = -1
    iterations = 0
    complete = False
    for _ in range(REVEAL_MAX_ITERATIONS):
        iterations += 1
        height = scroll_height_of(page)
        offset = min(offset + step, height)
        page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", offset)
        settle(page, network_timeout_ms=REVEAL_NETWORK_IDLE_TIMEOUT_MS)
        if offset >= height and height == previous_height:
            complete = True
            break
        previous_height = height

    rounds, loaders_left, loaders_hidden = drive_loaders(page)

    page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
    settle(page, network_timeout_ms=REVEAL_NETWORK_IDLE_TIMEOUT_MS)
    page.wait_for_timeout(REVEAL_QUIET_MS)
    return RevealResult(
        iterations=iterations,
        scroll_height=scroll_height_of(page),
        complete=complete,
        reveal_rounds=rounds,
        loaders_left=loaders_left,
        loaders_hidden=loaders_hidden,
    )


def unfired_loaders(page: Page) -> list[tuple[Any, bool]]:
    """Return the ``revealed`` loaders that have not dispatched their request.

    Args:
        page: The page to inspect.

    Returns:
        One ``(handle, visible)`` pair per loader, document order. Empty on an
        evaluation error — an unreadable page is photographed as it stands.
    """
    probe = f"""(sel) => {{
        {VISIBLE_FN}
        return Array.prototype.map.call(
            document.querySelectorAll(sel), function (el) {{ return pfAtlasVisible(el); }}
        );
    }}"""
    try:
        handles = list(page.query_selector_all(UNFIRED_LOADER_SELECTOR))
        visible = [bool(flag) for flag in page.evaluate(probe, UNFIRED_LOADER_SELECTOR)]
    except Exception:  # noqa: BLE001 - an unreadable page is not a reason to stall
        return []
    if len(visible) != len(handles):
        # The DOM moved between the two queries. Treat everything as visible:
        # driving a loader that did not need it costs a scroll, skipping one
        # that did costs a placeholder in the shot.
        return [(handle, True) for handle in handles]
    return list(zip(handles, visible, strict=True))


def fired_tally(page: Page) -> int:
    """Return how many HTMX requests the page has dispatched since it loaded.

    Args:
        page: The page to sample.

    Returns:
        The monotonic count kept by :data:`LOADER_MARKER_SCRIPT`, or ``-1`` if
        it cannot be read. Two unreadable samples in a row compare equal, which
        ends the loop — the right answer for a page that cannot be sampled at
        all, since it cannot be driven either.
    """
    try:
        return int(page.evaluate(f"() => window.{FIRED_TALLY} || 0"))
    except Exception:  # noqa: BLE001 - unreadable is not "nothing happened"
        return -1


def drive_loaders(page: Page) -> tuple[int, int, int]:
    """Fire every ``revealed`` loader by its own mechanism, round after round.

    The geometric pass above walks the page by *height*, which is the right way
    to exercise scroll-spy and sticky headers but the wrong way to guarantee a
    loader fires: a section that arrives on ``revealed`` may itself contain
    per-item loaders on the same trigger — ``charts_section.html`` swaps in one
    ``<article>`` per investment, each holding a second
    ``hx-get="/api/charts/investment/{id}"`` — and those did not exist when the
    walk passed their eventual position. Here the loaders themselves are the
    work list: each is scrolled to its own centre, the page is allowed to go
    quiet, and the DOM is re-read, because what just landed may have brought
    more.

    Firing is read off HTMX's own ``htmx:beforeRequest``
    (:data:`LOADER_MARKER_SCRIPT`) rather than off the element's disappearance:
    most loaders swap ``outerHTML`` and do vanish, but the portfolio-review
    stack swaps ``innerHTML`` into the ``<article>`` that carries the trigger,
    so that one would read as unfired forever.

    Only loaders a reader could see are driven, and the loop stops the moment a
    round dispatches nothing — a round that achieved nothing is a round every
    later round would repeat, and the ``<details>`` case below would otherwise
    spend the whole budget on one element that can never fire.

    Args:
        page: The page to drive, already walked and settled.

    Returns:
        Rounds spent, how many *visible* loaders were still unfired at the end,
        and how many unfired loaders were off-screen. The split matters: a
        visible one is a hole in the shot, while an off-screen one — inside a
        collapsed ``<details>`` or an inactive tab — is absent from the shot
        exactly as it is absent from the reader's view, so it is a design
        finding about that surface rather than a defect in the capture.
    """
    rounds = 0
    for _ in range(MAX_REVEAL_ROUNDS):
        pending = [handle for handle, visible in unfired_loaders(page) if visible]
        if not pending:
            break
        rounds += 1
        before = fired_tally(page)
        for loader in pending:
            with contextlib.suppress(Exception):
                loader.evaluate("(el) => el.scrollIntoView({block: 'center'})")
            settle(page, network_timeout_ms=REVEAL_NETWORK_IDLE_TIMEOUT_MS)
        if fired_tally(page) == before:
            break

    left = unfired_loaders(page)
    visible_left = sum(1 for _, visible in left if visible)
    return rounds, visible_left, len(left) - visible_left


def await_charts(page: Page) -> tuple[int, int]:
    """Wait until every Plotly target on the page has been drawn into.

    HTMX going quiet says the *markup* arrived; it says nothing about the
    figures. Every chart in the tree is drawn by an inline script that runs
    after the swap, and ``Plotly.newPlot`` is asynchronous, so ``.htmx-request``
    is long gone by the time the first trace appears. Without this wait a
    revealed page is photographed as a grid of empty boxes.

    Readiness is Plotly's own ``.js-plotly-plot`` marker on the container (or a
    ``.main-svg`` inside it), not the templates' ``data-pf-rendered`` flags:
    those are per-template conventions, and ``chart_snapshot.js`` sets its own
    *before* awaiting the draw. The vendored typeface is awaited first, via
    ``document.fonts.ready``: Plotly measures its margins against whatever face
    is mounted when it draws, so a figure drawn on the fallback keeps the
    fallback's metrics even after the real font swaps in.

    Args:
        page: The page to wait on.

    Returns:
        How many chart targets the page declares, and how many were still
        undrawn when the wait ended. A non-zero remainder means the timeout was
        reached — a figure that never lands is a finding, not a stall.
    """
    probe = f"""() => {{
        const targets = Array.prototype.slice.call(
            document.querySelectorAll({CHART_TARGET_SELECTOR!r})
        );
        const pending = targets.filter(function (el) {{
            return !el.matches({PLOTLY_DRAWN_SELECTOR!r})
                && !el.querySelector({PLOTLY_DRAWN_SELECTOR!r});
        }});
        return [targets.length, pending.length];
    }}"""

    # The vendored typeface is `font-display: swap`: a shot taken before it
    # lands photographs the fallback face. Plotly text re-renders on arrival,
    # but the measured margins do not, so wait for the fonts first.
    with contextlib.suppress(Exception):
        page.evaluate("() => document.fonts.ready.then(() => true)")

    total, pending = 0, 0
    deadline = time.monotonic() + CHART_TIMEOUT_MS / 1000
    while True:
        try:
            total, pending = (int(value) for value in page.evaluate(probe))
        except Exception:  # noqa: BLE001 - an unreadable page is photographed as it stands
            return total, pending
        if pending == 0 or time.monotonic() >= deadline:
            break
        with contextlib.suppress(Exception):
            page.wait_for_timeout(CHART_POLL_MS)

    # ``responsive: true`` figures size themselves against the viewport they
    # were drawn in, and the full-page shot is about to change it. One resize
    # and a frame to act on it costs nothing and saves a squashed axis.
    with contextlib.suppress(Exception):
        page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        page.evaluate("() => new Promise((done) => requestAnimationFrame(() => done()))")
    settle(page, network_timeout_ms=REVEAL_NETWORK_IDLE_TIMEOUT_MS)
    return total, pending


def count_loading_placeholders(page: Page) -> int:
    """Count the visible "Loading …" placeholders left on the page.

    The backstop under the two targeted waits: it knows nothing about HTMX or
    Plotly and simply reads what a human would see, so a loader neither
    :func:`drive_loaders` nor :func:`await_charts` recognises still shows up in
    the manifest instead of only in the PNG.

    Only an element's *own* text counts — the direct text-node children — so an
    ancestor is not reported alongside the placeholder it wraps.

    Args:
        page: The page to inspect, immediately before the shutter.

    Returns:
        The number of visible placeholders, or ``0`` if the page cannot be
        read.
    """
    probe = rf"""() => {{
        {VISIBLE_FN}
        const re = new RegExp({LOADING_PLACEHOLDER_PATTERN!r});
        let count = 0;
        document.querySelectorAll("body *").forEach(function (el) {{
            let own = "";
            for (const node of el.childNodes) {{
                if (node.nodeType === 3) own += node.nodeValue;
            }}
            own = own.trim().replace(/\s+/g, " ");
            if (!re.test(own)) return;
            if (!pfAtlasVisible(el)) return;
            count += 1;
        }});
        return count;
    }}"""
    try:
        return int(page.evaluate(probe))
    except Exception:  # noqa: BLE001 - an unreadable page is not a finding about loading
        return 0


# --------------------------------------------------------------------------- #
# Shot geometry
# --------------------------------------------------------------------------- #


def png_height(path: Path) -> int | None:
    """Read a PNG's pixel height out of its IHDR chunk.

    Eight bytes of signature, then the IHDR chunk: 4 length, 4 type, 4 width,
    4 height. No image library is needed — and none is wanted, because the only
    question asked of the file is how tall Chromium actually made it.

    Args:
        path: The PNG to measure.

    Returns:
        The height in pixels, or ``None`` if the file is absent, short, or not a
        PNG.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return int(struct.unpack(">I", header[20:24])[0])


def is_truncated(png_px: int | None, scroll_px: int) -> bool:
    """Decide whether a full-page PNG came back cut.

    Chromium does not raise on an over-tall page; it returns a shorter image. So
    the only evidence is the arithmetic: a PNG standing exactly at the cap, or
    falling more than a rounding pixel short of the document it was supposed to
    cover, was cut.

    Args:
        png_px: The PNG's own height, or ``None`` when it could not be read.
        scroll_px: The document height the reveal pass settled on.

    Returns:
        ``True`` when the image does not cover the page.
    """
    if png_px is None or scroll_px <= 0:
        return False
    if png_px == CHROMIUM_MAX_AXIS_PX:
        return True
    return scroll_px - png_px > TRUNCATION_TOLERANCE_PX


def band_rects(scroll_px: int, band_px: int) -> list[tuple[int, int]]:
    """Cut a page height into successive band rectangles.

    Args:
        scroll_px: The document height to cover.
        band_px: Band height in CSS pixels.

    Returns:
        ``(top, height)`` pairs, top to bottom, the last one short; empty when
        either argument is non-positive.
    """
    if band_px <= 0 or scroll_px <= 0:
        return []
    return [(top, min(band_px, scroll_px - top)) for top in range(0, scroll_px, band_px)]


def write_bands(
    page: Page, target: Path, out_dir: Path, *, scroll_px: int, band_px: int
) -> list[str]:
    """Cut the full-page render into chat-legible horizontal bands.

    Each band is a second ``full_page`` screenshot narrowed by ``clip`` to one
    slice of the same surface — so there is no stitching to go wrong, and the
    sticky Section header appears once, where it actually sits, rather than
    repeated at the top of every slice.

    A page that fits in a single band gets none: that band would be a second
    copy of the full PNG under a different name.

    Args:
        page: The page, already revealed and scrolled back to the top.
        target: The full-page PNG; the bands are named after its stem.
        out_dir: The run's output root, for the returned relative paths.
        scroll_px: The document height the reveal pass settled on.
        band_px: Band height in CSS pixels.

    Returns:
        The band paths relative to ``out_dir``, top to bottom.
    """
    rects = band_rects(scroll_px, band_px)
    if len(rects) < 2:
        return []
    width, _ = viewport_of(page)
    folder = target.parent / "bands"
    folder.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for index, (top, height) in enumerate(rects, start=1):
        band = folder / f"{target.stem}-{index:02d}.png"
        page.screenshot(
            path=str(band),
            full_page=True,
            clip={"x": 0, "y": top, "width": width, "height": height},
        )
        written.append(str(band.relative_to(out_dir)))
    return written


def capture_shot(page: Page, target: Path, out_dir: Path, *, band_px: int) -> ShotMeta:
    """Reveal the page, wait for its charts, photograph it whole, then measure.

    The one seam both the route pass and the scene pass go through, so a lazy
    Section is revealed, a chart is waited for and a truncation is caught
    identically in either.

    The order is the order the page itself works in: markup first
    (:func:`reveal`), then the figures the markup's inline scripts draw
    (:func:`await_charts`), then one last look for anything still saying
    "Loading …" (:func:`count_loading_placeholders`) — and only then the
    shutter.

    Args:
        page: The page to photograph, already settled and styled.
        target: Where the full-page PNG goes.
        out_dir: The run's output root, for the returned relative band paths.
        band_px: Band height in CSS pixels, or 0 for no bands.

    Returns:
        The :class:`ShotMeta` describing the shot.
    """
    revealed = reveal(page)
    charts_total, charts_pending = await_charts(page)
    placeholders = count_loading_placeholders(page)
    page.screenshot(path=str(target), full_page=True)
    height = png_height(target)
    meta = ShotMeta(
        reveal_iterations=revealed.iterations,
        reveal_complete=revealed.complete,
        scroll_height=revealed.scroll_height,
        png_height=height,
        truncated=is_truncated(height, revealed.scroll_height),
        reveal_rounds=revealed.reveal_rounds,
        loaders_left=revealed.loaders_left,
        loaders_hidden=revealed.loaders_hidden,
        charts_total=charts_total,
        charts_pending=charts_pending,
        loading_placeholders=placeholders,
    )
    if band_px > 0:
        meta.bands = write_bands(
            page, target, out_dir, scroll_px=revealed.scroll_height, band_px=band_px
        )
    return meta


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


def capture_route(session: Session, route: RouteRow, out_dir: Path, *, band_px: int = 0) -> Capture:
    """Visit one route, reveal its lazy Sections and write its full-page screenshot.

    A non-2xx response is still captured — an error page is a UX surface, and
    seeing it is the point of the atlas. A bounce to ``/login`` is the one
    outcome that is not: the session is gone, so every later route would
    photograph the login page instead of itself.

    Args:
        session: The authenticated session to visit through.
        route: The inventory row to capture.
        out_dir: The run's output root.
        band_px: Band height in CSS pixels, or 0 for no bands.

    Returns:
        The :class:`Capture` record, flagged ``suspect`` when the PNG is
        truncated, implausibly small, or the response was an error.

    Raises:
        LoginFailedError: If the route bounced to ``/login``. The capture rides
            on the error so the manifest still accounts for the route the run
            died on.
    """
    partial = not is_page(route)
    folder = out_dir / area_dir(route.area)
    if partial:
        folder = folder / "partials"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{route_slug(route.path)}.png"

    page = session.context.new_page()
    try:
        response = page.goto(
            f"{session.base_url}{route.path}",
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_MS,
        )
        settle(page)
        prepare(page)
        meta = capture_shot(page, target, out_dir, band_px=band_px)
        status = response.status if response is not None else None
        final_url = page.url
    finally:
        page.close()

    capture = Capture(
        area=route.area,
        path=route.path,
        file=str(target.relative_to(out_dir)),
        status=status,
        final_url=final_url,
        partial=partial,
        session=session.name,
        reveal_iterations=meta.reveal_iterations,
        reveal_complete=meta.reveal_complete,
        scroll_height=meta.scroll_height,
        png_height=meta.png_height,
        truncated=meta.truncated,
        reveal_rounds=meta.reveal_rounds,
        loaders_left=meta.loaders_left,
        loaders_hidden=meta.loaders_hidden,
        charts_total=meta.charts_total,
        charts_pending=meta.charts_pending,
        loading_placeholders=meta.loading_placeholders,
        bands=meta.bands,
    )
    size = target.stat().st_size if target.exists() else 0
    if final_url.rstrip("/").endswith("/login"):
        capture.suspect = True
        capture.reason = SESSION_LOST_REASON
        raise LoginFailedError(
            f"{route.path} bounced to /login — the session did not carry", capture=capture
        )
    if meta.truncated:
        capture.suspect = True
        capture.reason = TRUNCATION_REASON
    elif not meta.reveal_complete:
        capture.suspect = True
        capture.reason = REVEAL_INCOMPLETE_REASON
    elif meta.loaders_left > 0:
        capture.suspect = True
        capture.reason = f"{UNFIRED_LOADERS_REASON} ({meta.loaders_left})"
    elif meta.charts_pending > 0:
        capture.suspect = True
        capture.reason = f"{CHARTS_PENDING_REASON} ({meta.charts_pending} of {meta.charts_total})"
    elif meta.loading_placeholders > 0:
        capture.suspect = True
        capture.reason = f"{meta.loading_placeholders} loading placeholders visible"
    elif size < SUSPECT_MIN_BYTES:
        capture.suspect = True
        capture.reason = (
            f"png is {size} bytes (< {SUSPECT_MIN_BYTES}) — likely empty or an error card"
        )
    elif status is not None and status >= 400:
        capture.suspect = True
        capture.reason = f"HTTP {status}"
    return capture


def run_scene(
    scene: dict[str, Any],
    sessions: dict[str, Session],
    out_dir: Path,
    *,
    band_px: int = 0,
) -> SceneResult:
    """Walk one scene's steps and screenshot where it lands.

    Args:
        scene: One entry of the scenes file.
        sessions: The available sessions, keyed by name.
        out_dir: The run's output root.
        band_px: Band height in CSS pixels, or 0 for no bands.

    Returns:
        The :class:`SceneResult`; a step whose selector never appears yields
        ``ok=False`` with the failing index rather than aborting the run.
    """
    name = str(scene.get("name", "unnamed"))
    session_name = str(scene.get("session", "tenant"))
    session = sessions.get(session_name)
    if session is None:
        return SceneResult(name=name, file=None, ok=False, reason=f"no {session_name} session")

    folder = out_dir / area_dir(str(scene.get("area", "unassigned"))) / "scenes"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{scene.get('shot', name)}.png"

    page = session.context.new_page()
    try:
        page.goto(
            f"{session.base_url}{scene.get('start', '/')}",
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_MS,
        )
        settle(page)
        prepare(page)
        for index, step in enumerate(scene.get("steps", [])):
            try:
                apply_step(page, step)
            except Exception as exc:  # noqa: BLE001 - a missing selector is a finding, not a crash
                return SceneResult(
                    name=name,
                    file=None,
                    ok=False,
                    failed_step=index,
                    reason=type(exc).__name__,
                )
            settle(page)
        prepare(page)
        meta = capture_shot(page, target, out_dir, band_px=band_px)
    finally:
        page.close()
    return SceneResult(
        name=name,
        file=str(target.relative_to(out_dir)),
        ok=True,
        reveal_iterations=meta.reveal_iterations,
        reveal_complete=meta.reveal_complete,
        scroll_height=meta.scroll_height,
        png_height=meta.png_height,
        truncated=meta.truncated,
        reveal_rounds=meta.reveal_rounds,
        loaders_left=meta.loaders_left,
        loaders_hidden=meta.loaders_hidden,
        charts_total=meta.charts_total,
        charts_pending=meta.charts_pending,
        loading_placeholders=meta.loading_placeholders,
        bands=meta.bands,
    )


def apply_step(page: Page, step: dict[str, Any]) -> None:
    """Apply one scene step to a page.

    Args:
        page: The page being walked.
        step: A step object carrying exactly one verb.

    Raises:
        ValueError: If the step carries no recognised verb.
    """
    if "click" in step:
        page.click(str(step["click"]), timeout=STEP_TIMEOUT_MS)
        return
    if "fill" in step:
        spec = step["fill"]
        page.fill(str(spec["selector"]), str(spec["value"]), timeout=STEP_TIMEOUT_MS)
        return
    if "wait" in step:
        page.wait_for_selector(str(step["wait"]), timeout=STEP_TIMEOUT_MS)
        return
    if "wait_ms" in step:
        page.wait_for_timeout(int(step["wait_ms"]))
        return
    raise ValueError(f"unrecognised scene step: {sorted(step)}")


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


def git_head() -> str | None:
    """Return the short commit hash of ``HEAD``, or ``None`` outside a checkout.

    Read-only: the atlas records which tree it photographed and never writes to
    the repository's git state.

    Returns:
        The short hash, or ``None``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def file_sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file.

    Args:
        path: The file to hash.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def resolve_out_dir(explicit: str | None) -> Path:
    """Pick the output folder, suffixing a same-day re-run rather than overwriting.

    Args:
        explicit: The ``--out`` value, if the operator gave one.

    Returns:
        A folder path that does not yet exist (unless ``--out`` named one).
    """
    if explicit:
        return Path(explicit)
    stem = DEFAULT_OUT_PARENT / date.today().isoformat()
    if not stem.exists():
        return stem
    suffix = 2
    while (candidate := stem.with_name(f"{stem.name}-{suffix}")).exists():
        suffix += 1
    return candidate


def write_manifest(
    out_dir: Path,
    *,
    base_url: str,
    admin_base_url: str | None,
    viewport: tuple[int, int],
    band_px: int,
    routes_csv: Path,
    captured: list[Capture],
    scenes: list[SceneResult],
    skipped: list[Skip],
) -> None:
    """Write ``manifest.json``.

    The manifest records whether an admin base URL was configured, never any
    credential.

    Args:
        out_dir: The run's output root.
        base_url: The tenant base URL.
        admin_base_url: The super-admin base URL, or ``None``.
        viewport: Width and height in CSS pixels.
        band_px: The ``--bands`` height this run used, 0 when off — recorded so
            an empty ``bands`` list reads as "not asked for" rather than
            "attempted and empty".
        routes_csv: The route table this run selected from.
        captured: Every capture attempt.
        scenes: Every scene walk.
        skipped: Every route deliberately not visited.
    """
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": git_head(),
        "base_url": base_url,
        "admin_base_url": admin_base_url,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "bands": band_px,
        "routes_csv_sha256": file_sha256(routes_csv),
        "captured": [asdict(item) for item in captured],
        "scenes": [asdict(item) for item in scenes],
        "skipped": [asdict(item) for item in skipped],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def scene_shot_reason(scene: SceneResult) -> str | None:
    """Return why a scene's shot cannot be trusted, or ``None`` if it can.

    The scene-side twin of the ``suspect`` ladder in :func:`capture_route`, in
    the same order, so a scene and a route that came back wrong the same way
    are described the same way.

    Args:
        scene: A scene walk that landed (``ok``); a failed walk has no shot to
            judge.

    Returns:
        The reason string for the contact sheet, or ``None``.
    """
    if scene.truncated:
        return TRUNCATION_REASON
    if not scene.reveal_complete:
        return REVEAL_INCOMPLETE_REASON
    if scene.loaders_left:
        return f"{UNFIRED_LOADERS_REASON} ({scene.loaders_left})"
    if scene.charts_pending:
        return f"{CHARTS_PENDING_REASON} ({scene.charts_pending} of {scene.charts_total})"
    if scene.loading_placeholders:
        return f"{scene.loading_placeholders} loading placeholders visible"
    return None


def append_bands(lines: list[str], label: str, bands: list[str]) -> None:
    """Append a collapsible band gallery to the contact sheet, if there is one.

    A long page is a dozen bands, and inlining them would bury the contact
    sheet's one-image-per-route rhythm under them, so they fold away behind a
    ``<details>`` that every Markdown viewer renders closed.

    Args:
        lines: The document being built; appended to in place.
        label: Alt-text stem for the band images.
        bands: Band paths relative to the run root, top to bottom.
    """
    if not bands:
        return
    lines.append(f"<details><summary>{len(bands)} chat-legible bands</summary>")
    lines.append("")
    for index, band in enumerate(bands, start=1):
        lines.append(f"![{label} band {index:02d}]({band})")
        lines.append("")
    lines.append("</details>")
    lines.append("")


def write_index(
    out_dir: Path,
    *,
    base_url: str,
    viewport: tuple[int, int],
    captured: list[Capture],
    scenes: list[SceneResult],
    skipped: list[Skip],
) -> None:
    """Write ``index.md``, the contact sheet.

    One ``##`` section per Area — pages first, then partials, then scenes —
    followed by a closing section listing everything that needs a human look.

    Args:
        out_dir: The run's output root.
        base_url: The tenant base URL.
        viewport: Width and height in CSS pixels.
        captured: Every capture attempt.
        scenes: Every scene walk.
        skipped: Every route deliberately not visited.
    """
    lines: list[str] = [
        "# Screenshot atlas",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Commit: `{git_head() or 'unknown'}`",
        f"- Base URL: `{base_url}`",
        f"- Viewport: {viewport[0]}x{viewport[1]}, dark",
        f"- Captured: {len(captured)} route(s), {len(scenes)} scene(s), {len(skipped)} skipped",
        "",
    ]

    scenes_by_area: dict[str, list[SceneResult]] = {}
    for scene in scenes:
        if scene.file:
            scenes_by_area.setdefault(Path(scene.file).parts[0], []).append(scene)

    areas = sorted({area_dir(item.area) for item in captured} | set(scenes_by_area))
    for area in areas:
        lines.append(f"## {area}")
        lines.append("")
        rows = [item for item in captured if area_dir(item.area) == area]
        for label, subset in (
            ("Pages", [row for row in rows if not row.partial]),
            (
                "Partials (render without the shell chrome, by design)",
                [row for row in rows if row.partial],
            ),
        ):
            if not subset:
                continue
            lines.append(f"### {label}")
            lines.append("")
            for row in sorted(subset, key=lambda item: item.path):
                flag = f" — **suspect:** {row.reason}" if row.suspect else ""
                lines.append(f"**`{row.path}`** — HTTP {row.status}{flag}")
                lines.append("")
                lines.append(f"![{row.path}]({row.file})")
                lines.append("")
                append_bands(lines, row.path, row.bands)
        if area in scenes_by_area:
            lines.append("### Scenes")
            lines.append("")
            for scene in sorted(scenes_by_area[area], key=lambda item: item.name):
                lines.append(f"**{scene.name}**")
                lines.append("")
                lines.append(f"![{scene.name}]({scene.file})")
                lines.append("")
                append_bands(lines, scene.name, scene.bands)

    lines.append("## Needs a look")
    lines.append("")
    suspects = [item for item in captured if item.suspect]
    failed = [item for item in scenes if not item.ok]
    # A scene that failed its walk is listed above; these are walks that landed
    # and then produced a shot that cannot be trusted.
    flagged_scenes = [item for item in scenes if item.ok and scene_shot_reason(item)]
    if suspects:
        lines.append("### Suspect captures")
        lines.append("")
        for row in suspects:
            lines.append(f"- `{row.path}` — {row.reason}")
        lines.append("")
    if failed:
        lines.append("### Failed scenes")
        lines.append("")
        for scene in failed:
            where = "" if scene.failed_step is None else f" at step {scene.failed_step}"
            lines.append(f"- {scene.name}{where} — {scene.reason or 'failed'}")
        lines.append("")
    if flagged_scenes:
        lines.append("### Scene shots needing a look")
        lines.append("")
        for scene in flagged_scenes:
            lines.append(f"- {scene.name} — {scene_shot_reason(scene)}")
        lines.append("")
    if not suspects and not failed and not flagged_scenes:
        lines.append("Nothing suspect and no failed scene.")
        lines.append("")

    lines.append("### Skipped routes")
    lines.append("")
    by_reason: dict[str, list[str]] = {}
    for skip in skipped:
        by_reason.setdefault(skip.reason, []).append(skip.path)
    for reason in sorted(by_reason):
        paths = sorted(set(by_reason[reason]))
        lines.append(
            f"- **{reason}** ({len(by_reason[reason])}): "
            f"{', '.join(f'`{path}`' for path in paths[:8])}"
            f"{' …' if len(paths) > 8 else ''}"
        )
    lines.append("")

    (out_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_viewport(value: str) -> tuple[int, int]:
    """Parse a ``WxH`` viewport argument.

    Args:
        value: e.g. ``1440x900``.

    Returns:
        Width and height.

    Raises:
        argparse.ArgumentTypeError: If the value is not ``WxH``.
    """
    try:
        width, height = value.lower().split("x", 1)
        return int(width), int(height)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected WxH, got {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="ux_atlas",
        description="Screenshot every user-visible GET route into a dated, per-area atlas.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PF_ATLAS_BASE_URL"),
        help="Tenant base URL, e.g. http://minathena-capital.localhost:8000 "
        "(default: $PF_ATLAS_BASE_URL).",
    )
    parser.add_argument(
        "--admin-base-url",
        default=os.environ.get("PF_ATLAS_ADMIN_BASE_URL"),
        help="Super-admin base URL, e.g. http://admin.localhost:8000 "
        "(default: $PF_ATLAS_ADMIN_BASE_URL). Without it, super_admin routes are skipped.",
    )
    parser.add_argument(
        "--routes",
        default=str(DEFAULT_ROUTES),
        help="Inventory route table to select from (default: the P-UX-0 CSV).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output folder (default: docs/ux/atlas/<YYYY-MM-DD>/, suffixed on a re-run).",
    )
    parser.add_argument(
        "--scenes",
        default=str(DEFAULT_SCENES),
        help="Scenes file (default: docs/ux/atlas-scenes.json).",
    )
    parser.add_argument(
        "--area",
        action="append",
        default=None,
        metavar="SLUG",
        help="Restrict the run to one Area; repeatable.",
    )
    parser.add_argument("--no-scenes", action="store_true", help="Skip the scenes pass.")
    parser.add_argument(
        "--bands",
        type=int,
        default=0,
        metavar="PX",
        help="Also cut every shot into horizontal bands this tall, for uploading to a "
        "chat that downscales a long PNG into illegibility (default: 0, off; "
        f"pass {RECOMMENDED_BAND_PX}).",
    )
    parser.add_argument(
        "--viewport",
        type=parse_viewport,
        default=DEFAULT_VIEWPORT,
        metavar="WxH",
        help=f"Viewport in CSS pixels (default: {DEFAULT_VIEWPORT[0]}x{DEFAULT_VIEWPORT[1]}).",
    )
    return parser


def load_scenes(path: Path, areas: list[str] | None) -> list[dict[str, Any]]:
    """Read the scenes file, optionally narrowed to some Areas.

    Args:
        path: The scenes file.
        areas: Area slugs to keep, or ``None`` for all.

    Returns:
        The scene objects; an empty list when the file is absent.
    """
    if not path.exists():
        return []
    scenes = json.loads(path.read_text(encoding="utf-8"))
    if areas:
        scenes = [scene for scene in scenes if scene.get("area") in areas]
    return scenes


def launch(playwright: Any) -> Any:
    """Launch headless Chromium, turning a missing browser into a typed error.

    Args:
        playwright: The started Playwright driver.

    Returns:
        The launched browser.

    Raises:
        BrowserMissingError: If the Chromium build is not installed.
    """
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed error below
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise BrowserMissingError(INSTALL_HINT) from exc
        raise


def main(argv: list[str] | None = None) -> int:
    """Run the atlas.

    Args:
        argv: Command-line arguments, or ``None`` for ``sys.argv``.

    Returns:
        0 clean, 1 any suspect capture, flagged scene shot or failed scene, 2
        browser or package missing, 3 login failed or a session lost mid-run.
    """
    args = build_parser().parse_args(argv)

    if playwright_api is None:
        print(PACKAGE_HINT, file=sys.stderr)
        return EXIT_NO_BROWSER

    base_url = (args.base_url or "").rstrip("/")
    if not base_url:
        print(
            "No tenant base URL. Pass --base-url or export PF_ATLAS_BASE_URL "
            "(a tenant host, e.g. http://minathena-capital.localhost:8000).",
            file=sys.stderr,
        )
        return EXIT_LOGIN_FAILED

    user = os.environ.get("PF_ATLAS_USER")
    password = os.environ.get("PF_ATLAS_PASSWORD")
    if not user or not password:
        print(
            "No tenant credentials. Export PF_ATLAS_USER and PF_ATLAS_PASSWORD.",
            file=sys.stderr,
        )
        return EXIT_LOGIN_FAILED

    admin_base_url = (args.admin_base_url or "").rstrip("/") or None
    admin_user = os.environ.get("PF_ATLAS_ADMIN_USER")
    admin_password = os.environ.get("PF_ATLAS_ADMIN_PASSWORD")
    admin_session_wanted = bool(admin_base_url and admin_user and admin_password)

    routes_csv = Path(args.routes)
    routes = read_routes(routes_csv)

    selected: list[RouteRow] = []
    skipped: list[Skip] = []
    for route in routes:
        reason = skip_reason(route, admin_session=admin_session_wanted)
        if reason is not None:
            skipped.append(Skip(path=route.path, reason=reason))
            continue
        if args.area and area_dir(route.area) not in args.area:
            continue
        selected.append(route)
    selected.sort(key=lambda route: (route.area, route.path))

    # Resolved now so the suffix logic sees the pre-run state, but not created
    # until a session exists: a run that dies on a missing browser or a refused
    # login must leave no empty dated folder behind to shift the next run's name.
    out_dir = resolve_out_dir(args.out)

    band_px = max(0, args.bands)

    captured: list[Capture] = []
    scene_results: list[SceneResult] = []
    session_lost = False

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = launch(playwright)
        except BrowserMissingError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_NO_BROWSER

        try:
            sessions: dict[str, Session] = {}
            try:
                sessions["tenant"] = open_session(
                    browser,
                    name="tenant",
                    base_url=base_url,
                    user=user,
                    password=password,
                    confirm_path="/front-office",
                    viewport=args.viewport,
                )
                if admin_session_wanted:
                    assert admin_base_url is not None
                    assert admin_user is not None
                    assert admin_password is not None
                    sessions["super_admin"] = open_session(
                        browser,
                        name="super_admin",
                        base_url=admin_base_url,
                        user=admin_user,
                        password=admin_password,
                        confirm_path="/super-admin/tenants",
                        viewport=args.viewport,
                    )
            except LoginFailedError as exc:
                print(f"login failed: {exc}", file=sys.stderr)
                return EXIT_LOGIN_FAILED

            out_dir.mkdir(parents=True, exist_ok=True)

            for route in selected:
                session = sessions[
                    "super_admin" if route.auth_required == "super_admin" else "tenant"
                ]
                try:
                    capture = capture_route(session, route, out_dir, band_px=band_px)
                except LoginFailedError as exc:
                    # The session is gone; every remaining route would photograph
                    # the login page. Stop, but keep — and write — what the run
                    # already has.
                    if exc.capture is not None:
                        captured.append(exc.capture)
                    print(f"session lost: {exc}", file=sys.stderr)
                    session_lost = True
                    break
                captured.append(capture)
                mark = " SUSPECT" if capture.suspect else ""
                print(f"{capture.status} {route.path} -> {capture.file}{mark}")

            if not session_lost and not args.no_scenes:
                for scene in load_scenes(Path(args.scenes), args.area):
                    result = run_scene(scene, sessions, out_dir, band_px=band_px)
                    scene_results.append(result)
                    print(f"scene {result.name}: {'ok' if result.ok else 'FAILED'}")
        finally:
            browser.close()

    write_manifest(
        out_dir,
        base_url=base_url,
        admin_base_url=admin_base_url,
        viewport=args.viewport,
        band_px=band_px,
        routes_csv=routes_csv,
        captured=captured,
        scenes=scene_results,
        skipped=skipped,
    )
    write_index(
        out_dir,
        base_url=base_url,
        viewport=args.viewport,
        captured=captured,
        scenes=scene_results,
        skipped=skipped,
    )

    suspects = sum(1 for item in captured if item.suspect)
    failures = sum(1 for item in scene_results if not item.ok)
    flagged_scenes = sum(
        1 for item in scene_results if item.ok and (item.truncated or not item.reveal_complete)
    )
    print(
        f"captured: {len(captured)}  scenes: {len(scene_results)}  "
        f"skipped: {len(skipped)}  suspect: {suspects}  failed scenes: {failures}"
    )
    print(f"written to: {out_dir}")
    if session_lost:
        return EXIT_LOGIN_FAILED
    return EXIT_SUSPECT if (suspects or failures or flagged_scenes) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
