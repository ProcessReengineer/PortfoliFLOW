/* PortfoliFLOW — Shirley's three states and the one chat instance
 * (UX A-0, P-UX-A0e; record §2.10).
 *
 * Shirley is a shell element, not a Section body. ``.pf-shell`` carries
 * ``data-shirley`` with one of three values and ``layout.css`` does the
 * rest:
 *
 *   closed  — a 44 px rail at the right edge: one button, one dot.
 *   docked  — a 380 px column beside the work area. It PUSHES the
 *             content (the shell's third grid column widens); it never
 *             overlays it.
 *   stage   — the conversation at 420 px plus a canvas, filling the work
 *             area. ``#shell-main`` is hidden, not unmounted, so leaving
 *             the stage returns to exactly the view that was open.
 *
 * This file is the only thing that writes that attribute.
 *
 * ONE instance, moved — never cloned. ``#pf-chat`` is appended into
 * ``#dock-chat-host`` or ``#stage-chat-host`` as the state changes. Both
 * hosts live OUTSIDE ``#shell-main``, so an HTMX area swap never touches
 * them: the conversation, its ``EventSource`` and a half-streamed answer
 * all survive navigating to another Area. ``appendChild`` moves a live
 * subtree with its listeners intact, which is why ``htmx.process`` must
 * NOT be called after the move — it would double-bind every ``hx-*``.
 *
 * The keyboard lives in ``section_nav.js`` next to Ctrl K, one handler
 * file for the shell's shortcuts; it calls ``window.pfShirley.toggle``.
 */

(function () {
    "use strict";

    const OPEN_STATES = ["docked", "stage"];

    /** Last open state, so Ctrl J reopens where the reader left off. */
    let lastOpen = "docked";

    /* HTMX binds its triggers on DOMContentLoaded, which is AFTER every
     * deferred script — this one included, and shell.js after it. An
     * open resolved during that window (a server-rendered ``docked``, or
     * shell.js reading ``#shirley``) would therefore dispatch
     * ``pf:shirley-open`` at a host that is not listening yet, so the
     * first dispatch is held until htmx has processed the body. */
    let htmxReady = false;
    let openPending = false;

    function shell() {
        return document.querySelector(".pf-shell");
    }

    function state() {
        const el = shell();
        return (el && el.getAttribute("data-shirley")) || "closed";
    }

    /** Arm the dock host's one-time ``hx-get="/chat/dock"``. */
    function armDock() {
        const host = document.getElementById("dock-chat-host");
        if (!host) {
            return;
        }
        if (!htmxReady) {
            openPending = true;
            return;
        }
        host.dispatchEvent(new CustomEvent("pf:shirley-open"));
    }

    /** Move the one chat instance into the host the state names. */
    function relocate(next) {
        const chat = document.getElementById("pf-chat");
        if (!chat) {
            return; // Not loaded yet — the first open fetches it.
        }
        const hostId = next === "stage" ? "stage-chat-host" : "dock-chat-host";
        const host = document.getElementById(hostId);
        if (host && chat.parentElement !== host) {
            host.appendChild(chat);
        }
    }

    /** Flip the stage toggle's name to match where the conversation is. */
    function labelStageToggle(next) {
        const button = document.querySelector("[data-toggle-stage]");
        if (!button) {
            return;
        }
        const label = next === "stage" ? "Back to dock" : "Open on stage";
        button.setAttribute("aria-label", label);
        button.setAttribute("title", label);
    }

    function setState(next) {
        if (next !== "closed" && OPEN_STATES.indexOf(next) === -1) {
            return;
        }
        const el = shell();
        if (!el) {
            return;
        }
        const previous = state();
        el.setAttribute("data-shirley", next);

        if (next === "stage") {
            // Belt and braces with the CSS state rule: the markup ships
            // the stage ``hidden`` for the no-CSS case.
            const stage = document.getElementById("pf-stage");
            if (stage) {
                stage.hidden = false;
            }
        }

        if (next !== "closed") {
            lastOpen = next;
            armDock();
        }

        relocate(next);
        labelStageToggle(next);

        if (next === "closed") {
            if (previous !== "closed") {
                const main = document.getElementById("shell-main");
                if (main) {
                    main.focus();
                }
            }
        } else if (previous === "closed") {
            const input = document.getElementById("chat-input");
            if (input) {
                input.focus();
            }
        }
    }

    function toggle() {
        setState(state() === "closed" ? lastOpen : "closed");
    }

    // Delegated on the document: the dock's own buttons arrive later, in
    // the /chat/dock swap, and move between hosts with the conversation.
    document.addEventListener("click", function (event) {
        const target =
            event.target && event.target.closest
                ? event.target.closest("[data-set-shirley], [data-toggle-stage]")
                : null;
        if (!target) {
            return;
        }
        event.preventDefault();
        if (target.hasAttribute("data-set-shirley")) {
            setState(target.getAttribute("data-set-shirley"));
        } else {
            setState(state() === "stage" ? "docked" : "stage");
        }
    });

    // The chat always lands in the dock host (that is where the fetch
    // is); if the reader went straight to the stage, move it on arrival.
    document.body.addEventListener("htmx:afterSwap", function (event) {
        if (event.target && event.target.id === "dock-chat-host") {
            relocate(state());
            labelStageToggle(state());
            const input = document.getElementById("chat-input");
            if (input) {
                input.focus();
            }
        }
    });

    document.addEventListener("DOMContentLoaded", function () {
        htmxReady = true;
        if (openPending) {
            openPending = false;
            armDock();
        }
    });

    // A server-rendered open state (``/assistants?case=…`` arrives
    // docked) needs the same arming the click path does.
    if (state() !== "closed") {
        lastOpen = state();
        armDock();
    }

    window.pfShirley = { setState: setState, toggle: toggle, state: state };
})();
