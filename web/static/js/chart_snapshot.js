// Frozen-chart rendering — the one Plotly render path (ADR-0114).
//
// Three surfaces feed the same helper:
//
//   1. chat.js, for a chart arriving live on the SSE ``chart`` event;
//   2. the rehydrated chat history (``_partials/chat_history.html``),
//      restored from the per-session artefact sidecar;
//   3. the Cases timeline (``_partials/cases_detail_timeline.html``),
//      where a ``chart_snapshot`` pin renders from the journal payload.
//
// Cases 2 and 3 are server-rendered markup, so they declare themselves
// with data attributes rather than JavaScript:
//
//     <div ... data-pf-chart>
//       <div ... data-pf-chart-plot></div>
//       <script type="application/json" data-pf-chart-spec>{…}</script>
//     </div>
//
// The spec travels inside an ``application/json`` block — inert to the
// HTML parser and never interpolated into JavaScript source. Rendering is
// idempotent: a container is marked once rendered, so a repeated
// ``htmx:afterSwap`` over the same subtree does no work.
//
// Nothing here recomputes: the spec is the artefact, rendered verbatim.

(function () {
    "use strict";

    // Render ``spec`` into ``plotEl``. Returns a promise that settles when
    // the figure is on screen (or immediately when it cannot be drawn), so
    // callers can sequence layout work — e.g. chat's scroll-to-bottom.
    function renderSpec(plotEl, spec) {
        if (!plotEl) return Promise.resolve();
        if (!window.Plotly) {
            // Plotly.js failed to load — degrade to a clear inline message
            // rather than throwing, exactly as the live SSE path does.
            plotEl.innerHTML =
                '<span class="chat-error">Chart unavailable: ' +
                "Plotly.js did not load.</span>";
            return Promise.resolve();
        }
        const figure = spec || {};
        return window.Plotly.newPlot(
            plotEl,
            figure.data || [],
            figure.layout || {},
            figure.config || {}
        );
    }

    // Render every not-yet-rendered ``[data-pf-chart]`` container inside
    // ``root`` (which may itself be one). Returns a promise resolving when
    // all of them have settled.
    function initRoot(root) {
        const scope = root && root.querySelectorAll ? root : document;
        const containers = [];
        if (scope.matches && scope.matches("[data-pf-chart]")) containers.push(scope);
        scope.querySelectorAll("[data-pf-chart]").forEach(function (el) {
            containers.push(el);
        });

        const pending = [];
        containers.forEach(function (container) {
            if (container.dataset.pfChartRendered === "1") return;
            container.dataset.pfChartRendered = "1";

            const plotEl = container.querySelector("[data-pf-chart-plot]");
            const specEl = container.querySelector("[data-pf-chart-spec]");
            if (!plotEl || !specEl) return;

            let spec;
            try {
                spec = JSON.parse(specEl.textContent || "{}");
            } catch (err) {
                console.error("chart_snapshot.js: malformed chart spec", err);
                plotEl.innerHTML =
                    '<span class="chat-error">Chart unavailable: ' +
                    "the stored figure could not be read.</span>";
                return;
            }

            pending.push(
                renderSpec(plotEl, spec)
                    .catch(function (err) {
                        // A figure Plotly refuses is one dead frame, never a
                        // broken page — and never an unhandled rejection.
                        console.error("chart_snapshot.js: Plotly refused the spec", err);
                        plotEl.innerHTML =
                            '<span class="chat-error">Chart unavailable: ' +
                            "the stored figure could not be drawn.</span>";
                    })
                    .then(function () {
                        // Let the hosting surface react once the figure exists
                        // (the chat pane scrolls to the bottom on this).
                        container.dispatchEvent(
                            new CustomEvent("pf:chart-rendered", { bubbles: true })
                        );
                    })
            );
        });
        return Promise.all(pending);
    }

    window.pfChartSnapshot = { render: renderSpec, init: initRoot };

    // Server-rendered charts arrive either in an HTMX swap (chat history,
    // timeline refresh) or in the initial page (case detail).
    document.addEventListener("htmx:afterSwap", function (event) {
        if (event.target) initRoot(event.target);
    });
    document.addEventListener("DOMContentLoaded", function () {
        initRoot(document);
    });
})();
