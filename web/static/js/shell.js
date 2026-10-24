/* PortfoliFLOW — one section per view (P-UX-A0b).
 *
 * An area renders every one of its sections, but shows one: the URL
 * fragment selects it, the rest carry the ``hidden`` attribute. This
 * script is the only thing that moves that attribute around. It issues
 * no request and touches no ``hx-*`` attribute — the sections it leaves
 * hidden simply never intersect, so their ``intersect once`` loaders
 * never fire.
 */

(function () {
    "use strict";

    function sections() {
        return document.querySelectorAll("#shell-main [data-pf-section]");
    }

    // Assistants · Shirley IS the stage (P-GP-4). Showing that Section
    // opens the stage; showing any other Section — in any Area — leaves it,
    // so a view the reader navigated to is never loaded unseen beneath the
    // stage. shirley.js is loaded before this file, so the global is there;
    // the guard is for the auth pages, which load neither.
    //
    // One exception, on the first show of a page load only: a state the
    // server rendered ``docked`` stands. Only ``/assistants?case=…`` does
    // that — a case's "Consult Shirley" (ADR-0107 C6) asked for the dock.
    function syncStage(slug, initial) {
        if (!window.pfShirley) {
            return;
        }
        const current = window.pfShirley.state();
        const onShirley =
            slug === "shirley" && document.querySelector('[data-area="assistants"]') !== null;
        if (onShirley) {
            if (current === "stage" || (initial && current === "docked")) {
                return;
            }
            window.pfShirley.setState("stage");
        } else if (current === "stage") {
            window.pfShirley.leaveStage();
        }
    }

    function show(slug, initial) {
        if (!slug) {
            return;
        }
        sections().forEach(function (section) {
            section.hidden = section.getAttribute("data-pf-section") !== slug;
        });
        // The stage follows the shown Section — see syncStage().
        syncStage(slug, initial);
        document.querySelectorAll("[data-pf-section-link]").forEach(function (link) {
            if (link.getAttribute("data-pf-section-link") === slug) {
                link.setAttribute("aria-current", "true");
            } else {
                link.removeAttribute("aria-current");
            }
        });
        // No focus move: switching views is navigation the reader asked
        // for, and stealing focus would fight the fragment link itself.
        window.scrollTo(0, 0);
    }

    function resolve() {
        const all = sections();
        if (all.length === 0) {
            return null;
        }
        const fragment = window.location.hash.slice(1);
        for (let i = 0; i < all.length; i += 1) {
            if (all[i].getAttribute("data-pf-section") === fragment) {
                return fragment;
            }
        }
        // No fragment, or one naming something else on the page: fall
        // back to the section the server left visible — the landing view.
        for (let i = 0; i < all.length; i += 1) {
            if (!all[i].hidden) {
                return all[i].getAttribute("data-pf-section");
            }
        }
        return all[0].getAttribute("data-pf-section");
    }

    // Immediately, not on DOMContentLoaded. The script is deferred, so
    // the DOM is parsed but htmx has NOT yet processed the body — its own
    // handler runs on DOMContentLoaded, i.e. after every deferred script.
    // Hiding the non-landing sections here is therefore what keeps their
    // loaders from ever being armed against a visible element.
    show(resolve(), true);

    window.addEventListener("hashchange", function () {
        show(resolve());
    });

    // An area swap arrives with its own landing section visible and the
    // fragment possibly left over from the previous area; resolve()
    // handles both, so the same call serves.
    document.body.addEventListener("htmx:afterSwap", function (event) {
        if (event.target && event.target.id === "shell-main") {
            show(resolve());
        }
    });
})();
