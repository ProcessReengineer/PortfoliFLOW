# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Reading Desk profiles, their loader and the additive model fields (ADR-0139 §3)."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.exceptions import PortfoliFlowError
from services.scraper.models import (
    Confidence,
    Finding,
    Keyword,
    KeywordType,
    ReportExtraction,
    ScraperResult,
)
from services.scraper.profiles import PROFILE_IDS, ProfileError, get_profile, load_profiles

_SHIPPED = Path(__file__).resolve().parents[2] / "config" / "scraper_profiles"
_TARGET = "capital_notice.yaml"

# ---------------------------------------------------------------------------
# The shipped profiles
# ---------------------------------------------------------------------------


def test_shipped_profiles_load() -> None:
    profiles = load_profiles()

    assert tuple(profiles) == PROFILE_IDS
    assert tuple(profile.id for profile in profiles.values()) == PROFILE_IDS
    assert {profile.version for profile in profiles.values()} == {1}
    assert {name: len(profile.fields) for name, profile in profiles.items()} == {
        "quarterly_report": 17,
        "capital_notice": 12,
        "pitch_deck": 19,
        "other": 0,
    }
    for profile in profiles.values():
        assert all(isinstance(f.type, KeywordType) for f in profile.fields)


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_get_profile_returns_the_profile(profile_id: str) -> None:
    assert get_profile(profile_id) == load_profiles()[profile_id]


def test_no_shipped_field_asks_for_bank_details() -> None:
    """ADR-0139 §4. The bare word "account" is legitimate ("capital account")."""
    forbidden = ("iban", "bic", "swift", "bank", "routing", "wire", "account number")
    offenders = [
        f"{profile.id}.{profile_field.key}: {term}"
        for profile in load_profiles().values()
        for profile_field in profile.fields
        for text in (profile_field.key, profile_field.label, profile_field.hint or "")
        for term in forbidden
        if term in text.lower()
    ]
    assert not offenders


def test_lp_and_fund_level_are_separate_fields() -> None:
    keys = {f.key for f in get_profile("quarterly_report").fields}
    for figure in ("nav", "called", "distributed", "unfunded"):
        assert {f"{figure}_lp", f"{figure}_fund"} <= keys


# ---------------------------------------------------------------------------
# The refusals: one defect in a valid copy of the four files
# ---------------------------------------------------------------------------

Defect = Callable[[Path], None]


def _edit(change: Callable[[dict[str, Any]], object]) -> Defect:
    def apply(directory: Path) -> None:
        path = directory / _TARGET
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(profile)
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")

    return apply


def _set(name: str, value: object) -> Defect:
    return _edit(lambda profile: profile.update({name: value}))


def _drop(name: str) -> Defect:
    return _edit(lambda profile: profile.pop(name))


def _set_in_field(name: str, value: object) -> Defect:
    return _edit(lambda profile: profile["fields"][0].update({name: value}))


def _drop_in_field(name: str) -> Defect:
    return _edit(lambda profile: profile["fields"][0].pop(name))


def _content(data: bytes) -> Defect:
    return lambda directory: (directory / _TARGET).write_bytes(data)


def _repeat_key(profile: dict[str, Any]) -> None:
    profile["fields"][1]["key"] = profile["fields"][0]["key"]


def _file_is_a_directory(directory: Path) -> None:
    (directory / _TARGET).unlink()
    (directory / _TARGET).mkdir()


# id, defect, the file the message must name, what else it must name
_REFUSALS: list[tuple[str, Defect, str, str]] = [
    ("no-directory", shutil.rmtree, "profiles", "directory"),
    ("missing-file", lambda d: (d / _TARGET).unlink(), _TARGET, "missing"),
    ("stray-yaml", lambda d: (d / "letter.yaml").touch(), "letter.yaml", "unexpected"),
    ("stray-yml", lambda d: (d / "letter.yml").touch(), "letter.yml", "unexpected"),
    ("unreadable", _file_is_a_directory, _TARGET, "cannot be read"),
    ("not-utf-8", _content(b"\xff\xfe\xfa"), _TARGET, "cannot be read"),
    ("bad-yaml", _content(b"id: [unclosed"), _TARGET, "cannot be read"),
    ("impossible-date", _content(b"id: 2026-02-30"), _TARGET, "cannot be read"),
    ("bad-tag", _content(b"id: !!bool perhaps"), _TARGET, "cannot be read"),
    ("empty-file", _content(b""), _TARGET, "mapping"),
    ("not-a-mapping", _content(b"- id"), _TARGET, "mapping"),
    ("no-id", _drop("id"), _TARGET, "'id'"),
    ("no-version", _drop("version"), _TARGET, "'version'"),
    ("no-label", _drop("label"), _TARGET, "'label'"),
    ("no-fields", _drop("fields"), _TARGET, "'fields'"),
    ("unknown-top-key", _set("extra", 1), _TARGET, "'extra'"),
    ("id-not-stem", _set("id", "other"), _TARGET, "'id'"),
    ("version-zero", _set("version", 0), _TARGET, "'version'"),
    ("version-bool", _set("version", True), _TARGET, "'version'"),
    ("version-float", _set("version", 1.0), _TARGET, "'version'"),
    ("version-string", _set("version", "1"), _TARGET, "'version'"),
    ("label-empty", _set("label", " "), _TARGET, "'label'"),
    ("label-not-string", _set("label", 7), _TARGET, "'label'"),
    ("fields-not-list", _set("fields", {}), _TARGET, "'fields'"),
    ("fields-null", _set("fields", None), _TARGET, "'fields'"),
    ("field-not-mapping", _set("fields", ["nav"]), _TARGET, "fields[0]"),
    ("field-no-key", _drop_in_field("key"), _TARGET, "'key'"),
    ("field-no-label", _drop_in_field("label"), _TARGET, "'label'"),
    ("field-no-type", _drop_in_field("type"), _TARGET, "'type'"),
    ("field-unknown-key", _set_in_field("extra", 1), _TARGET, "'extra'"),
    ("key-upper-case", _set_in_field("key", "NavLp"), _TARGET, "fields[0].key"),
    ("key-leading-digit", _set_in_field("key", "1nav"), _TARGET, "fields[0].key"),
    ("key-trailing-newline", _set_in_field("key", "nav\n"), _TARGET, "fields[0].key"),
    ("key-not-string", _set_in_field("key", 7), _TARGET, "fields[0].key"),
    ("key-repeats", _edit(_repeat_key), _TARGET, "fields[1].key"),
    ("field-label-empty", _set_in_field("label", ""), _TARGET, "fields[0].label"),
    ("type-unknown", _set_in_field("type", "Money"), _TARGET, "fields[0].type"),
    ("type-unhashable", _set_in_field("type", ["Text"]), _TARGET, "fields[0].type"),
    ("unit-empty", _set_in_field("unit", ""), _TARGET, "fields[0].unit"),
    ("unit-null", _set_in_field("unit", None), _TARGET, "fields[0].unit"),
    ("hint-empty", _set_in_field("hint", ""), _TARGET, "fields[0].hint"),
    ("hint-not-string", _set_in_field("hint", 7), _TARGET, "fields[0].hint"),
]


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "profiles"
    shutil.copytree(_SHIPPED, directory)
    return directory


def test_the_copy_is_valid(profile_dir: Path) -> None:
    """The control: without a defect the copy loads, also after a YAML round trip."""
    _edit(lambda profile: None)(profile_dir)
    assert load_profiles(profile_dir) == load_profiles()


@pytest.mark.parametrize(
    ("defect", "file_named", "key_named"),
    [pytest.param(*case, id=name) for name, *case in _REFUSALS],
)
def test_loader_refuses(profile_dir: Path, defect: Defect, file_named: str, key_named: str) -> None:
    defect(profile_dir)

    with pytest.raises(ProfileError, match=re.escape(key_named)) as caught:
        load_profiles(profile_dir)

    assert file_named in str(caught.value)


def test_get_profile_refuses_an_unknown_id() -> None:
    with pytest.raises(ProfileError):
        get_profile("nope")


def test_profile_error_is_typed_and_a_value_error() -> None:
    assert issubclass(ProfileError, PortfoliFlowError)
    assert issubclass(ProfileError, ValueError)


# ---------------------------------------------------------------------------
# The models grew additively
# ---------------------------------------------------------------------------


def test_model_defaults() -> None:
    finding = Finding(
        keyword=Keyword(name="NAV", type=KeywordType.NUMBER),
        value="",
        source="",
        confidence=Confidence.HIGH,
    )
    assert finding.origin == "adhoc"
    assert finding.page is None
    assert finding.key == ""

    extraction = ReportExtraction(filename="x")
    assert extraction.note is None
    assert extraction.document_date is None
    assert extraction.document_type == ""

    assert ScraperResult().prompt_version == ""
