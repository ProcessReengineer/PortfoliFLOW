# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Capability-matrix routing, priority, and schema-sanity tests (ADR-0091).

The matrix is the third seam of provider-agnosticism and the authoritative
coverage declaration (property 2). These tests pin routing by priority, the
unsupported-capability error, and — the schema-sanity guard — that the shipped
matrix contains no aspirational entries: every provider has a builder, and
every declared scheme/kind is a known value.
"""

from __future__ import annotations

import pytest

from services.market_data.dto import IDENTIFIER_SCHEMES, SeriesKind
from services.market_data.factory import (
    _ADAPTER_BUILDERS,
    build_adapter,
    get_capability_matrix,
    load_capability_matrix,
    resolve_provider_name,
)
from services.market_data.provider import (
    MarketDataConfigurationError,
    MarketDataProvider,
    UnsupportedCapabilityError,
)


@pytest.fixture(scope="module")
def matrix():
    return get_capability_matrix()


class TestRouting:
    def test_ticker_price_routes_to_yahoo(self, matrix) -> None:
        assert resolve_provider_name("ticker", SeriesKind.NAV_PRICE, matrix=matrix) == "yahoo"

    def test_ticker_dividend_routes_to_yahoo(self, matrix) -> None:
        assert resolve_provider_name("ticker", SeriesKind.DIVIDEND, matrix=matrix) == "yahoo"

    def test_isin_price_is_unroutable_unforced(self, matrix) -> None:
        # Yahoo does not serve `isin`, and synthetic is `routing: forced_only`
        # (excluded from unforced routing) — so an isin/nav_price request is now
        # genuinely uncovered on the unforced path. It used to fall through to
        # synthetic; that is now the forced path's job only.
        with pytest.raises(UnsupportedCapabilityError):
            resolve_provider_name("isin", SeriesKind.NAV_PRICE, matrix=matrix)

    def test_ticker_coupon_is_unroutable_unforced(self, matrix) -> None:
        # The demo defect at the routing seam: yahoo does not serve `coupon`,
        # and synthetic is now `forced_only`, so ticker/coupon routes to NOTHING
        # on the unforced path (raising) instead of falling through to synthetic
        # — whose adapter build would then fail with an unset fixture and error
        # every eligible investment.
        with pytest.raises(UnsupportedCapabilityError):
            resolve_provider_name("ticker", SeriesKind.COUPON, matrix=matrix)

    def test_weight_kind_is_unroutable(self, matrix) -> None:
        # Slice-5 cleanup: `weight_*` kinds were removed from the synthetic
        # entry because the ingest write path cannot route a weight series
        # yet (no bucket dimension on the DTO). No provider now serves a
        # weight kind, so the shipped matrix routes nothing for it — the
        # declared non-availability of ADR-0091 property 2.
        with pytest.raises(UnsupportedCapabilityError):
            resolve_provider_name("isin", SeriesKind.WEIGHT_RATING, matrix=matrix)


class TestPriority:
    def test_higher_priority_wins_when_both_cover(self) -> None:
        # yahoo (100) and synthetic (0) both cover ticker/nav_price; the higher
        # priority wins so the real provider is never shadowed by synthetic.
        matrix = load_capability_matrix()
        priorities = {p.name: p.priority for p in matrix.providers}
        assert priorities["yahoo"] > priorities["synthetic"]
        assert resolve_provider_name("ticker", SeriesKind.NAV_PRICE, matrix=matrix) == "yahoo"

    def test_providers_sorted_descending(self) -> None:
        matrix = load_capability_matrix()
        priorities = [p.priority for p in matrix.providers]
        assert priorities == sorted(priorities, reverse=True)


class TestUnsupported:
    def test_unroutable_scheme_kind_raises(self) -> None:
        # Synthetic's full coverage means the shipped matrix routes everything;
        # to exercise the gap, drop synthetic so yahoo (ticker + price/dividend
        # only) is left — an isin/weight request is then genuinely uncovered.
        trimmed = _matrix_without_synthetic()
        with pytest.raises(UnsupportedCapabilityError):
            resolve_provider_name("isin", SeriesKind.WEIGHT_SECTOR, matrix=trimmed)

    def test_build_adapter_unknown_name_raises(self) -> None:
        # `preqin` is a valid identifier scheme but has no adapter builder yet.
        # (bloomberg now HAS a builder, so it is no longer the unknown case.)
        with pytest.raises(UnsupportedCapabilityError):
            build_adapter("preqin")


class TestSchemaSanity:
    def test_only_known_providers_declared(self, matrix) -> None:
        for provider in matrix.providers:
            assert provider.name in _ADAPTER_BUILDERS

    def test_only_known_schemes_and_kinds_declared(self, matrix) -> None:
        for provider in matrix.providers:
            assert provider.schemes <= IDENTIFIER_SCHEMES
            assert provider.kinds <= set(SeriesKind)

    def test_expected_providers_present(self, matrix) -> None:
        names = {p.name for p in matrix.providers}
        assert names == {"yahoo", "synthetic"}

    def test_yahoo_declares_only_implemented_coverage(self, matrix) -> None:
        yahoo = next(p for p in matrix.providers if p.name == "yahoo")
        assert yahoo.schemes == {"ticker"}
        assert yahoo.kinds == {SeriesKind.NAV_PRICE, SeriesKind.DIVIDEND}


class TestEnabledFlag:
    """The optional per-provider ``enabled`` flag (#036 §0.2).

    A disabled provider is fully validated but skipped for routing — "disabled"
    means "not declared" for routing purposes. These pin the boolean-only
    parsing, the unknown-key rejection that shipped alongside it, and that the
    shipped bloomberg entry is disabled (so it never shadows synthetic on
    figi/isin).
    """

    def test_shipped_bloomberg_is_disabled_and_unrouted(self, matrix) -> None:
        # bloomberg is declared (with a builder + credential policy) but ships
        # enabled: false, so it is absent from the routing set entirely.
        assert "bloomberg" not in {p.name for p in matrix.providers}
        assert matrix.credential_policy("bloomberg").requires is False

    def test_disabled_provider_excluded_from_routing(self, tmp_path) -> None:
        m = _load_full_matrix(
            tmp_path,
            "  - name: bloomberg\n"
            "    priority: 200\n"
            "    enabled: false\n"
            "    schemes: [figi, isin]\n"
            "    kinds: [nav_price]\n",
            "  bloomberg: none\n",
        )
        assert "bloomberg" not in {p.name for p in m.providers}

    def test_enabled_true_is_routed(self, tmp_path) -> None:
        m = _load_full_matrix(
            tmp_path,
            "  - name: bloomberg\n"
            "    priority: 200\n"
            "    enabled: true\n"
            "    schemes: [figi, isin]\n"
            "    kinds: [nav_price]\n",
            "  bloomberg: none\n",
        )
        assert m.resolve("figi", SeriesKind.NAV_PRICE).name == "bloomberg"

    def test_absent_enabled_defaults_to_routed(self, tmp_path) -> None:
        # No `enabled` key → default True → routed.
        m = _load_full_matrix(
            tmp_path,
            "  - name: bloomberg\n"
            "    priority: 200\n"
            "    schemes: [figi, isin]\n"
            "    kinds: [nav_price]\n",
            "  bloomberg: none\n",
        )
        assert m.resolve("isin", SeriesKind.NAV_PRICE).name == "bloomberg"

    def test_non_boolean_enabled_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError, match="enabled"):
            _load_full_matrix(
                tmp_path,
                "  - name: bloomberg\n"
                "    priority: 200\n"
                "    enabled: yes-please\n"
                "    schemes: [figi]\n"
                "    kinds: [nav_price]\n",
                "  bloomberg: none\n",
            )

    def test_unknown_provider_key_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError, match="unknown keys"):
            _load_full_matrix(
                tmp_path,
                "  - name: bloomberg\n"
                "    priority: 200\n"
                "    schemes: [figi]\n"
                "    kinds: [nav_price]\n"
                "    bogus: 1\n",
                "  bloomberg: none\n",
            )


class TestForcedOnlyRouting:
    """The optional per-provider ``routing`` flag (forced_only exclusion).

    A ``routing: forced_only`` provider is dropped from the unforced priority
    path entirely — reachable only through the ``--provider`` forced path —
    while its coverage (``serves``) stays routing-blind so the forced path can
    still resolve it. These pin the shipped synthetic flag, the unforced
    exclusion, the routing-blind coverage, the ``normal`` default, and the loud
    rejection of an invalid value.
    """

    def test_shipped_synthetic_is_forced_only(self, matrix) -> None:
        synthetic = next(p for p in matrix.providers if p.name == "synthetic")
        assert synthetic.routing == "forced_only"
        assert synthetic.forced_only is True
        # Still PRESENT in the routing set so the forced path (which iterates
        # `matrix.providers` and calls `serves`) can reach it.
        assert "synthetic" in {p.name for p in matrix.providers}

    def test_forced_only_excluded_from_unforced_resolve(self, tmp_path) -> None:
        # bloomberg (figi/isin, a real builder) marked forced_only: yahoo does
        # not serve figi, so figi/nav_price is uncovered on the unforced path
        # even though bloomberg's coverage would match.
        m = _load_full_matrix(
            tmp_path,
            "  - name: bloomberg\n"
            "    priority: 200\n"
            "    routing: forced_only\n"
            "    schemes: [figi, isin]\n"
            "    kinds: [nav_price]\n",
            "  bloomberg: none\n",
        )
        with pytest.raises(UnsupportedCapabilityError):
            m.resolve("figi", SeriesKind.NAV_PRICE)

    def test_forced_only_coverage_is_routing_blind(self, tmp_path) -> None:
        # The forced path relies on `serves` staying True for a forced_only
        # provider — that is how `--provider bloomberg` reaches it despite the
        # unforced exclusion.
        m = _load_full_matrix(
            tmp_path,
            "  - name: bloomberg\n"
            "    priority: 200\n"
            "    routing: forced_only\n"
            "    schemes: [figi, isin]\n"
            "    kinds: [nav_price]\n",
            "  bloomberg: none\n",
        )
        bloomberg = next(p for p in m.providers if p.name == "bloomberg")
        assert bloomberg.forced_only is True
        assert bloomberg.serves("figi", SeriesKind.NAV_PRICE) is True

    def test_absent_routing_defaults_to_normal(self, matrix) -> None:
        yahoo = next(p for p in matrix.providers if p.name == "yahoo")
        assert yahoo.routing == "normal"
        assert yahoo.forced_only is False

    def test_explicit_normal_routing_participates(self, tmp_path) -> None:
        m = _load_full_matrix(
            tmp_path,
            "  - name: bloomberg\n"
            "    priority: 200\n"
            "    routing: normal\n"
            "    schemes: [figi, isin]\n"
            "    kinds: [nav_price]\n",
            "  bloomberg: none\n",
        )
        # `normal` → participates in unforced routing exactly as before.
        assert m.resolve("figi", SeriesKind.NAV_PRICE).name == "bloomberg"

    def test_invalid_routing_value_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError, match="routing"):
            _load_full_matrix(
                tmp_path,
                "  - name: bloomberg\n"
                "    priority: 200\n"
                "    routing: sometimes\n"
                "    schemes: [figi]\n"
                "    kinds: [nav_price]\n",
                "  bloomberg: none\n",
            )


class TestBuild:
    def test_build_yahoo_is_a_provider(self) -> None:
        assert isinstance(build_adapter("yahoo"), MarketDataProvider)

    def test_build_synthetic_needs_fixture_env(self, monkeypatch) -> None:
        monkeypatch.delenv("MARKET_DATA_SYNTHETIC_FIXTURE", raising=False)
        with pytest.raises(MarketDataConfigurationError):
            build_adapter("synthetic")

    def test_build_synthetic_with_fixture_env(self, monkeypatch, tmp_path) -> None:
        fixture = tmp_path / "f.json"
        fixture.write_text('{"AAPL": {"nav_price": []}}', encoding="utf-8")
        monkeypatch.setenv("MARKET_DATA_SYNTHETIC_FIXTURE", str(fixture))
        assert isinstance(build_adapter("synthetic"), MarketDataProvider)


class TestCredentialPolicy:
    """Credential-policy parsing and schema-sanity (ADR-0095 §2).

    The policy is a fixed per-provider invariant declared in the matrix's
    ``credentials`` section. These pin that the shipped matrix parses, that a
    non-series provider (``openfigi``) may declare a policy without a routing
    entry, and — the schema-sanity guard — that an unknown policy value is
    rejected at load rather than silently defaulted.
    """

    def test_shipped_policies_parse(self, matrix) -> None:
        yahoo = matrix.credential_policy("yahoo")
        assert yahoo.requires is False
        synthetic = matrix.credential_policy("synthetic")
        assert synthetic.requires is False
        openfigi = matrix.credential_policy("openfigi")
        assert (openfigi.requires, openfigi.env_fallback, openfigi.optional) == (
            True,
            True,
            True,
        )

    def test_unknown_provider_policy_raises(self, matrix) -> None:
        # `pitchbook` declares no policy in the shipped matrix (bloomberg now
        # DOES — `bloomberg: none` — so it is no longer the unknown case).
        with pytest.raises(UnsupportedCapabilityError):
            matrix.credential_policy("pitchbook")

    def test_none_policy_parses(self, tmp_path) -> None:
        m = _load_matrix(tmp_path, "credentials:\n  yahoo: none\n")
        assert m.credential_policy("yahoo").requires is False

    def test_env_fallback_optional_policy_parses(self, tmp_path) -> None:
        m = _load_matrix(
            tmp_path,
            "credentials:\n"
            "  yahoo: none\n"
            "  openfigi:\n"
            "    env_fallback: allowed\n"
            "    optional: true\n",
        )
        p = m.credential_policy("openfigi")
        assert (p.requires, p.env_fallback, p.optional) == (True, True, True)

    def test_forbidden_policy_parses_optional_defaults_false(self, tmp_path) -> None:
        m = _load_matrix(
            tmp_path,
            "credentials:\n  yahoo: none\n  bloomberg:\n    env_fallback: forbidden\n",
        )
        p = m.credential_policy("bloomberg")
        assert (p.requires, p.env_fallback, p.optional) == (True, False, False)

    def test_missing_credentials_section_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(tmp_path, "")

    def test_empty_credentials_section_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(tmp_path, "credentials: {}\n")

    def test_unknown_env_fallback_value_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(
                tmp_path,
                "credentials:\n  yahoo:\n    env_fallback: maybe\n",
            )

    def test_non_bool_optional_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(
                tmp_path,
                "credentials:\n  yahoo:\n    env_fallback: allowed\n    optional: 3\n",
            )

    def test_unknown_policy_key_rejected(self, tmp_path) -> None:
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(
                tmp_path,
                "credentials:\n  yahoo:\n    env_fallback: allowed\n    bogus: 1\n",
            )

    def test_scalar_policy_value_rejected(self, tmp_path) -> None:
        # Neither `none` nor a mapping.
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(tmp_path, "credentials:\n  yahoo: banana\n")

    def test_routing_provider_without_policy_rejected(self, tmp_path) -> None:
        # `yahoo` is a routing provider but declares no credential policy.
        with pytest.raises(MarketDataConfigurationError):
            _load_matrix(tmp_path, "credentials:\n  openfigi: none\n")


def _matrix_without_synthetic():
    """Return the shipped matrix with the synthetic provider removed.

    Leaves only yahoo, so an ``isin`` / weight-kind request is genuinely
    uncovered — the case that must raise ``UnsupportedCapabilityError``.
    """
    from dataclasses import replace

    full = load_capability_matrix()
    return replace(
        full,
        providers=tuple(p for p in full.providers if p.name != "synthetic"),
    )


def _matrix_yaml(credentials_block: str) -> str:
    """Compose a minimal valid ``providers`` block plus a given credentials tail.

    The single ``yahoo`` routing entry has a registered builder, so only the
    ``credentials`` section under test varies across cases.
    """
    return (
        "providers:\n"
        "  - name: yahoo\n"
        "    priority: 100\n"
        "    schemes: [ticker]\n"
        "    kinds: [nav_price, dividend]\n" + credentials_block
    )


def _load_matrix(tmp_path, credentials_block: str):
    """Write a temp matrix with ``credentials_block`` and load it."""
    path = tmp_path / "m.yaml"
    path.write_text(_matrix_yaml(credentials_block), encoding="utf-8")
    return load_capability_matrix(path)


def _load_full_matrix(tmp_path, extra_provider_block: str, extra_credentials: str):
    """Write a yahoo + one extra provider matrix (plus credentials) and load it.

    The base ``yahoo`` entry keeps the ``providers`` list non-empty and gives a
    stable ticker→yahoo route; ``extra_provider_block`` is the entry under test
    (e.g. a bloomberg entry toggling ``enabled``), and ``extra_credentials`` its
    policy line — both providers must declare a policy (ADR-0095 §2).
    """
    yaml_text = (
        "providers:\n"
        "  - name: yahoo\n"
        "    priority: 100\n"
        "    schemes: [ticker]\n"
        "    kinds: [nav_price, dividend]\n"
        + extra_provider_block
        + "credentials:\n"
        + "  yahoo: none\n"
        + extra_credentials
    )
    path = tmp_path / "full.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_capability_matrix(path)
