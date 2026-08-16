# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Allowlist loader and matching for the Web Research capability.

The allowlist is read from ``config/web_research.yaml`` and provides the
single source of truth for which domains the capability is permitted to
fetch from (ADR-0023). Under ADR-0024 each entry additionally declares the
RSS/Atom feed URL(s) that drive query-to-article resolution.

v1 supports exact hostname matching only — no wildcards, no regex.

Errors loading or parsing the file raise :class:`AllowlistError`, which is a
typed subclass of :class:`core.exceptions.PortfoliFlowError`. Silent
degradation is unacceptable for a security boundary: a missing or malformed
allowlist must surface immediately at startup rather than quietly allow or
block fetches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import yaml

from core.exceptions import PortfoliFlowError

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "config" / "web_research.yaml"

# The closed, curated tag vocabulary (ADR-0087 Part B). Tags are the
# *bucket dimension* for Irene's deterministic RSS clustering: cross-source
# events merge into one bucket by shared tag, never by source name. The set
# is deliberately small and fixed so a reviewer can read it in full and a
# typo in the YAML fails fast at load time rather than silently opening a
# new, unaudited dimension. Keep this in step with
# ``services.irene.delta_config.DEFAULT_TAG_ASSET_CLASS_MAP``, which maps
# these tags onto the internal asset-class axis for the correlation lift.
_KNOWN_TAGS: frozenset[str] = frozenset(
    {
        "macro",
        "regulator",
        "equities",
        "credit",
        "real_estate",
        "private_markets",
        "swiss_finance",
    }
)


class AllowlistError(PortfoliFlowError):
    """Raised on any failure loading or parsing the allowlist file."""


@dataclass(frozen=True)
class AllowlistEntry:
    """One allowlist entry.

    Attributes:
        domain: Exact hostname (lowercase). No wildcards in v1.
        name: Human-readable display name.
        added_on: ISO date string recording when the entry was added.
        rationale: One-line reason the domain belongs on the list.
        feeds: RSS/Atom feed URLs to poll for candidate articles. Must be
            non-empty under ADR-0024. Each URL's host must itself be
            allowlisted (validated at load time).
        window_hours: Optional per-source override of the global
            ``default_window_hours``. ``None`` means "use the default".
        tags: Curated, source-level, multi-valued bucket dimensions for
            Irene's deterministic RSS clustering (ADR-0087 Part B). Each
            tag is drawn from the closed :data:`_KNOWN_TAGS` vocabulary,
            lowercase, and the tuple is sorted and de-duplicated. An empty
            tuple (a source with no ``tags:`` key) is valid — such items are
            bucketed under a reserved ``untagged`` dimension, never dropped.
    """

    domain: str
    name: str
    added_on: str
    rationale: str
    feeds: tuple[str, ...] = field(default_factory=tuple)
    window_hours: int | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AllowlistConfig:
    """Top-level loaded allowlist configuration.

    Attributes:
        entries: The allowlist entries in file order.
        default_window_hours: Global default time window applied to feed
            items whose owning entry does not override it.
    """

    entries: tuple[AllowlistEntry, ...]
    default_window_hours: int


def _parse_tags(tags_raw: object, *, src: Path, index: int) -> tuple[str, ...]:
    """Parse, validate, normalise, and de-duplicate one source's ``tags``.

    A missing ``tags:`` key (``tags_raw is None``) yields ``()`` — an
    untagged source is valid and clusters under the reserved ``untagged``
    dimension (ADR-0087 Part B). Present tags must be a list of non-empty
    strings drawn from the closed :data:`_KNOWN_TAGS` vocabulary; each is
    lowercased and the result is a sorted, de-duplicated tuple so the key
    material feeding Irene's bucket dimensions is deterministic.

    Args:
        tags_raw: The raw value of the entry's ``tags`` key, or ``None``.
        src: The allowlist file path, for error messages.
        index: The 0-based ``sources`` index, for error messages.

    Returns:
        The normalised, sorted, de-duplicated tag tuple (possibly empty).

    Raises:
        AllowlistError: If ``tags`` is not a list, contains a non-string
            or empty entry, or contains a tag outside :data:`_KNOWN_TAGS`.
    """
    if tags_raw is None:
        return ()
    if not isinstance(tags_raw, list):
        raise AllowlistError(
            f"{src}: sources[{index}].tags must be a list of strings if "
            f"present; got {type(tags_raw).__name__}."
        )
    normalised: set[str] = set()
    for j, tag in enumerate(tags_raw):
        if not isinstance(tag, str) or not tag.strip():
            raise AllowlistError(
                f"{src}: sources[{index}].tags[{j}] must be a non-empty string; got {tag!r}."
            )
        low = tag.strip().lower()
        if low not in _KNOWN_TAGS:
            raise AllowlistError(
                f"{src}: sources[{index}].tags[{j}] ({tag!r}) is not in the "
                f"curated tag vocabulary. Allowed: {sorted(_KNOWN_TAGS)}."
            )
        normalised.add(low)
    return tuple(sorted(normalised))


def load_allowlist(path: Path | None = None) -> AllowlistConfig:
    """Load and validate ``config/web_research.yaml``.

    Args:
        path: Override the default path. If ``None``, loads
            ``<repo_root>/config/web_research.yaml``.

    Returns:
        An :class:`AllowlistConfig` with the parsed entries and the
        global default window.

    Raises:
        AllowlistError: If the file is missing, unreadable, not YAML,
            missing any required field, has an empty ``feeds`` list,
            has a feed URL whose host is not on the allowlist, has a
            ``tags`` entry outside the closed :data:`_KNOWN_TAGS`
            vocabulary, or has a missing/invalid ``default_window_hours``.
    """
    src = path or _DEFAULT_PATH
    if not src.exists():
        raise AllowlistError(f"Allowlist file not found: {src}")

    try:
        raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AllowlistError(f"Could not parse YAML at {src}: {exc}") from exc
    except OSError as exc:
        raise AllowlistError(f"Could not read {src}: {exc}") from exc

    if not isinstance(raw, dict):
        raise AllowlistError(
            f"{src}: top-level document must be a mapping; got {type(raw).__name__}."
        )

    if "default_window_hours" not in raw:
        raise AllowlistError(f"{src}: required top-level field 'default_window_hours' is missing.")
    default_window_raw = raw["default_window_hours"]
    if not isinstance(default_window_raw, int) or default_window_raw <= 0:
        raise AllowlistError(
            f"{src}: 'default_window_hours' must be a positive integer; got {default_window_raw!r}."
        )
    default_window_hours = default_window_raw

    if "sources" not in raw:
        raise AllowlistError(f"{src}: top-level document must contain a 'sources' key.")
    items = raw["sources"]
    if not isinstance(items, list) or not items:
        raise AllowlistError(
            f"{src}: 'sources' must be a non-empty list; got {type(items).__name__}."
        )

    entries: list[AllowlistEntry] = []
    required = ("domain", "name", "added_on", "rationale")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise AllowlistError(
                f"{src}: sources[{i}] must be a mapping; got {type(item).__name__}."
            )
        missing = [k for k in required if not item.get(k)]
        if missing:
            raise AllowlistError(f"{src}: sources[{i}] is missing required field(s): {missing}.")

        feeds_raw = item.get("feeds")
        if not isinstance(feeds_raw, list) or not feeds_raw:
            raise AllowlistError(
                f"{src}: sources[{i}].feeds must be a non-empty list; "
                f"got {type(feeds_raw).__name__}."
            )
        feeds: list[str] = []
        for j, feed in enumerate(feeds_raw):
            if not isinstance(feed, str) or not feed.strip():
                raise AllowlistError(
                    f"{src}: sources[{i}].feeds[{j}] must be a non-empty string; got {feed!r}."
                )
            feeds.append(feed.strip())

        window_raw = item.get("window_hours")
        if window_raw is None:
            window_hours: int | None = None
        elif isinstance(window_raw, int) and window_raw > 0:
            window_hours = window_raw
        else:
            raise AllowlistError(
                f"{src}: sources[{i}].window_hours must be a positive "
                f"integer if present; got {window_raw!r}."
            )

        tags = _parse_tags(item.get("tags"), src=src, index=i)

        entries.append(
            AllowlistEntry(
                domain=str(item["domain"]).strip().lower(),
                name=str(item["name"]).strip(),
                added_on=str(item["added_on"]).strip(),
                rationale=str(item["rationale"]).strip(),
                feeds=tuple(feeds),
                window_hours=window_hours,
                tags=tags,
            )
        )

    # Second pass: every feed URL's host must itself be allowlisted.
    allowed_hosts = {e.domain for e in entries}
    for i, entry in enumerate(entries):
        for j, feed_url in enumerate(entry.feeds):
            host = (urlparse(feed_url).hostname or "").lower()
            if not host:
                raise AllowlistError(
                    f"{src}: sources[{i}].feeds[{j}] has no parseable hostname: {feed_url!r}."
                )
            if host not in allowed_hosts:
                raise AllowlistError(
                    f"{src}: sources[{i}].feeds[{j}] ({feed_url!r}) is on "
                    f"host {host!r}, which is not on the allowlist. Add a "
                    "sources entry for that host, or fix the feed URL."
                )

    logger.info(
        "Web research allowlist loaded from %s: %d entries, default_window_hours=%d.",
        src,
        len(entries),
        default_window_hours,
    )
    return AllowlistConfig(
        entries=tuple(entries),
        default_window_hours=default_window_hours,
    )


def is_allowed(url: str, entries: list[AllowlistEntry]) -> bool:
    """Return True iff ``url``'s hostname exactly matches an allowlist entry.

    Matching is case-insensitive on hostname. v1 does not support wildcard
    patterns or subdomain roll-up: every hostname that should be reachable
    must appear verbatim in the allowlist.

    Args:
        url: The fully-qualified URL to check.
        entries: The allowlist entries returned by :func:`load_allowlist`.

    Returns:
        ``True`` if the URL's hostname matches an entry. ``False`` on any
        other outcome, including invalid URLs.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == e.domain for e in entries)


def get_effective_window(
    entry: AllowlistEntry,
    global_default_hours: int,
) -> timedelta:
    """Return the effective time window for ``entry``.

    Per-entry ``window_hours`` takes precedence; falls back to
    ``global_default_hours``.

    Args:
        entry: The allowlist entry.
        global_default_hours: The ``default_window_hours`` from the top
            level of the config.

    Returns:
        The time window as a :class:`~datetime.timedelta`.
    """
    hours = entry.window_hours if entry.window_hours is not None else global_default_hours
    return timedelta(hours=hours)
