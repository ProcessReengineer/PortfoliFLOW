# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Back Office modules — operations, reporting, and compliance."""

# Each import triggers the @registry.register decorator on the module class.
from modules.back_office import benchmarks_attribution  # noqa: F401
from modules.back_office import limits  # noqa: F401
from modules.back_office import saa  # noqa: F401
