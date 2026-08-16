# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the RSS key is formed by a rule, never by a model.

ADR-0087 Part B rests on one invariant — the RSS cluster ``subject_key``
is formed **before any LLM sees the items**, and never *by* one. This
guard makes that machine-enforced, mirroring the analytics purity guard
(``tests/regression/test_analytics_layer_pure.py``):

1. **Source scan.** The pure key-former
   (:mod:`services.analytics.rss_bucketing`) must contain no token that
   would betray a model / embedding call — no ``embeddings``, no
   ``AsyncOpenAI``, no ``run_synthesis``, no ``.create(``, and not even
   the embedder interface name (``Embedder``). The key path is pure hash
   over membership.

2. **The key path never reaches synthesis.** ``run_synthesis`` — Irene's
   only synthesis call — must not appear anywhere on the key path
   (``rss_bucketing``, ``rss_clustering``, ``rss_delta``). Synthesis is a
   *consumer* of already-keyed buckets, wired only in the beat.

3. **Ordering.** In the beat's ``run_beat``, the RSS delta runs and the
   context is built from the keyed buckets **before** ``run_synthesis`` is
   called — a structural/ordering assertion on the source.

4. **Behavioural.** ``build_rss_buckets`` returns fully-keyed buckets
   (every ``subject_key`` carries the ``rss:cluster:`` prefix) from a stub
   embedder alone — the key exists the moment clustering returns, with no
   synthesis in the loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.analytics.rss_bucketing import form_subject_key
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS
from services.irene.rss_clustering import build_rss_buckets
from tests.services.irene._rss_fixtures import StubEmbedder, make_item

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_KEY_FORMER: Path = _REPO_ROOT / "services" / "analytics" / "rss_bucketing.py"

# Tokens that would betray a model / embedding call inside the pure
# key-former. None may appear in its source (docstrings included).
_FORBIDDEN_MODEL_TOKENS: tuple[str, ...] = (
    "embeddings",
    "AsyncOpenAI",
    "run_synthesis",
    ".create(",
    "Embedder",
)

# The key path: none of these modules may call synthesis.
_KEY_PATH_FILES: tuple[Path, ...] = (
    _REPO_ROOT / "services" / "analytics" / "rss_bucketing.py",
    _REPO_ROOT / "services" / "irene" / "rss_clustering.py",
    _REPO_ROOT / "services" / "irene" / "rss_delta.py",
)


def test_key_former_has_no_model_call_token() -> None:
    """The pure key-former mentions no embedding/LLM/model call token."""
    source = _KEY_FORMER.read_text(encoding="utf-8")
    offenders = [tok for tok in _FORBIDDEN_MODEL_TOKENS if tok in source]
    assert not offenders, (
        "ADR-0087 Part B: the RSS key-former must be free of any model / "
        f"embedding call token; found {offenders} in {_KEY_FORMER}."
    )


def test_key_path_never_calls_synthesis() -> None:
    """No module on the key-forming path invokes run_synthesis."""
    for path in _KEY_PATH_FILES:
        source = path.read_text(encoding="utf-8")
        assert "run_synthesis" not in source, (
            f"{path} is on the RSS key path and must not reference "
            "run_synthesis — synthesis consumes keyed buckets, it never "
            "forms the key."
        )


def test_beat_forms_context_before_synthesis() -> None:
    """In run_beat, RSS delta + context precede the synthesis call."""
    beat_src = (_REPO_ROOT / "services" / "irene" / "beat.py").read_text(encoding="utf-8")
    i_rss = beat_src.index("await evaluate_rss_deltas(")
    i_ctx = beat_src.index("context_messages = _build_delta_context(")
    i_syn = beat_src.index("ai_core.run_synthesis(")
    assert i_rss < i_ctx < i_syn, (
        "The RSS delta and context construction must precede the synthesis "
        "call in run_beat (the key precedes synthesis)."
    )


async def test_build_rss_buckets_returns_keyed_buckets() -> None:
    """Clustering alone yields fully-keyed buckets — no synthesis needed."""
    items = [
        make_item(
            "https://a/1",
            "RATE: one",
            datetime(2026, 6, 30, 9, tzinfo=timezone.utc),
            tags=("macro",),
        ),
        make_item(
            "https://a/2",
            "RATE: two",
            datetime(2026, 6, 30, 10, tzinfo=timezone.utc),
            tags=("macro",),
        ),
    ]
    embedder = StubEmbedder()
    buckets = await build_rss_buckets(
        None,
        embedder,
        items,
        now=datetime(2026, 6, 30, 12, tzinfo=timezone.utc),
        thresholds=DEFAULT_DELTA_THRESHOLDS,
    )
    assert buckets
    assert all(b.subject_key.startswith("rss:cluster:") for b in buckets)
    # The embedder (the only model call) did fire — the key is formed from
    # the resulting membership, not from any synthesis.
    assert embedder.calls
    # And the key equals the pure key-former over that membership.
    from services.analytics.rss_bucketing import item_identity

    for b in buckets:
        membership = frozenset(
            item_identity(published_at=m.published_at, url=m.url) for m in b.members
        )
        assert b.subject_key == form_subject_key(
            day_bucket=b.day_bucket, tag=b.tag, membership=membership
        )
