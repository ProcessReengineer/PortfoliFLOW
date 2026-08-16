// Bespoke client logic for the Report Scraper section.
//
// Mirrors the structure of chat.js — plain JS, no framework, native
// EventSource (the HTMX SSE extension's leading-space trim and
// reconnect storm are documented hazards we avoid).
//
// Responsibilities:
//   1. Keyword editor — add / remove rows.
//   2. On submit: serialise the rows to ``#scraper-keywords-json``
//      so the server gets a single JSON payload (Option B from the
//      design spec).
//   3. When HTMX swaps the run-mount fragment into ``#scraper-run``,
//      open an EventSource on its ``data-pf-scraper-sse-url`` and
//      route progress / result / error / cancelled events into the
//      DOM. Close on terminal events.
//   4. Cancel button: POST to ``/scraper/runs/<id>/cancel`` with the
//      session CSRF token (matches the chat surface's "New chat"
//      pattern).

(function () {
    "use strict";

    document.addEventListener("DOMContentLoaded", function () {
        wireKeywordEditor(document);
        attachScraperSseListeners(document);
    });

    document.addEventListener("htmx:afterSwap", function (event) {
        const target = event.target;
        if (!target) return;
        wireKeywordEditor(target);
        attachScraperSseListeners(target);
    });

    // The submit handler reads the current keyword rows and writes
    // them into the hidden JSON field. ``htmx:configRequest`` fires
    // after the form is gathered, so we can mutate the FormData
    // payload directly instead of touching the DOM right before
    // HTMX serialises it.
    document.addEventListener("htmx:configRequest", function (event) {
        const elt = event.detail.elt;
        if (!elt || elt.id !== "scraper-form") return;
        const json = collectKeywordsJson();
        if (event.detail.parameters) {
            event.detail.parameters.keywords_json = json;
        }
        const hidden = document.getElementById("scraper-keywords-json");
        if (hidden) hidden.value = json;
    });

    // ---- Keyword editor -------------------------------------------------

    function wireKeywordEditor(scope) {
        const addBtn = scope.querySelector
            ? scope.querySelector("#scraper-add-keyword")
            : null;
        if (addBtn && !addBtn.dataset.pfWired) {
            addBtn.dataset.pfWired = "1";
            addBtn.addEventListener("click", function () {
                const fieldset = document.getElementById("scraper-keywords");
                if (!fieldset) return;
                const row = makeKeywordRow("", "Number");
                fieldset.insertBefore(row, addBtn);
            });
        }
        const fieldset =
            scope.querySelector && scope.querySelector("#scraper-keywords");
        if (fieldset && !fieldset.dataset.pfRemoveWired) {
            fieldset.dataset.pfRemoveWired = "1";
            fieldset.addEventListener("click", function (ev) {
                const btn = ev.target.closest(".scraper__keyword-remove");
                if (!btn) return;
                const row = btn.closest(".scraper__keyword-row");
                if (row) row.remove();
            });
        }
    }

    function makeKeywordRow(name, type) {
        const row = document.createElement("div");
        row.className = "scraper__keyword-row";
        row.dataset.row = "";

        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.className = "scraper__keyword-name";
        nameInput.placeholder = "Keyword name";
        nameInput.value = name;
        nameInput.setAttribute("aria-label", "Keyword name");
        row.appendChild(nameInput);

        const typeSelect = document.createElement("select");
        typeSelect.className = "scraper__keyword-type";
        typeSelect.setAttribute("aria-label", "Keyword type");
        ["Number", "Percentage", "Date", "Text", "List"].forEach(function (t) {
            const opt = document.createElement("option");
            opt.value = t;
            opt.textContent = t;
            if (t === type) opt.selected = true;
            typeSelect.appendChild(opt);
        });
        row.appendChild(typeSelect);

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "scraper__keyword-remove btn btn--ghost";
        removeBtn.setAttribute("aria-label", "Remove keyword");
        removeBtn.textContent = "×";
        row.appendChild(removeBtn);

        return row;
    }

    function collectKeywordsJson() {
        const rows = document.querySelectorAll(
            "#scraper-keywords .scraper__keyword-row"
        );
        const out = [];
        rows.forEach(function (row) {
            const name = (row.querySelector(".scraper__keyword-name") || {}).value || "";
            const type = (row.querySelector(".scraper__keyword-type") || {}).value || "Text";
            if (name.trim()) {
                out.push({ name: name.trim(), type: type });
            }
        });
        return JSON.stringify(out);
    }

    // ---- EventSource bootstrap -----------------------------------------

    function attachScraperSseListeners(scope) {
        const mounts =
            scope.querySelectorAll &&
            scope.querySelectorAll(
                "[data-pf-scraper-sse-url]:not([data-pf-attached])"
            );
        if (!mounts) return;
        mounts.forEach(openOneShotEventSource);
    }

    function openOneShotEventSource(el) {
        el.dataset.pfAttached = "1";

        const url = el.dataset.pfScraperSseUrl;
        if (!url) return;

        const progressContainer = el.querySelector(".scraper-run-mount__progress");
        const resultsContainer = el.querySelector(".scraper-run-mount__results");
        if (!progressContainer || !resultsContainer) return;

        wireCancelButtons(el);

        const eventSource = new EventSource(url);

        eventSource.addEventListener("progress", function (event) {
            progressContainer.innerHTML = event.data || "";
            wireCancelButtons(el);
        });

        eventSource.addEventListener("result", function (event) {
            progressContainer.innerHTML = "";
            resultsContainer.innerHTML = event.data || "";
            eventSource.close();
        });

        eventSource.addEventListener("cancelled", function (event) {
            progressContainer.innerHTML = "";
            resultsContainer.innerHTML = event.data || "";
            eventSource.close();
        });

        eventSource.addEventListener("error", function (event) {
            // Server-emitted ``error`` event — distinct from the
            // transport-level ``onerror`` callback below.
            const text = (event && event.data) || "An unknown error occurred.";
            progressContainer.innerHTML =
                '<p class="scraper__form-error">' + escapeHtml(text) + "</p>";
            eventSource.close();
        });

        // Transport-level failure: render once and close, no retry.
        eventSource.onerror = function () {
            if (eventSource.readyState === EventSource.CLOSED) return;
            progressContainer.innerHTML =
                '<p class="scraper__form-error">' +
                "Connection lost — please try again.</p>";
            eventSource.close();
        };
    }

    function wireCancelButtons(scope) {
        const buttons = scope.querySelectorAll(
            "[data-pf-scraper-cancel]:not([data-pf-cancel-wired])"
        );
        buttons.forEach(function (btn) {
            btn.dataset.pfCancelWired = "1";
            btn.addEventListener("click", function () {
                const url = btn.dataset.pfScraperCancelUrl;
                if (!url) return;
                const csrf = scope.dataset.pfCsrfToken || "";
                fetch(url, {
                    method: "POST",
                    headers: { "X-CSRF-Token": csrf },
                    credentials: "same-origin",
                }).catch(function () {
                    // Swallow — the SSE stream will close on its own
                    // when the server picks up the cancel flag, or
                    // surface the transport-level error.
                });
                btn.disabled = true;
                btn.textContent = "Cancelling…";
            });
        });
    }

    function escapeHtml(text) {
        return String(text).replace(/[&<>]/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
        });
    }
})();
