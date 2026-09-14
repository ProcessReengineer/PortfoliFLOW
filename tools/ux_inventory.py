#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Re-runnable inventory of every user-visible surface, element and label in the web UI.

The instrument for the UX overhaul track (P-UX-0 and successors). It reads the
Jinja templates under the template root, the FastAPI route table, and the copy
that lives in Python rather than in a template, and writes three generated
artefacts under ``docs/ux/inventory/``:

``elements.csv``
    One row per (element, facet). An element that is both a button and an HTMX
    trigger yields two rows — that is what makes the per-area "buttons" and
    "HTMX triggers" counts in the summary meaningful. A template reachable from
    several Areas is inventoried once per Area.

``routes.csv``
    One row per route, with its Area, handler, rendered template, the
    authentication dependency that guards it, and how many script call sites
    reach it (``js_callers``, counted per method — a ``DELETE`` caller is not a
    caller of the sibling ``PUT`` on the same path).

``summary.md``
    The per-area baseline, flag counts, the frequent/long-label lists, and the
    full duplicate-label and synonym-candidate lists.

Standard library only, by design: the tool must run from a bare checkout with no
dependency beyond the project's own ``.venv`` (which it needs only for the
optional dynamic route pass).

Area attribution
----------------
Areas are *derived*, never hand-listed. The tool builds one graph over three
edge kinds and propagates Area labels across it to a fixed point:

* ``template --include--> template`` — ``{% include %}`` with a string literal,
  plus ``{% with %}`` / ``{% set %}`` assignments whose value is a template
  path (the project's ``areas/_section.html`` indirection).
* ``template --hx--> route`` — every ``hx-get`` / ``hx-post`` / ``form action``
  / ``href`` whose URL matches a route path pattern.
* ``route --renders--> template`` — the template names passed to
  ``TemplateResponse`` inside the handler (or a helper it calls in the same
  module).
* ``template --js--> route`` — a URL a script calls, linked back to the template
  element whose handler issues it (P-UX-0b). Carries the same weight as an
  ``hx-*`` edge; a scripted ``location.href`` is filtered like a plain ``href``,
  since navigating to a page is not composing one. An unlinked call adds no
  edge. ``--js-off`` skips the pass and reproduces the pre-JS artefacts exactly.

``{% extends %}`` is recorded but **not** traversed for Area attribution: layout
inheritance is shell chrome, not Area composition, so ``base.html`` stays in the
synthetic ``_shell`` Area rather than being counted nine times over.

Usage
-----
    source .venv/bin/activate
    python tools/ux_inventory.py --template-root web/templates --out docs/ux/inventory

Exit code 0 on success, 1 if any template raised a parse error. The outputs are
written either way.
"""

from __future__ import annotations

import argparse
import ast
import csv
import inspect
import re
import sys
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary and heuristics — the tunable part of the tool
# ---------------------------------------------------------------------------

#: The synthetic Area holding the shell chrome (sidebar, status bar, command
#: palette) — everything reached through ``{% extends "base.html" %}`` rather
#: than through an Area body.
SHELL_AREA = "_shell"

#: Templates that could not be reached from any Area seed.
UNASSIGNED = "unassigned"

#: HTML elements that never carry children.
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)

#: Tags whose end tag is optional; a second start tag implicitly closes the first.
AUTO_CLOSE_TAGS = frozenset({"li", "p", "option", "tr", "td", "th", "dt", "dd"})

#: The HTMX verbs that make an element a trigger.
HX_VERBS = ("hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")

#: Keyword-argument and dict-key names that carry user-visible copy in Python.
COPY_KEYS = frozenset(
    {
        "blurb",
        "caption",
        "crumb",
        "description",
        "headline",
        "heading",
        "hint",
        "kicker",
        "label",
        "message",
        "pill",
        "placeholder",
        "subtitle",
        "summary",
        "tile",
        "title",
    }
)

#: Substrings that mark internal vocabulary leaking into user-facing copy. The
#: first thirteen are the P-UX-0 starting heuristic; the rest are siblings found
#: in this tree and are listed as such in the report.
INTERNAL_NAMES = (
    # P-UX-0 starting list
    "irene",
    "shirley",
    "watch desk",
    "impact",
    "overlay",
    "emission",
    "reported",
    "blotter",
    "composer",
    "wizard",
    "figi",
    "rls",
    "tenant",
    # siblings observed in this tree
    "sentinel",
    "repace",
    "adr-",
    "htmx",
    "datastore",
    "stammtabelle",
    "upsert",
    "sidecar",
)

#: Class names that mark an element as a status pill. Matched against the BEM
#: *leaf* of a class token (see :func:`bem_leaf`), so ``pf-section__pill`` and
#: ``badge--warn`` match while ``pf-statusbar__item`` does not.
PILL_CLASS_HINTS = frozenset({"pill", "badge", "chip", "status"})

#: Class names that mark an element as a tile or card. Matched on the BEM leaf.
TILE_CLASS_HINTS = frozenset({"tile", "card"})

#: Kinds where a nested match is suppressed: a card inside a card, or the title
#: inside an empty state, is one element for inventory purposes, not two.
NESTING_SUPPRESSED = frozenset({"tile", "pill", "empty_state"})

_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_TAG_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_BLOCK_OPEN_RE = re.compile(r"\{%-?\s*block\s+([A-Za-z_][A-Za-z0-9_]*)")
_BLOCK_CLOSE_RE = re.compile(r"\{%-?\s*endblock\b")
_INCLUDE_RE = re.compile(r"\{%-?\s*include\s+[\"']([^\"']+)[\"']")
_EXTENDS_RE = re.compile(r"\{%-?\s*extends\s+[\"']([^\"']+)[\"']")
_WITH_RE = re.compile(r"\{%-?\s*(?:with|set)\b(.*?)-?%\}", re.DOTALL)
_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.DOTALL)
_WS_RE = re.compile(r"\s+")
_AREA_BODY_RE = re.compile(r"_([a-z][a-z0-9_]*)_body\.html$")
_AREA_PAGE_RE = re.compile(r"^areas/([a-z][a-z0-9_]*)\.html$")
_ROUTE_PARAM_RE = re.compile(r"\{[^/{}]+\}")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_SNAKE_RE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")
_LEADING_UNDERSCORE_RE = re.compile(r"(?<![A-Za-z0-9])_[A-Za-z]")
_KEBAB_WHOLE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_KEBAB_ANY_RE = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?:;])\s+")
_CAPITALISED_RE = re.compile(r"^[A-Z][a-z]+$")


# ---------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------


@dataclass
class Element:
    """One inventoried facet of one user-visible element."""

    area: str
    template: str
    block: str
    source_kind: str
    element_kind: str
    visible_text: str
    target: str
    hx_target: str
    source_file: str
    source_line: int
    flags: list[str] = field(default_factory=list)

    def as_row(self) -> list[str]:
        """Return the CSV row for this element."""
        return [
            self.area,
            self.template,
            self.block,
            self.source_kind,
            self.element_kind,
            self.visible_text,
            self.target,
            self.hx_target,
            self.source_file,
            str(self.source_line),
            "|".join(sorted(set(self.flags))),
        ]


@dataclass
class RouteRow:
    """One inventoried route."""

    area: str
    methods: str
    path: str
    handler_module: str
    handler_name: str
    source_file: str
    source_line: int
    template: str
    auth_required: str
    js_callers: int = 0

    def as_row(self) -> list[str]:
        """Return the CSV row for this route."""
        return [
            self.area,
            self.methods,
            self.path,
            self.handler_module,
            self.handler_name,
            self.source_file,
            str(self.source_line),
            self.template,
            self.auth_required,
            str(self.js_callers),
        ]


@dataclass
class TemplateFacts:
    """Everything the template pass learns about one template file."""

    path: str
    includes: list[str] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    hx_urls: list[str] = field(default_factory=list)
    nav_urls: list[str] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parse_error: str = ""


# ---------------------------------------------------------------------------
# Jinja scrubbing
# ---------------------------------------------------------------------------


def _blank_preserving_lines(match: re.Match[str]) -> str:
    """Replace a Jinja tag with blanks, keeping every newline in place."""
    return "".join("\n" if ch == "\n" else " " for ch in match.group(0))


def _escape_expression(match: re.Match[str]) -> str:
    """Keep a ``{{ … }}`` expression as literal text the HTML parser cannot choke on."""
    text = match.group(0)
    return text.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def scrub_jinja(source: str) -> str:
    """Return *source* with Jinja comments and tags blanked and expressions kept as text.

    Newlines survive every substitution, so ``HTMLParser.getpos()`` line numbers
    are the line numbers of the original file. ``{{ … }}`` expressions are kept
    verbatim (with the three HTML-significant characters entity-escaped, which
    ``convert_charrefs`` undoes again in text nodes and attribute values).

    Args:
        source: Raw template text.

    Returns:
        Text safe to feed to :class:`html.parser.HTMLParser`.
    """
    text = _COMMENT_RE.sub(_blank_preserving_lines, source)
    text = _JINJA_TAG_RE.sub(_blank_preserving_lines, text)
    return _JINJA_EXPR_RE.sub(_escape_expression, text)


def line_of(source: str, pos: int) -> int:
    """Return the 1-based line number of character offset *pos* in *source*."""
    return source.count("\n", 0, pos) + 1


def collapse(text: str) -> str:
    """Collapse whitespace runs in *text* and strip the ends."""
    return _WS_RE.sub(" ", text).strip()


def block_ranges(source: str) -> list[tuple[str, int, int]]:
    """Return ``(name, first_line, last_line)`` for every ``{% block %}`` in *source*."""
    events: list[tuple[int, str, str]] = []
    for match in _BLOCK_OPEN_RE.finditer(source):
        events.append((match.start(), "open", match.group(1)))
    for match in _BLOCK_CLOSE_RE.finditer(source):
        events.append((match.start(), "close", ""))
    events.sort()
    stack: list[tuple[str, int]] = []
    ranges: list[tuple[str, int, int]] = []
    for pos, kind, name in events:
        if kind == "open":
            stack.append((name, line_of(source, pos)))
        elif stack:
            open_name, open_line = stack.pop()
            ranges.append((open_name, open_line, line_of(source, pos)))
    for open_name, open_line in stack:
        ranges.append((open_name, open_line, line_of(source, len(source))))
    return ranges


# ---------------------------------------------------------------------------
# Flag heuristics
# ---------------------------------------------------------------------------


def is_dynamic(text: str) -> bool:
    """Return True when *text* still contains a Jinja expression."""
    return "{{" in text


def has_abbreviation(text: str) -> bool:
    """Return True when *text* carries an abbreviation token.

    An all-caps token of 2–6 letters (``AUM``, ``SAA``, ``NAV``, ``FX``) counts,
    and so does any mixed-case token with two or more capitals (``AnlV``,
    ``TVPI``), per the P-UX-0 definition.
    """
    for token in _TOKEN_RE.findall(text):
        capitals = sum(1 for ch in token if ch.isupper())
        if capitals >= 2 and 2 <= len(token) <= 8:
            return True
    return False


def identifier_leaks(text: str) -> list[str]:
    """Return the identifier-leak flags *text* earns.

    ``identifier-leak`` is the high-confidence reading: a ``snake_case`` token
    anywhere, a ``pf-`` prefix, a ``_``-led name, or a label that *is* nothing
    but a kebab-case identifier. ``identifier-leak-loose`` is the literal P-UX-0
    heuristic, which flags kebab-case anywhere and therefore also catches
    ordinary hyphenated English ("read-only", "cash-flow"); it is reported
    separately so the noise stays separable from the signal.
    """
    flags: list[str] = []
    stripped = text.strip()
    strict = bool(
        _SNAKE_RE.search(text)
        or "pf-" in text
        or _LEADING_UNDERSCORE_RE.search(text)
        or _KEBAB_WHOLE_RE.match(stripped)
    )
    if strict:
        flags.append("identifier-leak")
    if _KEBAB_ANY_RE.search(text) and "identifier-leak" not in flags:
        flags.append("identifier-leak-loose")
    return flags


def internal_names(text: str) -> list[str]:
    """Return the internal-vocabulary terms found in *text*, lower-cased."""
    lowered = text.lower()
    return [name for name in INTERNAL_NAMES if name in lowered]


def bem_leaf(token: str) -> str:
    """Return the BEM leaf of a class token.

    ``pf-section__pill`` → ``pill``; ``badge--warn`` → ``badge``;
    ``pf-statusbar__item`` → ``item``; ``inv-pill`` → ``pill``. Matching on the
    leaf rather than on a substring is what keeps a whole status *bar* from
    being inventoried as several hundred status *pills*.
    """
    return token.split("__")[-1].split("--")[0].split("-")[-1]


def has_class_part(classes: list[str], part: str) -> bool:
    """Return True when *part* appears as a hyphen- or underscore-delimited word."""
    return any(part in re.split(r"[-_]+", token) for token in classes)


def distinct_nouns(texts: list[str]) -> set[str]:
    """Return the distinct-noun set for a list of static visible texts.

    Heuristic, as documented in ``summary.md``: take every capitalised word
    (``^[A-Z][a-z]+$``) in static text, drop the first word of every sentence
    (sentences split on ``.!?:;``), and drop abbreviations (any token with two
    or more capitals). What survives is a proxy for the product's noun
    vocabulary — the thing the overhaul is trying to shrink.
    """
    nouns: set[str] = set()
    for text in texts:
        if is_dynamic(text):
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            tokens = _TOKEN_RE.findall(sentence)
            for index, token in enumerate(tokens):
                if index == 0:
                    continue
                if sum(1 for ch in token if ch.isupper()) >= 2:
                    continue
                if _CAPITALISED_RE.match(token):
                    nouns.add(token)
    return nouns


# ---------------------------------------------------------------------------
# HTML element collection
# ---------------------------------------------------------------------------


@dataclass
class _Frame:
    """One open HTML element during the parse."""

    tag: str
    attrs: dict[str, str]
    classes: list[str]
    line: int
    texts: list[str] = field(default_factory=list)
    options: int = 0


class ElementCollector(HTMLParser):
    """Collect the user-visible facets of every element in one template fragment."""

    def __init__(
        self,
        template: str,
        source_file: str,
        line_offset: int = 0,
        extra_flags: list[str] | None = None,
    ) -> None:
        """Initialise the collector.

        Args:
            template: Template path relative to the template root.
            source_file: Repository-relative path recorded on every row.
            line_offset: Added to every reported line; lets an inline HTML
                fragment lifted out of a ``{% with %}`` block report the line of
                the block it came from.
            extra_flags: Flags stamped on every row this collector produces.
        """
        super().__init__(convert_charrefs=True)
        self.template = template
        self.source_file = source_file
        self.line_offset = line_offset
        self.rows: list[Element] = []
        self.hx_urls: list[str] = []
        self.nav_urls: list[str] = []
        self.warnings: list[str] = []
        self._stack: list[_Frame] = []
        self._form_actions: list[str] = []
        self._raw_tag: str = ""
        self._extra_flags: list[str] = list(extra_flags or [])

    # -- helpers ---------------------------------------------------------

    def _line(self) -> int:
        return self.getpos()[0] + self.line_offset

    def _push_text(self, text: str) -> None:
        if self._stack:
            self._stack[-1].texts.append(text)

    @staticmethod
    def _classes(attrs: dict[str, str]) -> list[str]:
        raw = _JINJA_EXPR_RE.sub(" ", attrs.get("class", ""))
        return [token for token in raw.split() if token]

    def _enclosing_form_action(self) -> str:
        return self._form_actions[-1] if self._form_actions else ""

    # -- parser hooks ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a frame for *tag*, or emit it immediately when it is void."""
        attr_map = {name: (value or "") for name, value in attrs}
        classes = self._classes(attr_map)
        if tag in ("script", "style"):
            self._raw_tag = tag
            self._stack.append(_Frame(tag, attr_map, classes, self._line()))
            return
        if tag == "form":
            self._form_actions.append(attr_map.get("action", ""))
        if tag == "option" and self._stack:
            for frame in reversed(self._stack):
                if frame.tag == "select":
                    frame.options += 1
                    break
        self._collect_urls(attr_map)
        if tag in VOID_TAGS:
            self._emit(_Frame(tag, attr_map, classes, self._line()))
            return
        if tag in AUTO_CLOSE_TAGS and self._stack and self._stack[-1].tag == tag:
            self._close_frame(self._stack.pop())
        self._stack.append(_Frame(tag, attr_map, classes, self._line()))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle an explicitly self-closed tag as a childless element."""
        attr_map = {name: (value or "") for name, value in attrs}
        self._collect_urls(attr_map)
        self._emit(_Frame(tag, attr_map, self._classes(attr_map), self._line()))

    def handle_endtag(self, tag: str) -> None:
        """Close the innermost matching frame, auto-closing anything left open above it."""
        if self._raw_tag == tag:
            self._raw_tag = ""
        if tag == "form" and self._form_actions:
            self._form_actions.pop()
        depth = -1
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                depth = index
                break
        if depth < 0:
            self.warnings.append(f"unbalanced </{tag}> at line {self._line()}")
            return
        while len(self._stack) > depth:
            self._close_frame(self._stack.pop())

    def handle_data(self, data: str) -> None:
        """Accumulate text into the innermost open element."""
        if self._raw_tag:
            return
        self._push_text(data)

    def close(self) -> None:
        """Flush any element left open at end of fragment."""
        super().close()
        while self._stack:
            frame = self._stack.pop()
            if frame.tag not in AUTO_CLOSE_TAGS and frame.tag not in ("script", "style"):
                self.warnings.append(f"unclosed <{frame.tag}> opened at line {frame.line}")
            self._close_frame(frame)

    # -- emission --------------------------------------------------------

    def _close_frame(self, frame: _Frame) -> None:
        text = "".join(frame.texts)
        self._push_text(text)
        if frame.tag in ("script", "style"):
            return
        self._emit(frame, text)

    def _collect_urls(self, attrs: dict[str, str]) -> None:
        for key in (*HX_VERBS, "action", "formaction"):
            value = attrs.get(key, "").strip()
            if value.startswith("/"):
                self.hx_urls.append(value)
        href = attrs.get("href", "").strip()
        if href.startswith("/"):
            self.nav_urls.append(href)

    def _ancestor_claims(self, kind: str) -> bool:
        """Return True when an element still open above this one already carries *kind*."""
        for frame in self._stack:
            leaves = {bem_leaf(token) for token in frame.classes}
            if kind == "tile" and leaves & TILE_CLASS_HINTS:
                return True
            if kind == "pill" and leaves & PILL_CLASS_HINTS:
                return True
            if kind == "empty_state" and has_class_part(frame.classes, "empty"):
                return True
        return False

    def _hx_target_url(self, attrs: dict[str, str]) -> tuple[str, str]:
        for verb in HX_VERBS:
            if verb in attrs:
                return verb.removeprefix("hx-").upper(), attrs[verb].strip()
        return "", ""

    def _emit(self, frame: _Frame, raw_text: str = "") -> None:
        attrs = frame.attrs
        classes = frame.classes
        text = collapse(raw_text)
        tag = frame.tag
        verb, hx_url = self._hx_target_url(attrs)
        hx_target = attrs.get("hx-target", "")
        href = attrs.get("href", "").strip()
        leaves = {bem_leaf(token) for token in classes}

        if tag == "input" and attrs.get("type", "") in ("submit", "button"):
            text = collapse(attrs.get("value", ""))
        # A form control takes its accessible name from its <label>, and a
        # hidden poller is not a control at all — neither is an icon-only
        # button, whatever their own text content says.
        named_by_label = tag in ("input", "select", "textarea") and attrs.get("type", "") not in (
            "submit",
            "button",
        )

        def add(kind: str, value: str, target: str, extra: list[str] | None = None) -> None:
            if kind in NESTING_SUPPRESSED and self._ancestor_claims(kind):
                return
            flags = list(self._extra_flags)
            flags.extend(extra or [])
            if is_dynamic(value):
                flags.append("dynamic")
            else:
                if has_abbreviation(value):
                    flags.append("abbrev")
                flags.extend(identifier_leaks(value))
                if internal_names(value):
                    flags.append("internal-name")
            if "hidden" in attrs:
                flags.append("hidden")
            elif kind in ("button", "link", "htmx", "tile") and not text and not named_by_label:
                flags.append("icon-only")
                if not attrs.get("title") and not attrs.get("aria-label"):
                    flags.append("no-accessible-name")
            self.rows.append(
                Element(
                    area="",
                    template=self.template,
                    block="",
                    source_kind="template",
                    element_kind=kind,
                    visible_text=value,
                    target=collapse(target),
                    hx_target=collapse(hx_target),
                    source_file=self.source_file,
                    source_line=frame.line,
                    flags=flags,
                )
            )

        if tag in ("h1", "h2", "h3", "h4"):
            add("heading", text, "")
        if "pf-area__header" in classes and tag not in ("h1", "h2", "h3", "h4"):
            add("area_header", text, "")
        if any("crumb" in name for name in classes):
            add("crumb", text, href)
        if leaves & TILE_CLASS_HINTS:
            add("tile", text, hx_url or href)
        is_button = (
            tag == "button"
            or (tag == "input" and attrs.get("type", "") in ("submit", "button"))
            or any("btn" in name for name in classes)
        )
        if is_button:
            target = hx_url or attrs.get("formaction", "") or self._enclosing_form_action() or href
            add("button", text, target)
        if tag == "a" and href and not href.startswith(("#", "javascript:")):
            add("link", text, href)
        if verb:
            # The verb rides in the flags, not in the target: a button and its
            # own HTMX facet must share one target string, or every HTMX button
            # in the tree would flag itself as a duplicate label.
            add("htmx", text, hx_url, [f"verb:{verb}"])
        if tag == "label":
            add("form_label", text, attrs.get("for", ""))
        if "placeholder" in attrs:
            add("placeholder", collapse(attrs["placeholder"]), attrs.get("name", ""))
        if tag == "select":
            add("select", text, attrs.get("name", ""), [f"options:{frame.options}"])
        if leaves & PILL_CLASS_HINTS:
            add("pill", text, "")
        if has_class_part(classes, "empty"):
            add("empty_state", text, "")
        for attr_name in ("title", "aria-label"):
            if attrs.get(attr_name, "").strip():
                extra = [f"from:{attr_name}"]
                if not text:
                    extra.append("icon-only")
                add("tooltip", collapse(attrs[attr_name]), hx_url or href, extra)


def parse_fragment(
    template: str,
    source_file: str,
    fragment: str,
    line_offset: int = 0,
    extra_flags: list[str] | None = None,
) -> tuple[ElementCollector, str]:
    """Run the collector over one HTML fragment.

    Args:
        template: Template path relative to the template root.
        source_file: Repository-relative path recorded on every row.
        fragment: Scrubbed HTML text.
        line_offset: Added to every reported line number.
        extra_flags: Flags stamped on every row from this fragment.

    Returns:
        ``(collector, parse_error)``. ``parse_error`` is the empty string when
        the fragment parsed without raising.
    """
    collector = ElementCollector(template, source_file, line_offset, extra_flags)
    try:
        collector.feed(fragment)
        collector.close()
    except Exception as exc:  # noqa: BLE001 - a broken template must not stop the run
        return collector, f"{type(exc).__name__}: {exc}"
    return collector, ""


# ---------------------------------------------------------------------------
# Template pass
# ---------------------------------------------------------------------------


def scan_template(root: Path, repo_root: Path, path: Path) -> TemplateFacts:
    """Read one template and return everything the inventory needs from it."""
    rel = path.relative_to(root).as_posix()
    source_file = path.relative_to(repo_root).as_posix()
    source = path.read_text(encoding="utf-8")
    facts = TemplateFacts(path=rel)
    facts.includes.extend(_INCLUDE_RE.findall(source))
    facts.extends.extend(_EXTENDS_RE.findall(source))

    blocks = block_ranges(source)

    # {% with %} / {% set %} string literals: template paths become include
    # edges, section copy becomes rows, inline HTML is parsed as a fragment.
    for with_match in _WITH_RE.finditer(source):
        with_line = line_of(source, with_match.start())
        for assign in _ASSIGN_RE.finditer(with_match.group(1)):
            name = assign.group(1)
            value = assign.group(3) if assign.group(3) is not None else assign.group(4)
            if value is None:
                continue
            if value.endswith(".html"):
                facts.includes.append(value)
                continue
            if "<" in value and ">" in value:
                collector, error = parse_fragment(
                    rel, source_file, scrub_jinja(value), with_line - 1, ["inline-html"]
                )
                facts.elements.extend(collector.rows)
                facts.hx_urls.extend(collector.hx_urls)
                facts.nav_urls.extend(collector.nav_urls)
                facts.warnings.extend(collector.warnings)
                if error:
                    facts.parse_error = error
                continue
            if name.startswith("section_") and name != "section_slug" and value.strip():
                kind = f"jinja_{name.removeprefix('section_')}"
                flags: list[str] = []
                if is_dynamic(value):
                    flags.append("dynamic")
                else:
                    if has_abbreviation(value):
                        flags.append("abbrev")
                    flags.extend(identifier_leaks(value))
                    if internal_names(value):
                        flags.append("internal-name")
                facts.elements.append(
                    Element(
                        area="",
                        template=rel,
                        block="",
                        source_kind="jinja",
                        element_kind=kind,
                        visible_text=collapse(value),
                        target="",
                        hx_target="",
                        source_file=source_file,
                        source_line=with_line,
                        flags=flags,
                    )
                )

    collector, error = parse_fragment(rel, source_file, scrub_jinja(source))
    facts.elements.extend(collector.rows)
    facts.hx_urls.extend(collector.hx_urls)
    facts.nav_urls.extend(collector.nav_urls)
    facts.warnings.extend(collector.warnings)
    if error:
        facts.parse_error = error

    for element in facts.elements:
        element.block = _block_for(blocks, element.source_line) or _heading_before(
            facts.elements, element
        )
    return facts


def _block_for(blocks: list[tuple[str, int, int]], line: int) -> str:
    """Return the innermost ``{% block %}`` name containing *line*."""
    best = ""
    best_span = None
    for name, start, end in blocks:
        if start <= line <= end:
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = name, span
    return best


def _heading_before(elements: list[Element], element: Element) -> str:
    """Return the nearest preceding heading text, for templates with no blocks."""
    best = ""
    best_line = -1
    for other in elements:
        if other.element_kind != "heading":
            continue
        if best_line < other.source_line <= element.source_line:
            best, best_line = other.visible_text, other.source_line
    return best


# ---------------------------------------------------------------------------
# Route pass
# ---------------------------------------------------------------------------


def load_app() -> tuple[Any, str]:
    """Import the FastAPI application, trying the known construction seams.

    Returns:
        ``(app, note)``; ``app`` is ``None`` when every candidate failed, and
        ``note`` records what happened for the report.
    """
    candidates = (("web.main", "app"), ("web.main", "create_app"), ("web.app", "app"))
    errors: list[str] = []
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
            target = getattr(module, attr)
            app = target() if callable(target) and attr == "create_app" else target
            return app, f"dynamic pass via {module_name}.{attr}"
        except Exception as exc:  # noqa: BLE001 - fall through to the static pass
            errors.append(f"{module_name}.{attr}: {type(exc).__name__}: {exc}")
    return None, "import failed — " + "; ".join(errors)


@dataclass
class ModuleIndex:
    """What one route module says about the templates it renders.

    Route handlers in this project rarely name their template inline. They pass
    a module-level constant (``TemplateResponse(request, _SECTION_TEMPLATE, …)``),
    or they call a local ``_render`` helper that prepends a directory
    (``f"_partials/transactions/{template}"``). Resolving both is what keeps the
    HTMX response partials — most of the Transactions, Planning Desk and Cases
    surfaces — from landing in ``unassigned``.
    """

    constants: dict[str, str] = field(default_factory=dict)
    prefixes: list[str] = field(default_factory=list)
    calls: dict[str, set[str]] = field(default_factory=dict)
    candidates: dict[str, set[str]] = field(default_factory=dict)
    module_pool: set[str] = field(default_factory=set)


def index_module(source: str) -> ModuleIndex:
    """Build the template index for one Python module's source."""
    index = ModuleIndex()
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return index

    for node in tree.body:
        name = ""
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        if name and isinstance(value, ast.Constant) and isinstance(value.value, str):
            index.constants[name] = value.value
        if value is not None:
            for inner in ast.walk(value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    index.module_pool.add(inner.value)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "TemplateResponse":
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):
                head = arg.values[0] if arg.values else None
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    index.prefixes.append(head.value)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        strings: set[str] = set()
        called: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                strings.add(inner.value)
            elif isinstance(inner, ast.Name) and inner.id in index.constants:
                strings.add(index.constants[inner.id])
            elif isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                called.add(inner.func.id)
        index.candidates[node.name] = strings
        index.calls[node.name] = called
    return index


def resolve_templates(
    index: ModuleIndex, function: str, known: set[str], depth: int = 3
) -> list[str]:
    """Return the templates *function* can render, following local helpers *depth* deep.

    When nothing resolves from the handler and its helpers, the module-level
    string pool is consulted as a fallback. That is how the flow-keyed lookup
    tables — ``cases.py``'s composer dict, ``transactions.py``'s ``_FLOWS`` —
    give up their template names: the handler itself names no template, so the
    module's own literals are the only evidence there is. It is deliberately a
    last resort, because it attributes every template in the module to the
    handler rather than the one it actually renders.
    """

    def resolve(candidate: str) -> list[str]:
        out = [candidate] if candidate in known else []
        out.extend(prefix + candidate for prefix in index.prefixes if prefix + candidate in known)
        return out

    seen: set[str] = set()
    found: list[str] = []
    frontier = {function}
    for _ in range(depth):
        current = frontier - seen
        if not current:
            break
        seen |= current
        frontier = set()
        for name in sorted(current):  # sorted: the output must not depend on set order
            for candidate in sorted(index.candidates.get(name, set())):
                for resolved in resolve(candidate):
                    if resolved not in found:
                        found.append(resolved)
            frontier |= index.calls.get(name, set())
    if found:
        return found
    for candidate in sorted(index.module_pool):
        for resolved in resolve(candidate):
            if resolved not in found:
                found.append(resolved)
    return found


def _auth_of(route: Any) -> str:
    """Return the strongest authentication dependency guarding *route*."""
    seen: set[str] = set()
    stack = [getattr(route, "dependant", None)]
    while stack:
        dependant = stack.pop()
        if dependant is None:
            continue
        call = getattr(dependant, "call", None)
        if call is not None:
            seen.add(getattr(call, "__name__", ""))
        stack.extend(getattr(dependant, "dependencies", []))
    if "require_super_admin" in seen:
        return "super_admin"
    if "require_session" in seen:
        return "session"
    if "get_authenticated_user" in seen:
        return "user"
    return "none"


def dynamic_routes(
    app: Any, repo_root: Path, known: set[str]
) -> tuple[list[RouteRow], dict[str, list[str]]]:
    """Build the route rows from a live FastAPI application."""
    rows: list[RouteRow] = []
    renders: dict[str, list[str]] = defaultdict(list)
    indexes: dict[str, ModuleIndex] = {}
    for route in getattr(app, "routes", []):
        endpoint = getattr(route, "endpoint", None)
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if endpoint is None or not methods:
            continue
        code = getattr(endpoint, "__code__", None)
        source_file = ""
        source_line = 0
        if code is not None:
            try:
                source_file = Path(code.co_filename).resolve().relative_to(repo_root).as_posix()
            except ValueError:
                source_file = code.co_filename
            source_line = code.co_firstlineno
        module_name = getattr(endpoint, "__module__", "")
        if module_name not in indexes:
            indexes[module_name] = index_module(_module_source(module_name))
        templates = resolve_templates(
            indexes[module_name], getattr(endpoint, "__name__", ""), known
        )
        rows.append(
            RouteRow(
                area="",
                methods="|".join(sorted(set(methods) - {"HEAD"})),
                path=path,
                handler_module=module_name,
                handler_name=getattr(endpoint, "__name__", ""),
                source_file=source_file,
                source_line=source_line,
                template=";".join(templates),
                auth_required=_auth_of(route),
            )
        )
        renders[path].extend(templates)
    return rows, renders


def _module_source(module_name: str) -> str:
    """Return the source of an imported module, or the empty string."""
    module = sys.modules.get(module_name)
    if module is None:
        return ""
    try:
        return inspect.getsource(module)
    except (OSError, TypeError):
        return ""


_DECORATOR_RE = re.compile(
    r"@(?:router|app)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.MULTILINE
)
_DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def static_routes(repo_root: Path, known: set[str]) -> tuple[list[RouteRow], dict[str, list[str]]]:
    """Build the route rows by static scan, for when the application will not import."""
    rows: list[RouteRow] = []
    renders: dict[str, list[str]] = defaultdict(list)
    for path in sorted((repo_root / "web" / "routes").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo_root).as_posix()
        index = index_module(source)
        for match in _DECORATOR_RE.finditer(source):
            verb, url = match.group(1).upper(), match.group(2)
            tail = source[match.end() :]
            def_match = _DEF_RE.search(tail)
            handler = def_match.group(1) if def_match else ""
            templates = resolve_templates(index, handler, known)
            window = source[match.start() : match.end() + 2000]
            auth = "none"
            if "require_super_admin" in window:
                auth = "super_admin"
            elif "require_session" in window:
                auth = "session"
            elif "get_authenticated_user" in window:
                auth = "user"
            rows.append(
                RouteRow(
                    area="",
                    methods=verb,
                    path=url,
                    handler_module=f"web.routes.{path.stem}",
                    handler_name=handler,
                    source_file=rel,
                    source_line=line_of(source, match.start()),
                    template=";".join(templates),
                    auth_required=auth,
                )
            )
            renders[url].extend(templates)
    return rows, renders


# ---------------------------------------------------------------------------
# Python copy pass
# ---------------------------------------------------------------------------


_NAMED_COPY_MODULE_RE = re.compile(r"(copy|labels?|vocabulary|constants)")


def python_copy_files(repo_root: Path) -> list[Path]:
    """Return the Python files scanned for user-visible copy.

    Everything under ``web/`` (the surface's own glue, including ``web/shell.py``
    where the nine Area labels live), plus any module anywhere in the project
    whose *name* contains ``copy``, ``label``, ``vocabulary`` or ``constants``.
    """
    files = sorted(p for p in (repo_root / "web").rglob("*.py") if "__pycache__" not in p.parts)
    for top in ("services", "modules", "core"):
        base = repo_root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if _NAMED_COPY_MODULE_RE.search(path.stem):
                files.append(path)
    return files


def scan_python_copy(repo_root: Path, path: Path) -> list[Element]:
    """Return the user-visible copy defined in one Python module."""
    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(repo_root).as_posix()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    rows: list[Element] = []
    named_module = bool(_NAMED_COPY_MODULE_RE.search(path.stem))

    def record(key: str, value: str, line: int) -> None:
        text = collapse(value)
        if not text:
            return
        flags: list[str] = []
        if has_abbreviation(text):
            flags.append("abbrev")
        flags.extend(identifier_leaks(text))
        if internal_names(text):
            flags.append("internal-name")
        rows.append(
            Element(
                area="",
                template="",
                block=path.stem,
                source_kind="python",
                element_kind=f"python_{key}",
                visible_text=text,
                target="",
                hx_target="",
                source_file=rel,
                source_line=line,
                flags=flags,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg in COPY_KEYS
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    record(keyword.arg, keyword.value.value, keyword.value.lineno)
        elif isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                if not isinstance(key_node, ast.Constant) or key_node.value not in COPY_KEYS:
                    continue
                if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                    record(str(key_node.value), value_node.value, value_node.lineno)

    if named_module:
        for node in tree.body:
            target_name = ""
            value_node = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_name, value_node = node.target.id, node.value
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                target_name, value_node = node.targets[0].id, node.value
            if not target_name or not isinstance(value_node, ast.Constant):
                continue
            if isinstance(value_node.value, str) and " " in value_node.value.strip():
                record("constant", value_node.value, value_node.lineno)
    return rows


# ---------------------------------------------------------------------------
# Area attribution
# ---------------------------------------------------------------------------


#: Templates pinned to the shell Area. They are the layout and the HTMX
#: fragment wrapper, reached from every Area by ``{% extends %}`` or by the
#: route's own fragment branch; letting them accumulate Areas would count the
#: sidebar nine times over, so they are frozen instead.
SHELL_SEEDS = frozenset({"base.html", "_auth_base.html", "_partials/area_fragment.html"})


def seed_areas(templates: dict[str, TemplateFacts]) -> dict[str, set[str]]:
    """Return the Area seeds derived from template paths alone."""
    seeds: dict[str, set[str]] = defaultdict(set)
    for rel in templates:
        if rel in SHELL_SEEDS:
            seeds[rel].add(SHELL_AREA)
            continue
        body = _AREA_BODY_RE.search(rel)
        if body and "/areas/" in rel:
            seeds[rel].add(body.group(1))
            continue
        page = _AREA_PAGE_RE.match(rel)
        if page and not rel.endswith("_section.html"):
            seeds[rel].add(page.group(1))
            continue
        head, _, tail = rel.partition("/")
        if tail and head not in ("_partials", "areas"):
            # A full-page surface living outside the nine Area long-scroll
            # pages — /investments/* and /super-admin/* today. Named after its
            # own directory rather than folded into whichever Area links to it.
            seeds[rel].add(head)
    return seeds


def page_templates(templates: dict[str, TemplateFacts]) -> set[str]:
    """Return the templates that render a whole page (they extend the shell layout)."""
    pages: set[str] = set()
    for rel in templates:
        seen: set[str] = set()
        stack = [rel]
        while stack:
            current = stack.pop()
            if current in seen or current not in templates:
                continue
            seen.add(current)
            if "base.html" in templates[current].extends:
                pages.add(rel)
                break
            stack.extend(templates[current].extends)
    return pages


def route_matcher(paths: list[str]) -> list[tuple[re.Pattern[str], str]]:
    """Compile each route path pattern into a regex a concrete URL can be matched against.

    ``/api/cases/{case_id}/note`` becomes ``^/api/cases/[^/]+/note/?$``, so the
    ``hx-post="/api/cases/{{ case.id }}/note"`` in a template resolves to the
    route it calls. Literal paths are tried before parameterised ones — without
    that, ``/investments/new`` would resolve to ``/investments/{investment_id}``.
    """
    compiled: list[tuple[re.Pattern[str], str]] = []
    order = sorted(set(paths), key=lambda p: (len(_ROUTE_PARAM_RE.findall(p)), -len(p)))
    for path in order:
        segments = _ROUTE_PARAM_RE.split(path)
        params = _ROUTE_PARAM_RE.findall(path)
        body = ""
        for index, literal in enumerate(segments):
            body += re.escape(literal)
            if index < len(params):
                body += "[^/]+"
        try:
            compiled.append((re.compile(f"^{body}/?$"), path))
        except re.error:
            continue
    return compiled


def normalise_url(url: str) -> str:
    """Strip the query string and replace Jinja expressions with a path-segment wildcard."""
    url = url.split("?", 1)[0].split("#", 1)[0]
    return _JINJA_EXPR_RE.sub("_", url)


def match_route(url: str, matchers: list[tuple[re.Pattern[str], str]]) -> str:
    """Return the route path *url* belongs to, or the empty string."""
    candidate = normalise_url(url)
    for pattern, path in matchers:
        if pattern.match(candidate):
            return path
    return ""


def area_from_path(path: str, area_urls: dict[str, str]) -> set[str]:
    """Return the Area a route path encodes, from the Area URL space alone."""
    for url, slug in area_urls.items():
        if path == url or path.startswith(url + "/"):
            return {slug}
        if path == f"/api{url}" or path.startswith(f"/api{url}/"):
            return {slug}
    if path.startswith("/super-admin"):
        return {"super_admin"}
    return set()


# ---------------------------------------------------------------------------
# JavaScript pass — the scripts the template pass cannot see
# ---------------------------------------------------------------------------

#: Characters after which a ``/`` opens a regular-expression literal rather than
#: a division. The empty string covers the start of the file.
_JS_REGEX_PRECEDERS = frozenset("(,=:[!&|?{};+-*%~^<>") | {""}

#: Identifier prefixes dropped from a concatenated URL — they contribute an
#: origin, not a path.
_JS_ORIGIN_PREFIXES = ("window.location.origin", "location.origin", "baseUrl", "BASE_URL")

#: The placeholder a non-literal path segment collapses to.
_JS_PARAM = "{param}"

_JS_STRING_RE = re.compile(r"\"([^\"\\]*(?:\\.[^\"\\]*)*)\"|'([^'\\]*(?:\\.[^'\\]*)*)'")

#: The six call shapes that reach a route from a script.
_JS_FETCH_RE = re.compile(r"\bfetch\s*\(")
_JS_HTMX_AJAX_RE = re.compile(r"\bhtmx\s*\.\s*ajax\s*\(")
_JS_EVENTSOURCE_RE = re.compile(r"\bnew\s+EventSource\s*\(")
_JS_XHR_OPEN_RE = re.compile(r"\.open\s*\(")
_JS_LOCATION_RE = re.compile(r"\blocation\s*\.\s*(href|assign|replace)\s*(=(?!=)|\()")
_JS_SETATTR_RE = re.compile(
    r"\.setAttribute\s*\(\s*[\"'](hx-(?:get|post|put|patch|delete))[\"']\s*,"
)
_JS_FUNCTION_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)")
_JS_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
_JS_ASSIGN_RE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")
_JS_DATASET_RE = re.compile(r"\bdataset\s*\.\s*([A-Za-z_$][\w$]*)")
_JS_GETATTR_DATA_RE = re.compile(r"\.getAttribute\s*\(\s*[\"'](data-[\w-]+)[\"']\s*\)")
_JS_GET_BY_ID_RE = re.compile(r"\bgetElementById\s*\(\s*[\"']([^\"']+)[\"']")
_JS_QUERY_RE = re.compile(
    r"\b(?:querySelectorAll|querySelector|closest)\s*\(\s*[\"']([^\"']+)[\"']"
)
_JS_LISTENER_RE = re.compile(r"\baddEventListener\s*\(\s*[\"']([A-Za-z]+)[\"']")
_JS_ONEVENT_RE = re.compile(r"\.on([a-z]+)\s*=")
_JS_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE)
_JS_SRC_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)
_JS_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")

#: How far back the linker looks when it cannot bracket the enclosing function.
JS_LINK_WINDOW = 60


def strip_js_comments(source: str) -> str:
    """Blank JavaScript comments while preserving newlines, strings and regexes.

    A small state machine rather than a regex: ``//`` inside a string literal is
    not a comment, and ``/`` after a value is division rather than the start of
    a regular-expression literal. Every newline survives, so a line number taken
    from the stripped text is the line number in the original file.

    Args:
        source: Raw JavaScript text.

    Returns:
        The text with comment bodies replaced by spaces, newlines kept.
    """
    out: list[str] = []
    state = "code"
    prev = ""
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if state == "code":
            if char == "/" and nxt == "/":
                state, index = "line", index + 2
                out.append("  ")
                continue
            if char == "/" and nxt == "*":
                state, index = "block", index + 2
                out.append("  ")
                continue
            if char == "/" and prev in _JS_REGEX_PRECEDERS:
                state = "regex"
            elif char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "`":
                state = "template"
            out.append(char)
            if not char.isspace():
                prev = char
            index += 1
            continue
        if state in ("line", "block"):
            if state == "line" and char == "\n":
                state = "code"
                out.append("\n")
            elif state == "block" and char == "*" and nxt == "/":
                state, index = "code", index + 2
                out.append("  ")
                continue
            else:
                out.append("\n" if char == "\n" else " ")
            index += 1
            continue
        # Inside a string, template literal or regex: copy verbatim.
        out.append(char)
        if char == "\\" and index + 1 < length:
            out.append(source[index + 1])
            index += 2
            continue
        closers = {"single": "'", "double": '"', "template": "`", "regex": "/"}
        if char == closers[state]:
            state, prev = "code", char
        index += 1
    return "".join(out)


def _js_line_of(source: str, index: int) -> int:
    """Return the 1-based line number of *index* in *source*."""
    return source.count("\n", 0, index) + 1


def _js_scan_args(source: str, open_index: int) -> tuple[list[str], int]:
    """Split the argument list of the call whose ``(`` sits at *open_index*.

    Returns:
        ``(arguments, index_after_closing_paren)``. The arguments are raw source
        slices, stripped; an unterminated call yields what was collected.
    """
    args: list[str] = []
    depth = 0
    start = open_index + 1
    index = open_index
    length = len(source)
    state = "code"
    while index < length:
        char = source[index]
        if state == "code":
            if char in "\"'`":
                state = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth == 0:
                    args.append(source[start:index].strip())
                    return [a for a in args if a], index + 1
            elif char == "," and depth == 1:
                args.append(source[start:index].strip())
                start = index + 1
        else:
            if char == "\\":
                index += 2
                continue
            if char == state:
                state = "code"
        index += 1
    args.append(source[start:].strip())
    return [a for a in args if a], length


def _js_split_top_level(expr: str, separators: str) -> list[str]:
    """Split *expr* on *separators* that sit outside brackets and string literals."""
    parts: list[str] = []
    depth = 0
    state = "code"
    current: list[str] = []
    index = 0
    while index < len(expr):
        char = expr[index]
        if state == "code":
            if char in "\"'`":
                state = char
                current.append(char)
            elif char in "([{":
                depth += 1
                current.append(char)
            elif char in ")]}":
                depth -= 1
                current.append(char)
            elif char in separators and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(char)
        else:
            current.append(char)
            if char == "\\" and index + 1 < len(expr):
                current.append(expr[index + 1])
                index += 2
                continue
            if char == state:
                state = "code"
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts]


def _js_string_value(token: str) -> str | None:
    """Return the text of *token* when it is a single quoted string literal."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        inner = token[1:-1]
        if token[0] not in inner.replace("\\" + token[0], ""):
            return inner.replace("\\" + token[0], token[0])
    return None


def _js_template_value(token: str) -> str | None:
    """Return the text of a template literal with ``${…}`` collapsed to a placeholder."""
    token = token.strip()
    if len(token) >= 2 and token.startswith("`") and token.endswith("`"):
        return re.sub(r"\$\{[^}]*\}", _JS_PARAM, token[1:-1])
    return None


@dataclass
class JsCall:
    """One URL a script reaches for, with whatever the scanner could resolve about it."""

    origin: str
    line: int
    kind: str
    verb: str
    url: str
    template: str = ""
    variable: str = ""
    wrapper: str = ""
    route: str = ""
    event: str = ""
    selector: str = ""
    dataset: str = ""
    trigger_template: str = ""
    trigger_line: int = 0
    trigger_text: str = ""
    trigger_count: int = 0
    flags: list[str] = field(default_factory=list)


def js_url_candidates(
    expr: str,
    resolve: Callable[[str], list[str]] | None = None,
    require_absolute: bool = True,
) -> list[str]:
    """Resolve a URL expression to the concrete paths it can produce.

    Handles the three shapes the project's scripts use — a string literal, a
    template literal and a ``+`` concatenation — plus the ternary that picks
    between two endpoints (``isEdit ? "/x/" + id : "/x"``), which yields one
    candidate per branch. Any part that is not a literal collapses to
    ``{param}``, so ``"/investments/" + ID + "/navs"`` becomes
    ``/investments/{param}/navs``. A ``window.location.origin`` or ``baseUrl``
    prefix contributes an origin rather than a path and is dropped.

    Args:
        expr: The raw source of the URL argument.
        resolve: Optional one-level lookup for a concatenated identifier. Only a
            value that turns out to be a fragment or a query string is taken
            from it — ``"/back-office" + fragment`` is a link to ``/back-office``
            and not to a path segment named after the variable. Anything else
            stays ``{param}``, as the design specifies.
        require_absolute: When false, a value that does not start with ``/`` is
            still returned; used for the fragment lookup above.

    Returns:
        The candidate paths, query string and fragment removed, in source
        order. Empty when no literal path survives — the caller records those
        as ``<dynamic>``.
    """
    expr = expr.strip()
    if not expr:
        return []
    branches = _js_split_top_level(expr, "?")
    if len(branches) == 2:
        tail = _js_split_top_level(branches[1], ":")
        if len(tail) == 2:
            return [
                url
                for branch in tail
                for url in js_url_candidates(branch, resolve, require_absolute)
            ]
    literal_seen = False
    rendered: list[str] = []
    for part in _js_split_top_level(expr, "+"):
        if not part:
            continue
        if part.startswith("(") and part.endswith(")"):
            inner = js_url_candidates(part[1:-1], resolve, require_absolute)
            if inner:
                rendered.append(inner[0])
                literal_seen = True
                continue
        text = _js_string_value(part)
        if text is None:
            text = _js_template_value(part)
        if text is None:
            if part.startswith(_JS_ORIGIN_PREFIXES):
                continue
            found = resolve(part) if resolve and _JS_IDENT_RE.match(part) else []
            if found and found[0][:1] in ("#", "?"):
                rendered.append(found[0])
                continue
            rendered.append(_JS_PARAM)
            continue
        literal_seen = literal_seen or "/" in text or not require_absolute
        rendered.append(text)
    if not literal_seen:
        return []
    url = "".join(rendered)
    if not require_absolute:
        # The fragment lookup needs the ``#`` kept: it is what tells the caller
        # this concatenation contributes a fragment rather than a path segment.
        return [url]
    url = url.split("?", 1)[0].split("#", 1)[0]
    return [url] if url.startswith("/") else []


def js_option_verbs(option_source: str) -> list[str]:
    """Return the HTTP verbs named by the ``method`` key of an options object.

    ``{ method: "DELETE" }`` yields ``["DELETE"]``; the conditional
    ``{ method: isEdit ? "PUT" : "POST" }`` yields both, in branch order, so the
    caller can pair them with a matching conditional URL.
    """
    source = option_source.strip()
    if source.startswith("{") and source.endswith("}"):
        source = source[1:-1]
    for entry in _js_split_top_level(source, ","):
        key, sep, value = entry.partition(":")
        if not sep or key.strip().strip("\"'") != "method":
            continue
        verbs: list[str] = []
        for literal in _JS_STRING_RE.finditer(value):
            text = literal.group(1) if literal.group(1) is not None else literal.group(2)
            if text and text.isalpha():
                verbs.append(text.upper())
        return verbs
    return []


def _js_pair(urls: list[str], verbs: list[str], default: str) -> list[tuple[str, str]]:
    """Pair resolved URLs with resolved verbs.

    A conditional URL beside a conditional method is two real call sites, so
    equal-length lists zip; otherwise every URL takes the first verb found.
    """
    if not urls:
        return []
    if verbs and len(verbs) == len(urls):
        pairs = list(zip(urls, verbs))
    else:
        verb = verbs[0] if verbs else default
        pairs = [(url, verb) for url in urls]
    # Both branches of a conditional can normalise to the same endpoint — one
    # carrying a query string, one not. That is one call site, not two.
    unique: list[tuple[str, str]] = []
    for pair in pairs:
        if pair not in unique:
            unique.append(pair)
    return unique


#: Constructs that introduce a callback. A handler body is *inside* the binding,
#: so the linker has to step out of one to reach the element it is bound to.
_JS_CALLBACK_HEAD_RE = re.compile(
    r"(?:addEventListener\s*\(|forEach\s*\(|\.then\s*\(|\.map\s*\(|"
    r"setTimeout\s*\(|setInterval\s*\(|\bon[a-z]+\s*=\s*)[^{]*$"
)

#: How many callback levels the linker steps out of before giving up.
JS_CALLBACK_LEVELS = 3


def _js_function_brace(text: str, index: int) -> int | None:
    """Return the position of the ``{`` opening the function body enclosing *index*."""
    depth = 0
    cursor = index - 1
    while cursor >= 0:
        char = text[cursor]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                head = text[max(0, cursor - 200) : cursor]
                if re.search(r"(?:function\b[^{};]*|=>\s*)$", head):
                    return cursor
            else:
                depth -= 1
        cursor -= 1
    return None


def _js_enclosing_span(text: str, index: int) -> tuple[int, int]:
    """Return the span the linker searches backwards for a binding.

    The innermost function body is the wrong window on its own: a call inside
    ``btn.addEventListener("click", function () { … })`` is bracketed by the
    handler, while the ``btn`` it is bound to was selected *outside* it. So the
    walk steps out of each enclosing callback — up to ``JS_CALLBACK_LEVELS`` of
    them — and stops at the first named function body, which is where the
    element lookup lives. Falls back to a fixed window of ``JS_LINK_WINDOW``
    lines when nothing brackets the call at all.
    """
    position = index
    span_start: int | None = None
    for _ in range(JS_CALLBACK_LEVELS):
        brace = _js_function_brace(text, position)
        if brace is None:
            break
        span_start = brace
        if not _JS_CALLBACK_HEAD_RE.search(text[max(0, brace - 200) : brace]):
            break
        position = brace
    line = _js_line_of(text, index)
    fallback = 0
    if line > JS_LINK_WINDOW:
        offset = 0
        for _ in range(line - JS_LINK_WINDOW):
            offset = text.find("\n", offset) + 1
        fallback = offset
    if span_start is None:
        return fallback, index
    return span_start, index


def _js_read_expression(text: str, start: int) -> str:
    """Return the source of the expression beginning at *start*, up to its terminator."""
    depth = 0
    state = "code"
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if state == "code":
            if char in "\"'`":
                state = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif char == ";" and depth == 0:
                break
            elif char == "\n" and depth == 0:
                tail = text[cursor:].lstrip()
                if not tail.startswith(("?", ":", "+", ".", "&&", "||")):
                    following = text[start:cursor].strip()
                    if following and not following.rstrip().endswith(("+", "?", ":", "(", ",")):
                        break
        else:
            if char == "\\":
                cursor += 2
                continue
            if char == state:
                state = "code"
        cursor += 1
    return text[start:cursor].strip()


def _js_assignment_rhs(text: str, name: str, before: int, after: int = 0) -> str:
    """Return the right-hand side of the last ``const|let|var name =`` before *before*."""
    pattern = re.compile(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=(?!=)")
    found = None
    for match in pattern.finditer(text, after, before):
        found = match
    if found is None:
        return ""
    return _js_read_expression(text, found.end())


def _js_dataset_name(expr: str) -> str:
    """Return the ``data-`` attribute an expression reads, or the empty string."""
    dataset = _JS_DATASET_RE.search(expr)
    if dataset:
        return "data-" + _JS_CAMEL_RE.sub("-", dataset.group(1)).lower()
    attribute = _JS_GETATTR_DATA_RE.search(expr)
    return attribute.group(1) if attribute else ""


def _js_resolve_url(text: str, expr: str, index: int) -> tuple[list[str], str, str]:
    """Resolve a URL argument to candidate paths.

    A literal resolves directly. A bare identifier is chased back to its
    assignment inside the enclosing function; an assignment that reads a
    ``data-*`` attribute resolves no further here — the attribute name is
    returned so the linker can look the URL up in the template that writes it.

    Returns:
        ``(urls, variable_name, dataset_attribute)``.
    """
    start, _ = _js_enclosing_span(text, index)

    def resolve_part(name: str) -> list[str]:
        rhs = _js_assignment_rhs(text, name, index, start)
        return js_url_candidates(rhs, require_absolute=False) if rhs else []

    urls = js_url_candidates(expr, resolve_part)
    if urls:
        return urls, "", ""
    dataset = _js_dataset_name(expr)
    if dataset:
        return [], expr.strip(), dataset
    if not _JS_IDENT_RE.match(expr.strip()):
        return [], "", ""
    name = expr.strip()
    rhs = _js_assignment_rhs(text, name, index, start)
    if not rhs:
        rhs = _js_assignment_rhs(text, name, index)
    if not rhs:
        return [], name, ""
    dataset = _js_dataset_name(rhs)
    if dataset:
        return [], name, dataset
    return js_url_candidates(rhs, resolve_part), name, ""


def _js_function_body(text: str, open_brace: int) -> tuple[int, int]:
    """Return the span of the body whose opening brace is at *open_brace*."""
    depth = 0
    cursor = open_brace
    state = "code"
    while cursor < len(text):
        char = text[cursor]
        if state == "code":
            if char in "\"'`":
                state = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return open_brace, cursor
        else:
            if char == "\\":
                cursor += 2
                continue
            if char == state:
                state = "code"
        cursor += 1
    return open_brace, len(text)


def js_wrappers(text: str) -> dict[str, tuple[int, str, str]]:
    """Find the local helpers that wrap a call pattern around a URL parameter.

    ``fetchJson(url, options)`` and ``appendPinButton(wrap, kind, url)`` are the
    project's two idioms: the endpoint is literal at the *call site*, and the
    call pattern lives one level down in the helper. Without this, a 910-line
    script contributes one unresolvable row instead of the seven endpoints it
    actually calls.

    Returns:
        ``{name: (url_parameter_index, kind, fixed_verb)}``.
    """
    wrappers: dict[str, tuple[int, str, str]] = {}
    for match in _JS_FUNCTION_RE.finditer(text):
        name = match.group(1)
        params = [p.strip() for p in match.group(2).split(",") if p.strip()]
        if not params:
            continue
        brace = text.find("{", match.end())
        if brace < 0:
            continue
        start, end = _js_function_body(text, brace)
        body = text[start:end]
        for kind, pattern, url_arg, verb_arg in (
            ("fetch", _JS_FETCH_RE, 0, None),
            ("htmx.ajax", _JS_HTMX_AJAX_RE, 1, 0),
            ("eventsource", _JS_EVENTSOURCE_RE, 0, None),
        ):
            inner = pattern.search(body)
            if not inner:
                continue
            args, _ = _js_scan_args(body, body.index("(", inner.end() - 1))
            if len(args) <= url_arg:
                continue
            candidate = args[url_arg].strip()
            if candidate not in params:
                continue
            verb = "GET"
            if verb_arg is not None and len(args) > verb_arg:
                verb = _js_string_value(args[verb_arg]) or "GET"
            wrappers[name] = (params.index(candidate), kind, verb.upper())
            break
        if name in wrappers:
            continue
        attr = _JS_SETATTR_RE.search(body)
        if attr:
            args, _ = _js_scan_args(body, body.index("(", attr.start()))
            if len(args) > 1 and args[1].strip() in params:
                verb = attr.group(1).removeprefix("hx-").upper()
                wrappers[name] = (params.index(args[1].strip()), "hx-attr", verb)
    return wrappers


def _js_emit(
    calls: list[JsCall],
    *,
    origin: str,
    template: str,
    text: str,
    index: int,
    kind: str,
    urls: list[str],
    verbs: list[str],
    default_verb: str,
    variable: str,
    dataset: str,
    extra: list[str],
    wrapper: str = "",
) -> None:
    """Append one ``JsCall`` per resolved URL, or a single ``<dynamic>`` row."""
    line = _js_line_of(text, index)
    pairs = _js_pair(urls, verbs, default_verb)
    if not pairs:
        flags = ["dynamic-url", *extra]
        calls.append(
            JsCall(
                origin=origin,
                line=line,
                kind=kind,
                verb=(verbs[0] if verbs else default_verb),
                url="<dynamic>",
                template=template,
                variable=variable,
                dataset=dataset,
                wrapper=wrapper,
                flags=flags,
            )
        )
        return
    for url, verb in pairs:
        calls.append(
            JsCall(
                origin=origin,
                line=line,
                kind=kind,
                verb=verb,
                url=url,
                template=template,
                variable=variable,
                dataset=dataset,
                wrapper=wrapper,
                flags=list(extra),
            )
        )


def scan_js(source: str, origin: str, template: str = "") -> list[JsCall]:
    """Extract every URL *source* reaches for.

    Covers the six call shapes of the design: ``fetch``, ``htmx.ajax``,
    ``EventSource``, ``XMLHttpRequest.open``, the three ``location``
    navigations, and an ``hx-*`` attribute set from script. Calls routed
    through a local wrapper are resolved at their call sites as well, so the
    endpoint rather than the parameter name reaches the inventory.

    Args:
        source: Raw script text.
        origin: Repository-relative path recorded on every row.
        template: Template path when the script is inline, else the empty string.

    Returns:
        The calls, in source order.
    """
    text = strip_js_comments(source)
    wrappers = js_wrappers(text)
    calls: list[JsCall] = []

    def emit(
        index: int,
        kind: str,
        urls: list[str],
        verbs: list[str],
        default: str,
        variable: str,
        dataset: str,
        extra: list[str],
        wrapper: str = "",
    ) -> None:
        _js_emit(
            calls,
            origin=origin,
            template=template,
            text=text,
            index=index,
            kind=kind,
            urls=urls,
            verbs=verbs,
            default_verb=default,
            variable=variable,
            dataset=dataset,
            extra=extra,
            wrapper=wrapper,
        )

    for match in _JS_FETCH_RE.finditer(text):
        open_index = match.end() - 1
        args, _ = _js_scan_args(text, open_index)
        if not args:
            continue
        urls, variable, dataset = _js_resolve_url(text, args[0], open_index)
        verbs = js_option_verbs(args[1]) if len(args) > 1 else []
        emit(open_index, "fetch", urls, verbs, "GET", variable, dataset, [])

    for match in _JS_HTMX_AJAX_RE.finditer(text):
        open_index = match.end() - 1
        args, _ = _js_scan_args(text, open_index)
        if len(args) < 2:
            continue
        verb = (_js_string_value(args[0]) or "GET").upper()
        urls, variable, dataset = _js_resolve_url(text, args[1], open_index)
        emit(open_index, "htmx.ajax", urls, [verb], verb, variable, dataset, [])

    for match in _JS_EVENTSOURCE_RE.finditer(text):
        open_index = match.end() - 1
        args, _ = _js_scan_args(text, open_index)
        if not args:
            continue
        urls, variable, dataset = _js_resolve_url(text, args[0], open_index)
        emit(open_index, "eventsource", urls, [], "GET", variable, dataset, ["sse"])

    if "XMLHttpRequest" in text:
        for match in _JS_XHR_OPEN_RE.finditer(text):
            open_index = match.end() - 1
            args, _ = _js_scan_args(text, open_index)
            if len(args) < 2:
                continue
            verb = (_js_string_value(args[0]) or "GET").upper()
            urls, variable, dataset = _js_resolve_url(text, args[1], open_index)
            emit(open_index, "xhr", urls, [verb], verb, variable, dataset, [])

    for match in _JS_LOCATION_RE.finditer(text):
        if match.group(2) == "=":
            expr = _js_read_expression(text, match.end())
            index = match.end()
        else:
            index = match.end() - 1
            args, _ = _js_scan_args(text, index)
            expr = args[0] if args else ""
        urls, variable, dataset = _js_resolve_url(text, expr, index)
        emit(index, "navigation", urls, [], "GET", variable, dataset, ["navigation"])

    for match in _JS_SETATTR_RE.finditer(text):
        open_index = text.index("(", match.start())
        args, _ = _js_scan_args(text, open_index)
        if len(args) < 2:
            continue
        verb = match.group(1).removeprefix("hx-").upper()
        urls, variable, dataset = _js_resolve_url(text, args[1], open_index)
        emit(open_index, "hx-attr", urls, [verb], verb, variable, dataset, [])

    for name, (position, kind, fixed_verb) in sorted(wrappers.items()):
        pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        for match in pattern.finditer(text):
            head = text[max(0, match.start() - 40) : match.start()]
            if re.search(r"\bfunction\s+$", head):
                continue
            open_index = match.end() - 1
            args, _ = _js_scan_args(text, open_index)
            if len(args) <= position:
                continue
            urls, variable, dataset = _js_resolve_url(text, args[position], open_index)
            verbs = [verb for argument in args for verb in js_option_verbs(argument)]
            emit(
                open_index,
                kind,
                urls,
                verbs,
                fixed_verb,
                variable,
                dataset,
                [],
                wrapper=name,
            )

    calls.sort(key=lambda call: (call.line, call.kind, call.url))
    return calls


@dataclass
class TemplateAnchor:
    """One template element a script can bind to, indexed by id, class and ``data-*``."""

    template: str
    source_file: str
    line: int
    identifier: str = ""
    classes: list[str] = field(default_factory=list)
    data: dict[str, str] = field(default_factory=dict)
    text: str = ""


@dataclass
class AnchorIndex:
    """The template side of the trigger link."""

    by_id: dict[str, list[TemplateAnchor]] = field(default_factory=lambda: defaultdict(list))
    by_class: dict[str, list[TemplateAnchor]] = field(default_factory=lambda: defaultdict(list))
    by_data: dict[str, list[TemplateAnchor]] = field(default_factory=lambda: defaultdict(list))


_ANCHOR_TAG_RE = re.compile(r"<([A-Za-z][\w-]*)((?:\s+[^<>]*)?)>", re.DOTALL)
_ANCHOR_ATTR_RE = re.compile(r"([A-Za-z_:][-\w:.]*)\s*=\s*\"([^\"]*)\"")


def collect_template_anchors(
    root: Path, repo_root: Path, templates: dict[str, TemplateFacts]
) -> AnchorIndex:
    """Index every template element carrying an ``id``, ``class`` or ``data-*`` attribute.

    Deliberately a second, independent pass over the templates rather than a
    hook inside ``ElementCollector``: the element rows must come out of a JS run
    byte-identical to a ``--js-off`` run, and the surest way to guarantee that is
    to leave the collector alone.

    Args:
        root: Template root.
        repo_root: Repository root, for the recorded source path.
        templates: The scanned templates, read for the visible text of a row.

    Returns:
        The populated index.
    """
    index = AnchorIndex()
    texts: dict[tuple[str, int], str] = {}
    for rel, facts in templates.items():
        for element in facts.elements:
            key = (rel, element.source_line)
            if element.visible_text and not texts.get(key):
                texts[key] = element.visible_text
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root).as_posix()
        source_file = path.relative_to(repo_root).as_posix()
        scrubbed = scrub_jinja(path.read_text(encoding="utf-8"))
        for match in _ANCHOR_TAG_RE.finditer(scrubbed):
            attrs = dict(_ANCHOR_ATTR_RE.findall(match.group(2) or ""))
            identifier = _JINJA_EXPR_RE.sub("", attrs.get("id", "")).strip()
            classes = [
                token for token in _JINJA_EXPR_RE.sub(" ", attrs.get("class", "")).split() if token
            ]
            data = {name: value for name, value in attrs.items() if name.startswith("data-")}
            if not identifier and not classes and not data:
                continue
            line = _js_line_of(scrubbed, match.start())
            anchor = TemplateAnchor(
                template=rel,
                source_file=source_file,
                line=line,
                identifier=identifier,
                classes=classes,
                data=data,
                text=texts.get((rel, line), ""),
            )
            if identifier:
                index.by_id[identifier].append(anchor)
            for token in classes:
                index.by_class[token].append(anchor)
            for name in data:
                index.by_data[name].append(anchor)
    return index


def _js_last(pattern: re.Pattern[str], window: str) -> re.Match[str] | None:
    """Return the last match of *pattern* in *window* — the binding nearest the call."""
    found: re.Match[str] | None = None
    for match in pattern.finditer(window):
        found = match
    return found


def _js_offset_of_line(text: str, line: int) -> int:
    """Return the character offset at which 1-based *line* starts."""
    offset = 0
    for _ in range(line - 1):
        found = text.find("\n", offset)
        if found < 0:
            return offset
        offset = found + 1
    return offset


def _js_resolve_selector(selector: str, index: AnchorIndex) -> tuple[list[TemplateAnchor], bool]:
    """Resolve a simple CSS selector against the anchor index.

    Returns:
        ``(anchors, simple)``. A descendant combinator, a comma group or a
        pseudo-class is not resolved — the design records those as selector text
        instead of guessing.
    """
    selector = selector.strip()
    if not selector or re.search(r"[\s,>~+]|:not\(|::", selector):
        return [], False
    if selector.startswith("#"):
        return index.by_id.get(selector[1:], []), True
    if selector.startswith("."):
        return index.by_class.get(selector[1:], []), True
    attribute = re.fullmatch(r"\[([\w-]+)(?:[~|^$*]?=[\"']?([^\]\"']*)[\"']?)?\]", selector)
    if attribute:
        name, value = attribute.group(1), attribute.group(2)
        anchors = index.by_data.get(name, [])
        if value:
            anchors = [a for a in anchors if a.data.get(name, "").strip() == value]
        return anchors, True
    tag_attribute = re.fullmatch(r"[\w-]+\[([\w-]+)\]", selector)
    if tag_attribute:
        return index.by_data.get(tag_attribute.group(1), []), True
    return [], False


def link_js_calls(calls: list[JsCall], source: str, index: AnchorIndex) -> None:
    """Attach each call to the template element whose handler makes it.

    Looks backwards from the call to the nearest binding inside the enclosing
    function body — an id lookup, a selector, an event listener, a ``data-*``
    read — and resolves it against the template anchors. A ``data-*`` read wins
    over an id lookup: when the URL itself came out of an attribute, that
    attribute is both the trigger and the endpoint.
    """
    text = strip_js_comments(source)
    for call in calls:
        position = _js_offset_of_line(text, call.line)
        start, _ = _js_enclosing_span(text, position)
        window = text[start : max(start, position + 1)]

        listener = _js_last(_JS_LISTENER_RE, window) or _js_last(_JS_ONEVENT_RE, window)
        if listener is not None:
            call.event = listener.group(1)

        anchors: list[TemplateAnchor] = []
        if call.dataset:
            anchors = index.by_data.get(call.dataset, [])
            call.selector = f"[{call.dataset}]"
        if not anchors:
            identifier = _js_last(_JS_GET_BY_ID_RE, window)
            if identifier is not None:
                anchors = index.by_id.get(identifier.group(1), [])
                call.selector = call.selector or f"#{identifier.group(1)}"
        if not anchors:
            query = _js_last(_JS_QUERY_RE, window)
            if query is not None:
                call.selector = call.selector or query.group(1)
                anchors, _simple = _js_resolve_selector(query.group(1), index)
        if not anchors:
            dataset = _js_last(_JS_DATASET_RE, window)
            if dataset is not None:
                name = "data-" + _JS_CAMEL_RE.sub("-", dataset.group(1)).lower()
                anchors = index.by_data.get(name, [])
                call.selector = call.selector or f"[{name}]"

        if not anchors:
            call.flags.append("unlinked")
            continue
        ordered = sorted({(a.template, a.line, a.text) for a in anchors})
        call.trigger_count = len(ordered)
        call.trigger_template, call.trigger_line, call.trigger_text = ordered[0]
        call.flags.append("linked")
        if len(ordered) > 1:
            call.flags.append("multi-trigger")
        if call.event:
            call.flags.append(f"event:{call.event}")


def js_trigger_templates(call: JsCall, index: AnchorIndex) -> list[str]:
    """Return every distinct template the call's trigger resolves into."""
    if not call.selector:
        return [call.trigger_template] if call.trigger_template else []
    anchors, _ = _js_resolve_selector(call.selector, index)
    if not anchors and call.selector.startswith("#"):
        anchors = index.by_id.get(call.selector[1:], [])
    return sorted({anchor.template for anchor in anchors}) or (
        [call.trigger_template] if call.trigger_template else []
    )


def inline_script_text(source: str) -> str:
    """Return *source* with everything outside a ``<script>`` body blanked.

    Line numbers survive, so a call found in the result reports the line it
    occupies in the template. Jinja is scrubbed first, which is what keeps the
    word ``fetch()`` inside a ``{# … #}`` comment from being read as a call.
    """
    scrubbed = scrub_jinja(source)
    out = ["\n" if char == "\n" else " " for char in scrubbed]
    for match in _JS_SCRIPT_RE.finditer(scrubbed):
        if _JS_SRC_RE.search(match.group(1) or ""):
            continue
        start = match.start(2)
        for offset, char in enumerate(match.group(2)):
            out[start + offset] = char
    return "".join(out)


def js_sources(root: Path, repo_root: Path) -> list[tuple[str, str, str]]:
    """Return ``(origin, template, text)`` for every script the inventory reads.

    Both source kinds of the design: the static files under ``web/static/js``
    and every inline ``<script>`` block without a ``src`` in every template.
    """
    sources: list[tuple[str, str, str]] = []
    static_dir = repo_root / "web" / "static" / "js"
    for path in sorted(static_dir.glob("*.js")):
        origin = path.relative_to(repo_root).as_posix()
        sources.append((origin, "", path.read_text(encoding="utf-8")))
    for path in sorted(root.rglob("*.html")):
        raw = path.read_text(encoding="utf-8")
        if "<script" not in raw:
            continue
        text = inline_script_text(raw)
        if not text.strip():
            continue
        sources.append(
            (path.relative_to(repo_root).as_posix(), path.relative_to(root).as_posix(), text)
        )
    return sources


def resolve_js_routes(
    calls: list[JsCall], matchers: list[tuple[re.Pattern[str], str]], index: AnchorIndex
) -> None:
    """Match every call to a route, resolving ``data-*`` URLs through the template.

    A URL read out of ``el.dataset.pfSseUrl`` is unresolvable in the script, but
    the template that writes ``data-pf-sse-url`` holds the literal — so the
    attribute is followed to its value and the call gets a real endpoint.
    """
    for call in calls:
        if call.url == "<dynamic>" and call.dataset:
            for anchor in index.by_data.get(call.dataset, []):
                value = _JINJA_EXPR_RE.sub(_JS_PARAM, anchor.data.get(call.dataset, ""))
                value = value.split("?", 1)[0].split("#", 1)[0].strip()
                if value.startswith("/"):
                    call.url = value
                    call.flags = [flag for flag in call.flags if flag != "dynamic-url"]
                    call.flags.append("url-from-attribute")
                    break
        call.route = match_route(call.url, matchers) if call.url != "<dynamic>" else ""


def js_call_areas(
    call: JsCall,
    index: AnchorIndex,
    template_areas: dict[str, set[str]],
    route_areas: dict[str, set[str]],
) -> list[str]:
    """Return the Areas a JS call row belongs to.

    A linked call belongs where its trigger lives. An unlinked call in an inline
    script belongs to the template that carries the script. An unlinked call in
    a static file has no template evidence at all, so it falls back to the Area
    of the route it calls — and to ``unassigned`` when even that is unknown.
    """
    areas: set[str] = set()
    if "linked" in call.flags:
        for rel in js_trigger_templates(call, index):
            areas |= template_areas.get(rel, set())
    elif call.template:
        areas |= template_areas.get(call.template, set())
    if not areas and call.route:
        areas |= route_areas.get(call.route, set())
    return sorted(areas) or [UNASSIGNED]


def js_elements(
    calls: list[JsCall],
    index: AnchorIndex,
    template_areas: dict[str, set[str]],
    route_areas: dict[str, set[str]],
) -> list[Element]:
    """Turn the scanned calls into ``elements.csv`` rows."""
    rows: list[Element] = []
    for call in calls:
        flags = [f"verb:{call.verb}", *call.flags]
        if call.wrapper:
            flags.append(f"via:{call.wrapper}")
        template = call.trigger_template if "linked" in call.flags else call.template
        for area in js_call_areas(call, index, template_areas, route_areas):
            rows.append(
                Element(
                    area=area,
                    template=template,
                    block="",
                    source_kind="js",
                    element_kind="js_call",
                    visible_text=call.trigger_text if "linked" in call.flags else "",
                    target=call.url,
                    hx_target="",
                    source_file=call.origin,
                    source_line=call.line,
                    flags=list(flags),
                )
            )
    rows.sort(
        key=lambda row: (row.area, row.source_file, row.source_line, row.target, row.template)
    )
    return rows


def js_edges_for(
    calls: list[JsCall], index: AnchorIndex
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return the ``template --js--> route`` edges a linked call contributes.

    Split into the two kinds the propagation treats differently. A scripted
    request is composition and carries an Area like an ``hx-*`` attribute; a
    scripted ``location.href`` is navigation, and propagating it would make the
    Admin data-import section an owner of ``/investments`` merely because it
    redirects there when the import succeeds.

    An unlinked call contributes nothing either way: without a trigger there is
    no template to carry an Area, and guessing one would be weaker evidence than
    the handler-module fallback it is meant to replace.

    Returns:
        ``(request_edges, navigation_edges)``.
    """
    edges: dict[str, set[str]] = defaultdict(set)
    nav_edges: dict[str, set[str]] = defaultdict(set)
    for call in calls:
        if "linked" not in call.flags or not call.route:
            continue
        bucket = nav_edges if "navigation" in call.flags else edges
        for rel in js_trigger_templates(call, index):
            bucket[rel].add(call.route)
    return edges, nav_edges


def propagate(
    templates: dict[str, TemplateFacts],
    routes: list[RouteRow],
    renders: dict[str, list[str]],
    area_urls: dict[str, str],
    js_edges: dict[str, set[str]] | None = None,
    js_nav_edges: dict[str, set[str]] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]], list[str]]:
    """Propagate Area labels over the include / HTMX / renders graph to a fixed point.

    Three rules keep the propagation honest:

    * ``{% extends %}`` is never traversed — layout inheritance is not Area
      composition.
    * The shell Area is a sink. Its chrome partials reach it through
      ``{% include %}``, but its links into the nine Areas propagate nothing:
      the sidebar links to every Area without owning any of them.
    * A plain ``href`` never propagates into another whole page — neither one
      the URL space already places in an Area (Assistants' pointer at
      ``/admin#providers-credentials`` leaves Admin alone) nor a full-page
      surface of its own (Admin's "Change investment settings" link leaves
      ``/investments`` to the ``investments`` surface). Linking to a page is
      navigation; only a link that pulls in a fragment is composition.

    Two fallbacks run after the first fixed point, for routes no template
    triggers visibly. A route reachable only from the shell chrome becomes
    ``_shell``; a route whose sibling routes in the same handler module all
    agree on one Area inherits it. The second is the weakest evidence in the
    tool, so the routes that needed it are returned for the summary to list —
    they are the routes whose trigger lives in JavaScript, where an HTML-only
    inventory cannot see it.

    Returns:
        ``(template_areas, route_areas, module_attributed)``.
    """
    template_areas = seed_areas(templates)
    route_areas: dict[str, set[str]] = defaultdict(set)
    matchers = route_matcher([route.path for route in routes])
    pages = page_templates(templates)
    path_areas: dict[str, set[str]] = {}
    renders_page: set[str] = set()
    for route in routes:
        path_areas[route.path] = area_from_path(route.path, area_urls)
        route_areas[route.path] |= path_areas[route.path]
        if any(rel in pages for rel in renders.get(route.path, [])):
            renders_page.add(route.path)

    template_to_routes: dict[str, set[str]] = defaultdict(set)
    for rel, facts in templates.items():
        for url in facts.hx_urls:
            path = match_route(url, matchers)
            if path:
                template_to_routes[rel].add(path)
        for url in facts.nav_urls:
            path = match_route(url, matchers)
            if path and not path_areas.get(path) and path not in renders_page:
                template_to_routes[rel].add(path)
    for rel, paths in (js_edges or {}).items():
        template_to_routes[rel] |= paths
    # A scripted ``location.href`` is navigation, not composition — the same
    # rule a plain ``href`` already obeys, so it gets the same filter.
    for rel, paths in (js_nav_edges or {}).items():
        for path in paths:
            if not path_areas.get(path) and path not in renders_page:
                template_to_routes[rel].add(path)

    def run_fixed_point() -> None:
        changed = True
        guard = 0
        while changed and guard < 50:
            changed = False
            guard += 1
            for rel, facts in templates.items():
                areas = template_areas.get(rel, set())
                if not areas:
                    continue
                for include in facts.includes:
                    if (
                        include in templates
                        and include not in SHELL_SEEDS
                        and not areas <= template_areas[include]
                    ):
                        template_areas[include] |= areas
                        changed = True
                outward = areas - {SHELL_AREA}
                if not outward:
                    continue
                for path in template_to_routes.get(rel, set()):
                    if not outward <= route_areas[path]:
                        route_areas[path] |= outward
                        changed = True
            for path, rendered in renders.items():
                areas = route_areas.get(path, set())
                if not areas:
                    continue
                for rel in rendered:
                    if (
                        rel in templates
                        and rel not in SHELL_SEEDS
                        and not areas <= template_areas[rel]
                    ):
                        template_areas[rel] |= areas
                        changed = True

    run_fixed_point()

    shell_triggered = {
        path
        for rel, paths in template_to_routes.items()
        if template_areas.get(rel) == {SHELL_AREA}
        for path in paths
    }
    for path in shell_triggered:
        if not route_areas.get(path):
            route_areas[path].add(SHELL_AREA)

    by_module: dict[str, set[str]] = defaultdict(set)
    for route in routes:
        if route.handler_module.startswith("web.routes."):
            by_module[route.handler_module] |= route_areas.get(route.path, set())
    module_attributed: list[str] = []
    for route in routes:
        if route_areas.get(route.path):
            continue
        agreed = by_module.get(route.handler_module, set())
        if len(agreed) == 1 and SHELL_AREA not in agreed:
            route_areas[route.path] |= agreed
            module_attributed.append(f"{route.methods} {route.path}")

    run_fixed_point()
    return template_areas, route_areas, sorted(module_attributed)


def include_closure(templates: dict[str, TemplateFacts], roots: set[str]) -> set[str]:
    """Return *roots* plus everything they include or extend, transitively."""
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        rel = stack.pop()
        if rel in seen or rel not in templates:
            continue
        seen.add(rel)
        stack.extend(templates[rel].includes)
        stack.extend(templates[rel].extends)
    return seen


# ---------------------------------------------------------------------------
# Cross-inventory flags
# ---------------------------------------------------------------------------


def apply_cross_flags(elements: list[Element]) -> None:
    """Stamp ``duplicate-label`` and ``synonym-candidate`` across the whole inventory."""
    by_text: dict[str, set[str]] = defaultdict(set)
    by_target: dict[str, set[str]] = defaultdict(set)
    for element in elements:
        text = element.visible_text.strip()
        if not text or is_dynamic(text):
            continue
        key = text.casefold()
        if element.target:
            by_text[key].add(element.target)
            by_target[element.target].add(key)
    for element in elements:
        text = element.visible_text.strip()
        if not text or is_dynamic(text):
            continue
        key = text.casefold()
        if len(by_text.get(key, ())) > 1:
            element.flags.append("duplicate-label")
        if element.target and len(by_target.get(element.target, ())) > 1:
            element.flags.append("synonym-candidate")


def duplicate_groups(elements: list[Element]) -> list[tuple[str, list[str]]]:
    """Return every visible text used for two or more distinct targets."""
    by_text: dict[str, set[str]] = defaultdict(set)
    for element in elements:
        text = element.visible_text.strip()
        if text and not is_dynamic(text) and element.target:
            by_text[text.casefold()].add(element.target)
    return sorted(
        ((text, sorted(targets)) for text, targets in by_text.items() if len(targets) > 1),
        key=lambda item: (-len(item[1]), item[0]),
    )


def synonym_groups(elements: list[Element]) -> list[tuple[str, list[str]]]:
    """Return every target reached by two or more distinct visible texts."""
    by_target: dict[str, set[str]] = defaultdict(set)
    for element in elements:
        text = element.visible_text.strip()
        if text and not is_dynamic(text) and element.target:
            by_target[element.target].add(text)
    return sorted(
        ((target, sorted(texts)) for target, texts in by_target.items() if len(texts) > 1),
        key=lambda item: (-len(item[1]), item[0]),
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

ELEMENT_HEADER = [
    "area",
    "template",
    "block",
    "source_kind",
    "element_kind",
    "visible_text",
    "target",
    "hx_target",
    "source_file",
    "source_line",
    "flags",
]
ROUTE_HEADER = [
    "area",
    "methods",
    "path",
    "handler_module",
    "handler_name",
    "source_file",
    "source_line",
    "template",
    "auth_required",
    "js_callers",
]

NOUN_HEURISTIC = """\
**Distinct-noun count.** For every *static* visible text (rows not flagged
`dynamic`), the text is split into sentences on `.`, `!`, `?`, `:` and `;`. In
each sentence every token after the first is kept when it matches
`^[A-Z][a-z]+$` — a capitalised ordinary word — and dropped when it carries two
or more capitals (an abbreviation by the `abbrev` rule). The distinct set of
what survives is the Area's noun vocabulary. The first word of a sentence is
dropped because sentence case capitalises it regardless of whether it is a
product noun. The measure is a proxy, not a count of concepts: it over-counts
proper nouns in prose and under-counts lower-case product terms.
"""


def js_caller_section(module_attributed: list[str], js_calls: list[JsCall]) -> list[str]:
    """Render the summary section that replaces the handler-module fallback list.

    The population is the pre-JS fallback set — the routes no template triggers
    visibly. For each, the scripts that call it and, where the linker could
    bracket a binding, the template element the handler is bound to. What the
    scripts still do not explain is listed separately rather than left implicit.

    Args:
        module_attributed: ``"METHOD /path"`` entries from the pre-JS run.
        js_calls: Every call the JavaScript pass found.

    Returns:
        The Markdown lines.
    """
    by_route: dict[str, list[JsCall]] = defaultdict(list)
    for call in js_calls:
        if call.route:
            by_route[call.route].append(call)

    # A path routinely carries several route rows, one per method — ``PUT`` and
    # ``DELETE`` on ``/investments/{id}/navs/{nav_id}`` are two rows, and only
    # one of them may be in this population. Matching on the verb as well as the
    # path is what keeps a DELETE caller from being offered as evidence for the
    # sibling PUT. A call that matches no row here is not lost: it is still a row
    # in ``elements.csv`` and still counted in ``routes.csv``.
    found: list[tuple[str, list[JsCall]]] = []
    residual: list[str] = []
    for entry in module_attributed:
        methods, _, path = entry.partition(" ")
        path = path or entry
        allowed = set(methods.split("|"))
        calls = [call for call in by_route.get(path, []) if call.verb in allowed]
        if calls:
            found.append((entry, calls))
        else:
            residual.append(entry)

    lines: list[str] = []
    lines.append(f"## Routes with JavaScript callers only ({len(found)})")
    lines.append("")
    lines.append(
        "No template triggers these visibly — their caller is a script. The JavaScript pass "
        "reads those scripts, so the caller below is evidence rather than inference: the "
        "script location that issues the request and, where the handler could be traced back "
        "to the element it is bound to, that element and its visible text. A route that gains "
        "a linked trigger no longer needs the handler-module fallback for its Area."
    )
    lines.append("")
    for entry, calls in found:
        lines.append(f"* `{entry}`")
        for call in sorted(calls, key=lambda c: (c.origin, c.line)):
            detail = f"  * `{call.origin}:{call.line}` — {call.kind}, `{call.verb}`"
            if call.wrapper:
                detail += f", via `{call.wrapper}()`"
            if "linked" in call.flags:
                detail += f" — trigger `{call.selector}` in `{call.trigger_template}`"
                detail += f":{call.trigger_line}"
                if call.trigger_text:
                    detail += f' — "{_md(call.trigger_text)}"'
                if "multi-trigger" in call.flags:
                    detail += f" (+{call.trigger_count - 1} more)"
            else:
                detail += " — no trigger element resolved"
            lines.append(detail)
    lines.append("")
    lines.append(f"### Still without any known caller ({len(residual)})")
    lines.append("")
    lines.append(
        "Neither a template nor a script explains these. They keep the handler-module Area, "
        "which remains the weakest evidence in the inventory."
    )
    lines.append("")
    for entry in residual:
        lines.append(f"* `{entry}`")
    lines.append("")
    return lines


def write_elements(out_dir: Path, elements: list[Element]) -> None:
    """Write ``elements.csv``."""
    with (out_dir / "elements.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ELEMENT_HEADER)
        for element in elements:
            writer.writerow(element.as_row())


def write_routes(out_dir: Path, routes: list[RouteRow]) -> None:
    """Write ``routes.csv``."""
    with (out_dir / "routes.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ROUTE_HEADER)
        for route in routes:
            writer.writerow(route.as_row())


def write_summary(
    out_dir: Path,
    elements: list[Element],
    routes: list[RouteRow],
    templates: dict[str, TemplateFacts],
    template_areas: dict[str, set[str]],
    excluded: set[str],
    route_note: str,
    module_attributed: list[str],
    js_rows: list[Element] | None = None,
    js_calls: list[JsCall] | None = None,
    js_enabled: bool = False,
) -> None:
    """Write the generated ``summary.md``."""
    js_rows = js_rows or []
    js_calls = js_calls or []
    areas = sorted(
        {element.area for element in elements}
        | {part for route in routes for part in route.area.split("|")}
        | {row.area for row in js_rows}
    )
    lines: list[str] = []
    lines.append("# UI inventory — summary")
    lines.append("")
    lines.append(
        "Generated by `tools/ux_inventory.py`. Do not hand-edit — re-run the tool instead."
    )
    lines.append("")
    lines.append(f"* elements (rows): **{len(elements)}**")
    distinct = {(e.source_file, e.source_line, e.element_kind, e.visible_text) for e in elements}
    lines.append(f"* distinct element facets (file, line, kind, text): **{len(distinct)}**")
    lines.append(f"* routes: **{len(routes)}**  ({route_note})")
    lines.append(f"* templates read: **{len(templates)}**")
    lines.append(f"* templates excluded (reachable only without login): **{len(excluded)}**")
    unattributed = sorted(
        rel for rel in templates if not template_areas.get(rel) and rel not in excluded
    )
    lines.append(f"* templates with no owning Area: **{len(unattributed)}**")
    lines.append("")

    lines.append("## Per Area")
    lines.append("")
    js_column = " js calls |" if js_enabled else ""
    js_rule = "---:|" if js_enabled else ""
    lines.append(
        "| Area | templates | element rows | headings | buttons | HTMX triggers | links | "
        "form labels | pills | tiles | routes |" + js_column + " distinct nouns |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|" + js_rule + "---:|")
    js_by_area = Counter(row.area for row in js_rows)
    for area in areas:
        rows = [element for element in elements if element.area == area]
        kinds = Counter(element.element_kind for element in rows)
        tpls = {element.template for element in rows if element.template}
        nouns = distinct_nouns([element.visible_text for element in rows])
        route_count = sum(1 for route in routes if area in route.area.split("|"))
        js_cell = f" {js_by_area[area]} |" if js_enabled else ""
        lines.append(
            f"| {area} | {len(tpls)} | {len(rows)} | {kinds['heading']} | {kinds['button']} | "
            f"{kinds['htmx']} | {kinds['link']} | {kinds['form_label']} | {kinds['pill']} | "
            f"{kinds['tile']} | {route_count} |" + js_cell + f" {len(nouns)} |"
        )
    lines.append("")
    lines.append(NOUN_HEURISTIC)
    lines.append("")

    lines.append("## Element rows by kind")
    lines.append("")
    lines.append("| kind | rows |")
    lines.append("|---|---:|")
    for kind, count in Counter(element.element_kind for element in elements).most_common():
        lines.append(f"| {kind} | {count} |")
    lines.append("")

    all_flags = sorted(
        {
            flag
            for element in elements
            for flag in element.flags
            if not flag.startswith(("options:", "verb:"))
        }
        | ({"unlinked"} if js_enabled else set())
    )
    lines.append("## Flags per Area")
    lines.append("")
    verb_counts = Counter(
        flag.removeprefix("verb:")
        for element in elements
        if element.element_kind == "htmx"
        for flag in element.flags
        if flag.startswith("verb:")
    )
    lines.append(
        "HTMX verbs ride on the row as a `verb:` flag and are left out of the table below — "
        + ", ".join(f"{verb} x{count}" for verb, count in sorted(verb_counts.items()))
        + "."
    )
    lines.append("")
    option_counts = Counter(
        flag
        for element in elements
        if element.element_kind == "select"
        for flag in element.flags
        if flag.startswith("options:")
    )
    lines.append(
        "`<select>` option texts are not inventoried; the per-select option count is carried "
        "on the row as an `options:N` flag and left out of the table below — "
        + (", ".join(f"{flag} ×{count}" for flag, count in sorted(option_counts.items())) or "none")
        + "."
    )
    lines.append("")
    lines.append("| Area | " + " | ".join(all_flags) + " |")
    lines.append("|---" * (len(all_flags) + 1) + "|")
    for area in areas:
        counts = Counter(
            flag for element in elements if element.area == area for flag in set(element.flags)
        )
        if js_enabled:
            counts["unlinked"] = sum(
                1 for row in js_rows if row.area == area and "unlinked" in row.flags
            )
        lines.append(f"| {area} | " + " | ".join(str(counts[flag]) for flag in all_flags) + " |")
    lines.append("")

    static_texts = [
        element.visible_text
        for element in elements
        if element.visible_text and not is_dynamic(element.visible_text)
    ]
    lines.append("## Ten most frequent visible texts")
    lines.append("")
    lines.append("| count | text |")
    lines.append("|---:|---|")
    for text, count in Counter(static_texts).most_common(10):
        lines.append(f"| {count} | {_md(text)} |")
    lines.append("")

    lines.append("## Ten longest visible texts")
    lines.append("")
    lines.append("| chars | text |")
    lines.append("|---:|---|")
    for text in sorted(set(static_texts), key=len, reverse=True)[:10]:
        lines.append(f"| {len(text)} | {_md(text)} |")
    lines.append("")

    duplicates = duplicate_groups(elements)
    lines.append(f"## Duplicate labels ({len(duplicates)})")
    lines.append("")
    lines.append("The same visible text pointing at two or more different targets.")
    lines.append("")
    lines.append("| text | targets |")
    lines.append("|---|---|")
    for text, targets in duplicates:
        lines.append(f"| {_md(text)} | {_md(' · '.join(targets))} |")
    lines.append("")

    synonyms = synonym_groups(elements)
    lines.append(f"## Synonym candidates ({len(synonyms)})")
    lines.append("")
    lines.append("Two or more different visible texts pointing at the same target.")
    lines.append("")
    lines.append("| target | texts |")
    lines.append("|---|---|")
    for target, texts in synonyms:
        lines.append(f"| {_md(target)} | {_md(' · '.join(texts))} |")
    lines.append("")

    errors = sorted(
        (rel, facts.parse_error) for rel, facts in templates.items() if facts.parse_error
    )
    lines.append(f"## Parse errors ({len(errors)})")
    lines.append("")
    if errors:
        for rel, error in errors:
            lines.append(f"* `{rel}` — {error}")
    else:
        lines.append("None.")
    lines.append("")

    warned = sorted(
        (rel, len(facts.warnings)) for rel, facts in templates.items() if facts.warnings
    )
    total_warnings = sum(count for _, count in warned)
    lines.append(f"## Structural warnings ({total_warnings} in {len(warned)} templates)")
    lines.append("")
    lines.append(
        "Unbalanced or unclosed tags. Expected in partials that open a wrapper one "
        "template and close it in another; they do not stop the parse."
    )
    lines.append("")
    for rel, count in sorted(warned, key=lambda item: -item[1]):
        lines.append(f"* `{rel}` — {count}")
    lines.append("")

    unassigned_routes = [route for route in routes if route.area == UNASSIGNED]
    lines.append(f"## Unassigned routes ({len(unassigned_routes)})")
    lines.append("")
    lines.append("Routes whose Area could be derived neither from the URL nor from a template.")
    lines.append("")
    for route in unassigned_routes:
        lines.append(
            f"* `{route.methods} {route.path}` — {route.handler_module}.{route.handler_name}"
        )
    lines.append("")

    if js_enabled:
        lines.extend(js_caller_section(module_attributed, js_calls))
    else:
        lines.append(f"## Routes attributed by handler module ({len(module_attributed)})")
        lines.append("")
        lines.append(
            "No template triggers these visibly — their caller is JavaScript, which this "
            "inventory does not read. They inherit the single Area their sibling routes agree "
            "on. Weakest evidence in the inventory; treat the Area as indicative."
        )
        lines.append("")
        for entry in module_attributed:
            lines.append(f"* `{entry}`")
        lines.append("")

    lines.append(f"## Templates with no owning Area ({len(unattributed)})")
    lines.append("")
    for rel in unattributed:
        lines.append(f"* `{rel}`")
    lines.append("")

    lines.append(f"## Excluded templates ({len(excluded)})")
    lines.append("")
    lines.append("Reachable only from routes with no authentication dependency.")
    lines.append("")
    for rel in sorted(excluded):
        lines.append(f"* `{rel}`")
    lines.append("")

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md(text: str) -> str:
    """Escape a cell value for a Markdown table."""
    return text.replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--template-root",
        type=Path,
        default=repo_root / "web" / "templates",
        help="Jinja2Templates directory (default: web/templates)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo_root / "docs" / "ux" / "inventory",
        help="output directory for the generated artefacts (default: docs/ux/inventory)",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=repo_root, help="repository root (default: inferred)"
    )
    parser.add_argument(
        "--no-import",
        action="store_true",
        help="force the static route fallback instead of importing the application",
    )
    parser.add_argument(
        "--js-off",
        action="store_true",
        help="skip the JavaScript pass entirely (reproduces the pre-JS artefacts)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the inventory and write the artefacts. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    root: Path = args.template_root.resolve()
    out_dir: Path = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    known_templates = {path.relative_to(root).as_posix() for path in root.rglob("*.html")}

    # -- routes ----------------------------------------------------------
    app: Any = None
    route_note = "static fallback (forced with --no-import)"
    if not args.no_import:
        app, route_note = load_app()
    if app is not None:
        routes, renders = dynamic_routes(app, repo_root, known_templates)
    else:
        routes, renders = static_routes(repo_root, known_templates)
        if not args.no_import:
            route_note = f"static fallback — {route_note}"

    area_urls: dict[str, str] = {}
    try:
        from web.shell import all_areas  # noqa: PLC0415 - optional, guarded

        area_urls = {area.url: area.slug for area in all_areas()}
    except Exception:  # noqa: BLE001 - the URL space is a convenience, not a requirement
        area_urls = {}

    # -- templates -------------------------------------------------------
    templates: dict[str, TemplateFacts] = {}
    for path in sorted(root.rglob("*.html")):
        facts = scan_template(root, repo_root, path)
        templates[facts.path] = facts

    # -- JavaScript ------------------------------------------------------
    js_calls: list[JsCall] = []
    anchors = AnchorIndex()
    js_edges: dict[str, set[str]] = {}
    js_nav_edges: dict[str, set[str]] = {}
    if not args.js_off:
        anchors = collect_template_anchors(root, repo_root, templates)
        matchers = route_matcher([route.path for route in routes])
        for origin, template, text in js_sources(root, repo_root):
            found = scan_js(text, origin, template)
            link_js_calls(found, text, anchors)
            js_calls.extend(found)
        resolve_js_routes(js_calls, matchers, anchors)
        js_edges, js_nav_edges = js_edges_for(js_calls, anchors)

    # The pre-JS attribution is kept as the baseline: it is the population the
    # summary's JavaScript section reports against, and the only way to say
    # which Areas the new edges actually changed.
    base_template_areas, base_route_areas, base_module_attributed = propagate(
        templates, routes, renders, area_urls
    )
    if js_edges or js_nav_edges:
        # The post-JS fallback list is discarded on purpose: the summary reports
        # against the pre-JS population, which is what "the 29" refers to.
        template_areas, route_areas, _ = propagate(
            templates, routes, renders, area_urls, js_edges, js_nav_edges
        )
    else:
        template_areas = base_template_areas
        route_areas = base_route_areas

    area_changes = sorted(
        f"{path}: {'|'.join(sorted(base_route_areas.get(path, set()))) or UNASSIGNED}"
        f" -> {'|'.join(sorted(route_areas.get(path, set()))) or UNASSIGNED}"
        for path in {route.path for route in routes}
        if base_route_areas.get(path, set()) != route_areas.get(path, set())
    )

    for route in routes:
        areas = route_areas.get(route.path, set())
        route.area = "|".join(sorted(areas)) if areas else UNASSIGNED

    # -- exclusions ------------------------------------------------------
    open_roots = {
        rel
        for route in routes
        if route.auth_required == "none"
        for rel in route.template.split(";")
        if rel
    }
    guarded_roots = {
        rel
        for route in routes
        if route.auth_required != "none"
        for rel in route.template.split(";")
        if rel
    }
    excluded = include_closure(templates, open_roots) - include_closure(templates, guarded_roots)

    # -- elements --------------------------------------------------------
    elements: list[Element] = []
    for rel, facts in templates.items():
        if rel in excluded:
            continue
        areas = sorted(template_areas.get(rel, {UNASSIGNED}))
        for area in areas:
            for element in facts.elements:
                copy = Element(**{**element.__dict__, "area": area, "flags": list(element.flags)})
                elements.append(copy)

    module_areas: dict[str, set[str]] = defaultdict(set)
    for route in routes:
        if route.source_file and route.area != UNASSIGNED:
            module_areas[route.source_file] |= set(route.area.split("|"))
    for path in python_copy_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        rows = scan_python_copy(repo_root, path)
        areas = sorted(module_areas.get(rel, set())) or [
            SHELL_AREA if rel.startswith("web/") else UNASSIGNED
        ]
        for area in areas:
            for element in rows:
                copy = Element(**{**element.__dict__, "area": area, "flags": list(element.flags)})
                elements.append(copy)

    apply_cross_flags(elements)
    elements.sort(key=lambda e: (e.area, e.template, e.source_file, e.source_line, e.element_kind))

    # Count per (path, verb), not per path: ``/investments/{id}/navs/{nav_id}``
    # is two route rows, and a DELETE caller is not a caller of the sibling PUT.
    callers = Counter((call.route, call.verb) for call in js_calls if call.route)
    for route in routes:
        methods = set(route.methods.split("|"))
        route.js_callers = sum(
            count
            for (path, verb), count in callers.items()
            if path == route.path and verb in methods
        )
    routes.sort(key=lambda r: (r.area, r.path, r.methods))

    js_rows = js_elements(js_calls, anchors, template_areas, route_areas) if js_calls else []

    write_elements(out_dir, elements + js_rows)
    write_routes(out_dir, routes)
    write_summary(
        out_dir,
        elements,
        routes,
        templates,
        template_areas,
        excluded,
        route_note,
        base_module_attributed,
        js_rows,
        js_calls,
        not args.js_off,
    )

    parse_errors = [rel for rel, facts in templates.items() if facts.parse_error]
    print(
        f"templates: {len(templates)}  elements: {len(elements)}  routes: {len(routes)}"
        + (f"  js rows: {len(js_rows)} from {len(js_calls)} calls" if js_calls else "")
    )
    print(f"route pass: {route_note}")
    for change in area_changes:
        print(f"area change: {change}")
    print(f"written to: {out_dir}")
    if parse_errors:
        for rel in parse_errors:
            print(f"parse error: {rel}: {templates[rel].parse_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
