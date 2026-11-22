# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Report Scraper service — orchestrates LLM-based extraction from PDFs.

Strictly sequential. Qt-free. Calls
:meth:`services.ai_service_core.AIServiceCore.send_one_shot_extraction` which
is synchronous and tool-free. This method blocks; the web surface drives it
through ``asyncio.to_thread`` (``web/routes/scraper.py``).

The endpoint, credential and model arrive as a
:class:`~services.ai_service_core.ResolvedLLM` the *caller* resolved for its
tenant (ADR-0123): the service never consults the process-global singleton's
parked credentials, so one process extracts for many tenants on their own
keys. ``ResolvedLLM`` is a plain value object from a module this service
already imports — the service stays DB-free, FastAPI-free and Qt-free.

Partial-success model: per-file failures are captured into
:attr:`services.scraper.models.ReportExtraction.error` and the run continues
with the next file. Run-wide preconditions (unsupported model, missing or
malformed prompt file, profiles that do not load, unknown profile id) raise
before any file is processed.

The model's response is document text: no error text and no log line written
here carries any of it (ADR-0139 §4).
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from collections.abc import Callable

from services.ai_service_core import OutputBudgetExceeded, ResolvedLLM
from services.ai_service_core import get_ai_service_core as get_ai_service
from services.scraper.bank_filter import asks_for_bank_details
from services.scraper.capabilities import (
    ModelCapability,
    lookup_capability,
)
from services.scraper.json_parser import JsonParseError, parse_extraction_response
from services.scraper.mapping import apply_parsed_output
from services.scraper.message_builder import build_extraction_messages
from services.scraper.models import (
    Attachment,
    Keyword,
    ReportExtraction,
    ScraperResult,
)
from services.scraper.profiles import PROFILE_IDS, Profile, ProfileError, load_profiles

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_DIR = _REPO_ROOT / "prompts"
_PROMPT_PATH = _PROMPT_DIR / "Scraper_Prompt.md"

# Matches a bare ``` fence at the start of a line with no language specifier.
# This is intentionally stricter than a plain str.find("```") so that inline
# ```json references inside the prompt content are not mistaken for fence
# delimiters.
_FENCE_RE = re.compile(r"^```\s*$", re.MULTILINE)

# RD-D-28. The timeout is per attempt: the SDK client may retry twice.
_EXTRACTION_TIMEOUT_S = 600.0
_EXTRACTION_MAX_TOKENS = 16000

_PROMPT_VERSION_CHARS = 12

_USER_INSTRUCTION = (
    "Read the attached document as the system prompt describes: classify it, fill the head, "
    "the fields of the chosen profile, the keywords listed below and the reading note. "
    "Return a single JSON object inside a json fenced code block, following the schema in "
    "the system prompt."
)

ProgressCallback = Callable[[int, int, str], None]  # (done, total, filename)
CancelCheck = Callable[[], bool]


def load_scraper_prompt(path: Path | None = None) -> str:
    """Load the scraper system prompt from ``prompts/Scraper_Prompt.md``.

    Extracts the content between the first pair of bare triple-backtick
    fences (fences with no language specifier), following the same structural
    convention as ``prompts/Soul_Shirley.md``.

    Args:
        path: Override path (for tests). If ``None``, uses the default.

    Returns:
        The prompt text between the first pair of bare triple-backtick fences.

    Raises:
        FileNotFoundError: If the prompt file is missing.
        ValueError: If the fence block is malformed or empty.
    """
    src = path or _PROMPT_PATH
    if not src.exists():
        raise FileNotFoundError(f"Scraper prompt not found: {src}")
    text = src.read_text(encoding="utf-8")
    matches = list(_FENCE_RE.finditer(text))
    if len(matches) < 1:
        raise ValueError(f"No ``` fence in {src}")
    if len(matches) < 2:
        raise ValueError(f"Unclosed ``` fence in {src}")
    start = matches[0].end()
    end = matches[1].start()
    prompt = text[start:end].strip()
    if not prompt:
        raise ValueError(f"Empty fence block in {src}")
    return prompt


class ScraperService:
    """Orchestrates Scraper extraction runs.

    Qt-free and blocking; the web route ``web/routes/scraper.py`` calls
    it via ``asyncio.to_thread``.

    Typical usage::

        svc = ScraperService()
        result = svc.scrape_reports(
            attachments=[...],
            keywords=[...],
            llm=ResolvedLLM(base_url=..., api_key=..., model="anthropic/claude-sonnet-4.5"),
            progress_callback=lambda d, t, f: ...,
            cancel_check=lambda: self._cancel_flag.is_set(),
        )
    """

    def scrape_reports(
        self,
        attachments: list[Attachment],
        keywords: list[Keyword],
        llm: ResolvedLLM,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        *,
        profile_id: str | None = None,
    ) -> ScraperResult:
        """Run extraction against a list of attachments, sequentially.

        Args:
            attachments: Files to process, in order.
            keywords: Keywords to extract from each file.
            llm: The caller's per-tenant resolution (ADR-0123) — endpoint,
                credential and model for every call of this run. Its
                ``model`` must match a pattern in the capability map.
            progress_callback: Called after each file as
                ``(files_done, total_files, current_filename)``.
            cancel_check: Polled before each file; returning ``True`` aborts
                the run. ``result.cancelled`` is set to ``True`` on abort.
            profile_id: The profile to read every file as, or ``None`` (the
                default) to let the model classify each one (ADR-0139 §3).

        Returns:
            :class:`ScraperResult` with one :class:`ReportExtraction` per
            input file. Per-file errors are captured in
            ``ReportExtraction.error``; other files continue processing.
            ``prompt_version`` is set, also on a cancelled run.

        Raises:
            UnsupportedModelError: If the resolved model is not in the
                capability map. Raised before any file is processed.
            FileNotFoundError: If the scraper prompt file is missing.
            ValueError: If the scraper prompt file is malformed.
            ProfileError: If the profiles do not load or ``profile_id`` is
                none of theirs. Raised before any file is processed.
        """
        capability = lookup_capability(llm.model)
        system_prompt = load_scraper_prompt()
        profiles = load_profiles()
        preselected: Profile | None = None
        if profile_id is not None:
            if profile_id not in profiles:
                raise ProfileError(f"Unknown profile id; expected one of {list(profiles)}.")
            preselected = profiles[profile_id]

        logger.info(
            "ScraperService: starting run with %d files, %d keywords, model=%s",
            len(attachments),
            len(keywords),
            llm.model,
        )

        result = ScraperResult(
            prompt_version=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[
                :_PROMPT_VERSION_CHARS
            ]
        )

        for idx, att in enumerate(attachments):
            if cancel_check and cancel_check():
                logger.info(
                    "ScraperService: cancelled before file %d (%s).",
                    idx,
                    att.filename,
                )
                result.cancelled = True
                return result

            extraction = self._scrape_one(
                att, keywords, llm, capability, system_prompt, profiles, preselected
            )
            result.extractions.append(extraction)

            if progress_callback:
                progress_callback(idx + 1, len(attachments), att.filename)

        logger.info(
            "ScraperService: run complete, %d files processed.",
            len(result.extractions),
        )
        return result

    def _scrape_one(
        self,
        att: Attachment,
        keywords: list[Keyword],
        llm: ResolvedLLM,
        capability: ModelCapability,
        system_prompt: str,
        profiles: dict[str, Profile],
        preselected: Profile | None,
    ) -> ReportExtraction:
        """Process a single attachment.

        All error paths capture into ``extraction.error`` without raising,
        so the caller can continue with the next file.

        Args:
            att: The attachment to process.
            keywords: The run's keywords. One that asks for a bank detail is
                not sent to the model but still gets its finding.
            llm: The run's resolution — endpoint, credential and model for
                this API call (ADR-0123).
            capability: Resolved capability for the model.
            system_prompt: The loaded system prompt text.
            profiles: The run's profiles by id.
            preselected: The profile to read the file as, or ``None``.

        Returns:
            A :class:`ReportExtraction` populated from the LLM response,
            or with ``error`` set if processing failed.
        """
        extraction = ReportExtraction(filename=att.filename)

        # Size validation (binary attachments only; text attachments are unlimited)
        if isinstance(att.data, bytes):
            size_mb = len(att.data) / (1024 * 1024)
            if size_mb > capability.max_pdf_mb:
                extraction.error = (
                    f"File '{att.filename}' is {size_mb:.1f} MB, which exceeds "
                    f"the {capability.max_pdf_mb} MB limit for model pattern "
                    f"'{capability.pattern}'."
                )
                logger.warning("ScraperService: %s", extraction.error)
                return extraction

        asked = [keyword for keyword in keywords if not asks_for_bank_details(keyword.name)]
        offered = [preselected] if preselected else [profiles[key] for key in PROFILE_IDS]

        # Build messages and call the LLM
        try:
            messages = build_extraction_messages(
                system_prompt=system_prompt,
                user_instruction=_USER_INSTRUCTION,
                keywords=asked,
                attachments=[att],
                model_format=capability.format,
                profiles=offered,
                preselected=preselected is not None,
            )

            raw_response = get_ai_service().send_one_shot_extraction(
                messages=messages,
                llm=llm,
                timeout=_EXTRACTION_TIMEOUT_S,
                max_tokens=_EXTRACTION_MAX_TOKENS,
            )
        except OutputBudgetExceeded:
            extraction.error = (
                f"The reading exceeded the output budget of {_EXTRACTION_MAX_TOKENS} tokens."
            )
            logger.warning("ScraperService: output budget exceeded for %s.", att.filename)
            return extraction
        except Exception as exc:  # noqa: BLE001 — partial-success boundary
            extraction.error = f"API call failed: {type(exc).__name__}: {exc}"
            logger.exception("ScraperService: API call failed for %s", att.filename)
            return extraction

        # Parse and map. The response is untrusted document text: whatever its
        # shape, it costs this file at most and never the run.
        try:
            try:
                parsed = parse_extraction_response(raw_response)
            except JsonParseError:
                # Not interpolated: the exception's message quotes the response.
                extraction.error = "Could not parse the model's response."
                logger.warning(
                    "ScraperService: parse error for %s (response of %d characters).",
                    att.filename,
                    len(raw_response),
                )
                return extraction

            # The parser is typed as a dict and returns whatever JSON it found.
            if not isinstance(parsed, dict):
                extraction.error = "The model's response is not a JSON object."
                logger.warning("ScraperService: no JSON object for %s.", att.filename)
                return extraction

            apply_parsed_output(
                extraction,
                parsed,
                profiles=profiles,
                preselected=preselected,
                keywords=keywords,
            )
        except Exception as exc:  # noqa: BLE001 — partial-success boundary
            # No traceback and no message: both can quote the response. A fresh
            # extraction, so that no half-mapped field survives.
            logger.warning(
                "ScraperService: could not read the response for %s (%s).",
                att.filename,
                type(exc).__name__,
            )
            return ReportExtraction(
                filename=att.filename,
                error="Could not read the model's response.",
            )

        return extraction
