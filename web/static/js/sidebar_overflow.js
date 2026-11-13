/* PortfoliFLOW — the sidebar's overflow cue (P-GP-7).
 *
 * ``.pf-sidebar__nav`` scrolls without a visible bar (P-GP-3). This file
 * says when there is something to scroll to: it writes ``data-pf-overflow``
 * on the nav — ``below``, ``above`` or ``both`` — while entries lie beyond
 * that edge, and removes it when none do. ``layout.css`` fades the edge.
 *
 * This file is the only thing that writes that attribute. It issues no
 * request and touches no ``hx-*`` attribute.
 *
 * Nothing is bound to the nav element itself: an Area swap replaces the
 * whole sidebar out of band (``sidebar_oob.html``), so a listener on the
 * element would be lost with it. ``scroll`` does not bubble, hence the
 * capture listener on ``document``; the htmx listeners re-measure the
 * replaced element.
 */

(function () {
    "use strict";

    const NAV_SELECTOR = ".pf-sidebar__nav";
    const ATTRIBUTE = "data-pf-overflow";

    /* One pixel of slack: fractional layout heights at zoom levels other
     * than 100 % otherwise leave a phantom overflow. */
    const SLACK = 1;

    let queued = false;

    function measure() {
        queued = false;
        const nav = document.querySelector(NAV_SELECTOR);
        if (!nav) {
            return; // Defensive: every page that loads this script renders the nav.
        }
        const above = nav.scrollTop > SLACK;
        const below = nav.scrollHeight - nav.clientHeight - nav.scrollTop > SLACK;
        let next = "";
        if (above && below) {
            next = "both";
        } else if (below) {
            next = "below";
        } else if (above) {
            next = "above";
        }
        if (next === "") {
            nav.removeAttribute(ATTRIBUTE);
        } else if (nav.getAttribute(ATTRIBUTE) !== next) {
            nav.setAttribute(ATTRIBUTE, next);
        }
    }

    function schedule() {
        if (queued) {
            return;
        }
        queued = true;
        window.requestAnimationFrame(measure);
    }

    // Deferred, so the DOM is parsed: the first measure needs no event.
    measure();

    document.addEventListener(
        "scroll",
        function (event) {
            const target = event.target;
            if (target instanceof Element && target.matches(NAV_SELECTOR)) {
                schedule();
            }
        },
        { capture: true, passive: true }
    );

    window.addEventListener("resize", schedule);
    window.addEventListener("load", schedule);

    // An Area swap replaces the sidebar out of band and changes the
    // second level's length; both events are cheap and idempotent.
    document.body.addEventListener("htmx:oobAfterSwap", schedule);
    document.body.addEventListener("htmx:afterSettle", schedule);
})();
