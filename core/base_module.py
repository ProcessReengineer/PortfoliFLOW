# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Abstract base class that every PortfoliFLOW module must implement.

New modules are created by subclassing ``BaseModule``, setting the class-level
attributes, and implementing ``run()``.  No other file needs to be modified —
the module is discovered via ``ModuleRegistry``.

Example::

    from core.base_module import BaseModule
    from modules.module_registry import registry

    @registry.register
    class MyModule(BaseModule):
        module_name = "my_module"
        module_area = "front_office"

        def run(self, *args, **kwargs) -> dict:
            return {"status": "ok"}
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from core.config import Settings

logger = logging.getLogger(__name__)

VALID_AREAS = frozenset(
    {
        "front_office",
        "back_office",
        "admin",
        "investor_communication",
        "assistants",
        # Sixth top-level Area (ADR-0089): the Watch Desk / Irene surface.
        "watch_desk",
        # Seventh top-level Area (ADR-0104): the Planning Desk — Cash Flow
        # Planning and Scenario Analysis over the plan world.
        "planning_desk",
        # Eighth top-level Area (ADR-0107): the Cases workflow — open questions
        # worked to a documented close, sitting between the Watch Desk
        # (watches and raises) and the Planning Desk (projects and simulates).
        "cases",
    }
)


class BaseModule(ABC):
    """Abstract base class for all PortfoliFLOW modules.

    Subclasses **must** define:

    * ``module_name`` — unique snake_case identifier (e.g. ``"data_import"``).
    * ``module_area`` — one of the seven Areas in :data:`VALID_AREAS`.
    * ``run()`` — entry-point called by the GUI and by other modules.

    Attributes:
        module_name: Unique snake_case identifier for this module.
        module_area: Logical area the module belongs to.
        version: Semantic version string; defaults to ``"0.1.0"``.
        config: The ``Settings`` instance injected at construction time.
    """

    module_name: str = ""
    module_area: str = ""
    version: str = "0.1.0"

    def __init__(self, config: Settings) -> None:
        """Initialise the module with application configuration.

        Args:
            config: The application ``Settings`` singleton.

        Raises:
            TypeError: If ``module_name`` or ``module_area`` are not set on the
                subclass.
        """
        if not self.module_name:
            raise TypeError(f"{type(self).__name__} must define 'module_name'.")
        if not self.module_area:
            raise TypeError(f"{type(self).__name__} must define 'module_area'.")
        if self.module_area not in VALID_AREAS:
            raise TypeError(
                f"{type(self).__name__} has invalid module_area '{self.module_area}'. "
                f"Must be one of: {', '.join(sorted(VALID_AREAS))}"
            )

        self.config = config
        self._logger = logging.getLogger(f"modules.{self.module_area}.{self.module_name}")

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Execute the module's primary workflow.

        Args:
            *args: Positional arguments specific to the module.
            **kwargs: Keyword arguments specific to the module.

        Returns:
            A dict containing at minimum ``{"status": "ok" | "error"}``.
            Modules may include additional result keys as documented in their
            own docstrings.

        Raises:
            ModuleError: On unexpected runtime failures.
            ValidationError: If inputs do not pass ``validate_inputs()``.
        """

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def validate_inputs(self, **kwargs: Any) -> None:
        """Validate keyword inputs before the module runs.

        The base implementation is a no-op.  Subclasses should override this
        method and raise ``ValidationError`` for any invalid input.

        Args:
            **kwargs: The same keyword arguments that will be passed to
                ``run()``.

        Raises:
            ValidationError: When a required field is missing or has an
                invalid value.
        """
        _ = kwargs  # suppress unused-argument warnings in stubs

    def get_metadata(self) -> dict:
        """Return a dict describing this module instance.

        Returns:
            A dict with keys ``name``, ``area``, and ``version``.

        Example::

            >>> mod.get_metadata()
            {'name': 'data_import', 'area': 'front_office', 'version': '0.1.0'}
        """
        return {
            "name": self.module_name,
            "area": self.module_area,
            "version": self.version,
        }
