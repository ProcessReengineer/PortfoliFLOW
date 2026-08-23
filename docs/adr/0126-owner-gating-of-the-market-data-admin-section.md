# ADR-0126 — Owner-gating of the Market Data admin section

Date: 2026-08-23
Status: Accepted

## Context

ADR-0125 §6 owner-gated the "Refresh now" action on the reasoning that
"nothing observable changes in Admin, which is already an owner surface
under ADR-0121". That premise is false. ADR-0121 made the **Users**
section owner-only; the Admin *area* itself renders for every
authenticated tenant user, and `is_tenant_owner` is documented in
`web/routes/areas.py` as cosmetic mirroring — the authoritative gate
exists only on the `tenant_users` routes.

Three consequences of that false premise are live today:

1. `POST /api/market-data/schedule` (`save_schedule`) carries no role
   gate. Any member can change the tenant's refresh cadence, anchor
   hour, timezone, and enabled flag — a tenant-level configuration that
   spends the tenant's provider budget.
2. The Market Data section renders for members, offering a form whose
   save any member can use and a "Refresh now" button that silently
   does nothing for them (the route 403s; HTMX swaps nothing on 4xx).
   A silently dead affordance violates the no-silent-fallback
   principle.
3. `README-market-data-tick.md` states "an owner opts in", which the
   code does not enforce for the opt-in itself.

The schedule is a tenant-level resource in the same sense as tenant
user management: it moves a shared cursor and spends shared budget.
The owner-gating vocabulary is established: `require_role("owner")`
(authoritative, route layer) plus `is_tenant_owner` (cosmetic,
template layer), per ADR-0121 §6.

Unlike Providers & Credentials and Users, the Market Data section is
rendered **eagerly** in the `/admin` request via
`load_market_data_section_context` — there is no lazy section GET to
gate.

## Decision

Make the Market Data admin section an owner surface, following the
ADR-0121 §6 pattern as implemented for the Users section:

1. **Route gate (authoritative).** `save_schedule` in
   `web/routes/market_data.py` gains
   `_owner: UserDTO = Depends(require_role("owner"))`. Both mutating
   routes of the module are then owner-gated.
2. **Template gate (cosmetic).** The Market Data include in
   `web/templates/_partials/areas/_admin_body.html` moves inside the
   same `{% if is_tenant_owner %}` conditional as the Users section,
   with the same comment pattern. A member sees no Market Data section;
   the dead "Refresh now" affordance disappears with it.
3. **Eager load skipped for non-owners.** `admin_view` in
   `web/routes/areas.py` calls `load_market_data_section_context` only
   when the user is a tenant owner and spreads an empty context
   otherwise. A member's `/admin` request no longer pays a DB read for
   a section it never renders; the missing context keys are harmless
   because the only consumer is the include inside the owner
   conditional.
4. **The refresh poll stays ungated — a deliberate exception.**
   `GET /api/market-data/refresh/poll` remains on `require_session`.
   Gating it through `require_role` would route every poll through
   `require_authenticated_session` and its idle-timer touch, so an
   abandoned tab's poller would keep the session alive — exactly what
   the ADR-0120 poll discipline exists to prevent. The cost is that a
   member who calls the URL by hand can read the panel fragment
   (cadence, enabled flag, last-run stamp). This leaks configuration
   cosmetics, not secrets: the member-facing freshness surface
   (ADR-0125 §7, the Overview line) shows the data freshness anyway.
   The poll is read-only and mutates nothing.

## Consequences

- Members lose the "next due / last run" detail in Admin. Their
  freshness surface is the Front Office Overview line (ADR-0125).
- `README-market-data-tick.md`'s "an owner opts in" becomes literally
  true; no README change is needed.
- This ADR supersedes **one sentence** of ADR-0125 §6 — "Nothing
  observable changes in Admin, which is already an owner surface under
  ADR-0121" — which was written on a false premise. The rest of
  ADR-0125 §6, including the owner gate on `refresh_now` itself,
  stands unchanged. ADR-0125 is not edited (ADR immutability).
- Two of the module's three routes are owner-gated; the poll is a
  documented exception (Decision 4), not a gap.
- The docstring of `refresh_now`, which repeats the false premise,
  is corrected in the implementation of this ADR — a docstring is
  code commentary, not an accepted ADR, and may track reality.
