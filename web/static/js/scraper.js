// Bespoke client logic for the Read pane of the Reading Desk.
//
// Plain JS, no framework, native EventSource (the HTMX SSE extension's
// leading-space trim and reconnect storm are documented hazards we avoid).
// Elements are found by id and by ``data-pf-rd`` / ``data-pf-scraper-*``
// hooks; this file reads and writes no class and assembles no markup — a
// note is cloned from a ``<template>`` the server rendered.
//
// Responsibilities:
//   1. Keyword editor — add a row cloned from
//      ``#scraper-keyword-template``, remove a row. Delegated from
//      ``document``: the form arrives lazily with the Section frame.
//   2. On submit: serialise the rows to ``#scraper-keywords-json``
//      so the server gets a single JSON payload.
//   3. When HTMX swaps the run mount into ``#scraper-run``, open an
//      EventSource on its ``data-pf-scraper-sse-url`` and route progress /
//      result / error / cancelled events into its two containers. Close on
//      terminal events. A result is handed to ``htmx.process`` so its
//      "Open R-nnnn" links open the reading in place.
//   4. Cancel button: POST to ``/scraper/runs/<id>/cancel`` with the
//      session CSRF token.
//   5. Elapsed timer: after each ``progress`` swap, tick the time since
//      that swap (``m:ss``) into ``[data-pf-scraper-elapsed]``; stopped
//      on every terminal event and when the mount leaves the DOM.
//   6. 400 / 404 answers aimed at ``#scraper-run`` (a refused keyword, a
//      confirm of a run no longer held) are swapped in like a 200.
//   7. One run at a time (D-RD-2-8): *Read documents* is disabled while a
//      run streams and while the already-read notice waits for its answer;
//      during a run the action bar's hint says so.

(function () {
    "use strict";

    // True from the moment a stream is opened until its terminal event.
    let running = false;

    document.addEventListener("DOMContentLoaded", function () {
        attachScraperSseListeners(document);
    });

    document.addEventListener("htmx:afterSwap", function (event) {
        const target = event.target;
        if (!target) return;
        attachScraperSseListeners(target);
        syncForm();
    });

    // htmx lifts its own ``hx-disabled-elt`` lock when a request ends, which
    // can be after the swap that started a run: state the button again.
    document.addEventListener("htmx:afterRequest", syncForm);
    document.addEventListener("htmx:afterSettle", syncForm);

    // htmx 1.9.12 does not swap error responses, so a 400 / 404 fragment
    // aimed at the run area (a refused keyword, a confirm of a run no
    // longer held) would show nothing. Same pattern as the Cases file
    // area (``_cases_body.html``); nothing else is swapped.
    document.addEventListener("htmx:beforeSwap", function (event) {
        const detail = event.detail;
        if (!detail || !detail.target || detail.target.id !== "scraper-run") return;
        const status = detail.xhr ? detail.xhr.status : 0;
        if (status === 400 || status === 404) {
            detail.shouldSwap = true;
            detail.isError = false;
        }
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

    // ---- One run at a time ----------------------------------------------

    function syncForm() {
        const button = document.getElementById("scraper-extract");
        if (!button) return;
        const mount = document.getElementById("scraper-run");
        const held = !!(mount && mount.querySelector('[data-pf-rd="dedup"]'));
        button.disabled = running || held;
        const idle = document.querySelector('[data-pf-rd="hint-idle"]');
        const busy = document.querySelector('[data-pf-rd="hint-running"]');
        if (idle) idle.hidden = running;
        if (busy) busy.hidden = !running;
    }

    function setRunning(value) {
        running = value;
        syncForm();
    }

    // ---- Keyword editor -------------------------------------------------

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const addBtn = target.closest('[data-pf-rd="keyword-add"]');
        if (addBtn) {
            const template = document.getElementById("scraper-keyword-template");
            const row = template ? template.content.firstElementChild : null;
            if (!row || !addBtn.parentNode) return;
            const added = row.cloneNode(true);
            addBtn.parentNode.insertBefore(added, addBtn);
            const nameInput = added.querySelector('[data-pf-rd="keyword-name"]');
            if (nameInput) nameInput.focus();
            return;
        }
        const removeBtn = target.closest('[data-pf-rd="keyword-remove"]');
        if (removeBtn) {
            const row = removeBtn.closest('[data-pf-rd="keyword"]');
            if (row) row.remove();
        }
    });

    function collectKeywordsJson() {
        const rows = document.querySelectorAll('#scraper-keywords [data-pf-rd="keyword"]');
        const out = [];
        rows.forEach(function (row) {
            const name = (row.querySelector('[data-pf-rd="keyword-name"]') || {}).value || "";
            const type = (row.querySelector('[data-pf-rd="keyword-type"]') || {}).value || "Text";
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

        const progressContainer = el.querySelector('[data-pf-rd="run-progress"]');
        const resultsContainer = el.querySelector('[data-pf-rd="run-results"]');
        if (!progressContainer || !resultsContainer) return;

        wireCancelButtons(el);
        setRunning(true);

        const eventSource = new EventSource(url);

        // One interval at most: the time since the last ``progress`` swap.
        let elapsedTimer = null;

        function stopElapsed() {
            if (elapsedTimer !== null) {
                clearInterval(elapsedTimer);
                elapsedTimer = null;
            }
        }

        function startElapsed() {
            stopElapsed();
            const span = progressContainer.querySelector("[data-pf-scraper-elapsed]");
            if (!span) return;
            const startedAt = Date.now();
            elapsedTimer = setInterval(function () {
                if (!el.isConnected) {
                    // The mount left the DOM: the run ends for this view.
                    finish();
                    return;
                }
                span.textContent = formatElapsed(Date.now() - startedAt);
            }, 1000);
        }

        // Ends the run in this view: the timer, the stream, the button.
        function finish() {
            stopElapsed();
            eventSource.close();
            setRunning(false);
        }

        function showResults(html) {
            progressContainer.replaceChildren();
            resultsContainer.innerHTML = html || "";
            if (window.htmx) window.htmx.process(resultsContainer);
        }

        // Replaces the progress frame by a note cloned from one of the
        // mount's templates; ``text`` becomes the note's first paragraph.
        function showNote(hook, text) {
            const template = el.querySelector('template[data-pf-rd="' + hook + '"]');
            if (!template) {
                progressContainer.textContent = text || "";
                return;
            }
            const note = template.content.cloneNode(true);
            const lead = note.querySelector("p");
            if (lead && text) lead.textContent = text;
            progressContainer.replaceChildren(note);
        }

        eventSource.addEventListener("progress", function (event) {
            progressContainer.innerHTML = event.data || "";
            wireCancelButtons(el);
            startElapsed();
        });

        eventSource.addEventListener("result", function (event) {
            finish();
            showResults(event.data);
        });

        eventSource.addEventListener("cancelled", function (event) {
            finish();
            showResults(event.data);
        });

        eventSource.addEventListener("error", function (event) {
            // Server-emitted ``error`` event — it carries data. The
            // transport-level failure arrives on the same event name
            // without data and is handled by ``onerror`` below.
            if (!event || typeof event.data !== "string") return;
            finish();
            showNote("run-error", event.data || "An unknown error occurred.");
        });

        // Transport-level failure: render once and close, no retry.
        eventSource.onerror = function () {
            if (eventSource.readyState === EventSource.CLOSED) return;
            finish();
            showNote("run-lost", "");
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

    // ``m:ss`` — minutes unpadded, seconds two digits.
    function formatElapsed(ms) {
        const seconds = Math.floor(ms / 1000);
        const rest = seconds % 60;
        return Math.floor(seconds / 60) + ":" + (rest < 10 ? "0" : "") + rest;
    }
})();
