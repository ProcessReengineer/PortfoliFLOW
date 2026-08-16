// Data Import section — single-button workflow confirm-then-redirect.
//
// Sub-stream 6F: upload + dry-run preview is one server round-trip; only
// the final "Apply to Investments" step is client-driven (it issues the
// destructive ``dry_run=false`` POST).
//
// Event delegation: the click listener is bound once at the panel level
// and survives every HTMX swap. The button itself is rebuilt on each
// preview render, but we never touch the button directly — we identify
// the click via ``event.target.closest('#import-confirm-btn')``.
(() => {
    'use strict';

    const PANEL_ID = 'pf-data-import-panel';

    async function handleConfirmClick(confirmBtn) {
        const resultBox = document.getElementById('import-as-investments-result');
        const uploadId = confirmBtn.dataset.uploadId;
        const csrfToken = confirmBtn.dataset.csrfToken;

        function showAlert(kind, message) {
            if (!resultBox) return;
            resultBox.className = `alert alert--${kind}`;
            resultBox.textContent = message;
            resultBox.hidden = false;
        }

        if (!uploadId || !csrfToken) {
            showAlert('error', 'Missing upload id or CSRF token. Reload and try again.');
            return;
        }

        confirmBtn.disabled = true;
        try {
            const response = await fetch(
                `/api/data-uploads/${uploadId}/import-as-investments?dry_run=false`,
                {
                    method: 'POST',
                    headers: {
                        'X-CSRF-Token': csrfToken,
                        'Accept': 'application/json',
                    },
                },
            );
            let body = null;
            try {
                body = await response.json();
            } catch (_e) {
                // Ignore JSON parse failure — surfaced via response.ok below.
            }
            if (!response.ok) {
                const detail =
                    (body && body.detail) ||
                    `Request failed (HTTP ${response.status}).`;
                throw new Error(detail);
            }
            const total =
                body.investments_created +
                body.investments_updated +
                body.investments_reactivated;
            showAlert(
                'success',
                `Imported ${total} investment(s); ${body.investments_deactivated} deactivated. Redirecting…`,
            );
            window.setTimeout(() => {
                window.location.href = '/investments';
            }, 1200);
        } catch (err) {
            showAlert('error', err.message);
            confirmBtn.disabled = false;
        }
    }

    function bindPanelDelegate() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel || panel.dataset.pfDelegateBound === '1') {
            return;
        }
        panel.dataset.pfDelegateBound = '1';
        panel.addEventListener('click', (event) => {
            const confirmBtn = event.target.closest('#import-confirm-btn');
            if (confirmBtn && panel.contains(confirmBtn)) {
                event.preventDefault();
                handleConfirmClick(confirmBtn);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindPanelDelegate);
    } else {
        bindPanelDelegate();
    }
})();
