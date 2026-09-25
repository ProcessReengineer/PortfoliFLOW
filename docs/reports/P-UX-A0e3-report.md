<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0e3 — the voice toggle wired from the document on the dock swap

**Hotfix, outside strand A-1** · **Date:** 2026-09-25 ·
**Ran after:** P-UX-A0e2 (`38f94b2`) ·
**Scope:** `web/static/js/chat.js` (one listener body), this report.
No refactoring, no full suite, no atlas, no test module touched.

Opened on the stage, Shirley's mic did nothing. `shirley.js` listens for
`htmx:afterSwap` on `document.body`; on the `/chat/dock` swap it relocates
`#pf-chat` into the stage host when the reader went straight to the stage.
`chat.js` listens on `document`, so it runs **after** that body listener and
called `initVoiceControls(event.target)` with `event.target =
#dock-chat-host` — by then an empty node, no toggle inside it, nothing
wired. Opened docked, nothing relocates and the same call found the toggle,
which is why the bug was invisible from the dock.

On that one swap the wiring now scopes from `document` instead of the swap
target, so it finds the toggle in whichever host the conversation ended up
in. `shirley.js` is unchanged: its ordering is by design (relocate before
focus).

---

## OPERATOR ACTION REQUIRED

### 1. Commit

```
git add web/static/js/chat.js docs/reports/P-UX-A0e3-report.md
git commit -m "fix(chat): wire the voice toggle from the document on the dock swap — shirley.js relocates the conversation before chat.js sees it (UX hotfix, P-UX-A0e3)"
```

### 2. Browser round — the stage path

Reload the page (the dock fragment is fetched once per page life), then on
`minathena-capital.localhost:8000`:

- **Assistants → Shirley (stage):** the mic icon opens the `● Record`
  panel and carries the pressed border;
- **`Back to dock`, then back to the stage:** the toggle still responds —
  the dataset guard means the second wire is a no-op, not a double bind;
- **Front Office → Ctrl J (docked open):** the toggle responds there too —
  the path that already worked must keep working.

---

## Verify-first

Line numbers are the **pre-edit** working tree.

| # | Check | Found |
|---|---|---|
| 1 | `git status --porcelain` empty | clean |
| 1 | `grep -c "P-UX-A0e2"` over `log -40` | 1 (`38f94b2`) |
| 2 | `chat.js` — `document.addEventListener("htmx:afterSwap", function (event) {` | `chat.js:54` |
| 2 | comment starting `// The assistants section (with its composer + voice controls) is` | `chat.js:63–66` |
| 2 | `if (event.target) initVoiceControls(event.target);` directly after it | `chat.js:67` |
| 3 | `shirley.js` — `document.body.addEventListener("htmx:afterSwap", function (event) {` | `shirley.js:171` |
| 3 | `if (event.target && event.target.id === "dock-chat-host") {` | `shirley.js:172` |
| 3 | `relocate(state());` | `shirley.js:173` |

## §1 — `web/static/js/chat.js`

The four comment lines and the one-line call become a six-line comment and
a conditional scope, at `63–73` post-edit:

```diff
@@ -60,11 +60,17 @@
             scrollHistoryToBottom();
             attachSseListeners(event.target);
         }
-        // The assistants section (with its composer + voice controls) is
-        // swapped in on area navigation; wire the controls each time. The
-        // dataset guard in initVoiceControls makes this idempotent, and it
-        // is a no-op when the swapped subtree has no voice toggle.
-        if (event.target) initVoiceControls(event.target);
+        // The /chat/dock swap lands in the dock host, and shirley.js — a
+        // body listener, which runs before this document one — may already
+        // have moved the conversation into the stage host. On that swap
+        // wire from the document; the dataset guard in initVoiceControls
+        // keeps this idempotent, and it is a no-op for swaps without a
+        // toggle (P-UX-A0e3).
+        if (event.target) {
+            initVoiceControls(
+                event.target.id === "dock-chat-host" ? document : event.target
+            );
+        }
     });
```

Every other swap keeps the narrow scope it had. `initVoiceControls` itself
is untouched — its `scope = root && root.querySelector ? root : document`
line already accepts a document, and the `dataset.pfVoiceWired` guard makes
the widened scope safe to re-run. `shirley.js`, the two hosts and the
`shirley_dock.html` fragment are byte-identical.

## Gate

```
$ pytest tests/web/test_shirley_dock.py tests/web/test_chat_voice.py \
         tests/regression/test_shirley_shell_element.py -q
26 passed in 35.40s
```

Green. No JS test exists for this path (the repo has no JS test runner —
`package.json` carries `repomix` alone, and no prettier/eslint gate applies
to `web/static/js/`), so the browser round above is the real check. The
full suite was not run; the A-1 baseline
(`docs/reports/full-suite-2026-09-25-report.md`) stands and this hotfix
rides on the three modules above.

---

## Deliberate deviations

None. Both verify steps matched on the first read, §1 was applied exactly as
written, and nothing outside `chat.js` and this report changed.

---

## Flags

- **The listener-ordering dependency is now load-bearing in two places.**
  `shirley.js` on `document.body` runs before `chat.js` on `document`, and
  two behaviours rely on it: relocate-before-focus (A0e, by design) and now
  relocate-before-wire (this fix, which *compensates* for it). Neither file
  says so in its head comment. Worth a sentence in `shirley.js`'s head
  comment in a later docs prompt — the ordering is a DOM-event-capture
  property, not a convention anyone reading either file would notice.
- **A-7 may remove the dependency altogether.** If the voice wiring moves
  onto delegated document-level handlers, the way attach did in A0e2 §5,
  then nothing needs to be wired at swap time and the ordering stops
  mattering. That is the structural fix; this is the hotfix.
- The `dock-chat-host` id is now named in `chat.js` as well as in
  `shirley.js` and `base.html`. Three literals, no shared constant. Small,
  but it is the kind of string that drifts.
