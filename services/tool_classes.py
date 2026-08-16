# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tool-class taxonomy for AI-callable tools.

Defines the four trust / blast-radius classes that every tool registered with
:class:`services.tool_registry.ToolRegistry` must declare. The taxonomy and
its gating rules are specified in ADR-0022 (Tool Trust Classes and Gating
Policy). See :mod:`services.tool_registry` for the enforcement surface and
``docs/adr/0022-tool-trust-classes-and-gating-policy.md`` for the decision
record.

Every tool must declare its class at registration time; silent defaults are a
deliberate non-feature.
"""

from __future__ import annotations

from enum import StrEnum


class ToolClass(StrEnum):
    """The four trust classes a tool can belong to (see ADR-0022).

    Members:
        READ_INTERNAL: Reads application state the user has already loaded or
            configured (DataStore, local configs). Return values are *trusted*
            content — the user's own data.
        WRITE_INTERNAL: Mutates application state inside PortfoliFLOW (load /
            delete a dataset, modify the SAA, change a configuration).
        READ_EXTERNAL_UNTRUSTED: Fetches content from outside the application
            whose content cannot be assumed benign. Return values are
            *untrusted* content and must be wrapped in ``<external_content>``
            delimiters before reaching the agent. Executing any tool of this
            class locks :attr:`WRITE_INTERNAL` and :attr:`EXTERNAL_EFFECT`
            tools for the remainder of the current user turn.
        EXTERNAL_EFFECT: Actions with side effects outside the application
            (send email, export to a third party, place an order). Require
            explicit user confirmation via a GUI dialog before execution.
    """

    READ_INTERNAL = "read_internal"
    WRITE_INTERNAL = "write_internal"
    READ_EXTERNAL_UNTRUSTED = "read_external_untrusted"
    EXTERNAL_EFFECT = "external_effect"
