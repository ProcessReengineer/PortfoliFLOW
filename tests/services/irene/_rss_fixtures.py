# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared RSS-clustering test helpers (ADR-0087 Part B).

An offline, deterministic embedder stub and a :class:`FeedItem` builder,
reused by the bucketing-determinism tests, the freeze regression, and the
correlation-lift / beat tests. No network, no DB.

The leading underscore keeps pytest from collecting this as a test module,
mirroring ``tests/services/irene/_book_fixtures.py``.
"""

from __future__ import annotations

from datetime import datetime

from services.web_research.models import FeedItem

# Fixed vector dimension for the stub — large enough that distinct topics
# land on distinct one-hot axes without collision in these small tests.
_DIM = 32


def make_item(
    url: str,
    title: str,
    published_at: datetime,
    *,
    tags: tuple[str, ...],
    description: str | None = None,
    source: str = "SRC",
) -> FeedItem:
    """Build a :class:`FeedItem` carrying the given curated tags."""
    return FeedItem.from_components(
        url=url,
        title=title,
        description=description,
        published_at=published_at,
        source_name=source,
        tags=tags,
    )


class StubEmbedder:
    """A deterministic, offline embedder for clustering tests.

    Each text is mapped to a fixed one-hot unit vector by its *topic* — the
    text up to the first ``:``. Same topic → identical vector (cosine
    ``1.0`` ≥ any threshold ⇒ same bucket); different topic → orthogonal
    (cosine ``0.0`` < threshold ⇒ separate buckets). With
    ``vary_by_model=True`` the axis is rotated by a model-derived offset, so
    the same topic vectorises differently under a different model id — the
    hook the freeze regression uses to prove the key survives a model
    change by membership, not by vector.

    ``calls`` records every ``(model, texts)`` invocation so tests can spy
    on call ordering (e.g. that the key is formed before synthesis).
    """

    def __init__(self, *, vary_by_model: bool = False) -> None:
        self._axis: dict[str, int] = {}
        self._vary = vary_by_model
        self.calls: list[tuple[str, list[str]]] = []

    @staticmethod
    def _topic(text: str) -> str:
        return text.split(":", 1)[0].strip().upper()

    def _offset(self, model: str) -> int:
        return (sum(ord(c) for c in model) % _DIM) if self._vary else 0

    async def embed(self, texts, *, model: str) -> list[list[float]]:
        self.calls.append((model, list(texts)))
        offset = self._offset(model)
        vectors: list[list[float]] = []
        for text in texts:
            topic = self._topic(text)
            if topic not in self._axis:
                self._axis[topic] = len(self._axis)
            axis = (self._axis[topic] + offset) % _DIM
            vec = [0.0] * _DIM
            vec[axis] = 1.0
            vectors.append(vec)
        return vectors
