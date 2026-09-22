/* PortfoliFLOW — command palette.
 *
 * Sub-stream 6F-2, narrowed by P-UX-A0b. The palette is a native
 * ``<dialog>``: Cmd/Ctrl+K or a view header's Search control opens it,
 * the input field is debounced and fetches ``/api/cmd-search``, arrow
 * keys navigate, Enter activates, Escape closes.
 *
 * The scroll-spy that used to share this file went with the right-edge
 * dot strip (P-UX-A0b): an area now shows one section at a time, so
 * there is no scroll position to spy on — ``shell.js`` marks the
 * current section from the URL fragment instead.
 *
 * The script is loaded with ``defer`` from ``base.html``, so the DOM is
 * parsed before this code runs. The Search control lives inside
 * ``#shell-main`` and is re-rendered by every area swap, so its click
 * handler is delegated on ``document`` rather than bound per button.
 */

(function () {
    "use strict";

    // --- Command palette --------------------------------------------

    const PALETTE_DEBOUNCE_MS = 120;

    let paletteDialog = null;
    let paletteInput = null;
    let paletteResults = null;
    let paletteRows = [];
    let paletteActiveIndex = -1;
    let paletteDebounceHandle = null;
    let paletteFetchToken = 0;

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function renderPaletteResults(payload) {
        const groups = [
            { key: "areas", label: "Areas" },
            { key: "sections", label: "Sections" },
            { key: "actions", label: "Actions" },
        ];
        const parts = [];
        let totalRows = 0;
        groups.forEach(function (group) {
            const entries = payload[group.key] || [];
            if (entries.length === 0) {
                return;
            }
            parts.push(
                '<li class="pf-palette__group-heading" role="presentation">' +
                    escapeHtml(group.label) +
                    "</li>"
            );
            entries.forEach(function (entry) {
                const meta = entry.area ? entry.area : group.key;
                parts.push(
                    '<li class="pf-palette__row" role="option" tabindex="-1" ' +
                        'data-url="' +
                        escapeHtml(entry.url) +
                        '" data-kind="' +
                        escapeHtml(group.key) +
                        '" data-index="' +
                        totalRows +
                        '">' +
                        '<span class="pf-palette__row-label">' +
                        escapeHtml(entry.label) +
                        "</span>" +
                        '<span class="pf-palette__row-meta">' +
                        escapeHtml(meta) +
                        "</span>" +
                        "</li>"
                );
                totalRows += 1;
            });
        });

        if (totalRows === 0) {
            paletteResults.innerHTML =
                '<li class="pf-palette__empty">No matches.</li>';
            paletteRows = [];
            paletteActiveIndex = -1;
            return;
        }

        paletteResults.innerHTML = parts.join("");
        paletteRows = Array.prototype.slice.call(
            paletteResults.querySelectorAll(".pf-palette__row")
        );
        paletteActiveIndex = paletteRows.length > 0 ? 0 : -1;
        updatePaletteActive();
    }

    function updatePaletteActive() {
        paletteRows.forEach(function (row, idx) {
            if (idx === paletteActiveIndex) {
                row.classList.add("is-active");
                row.scrollIntoView({ block: "nearest" });
            } else {
                row.classList.remove("is-active");
            }
        });
    }

    function fetchPaletteResults(query) {
        const token = ++paletteFetchToken;
        const url =
            "/api/cmd-search?q=" + encodeURIComponent(query || "");
        fetch(url, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        })
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("cmd-search " + resp.status);
                }
                return resp.json();
            })
            .then(function (payload) {
                if (token !== paletteFetchToken) {
                    return;
                }
                renderPaletteResults(payload);
            })
            .catch(function () {
                if (token !== paletteFetchToken) {
                    return;
                }
                paletteResults.innerHTML =
                    '<li class="pf-palette__empty">Search unavailable.</li>';
                paletteRows = [];
                paletteActiveIndex = -1;
            });
    }

    function activatePaletteRow(row) {
        if (!row) {
            return;
        }
        const url = row.getAttribute("data-url");
        const kind = row.getAttribute("data-kind");
        closePalette();
        if (!url) {
            return;
        }
        if (kind === "areas" && typeof window.htmx !== "undefined") {
            window.htmx.ajax("GET", url, { target: "#shell-main" });
            if (window.history && typeof window.history.pushState === "function") {
                window.history.pushState({}, "", url);
            }
            return;
        }
        window.location.assign(url);
    }

    function openPalette() {
        if (!paletteDialog || paletteDialog.open) {
            return;
        }
        paletteDialog.showModal();
        if (paletteInput) {
            paletteInput.value = "";
            paletteInput.focus();
        }
        fetchPaletteResults("");
    }

    function closePalette() {
        if (paletteDialog && paletteDialog.open) {
            paletteDialog.close();
        }
    }

    function isPaletteHotkey(event) {
        if (event.key !== "k" && event.key !== "K") {
            return false;
        }
        return event.metaKey || event.ctrlKey;
    }

    function bindPalette() {
        paletteDialog = document.getElementById("pf-palette");
        if (!paletteDialog) {
            return;
        }
        paletteInput = paletteDialog.querySelector(".pf-palette__input");
        paletteResults = paletteDialog.querySelector(".pf-palette__results");

        document.addEventListener("keydown", function (event) {
            if (isPaletteHotkey(event)) {
                event.preventDefault();
                if (paletteDialog.open) {
                    closePalette();
                } else {
                    openPalette();
                }
            }
        });

        paletteDialog.addEventListener("keydown", function (event) {
            if (event.key === "ArrowDown") {
                event.preventDefault();
                if (paletteRows.length === 0) {
                    return;
                }
                paletteActiveIndex =
                    (paletteActiveIndex + 1) % paletteRows.length;
                updatePaletteActive();
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                if (paletteRows.length === 0) {
                    return;
                }
                paletteActiveIndex =
                    (paletteActiveIndex - 1 + paletteRows.length) %
                    paletteRows.length;
                updatePaletteActive();
            } else if (event.key === "Enter") {
                event.preventDefault();
                if (paletteActiveIndex >= 0) {
                    activatePaletteRow(paletteRows[paletteActiveIndex]);
                }
            }
        });

        if (paletteInput) {
            paletteInput.addEventListener("input", function () {
                if (paletteDebounceHandle !== null) {
                    clearTimeout(paletteDebounceHandle);
                }
                const query = paletteInput.value;
                paletteDebounceHandle = window.setTimeout(function () {
                    paletteDebounceHandle = null;
                    fetchPaletteResults(query);
                }, PALETTE_DEBOUNCE_MS);
            });
        }

        if (paletteResults) {
            paletteResults.addEventListener("click", function (event) {
                const row = event.target.closest(".pf-palette__row");
                if (row) {
                    activatePaletteRow(row);
                }
            });
            paletteResults.addEventListener("mousemove", function (event) {
                const row = event.target.closest(".pf-palette__row");
                if (!row) {
                    return;
                }
                const idx = parseInt(row.getAttribute("data-index"), 10);
                if (!Number.isNaN(idx) && idx !== paletteActiveIndex) {
                    paletteActiveIndex = idx;
                    updatePaletteActive();
                }
            });
        }
    }

    // Delegated: each area swap re-renders the view header, so a handler
    // bound to the button itself would be dropped with the old markup.
    document.addEventListener("click", function (event) {
        const trigger = event.target.closest("[data-pf-palette-open]");
        if (!trigger) {
            return;
        }
        event.preventDefault();
        openPalette();
    });

    document.addEventListener("DOMContentLoaded", bindPalette);
})();
