# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The beat's briefing step — Irene's second synthesis call (ADR-0133 §1.3–§1.4).

After the findings of a completed check are persisted — on the surfaced
path and on the silence path alike — the beat hands over to
:func:`run_briefing_step`. It assembles the material
(:func:`services.irene.briefing_material.gather_briefing_material`), calls
``run_synthesis`` with the same resolved ``llm``, the briefing prompt and
the one forced ``write_briefing`` tool
(:mod:`services.irene.briefing_tool`), validates the output as exactly one
call carrying both texts, and appends one ``irene_briefing`` row.

§1.4: the whole step, the material reads included, runs inside one
savepoint, so no failure here can abort the tick's transaction or touch
the findings the beat has already written. A raising call is a
``skipped`` row with ``llm_error``; output that is not one valid call is
``invalid_output``; material with nothing to brief is ``nothing_to_write``
and makes no call. A failure inside the savepoint itself — a material read
or the row write — rolls back only the savepoint, is logged, and leaves no
row. The step never raises and never touches ``BeatResult.error``.

The status vocabulary is the model's
(:data:`core.models.irene_briefing.BRIEFING_STATUSES`,
:data:`core.models.irene_briefing.BRIEFING_SKIP_REASONS`).

What the writer receives is one user message: a fixed header above the
indented JSON of the material's ``inputs``. The persisted ``inputs`` are
therefore exactly what the writer saw.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories.irene_briefing_repository import IreneBriefingRepository
from core.repositories.irene_finding_repository import IreneFindingDTO
from services.irene.briefing_material import gather_briefing_material
from services.irene.briefing_tool import (
    WRITE_BRIEFING_TOOL,
    WRITE_BRIEFING_TOOL_CHOICE,
    parse_write_briefing,
)
from services.irene.rss_clustering import RssBucket
from services.watch_desk.overlay import WatchDeskResolution

if TYPE_CHECKING:
    from services.ai_service_core import ResolvedLLM

logger = logging.getLogger(__name__)

#: The one user message the briefing call receives, above the JSON material.
BRIEFING_CONTEXT_HEADER: str = (
    'Material for this check, as JSON: "markets" is Part 1, "watch" is Part 2.'
)


def _briefing_context(inputs: dict[str, Any]) -> str:
    """Render the material as the writer's one user message."""
    return BRIEFING_CONTEXT_HEADER + "\n\n" + json.dumps(inputs, ensure_ascii=False, indent=2)


async def run_briefing_step(
    session: AsyncSession,
    ai_core: Any,
    *,
    tenant_id: UUID,
    llm: ResolvedLLM,
    resolution: WatchDeskResolution,
    now: datetime,
    rss_buckets: Sequence[RssBucket],
    findings: Sequence[IreneFindingDTO],
) -> str | None:
    """Write this check's briefing row inside a savepoint.

    Args:
        session: The beat's tenant-scoped session.
        ai_core: The AI service core (duck-typed: needs
            ``get_briefing_prompt`` and an async ``run_synthesis`` that
            accepts ``tool_choice``).
        tenant_id: The tenant the beat runs for; named in the log lines.
        llm: The beat's resolved endpoint, credential and model — the same
            one the finding call used.
        resolution: The beat's one resolution of the tenant's calibration.
        now: The beat's clock; stored as the row's ``beat_at``.
        rss_buckets: The press clusters the beat formed.
        findings: The findings the beat appended, with their final band.

    Returns:
        The appended row's status (``"written"`` or ``"skipped"``), or
        ``None`` when no row was written because the savepoint rolled back.
    """
    try:
        async with session.begin_nested():
            material = await gather_briefing_material(
                session,
                resolution=resolution,
                now=now,
                rss_buckets=rss_buckets,
                findings=findings,
            )
            skip_reason: str | None = None
            texts: tuple[str, str] | None = None
            model_id: str | None = None
            if material.nothing_to_write:
                skip_reason = "nothing_to_write"
            else:
                model_id = llm.model
                try:
                    result = await ai_core.run_synthesis(
                        system_prompt=ai_core.get_briefing_prompt(),
                        context_messages=[
                            {"role": "user", "content": _briefing_context(material.inputs)}
                        ],
                        tool=WRITE_BRIEFING_TOOL,
                        llm=llm,
                        tool_choice=WRITE_BRIEFING_TOOL_CHOICE,
                    )
                except Exception:  # noqa: BLE001 — a failed writer skips the briefing only
                    logger.warning(
                        "irene-beat: tenant %s — the briefing call failed.",
                        tenant_id,
                        exc_info=True,
                    )
                    skip_reason = "llm_error"
                else:
                    texts = parse_write_briefing(result.tool_calls)
                    if texts is None:
                        skip_reason = "invalid_output"

            status = "written" if skip_reason is None else "skipped"
            await IreneBriefingRepository(session).append(
                beat_at=now,
                written_at=datetime.now(timezone.utc),
                status=status,
                skip_reason=skip_reason,
                text_markets=texts[0] if texts is not None else None,
                text_watch=texts[1] if texts is not None else None,
                inputs=material.inputs,
                finding_ids=[f.id for f in findings],
                model_id=model_id,
            )
    except Exception:  # noqa: BLE001 — the briefing must not fail the check
        logger.exception(
            "irene-beat: tenant %s — briefing not recorded; the check stands.", tenant_id
        )
        return None

    if skip_reason is None:
        logger.info("irene-beat: tenant %s — briefing %s.", tenant_id, status)
    else:
        logger.info("irene-beat: tenant %s — briefing %s (%s).", tenant_id, status, skip_reason)
    return status


__all__ = ["BRIEFING_CONTEXT_HEADER", "run_briefing_step"]
