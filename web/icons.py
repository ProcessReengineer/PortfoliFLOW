# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Inline SVG icons for the web shell — one open-source set, vendored.

The shell draws every icon from Lucide (ISC), vendored as individual SVG
files under ``web/static/vendor/lucide/``. Templates never name a file: they
call the Jinja global ``pf_icon(name, label=None)`` with a *product* name from
:data:`ICONS`, so a renamed upstream file is a one-line change here. The
markup follows the design parameters: 16 px, 1.5 px stroke, ``currentColor``;
decorative by default (``aria-hidden="true"``), or an image with an
accessible name when ``label`` is given — the form an icon-only control uses.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from markupsafe import Markup, escape

ICONS: dict[str, str] = {
    # Areas — one icon per top-level Area, in sidebar order (ADR-0122 §1),
    # plus the platform-operations surface the System Tenant reaches.
    "front-office": "layout-dashboard",
    "back-office": "landmark",
    "assistants": "bot",
    "planning-desk": "calendar-range",
    "investor-communication": "megaphone",
    "watch-desk": "eye",
    "cases": "folder-open",
    "transactions": "arrow-left-right",
    "admin": "settings",
    "platform-admin": "shield",
    # Shell chrome — navigation, the assistant dock, and the controls that
    # recur across surfaces.
    "search": "search",
    "nav-collapse": "panel-left-close",
    "nav-expand": "panel-left-open",
    "sign-out": "log-out",
    "shirley": "message-square",
    "stage": "maximize-2",
    "dock": "minimize-2",
    "close": "x",
    "new": "plus",
    "attach": "paperclip",
    "voice": "mic",
    "send": "send-horizontal",
    "back": "chevron-left",
    "forward": "chevron-right",
    "expand": "chevron-down",
    "collapse": "chevron-up",
    "done": "check",
    "warn": "triangle-alert",
    "block": "octagon-alert",
    "info": "info",
    "menu": "ellipsis",
    "refresh": "refresh-cw",
    "external": "external-link",
    "filter": "filter",
    "sort-asc": "arrow-up",
    "sort-desc": "arrow-down",
    "calendar": "calendar",
    "download": "download",
}
"""Product icon name → Lucide file stem under :data:`_VENDOR_DIR`.

Templates address icons by the key. Where Lucide renamed an icon between
releases the pinned release's stem is the value, so an upstream rename is
absorbed here rather than in every call site.
"""

_VENDOR_DIR: Path = Path(__file__).resolve().parent / "static" / "vendor" / "lucide"

# Lucide ships a full <svg> document per file. The shell needs only the
# drawing instructions, because the wrapper is rebuilt here with the
# project's own geometry and accessibility attributes. Taking the inner
# markup also drops the leading "<!-- @license ... -->" comment that newer
# lucide-static releases put above the root element.
_INNER_RE = re.compile(r"<svg\b[^>]*>(.*?)</svg>", re.S)


@functools.cache
def _inner(stem: str) -> str:
    """Return the inner markup of the vendored ``stem`` SVG.

    Cached: the shell renders the same handful of icons on every request,
    and each miss is a file read plus a regex match.

    Args:
        stem: Lucide file stem, without the ``.svg`` suffix.

    Returns:
        The markup between the ``<svg>`` tags, whitespace-stripped.

    Raises:
        LookupError: When the file is absent or carries no ``<svg>``
            element — a vendored set that has drifted from :data:`ICONS`
            is a packaging error, not a render-time fallback.
    """
    path = _VENDOR_DIR / f"{stem}.svg"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LookupError(
            f"No vendored icon {stem!r} in {_VENDOR_DIR}; vendor the Lucide "
            "file rather than pointing ICONS at a stem the set does not ship."
        ) from exc
    match = _INNER_RE.search(raw)
    if match is None:
        raise LookupError(
            f"Vendored icon {stem!r} in {_VENDOR_DIR} has no <svg> element; "
            "re-vendor it from the pinned lucide-static release."
        )
    return match.group(1).strip()


def pf_icon(name: str, label: str | None = None, *, size: int = 16) -> Markup:
    """Render the ``name`` icon as an inline SVG.

    Registered as the ``pf_icon`` Jinja global. The icon inherits its
    colour from the surrounding text (``currentColor``), so a template
    styles it by styling its parent.

    Args:
        name: Product icon name — a key of :data:`ICONS`.
        label: Accessible name. Omit it for an icon that merely decorates
            adjacent text; supply it for an icon-only control, where it
            becomes the control's name.
        size: Edge length in CSS pixels.

    Returns:
        Markup for one ``<svg>`` element, safe to emit unescaped.

    Raises:
        LookupError: For a name absent from :data:`ICONS`. A typo in a
            template is drift, and failing the render surfaces it rather
            than shipping a silent gap in the chrome.
    """
    try:
        stem = ICONS[name]
    except KeyError as exc:
        raise LookupError(
            f"No icon named {name!r}; add it to web.icons.ICONS rather than "
            "naming a vendored file from the template."
        ) from exc

    if label is None:
        accessibility = 'aria-hidden="true"'
    else:
        accessibility = f'role="img" aria-label="{escape(label)}"'

    # Every interpolated part is either escaped (the label) or an int.
    return Markup(
        f'<svg class="pf-icon" width="{int(size)}" height="{int(size)}" '
        'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
        f'{accessibility} focusable="false">{_inner(stem)}</svg>'
    )
