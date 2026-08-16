# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider taxonomy v1 — the ADR-0112 §3 table as a frozen declaration.

The ``provider`` and ``key`` columns of ``scoped_settings`` are validated
**in code** against this table rather than by a CHECK constraint (ADR-0112
§3): the provider set grows with adapters, and a CHECK would force a
migration per adapter without buying a second source of truth worth
having. This module *is* that source of truth for the field set; it is
read by the F2 resolver's vault sources and by the F3 admin write path.

**Two kinds of field, two different chaining rules.** A provider's
*secret* fields are its **credential**, and the credential chains as a
unit: ADR-0112 §1's completeness and no-cross-scope-mixing rules apply to
all of a provider's secret fields together — either one scope holds every
one of them, or that scope declines as a whole and resolution falls
through to the next scope. A provider's *config* fields
(``openrouter.model``, ``base_url``, …) are **not** part of the
credential: each is an independently chained setting resolved along its
own annex chain by its consumer. F2 therefore reads secret fields only;
the config half is declared here so F3 can validate writes against it and
F4 can chain it per setting.

**Policy has exactly one source per provider.** Market-data providers
carry ``managed_by_matrix=True`` and their resolution policy
(``env_fallback``, ``optional``) stays authoritative in
``config/market_data_capabilities.yaml`` (ADR-0095 §2, unchanged by
ADR-0112) — the two policy flags here are ``None`` for them, so the
declaration cannot drift from the matrix. For every other provider this
taxonomy *is* the policy source and both flags are set.
``tests/services/credential_vault/test_taxonomy.py`` machine-enforces the
split in both directions.

Deliberately absent, per ADR-0112 §3: the future credentialed market-data
adapters (``bloomberg_serverapi``, ``preqin``, …) — those are declared by
their own adapter ADRs when the adapters land, not aspirationally here.
The voice providers were absent from v1 for the same reason and are absent
no longer: ADR-0118 declares them, as the annex amendment ADR-0112 §3
anticipated when it pinned them application-scope/env and called the table
"taxonomy-extensible later". ADR-0123 is the second such amendment and the
smallest kind the table admits — one new ``openrouter`` config field,
``scraper_model``, so the Report Scraper's model is chosen where every other
model is chosen. No new provider, no new secret, no migration: a
``scoped_settings`` row is ``(scope, provider, key)``, which is exactly the
extensibility ADR-0112 §3 built this table for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from services.market_data.provider import UnsupportedCapabilityError

#: The closed scope vocabulary (ADR-0112 §1). Mirrors the schema's
#: ``ck_scoped_settings_scope_vocabulary`` CHECK and the repository's
#: ``_VALID_SCOPES``; the guard suite pins the three against each other so
#: the mirror cannot rot (this module must not import the repository — it
#: would drag SQLAlchemy into the vault package).
SCOPES: frozenset[str] = frozenset({"application", "tenant", "user"})

_TENANT: frozenset[str] = frozenset({"tenant"})
_USER: frozenset[str] = frozenset({"user"})
_USER_OR_TENANT: frozenset[str] = frozenset({"user", "tenant"})


@dataclass(frozen=True)
class ProviderField:
    """One field of one provider — a single ``scoped_settings`` row's identity.

    Attributes:
        name: The field name, i.e. the row's ``key`` (``api_key``,
            ``model``, ``bot_token``, …).
        is_secret: Whether the field is a credential secret (encrypted,
            ``value_ciphertext``) rather than a config value
            (``value_plain``). Secret fields chain as a unit; config
            fields chain individually.
        scopes: The scopes this field may be **written** at. Read-side,
            a scope that a provider's secret field does not declare can
            never satisfy the §1 completeness rule for that provider, so
            the resolver's source for that scope declines before it
            queries.

    Raises:
        ValueError: If ``scopes`` is empty, names a scope outside
            :data:`SCOPES`, or lets a secret field live at application
            scope — application secrets live in the environment (§1),
            never in ``scoped_settings``.
    """

    name: str
    is_secret: bool
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.scopes:
            raise ValueError(f"Field {self.name!r} declares no scope.")
        unknown = self.scopes - SCOPES
        if unknown:
            raise ValueError(
                f"Field {self.name!r} declares unknown scope(s) {sorted(unknown)}; "
                f"expected a subset of {sorted(SCOPES)}."
            )
        if self.is_secret and "application" in self.scopes:
            raise ValueError(
                f"Secret field {self.name!r} may not declare the application "
                "scope — application-scope secrets live in the environment "
                "(ADR-0112 §1), not in scoped_settings."
            )


@dataclass(frozen=True)
class ProviderDeclaration:
    """One provider's declared fields and — where it owns it — its policy.

    Attributes:
        provider: The taxonomy key, i.e. the row's ``provider``.
        fields: Every declared field, secret and config alike.
        managed_by_matrix: Whether the market-data capability matrix owns
            this provider's resolution policy (ADR-0095 §2). When
            ``True``, ``env_fallback`` and ``optional`` are ``None`` here
            and the matrix is read instead.
        env_fallback: Whether an environment-sourced credential may serve
            this provider. ``None`` iff ``managed_by_matrix``.
        optional: Whether an absent credential is tolerated without error.
            ``None`` iff ``managed_by_matrix``.

    Raises:
        ValueError: If the policy flags do not follow ``managed_by_matrix``,
            or two fields share a name.
    """

    provider: str
    fields: tuple[ProviderField, ...]
    managed_by_matrix: bool
    env_fallback: bool | None = None
    optional: bool | None = None

    def __post_init__(self) -> None:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"Provider {self.provider!r} declares a duplicate field name.")
        policy_declared = (self.env_fallback is not None, self.optional is not None)
        if self.managed_by_matrix and any(policy_declared):
            raise ValueError(
                f"Provider {self.provider!r} is matrix-managed, so its resolution "
                "policy belongs to config/market_data_capabilities.yaml (ADR-0095 "
                "§2) and both policy flags must be None here."
            )
        if not self.managed_by_matrix and not all(policy_declared):
            raise ValueError(
                f"Provider {self.provider!r} is not matrix-managed, so this "
                "taxonomy is its policy source and must declare both "
                "env_fallback and optional."
            )

    @property
    def secret_fields(self) -> tuple[ProviderField, ...]:
        """The provider's credential — the secret fields, chained as a unit."""
        return tuple(field for field in self.fields if field.is_secret)

    @property
    def config_fields(self) -> tuple[ProviderField, ...]:
        """The provider's config fields — each chained independently."""
        return tuple(field for field in self.fields if not field.is_secret)

    def field(self, name: str) -> ProviderField | None:
        """Return the declared field called ``name``, or ``None``."""
        for field in self.fields:
            if field.name == name:
                return field
        return None


#: The ADR-0112 §3 v1 table. Adding a provider or changing a field's scopes
#: is an annex amendment (ADR-0112 §1), never an ad-hoc code decision.
_V1_DECLARATIONS: tuple[ProviderDeclaration, ...] = (
    # Market data. The one live credentialed consumer today; policy stays in
    # the capability matrix (env_fallback: allowed, optional: true).
    ProviderDeclaration(
        provider="openfigi",
        fields=(ProviderField(name="api_key", is_secret=True, scopes=_TENANT),),
        managed_by_matrix=True,
    ),
    # LLM. The user-scope key is carried by the model with no UI in v1
    # (ADR-0112 §3, §7) — the scope is declared so F3/F4 need no schema or
    # taxonomy change to expose it.
    ProviderDeclaration(
        provider="openrouter",
        fields=(
            ProviderField(name="api_key", is_secret=True, scopes=_USER_OR_TENANT),
            ProviderField(name="model", is_secret=False, scopes=_USER_OR_TENANT),
            # ``scraper_model`` (ADR-0123) is tenant-only, mirroring
            # ``irene_model``: the Report Scraper is a tenant tool, not a
            # personal one, and a user-scope model would only widen the
            # surface on which a non-PDF-capable model can be chosen. It
            # sits between the two other model fields so the Admin card
            # renders its three model rows as one block (Shirley model →
            # Report Scraper model → Watch Desk model → Base URL). Field
            # order here is presentational only; nothing chains on it.
            ProviderField(name="scraper_model", is_secret=False, scopes=_TENANT),
            ProviderField(name="irene_model", is_secret=False, scopes=_TENANT),
            ProviderField(name="base_url", is_secret=False, scopes=_TENANT),
        ),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    ),
    # Telegram. Declared for F3/F5 (ADR-0112 §5); no F2 consumer. The token is
    # the tenant's bot, the chat binding is the user's pairing result.
    ProviderDeclaration(
        provider="telegram",
        fields=(
            ProviderField(name="bot_token", is_secret=True, scopes=_TENANT),
            ProviderField(name="chat_id", is_secret=False, scopes=_USER),
            ProviderField(name="enabled", is_secret=False, scopes=_TENANT),
        ),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    ),
    # Voice (ADR-0118, annex amendment to ADR-0112 §3). Three declarations:
    # the STT/TTS halves are separate providers so §1's unit-chaining rule
    # cannot force both keys into one scope (Groq STT + OpenAI TTS with
    # different owners stays possible), and the service-level switch lives on
    # a third, config-only declaration — a credential with an empty secret
    # set is legal, and one switch on one card cannot diverge or lie about
    # what it gates. ``stt_provider`` / ``tts_provider`` stay env-only until
    # a second adapter's ADR declares them.
    ProviderDeclaration(
        provider="voice",
        fields=(ProviderField(name="enabled", is_secret=False, scopes=_TENANT),),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    ),
    ProviderDeclaration(
        provider="voice_stt",
        fields=(
            ProviderField(name="api_key", is_secret=True, scopes=_TENANT),
            ProviderField(name="model", is_secret=False, scopes=_TENANT),
            ProviderField(name="base_url", is_secret=False, scopes=_TENANT),
        ),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    ),
    ProviderDeclaration(
        provider="voice_tts",
        fields=(
            ProviderField(name="api_key", is_secret=True, scopes=_TENANT),
            ProviderField(name="model", is_secret=False, scopes=_TENANT),
            ProviderField(name="voice", is_secret=False, scopes=_TENANT),
        ),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    ),
)

#: Provider key → declaration. Read-only; the resolver and the F3 write path
#: both look up through it (or through an injected double, in tests).
PROVIDER_TAXONOMY: Mapping[str, ProviderDeclaration] = MappingProxyType(
    {declaration.provider: declaration for declaration in _V1_DECLARATIONS}
)


def is_declared(
    provider: str,
    taxonomy: Mapping[str, ProviderDeclaration] | None = None,
) -> bool:
    """Return whether ``provider`` has a declaration.

    Args:
        provider: The provider key.
        taxonomy: The table to look in. Defaults to
            :data:`PROVIDER_TAXONOMY`; injectable for tests.

    Returns:
        ``True`` if the provider is declared, ``False`` otherwise.
    """
    return provider in (taxonomy if taxonomy is not None else PROVIDER_TAXONOMY)


def declaration_for(
    provider: str,
    taxonomy: Mapping[str, ProviderDeclaration] | None = None,
) -> ProviderDeclaration:
    """Return ``provider``'s declaration.

    Args:
        provider: The provider key.
        taxonomy: The table to look in. Defaults to
            :data:`PROVIDER_TAXONOMY`; injectable for tests.

    Returns:
        The :class:`ProviderDeclaration`.

    Raises:
        UnsupportedCapabilityError: If the provider is not declared — the
            same typed error the capability matrix raises for a provider
            it declares no policy for, so a caller of the single
            credential façade (ADR-0112 §4) catches one error type
            whichever half of the declaration was missing.
    """
    table = taxonomy if taxonomy is not None else PROVIDER_TAXONOMY
    declaration = table.get(provider)
    if declaration is None:
        raise UnsupportedCapabilityError(
            f"No taxonomy declaration for provider {provider!r} (ADR-0112 §3)."
        )
    return declaration


def secret_fields(
    provider: str,
    taxonomy: Mapping[str, ProviderDeclaration] | None = None,
) -> tuple[ProviderField, ...]:
    """Return ``provider``'s secret fields — its credential, chained as a unit.

    Args:
        provider: The provider key.
        taxonomy: The table to look in. Defaults to
            :data:`PROVIDER_TAXONOMY`; injectable for tests.

    Returns:
        The declared secret fields, possibly empty.

    Raises:
        UnsupportedCapabilityError: If the provider is not declared.
    """
    return declaration_for(provider, taxonomy).secret_fields


__all__ = [
    "PROVIDER_TAXONOMY",
    "SCOPES",
    "ProviderDeclaration",
    "ProviderField",
    "declaration_for",
    "is_declared",
    "secret_fields",
]
