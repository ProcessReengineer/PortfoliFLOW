# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Loader for the Reading Desk's document profiles (ADR-0139 §3).

A profile is a versioned file under ``config/scraper_profiles/``, one per
document type, with typed fields. The field lists are those of the concept
record ``docs/concepts/reading-desk-concept.md`` §5.2.

Pure: no database, no web. No cache, no fallback and no default profile: a
missing or malformed file raises :class:`ProfileError`, because a stored
reading refers to these ids, versions, keys and types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.exceptions import PortfoliFlowError
from services.scraper.models import KeywordType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_DIR = _REPO_ROOT / "config" / "scraper_profiles"

#: The profile ids, in the order they are offered (ADR-0139 §3). The loader
#: demands exactly these files; core's _VALID_DOCUMENT_TYPES mirrors the set.
PROFILE_IDS: tuple[str, ...] = ("quarterly_report", "capital_notice", "pitch_deck", "other")

_PROFILE_KEYS = frozenset({"id", "version", "label", "fields"})
_REQUIRED_FIELD_KEYS = frozenset({"key", "label", "type"})
_OPTIONAL_FIELD_KEYS = frozenset({"unit", "hint"})
_FIELD_TYPES = tuple(member.value for member in KeywordType)
# Used with fullmatch: ``$`` would accept a trailing newline.
_FIELD_KEY = re.compile(r"[a-z][a-z0-9_]*")


class ProfileError(PortfoliFlowError, ValueError):
    """A profile file is missing, unreadable or malformed."""


@dataclass(frozen=True)
class ProfileField:
    """One typed field of a profile.

    Attributes:
        key: Stable identifier, unique within the profile.
        label: Display label.
        type: The type hint for the model.
        unit: Optional unit: ``currency`` (the document's currency) or ``x``.
        hint: Optional instruction that tells the model what the field means.
    """

    key: str
    label: str
    type: KeywordType
    unit: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class Profile:
    """One document type's profile.

    Attributes:
        id: The document type; equals the file's stem.
        version: Recorded with every reading; raised with every change to ``fields``.
        label: Display label.
        fields: The fields, in file order.
    """

    id: str
    version: int
    label: str
    fields: tuple[ProfileField, ...]


def load_profiles(directory: Path | None = None) -> dict[str, Profile]:
    """Load and validate the four profiles.

    Args:
        directory: Override directory (for tests). If ``None``, uses
            ``<repo root>/config/scraper_profiles``.

    Returns:
        The profiles keyed by id, in :data:`PROFILE_IDS` order.

    Raises:
        ProfileError: If the directory or a file is missing or unreadable, the
            directory holds another YAML file, or a file breaks the profile
            schema. The message names the file and the offending key.
    """
    src = directory or _PROFILE_DIR
    try:
        found = {p.name for p in src.iterdir() if p.suffix.lower() in (".yaml", ".yml")}
    except OSError as exc:
        raise ProfileError(f"Could not read the profile directory {src}: {exc}") from exc

    expected = {f"{profile_id}.yaml" for profile_id in PROFILE_IDS}
    if missing := expected - found:
        raise ProfileError(f"{src}: missing profile file(s) {sorted(missing)}.")
    if unexpected := found - expected:
        raise ProfileError(f"{src}: unexpected profile file(s) {sorted(unexpected)}.")

    return {profile_id: _load_profile(src / f"{profile_id}.yaml") for profile_id in PROFILE_IDS}


def get_profile(profile_id: str, directory: Path | None = None) -> Profile:
    """Return one profile by id.

    Args:
        profile_id: One of :data:`PROFILE_IDS`.
        directory: Override directory (for tests).

    Returns:
        The profile.

    Raises:
        ProfileError: If the id is unknown or the profiles do not load.
    """
    if profile_id not in PROFILE_IDS:
        # The id may be a model's answer to GP text, so it is not repeated.
        raise ProfileError(f"Unknown profile id; expected one of {list(PROFILE_IDS)}.")
    return load_profiles(directory)[profile_id]


def _load_profile(path: Path) -> Profile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # safe_load raises more than YAMLError: ValueError for an impossible
        # date, KeyError or AttributeError for a bad explicit tag.
        raise ProfileError(f"{path}: cannot be read or parsed as YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: the document must be a mapping.")
    if missing := _PROFILE_KEYS - raw.keys():
        raise ProfileError(f"{path}: missing key(s) {sorted(missing)}.")
    if unknown := raw.keys() - _PROFILE_KEYS:
        raise ProfileError(f"{path}: unknown key(s) {sorted(unknown, key=repr)}.")

    if raw["id"] != path.stem:
        raise ProfileError(f"{path}: 'id' must equal the file stem {path.stem!r}.")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ProfileError(f"{path}: 'version' must be an integer >= 1; got {version!r}.")
    label = _non_empty_string(raw["label"], path, "label")
    if not isinstance(raw["fields"], list):
        raise ProfileError(f"{path}: 'fields' must be a list.")

    fields: list[ProfileField] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["fields"]):
        parsed = _parse_field(item, path, f"fields[{index}]")
        if parsed.key in seen:
            raise ProfileError(f"{path}: 'fields[{index}].key' repeats {parsed.key!r}.")
        seen.add(parsed.key)
        fields.append(parsed)

    return Profile(id=path.stem, version=version, label=label, fields=tuple(fields))


def _parse_field(item: Any, path: Path, where: str) -> ProfileField:
    if not isinstance(item, dict):
        raise ProfileError(f"{path}: '{where}' must be a mapping.")
    if missing := _REQUIRED_FIELD_KEYS - item.keys():
        raise ProfileError(f"{path}: '{where}' lacks key(s) {sorted(missing)}.")
    if unknown := item.keys() - _REQUIRED_FIELD_KEYS - _OPTIONAL_FIELD_KEYS:
        raise ProfileError(f"{path}: '{where}' has unknown key(s) {sorted(unknown, key=repr)}.")

    key = _non_empty_string(item["key"], path, f"{where}.key")
    if not _FIELD_KEY.fullmatch(key):
        raise ProfileError(f"{path}: '{where}.key' must match ^[a-z][a-z0-9_]*$; got {key!r}.")
    if item["type"] not in _FIELD_TYPES:
        raise ProfileError(f"{path}: '{where}.type' must be one of {list(_FIELD_TYPES)}.")
    optional = {
        name: _non_empty_string(item[name], path, f"{where}.{name}")
        for name in _OPTIONAL_FIELD_KEYS & item.keys()
    }
    return ProfileField(
        key=key,
        label=_non_empty_string(item["label"], path, f"{where}.label"),
        type=KeywordType(item["type"]),
        unit=optional.get("unit"),
        hint=optional.get("hint"),
    )


def _non_empty_string(value: Any, path: Path, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{path}: '{where}' must be a non-empty string.")
    return value
