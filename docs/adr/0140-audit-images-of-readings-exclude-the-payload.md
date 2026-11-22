# ADR-0140: Audit Images of Readings Exclude the Payload — a Column-Excluding Audit Trigger for `document_readings`

- **Status:** Accepted (2026-11-22)
- **Date:** 2026-11-22
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #095 (Reading Desk v1)
- **Amends:** ADR-0139 §5, the *Audit* clause, for `document_readings` only (annex amendment). ADR-0139 stays Accepted and unedited; this ADR records the revision (ADR-0134 §3 precedence). It revises RD-D-20 of the concept record for the same table.
- **Honours:** ADR-0035/0036 (RLS, the audit trigger and the `app.user_id` GUC — the generic function is not changed), ADR-0112 §6 (what must not persist in `audit_log` is kept out of it — the b032 precedent), ADR-0139 §5 (removing a reading purges its payload), ADR-0008 (English-only codebase).
- **Numbering note:** ADR-0139 §9 and the concept record expected the number 0140 for the Cases annex on the kept class `reading_excerpt`. That annex takes the next free number when it is written.
- **Tags:** reading-desk, audit, schema, persistence, retention, multi-tenant, bait-vait

---

## Context

ADR-0139 §5 decides two things about a reading that do not hold together in the tree as it stands:

- **Removal.** Removing a reading leaves a tombstone: identity and removal fields stay; `findings`, `note` and `search_text` are purged. A retention run does the same for every reading older than its cutoff (RD-D-8).
- **Audit.** `document_readings` and `reading_purges` carry `audit_trigger_function()` (RD-D-20).

`audit_trigger_function()` (initial schema) writes full row images — `to_jsonb(OLD)` and `to_jsonb(NEW)`, with no column exclusion — into `audit_log`. On `document_readings` it would copy `findings`, `note` and `search_text` into `audit_log` when the reading is written and again, as old and new image, on every later update: an assignment, a removal, a retention run. A tombstone would then empty the reading's row while its payload stayed in `audit_log` indefinitely, readable there by the tenant's `auditor` role. "Removed" would not mean removed, and a retention run would purge nothing from the database.

The tree knows this property of the function: migration b032 left the trigger off `scoped_settings` because a full image would have copied secret ciphertext into `audit_log` (ADR-0112 §6), and says so in its docstring.

Strand RD-1 found the collision in its verify-first pass, before migration b038 was written (question Q-RD-1-1 to Mission Control, 2026-11-22). The owner decided it the same day.

Audit relevance: BAIT/VAIT traceability of who read, assigned and removed which document, and the effectiveness of a deletion the application promises.

## Decision

1. **A second trigger function for `document_readings`.** Migration b038 creates `audit_trigger_function_excluding`, with the body of `audit_trigger_function()` and one difference: the column names passed as trigger arguments are subtracted from both row images before the `audit_log` row is written. `document_readings` carries this function, with the arguments `findings`, `note`, `search_text`.
2. **The excluded columns are the purged columns.** The audit image of a reading leaves out exactly the columns a tombstone purges — no more, no fewer. A test holds the two lists equal.
3. **Everything else is audited as before.** Identity (number, file name, `sha256`, size, page count, read at, read by, origin), classification, subject and assignment, provenance, and the removal fields with the reason and the purge reference appear in every image. Tenant, record id, operation and acting user are attributed exactly as the generic function attributes them.
4. **`reading_purges` carries the generic `audit_trigger_function()`**, unchanged.
5. **The generic function is not touched,** and no other table uses the excluding function. Using it elsewhere is a decision of its own.
6. **`audit_log` is never edited.** Nothing is scrubbed from it after the fact; the payload does not enter it in the first place.

## Rationale

- A reading's payload is written once. ADR-0139 gives it no edit path — the document type is fixed (§3) and nothing edits findings or the note — so the only change it ever sees is its purge. An audit trail of payload changes would record nothing but copies.
- What an audit of readings has to answer — who read which document when, who assigned it to what, who removed it and why, which retention run took it — is answered by the columns that stay.
- A deletion the surface offers must be a deletion in the database. GP documents can name persons, and the retention run exists so that old content actually leaves.
- Without the exclusion `audit_log` would grow by two copies of 10–50 KB on every assignment of a reading.

## Alternatives Considered

- **The generic trigger, the retained payload accepted.** Rejected: it voids the removal and the retention run of ADR-0139 §5.
- **No audit trigger on `document_readings`** (the b031 / b037 idiom). Rejected: assignment and removal would lose their actor attribution in `audit_log`; the table is not its own append-only record.
- **Scrubbing `audit_log` when a reading is removed.** Rejected: the audit log is append-only; an application path that rewrites it is worse than the problem it solves.
- **Extending `audit_trigger_function()` itself with optional arguments.** Rejected: every audited table depends on that function; a change to it for one table's sake puts all of them at risk, and a separate function is dropped cleanly on downgrade.
- **Moving the payload into a second table without a trigger.** Rejected: it changes the schema of ADR-0139 §2 and every read path for no gain over excluding three columns.

## Consequences

### Positive

- Removing a reading and running retention remove its content from the database.
- The audit trail of readings stays complete for actions and actors, and small.

### Negative / accepted

- The audit image of a reading is not a full row image: the content of a reading cannot be reconstructed from `audit_log`. That is the intent.
- Two audit functions exist. A later change to the generic function's attribution logic has to be mirrored in the excluding one.

### Neutral / follow-ups

- The content of a live reading is in its row; the content of a removed reading is gone.
- Documentation that describes the audit trigger as capturing full row images names this one exception (the track's close-out docs).

## Implementation Notes

- Migration `b038` (strand RD-1): `CREATE FUNCTION audit_trigger_function_excluding`, the trigger on `document_readings` with the three column names as arguments, the generic trigger on `reading_purges`; the downgrade drops trigger and function.
- Tests: the b038 round-trip regression test; a repository test asserting that no `audit_log` row of `document_readings` carries the keys `findings`, `note` or `search_text` after insert, assignment, removal and a retention run, that the removal's row carries the reason and the acting user, and that the excluded list equals the purged list.

## Compliance & Audit Relevance

- **ISO 25010:** security (confidentiality of removed content), functional suitability (a removal that removes), maintainability.
- **Regulatory references:** BAIT/VAIT traceability of reading, assignment, removal and retention runs — unchanged; data minimisation in the audit log.
- **Audit evidence:** the trigger definition in migration b038; the repository test named above; `audit_log` rows of `document_readings` and `reading_purges`.

## References

- ADR-0139 (§2, §3, §5), ADR-0035, ADR-0036, ADR-0112 (§6), ADR-0134 (§3).
- Migrations `initial_schema` (`audit_trigger_function`), `b032_add_scoped_settings` and `b033_add_watchpoints` (their docstrings on full row images).
- `docs/concepts/reading-desk-concept.md` (RD-D-8, RD-D-20).

---

## Revision History

| Date       | Author                     | Change                         |
|------------|----------------------------|--------------------------------|
| 2026-11-22 | PortfoliFLOW project owner | Initial draft, status Proposed |
| 2026-11-22 | PortfoliFLOW project owner | Accepted; index row added      |
