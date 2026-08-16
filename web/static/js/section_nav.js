/* PortfoliFLOW — section indicator scroll-spy and command palette.
 *
 * Sub-stream 6F-2. Two responsibilities live in this file:
 *
 *   1. The section indicator's scroll-spy: an IntersectionObserver
 *      watches each ``.pf-section`` and toggles ``is-active`` on the
 *      matching ``.pf-section-indicator__dot``.
 *   2. The command palette: Cmd/Ctrl+K opens a native ``<dialog>``;
 *      the input field is debounced and fetches ``/api/cmd-search``;
 *      arrow keys navigate, Enter activates, Escape closes.
 *
 * The script is loaded with ``defer`` from ``base.html``, so the DOM
 * is parsed before this code runs. HTMX area swaps replace the
 * ``#shell-main`` subtree, which drops the previous indicator's dots
 * out of the observer; the ``htmx:afterSwap`` handler rebinds the
 * observer to the new dots.
 */

(function () {
    "use strict";

    // --- Scroll-spy --------------------------------------------------

    let scrollSpyObserver = null;

    function bindScrollSpy() {
        if (scrollSpyObserver !== null) {
            scrollSpyObserver.disconnect();
            scrollSpyObserver = null;
        }

        const dots = document.querySelectorAll(
            ".pf-section-indicator__dot"
        );
        if (dots.length === 0) {
            return;
        }
        const dotBySlug = new Map();
        dots.forEach(function (dot) {
            dotBySlug.set(dot.getAttribute("data-section"), dot);
        });

        // Centre-of-viewport heuristic: a section counts as "current"
        // when its bounds straddle the middle 20 % of the viewport.
        scrollSpyObserver = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    const slug = entry.target.getAttribute("id");
                    if (!slug) {
                        return;
                    }
                    const dot = dotBySlug.get(slug);
                    if (!dot) {
                        return;
                    }
                    if (entry.isIntersecting) {
                        dots.forEach(function (other) {
                            other.classList.remove("is-active");
                        });
                        dot.classList.add("is-active");
                    }
                });
            },
            {
                root: null,
                rootMargin: "-40% 0px -40% 0px",
                threshold: 0,
            }
        );

        document.querySelectorAll(".pf-section").forEach(function (section) {
            scrollSpyObserver.observe(section);
        });
    }

    document.addEventListener("DOMContentLoaded", bindScrollSpy);
    document.body.addEventListener("htmx:afterSwap", bindScrollSpy);

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

    document.addEventListener("DOMContentLoaded", bindPalette);
})();
