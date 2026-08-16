# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Embedding interface for Irene's RSS clustering (ADR-0087 Part B).

The deterministic bucketing engine (:mod:`services.irene.rss_clustering`)
needs to turn feed-item text into vectors so it can group semantically
similar items. That is the *only* model call in the whole RSS path, and
it is injected through the small :class:`Embedder` interface defined here
— so tests drive a fixed-vector stub (no network) and production drives
:class:`OpenRouterEmbedder`, which reaches the pinned embedding model over
the **same** OpenRouter client the beat's synthesis call uses.

Design (ADR-0087 Part B, option E1):

* One HTTP stack, one credential path. :class:`OpenRouterEmbedder` is handed
  a **client factory** — in production
  :meth:`services.ai_service_core.ResolvedLLM.make_client`, the same
  ``openai.AsyncOpenAI`` construction the beat's synthesis call uses, bound
  to the tenant's own ``base_url`` / ``api_key``. No second client, no new
  credential, and no reach into a core's privates. Since ADR-0112 §4b the
  credential is resolved *per tenant*, so the tick builds one embedder per
  tenant: a tenant's key vectorises only that tenant's beat.
* The model id is not baked into the embedder; it is passed per call from
  the auditable ``DeltaThresholds.embedding_model`` so a config change is
  a single, logged edit that flows straight into every vectorisation.
* Vectors never touch the key-former. The engine vectorises here, clusters
  by cosine similarity, then hands the cluster *membership* (stable item
  identities, not vectors) to
  :func:`services.analytics.rss_bucketing.form_subject_key`.

Layering: this module lives under ``services/`` and imports from nothing
inside the project — the client arrives as an injected factory. It is
Qt-free and holds no DB access.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)


@runtime_checkable
class Embedder(Protocol):
    """The injectable vectorisation seam for RSS clustering.

    A single async method that turns a batch of texts into a list of
    float vectors (one per input, same order). Implementations must be
    deterministic for a given ``(text, model)`` pair — the clustering
    engine relies on stable vectors for a stable membership, and hence a
    stable key.
    """

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        """Return one vector per input text, in input order."""
        ...


class OpenRouterEmbedder:
    """:class:`Embedder` backed by an OpenRouter client factory.

    Builds a short-lived ``openai.AsyncOpenAI`` per batch from the injected
    factory, so the embedding call rides the same OpenRouter base-url /
    api-key seam the beat's synthesis call does (no second HTTP stack).
    Results are memoised per ``(model, text)`` for the lifetime of the
    instance, so one beat never re-vectorises the same feed item twice.

    Instance lifetime is the **credential's** lifetime: since ADR-0112 §4b
    the tick constructs one embedder per tenant, from that tenant's
    resolution. The memo therefore spans one tenant's beat rather than the
    whole tick — the deliberate cost of never letting one tenant's key
    serve another's vectorisation.
    """

    def __init__(self, client_factory: Callable[[], Any]) -> None:
        """Bind the embedder to a client factory.

        Args:
            client_factory: A zero-argument callable returning a fresh
                ``openai.AsyncOpenAI`` bound to the credential this
                embedder should use — in production
                :meth:`services.ai_service_core.ResolvedLLM.make_client`.
                Called once per batch that has cache misses; the embedder
                closes what it opens.
        """
        self._client_factory = client_factory
        self._cache: dict[tuple[str, str], list[float]] = {}

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        """Vectorise ``texts`` with ``model`` via the OpenRouter client.

        Only texts not already cached for this ``model`` are sent; the
        results are returned in input order (cache hits included).

        Args:
            texts: The batch of texts to vectorise.
            model: The pinned embedding model id (from
                ``DeltaThresholds.embedding_model``).

        Returns:
            One vector per input text, in the same order.
        """
        texts = list(texts)
        missing = [t for t in dict.fromkeys(texts) if (model, t) not in self._cache]
        if missing:
            client = self._client_factory()
            try:
                response = await client.embeddings.create(model=model, input=missing)
            finally:
                await client.close()
            # The SDK returns data in request order; pair by index.
            for text, datum in zip(missing, response.data):
                self._cache[(model, text)] = list(datum.embedding)
            logger.debug(
                "OpenRouterEmbedder.embed: model=%s, %d new vector(s), %d cached.",
                model,
                len(missing),
                len(texts) - len(missing),
            )
        return [self._cache[(model, t)] for t in texts]


__all__ = ["Embedder", "OpenRouterEmbedder"]
