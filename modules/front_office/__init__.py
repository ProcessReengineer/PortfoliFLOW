# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Front Office modules — investment analysis and decision support."""

# Each import triggers the @registry.register decorator on the module class.
from modules.front_office import data_import  # noqa: F401
from modules.front_office import overview  # noqa: F401
