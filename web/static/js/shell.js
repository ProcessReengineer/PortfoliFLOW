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

    function show(slug) {
        if (!slug) {
            return;
        }
        sections().forEach(function (section) {
            section.hidden = section.getAttribute("data-pf-section") !== slug;
        });
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
    show(resolve());

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
