# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Orchestration tests for the shared Irene tick runner (ADR-0086, ADR-0117).

These are fully-mocked orchestration tests: every seam the tick reaches
(the AI core, the credential vault, the cross-tenant due read,
``tenant_context``, the RSS harvest, the credential façade, the beat
handler, and the schedule repository) is monkeypatched at the
``services.scheduler.tick_runner`` module level, and the engine is injected
as a fake, so no live DB and no LLM is required. They assert the tick's
control flow — beats per due tenant, advisory-lock skip on contention, the
tolerant no-credential-anywhere exit, and (since ADR-0112 §4b) per-tenant
credential resolution: one tenant's missing key skips only that tenant, and
one tenant's key never reaches another tenant's embedder.

They moved here from ``tests/cli/test_irene_tick.py`` with the
orchestration itself (ADR-0117 §2): the runner is now the single
implementation both the CLI tick and the in-process scheduler drive, so
this is where its behaviour is pinned. What stayed with the CLI is the
wrapper's own surface — exit codes, engine lifecycle, flags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import uuid4

import openai

import services.scheduler.tick_runner as tick_runner
import services.web_research.service as web_research_service
from services.investments.credential_resolver import (
    CredentialUnavailableError,
    ProviderCredential,
)
from services.irene.beat import BeatResult
from services.irene.scheduling import DueTenant
from services.scheduler.tick_runner import run_irene_tick


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, api_key: str | None) -> None:
        self.openrouter_api_key = api_key
        self.openrouter_base_url = "https://openrouter.ai/api/v1"


class _FakeCore:
    """The tick no longer configures the core — it only carries the turn
    machinery (ADR-0112 §4b). A bare object is enough."""


class _FakeResolver:
    """Records every façade call and answers from a per-tenant script.

    Constructed by the tick as ``CredentialResolver(session=session)``; the
    installer hands each instance the script of the tenant whose context it
    was built in, so a resolver can never answer with another tenant's
    values — the property the isolation tests turn on.
    """

    def __init__(self, *, key: str | None, config: dict[tuple[str, tuple | None], str]) -> None:
        self._key = key
        self._config = config
        self.config_calls: list[tuple[str, tuple | None]] = []

    async def resolve(self, provider: str, **kwargs: Any) -> Any:
        if self._key is None:
            raise CredentialUnavailableError(f"no credential for {provider!r} (test)")
        return ProviderCredential(provider=provider, payload={"api_key": self._key})

    async def resolve_config(
        self,
        provider: str,
        key: str,
        *,
        user_id: Any = None,
        scopes: tuple[str, ...] | None = None,
    ) -> str | None:
        self.config_calls.append((key, scopes))
        return self._config.get((key, scopes))


class _ExplodingWebResearchService:
    """Stands in for ``WebResearchService`` when the harvest must fail."""

    def harvest_items(self) -> list:
        raise RuntimeError("feed fetch failed (test)")


class _RecordingEmbedder:
    """Stands in for ``OpenRouterEmbedder``; keeps the injected factory."""

    instances: ClassVar[list[_RecordingEmbedder]] = []

    def __init__(self, client_factory: Any) -> None:
        self.client_factory = client_factory
        _RecordingEmbedder.instances.append(self)


class _FakeConn:
    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeEngine:
    """The injected engine. Counts connects; disposal is the caller's job."""

    def __init__(self) -> None:
        self.connects = 0
        self.disposed = False

    def connect(self) -> _FakeConn:
        self.connects += 1
        return _FakeConn()

    async def dispose(self) -> None:
        self.disposed = True


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    """A session whose only executed statement is the advisory-lock claim."""

    def __init__(self, claim: bool) -> None:
        self._claim = claim

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        # The tick issues exactly one execute() on the session: the
        # pg_try_advisory_xact_lock claim. Return the preset claim result.
        return _FakeResult(self._claim)


class _FakeTenantCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@dataclass
class _Recorder:
    engine: _FakeEngine = field(default_factory=_FakeEngine)
    settings: _FakeSettings = field(default_factory=lambda: _FakeSettings("sk-test"))
    tenant_contexts: list = field(default_factory=list)
    beats: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    #: ``(tenant_id, ResolvedLLM)`` for every beat that got that far.
    llms: list = field(default_factory=list)
    #: The resolver instance built inside each tenant's context, by tenant.
    resolvers: dict = field(default_factory=dict)


def _due(n: int) -> list[DueTenant]:
    return [
        DueTenant(
            tenant_id=uuid4(),
            schedule_id=uuid4(),
            cadence="daily",
            timezone="UTC",
            preferred_hour=6,
        )
        for _ in range(n)
    ]


def _install(
    monkeypatch: Any,
    *,
    due: list[DueTenant],
    api_key: str | None = "sk-test",
    claims: dict | None = None,
    beat_results: dict | None = None,
    vault_configured: bool = False,
    tenant_keys: dict | None = None,
    tenant_config: dict | None = None,
    rss_items: list | None = None,
    stub_harvest: bool = True,
) -> _Recorder:
    """Patch every ``services.scheduler.tick_runner`` seam and return a recorder.

    Args:
        due: The due tenants ``find_due_tenants`` returns.
        api_key: The (fake) application-scope OpenRouter key; ``None``
            exercises the no-environment-key path.
        claims: Optional ``{tenant_id: bool}`` — the advisory-lock claim
            result per tenant (defaults to True = claimed).
        beat_results: Optional ``{tenant_id: BeatResult}`` — override the
            beat outcome per tenant (defaults to a silent success).
        vault_configured: What ``is_vault_configured()`` reports.
        tenant_keys: Optional ``{tenant_id: key | None}`` — the credential
            each tenant resolves. Defaults to ``api_key`` for every tenant
            (the pre-F4 env-only world), so existing tests are unaffected.
        tenant_config: Optional ``{tenant_id: {(key, scopes): value}}`` —
            the config chain each tenant answers with. Defaults to a model
            at env scope, mirroring ``SHIRLEY_MODEL``.
        rss_items: The harvested items; non-empty makes the tick build a
            per-tenant embedder.
        stub_harvest: Whether to replace ``_harvest_rss_items`` with a stub
            returning ``rss_items``. ``False`` drives the real function (the
            harvest-tolerance tests).
    """
    claims = claims or {}
    beat_results = beat_results or {}
    tenant_keys = tenant_keys if tenant_keys is not None else {}
    tenant_config = tenant_config or {}
    rec = _Recorder(settings=_FakeSettings(api_key))
    _RecordingEmbedder.instances.clear()

    monkeypatch.setattr(tick_runner, "get_ai_service_core", lambda: _FakeCore())
    monkeypatch.setattr(tick_runner, "is_vault_configured", lambda: vault_configured)
    monkeypatch.setattr(tick_runner, "OpenRouterEmbedder", _RecordingEmbedder)
    # RSS harvest is a per-tick seam; stub it to no items (no network) so
    # these control-flow tests stay fully offline.
    if stub_harvest:
        monkeypatch.setattr(tick_runner, "_harvest_rss_items", lambda: list(rss_items or []))

    # The façade is constructed *inside* each tenant's context, so the
    # installer tracks which tenant is open and hands that tenant's script
    # to the resolver built there.
    open_tenant: list[Any] = [None]

    def _fake_resolver(*, session: Any = None) -> _FakeResolver:
        tenant_id = open_tenant[0]
        resolver = _FakeResolver(
            key=tenant_keys.get(tenant_id, api_key),
            config=tenant_config.get(tenant_id, {("model", ("env",)): "shirley-model"}),
        )
        rec.resolvers[tenant_id] = resolver
        return resolver

    monkeypatch.setattr(tick_runner, "CredentialResolver", _fake_resolver)

    async def _fake_find_due(conn: Any) -> list[DueTenant]:
        return due

    monkeypatch.setattr(tick_runner, "find_due_irene_tenants", _fake_find_due)

    # Map each due tenant to its claim so tenant_context can hand back a
    # session with the right advisory-lock result.
    claim_by_tenant = {d.tenant_id: claims.get(d.tenant_id, True) for d in due}

    def _fake_tenant_context(engine: Any, tenant_id: Any, *a: Any, **k: Any):
        rec.tenant_contexts.append(tenant_id)
        open_tenant[0] = tenant_id
        return _FakeTenantCtx(_FakeSession(claim_by_tenant.get(tenant_id, True)))

    monkeypatch.setattr(tick_runner, "tenant_context", _fake_tenant_context)

    # run_beat is keyed by call order over the tenants that actually reach
    # it (the tick beats in due order, minus those skipped for a held lock
    # or a missing credential); record the resolution and return the
    # preset / default result.
    reaching = [
        d
        for d in due
        if claims.get(d.tenant_id, True) and tenant_keys.get(d.tenant_id, api_key) is not None
    ]
    order = iter(reaching)

    async def _fake_run_beat(
        session: Any,
        ai_core: Any,
        *,
        llm: Any,
        now: Any,
        rss_items: Any = None,
        embedder: Any = None,
    ) -> BeatResult:
        current = next(order)
        rec.beats.append(current.tenant_id)
        rec.llms.append((current.tenant_id, llm))
        return beat_results.get(
            current.tenant_id,
            BeatResult(
                tenant_id=current.tenant_id,
                findings_written=0,
                silence=True,
                error=None,
            ),
        )

    monkeypatch.setattr(tick_runner, "run_beat", _fake_run_beat)

    class _FakeRepo:
        def __init__(self, session: Any) -> None: ...

        async def mark_beat_done(
            self, *, schedule_id: Any, last_beat_at: Any, next_due_at: Any
        ) -> None:
            rec.marks.append(schedule_id)

    monkeypatch.setattr(tick_runner, "IreneScheduleRepository", _FakeRepo)

    return rec


async def _tick(rec: _Recorder) -> Any:
    """Run one tick on the recorder's injected engine and settings."""
    return await run_irene_tick(rec.engine, settings=rec.settings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_no_due_tenants_no_beats(monkeypatch: Any) -> None:
    rec = _install(monkeypatch, due=[])
    summary = await _tick(rec)
    assert rec.beats == []
    assert rec.marks == []
    assert summary.due == 0 and summary.beaten == 0
    # The engine was used for the due read — and left to its owner to
    # dispose of (the web host's engine outlives the tick, ADR-0117 §2).
    assert rec.engine.connects == 1
    assert rec.engine.disposed is False


async def test_two_due_tenants_two_beats_each_own_context(monkeypatch: Any) -> None:
    due = _due(2)
    rec = _install(monkeypatch, due=due)
    summary = await _tick(rec)
    # Two beats, one per tenant.
    assert rec.beats == [d.tenant_id for d in due]
    # Each beat ran inside its own tenant_context (distinct tenant ids).
    assert rec.tenant_contexts == [d.tenant_id for d in due]
    # Both schedules advanced.
    assert rec.marks == [d.schedule_id for d in due]
    assert (summary.due, summary.beaten, summary.errors) == (2, 2, 0)


async def test_advisory_lock_contention_skips_that_tenant(monkeypatch: Any) -> None:
    due = _due(2)
    # The second tenant's advisory-lock claim fails (held elsewhere).
    claims = {due[1].tenant_id: False}
    rec = _install(monkeypatch, due=due, claims=claims)
    summary = await _tick(rec)
    # Only the first tenant is beaten; the second is skipped, not blocked.
    assert rec.beats == [due[0].tenant_id]
    assert rec.marks == [due[0].schedule_id]
    # Both tenants still opened a context (the claim happens inside it).
    assert rec.tenant_contexts == [d.tenant_id for d in due]
    assert (summary.beaten, summary.skipped) == (1, 1)


async def test_beat_error_does_not_advance_schedule(monkeypatch: Any) -> None:
    due = _due(1)
    results = {
        due[0].tenant_id: BeatResult(
            tenant_id=due[0].tenant_id,
            findings_written=0,
            silence=False,
            error="LLM down",
        )
    }
    rec = _install(monkeypatch, due=due, beat_results=results)
    # A single tenant's beat error does not fail the tick.
    summary = await _tick(rec)
    assert rec.beats == [due[0].tenant_id]
    # Schedule is NOT advanced on error — next tick retries.
    assert rec.marks == []
    assert (summary.beaten, summary.errors) == (0, 1)


async def test_tenant_failure_is_isolated_and_does_not_fail_the_tick(
    monkeypatch: Any,
) -> None:
    """A raising beat is caught, counted, and the next tenant still beats."""
    due = _due(2)
    rec = _install(monkeypatch, due=due)
    beaten: list = []
    original = tick_runner.run_beat

    async def _raising_run_beat(session: Any, ai_core: Any, **kwargs: Any) -> Any:
        result = await original(session, ai_core, **kwargs)
        if result.tenant_id == due[0].tenant_id:
            raise RuntimeError("beat exploded")
        beaten.append(result.tenant_id)
        return result

    monkeypatch.setattr(tick_runner, "run_beat", _raising_run_beat)

    summary = await _tick(rec)

    assert beaten == [due[1].tenant_id]
    assert rec.marks == [due[1].schedule_id]
    assert (summary.beaten, summary.errors) == (1, 1)


async def test_no_vault_and_no_env_key_is_an_early_no_op(monkeypatch: Any) -> None:
    """Neither scope can serve: warn, return an empty summary, touch no DB."""
    rec = _install(monkeypatch, due=_due(1), api_key=None, vault_configured=False)
    summary = await _tick(rec)

    assert rec.beats == []
    # Returned before the due read — the engine was never even connected.
    assert rec.engine.connects == 0
    assert summary == tick_runner.IreneTickSummary()


def test_harvest_failure_returns_no_items_and_warns(monkeypatch: Any, caplog: Any) -> None:
    """The tolerance contract itself: any harvest failure degrades to ``[]``."""
    monkeypatch.setattr(
        web_research_service,
        "WebResearchService",
        _ExplodingWebResearchService,
    )

    with caplog.at_level(logging.WARNING, logger="portfoliflow.scheduler"):
        items = tick_runner._harvest_rss_items()

    assert items == []
    assert any("RSS harvest unavailable" in r.getMessage() for r in caplog.records)


async def test_harvest_failure_degrades_to_internal_only_beats(monkeypatch: Any) -> None:
    """A failing harvest is not a tick error — it is an internal-only beat.

    Drives the *real* ``_harvest_rss_items`` (no stub) against a raising
    ``WebResearchService``: the tick beats on, and with no items no embedder
    is built.
    """
    due = _due(1)
    rec = _install(monkeypatch, due=due, stub_harvest=False)
    monkeypatch.setattr(
        web_research_service,
        "WebResearchService",
        _ExplodingWebResearchService,
    )

    summary = await _tick(rec)

    assert summary.beaten == 1
    assert rec.beats == [due[0].tenant_id]
    # No RSS ⇒ no embedder is built for the beat.
    assert _RecordingEmbedder.instances == []


# ---------------------------------------------------------------------------
# Per-tenant credential resolution (ADR-0112 §4b)
# ---------------------------------------------------------------------------


async def test_vault_configured_without_env_key_still_runs(monkeypatch: Any) -> None:
    """A vault can hold a tenant's own key, so the tick must not shortcut.

    The pre-F4 shortcut ("no OPENROUTER_API_KEY ⇒ nothing to beat") is only
    sound when the vault is *also* absent; with one configured, the tick has
    to run and let per-tenant resolution decide.
    """
    due = _due(1)
    rec = _install(
        monkeypatch,
        due=due,
        api_key=None,
        vault_configured=True,
        tenant_keys={due[0].tenant_id: "sk-tenant"},
    )
    await _tick(rec)

    assert rec.beats == [due[0].tenant_id]
    assert rec.llms[0][1].api_key == "sk-tenant"


async def test_keyless_tenant_is_skipped_and_keyed_tenant_is_beaten(
    monkeypatch: Any, caplog: Any
) -> None:
    """One tenant's missing credential skips only that tenant (D5/D6)."""
    due = _due(2)
    keyed, keyless = due[0], due[1]
    rec = _install(
        monkeypatch,
        due=due,
        api_key=None,
        vault_configured=True,
        tenant_keys={keyed.tenant_id: "sk-a", keyless.tenant_id: None},
    )
    with caplog.at_level(logging.WARNING, logger="portfoliflow.scheduler"):
        summary = await _tick(rec)

    # Only the keyed tenant beat; the keyless one never reached run_beat.
    assert rec.beats == [keyed.tenant_id]
    assert rec.marks == [keyed.schedule_id]
    # Both still opened a context — resolution happens inside it.
    assert rec.tenant_contexts == [d.tenant_id for d in due]
    assert (summary.beaten, summary.no_key_skipped) == (1, 1)
    # And the skip is loud enough to diagnose.
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "no resolvable LLM credential" in m and str(keyless.tenant_id) in m for m in messages
    )
    # Not the all-skipped closing warning: one tenant did beat.
    assert not any("none resolved an LLM credential" in m for m in messages)


async def test_all_tenants_keyless_logs_the_closing_warning(monkeypatch: Any, caplog: Any) -> None:
    """Due tenants but no key anywhere: no beats, and say so once at the end."""
    due = _due(2)
    rec = _install(
        monkeypatch,
        due=due,
        api_key=None,
        vault_configured=True,
        tenant_keys={d.tenant_id: None for d in due},
    )
    with caplog.at_level(logging.WARNING, logger="portfoliflow.scheduler"):
        summary = await _tick(rec)

    assert rec.beats == []
    assert summary.no_key_skipped == summary.due == 2
    assert any("none resolved an LLM credential" in r.getMessage() for r in caplog.records)


async def test_model_chain_is_scope_major_with_irene_winning_inside_each_scope(
    monkeypatch: Any,
) -> None:
    """The D4 chain, pinned as a call sequence.

    ``tenant irene_model`` → ``tenant model`` → ``env IRENE_MODEL`` →
    ``env SHIRLEY_MODEL`` → default: scope-major, and inside each scope
    Irene's own field outranks Shirley's.
    """
    due = _due(1)
    tenant_id = due[0].tenant_id
    rec = _install(
        monkeypatch,
        due=due,
        vault_configured=True,
        tenant_keys={tenant_id: "sk-a"},
        # Only the *last* model link is set, so every earlier link is
        # consulted and the whole order becomes observable.
        tenant_config={tenant_id: {("model", ("env",)): "shirley/env-model"}},
    )
    await _tick(rec)

    assert rec.llms[0][1].model == "shirley/env-model"
    assert rec.resolvers[tenant_id].config_calls[:4] == [
        ("irene_model", ("tenant",)),
        ("model", ("tenant",)),
        ("irene_model", ("env",)),
        ("model", ("env",)),
    ]


async def test_env_irene_model_outranks_env_shirley_model(monkeypatch: Any) -> None:
    """Inside the environment scope, Irene's field still wins — and the
    chain short-circuits there rather than asking for Shirley's."""
    due = _due(1)
    tenant_id = due[0].tenant_id
    rec = _install(
        monkeypatch,
        due=due,
        vault_configured=True,
        tenant_keys={tenant_id: "sk-a"},
        tenant_config={
            tenant_id: {
                ("irene_model", ("env",)): "irene/env-model",
                ("model", ("env",)): "shirley/env-model",
            }
        },
    )
    await _tick(rec)

    assert rec.llms[0][1].model == "irene/env-model"
    assert ("model", ("env",)) not in rec.resolvers[tenant_id].config_calls


async def test_tenant_model_outranks_both_environment_fields(monkeypatch: Any) -> None:
    """A tenant's plain ``model`` beats the environment's ``IRENE_MODEL``."""
    due = _due(1)
    tenant_id = due[0].tenant_id
    rec = _install(
        monkeypatch,
        due=due,
        vault_configured=True,
        tenant_keys={tenant_id: "sk-a"},
        tenant_config={
            tenant_id: {
                ("model", ("tenant",)): "tenant/model",
                ("irene_model", ("env",)): "irene/env-model",
            }
        },
    )
    await _tick(rec)

    assert rec.llms[0][1].model == "tenant/model"


async def test_model_falls_back_to_the_default_when_no_scope_sets_one(monkeypatch: Any) -> None:
    due = _due(1)
    tenant_id = due[0].tenant_id
    rec = _install(
        monkeypatch,
        due=due,
        vault_configured=True,
        tenant_keys={tenant_id: "sk-a"},
        tenant_config={tenant_id: {}},
    )
    await _tick(rec)

    assert rec.llms[0][1].model == tick_runner._DEFAULT_IRENE_MODEL


async def test_each_tenant_gets_its_own_embedder_and_never_the_other_key(
    monkeypatch: Any,
) -> None:
    """One embedder per tenant, built from *that* tenant's credential.

    The tick used to build a single embedder per tick from the singleton's
    one key. With per-tenant credentials that would hand tenant A's key to
    tenant B's vectorisation, so the embedder moved inside the loop — and
    the client each one builds must carry only its own tenant's key.
    """
    due = _due(2)
    a, b = due[0], due[1]
    rec = _install(
        monkeypatch,
        due=due,
        vault_configured=True,
        tenant_keys={a.tenant_id: "sk-a", b.tenant_id: "sk-b"},
        rss_items=["one-harvested-item"],
    )

    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: captured.append(kwargs) or object())

    await _tick(rec)

    assert rec.beats == [a.tenant_id, b.tenant_id]
    # Two embedders, one per tenant — not one shared across the tick.
    assert len(_RecordingEmbedder.instances) == 2
    # Each builds a client on its own tenant's key, and only that one.
    for embedder in _RecordingEmbedder.instances:
        embedder.client_factory()
    assert [c["api_key"] for c in captured] == ["sk-a", "sk-b"]
