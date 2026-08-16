# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Capability-matrix-driven resolution of market-data adapters (ADR-0091).

The factory reads the declarative capability matrix
(``config/market_data_capabilities.yaml``) and answers one question: *which
adapter serves this ``(scheme, kind)`` request?* Adding a provider is one
adapter file plus one matrix entry — the factory picks it up with no change
here beyond registering its builder. This mirrors ``services/voice/factory.py``
(no silent fallback: an unroutable request raises).

Config layering follows ADR-0091 §"Factory & config layering":

- **Wiring & capability** — which provider serves which scheme/kind, and in
  what priority — is the versioned matrix fixture, validated at load.
- **Secrets & concrete paths** — an API key, the synthetic fixture path —
  come from the environment, never the matrix and never the DB.

Routing is by **descending priority**: providers are tried highest-priority
first, and the first whose ``schemes`` and ``kinds`` both cover the request
wins. A provider declared ``routing: forced_only`` — the ``synthetic``
test-event provider — is excluded from this unforced path entirely: it is
reachable **only** through the explicit forced-provider path (``--provider``).
An unforced request that no real provider serves therefore raises
``UnsupportedCapabilityError`` rather than falling through to synthetic
(whose adapter build would then fail when its fixture is unset). Coverage
(``serves``) stays routing-blind, so the forced path still honours the
matrix's declaration for a ``forced_only`` provider.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

import yaml

from services.market_data.adapters.bloomberg import (
    BloombergAdapter,
    BlpapiDesktopGateway,
)
from services.market_data.adapters.synthetic import SyntheticAdapter
from services.market_data.adapters.yahoo import YahooAdapter
from services.market_data.dto import IDENTIFIER_SCHEMES, SeriesKind
from services.market_data.provider import (
    MarketDataConfigurationError,
    MarketDataProvider,
    UnsupportedCapabilityError,
)

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_DEFAULT_MATRIX_PATH: Path = _REPO_ROOT / "config" / "market_data_capabilities.yaml"

#: Environment variable naming the synthetic adapter's fixture file. Kept out
#: of the matrix (it is a concrete path, not capability wiring) per ADR-0091.
_SYNTHETIC_FIXTURE_ENV: str = "MARKET_DATA_SYNTHETIC_FIXTURE"

#: Bloomberg Desktop-API connection settings — the local Terminal's host/port.
#: Read here (not from the matrix, not from the DB) exactly like the synthetic
#: fixture path: they are concrete connection settings, not capability wiring
#: (ADR-0091 §"Factory & config layering"). The adapter stays
#: credential-source-blind (ADR-0095 §1); the Terminal session is the auth
#: boundary, so the Desktop-API variant declares ``credentials: none``.
_BLPAPI_HOST_ENV: str = "BLPAPI_HOST"
_BLPAPI_PORT_ENV: str = "BLPAPI_PORT"
_BLPAPI_HOST_DEFAULT: str = "localhost"
_BLPAPI_PORT_DEFAULT: int = 8194

#: The keys a ``providers`` entry may carry. Any other key fails validation —
#: the same strictness the ``credentials`` section applies, so a typo'd or
#: aspirational key cannot slip in unnoticed.
_ALLOWED_PROVIDER_KEYS: frozenset[str] = frozenset(
    {"name", "priority", "schemes", "kinds", "enabled", "routing"}
)

#: The values the optional per-provider ``routing`` key may take. ``normal``
#: (the default when the key is absent) participates in unforced priority
#: routing; ``forced_only`` is excluded from it and reachable only through the
#: ``--provider`` forced path. Any other value fails validation loudly.
_ALLOWED_ROUTING_VALUES: frozenset[str] = frozenset({"normal", "forced_only"})


@dataclass(frozen=True)
class ProviderCapability:
    """One provider's declared coverage, parsed from the matrix.

    Attributes:
        name: The provider key (has a builder in :data:`_ADAPTER_BUILDERS`).
        priority: Routing priority; higher is tried first. Immaterial for a
            ``forced_only`` provider, which the unforced path never consults.
        schemes: The identifier schemes this provider serves (⊆
            :data:`~services.market_data.dto.IDENTIFIER_SCHEMES`).
        kinds: The :class:`SeriesKind`s this provider serves.
        routing: The routing policy — ``normal`` (participates in unforced
            priority routing) or ``forced_only`` (excluded from it; reachable
            only through the ``--provider`` forced path). Defaults to
            ``normal`` when the matrix entry omits the key.
    """

    name: str
    priority: int
    schemes: frozenset[str]
    kinds: frozenset[SeriesKind]
    routing: str = "normal"

    @property
    def forced_only(self) -> bool:
        """Whether this provider is excluded from unforced priority routing."""
        return self.routing == "forced_only"

    def serves(self, scheme: str, kind: SeriesKind) -> bool:
        """Return whether this provider covers ``(scheme, kind)``.

        Pure coverage — deliberately routing-blind, so the forced-provider
        path can still resolve a ``forced_only`` provider against the matrix's
        declaration. The unforced :meth:`CapabilityMatrix.resolve` is the sole
        place the ``forced_only`` exclusion is applied.
        """
        return scheme in self.schemes and kind in self.kinds


@dataclass(frozen=True)
class CredentialPolicy:
    """A provider's credential-source policy (ADR-0095 §2).

    Parsed from the matrix's ``credentials`` section — versioned, reviewable
    capability metadata that sits next to the coverage declaration. It is a
    fixed invariant per provider class, **not** a deployment knob (the same
    posture as Excel precedence, ADR-0092): a deployment may not loosen a
    ``forbidden`` policy to serve tenant-licensed data from a global key.

    The factory only *parses* this declaration; it never resolves or injects a
    credential — that is the :class:`CredentialResolver`'s job in
    ``services/investments/`` (ADR-0095 §1), keeping the adapters
    credential-source-blind and this layer DB-free.

    Attributes:
        provider: The provider key this policy governs.
        requires: Whether the provider takes any credentials at all. ``False``
            for a provider declared ``none``.
        env_fallback: Whether an environment-sourced credential may serve this
            provider (Stage 1's only source, ADR-0095 §3). Meaningful only when
            ``requires`` is ``True``.
        optional: Whether an absent credential is tolerated without error (the
            provider works without one, e.g. OpenFIGI keyless). Meaningful only
            when ``requires`` is ``True``.
    """

    provider: str
    requires: bool
    env_fallback: bool
    optional: bool

    @classmethod
    def none(cls, provider: str) -> CredentialPolicy:
        """Return the policy for a provider that takes no credentials."""
        return cls(
            provider=provider,
            requires=False,
            env_fallback=False,
            optional=True,
        )


@dataclass(frozen=True)
class CapabilityMatrix:
    """The parsed, validated capability matrix.

    Attributes:
        providers: Provider capabilities, sorted by descending priority so the
            first match in iteration order is the preferred one.
        credential_policies: Per-provider credential-source policy (ADR-0095
            §2), keyed by provider name. Includes non-routing providers (e.g.
            ``openfigi``) that appear only in the ``credentials`` section.
            Excluded from equality/hash so the matrix stays hashable.
    """

    providers: tuple[ProviderCapability, ...]
    credential_policies: Mapping[str, CredentialPolicy] = field(default_factory=dict, compare=False)

    def credential_policy(self, provider: str) -> CredentialPolicy:
        """Return the credential policy declared for ``provider``.

        Args:
            provider: The provider key.

        Returns:
            The declared :class:`CredentialPolicy`.

        Raises:
            UnsupportedCapabilityError: If the matrix declares no policy for the
                provider — a resolver must never guess a credential source.
        """
        policy = self.credential_policies.get(provider)
        if policy is None:
            raise UnsupportedCapabilityError(
                f"No credential policy declared for provider {provider!r} in the capability matrix."
            )
        return policy

    def resolve(self, scheme: str, kind: SeriesKind) -> ProviderCapability:
        """Return the highest-priority provider serving ``(scheme, kind)``.

        This is the **unforced** routing path. A provider declared
        ``routing: forced_only`` (the ``synthetic`` test-event provider) is
        skipped entirely, so a request only it could cover raises rather than
        routing to it — the forced ``--provider`` path is the only way to
        reach a ``forced_only`` provider.

        Args:
            scheme: The identifier scheme requested.
            kind: The :class:`SeriesKind` requested.

        Returns:
            The winning :class:`ProviderCapability`.

        Raises:
            UnsupportedCapabilityError: If no non-``forced_only`` provider
                covers the request — the explicit non-availability of ADR-0091
                property 2.
        """
        for provider in self.providers:
            if provider.forced_only:
                continue
            if provider.serves(scheme, kind):
                return provider
        raise UnsupportedCapabilityError(
            f"No market-data provider serves scheme={scheme!r} kind={kind.value!r}."
        )


def _build_yahoo() -> MarketDataProvider:
    """Build the Yahoo adapter (keyless, no configuration needed)."""
    return YahooAdapter()


def _build_synthetic() -> MarketDataProvider:
    """Build the synthetic adapter from the env-configured fixture path.

    Raises:
        MarketDataConfigurationError: If :data:`_SYNTHETIC_FIXTURE_ENV` is
            unset — the synthetic provider cannot run without a fixture.
    """
    fixture_path = os.getenv(_SYNTHETIC_FIXTURE_ENV)
    if not fixture_path:
        raise MarketDataConfigurationError(
            f"{_SYNTHETIC_FIXTURE_ENV} is unset; the synthetic market-data "
            "provider requires a fixture path."
        )
    return SyntheticAdapter(Path(fixture_path))


def _build_bloomberg() -> MarketDataProvider:
    """Build the Bloomberg Desktop-API adapter from env connection settings.

    Reads ``BLPAPI_HOST`` / ``BLPAPI_PORT`` from the environment (defaults
    ``localhost`` / ``8194``) — the synthetic-fixture-path precedent. The real
    gateway imports ``blpapi`` **lazily**, so building this adapter on a machine
    without ``blpapi`` does NOT raise; the
    :class:`MarketDataConfigurationError` surfaces only when a bloomberg fetch is
    actually attempted.

    Raises:
        MarketDataConfigurationError: If ``BLPAPI_PORT`` is set to a
            non-integer value.
    """
    host = os.getenv(_BLPAPI_HOST_ENV) or _BLPAPI_HOST_DEFAULT
    gateway = BlpapiDesktopGateway(host=host, port=_read_blpapi_port())
    return BloombergAdapter(gateway=gateway)


def _read_blpapi_port() -> int:
    """Return the Bloomberg port from the env, or the Desktop-API default.

    Raises:
        MarketDataConfigurationError: If the env var is set but not an integer.
    """
    raw = os.getenv(_BLPAPI_PORT_ENV)
    if raw is None or not raw.strip():
        return _BLPAPI_PORT_DEFAULT
    try:
        return int(raw)
    except ValueError as exc:
        raise MarketDataConfigurationError(
            f"{_BLPAPI_PORT_ENV}={raw!r} is not a valid integer port."
        ) from exc


#: Provider key → adapter builder. A matrix entry naming a provider absent
#: from this map fails matrix validation — the guard against aspirational
#: provider entries.
_ADAPTER_BUILDERS: dict[str, Callable[[], MarketDataProvider]] = {
    "yahoo": _build_yahoo,
    "synthetic": _build_synthetic,
    "bloomberg": _build_bloomberg,
}


def load_capability_matrix(path: Path | None = None) -> CapabilityMatrix:
    """Load and validate the capability matrix.

    Every entry is validated: the provider name has a registered builder,
    ``priority`` is an int, every scheme is in :data:`IDENTIFIER_SCHEMES`, and
    every kind is a known :class:`SeriesKind`. An invalid matrix fails loudly
    here rather than at routing time.

    Args:
        path: Matrix file to load. Defaults to
            ``<repo_root>/config/market_data_capabilities.yaml``.

    Returns:
        A :class:`CapabilityMatrix` with providers sorted by descending
        priority.

    Raises:
        MarketDataConfigurationError: If the file is missing, unparseable, or
            structurally invalid.
    """
    src = path or _DEFAULT_MATRIX_PATH
    if not src.exists():
        raise MarketDataConfigurationError(f"Capability matrix not found at {src}.")
    try:
        raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict) or "providers" not in raw:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} must be a mapping with a 'providers' key."
        )
    entries = raw["providers"]
    if not isinstance(entries, list) or not entries:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} 'providers' must be a non-empty list."
        )

    parsed: list[tuple[ProviderCapability, bool]] = []
    seen_names: set[str] = set()
    for entry in entries:
        parsed.append(_parse_entry(entry, src))
    for capability, _enabled in parsed:
        if capability.name in seen_names:
            raise MarketDataConfigurationError(
                f"Capability matrix at {src} lists provider {capability.name!r} more than once."
            )
        seen_names.add(capability.name)

    credential_policies = _parse_credential_policies(raw, src)
    # Every DECLARED provider (enabled or not) states a credential policy — a
    # disabled entry still declares its posture (§0.3), so the policy check is
    # over all declared names, not only the routed ones.
    _require_policy_for_each_provider(seen_names, credential_policies, src)

    # `enabled: false` means "not declared" for routing: the factory skips a
    # disabled provider entirely (§0.2). It is still fully validated above (name
    # has a builder, schemes/kinds are known), so a typo in a disabled entry is
    # still caught — it just does not participate in routing.
    routed = [cap for cap, enabled in parsed if enabled]
    routed.sort(key=lambda cap: cap.priority, reverse=True)
    return CapabilityMatrix(
        providers=tuple(routed),
        credential_policies=credential_policies,
    )


def _parse_credential_policies(raw: dict, src: Path) -> dict[str, CredentialPolicy]:
    """Parse and validate the matrix's ``credentials`` section (ADR-0095 §2).

    Each key is a provider name; each value is either the literal ``none`` (the
    provider takes no credentials) or a mapping declaring ``env_fallback``
    (``allowed`` / ``forbidden``) and an optional boolean ``optional``.
    Non-series providers (e.g. ``openfigi``) may appear here without a routing
    entry — the credential policy is orthogonal to series routing.

    Raises:
        MarketDataConfigurationError: If the section is missing, not a non-empty
            mapping, or declares an unknown policy shape/value.
    """
    section = raw.get("credentials")
    if not isinstance(section, dict) or not section:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} must declare a non-empty "
            "'credentials' mapping (ADR-0095 §2)."
        )
    policies: dict[str, CredentialPolicy] = {}
    for name, value in section.items():
        if not isinstance(name, str) or not name:
            raise MarketDataConfigurationError(
                f"Capability matrix at {src} 'credentials' has a non-string provider key: {name!r}."
            )
        policies[name] = _parse_one_credential_policy(name, value, src)
    return policies


def _parse_one_credential_policy(name: str, value: object, src: Path) -> CredentialPolicy:
    """Parse one ``credentials`` entry into a :class:`CredentialPolicy`.

    Raises:
        MarketDataConfigurationError: On an unknown policy shape or value — the
            schema-sanity guard the resolver relies on (no silent defaults).
    """
    if value == "none":
        return CredentialPolicy.none(name)
    if not isinstance(value, dict):
        raise MarketDataConfigurationError(
            f"Credential policy for provider {name!r} in {src} must be 'none' "
            f"or a mapping; got {value!r}."
        )
    unknown = set(value) - {"env_fallback", "optional"}
    if unknown:
        raise MarketDataConfigurationError(
            f"Credential policy for provider {name!r} in {src} has unknown "
            f"keys: {sorted(unknown)} (allowed: env_fallback, optional)."
        )
    env_fallback = value.get("env_fallback")
    if env_fallback not in ("allowed", "forbidden"):
        raise MarketDataConfigurationError(
            f"Credential policy for provider {name!r} in {src} must declare "
            f"env_fallback as 'allowed' or 'forbidden'; got {env_fallback!r}."
        )
    optional = value.get("optional", False)
    if not isinstance(optional, bool):
        raise MarketDataConfigurationError(
            f"Credential policy 'optional' for provider {name!r} in {src} must "
            f"be a boolean; got {optional!r}."
        )
    return CredentialPolicy(
        provider=name,
        requires=True,
        env_fallback=(env_fallback == "allowed"),
        optional=optional,
    )


def _require_policy_for_each_provider(
    provider_names: set[str],
    policies: Mapping[str, CredentialPolicy],
    src: Path,
) -> None:
    """Ensure every routing provider declares a credential policy.

    A routing provider without a policy would leave the resolver unable to
    decide its credential source — a matrix error, caught at load rather than
    at resolution.

    Raises:
        MarketDataConfigurationError: If a routing provider has no policy.
    """
    missing = sorted(provider_names - set(policies))
    if missing:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src}: routing providers {missing} have no "
            "credential policy in the 'credentials' section (ADR-0095 §2)."
        )


def _parse_entry(entry: object, src: Path) -> tuple[ProviderCapability, bool]:
    """Parse and validate one ``providers`` entry.

    Returns:
        The parsed :class:`ProviderCapability` and its ``enabled`` flag
        (defaulting to ``True`` when the key is absent). A disabled provider is
        still fully validated; the caller drops it from routing.

    Raises:
        MarketDataConfigurationError: On any structural or referential error
            (unknown provider, unknown scheme, unknown kind, unknown key, bad
            types).
    """
    if not isinstance(entry, dict):
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} has a non-mapping provider entry: {entry!r}."
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} has a provider entry with no name."
        )
    unknown = set(entry) - _ALLOWED_PROVIDER_KEYS
    if unknown:
        raise MarketDataConfigurationError(
            f"Provider {name!r} in {src} has unknown keys: {sorted(unknown)} "
            f"(allowed: {sorted(_ALLOWED_PROVIDER_KEYS)})."
        )
    if name not in _ADAPTER_BUILDERS:
        raise MarketDataConfigurationError(
            f"Capability matrix at {src} names provider {name!r}, which has "
            f"no adapter builder (known: {sorted(_ADAPTER_BUILDERS)})."
        )

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MarketDataConfigurationError(
            f"Provider {name!r} in {src} 'enabled' must be a boolean; got {enabled!r}."
        )

    priority = entry.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise MarketDataConfigurationError(
            f"Provider {name!r} in {src} must declare an integer 'priority'."
        )

    schemes_raw = entry.get("schemes")
    if not isinstance(schemes_raw, list) or not schemes_raw:
        raise MarketDataConfigurationError(
            f"Provider {name!r} in {src} must declare a non-empty 'schemes'."
        )
    schemes: set[str] = set()
    for scheme in schemes_raw:
        if scheme not in IDENTIFIER_SCHEMES:
            raise MarketDataConfigurationError(
                f"Provider {name!r} in {src} declares unknown scheme "
                f"{scheme!r}; expected one of {sorted(IDENTIFIER_SCHEMES)}."
            )
        schemes.add(scheme)

    kinds_raw = entry.get("kinds")
    if not isinstance(kinds_raw, list) or not kinds_raw:
        raise MarketDataConfigurationError(
            f"Provider {name!r} in {src} must declare a non-empty 'kinds'."
        )
    kinds: set[SeriesKind] = set()
    for kind in kinds_raw:
        try:
            kinds.add(SeriesKind(kind))
        except ValueError as exc:
            raise MarketDataConfigurationError(
                f"Provider {name!r} in {src} declares unknown kind {kind!r}."
            ) from exc

    routing = entry.get("routing", "normal")
    if routing not in _ALLOWED_ROUTING_VALUES:
        raise MarketDataConfigurationError(
            f"Provider {name!r} in {src} declares unknown routing {routing!r}; "
            f"expected one of {sorted(_ALLOWED_ROUTING_VALUES)}."
        )

    capability = ProviderCapability(
        name=name,
        priority=priority,
        schemes=frozenset(schemes),
        kinds=frozenset(kinds),
        routing=routing,
    )
    return capability, enabled


_matrix_cache: CapabilityMatrix | None = None


def get_capability_matrix() -> CapabilityMatrix:
    """Return the process-wide capability matrix, loaded once and cached."""
    global _matrix_cache
    if _matrix_cache is None:
        _matrix_cache = load_capability_matrix()
    return _matrix_cache


def resolve_provider_name(
    scheme: str, kind: SeriesKind, *, matrix: CapabilityMatrix | None = None
) -> str:
    """Return the name of the provider that serves ``(scheme, kind)``.

    Pure routing — no adapter is constructed, so this needs neither the
    environment nor the network. Used by tests to assert priority and
    unsupported-capability behaviour independently of instantiation.

    Args:
        scheme: The identifier scheme requested.
        kind: The :class:`SeriesKind` requested.
        matrix: Matrix to resolve against; defaults to the cached one.

    Returns:
        The winning provider name.

    Raises:
        UnsupportedCapabilityError: If no provider covers the request.
    """
    resolved = (matrix or get_capability_matrix()).resolve(scheme, kind)
    return resolved.name


def build_adapter(name: str) -> MarketDataProvider:
    """Construct the adapter for ``name``.

    Args:
        name: A provider key present in :data:`_ADAPTER_BUILDERS`.

    Returns:
        A ready :class:`MarketDataProvider` adapter.

    Raises:
        UnsupportedCapabilityError: If ``name`` has no registered builder.
        MarketDataConfigurationError: If the adapter's own configuration is
            missing (e.g. the synthetic fixture path).
    """
    builder = _ADAPTER_BUILDERS.get(name)
    if builder is None:
        raise UnsupportedCapabilityError(f"No adapter builder registered for provider {name!r}.")
    return builder()


def get_provider(
    scheme: str, kind: SeriesKind, *, matrix: CapabilityMatrix | None = None
) -> MarketDataProvider:
    """Resolve and construct the adapter serving ``(scheme, kind)``.

    Args:
        scheme: The identifier scheme requested.
        kind: The :class:`SeriesKind` requested.
        matrix: Matrix to route with; defaults to the cached one.

    Returns:
        The constructed :class:`MarketDataProvider`.

    Raises:
        UnsupportedCapabilityError: If nothing serves the request.
        MarketDataConfigurationError: If the resolved adapter is
            misconfigured.
    """
    return build_adapter(resolve_provider_name(scheme, kind, matrix=matrix))
