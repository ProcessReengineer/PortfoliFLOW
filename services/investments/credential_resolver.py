# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider-credential resolver — the single credential façade (ADR-0112 §4).

Resolves, per ``(tenant, user, provider)``, an opaque credential payload and
hands **plain values** to its consumers — the market-data factory/adapters
today, LLM and Telegram from F4/F5 on. Consumers never learn where a credential
came from — that source-blindness is the whole point (ADR-0095 §1): it keeps
``services/market_data/`` DB-free (its purity guard,
``tests/regression/test_market_data_layer_pure.py``) while this resolver, one
layer up in ``services/investments/`` (parallel to :mod:`live_refresh`,
**never** inside ``services/market_data/``), is free to read ``os.environ`` and
the ``scoped_settings`` credential vault.

Resolution order (ADR-0095 §1 as generalised by ADR-0112 §1), evaluated per
provider:

1. **Vault, user scope** — a ``scoped_settings`` user-scope row set, read
   through :class:`~core.repositories.scoped_setting_repository.ScopedSettingRepository`
   inside the caller's ``tenant_context``. Only consulted when a ``user_id`` is
   given.
2. **Vault, tenant scope** — the same, for the tenant's own rows.
3. **Environment fallback** — the application scope; only where the provider's
   fallback policy allows it (ADR-0095 §2 for market-data providers, the
   taxonomy for the rest).
4. **Explicit failure** — a typed :class:`CredentialUnavailableError`; never a
   silent skip, never a silent global-key substitution.

The two vault sources exist only when the resolver is constructed **with a
session**. Without one the source list is the environment alone — the Stage-1
behaviour, unchanged — which is what keeps a DB-free caller (a CLI probe, a
pure test) working exactly as before.

**Completeness and no cross-scope mixing (ADR-0112 §1).** A provider's *secret*
fields are its credential and chain **as a unit**: a scope source assembles a
payload only from its own rows, and only when every declared secret field is
present there. One field in the tenant scope and the other in the environment
never combine — the tenant source declines as a whole and resolution falls
through. Config fields (``openrouter.model``, ``base_url``, …) are *not* part of
the credential: each is an independently chained setting, resolved one at a time
by :meth:`CredentialResolver.resolve_config` (ADR-0112 §4b). The two methods are
the two halves of the one façade — :meth:`~CredentialResolver.resolve` chains
secrets as a unit and fails loudly, :meth:`~CredentialResolver.resolve_config`
chains one config field and returns ``None`` when it is set nowhere, leaving the
default to the consumer that knows what a sensible one is.

**A wrong key is loud.** A :class:`~services.credential_vault.VaultDecryptError`
propagates out of :meth:`CredentialResolver.resolve` rather than falling through
to the environment: ciphertext that will not decrypt means a mis-rotated or
mis-restored vault, an operator problem that a silent env fallback would hide.
An *unconfigured* vault is the other case — one WARNING per resolver instance,
then the vault sources decline and the environment serves (ADR-0112 §2).

Three outcomes (ADR-0095 §1):

- a credential is found → :class:`ProviderCredential` (opaque; its ``repr`` and
  log lines mask the values, ADR-0095 §4 — the payload is never logged);
- the provider needs none, or an *optional* credential is simply absent → an
  explicit :class:`NoCredential` result (**not** an error). OpenFIGI is the
  optional case: a missing key means keyless at a lower rate limit, not a
  failure;
- the policy forbids the environment fallback and no vault scope can serve it
  → :class:`CredentialUnavailableError`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import PortfoliFlowError
from core.repositories.scoped_setting_repository import ScopedSettingRepository
from services.credential_vault import (
    MASTER_KEY_ENV_VAR,
    PROVIDER_TAXONOMY,
    ProviderDeclaration,
    ProviderField,
    VaultCipher,
    VaultDecryptError,
    declaration_for,
    is_vault_configured,
)
from services.market_data.factory import (
    CapabilityMatrix,
    CredentialPolicy,
    get_capability_matrix,
)
from services.market_data.provider import UnsupportedCapabilityError

_LOG = logging.getLogger("portfoliflow.market_data.credentials")

#: Per-provider environment-variable mapping (ADR-0095 §3), declared in **one**
#: place: credential *field name* → environment variable name. This is the
#: application-scope source declaration; the *policy* (whether the environment
#: may serve a provider at all, and whether it is optional) lives in the
#: capability matrix for market-data providers and in the ADR-0112 §3 taxonomy
#: for the rest. Yahoo and synthetic take no credentials and are deliberately
#: absent here. A future multi-field credential (e.g. a Bloomberg DL
#: account/key pair) lists one prefixed variable per field; all declared fields
#: must be present for the environment source to yield a credential.
_ENV_CREDENTIAL_FIELDS: dict[str, dict[str, str]] = {
    "openfigi": {"api_key": "OPENFIGI_API_KEY"},
    # The LLM credential's application-scope link (ADR-0112 §4b). The
    # taxonomy has declared ``openrouter`` with ``env_fallback=True`` since
    # F2, but F2 shipped no LLM consumer, so nothing had yet asked the
    # environment for it. F4's consumers do — and without this entry the
    # declared fallback would be dead, taking every env-only deployment
    # (the single-tenant default) with it.
    "openrouter": {"api_key": "OPENROUTER_API_KEY"},
    # The Telegram bot token's application-scope link (ADR-0112 §5). The
    # multi-bot start path does *not* resolve through here — discovery is a
    # cross-tenant scan that runs before any tenant context exists, so it
    # reads the rows directly (``bot/token_discovery.py``) and applies the
    # same precedence itself. The entry exists so the declared
    # ``env_fallback=True`` is not a dead letter for any single-tenant
    # caller that does resolve one tenant's token through the façade, and so
    # TX-02 pins the field name against the taxonomy.
    "telegram": {"bot_token": "TELEGRAM_BOT_TOKEN"},
    # The voice credentials' application-scope links (ADR-0118 §1). Two
    # halves, two providers — see the taxonomy for why the split exists.
    "voice_stt": {"api_key": "VOICE_STT_API_KEY"},
    "voice_tts": {"api_key": "VOICE_TTS_API_KEY"},
}

#: The config-half twin of :data:`_ENV_CREDENTIAL_FIELDS` (ADR-0112 §4b):
#: config *field name* → environment variable name, per provider. Same shape,
#: same purpose — one declaration of the application-scope source — but read by
#: :meth:`CredentialResolver.resolve_config`, which chains each config field
#: **individually** rather than as a unit. A field absent here simply has no
#: environment link and its chain ends at the vault scopes.
#:
#: ``tests/services/credential_vault/test_taxonomy.py`` (TX-06) pins every entry
#: to a declared, non-secret field of a declared provider, so this table cannot
#: drift from the taxonomy.
_ENV_CONFIG_FIELDS: dict[str, dict[str, str]] = {
    "openrouter": {
        "model": "SHIRLEY_MODEL",
        "base_url": "OPENROUTER_BASE_URL",
        "irene_model": "IRENE_MODEL",
        "scraper_model": "SCRAPER_MODEL",
    },
    # ``telegram.enabled`` is the per-tenant opt-out switch; its
    # application-scope link is the master kill switch the bot thread
    # already reads at start (ADR-0112 §5). Declared here so a consumer
    # asking "is Telegram on for this tenant" through ``resolve_config``
    # walks the same chain as every other setting — tenant row first,
    # environment last — rather than inventing a second precedence.
    "telegram": {"enabled": "TELEGRAM_BOT_ENABLED"},
    # Voice config chains (ADR-0118 §1). ``voice.enabled`` is the service-
    # level switch — one chain gates STT and TTS together (tenant row →
    # VOICE_ENABLED → default off at the consumer). The per-half model /
    # endpoint / persona-voice settings chain individually, as every config
    # field does.
    "voice": {"enabled": "VOICE_ENABLED"},
    "voice_stt": {"model": "VOICE_STT_MODEL", "base_url": "VOICE_STT_BASE_URL"},
    "voice_tts": {"model": "VOICE_TTS_MODEL", "voice": "VOICE_TTS_VOICE"},
}

#: The config chain's source labels, in resolution order. ``resolve_config``'s
#: ``scopes`` argument is a subset of these; they double as the log line's
#: ``source=`` values.
_CONFIG_SOURCE_LABELS: tuple[str, ...] = ("user", "tenant", "env")


@dataclass(frozen=True)
class ProviderCredential:
    """An opaque, resolved credential for one provider.

    Callers read :attr:`payload` to inject the plain values, but the object is
    designed to be **inert in logs**: :func:`repr`/:func:`str` mask the values
    and the resolver never logs the payload (ADR-0095 §4). The payload is stored
    as a read-only mapping so it cannot be mutated after resolution.

    Attributes:
        provider: The provider this credential is for.
        payload: Credential field name → plain value (e.g.
            ``{"api_key": "..."}``). Read-only.
    """

    provider: str
    payload: Mapping[str, str]

    def __post_init__(self) -> None:
        # Freeze the payload into a read-only view; defend against later mutation
        # of a dict handed in by a caller.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def __repr__(self) -> str:
        # Never leak values: show only the field count. Applies to str() too.
        return (
            f"ProviderCredential(provider={self.provider!r}, "
            f"payload=<{len(self.payload)} field(s); values masked>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class NoCredential:
    """The explicit no-credential outcome — the provider proceeds without one.

    Returned when the provider declares ``none`` (takes no credentials) or when
    an *optional* credential is simply absent. Distinct from
    :class:`CredentialUnavailableError`, which is the hard failure a
    ``forbidden``-policy provider raises when no source can serve it.

    Attributes:
        provider: The provider this outcome is for.
    """

    provider: str


class CredentialUnavailableError(PortfoliFlowError):
    """No credential could be resolved for a provider that requires one.

    Raised when no vault scope holds the provider's complete credential and the
    environment cannot serve it either — because the policy forbids the
    fallback, or because the environment lacks a declared field. The refresh
    core surfaces this in the tick log and the ingest report (ADR-0095 §1)
    rather than silently skipping.
    """


class _CipherHolder:
    """One :class:`VaultCipher` per resolution, built on first actual decrypt.

    Both vault sources of a resolution share this holder, so a resolution that
    decrypts N fields across two scopes constructs one cipher — and a
    resolution that never reaches a secret row constructs none, which is why a
    malformed master key surfaces only when the vault is genuinely used.
    """

    __slots__ = ("_cipher",)

    def __init__(self) -> None:
        self._cipher: VaultCipher | None = None

    def get(self) -> VaultCipher:
        """Return the resolution's cipher, constructing it on first use."""
        if self._cipher is None:
            self._cipher = VaultCipher.from_env()
        return self._cipher


#: One credential source: given the provider, its policy, the resolving user
#: and the resolution's cipher holder, either yield a credential or decline
#: with ``None``. The env source ignores the last two.
_CredentialSource = Callable[
    [str, CredentialPolicy, "UUID | None", _CipherHolder],
    Awaitable["ProviderCredential | None"],
]


class CredentialResolver:
    """Resolves per-``(tenant, user, provider)`` credentials (ADR-0112 §4a).

    The resolution order of ADR-0095 §1, as generalised by ADR-0112 §1, is
    encoded as an ordered source list: the two vault sources prepend to the
    environment source when a session is bound, and the first source to yield a
    credential wins. Constructed without a session, the resolver consults the
    environment alone.
    """

    def __init__(
        self,
        *,
        matrix: CapabilityMatrix | None = None,
        environ: Mapping[str, str] | None = None,
        session: AsyncSession | None = None,
        taxonomy: Mapping[str, ProviderDeclaration] | None = None,
    ) -> None:
        """Initialise the resolver.

        Args:
            matrix: The capability matrix carrying the per-provider credential
                policy for market-data providers (ADR-0095 §2). Defaults to the
                cached shipped matrix.
            environ: The environment mapping to read credentials from. Defaults
                to :data:`os.environ`; injectable for tests.
            session: A **tenant-scoped** :class:`AsyncSession` (acquired via
                ``tenant_context``) for the vault sources to read
                ``scoped_settings`` through. Without one there are no vault
                sources and the environment is the only source.
            taxonomy: The provider taxonomy (ADR-0112 §3) the vault sources read
                their secret-field set from. Defaults to
                :data:`~services.credential_vault.PROVIDER_TAXONOMY`;
                injectable for tests.
        """
        self._matrix = matrix or get_capability_matrix()
        self._environ = environ if environ is not None else os.environ
        self._taxonomy = taxonomy if taxonomy is not None else PROVIDER_TAXONOMY
        self._repository = ScopedSettingRepository(session) if session is not None else None
        # One WARNING per resolver instance when the vault is unconfigured, not
        # one per resolution (ADR-0112 §2) — both vault sources share the flag.
        self._vault_disabled_warned = False
        # Ordered credential sources (ADR-0095 §1, ADR-0112 §1). The vault
        # sources exist only with a bound session; the first source to yield a
        # credential wins, so call sites never change.
        sources: list[tuple[str, _CredentialSource]] = []
        if session is not None:
            sources.append(("vault-user", partial(self._resolve_from_vault, scope="user")))
            sources.append(("vault-tenant", partial(self._resolve_from_vault, scope="tenant")))
        sources.append(("env", self._resolve_from_env))
        self._sources: tuple[tuple[str, _CredentialSource], ...] = tuple(sources)

    async def resolve(
        self,
        provider: str,
        *,
        tenant_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> ProviderCredential | NoCredential:
        """Resolve the credential for ``provider`` (ADR-0095 §1, ADR-0112 §1).

        Args:
            provider: The provider key (must carry a credential policy — in the
                capability matrix for market-data providers, in the taxonomy
                for the rest).
            tenant_id: The tenant the resolution is for. The vault sources read
                it implicitly from the session's tenant context; it is threaded
                here for the log line.
            user_id: The user the resolution is for. Without it the user-scope
                vault source does not fire — a beat or job resolving for a
                tenant rather than a person simply has no user link in its
                chain.

        Returns:
            A :class:`ProviderCredential` when a credential is found, or a
            :class:`NoCredential` when the provider needs none or an optional
            credential is absent.

        Raises:
            CredentialUnavailableError: If the provider requires a credential
                and no vault scope or the environment can serve it.
            VaultDecryptError: If a vault row's ciphertext will not decrypt
                under the active master key — a wrong-key or corrupt vault is
                an operator problem and never falls through to the environment.
            UnsupportedCapabilityError: If neither the matrix nor the taxonomy
                declares the provider.
        """
        policy = self._policy_for(provider)

        if not policy.requires:
            self._log(provider, tenant_id, "none-required")
            return NoCredential(provider)

        cipher = _CipherHolder()
        for label, source in self._sources:
            credential = await source(provider, policy, user_id, cipher)
            if credential is not None:
                self._log(provider, tenant_id, label)
                return credential

        if policy.optional:
            # Optional and absent: proceed without a credential (e.g. OpenFIGI
            # keyless). Not an error.
            self._log(provider, tenant_id, "none-optional-absent")
            return NoCredential(provider)

        consulted = ", ".join(label for label, _ in self._sources)
        raise CredentialUnavailableError(
            f"No credential for provider {provider!r} in tenant {tenant_id} "
            f"(env fallback {'allowed' if policy.env_fallback else 'forbidden'}"
            f"; sources consulted: {consulted})."
        )

    async def resolve_config(
        self,
        provider: str,
        key: str,
        *,
        user_id: UUID | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> str | None:
        """Resolve one **config** field along its own chain (ADR-0112 §4b).

        The config half of the façade. Where :meth:`resolve` chains a
        provider's secret fields as a *unit*, each config field
        (``openrouter.model``, ``base_url``, …) is an independently chained
        setting: this method walks ``user`` → ``tenant`` → ``env`` and returns
        the first value that is set, or ``None``.

        A ``None`` is not a failure — it means "nothing is configured
        anywhere", and the **caller** applies its own default. That split is
        deliberate: the resolver knows the chain, the consumer knows what a
        sensible default is (``WebSettings.openrouter_base_url`` for the web
        surface, ``_DEFAULT_IRENE_MODEL`` for the tick, …).

        No cipher is ever constructed here and no secret row is ever read:
        config rows carry ``value_plain``, so an unconfigured vault is
        irrelevant to this path (unlike :meth:`resolve`, whose vault sources
        decline without a master key).

        Args:
            provider: The provider key; must be declared in the taxonomy.
            key: The config field name; must be a declared **non-secret**
                field of ``provider``.
            user_id: The user the resolution is for. The ``user`` source only
                fires when this is given *and* the field declares the user
                scope — a beat or bot turn resolving for a tenant rather than
                a person simply has no user link in its chain.
            scopes: Restrict the chain to these sources, a subset of
                ``("user", "tenant", "env")`` in that fixed order. ``None``
                (the default) means the field's full declared chain. Used by
                consumers that need scope-major precedence across two fields —
                the tick resolves ``irene_model`` then ``model`` at tenant
                scope before descending to the environment (ADR-0112 §4b), and
                the Report Scraper walks the same shape with
                ``scraper_model`` in Irene's place (ADR-0123).

        Returns:
            The first value found along the chain, or ``None`` when the field
            is set nowhere. A disabled row counts as absent, and so does an
            empty value — an empty model id is not a configured model.

        Raises:
            UnsupportedCapabilityError: If the provider is not declared, if it
                declares no field called ``key``, or if that field is a
                **secret** — secrets are the credential and resolve through
                :meth:`resolve`, never through this method.
            ValueError: If ``scopes`` names a source outside
                :data:`_CONFIG_SOURCE_LABELS`.
        """
        field = self._config_field(provider, key)
        for label in self._config_chain(field, user_id=user_id, scopes=scopes):
            value = await self._config_from_source(label, provider, key, user_id=user_id)
            if value:
                self._log_config(provider, key, label)
                return value
        self._log_config(provider, key, "unset")
        return None

    def _config_field(self, provider: str, key: str) -> ProviderField:
        """Return the declared **config** field ``provider.key``.

        Raises:
            UnsupportedCapabilityError: If the provider is undeclared, the
                field is undeclared, or the field is a secret. One typed error
                across the façade — the same class
                :func:`~services.credential_vault.taxonomy.declaration_for`
                and the capability matrix raise — so a caller catches one
                type whichever half of the declaration was missing.
        """
        declaration = declaration_for(provider, self._taxonomy)
        field = declaration.field(key)
        if field is None:
            raise UnsupportedCapabilityError(
                f"Provider {provider!r} declares no field {key!r} (ADR-0112 §3)."
            )
        if field.is_secret:
            raise UnsupportedCapabilityError(
                f"Field {provider!r}.{key!r} is a secret: it is part of the "
                "provider's credential and resolves through resolve(), which "
                "chains all secret fields as a unit (ADR-0112 §1). "
                "resolve_config() serves config fields only."
            )
        return field

    @staticmethod
    def _config_chain(
        field: ProviderField,
        *,
        user_id: UUID | None,
        scopes: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        """Return the ordered source labels this config resolution consults.

        The field's declared scopes decide which vault sources can appear at
        all; ``user`` additionally needs a ``user_id``. The environment is
        always the last link (a field with no ``_ENV_CONFIG_FIELDS`` entry
        simply finds nothing there). ``scopes`` then filters that chain
        without reordering it.

        Raises:
            ValueError: If ``scopes`` names an unknown source.
        """
        chain = [
            label
            for label in _CONFIG_SOURCE_LABELS
            if label == "env"
            or (label in field.scopes and (label != "user" or user_id is not None))
        ]
        if scopes is None:
            return tuple(chain)
        unknown = set(scopes) - set(_CONFIG_SOURCE_LABELS)
        if unknown:
            raise ValueError(
                f"resolve_config: unknown scope(s) {sorted(unknown)}; "
                f"expected a subset of {list(_CONFIG_SOURCE_LABELS)}."
            )
        return tuple(label for label in chain if label in scopes)

    async def _config_from_source(
        self,
        label: str,
        provider: str,
        key: str,
        *,
        user_id: UUID | None,
    ) -> str | None:
        """Read one config source, or decline with ``None``.

        The vault sources read ``value_plain`` off an **enabled, non-secret**
        row of their own scope; anything else (missing row, disabled row, a
        secret row squatting the key) declines. The environment source reads
        the variable :data:`_ENV_CONFIG_FIELDS` declares for the field.
        """
        if label == "env":
            env_var = _ENV_CONFIG_FIELDS.get(provider, {}).get(key)
            return self._environ.get(env_var) if env_var else None
        repository = self._repository
        if repository is None:
            return None
        row = await repository.get(
            label, provider, key, user_id=user_id if label == "user" else None
        )
        if row is None or not row.enabled or row.is_secret:
            return None
        return row.value_plain

    @staticmethod
    def _log_config(provider: str, key: str, source: str) -> None:
        """Emit the one DEBUG line per config resolution (ADR-0112 §4b).

        States the *source* — ``user``, ``tenant``, ``env`` or ``unset``.
        Config values are not secrets, but the line stays value-free anyway so
        every resolution log in this module reads the same way.
        """
        _LOG.debug(
            "config resolve: provider=%s key=%s source=%s",
            provider,
            key,
            source,
        )

    def _policy_for(self, provider: str) -> CredentialPolicy:
        """Return the provider's credential policy from its single owner.

        Market-data providers keep their policy in the capability matrix
        (ADR-0095 §2, unchanged by ADR-0112); every other declared provider
        takes it from the taxonomy, which is *its* policy source (ADR-0112 §3).
        A provider neither declares raises the matrix's typed unknown-provider
        error.

        Args:
            provider: The provider key.

        Returns:
            The resolved :class:`CredentialPolicy`.

        Raises:
            UnsupportedCapabilityError: If neither source declares the provider.
        """
        declaration = self._taxonomy.get(provider)
        if declaration is not None and not declaration.managed_by_matrix:
            return CredentialPolicy(
                provider=provider,
                # A provider with no secret field takes no credential; its
                # config fields chain individually, elsewhere.
                requires=bool(declaration.secret_fields),
                env_fallback=bool(declaration.env_fallback),
                optional=bool(declaration.optional),
            )
        return self._matrix.credential_policy(provider)

    async def _resolve_from_vault(
        self,
        provider: str,
        policy: CredentialPolicy,
        user_id: UUID | None,
        cipher: _CipherHolder,
        *,
        scope: str,
    ) -> ProviderCredential | None:
        """Resolve one scope's rows out of the vault (ADR-0112 §4a).

        The single vault-source implementation, bound once per scope. It
        assembles a payload **only** from rows of its own scope, and only when
        every declared secret field is present there — the §1 completeness rule,
        from which no-cross-scope-mixing falls out structurally.

        Declines (returns ``None``) when: the user scope is asked for without a
        user; the vault is unconfigured; the provider is undeclared; the
        provider has no secret field; a secret field does not declare this
        scope (so the scope could never be complete); or a row is missing,
        disabled, or not a secret row.

        Raises:
            VaultDecryptError: If a present row's ciphertext will not decrypt.
                Deliberately not caught: it must not fall through to the
                environment.
        """
        repository = self._repository
        if repository is None:
            return None
        if scope == "user" and user_id is None:
            return None
        if not is_vault_configured():
            self._warn_vault_disabled_once()
            return None

        declaration = self._taxonomy.get(provider)
        if declaration is None:
            # Undeclared here is not an error — the environment source may still
            # serve the provider on the matrix's policy.
            return None
        fields = declaration.secret_fields
        if not fields or any(scope not in field.scopes for field in fields):
            return None

        row_user_id = user_id if scope == "user" else None
        payload: dict[str, str] = {}
        for field in fields:
            row = await repository.get(scope, provider, field.name, user_id=row_user_id)
            if row is None or not row.enabled or not row.is_secret or row.value_ciphertext is None:
                # Completeness (ADR-0112 §1): one absent field declines the
                # whole scope. Never a partial payload, never a field borrowed
                # from another scope.
                return None
            try:
                payload[field.name] = cipher.get().decrypt(row.value_ciphertext)
            except VaultDecryptError as exc:
                # Re-raised with the row's identity so the operator can find it;
                # neither the ciphertext nor the plaintext is named.
                raise VaultDecryptError(
                    f"Vault row {row.id} ({scope}-scope {provider!r}.{field.name}) "
                    "could not be decrypted with the active master key — the key "
                    "does not match the one the row was written under. See "
                    "docs/deploy/credential-vault.md."
                ) from exc
        return ProviderCredential(provider=provider, payload=payload)

    async def _resolve_from_env(
        self,
        provider: str,
        policy: CredentialPolicy,
        user_id: UUID | None,
        cipher: _CipherHolder,
    ) -> ProviderCredential | None:
        """Resolve from the environment, honouring the fallback policy.

        The application-scope source. ``user_id`` and ``cipher`` are part of the
        uniform source signature and unused here — the environment has no user
        axis and holds no ciphertext.

        Returns ``None`` (this source declines) when the policy forbids the
        environment fallback, when no environment mapping is declared for the
        provider, or when a declared credential field is unset — an incomplete
        credential is treated as absent, never as a partial one.
        """
        if not policy.env_fallback:
            return None
        fields = _ENV_CREDENTIAL_FIELDS.get(provider, {})
        if not fields:
            return None
        payload: dict[str, str] = {}
        for field_name, env_var in fields.items():
            value = self._environ.get(env_var)
            if value:
                payload[field_name] = value
        if len(payload) != len(fields):
            return None
        return ProviderCredential(provider=provider, payload=payload)

    def _warn_vault_disabled_once(self) -> None:
        """Emit the single unconfigured-vault WARNING per resolver (ADR-0112 §2)."""
        if self._vault_disabled_warned:
            return
        self._vault_disabled_warned = True
        _LOG.warning(
            "credential vault disabled: %s is not set — vault sources decline "
            "and resolution falls through to the environment. See "
            "docs/deploy/credential-vault.md.",
            MASTER_KEY_ENV_VAR,
        )

    @staticmethod
    def _log(provider: str, tenant_id: UUID | None, source: str) -> None:
        """Emit the one structured INFO line per resolution (ADR-0095 §4).

        States the *source* — ``vault-user``, ``vault-tenant``, ``env`` or one
        of the ``none-*`` outcomes (ADR-0112 §4a) — never the value (the
        payload never appears in logs).
        """
        _LOG.info(
            "credential resolve: provider=%s tenant=%s source=%s",
            provider,
            tenant_id,
            source,
        )


__all__ = [
    "CredentialResolver",
    "CredentialUnavailableError",
    "NoCredential",
    "ProviderCredential",
]
