# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Manual accessibility procedure for the Phase-2 web surface.

Per ADR-0037 §10, Phase 2 commits to the WCAG 2.1 AA *minima*
(semantic HTML, keyboard navigability, ARIA where necessary, contrast
ratios consistent with ``config/ui_theme.json``) — not to formal
certification. Automated CI accessibility scanning is a Phase-5
concern.

Until then, the procedure below is run **manually** before each demo-
stable cut. The test in this module is a no-op marker that documents
the procedure; remove the ``pytest.skip`` and replace with a real
Pa11y / axe-core driver once the automation lands.

Manual procedure
----------------

1. Bring up Postgres, apply migrations, bootstrap the sentinel:

       podman compose up -d
       alembic -c db/alembic.ini upgrade head
       portfoliflow bootstrap

2. In a separate shell, regenerate theme artefacts and start the web:

       python -m scripts.generate_theme_artifacts
       portfoliflow-web

3. With the dev tools open, visit the three pages and run the chosen
   accessibility scanner against each:

       /login
       /assistants                  (Shirley is embedded under #shirley)
       /                            (after sign-in — redirects to /front-office)

   Recommended tools (pick one):
       - Pa11y CLI:    pa11y http://127.0.0.1:8000/login
       - axe DevTools: browser extension, "Scan All of My Page"

4. Expected outcome — **zero AA violations** on each page. Common
   things to check by hand even when the scanner is silent:

   * Tab order is sequential and reaches every interactive element.
   * Focus styles are visible (the global outline rule in base.css
     turns on a 2px accent-coloured outline; verify it renders on
     buttons, the textarea, and the email/password inputs).
   * The skip link is reachable via keyboard before the navigation.
   * The error alert on a failed login is announced (it carries
     ``role="alert"``).
   * The chat history has ``role="log"`` and ``aria-live="polite"``
     so streaming tokens are announced incrementally.
   * Colour contrast in the dark theme passes 4.5:1 for text and
     3:1 for large/UI-component contrast — the default theme's
     accent / background pairs were sized for that target but
     revalidate after any theme tweak.

5. Record the run date and tool version in your handover notes.
"""

from __future__ import annotations

import pytest


def test_accessibility_minima_documentation_marker() -> None:
    """Documents the manual a11y procedure; intentionally skipped.

    Keeping this as a discoverable test (rather than a markdown file
    only) means ``pytest`` lists the marker and a future automation
    pass can replace the body without renaming the file.
    """
    pytest.skip(
        "Accessibility minima are validated manually in Phase 2 (ADR-0037 "
        "§10). See this module's docstring for the procedure."
    )
