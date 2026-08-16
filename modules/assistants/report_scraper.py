# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Assistants — Report Scraper module.

Thin registry shell for the Report Scraper. The full extraction logic lives
in :class:`services.scraper.service.ScraperService`; the GUI widget
(``gui/widgets/report_scraper_widget.py``, future) drives that service from
a QThread worker.

Pattern: same thin-shell pattern as :class:`~modules.assistants.shirley.Shirley`.
The module holds a ``ScraperService`` instance as ``self.service`` so the Widget
can call ``module.service.scrape_reports(...)`` directly without going through
``run()``.
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from core.config import Settings
from core.exceptions import ValidationError
from modules.module_registry import registry
from services.ai_service_core import ResolvedLLM
from services.scraper.service import ScraperService


@registry.register
class ReportScraperModule(BaseModule):
    """Report Scraper module — LLM-based extraction from GP quarterly reports.

    Holds a :class:`~services.scraper.service.ScraperService` instance that
    the Widget layer invokes directly for extraction runs.  The ``run()`` method
    provides a programmatic entry-point for non-GUI callers (test harnesses,
    batch scripts).

    Attributes:
        module_name: ``"report_scraper"``
        module_area: ``"assistants"``
        service: The :class:`~services.scraper.service.ScraperService` instance.
    """

    module_name = "report_scraper"
    module_area = "assistants"

    def __init__(self, config: Settings) -> None:
        """Initialise the module and create the ScraperService.

        Args:
            config: The application ``Settings`` singleton.
        """
        super().__init__(config)
        self.service = ScraperService()

    def validate_inputs(self, **kwargs: Any) -> None:
        """Validate that the required scrape arguments are present.

        Args:
            **kwargs: Must include ``attachments`` (non-empty list),
                ``keywords`` (non-empty list), and ``llm`` (a
                :class:`~services.ai_service_core.ResolvedLLM` carrying a
                model).

        Raises:
            ValidationError: If any required argument is missing or invalid.
        """
        attachments = kwargs.get("attachments")
        if not attachments or not isinstance(attachments, list):
            raise ValidationError(
                "ReportScraperModule.run() requires a non-empty 'attachments' list."
            )
        keywords = kwargs.get("keywords")
        if not keywords or not isinstance(keywords, list):
            raise ValidationError("ReportScraperModule.run() requires a non-empty 'keywords' list.")
        llm = kwargs.get("llm")
        if not isinstance(llm, ResolvedLLM) or not llm.model.strip():
            raise ValidationError(
                "ReportScraperModule.run() requires an 'llm' ResolvedLLM carrying a model."
            )

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Run an extraction pass over a list of attachments.

        Keyword Args:
            attachments (list[Attachment]): Files to process (required).
            keywords (list[Keyword]): Keywords to extract (required).
            llm (ResolvedLLM): The caller's per-tenant resolution — endpoint,
                credential and model (required, ADR-0123).
            progress_callback (ProgressCallback | None): Optional progress hook.
            cancel_check (CancelCheck | None): Optional cancellation hook.

        Returns:
            A dict with keys:

            - ``status`` — ``"ok"`` or ``"error"``.
            - ``result`` — The :class:`~services.scraper.models.ScraperResult`,
              or ``None`` on validation failure.

        Raises:
            ValidationError: If required arguments are missing.
            UnsupportedModelError: If the resolved model is not in the
                capability map.
            FileNotFoundError: If the scraper prompt file is missing.
        """
        self.validate_inputs(**kwargs)

        self._logger.debug(
            "ReportScraperModule.run: %d files, model='%s'",
            len(kwargs["attachments"]),
            kwargs["llm"].model,
        )

        result = self.service.scrape_reports(
            attachments=kwargs["attachments"],
            keywords=kwargs["keywords"],
            llm=kwargs["llm"],
            progress_callback=kwargs.get("progress_callback"),
            cancel_check=kwargs.get("cancel_check"),
        )
        return {"status": "ok", "result": result}
