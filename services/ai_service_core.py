# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Qt-free asyncio core of the AIService.

This module owns the OpenAI / OpenRouter client construction, the
tool-execution loop, soul-identity injection, and the
:class:`~services.tool_registry.ToolRegistry` integration with
ADR-0022's per-turn ``begin_turn`` / ``end_turn`` brackets. It MUST NOT
import from :mod:`PyQt6` in any form. The Qt-coupled side of the
service lives in :mod:`services.ai_service_qt`.

ADR-0038 named this module as the resolution of ADR-0011's follow-up:
the layering exception in ``services/`` shrinks from "the entire AI
service" to "the Qt adapter file only". The split also makes the
asyncio-native consumers (FastAPI in Phase 2, modern Telegram libs)
first-class without forcing them through a Qt-bridge.

The streaming surface is :meth:`AIServiceCore.stream_response`, an
async generator yielding :class:`StreamEvent` records. Adapters
(``ai_service_qt``, future SSE handler, future Telegram consumer)
translate those events into their respective wire shapes.

Concurrency:
    A module-level :class:`threading.Lock` (``_TURN_LOCK``) serialises
    every call to :meth:`AIServiceCore.stream_response` against every
    other call, regardless of which event loop or thread the call was
    issued from. This is the consolidated home for the interim
    concurrency control originally introduced by ADR-0031 in
    :mod:`services.headless_shirley`; stream A2 of ADR-0038 relocated
    it to the core because every consumer (GUI Qt adapter, Telegram
    bot, FastAPI SSE handler) routes through this seam, so a lock here
    closes the cross-channel race characterised in ADR-0031.

    The lock is :class:`threading.Lock`, not :class:`asyncio.Lock`,
    because consumers dispatch from different event loops: the Qt
    adapter spawns one ``asyncio.run`` per ``send_message`` inside a
    fresh ``QThread``; the Telegram bot runs on its own daemon-thread
    asyncio loop; the FastAPI handler runs many SSE turns as
    concurrent tasks on one uvicorn loop. A :class:`threading.Lock` is
    loop-agnostic and works across threads without binding to any one
    loop.

    The lock is *acquired* via ``asyncio.to_thread(_TURN_LOCK.acquire)``
    — not a synchronous ``with _TURN_LOCK:``. Under FastAPI the earlier
    assumption that "no other task on the same loop wants the lock" is
    false: concurrent SSE turns are tasks on the *same* uvicorn loop
    and all contend for it, so a turn that synchronously blocked on
    ``acquire()`` would freeze the whole loop — every request, and
    signal handling — until the lock came free. Parking the *wait* on
    a worker thread keeps the loop responsive. Once acquired, the lock
    is still *held* across this coroutine's ``await`` points; holding
    it is fine — it was the blocking *wait* that was the bug. For the
    same reason :meth:`_stream_response_locked` dispatches each
    synchronous ``ToolRegistry.execute_tool`` call via
    ``asyncio.to_thread`` (a tool may block on a DB workflow).

    The lock is **process-global**: it serialises every turn across
    *all* users, not just the per-turn gating state of one user. That
    is acceptable for the current single-tenant phase but is
    inadequate for multi-tenant — the multi-tenant conversion must
    replace it with per-tenant (or finer) turn isolation, bundled with
    the ``ToolRegistry`` thread-safety work (ADR-0018). This is the
    same "minimal now, real fix in the multi-tenant run" stance as
    ``services.tools._tool_context.resolve_tenant_id``. Until then this
    lock remains the deliberately narrow interim measure that protects
    the per-turn gating state defined by ADR-0022.

Singleton:
    :func:`get_ai_service_core` returns the application-wide instance,
    mirroring the singleton pattern from ADR-0010.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import openai

from services.ai_models import (
    ConnectionStatus,
    Conversation,
    Message,
    MessageRole,
    ToolCall,
)
from services.tools._tool_context import (
    clear_tool_context,
    clear_tool_data,
    set_tool_context,
)

if TYPE_CHECKING:
    from services.tools._tool_context import ToolExecutionContext

logger = logging.getLogger(__name__)

# Repo root — services/ai_service_core.py → repo root is two levels up.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Maximum number of tool-call iterations per user message. Mirrored
# verbatim from the legacy _MAX_TOOL_ITERATIONS in services/ai_service.py.
_MAX_TOOL_ITERATIONS = 10

# Process-wide turn lock. Acquired in :meth:`AIServiceCore.stream_response`
# around the entire turn so concurrent turns from any consumer (Qt
# adapter, Telegram bot, FastAPI SSE handler) cannot race the
# :class:`~services.tool_registry.ToolRegistry` per-turn gating state
# (ADR-0022). Acquired via ``asyncio.to_thread`` so a waiting turn never
# freezes the uvicorn event loop, and process-global — it serialises
# every user's turns, which is adequate for the single-tenant phase but
# is replaced by per-tenant turn isolation in the multi-tenant
# conversion. See the module docstring "Concurrency" section for the
# full rationale and why this is :class:`threading.Lock` rather than
# :class:`asyncio.Lock`.
_TURN_LOCK = threading.Lock()


# Control tokens that some non-default models leak verbatim into the
# stream. The default Anthropic models the project ships with do not
# emit these, but Llama-family and a few others do, and the bug is
# only visible once the web variant exposes the model picker. See
# sub-stream 2c, Task 4 (Option A): strip server-side so every consumer
# (Qt adapter, web SSE, bot) inherits the fix.
_STOP_TOKENS: tuple[str, ...] = (
    "<|eom|>",
    "<|eot_id|>",
    "<|im_end|>",
    "<|im_start|>",
    "<|endoftext|>",
    "<|end|>",
)


class _StopTokenStripper:
    """Stateful filter that removes known control tokens from a stream.

    Tokens may span ``delta.content`` chunk boundaries, so a naive
    ``str.replace`` on each chunk would let a split token slip through.
    This filter buffers the smallest tail that *could* be the start of
    a known token, emits everything before it, and only releases the
    tail when the next chunk either completes the token (whole match
    deleted) or proves it benign (released to the consumer).
    """

    def __init__(self) -> None:
        """Initialise with an empty pending tail."""
        self._tail = ""

    def process(self, chunk: str) -> str:
        """Strip stop tokens from ``chunk`` and return the safe prefix.

        Any suffix that might be the start of a stop token is held
        back internally and re-emitted by the next ``process`` call
        (or by :meth:`flush`).

        Args:
            chunk: The text delta from the streaming API.

        Returns:
            The stop-token-free portion of the buffered text.
        """
        text = self._tail + chunk
        for tok in _STOP_TOKENS:
            if tok in text:
                text = text.replace(tok, "")

        hold = 0
        for tok in _STOP_TOKENS:
            longest = min(len(tok) - 1, len(text))
            for i in range(longest, 0, -1):
                if text.endswith(tok[:i]):
                    hold = max(hold, i)
                    break

        if hold:
            self._tail = text[-hold:]
            return text[:-hold]
        self._tail = ""
        return text

    def flush(self) -> str:
        """Release any pending tail at end-of-stream.

        Returns:
            The remaining buffered text after one final stop-token
            scrub. Empty when nothing was held back.
        """
        text = self._tail
        self._tail = ""
        for tok in _STOP_TOKENS:
            if tok in text:
                text = text.replace(tok, "")
        return text


EventType = Literal[
    "chunk",
    "tool_called",
    "tool_completed",
    "chart_artifact",
    "stream_finished",
    "error",
]


@dataclass(frozen=True)
class StreamEvent:
    """One event yielded by :meth:`AIServiceCore.stream_response`.

    The event vocabulary covers all observable transitions of one
    Shirley turn. Adapters map each ``event_type`` onto their channel's
    wire shape (Qt signals, SSE frames, Telegram message edits).

    Attributes:
        event_type: The discrete kind of event. See :data:`EventType`.
        payload: Event-specific data. Keys depend on ``event_type``:

            * ``chunk`` — ``{"text": str}``.
            * ``tool_called`` — ``{"name": str, "arguments": str,
              "tool_call_id": str}``. ``arguments`` is the raw JSON
              string the model produced.
            * ``tool_completed`` — ``{"name": str, "result": str,
              "tool_call_id": str}``. ``result`` is the
              LLM-bound text (post-artefact-stripping).
            * ``chart_artifact`` — ``{"chart_format": str,
              "image_base64": str, "spec": dict | None,
              "caption": str}``. ``chart_format`` is ``"png"`` (legacy
              matplotlib path — ``image_base64`` carries the image) or
              ``"plotly"`` (``spec`` carries the figure spec). Both
              keys are always present; the unused one is empty / None.
            * ``stream_finished`` — ``{"message": Message,
              "iterations": int}``. ``message`` carries the final
              assembled assistant reply.
            * ``error`` — ``{"message": str}``.
    """

    event_type: EventType
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedLLM:
    """One turn's endpoint, credential and model, resolved for one tenant.

    The value object that ends the process-global "configure once, serve
    everyone" posture (ADR-0112 §4b). The web chat route resolves one of these
    per turn, the Irene tick one per tenant per beat, and the Telegram handler
    one per turn — each through
    :class:`~services.investments.credential_resolver.CredentialResolver`,
    inside that tenant's context — and hands it to :meth:`stream_response` or
    :meth:`run_synthesis` as ``llm=``. Nothing is cached and nothing is stashed:
    the object lives for exactly one call, so a tenant's key can never be held
    where another tenant's turn could reach it.

    Deliberately **inert in logs**: :func:`repr` (hence :func:`str`, hence any
    f-string, log line or traceback that renders it) masks the key, mirroring
    :class:`~services.investments.credential_resolver.ProviderCredential`.

    Attributes:
        base_url: The OpenAI-compatible endpoint for this turn.
        api_key: The plain API key. Never logged, never stashed.
        model: The model id this turn runs on.
    """

    base_url: str
    api_key: str
    model: str

    def __repr__(self) -> str:
        # Never leak the key. Applies to str() too.
        return (
            f"ResolvedLLM(base_url={self.base_url!r}, "
            f"api_key=<{'set' if self.api_key else 'unset'}; masked>, "
            f"model={self.model!r})"
        )

    __str__ = __repr__

    def make_client(self) -> openai.AsyncOpenAI:
        """Construct a fresh ``AsyncOpenAI`` client bound to this resolution.

        Short-lived and bound to the calling loop, exactly like
        :meth:`AIServiceCore._make_async_client` — see that method's docstring
        for why a client is never shared across loops. Being a zero-argument
        bound method, this doubles as the client factory
        :class:`~services.irene.embedding.OpenRouterEmbedder` takes.

        Returns:
            A new ``openai.AsyncOpenAI`` instance.
        """
        return openai.AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)


@dataclass(frozen=True)
class SynthesisResult:
    """The parsed outcome of one non-streaming Irene synthesis call.

    Returned by :meth:`AIServiceCore.run_synthesis` (ADR-0086). Unlike
    the streaming turn, ``run_synthesis`` does not *execute* the tool
    calls — it hands the parsed ``surface_finding`` invocations back to
    the caller (the Irene beat handler), which decides what to persist.

    Attributes:
        tool_calls: One entry per tool call the model made, each a dict
            ``{"name": str, "arguments": dict, "id": str}`` with
            ``arguments`` already JSON-decoded (an empty dict on a
            malformed argument string). An **empty list means silence** —
            the "nothing material" outcome ``tool_choice="auto"`` yields
            natively.
        raw_text: The assistant message's text content (``""`` when the
            model returned only tool calls or null content).
    """

    tool_calls: list[dict[str, Any]]
    raw_text: str


_instance: AIServiceCore | None = None


class AIServiceCore:
    """Qt-free asyncio core of the AIService.

    Responsibilities:

    * OpenAI / OpenRouter client construction (``openai.AsyncOpenAI``).
    * The full tool-execution loop, structurally unchanged from the
      legacy ``_StreamWorker.run`` per ADR-0038's "lift the loop
      unchanged" rule.
    * Soul-identity injection from ``docs/Soul_Shirley.md`` (see
      :meth:`get_system_prompt`).
    * ``ToolRegistry`` integration with the ADR-0022 ``begin_turn`` /
      ``end_turn`` brackets.
    * Synchronous one-shot extraction (:meth:`send_one_shot_extraction`)
      for callers that want a complete response without streaming —
      either on a per-call :class:`ResolvedLLM` (the Report Scraper, per
      tenant, ADR-0112 §4b / ADR-0123) or on the parked singleton
      credentials (the Fetcher-LLM, which has no tenant context to
      resolve in).

    What this class does *not* do:

    * No Qt signals, no ``QObject``, no ``QThread``. The Qt adapter
      lives in :mod:`services.ai_service_qt`.
    * No persistence. The legacy ``QSettings``-based credential storage
      is preserved on the adapter side; the core never reads or writes
      ``QSettings``.
    * No GUI-thread management. Each adapter is responsible for
      delivering events to its own event loop.

    Singleton:
        Use :func:`get_ai_service_core` to obtain the
        application-wide instance. Direct instantiation is permitted
        for tests but contradicts the ADR-0010 pattern in
        application code.
    """

    def __init__(self) -> None:
        """Initialise the core and register default tools.

        Default tools are imported lazily here (their ``register_tool``
        calls run at import time). Importing the tool modules
        transitively pulls in :mod:`services.web_research`, which is
        why ``services/web_research/service.py`` must import from this
        module rather than from the legacy ``services.ai_service``
        shim — otherwise the cycle would re-introduce PyQt6 into the
        core's import graph.

        Endpoint credentials are stored as plain attributes; the
        ``openai.AsyncOpenAI`` client is constructed *per call* inside
        each async method. That avoids the cross-thread / cross-loop
        sharing hazard that would otherwise arise when the Qt adapter
        spawns a fresh ``QThread`` (with its own ``asyncio.run`` loop)
        for every ``send_message`` invocation: an ``httpx.AsyncClient``
        is bound to the loop in which it was created, so handing one
        across threads is undefined behaviour.
        """
        self._base_url: str | None = None
        self._api_key: str | None = None
        self._status: ConnectionStatus = ConnectionStatus.DISCONNECTED
        self._available_models: list[str] = []
        self._active_model: str = ""
        self._register_default_tools()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def configure(self, base_url: str, api_key: str) -> None:
        """Store the endpoint credentials.

        No I/O is performed; the actual ``openai.AsyncOpenAI`` client
        is constructed per-call inside the async methods (see the
        class docstring for why). Async work — the model-list fetch
        — happens in :meth:`fetch_models`.

        Args:
            base_url: The API base URL (e.g.
                ``"https://openrouter.ai/api/v1"``).
            api_key: The API key for authentication.
        """
        self._base_url = base_url
        self._api_key = api_key
        logger.info("AIServiceCore.configure: credentials stored for %s", base_url)

    def _make_async_client(self, llm: ResolvedLLM | None = None) -> openai.AsyncOpenAI:
        """Construct a fresh ``AsyncOpenAI`` client for this call.

        Called once per async method invocation. The client is
        short-lived and bound to the current asyncio loop. ``httpx``
        intercepts at the transport layer, so ``pytest-httpx`` mocks
        work transparently with this pattern.

        Args:
            llm: A per-turn resolution (ADR-0112 §4b). When given, the
                client is built from *its* endpoint and key and the
                singleton's stored credentials are not consulted at all.
                ``None`` is the singleton path — the Qt/GUI flow and the
                one-shot extraction consumers — and behaves exactly as it
                always has.

        Returns:
            A new ``openai.AsyncOpenAI`` instance.

        Raises:
            RuntimeError: If ``llm`` is ``None`` and :meth:`configure` has
                not been called.
        """
        if llm is not None:
            return llm.make_client()
        if self._base_url is None or self._api_key is None:
            raise RuntimeError("AIServiceCore: not configured.")
        return openai.AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)

    async def fetch_models(self) -> list[str]:
        """Fetch the model list from the configured endpoint.

        Returns:
            A sorted list of model ID strings.

        Raises:
            RuntimeError: If :meth:`configure` has not been called.
            openai.OpenAIError: Any error class the SDK raises on
                connection / auth / status failure. Callers translate
                these to channel-appropriate error events.
        """
        client = self._make_async_client()
        try:
            page = await client.models.list()
        finally:
            await client.close()
        ids = sorted(m.id for m in page.data)
        logger.debug("AIServiceCore.fetch_models: fetched %d models.", len(ids))
        return ids

    def reset(self) -> None:
        """Forget endpoint credentials and clear cached model list / status.

        Equivalent to the legacy ``AIService.disconnect`` minus the Qt
        signal emission.
        """
        self._base_url = None
        self._api_key = None
        self._available_models = []
        self._active_model = ""
        self._status = ConnectionStatus.DISCONNECTED
        logger.info("AIServiceCore.reset: credentials cleared.")

    # ------------------------------------------------------------------
    # State accessors / mutators (called by the adapter)
    # ------------------------------------------------------------------

    def get_status(self) -> ConnectionStatus:
        """Return the current connection status."""
        return self._status

    def set_status(self, status: ConnectionStatus) -> None:
        """Update the connection status.

        The adapter is responsible for emitting the corresponding
        ``connection_status_changed`` signal.

        Args:
            status: The new status.
        """
        self._status = status
        logger.debug("AIServiceCore.set_status: %s", status.value)

    def get_available_models(self) -> list[str]:
        """Return the cached list of model IDs from the last fetch."""
        return list(self._available_models)

    def set_available_models(self, model_ids: list[str]) -> None:
        """Cache a model list.

        Args:
            model_ids: Sorted list of model ID strings.
        """
        self._available_models = list(model_ids)

    def set_model(self, model_id: str) -> None:
        """Set the active model.

        Args:
            model_id: The model identifier (e.g. ``"openai/gpt-4o"``).
        """
        self._active_model = model_id
        logger.debug("AIServiceCore.set_model: model='%s'", model_id)

    def get_model(self) -> str:
        """Return the currently selected model ID, or ``""``."""
        return self._active_model

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_response(
        self,
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: ToolExecutionContext | None = None,
        *,
        llm: ResolvedLLM | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Drive one Shirley turn end-to-end, yielding :class:`StreamEvent`.

        The control flow mirrors the legacy ``_StreamWorker.run``
        verbatim per ADR-0038's "lift the loop unchanged" rule:

        * Bracket the turn in ``ToolRegistry.begin_turn()`` /
          ``end_turn()`` (ADR-0022).
        * Stream a chat completion; accumulate text deltas and tool
          calls indexed by ``tc_delta.index``.
        * On ``finish_reason == "tool_calls"``, dispatch each tool
          call sequentially (in declared index order — ADR-0038 §3),
          detect chart-artefact envelopes, append the LLM-bound
          tool-result message, and continue the loop.
        * On ``stop`` / other finish reasons, yield ``stream_finished``
          with the assembled :class:`Message` and return.
        * Honour the iteration cap (:data:`_MAX_TOOL_ITERATIONS`).

        Errors are surfaced as ``error`` events; the generator returns
        without yielding ``stream_finished`` when an error path is
        taken. ``end_turn`` always runs via the ``finally`` block.

        Args:
            conversation: The full conversation, serialised through
                :meth:`Conversation.to_openai_messages`.
            system_prompt: Optional system prompt prepended to the
                outgoing message list. Empty string means no system
                message is added.
            temperature: Sampling temperature.
            tool_context: Optional per-turn tool-execution context. When
                provided it is set under ``_TURN_LOCK`` at the start of
                the turn and cleared (along with the turn-scoped data
                cache) in a ``finally`` when the turn ends — so the
                Postgres-native tools resolve the right tenant and no
                surface can race another on the shared module-level
                context. ``None`` (the GUI / Qt path) leaves the context
                unset and the tools degrade gracefully.
            llm: This turn's resolved endpoint, credential and model
                (ADR-0112 §4b). When given, the client and the model come
                from it and the singleton's stored triple and
                :class:`ConnectionStatus` are **not** consulted — that is
                what lets one process serve many tenants without a
                configure-once global. ``None`` keeps the singleton
                behaviour verbatim: the Qt / GUI path, where
                :meth:`configure` / :meth:`set_model` / :meth:`set_status`
                remain the seam.

        Yields:
            :class:`StreamEvent` records describing the turn.
        """
        if llm is None:
            if (
                self._base_url is None
                or self._api_key is None
                or self._status != ConnectionStatus.CONNECTED
            ):
                yield StreamEvent(
                    "error",
                    {"message": "Not connected. Call configure() first."},
                )
                return
            if not self._active_model:
                yield StreamEvent(
                    "error",
                    {"message": "No model selected. Call set_model() first."},
                )
                return
        elif not llm.model:
            # A resolution carrying no model cannot drive a turn. The
            # consumers fail loudly long before they get here (ADR-0112
            # §4b); this is the backstop that keeps a malformed resolution
            # from reaching the API as ``model=""``.
            yield StreamEvent(
                "error",
                {"message": "No model resolved for this turn."},
            )
            return

        from services.tool_registry import get_tool_registry

        # Acquire the process-wide turn lock before any ToolRegistry
        # mutation, and hold it across every yield until the generator
        # finishes or is closed. The acquire runs via
        # ``asyncio.to_thread`` so a turn that has to *wait* for the
        # lock parks that wait on a worker thread instead of freezing
        # the uvicorn event loop — see the module docstring
        # "Concurrency" section. ``release()`` is non-blocking and runs
        # directly; the ``try/finally`` guarantees the lock is freed
        # even if ``_stream_response_locked`` raises or the generator
        # is closed mid-stream.
        await asyncio.to_thread(_TURN_LOCK.acquire)
        try:
            async for event in self._stream_response_locked(
                conversation,
                system_prompt,
                temperature,
                get_tool_registry(),
                tool_context,
                llm=llm,
            ):
                yield event
        finally:
            _TURN_LOCK.release()

    async def _stream_response_locked(
        self,
        conversation: Conversation,
        system_prompt: str,
        temperature: float,
        tool_reg: Any,
        tool_context: ToolExecutionContext | None = None,
        *,
        llm: ResolvedLLM | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Body of :meth:`stream_response` assuming the turn lock is held.

        Split out so the lock-acquisition site stays small and easy to
        audit, mirroring the ``_run_turn_locked`` shape from the
        retired ``services.headless_shirley.run_turn``.

        Owns the tool-execution context lifecycle: when ``tool_context``
        is provided it is set here — under ``_TURN_LOCK``, where exactly
        one turn is ever active — before the turn begins, and the
        context and the turn-scoped data cache are cleared in a
        ``finally`` when the turn ends. Setting and clearing here (rather
        than having the chat route bracket it outside the lock) is what
        lets the web chat surface and the Telegram bot share this
        process-wide context without racing on it.

        ``llm`` is this turn's resolution (ADR-0112 §4b): it supplies both
        the client and the model, so nothing below reads ``_active_model``
        or the stored credentials. ``None`` falls back to the singleton's
        state — the Qt / GUI path, unchanged.
        """
        if tool_context is not None:
            set_tool_context(tool_context)
        active_model = llm.model if llm is not None else self._active_model
        try:
            tool_reg.begin_turn()
            client = self._make_async_client(llm)
            try:
                tool_defs = tool_reg.get_tool_definitions() or None
                messages: list[dict[str, Any]] = conversation.to_openai_messages()
                if system_prompt:
                    messages = [{"role": "system", "content": system_prompt}, *messages]
                iteration = 0
                accumulated = ""

                while iteration < _MAX_TOOL_ITERATIONS:
                    accumulated = ""
                    tool_calls_raw: list[dict[str, Any]] = []
                    finish_reason: str | None = None
                    stripper = _StopTokenStripper()

                    try:
                        kwargs: dict[str, Any] = {
                            "model": active_model,
                            "messages": messages,
                            "temperature": temperature,
                            "stream": True,
                        }
                        if tool_defs:
                            kwargs["tools"] = tool_defs

                        stream = await client.chat.completions.create(**kwargs)
                        async for delta_chunk in stream:
                            choice = delta_chunk.choices[0] if delta_chunk.choices else None
                            if choice is None:
                                continue
                            delta = choice.delta

                            if delta.content:
                                safe = stripper.process(delta.content)
                                if safe:
                                    accumulated += safe
                                    yield StreamEvent("chunk", {"text": safe})

                            if delta.tool_calls:
                                for tc_delta in delta.tool_calls:
                                    idx = tc_delta.index
                                    while len(tool_calls_raw) <= idx:
                                        tool_calls_raw.append(
                                            {"id": "", "name": "", "arguments": ""}
                                        )
                                    if tc_delta.id:
                                        tool_calls_raw[idx]["id"] += tc_delta.id
                                    if tc_delta.function and tc_delta.function.name:
                                        tool_calls_raw[idx]["name"] += tc_delta.function.name
                                    if tc_delta.function and tc_delta.function.arguments:
                                        tool_calls_raw[idx]["arguments"] += (
                                            tc_delta.function.arguments
                                        )

                            if choice.finish_reason:
                                finish_reason = choice.finish_reason

                    except openai.AuthenticationError as exc:
                        yield StreamEvent(
                            "error",
                            {"message": (f"Authentication failed — check your API key. ({exc})")},
                        )
                        return
                    except openai.RateLimitError as exc:
                        detail = getattr(exc, "message", str(exc))
                        yield StreamEvent(
                            "error",
                            {
                                "message": (
                                    "Rate limit exceeded — try a different model or "
                                    f"wait a moment. ({detail})"
                                )
                            },
                        )
                        return
                    except openai.APIConnectionError as exc:
                        yield StreamEvent(
                            "error",
                            {"message": f"Connection lost during streaming: {exc}"},
                        )
                        return
                    except openai.APIStatusError as exc:
                        detail = getattr(exc, "message", str(exc))
                        yield StreamEvent(
                            "error",
                            {"message": f"API error {exc.status_code}: {detail}"},
                        )
                        return
                    except Exception as exc:  # noqa: BLE001 — surface unexpected errors
                        yield StreamEvent(
                            "error",
                            {"message": f"Unexpected error during streaming: {exc}"},
                        )
                        return

                    # Flush any pending stop-token tail. If a partial token
                    # was being held back at end-of-stream, the buffered
                    # tail turned out to be benign tail-text — release it
                    # so the consumer sees the full final word.
                    tail = stripper.flush()
                    if tail:
                        accumulated += tail
                        yield StreamEvent("chunk", {"text": tail})

                    # --- Tool-call branch ---------------------------------
                    if finish_reason == "tool_calls" and tool_calls_raw:
                        assistant_msg: dict[str, Any] = {
                            "role": "assistant",
                            "content": accumulated or None,
                            "tool_calls": [
                                {
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": tc["arguments"],
                                    },
                                }
                                for tc in tool_calls_raw
                            ],
                        }
                        messages.append(assistant_msg)

                        for tc in tool_calls_raw:
                            yield StreamEvent(
                                "tool_called",
                                {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                    "tool_call_id": tc["id"],
                                },
                            )
                            try:
                                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                            except json.JSONDecodeError:
                                args = {}

                            # Run the tool off the event loop.
                            # ``execute_tool`` is synchronous and may block:
                            # the Postgres-native tools drive a DB workflow
                            # via ``run_async_in_fresh_loop``, whose
                            # internal ``Thread.join`` would otherwise
                            # freeze the uvicorn loop for the workflow's
                            # whole duration. ``asyncio.to_thread`` keeps
                            # the loop free to service other SSE turns (and
                            # signal handling) while the tool runs.
                            result = await asyncio.to_thread(
                                tool_reg.execute_tool, tc["name"], args
                            )

                            # Chart-artefact detection — envelope shape
                            # produced by ``services.tools.chart_tools``.
                            # Two formats coexist: ``generate_chart`` emits
                            # ``chart_format="png"`` with ``image_base64``
                            # (the GUI path); ``render_chart`` emits
                            # ``chart_format="plotly"`` with ``spec`` (the
                            # web path, ADR-0048). Both keys are always
                            # forwarded so every adapter can branch cleanly;
                            # a missing ``chart_format`` is treated as png.
                            # The large payload (image or spec) is stripped
                            # here — the LLM only ever sees ``llm_response``.
                            llm_text = result
                            try:
                                parsed = json.loads(result)
                                if (
                                    isinstance(parsed, dict)
                                    and parsed.get("__artifact__") == "chart"
                                ):
                                    yield StreamEvent(
                                        "chart_artifact",
                                        {
                                            "chart_format": parsed.get("chart_format", "png"),
                                            "image_base64": parsed.get("image_base64", ""),
                                            "spec": parsed.get("spec"),
                                            "caption": parsed.get("caption", ""),
                                        },
                                    )
                                    llm_text = parsed.get("llm_response", "Chart generated.")
                            except (json.JSONDecodeError, KeyError, TypeError):
                                pass

                            yield StreamEvent(
                                "tool_completed",
                                {
                                    "name": tc["name"],
                                    "result": llm_text,
                                    "tool_call_id": tc["id"],
                                },
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": llm_text,
                                }
                            )

                        iteration += 1
                        if iteration >= _MAX_TOOL_ITERATIONS:
                            yield StreamEvent(
                                "chunk",
                                {
                                    "text": (
                                        "\n\n[Tool call limit reached. Please rephrase "
                                        "or break into smaller steps.]"
                                    )
                                },
                            )
                            # fall through to emit final stream_finished
                        else:
                            continue

                    # --- Final response -----------------------------------
                    tool_calls: list[ToolCall] = []
                    for raw in tool_calls_raw:
                        try:
                            args = json.loads(raw["arguments"]) if raw["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {"_raw": raw["arguments"]}
                        tool_calls.append(ToolCall(id=raw["id"], name=raw["name"], arguments=args))

                    msg = Message(
                        role=MessageRole.ASSISTANT,
                        content=accumulated,
                        model=active_model,
                        tool_calls=tool_calls,
                    )
                    logger.debug(
                        "AIServiceCore.stream_response: complete — %d chars, "
                        "%d tool calls, %d iterations.",
                        len(accumulated),
                        len(tool_calls),
                        iteration,
                    )
                    yield StreamEvent(
                        "stream_finished",
                        {"message": msg, "iterations": iteration},
                    )
                    return

                # Safety net — only reachable if the iteration cap is the
                # exit path *and* the loop hits the while-condition check
                # before yielding the final message above.
                msg = Message(
                    role=MessageRole.ASSISTANT,
                    content=accumulated,
                    model=active_model,
                )
                yield StreamEvent(
                    "stream_finished",
                    {"message": msg, "iterations": iteration},
                )
            finally:
                tool_reg.end_turn()
                await client.close()
        finally:
            clear_tool_context()
            clear_tool_data()

    # ------------------------------------------------------------------
    # Synchronous one-shot extraction
    # ------------------------------------------------------------------

    def send_one_shot_extraction(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        *,
        llm: ResolvedLLM | None = None,
    ) -> str:
        """Synchronous, non-streaming, no-tools extraction call.

        Same contract as the legacy
        ``AIService.send_one_shot_extraction``: the request body never
        carries a ``tools`` key, the ``ToolRegistry`` is never queried,
        the response content is returned as a string. Internally this
        wraps the async path in :func:`asyncio.run`, so the request
        traverses ``openai.AsyncOpenAI`` but the public contract stays
        synchronous for the existing scraper / web-research consumers.

        When the calling thread already has a live asyncio event loop
        (FastAPI request handler, Qt ``_StreamWorker`` after ADR-0038,
        any other async-driven consumer), the coroutine is dispatched
        to a fresh daemon thread that runs its own ``asyncio.run`` and
        the calling thread blocks on the join. This preserves the
        synchronous contract without colliding with the caller's loop.

        **Two paths, the same ``llm | model`` contract as**
        :meth:`run_synthesis` (ADR-0112 §4b, ADR-0123): exactly one of the
        two is given. With ``llm`` the client comes from
        :meth:`ResolvedLLM.make_client` and the model from ``llm.model``,
        and the singleton's stored triple and :class:`ConnectionStatus` are
        **not** consulted — that is what lets the Report Scraper extract on
        the requesting tenant's own credential. With ``model`` alone the
        singleton path behaves verbatim as it always has, which is what
        keeps the Fetcher-LLM (``services/web_research``) working off the
        application-scope credentials ``web/main.py`` parks.

        Args:
            messages: OpenAI-format message list. Sent verbatim.
            model: Model ID for this call. Required on the singleton path;
                **forbidden** together with ``llm``, which carries its own
                model — two sources of truth for one call is exactly the
                drift ADR-0112 §4b removes.
            temperature: Sampling temperature (default ``0.0``).
            timeout: Per-request timeout in seconds (default ``120``).
            llm: This call's resolved endpoint, credential and model
                (ADR-0112 §4b, ADR-0123). When given, the client **and** the
                model come from it and the singleton is not consulted at all.

        Returns:
            The assistant's response content; empty string when the
            API returns null content.

        Raises:
            RuntimeError: If ``llm`` is ``None`` and the core is not
                connected.
            ValueError: If both ``llm`` and ``model`` are given, or neither.
            openai.OpenAIError: Any error class the SDK raises.
        """
        if llm is not None:
            if model is not None:
                raise ValueError(
                    "AIServiceCore.send_one_shot_extraction: pass either `llm` or "
                    "`model`, not both — `llm` carries the model (ADR-0112 §4b)."
                )
        else:
            if (
                self._base_url is None
                or self._api_key is None
                or self._status != ConnectionStatus.CONNECTED
            ):
                raise RuntimeError("AIServiceCore not connected. Call configure() first.")
            if model is None:
                raise ValueError(
                    "AIServiceCore.send_one_shot_extraction: `model` is required "
                    "on the singleton path (no `llm` given)."
                )

        def _run_in_new_loop() -> str:
            return asyncio.run(
                self._send_one_shot_extraction_async(messages, model, temperature, timeout, llm)
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No live loop on this thread — direct path.
            return _run_in_new_loop()

        # A live loop is running on this thread. Scheduling back onto it
        # while we synchronously join would deadlock, so spawn a fresh
        # daemon thread that owns its own loop, capture the result or
        # any raised exception, and re-raise on the caller's thread so
        # callers (WebResearchService, scraper) see the SDK error class
        # verbatim.
        container: dict[str, Any] = {}

        def _runner() -> None:
            try:
                container["result"] = _run_in_new_loop()
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                container["error"] = exc

        worker = threading.Thread(
            target=_runner,
            name="AIServiceCore.one-shot-extraction",
            daemon=True,
        )
        worker.start()
        worker.join()

        if "error" in container:
            raise container["error"]
        return container["result"]

    async def _send_one_shot_extraction_async(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        temperature: float,
        timeout: float,
        llm: ResolvedLLM | None = None,
    ) -> str:
        """Async implementation of :meth:`send_one_shot_extraction`.

        ``llm`` is this call's resolution (ADR-0112 §4b, ADR-0123): it
        supplies both the client and the model, so nothing below reads the
        singleton's stored credentials. ``None`` falls back to the singleton
        — the Fetcher-LLM path, unchanged. The caller has already enforced
        that exactly one of ``llm`` / ``model`` is set.
        """
        active_model = llm.model if llm is not None else model
        client = self._make_async_client(llm)
        try:
            kwargs: dict[str, Any] = {
                "model": active_model,
                "messages": messages,
                "temperature": temperature,
                "timeout": timeout,
            }
            logger.debug(
                "AIServiceCore.send_one_shot_extraction: model=%s, %d messages",
                active_model,
                len(messages),
            )
            response = await client.chat.completions.create(**kwargs)
        finally:
            await client.close()
        content = response.choices[0].message.content
        result = content if content is not None else ""
        logger.debug(
            "AIServiceCore.send_one_shot_extraction: returned %d chars",
            len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Non-streaming structured synthesis (Irene / ADR-0086)
    # ------------------------------------------------------------------

    async def run_synthesis(
        self,
        *,
        system_prompt: str,
        context_messages: list[dict[str, Any]],
        tool: dict[str, Any],
        model: str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        llm: ResolvedLLM | None = None,
    ) -> SynthesisResult:
        """Issue one non-streaming, structured synthesis call (ADR-0086).

        Irene's heartbeat is structurally unlike Shirley's turn: it needs
        a **single, non-streaming** completion offering exactly one tool
        (``surface_finding``) with ``tool_choice="auto"`` so that zero
        tool calls falls out natively as *silence*. This method is that
        entry point.

        It deliberately does **not** reuse :meth:`stream_response`:

        * **No** ``_TURN_LOCK``. The beat runs in a separate process (the
          tick CLI), so the process-wide turn lock is irrelevant by
          construction — this method never touches it, and Shirley's
          streaming path is entirely unaffected.
        * **No** iteration loop and **no** tool *execution*. The parsed
          ``surface_finding`` invocations are returned to the caller; the
          Irene beat persists findings itself (they are not a tool
          side-effect).

        Message assembly mirrors :meth:`_stream_response_locked`: the
        system prompt (when non-empty) is prepended to the context turns.

        Args:
            system_prompt: The Irene system prompt. Prepended as the first
                ``system`` message when non-empty.
            context_messages: The world-state turns to synthesise over, in
                OpenAI message shape. Sent verbatim after the system
                message.
            tool: The OpenAI function-tool dict to offer — for Irene, the
                ``surface_finding`` schema. Passed as the sole entry of
                the request ``tools`` array with ``tool_choice="auto"``.
            model: Model id for this call (Irene's model, which need not
                equal Shirley's — see ``IRENE_MODEL``). Required on the
                singleton path; **forbidden** together with ``llm``, which
                carries its own model — two sources of truth for one call
                is exactly the drift ADR-0112 §4b removes.
            temperature: Sampling temperature (default ``0.0`` — synthesis
                is deterministic).
            timeout: Per-request timeout in seconds.
            llm: This beat's resolved endpoint, credential and model
                (ADR-0112 §4b). When given, the client **and** the model
                come from it and the singleton's stored triple and
                :class:`ConnectionStatus` are not consulted — which is what
                lets one tick beat many tenants on their own credentials.

        Returns:
            A :class:`SynthesisResult`; an empty ``tool_calls`` list is
            the silence outcome.

        Raises:
            RuntimeError: If ``llm`` is ``None`` and the core is not
                configured / connected.
            ValueError: If both ``llm`` and ``model`` are given, or neither.
            openai.OpenAIError: Any error class the SDK raises on
                connection / auth / status failure. The caller (the beat
                handler) catches these so one tenant's LLM failure does
                not abort the whole tick.
        """
        if llm is not None:
            if model is not None:
                raise ValueError(
                    "AIServiceCore.run_synthesis: pass either `llm` or `model`, "
                    "not both — `llm` carries the model (ADR-0112 §4b)."
                )
            active_model = llm.model
        else:
            if (
                self._base_url is None
                or self._api_key is None
                or self._status != ConnectionStatus.CONNECTED
            ):
                raise RuntimeError("AIServiceCore not connected. Call configure() first.")
            if model is None:
                raise ValueError(
                    "AIServiceCore.run_synthesis: `model` is required on the "
                    "singleton path (no `llm` given)."
                )
            active_model = model

        messages: list[dict[str, Any]] = list(context_messages)
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}, *messages]

        client = self._make_async_client(llm)
        try:
            logger.debug(
                "AIServiceCore.run_synthesis: model=%s, %d context messages",
                active_model,
                len(context_messages),
            )
            response = await client.chat.completions.create(
                model=active_model,
                messages=messages,
                tools=[tool],
                tool_choice="auto",
                temperature=temperature,
                timeout=timeout,
                stream=False,
            )
        finally:
            await client.close()

        message = response.choices[0].message
        parsed: list[dict[str, Any]] = []
        for tc in message.tool_calls or []:
            raw_args = tc.function.arguments
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}
            parsed.append({"name": tc.function.name, "arguments": args, "id": tc.id})

        logger.debug(
            "AIServiceCore.run_synthesis: %d tool call(s) returned.",
            len(parsed),
        )
        return SynthesisResult(tool_calls=parsed, raw_text=message.content or "")

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        """Load a system prompt from ``docs/Soul_<Name>.md``.

        Identical parsing rules to the legacy
        ``AIService.get_system_prompt``: extract the content between
        the first triple-backtick fence pair, then append any
        companion context files. Falls back to a minimal default if
        the soul file is missing or malformed.

        Args:
            prompt_name: Base name of the soul file. Default
                ``"shirley"`` resolves to ``docs/Soul_Shirley.md``.

        Returns:
            The composed system prompt string.
        """
        soul_path = _REPO_ROOT / "docs" / f"Soul_{prompt_name.capitalize()}.md"
        fallback = "You are Shirley, an AI assistant for institutional portfolio management."
        if not soul_path.exists():
            logger.warning(
                "AIServiceCore.get_system_prompt: '%s' not found, using fallback.",
                soul_path,
            )
            return fallback

        try:
            text = soul_path.read_text(encoding="utf-8")
            first = text.find("```")
            if first == -1:
                logger.warning(
                    "AIServiceCore.get_system_prompt: no ``` fence in '%s', using fallback.",
                    soul_path,
                )
                return fallback
            start = text.find("\n", first) + 1
            end = text.find("```", start)
            if end == -1:
                logger.warning(
                    "AIServiceCore.get_system_prompt: unclosed ``` fence in '%s', using fallback.",
                    soul_path,
                )
                return fallback
            prompt = text[start:end].strip()
            if not prompt:
                logger.warning(
                    "AIServiceCore.get_system_prompt: empty fence block in '%s', using fallback.",
                    soul_path,
                )
                return fallback

            # Ground the prompt in the live ToolRegistry: inject the generated
            # tool inventory after the Soul content and before the hand-authored
            # orchestration context that references the tools — "here are your
            # tools" first, then the heuristics for using them (ADR-0012, B8).
            # A registry failure degrades to the un-grounded prompt;
            # ``_render_tool_inventory`` never raises.
            inventory = self._render_tool_inventory()
            if inventory:
                prompt = prompt + "\n\n" + inventory

            context_files = [
                _REPO_ROOT / "docs" / "Shirley_AnalysisResults_Context.md",
                _REPO_ROOT / "docs" / "Shirley_ToolOrchestration_Context.md",
            ]
            for ctx_path in context_files:
                if ctx_path.exists():
                    try:
                        ctx_text = ctx_path.read_text(encoding="utf-8").strip()
                        if ctx_text:
                            prompt = prompt + "\n\n" + ctx_text
                    except OSError as exc:
                        logger.warning(
                            "AIServiceCore.get_system_prompt: could not read context file '%s': %s",
                            ctx_path,
                            exc,
                        )
            return prompt
        except OSError as exc:
            logger.error(
                "AIServiceCore.get_system_prompt: could not read '%s': %s",
                soul_path,
                exc,
            )
            return fallback

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _render_tool_inventory(self) -> str:
        """Render a Markdown inventory of the currently registered tools.

        The :class:`~services.tool_registry.ToolRegistry` is the single
        source of truth for which tools exist and what they do (ADR-0012).
        This block is generated at prompt-assembly time and injected into the
        system prompt so the prompt's tool list can never drift from the tools
        actually exposed to the model via the API ``tools`` field — the very
        drift B8 removes by construction.

        Each line carries the tool name, the first sentence of its description
        (kept short — the full description still reaches the model through the
        API ``tools`` field), and its declared
        :class:`~services.tool_classes.ToolClass`. Ordering follows registry
        insertion order, which is deterministic.

        Returns:
            A Markdown section headed ``## Your currently available tools``,
            or an empty string if the registry is empty or cannot be read.
            Never raises — a registry failure degrades the prompt to its
            un-grounded form.
        """
        try:
            from services.tool_registry import get_tool_registry

            registry = get_tool_registry()
            tool_defs = registry.get_tool_definitions()
        except Exception as exc:  # noqa: BLE001 — grounding must never break prompt assembly
            logger.warning(
                "AIServiceCore._render_tool_inventory: could not read the tool "
                "registry; system prompt will be un-grounded: %s",
                exc,
            )
            return ""

        if not tool_defs:
            return ""

        lines = ["## Your currently available tools", ""]
        for tool_def in tool_defs:
            fn = tool_def.get("function", {})
            name = fn.get("name")
            if not name:
                continue
            first_sentence = self._first_sentence(fn.get("description") or "")
            try:
                trust_suffix = f" _({registry.get_tool_class(name).value})_"
            except Exception:  # noqa: BLE001 — trust annotation is best-effort
                trust_suffix = ""
            if first_sentence:
                lines.append(f"- **{name}** — {first_sentence}{trust_suffix}")
            else:
                lines.append(f"- **{name}**{trust_suffix}")
        return "\n".join(lines)

    @staticmethod
    def _first_sentence(description: str) -> str:
        """Return the leading sentence of a tool description.

        Collapses internal whitespace (descriptions may span several lines)
        and returns the text up to and including the first sentence-final
        period. Keeps the inventory block compact while the full description
        still reaches the model via the API ``tools`` field.

        Args:
            description: The full tool description.

        Returns:
            The first sentence, or the whole collapsed string (with a trailing
            period appended) if it contains no sentence break. Empty string for
            empty input.
        """
        flat = " ".join(description.split())
        if not flat:
            return ""
        idx = flat.find(". ")
        if idx != -1:
            return flat[: idx + 1]
        return flat if flat.endswith(".") else flat + "."

    def _register_default_tools(self) -> None:
        """Import tool modules to trigger their registry.register_tool calls.

        Superset of the legacy ``AIService._register_default_tools``:
        the legacy adapter registered three tool modules; the Qt-free
        core adds a fourth, ``investment_tools`` (the Postgres-native
        investment read tools for the web chat surface, ADR-0047), and a
        fifth, ``analysis_tools`` (the back-office analysis tools —
        limit coverage, the SAA-hypothetical comparison, and portfolio
        statistics, ADR-0069). Both Postgres-native modules are imported
        here but only become *reachable* once the chat route populates
        the tool-execution context — on the GUI path they degrade
        gracefully (see ``services/tools/investment_tools.py`` and
        ``services/tools/analysis_tools.py``).

        Imports happen at first instance construction; Python's import
        cache prevents re-registration on subsequent instantiations.
        """
        import services.tools.datastore_tools
        import services.tools.chart_tools
        import services.tools.web_research_tool
        import services.tools.investment_tools
        import services.tools.analysis_tools  # noqa: F401

        logger.info("AIServiceCore: default tools registered with ToolRegistry.")


def get_ai_service_core() -> AIServiceCore:
    """Return the application-wide :class:`AIServiceCore` singleton.

    Lazy on first call; thereafter every call returns the same
    instance. Mirrors the singleton lifecycle of the legacy
    :func:`services.ai_service.get_ai_service`.

    Returns:
        The shared :class:`AIServiceCore` instance.
    """
    global _instance
    if _instance is None:
        _instance = AIServiceCore()
        logger.debug("AIServiceCore singleton created.")
    return _instance


__all__ = [
    "AIServiceCore",
    "EventType",
    "ResolvedLLM",
    "StreamEvent",
    "SynthesisResult",
    "get_ai_service_core",
]
