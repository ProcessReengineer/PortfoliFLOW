# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Central registry that maps module names to their classes.

The GUI and any orchestration layer should import **only** this module to
discover and instantiate modules.  Individual module files register themselves
via the ``@registry.register`` decorator, so adding a new module never requires
touching existing code.

Usage::

    # In a module file:
    from modules.module_registry import registry

    @registry.register
    class DataImport(BaseModule):
        module_name = "data_import"
        module_area = "front_office"
        ...

    # In the GUI or tests:
    from modules.module_registry import registry
    from core.config import get_config

    cls = registry.get("data_import")
    instance = cls(config=get_config())
    result = instance.run()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.base_module import BaseModule as BaseModuleType

from core.exceptions import ModuleError

logger = logging.getLogger(__name__)


class ModuleRegistry:
    """Registry mapping module names to ``BaseModule`` subclasses.

    Supports both decorator and direct-call registration patterns.

    Attributes:
        _registry: Internal dict of ``{module_name: module_class}``.
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[BaseModuleType]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, module_class: type[BaseModuleType]) -> type[BaseModuleType]:
        """Register a module class by its ``module_name`` attribute.

        Can be used as a decorator::

            @registry.register
            class MyModule(BaseModule): ...

        Or called directly::

            registry.register(MyModule)

        Args:
            module_class: A concrete subclass of ``BaseModule`` with
                ``module_name`` and ``module_area`` set.

        Returns:
            The unmodified ``module_class`` so decorator usage is transparent.

        Raises:
            ModuleError: If ``module_name`` is empty or already registered.
        """
        name = getattr(module_class, "module_name", "")
        if not name:
            raise ModuleError(f"Cannot register {module_class.__name__}: 'module_name' is not set.")
        if name in self._registry:
            raise ModuleError(
                f"Module '{name}' is already registered "
                f"(existing: {self._registry[name].__name__})."
            )
        self._registry[name] = module_class
        logger.debug("Registered module '%s' (%s).", name, module_class.__name__)
        return module_class

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> type[BaseModuleType]:
        """Return the class registered under *name*.

        Args:
            name: The ``module_name`` string to look up.

        Returns:
            The registered module class (not an instance).

        Raises:
            ModuleError: If *name* is not in the registry.
        """
        try:
            return self._registry[name]
        except KeyError:
            available = ", ".join(sorted(self._registry)) or "<none>"
            raise ModuleError(
                f"Module '{name}' not found. Available modules: {available}."
            ) from None

    def list_by_area(self, area: str) -> list[type[BaseModuleType]]:
        """Return all module classes belonging to *area*.

        Args:
            area: One of ``"front_office"``, ``"back_office"``, ``"admin"``,
                ``"investor_communication"``, ``"assistants"``.

        Returns:
            List of module classes; empty list if the area has no registrations.
        """
        return [cls for cls in self._registry.values() if getattr(cls, "module_area", "") == area]

    def all(self) -> dict[str, type[BaseModuleType]]:
        """Return a shallow copy of the full registry dict.

        Returns:
            Dict mapping each ``module_name`` to its class.
        """
        return dict(self._registry)


#: Module-level singleton — import and use this object directly.
registry = ModuleRegistry()
