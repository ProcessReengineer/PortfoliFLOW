/* PortfoliFLOW — the Reading Desk Section's tabs and its reading (P-RD-2b1).
 *
 * Moves the Read and Library panes and the reading sub-surface
 * (#rd-reading) by their ``hidden`` attribute, and mirrors the selected tab
 * in ``aria-selected`` and an open toggle in ``aria-expanded``. It reads and
 * writes no class. It issues no request of its own: showing the Library
 * asks htmx to fire the loader or the filter form the server rendered, and
 * the Section head is restored by the way back's own ``hx-get``.
 *
 * The panes are only ever hidden, never swapped away, so a run streaming in
 * the Read pane survives every move here. The frame arrives lazily, so
 * every handler is delegated from ``document`` and looks its elements up
 * under ``[data-pf-rd="root"]`` when the event happens.
 */

(function () {
    "use strict";

    // The tab to return to when a reading opened from a tab is closed.
    let remembered = null;

    function root() {
        return document.querySelector('[data-pf-rd="root"]');
    }

    function hasTab(container, tab) {
        return container.querySelector('[data-pf-rd="tab"][data-pf-rd-tab="' + tab + '"]') !== null;
    }

    function activate(tab) {
        const container = root();
        if (!container) {
            return;
        }
        const wanted = hasTab(container, tab) ? tab : "library";
        container.querySelectorAll('[data-pf-rd="tab"]').forEach(function (button) {
            const selected = button.getAttribute("data-pf-rd-tab") === wanted;
            button.setAttribute("aria-selected", selected ? "true" : "false");
        });
        container.querySelectorAll('[data-pf-rd="pane"]').forEach(function (pane) {
            pane.hidden = pane.getAttribute("data-pf-rd-pane") !== wanted;
        });
        if (wanted === "library" && window.htmx) {
            const form = container.querySelector('#rd-library [data-pf-rd="filters"]');
            if (form) {
                // A refresh that keeps the filters.
                window.htmx.trigger(form, "submit");
            } else {
                window.htmx.trigger("#rd-library", "rd:load");
            }
        }
    }

    function selectedTab(container) {
        const selected = container.querySelector('[data-pf-rd="tab"][aria-selected="true"]');
        return selected ? selected.getAttribute("data-pf-rd-tab") : null;
    }

    function openReading() {
        const container = root();
        if (!container) {
            return;
        }
        const tabs = container.querySelector('[data-pf-rd="tabs"]');
        if (tabs && !tabs.hidden) {
            remembered = selectedTab(container);
            tabs.hidden = true;
        }
        container.querySelectorAll('[data-pf-rd="pane"]').forEach(function (pane) {
            pane.hidden = true;
        });
        const reading = container.querySelector('[data-pf-rd="reading"]');
        if (reading) {
            reading.hidden = false;
        }
        window.scrollTo(0, 0);
    }

    function closeReading(tab) {
        const container = root();
        if (!container) {
            return;
        }
        const reading = container.querySelector('[data-pf-rd="reading"]');
        if (reading) {
            reading.replaceChildren();
            reading.hidden = true;
        }
        const tabs = container.querySelector('[data-pf-rd="tabs"]');
        if (tabs) {
            tabs.hidden = false;
        }
        activate(tab || remembered || "library");
        remembered = null;
    }

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) {
            return;
        }
        const tab = target.closest('[data-pf-rd="tab"]');
        if (tab) {
            activate(tab.getAttribute("data-pf-rd-tab"));
            return;
        }
        const goto = target.closest('[data-pf-rd="goto"]');
        if (goto) {
            event.preventDefault();
            activate(goto.getAttribute("data-pf-rd-tab"));
            return;
        }
        const back = target.closest('[data-pf-rd="back"]');
        if (back) {
            // htmx has already sent the button's own hx-get for the head.
            closeReading(back.getAttribute("data-pf-rd-tab"));
            return;
        }
        const toggle = target.closest("[data-pf-rd-toggle]");
        if (toggle) {
            const panel = document.getElementById(toggle.getAttribute("data-pf-rd-toggle"));
            if (panel) {
                panel.hidden = !panel.hidden;
                toggle.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
            }
        }
    });

    document.addEventListener("htmx:afterSwap", function (event) {
        const swapped = event.detail && event.detail.target;
        if (swapped && swapped.id === "rd-reading") {
            openReading();
        }
    });
})();
