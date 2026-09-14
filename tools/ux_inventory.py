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
    One row per route, with its Area, handler, rendered template and the
    authentication dependency that guards it.

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


def propagate(
    templates: dict[str, TemplateFacts],
    routes: list[RouteRow],
    renders: dict[str, list[str]],
    area_urls: dict[str, str],
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
) -> None:
    """Write the generated ``summary.md``."""
    areas = sorted(
        {element.area for element in elements}
        | {part for route in routes for part in route.area.split("|")}
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
    lines.append(
        "| Area | templates | element rows | headings | buttons | HTMX triggers | links | "
        "form labels | pills | tiles | routes | distinct nouns |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for area in areas:
        rows = [element for element in elements if element.area == area]
        kinds = Counter(element.element_kind for element in rows)
        tpls = {element.template for element in rows if element.template}
        nouns = distinct_nouns([element.visible_text for element in rows])
        route_count = sum(1 for route in routes if area in route.area.split("|"))
        lines.append(
            f"| {area} | {len(tpls)} | {len(rows)} | {kinds['heading']} | {kinds['button']} | "
            f"{kinds['htmx']} | {kinds['link']} | {kinds['form_label']} | {kinds['pill']} | "
            f"{kinds['tile']} | {route_count} | {len(nouns)} |"
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

    lines.append(f"## Routes attributed by handler module ({len(module_attributed)})")
    lines.append("")
    lines.append(
        "No template triggers these visibly — their caller is JavaScript, which this "
        "inventory does not read. They inherit the single Area their sibling routes agree on. "
        "Weakest evidence in the inventory; treat the Area as indicative."
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

    template_areas, route_areas, module_attributed = propagate(
        templates, routes, renders, area_urls
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
    routes.sort(key=lambda r: (r.area, r.path, r.methods))

    write_elements(out_dir, elements)
    write_routes(out_dir, routes)
    write_summary(
        out_dir,
        elements,
        routes,
        templates,
        template_areas,
        excluded,
        route_note,
        module_attributed,
    )

    parse_errors = [rel for rel, facts in templates.items() if facts.parse_error]
    print(f"templates: {len(templates)}  elements: {len(elements)}  routes: {len(routes)}")
    print(f"route pass: {route_note}")
    print(f"written to: {out_dir}")
    if parse_errors:
        for rel in parse_errors:
            print(f"parse error: {rel}: {templates[rel].parse_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
