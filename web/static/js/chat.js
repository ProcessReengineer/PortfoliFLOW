// Bespoke chat-surface logic — kept small, per ADR-0037 §6.
//
// The chat surface uses a native ``EventSource`` rather than the HTMX
// SSE extension. The extension trims a leading space from every
// ``data:`` payload (HTMX bug #2343), which collapsed Shirley's
// streamed prose into "AlleskarIchbinShirley". A native ``EventSource``
// preserves the spec-compliant single-space trim and nothing more,
// and gives us unilateral control over connection teardown so the
// post-completion 404 reconnect storm cannot happen. HTMX continues
// to drive transport elsewhere; only this surface is bespoke.
//
// Responsibilities:
//
//   1. Auto-scroll the chat history to the bottom whenever new
//      content is swapped in.
//   2. Submit on Enter; insert a newline on Shift+Enter.
//   3. Open a one-shot ``EventSource`` for each turn-started fragment
//      that HTMX swaps into ``#chat-history``, dispatching the full
//      server event vocabulary (message, tool_called, tool_completed,
//      chart, case_write, done, error) into the assistant bubble. The ``chart``
//      event carries either an interactive Plotly figure spec
//      (``chart_format: "plotly"``, the web path — ADR-0048) or a
//      data: URI (``chart_format: "png"``, from ``generate_chart``);
//      Plotly figures go through the shared render helper in
//      ``chart_snapshot.js``, the same path the rehydrated history and
//      the Cases timeline use (ADR-0114). A plotly payload also carries
//      an ``artifact_id`` when the server archived the spec — the handle
//      the figure's Keep gestures post (ADR-0138 §8). A ``case_write``
//      event is a row Shirley added to a case through ``case_add``: it
//      becomes a line with Undo in the bubble's tools container (ADR-0138
//      §10, D-CS-B-115).

(function () {
    "use strict";

    // Monotonic counter giving every Plotly chart div a unique,
    // deterministic id across the lifetime of the page.
    let chartCounter = 0;

    function scrollHistoryToBottom() {
        const history = document.getElementById("chat-history");
        if (history) {
            history.scrollTop = history.scrollHeight;
        }
    }

    // A rehydrated or live figure finishes drawing after its swap, so the
    // pane is scrolled again once it exists (chart_snapshot.js fires this).
    document.addEventListener("pf:chart-rendered", function (event) {
        if (event.target && event.target.closest && event.target.closest("#chat-history")) {
            scrollHistoryToBottom();
        }
    });

    document.addEventListener("htmx:afterSwap", function (event) {
        if (
            event.target &&
            (event.target.id === "chat-history" ||
                (event.target.closest && event.target.closest("#chat-history")))
        ) {
            scrollHistoryToBottom();
            attachSseListeners(event.target);
        }
        // The /chat/dock swap lands in the dock host, and shirley.js — a
        // body listener, which runs before this document one — may already
        // have moved the conversation into the stage host. On that swap
        // wire from the document; the dataset guard in initVoiceControls
        // keeps this idempotent, and it is a no-op for swaps without a
        // toggle (P-UX-A0e3).
        if (event.target) {
            initVoiceControls(
                event.target.id === "dock-chat-host" ? document : event.target
            );
        }
    });

    // Enter / Shift+Enter on the composer textarea.
    document.addEventListener("keydown", function (event) {
        const target = event.target;
        if (!target || target.id !== "chat-input") return;
        if (event.key !== "Enter") return;
        if (event.shiftKey) return;
        const form = document.getElementById("chat-form");
        if (!form) return;
        event.preventDefault();
        if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
        } else {
            form.submit();
        }
    });

    // ---- Attach (P-UX-A0e2) --------------------------------------
    // The native file input is hidden; the paperclip button opens it and
    // the chosen names render beneath the composer row. All delegated:
    // the composer is swapped in later and moves between the two hosts.
    function renderAttachNames() {
        const input = document.getElementById("chat-attach");
        const out = document.querySelector("[data-pf-attach-names]");
        if (!input || !out) return;
        const names = Array.prototype.map
            .call(input.files || [], function (f) {
                return f.name;
            })
            .join(", ");
        out.textContent = names;
        out.hidden = names.length === 0;
    }

    document.addEventListener("click", function (event) {
        const trigger =
            event.target && event.target.closest
                ? event.target.closest("[data-pf-attach-trigger]")
                : null;
        if (!trigger) return;
        event.preventDefault();
        const input = document.getElementById("chat-attach");
        if (input) input.click();
    });

    document.addEventListener("change", function (event) {
        if (event.target && event.target.id === "chat-attach") renderAttachNames();
    });

    // ``reset`` does not bubble — capture it. The form resets itself after
    // every request (hx-on::after-request); the files are cleared by the
    // time the next task runs, hence the deferral.
    document.addEventListener(
        "reset",
        function (event) {
            if (event.target && event.target.id === "chat-form") {
                setTimeout(renderAttachNames, 0);
            }
        },
        true
    );

    // ---- EventSource bootstrap ------------------------------------

    function escapeHtml(text) {
        return String(text).replace(/[&<>]/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
        });
    }

    // ADR-0138 §8: Keep's two gestures ("Keep →" · "Keep to…") under a
    // completed answer (kind "message", the message's server-side id) or a
    // figure (kind "chart", the server's sidecar handle — the client never
    // posts the spec, so an unarchived figure without an ``artifact_id`` gets
    // none). A placeholder that fetches the gestures from the server on
    // ``load``, so the markup has one source — the template the rehydrated
    // history includes. Inserted by raw JS, not an HTMX swap, so the subtree
    // is processed by hand. One per artefact: the guard keys on the handle,
    // which every Keep partial carries as ``data-pf-keep-ref``.
    function addKeepActions(wrap, kind, id) {
        if (!wrap || !id) return;
        if (wrap.querySelector('[data-pf-keep-ref="' + CSS.escape(id) + '"]')) return;
        const param = kind === "chart" ? "artifact_id" : "message_id";
        const holder = document.createElement("div");
        holder.className = "chat-keep";
        holder.dataset.pfKeep = kind;
        holder.dataset.pfKeepRef = id;
        holder.setAttribute(
            "hx-get",
            "/api/chat/keep/actions?" + param + "=" + encodeURIComponent(id)
        );
        holder.setAttribute("hx-trigger", "load");
        holder.setAttribute("hx-swap", "outerHTML");
        wrap.appendChild(holder);
        if (window.htmx) window.htmx.process(holder);
    }

    // ADR-0138 §10: one line per write Shirley made through ``case_add`` —
    // "Added to CASE-nnnn · {kind}: {summary} · Undo". The addKeepActions
    // idiom: a placeholder that fetches the line from the server on ``load``,
    // processed by hand because it is inserted by raw JS. It goes into the
    // bubble's tools container, after the running badge of the tool that
    // wrote it; without one, under the answer. Two writes in one turn are two
    // lines. Live only — the rehydrated history renders none.
    function addWriteLine(container, payload) {
        if (!container || !payload || !payload.case_id || !payload.undo) return;
        const params = new URLSearchParams({
            badge: payload.badge || "",
            case_id: payload.case_id,
            kind: payload.kind || "",
            summary: payload.summary || "",
            row_id: payload.undo.row_id || "",
            appended: payload.undo.appended || "",
        });
        const holder = document.createElement("div");
        holder.className = "chat-write";
        holder.dataset.pfWrite = payload.kind || "";
        holder.setAttribute("hx-get", "/api/chat/case-write/line?" + params.toString());
        holder.setAttribute("hx-trigger", "load");
        holder.setAttribute("hx-swap", "outerHTML");
        container.appendChild(holder);
        if (window.htmx) window.htmx.process(holder);
    }

    function attachSseListeners(container) {
        const elements = container.querySelectorAll(
            "[data-pf-sse-url]:not([data-pf-attached])"
        );
        elements.forEach(openOneShotEventSource);
    }

    function openOneShotEventSource(el) {
        el.dataset.pfAttached = "1";

        const url = el.dataset.pfSseUrl;
        const bubbleId = el.dataset.pfSseTarget;
        const toolsId = el.dataset.pfSseToolsTarget;
        // Voice turns carry data-pf-voice="1"; only those speak the reply.
        const isVoice = el.dataset.pfVoice === "1";

        const bubble = document.getElementById(bubbleId);
        const toolsContainer = toolsId
            ? document.getElementById(toolsId)
            : null;
        if (!bubble || !url) {
            el.remove();
            return;
        }

        // Track the most recently appended running badge per tool name
        // so ``tool_completed`` can find the right badge to flip.
        const runningBadges = new Map();
        let serverErrorRendered = false;

        const eventSource = new EventSource(url);

        // Server emits chunks already HTML-escaped by ``_html_escape``;
        // we use ``insertAdjacentHTML`` so entities (``&amp;`` etc.) render
        // correctly in the bubble. Whitespace is preserved verbatim by
        // the native ``EventSource`` — no leading-space trim like the
        // HTMX SSE extension does.
        eventSource.addEventListener("message", function (event) {
            bubble.insertAdjacentHTML("beforeend", event.data);
            scrollHistoryToBottom();
        });

        eventSource.addEventListener("tool_called", function (event) {
            const name = event.data || "";
            if (!toolsContainer) return;
            const badge = document.createElement("span");
            badge.className = "chat-tool-badge chat-tool-badge--running";
            badge.textContent = name;
            toolsContainer.appendChild(badge);
            runningBadges.set(name, badge);
        });

        eventSource.addEventListener("tool_completed", function (event) {
            const name = event.data || "";
            const running = runningBadges.get(name);
            if (running) {
                running.classList.remove("chat-tool-badge--running");
                running.classList.add("chat-tool-badge--done");
                runningBadges.delete(name);
                return;
            }
            if (!toolsContainer) return;
            const badge = document.createElement("span");
            badge.className = "chat-tool-badge chat-tool-badge--done";
            badge.textContent = name;
            toolsContainer.appendChild(badge);
            console.warn("chat.js: tool_completed for unknown tool", name);
        });

        eventSource.addEventListener("chart", function (event) {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (err) {
                console.error("chat.js: malformed chart payload", err);
                return;
            }
            const wrap = document.getElementById(bubbleId + "-wrap");
            if (!wrap) return;

            const figure = document.createElement("div");
            figure.className = "chat-chart";

            // Two artefact formats (ADR-0048): "plotly" carries an
            // interactive figure spec; "png" carries a data: URI from
            // ``generate_chart``, which is registered for every consumer,
            // so the png branch runs whenever the model picks it.
            let plotDiv = null;
            if (payload.chart_format === "plotly") {
                plotDiv = document.createElement("div");
                plotDiv.className = "chat-chart__plot";
                plotDiv.id = bubbleId + "-chart-" + chartCounter;
                chartCounter += 1;
                figure.appendChild(plotDiv);
            } else {
                const img = document.createElement("img");
                img.src = payload.src || "";
                img.alt = payload.caption || "";
                figure.appendChild(img);
            }

            if (payload.caption) {
                const caption = document.createElement("div");
                caption.className = "chat-chart__caption";
                caption.textContent = payload.caption;
                figure.appendChild(caption);
            }
            wrap.appendChild(figure);
            // The server archived this spec and handed back its handle
            // (ADR-0114): offer Keep right under the live figure.
            addKeepActions(wrap, "chart", payload.artifact_id);

            if (plotDiv) {
                // One render path for live and restored figures — the helper
                // owns the Plotly call and the "did not load" degradation.
                // Plotly renders asynchronously; scroll once it has. If the
                // helper itself is absent, degrade the same way rather than
                // leaving a blank frame.
                if (!window.pfChartSnapshot) {
                    plotDiv.innerHTML =
                        '<span class="chat-error">Chart unavailable: ' +
                        "the chart renderer did not load.</span>";
                    scrollHistoryToBottom();
                    return;
                }
                window.pfChartSnapshot
                    .render(plotDiv, payload.spec || {})
                    .then(scrollHistoryToBottom);
            } else {
                scrollHistoryToBottom();
            }
        });

        eventSource.addEventListener("case_write", function (event) {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (err) {
                console.error("chat.js: malformed case_write payload", err);
                return;
            }
            addWriteLine(
                toolsContainer || document.getElementById(bubbleId + "-wrap"),
                payload
            );
            scrollHistoryToBottom();
        });

        eventSource.addEventListener("error", function (event) {
            // Server-emitted, named ``error`` event — distinct from
            // the EventSource transport-level ``onerror`` callback.
            const text = (event.data || "An unknown error occurred.").trim();
            bubble.insertAdjacentHTML(
                "beforeend",
                '<span class="chat-error">' + escapeHtml(text) + "</span>"
            );
            serverErrorRendered = true;
            eventSource.close();
            el.remove();
            scrollHistoryToBottom();
        });

        eventSource.addEventListener("done", function (event) {
            eventSource.close();
            if (isVoice) {
                // ``textContent`` is the decoded prose (the wire is
                // HTML-escaped). Fire-and-forget — playback failure never
                // affects the already-rendered text answer.
                const prose = bubble.textContent.trim();
                if (prose) speakProse(prose);
            }
            // ``done`` carries the completed assistant message's server-side id
            // (empty when no assistant message was produced, e.g. a bare
            // error). Offer Keep for exactly that message.
            const messageId = (event && event.data ? event.data : "").trim();
            if (messageId) {
                addKeepActions(
                    document.getElementById(bubbleId + "-wrap"),
                    "message",
                    messageId
                );
            }
            el.remove();
            scrollHistoryToBottom();
        });

        // Transport-level error: network drop / server crash before
        // ``done`` arrived. Render once and close — ``EventSource``'s
        // default reconnect behaviour is exactly what produced the
        // post-completion 404 storm; we explicitly do not retry.
        eventSource.onerror = function () {
            if (eventSource.readyState === EventSource.CLOSED) return;
            if (!serverErrorRendered) {
                bubble.insertAdjacentHTML(
                    "beforeend",
                    '<span class="chat-error">Connection lost — please try again.</span>'
                );
            }
            eventSource.close();
            el.remove();
            scrollHistoryToBottom();
        };
    }

    // ---- Voice (ADR-0076) -----------------------------------------

    function csrfToken() {
        const el = document.querySelector('#chat-form input[name="csrf_token"]');
        return el ? el.value : "";
    }

    // Append an inline error in the same shape as chat_error.html, so a
    // voice-capture failure (mic denied, unsupported recorder, network)
    // surfaces in the history without disturbing the text composer.
    function renderInlineError(message) {
        const history = document.getElementById("chat-history");
        if (!history) return;
        history.insertAdjacentHTML(
            "beforeend",
            '<article class="chat-message chat-message--assistant">' +
                '<div class="chat-message__bubble"><span class="chat-error">' +
                escapeHtml(message) +
                "</span></div></article>"
        );
        scrollHistoryToBottom();
    }

    // POST the assistant prose to /chat/tts and play the returned MP3.
    // Fire-and-forget: any failure is logged and ignored — the text
    // answer is already on screen.
    function speakProse(text) {
        const body = new FormData();
        body.append("text", text);
        body.append("csrf_token", csrfToken());
        fetch("/chat/tts", {
            method: "POST",
            headers: { "X-CSRF-Token": csrfToken() },
            body: body,
        })
            .then(function (resp) {
                if (resp.status === 204) return null; // chart-only turn
                if (!resp.ok) throw new Error("tts " + resp.status);
                return resp.blob();
            })
            .then(function (blob) {
                if (!blob) return;
                const audio = new Audio(URL.createObjectURL(blob));
                audio.play().catch(function () {
                    /* autoplay blocked: silent */
                });
            })
            .catch(function (err) {
                // Text answer already shown — a TTS failure is non-fatal.
                console.warn("chat.js: TTS unavailable", err);
            });
    }

    // Wire the record / stop / cancel controls within ``root``. Guards
    // against double-wiring with a dataset flag on the toggle, so the
    // repeated htmx:afterSwap of the assistants section is idempotent.
    function initVoiceControls(root) {
        const scope = root && root.querySelector ? root : document;
        const toggle = scope.querySelector("[data-pf-voice-toggle]");
        if (!toggle || toggle.dataset.pfVoiceWired === "1") return;
        toggle.dataset.pfVoiceWired = "1";

        const controls = scope.querySelector("[data-pf-voice-controls]");
        const recordBtn = scope.querySelector("[data-pf-voice-record]");
        const stopBtn = scope.querySelector("[data-pf-voice-stop]");
        const cancelBtn = scope.querySelector("[data-pf-voice-cancel]");
        const statusEl = scope.querySelector("[data-pf-voice-status]");
        if (!controls || !recordBtn || !stopBtn || !cancelBtn) return;

        let recorder = null;
        let chunks = [];
        let stream = null;

        function setStatus(text) {
            if (statusEl) statusEl.textContent = text || "";
        }

        function stopTracks() {
            if (stream) {
                stream.getTracks().forEach(function (t) {
                    t.stop();
                });
                stream = null;
            }
        }

        // Reset the panel to the idle state: Record shown, Stop/Cancel
        // hidden, status cleared.
        function resetIdle() {
            recordBtn.hidden = false;
            stopBtn.hidden = true;
            cancelBtn.hidden = true;
            setStatus("");
            recorder = null;
            chunks = [];
        }

        function pickMimeType() {
            if (
                window.MediaRecorder &&
                MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
            ) {
                return "audio/webm;codecs=opus";
            }
            if (window.MediaRecorder && MediaRecorder.isTypeSupported("audio/mp4")) {
                return "audio/mp4";
            }
            return null;
        }

        function filenameFor(mime) {
            return mime.indexOf("audio/mp4") === 0 ? "voice.mp4" : "voice.webm";
        }

        toggle.addEventListener("click", function () {
            const showing = !controls.hidden;
            controls.hidden = showing;
            toggle.setAttribute("aria-pressed", showing ? "false" : "true");
            if (showing) {
                // Hiding the panel mid-recording: tear down cleanly.
                if (recorder && recorder.state === "recording") {
                    recorder.onstop = null;
                    try {
                        recorder.stop();
                    } catch (e) {
                        /* already stopped */
                    }
                }
                stopTracks();
                resetIdle();
            } else {
                resetIdle();
            }
        });

        recordBtn.addEventListener("click", function () {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                renderInlineError(
                    "Voice recording isn't supported in this browser — please type your message."
                );
                return;
            }
            const mimeType = pickMimeType();
            if (!mimeType) {
                renderInlineError(
                    "Voice recording isn't supported in this browser — please type your message."
                );
                return;
            }
            navigator.mediaDevices
                .getUserMedia({ audio: true })
                .then(function (s) {
                    stream = s;
                    chunks = [];
                    recorder = new MediaRecorder(stream, { mimeType: mimeType });
                    recorder.addEventListener("dataavailable", function (e) {
                        if (e.data && e.data.size > 0) chunks.push(e.data);
                    });
                    recorder.start();
                    recordBtn.hidden = true;
                    stopBtn.hidden = false;
                    cancelBtn.hidden = false;
                    setStatus("Recording…");
                })
                .catch(function () {
                    renderInlineError(
                        "Microphone access was denied — please type your message or enable the mic."
                    );
                });
        });

        stopBtn.addEventListener("click", function () {
            if (!recorder) {
                resetIdle();
                return;
            }
            const mimeType = recorder.mimeType || "audio/webm";
            setStatus("Sending…");
            recorder.onstop = function () {
                stopTracks();
                const blob = new Blob(chunks, { type: mimeType });
                const fd = new FormData();
                fd.append("audio", blob, filenameFor(mimeType));
                fd.append("csrf_token", csrfToken());
                fetch("/chat/voice", {
                    method: "POST",
                    headers: { "X-CSRF-Token": csrfToken() },
                    body: fd,
                })
                    .then(function (r) {
                        return r.text();
                    })
                    .then(function (html) {
                        const history = document.getElementById("chat-history");
                        // turn_started OR chat_error — both append cleanly.
                        history.insertAdjacentHTML("beforeend", html);
                        attachSseListeners(history); // opens EventSource if present
                        scrollHistoryToBottom();
                    })
                    .catch(function () {
                        renderInlineError(
                            "Couldn't send the recording — please try again."
                        );
                    });
                resetIdle();
            };
            try {
                recorder.stop();
            } catch (e) {
                stopTracks();
                resetIdle();
            }
        });

        cancelBtn.addEventListener("click", function () {
            if (recorder && recorder.state === "recording") {
                recorder.onstop = null;
                try {
                    recorder.stop();
                } catch (e) {
                    /* already stopped */
                }
            }
            stopTracks();
            resetIdle();
        });
    }

    // Attach on initial DOMContentLoaded too, so any pre-rendered
    // bootstrap elements (none today, but cheap insurance) get wired.
    document.addEventListener("DOMContentLoaded", function () {
        attachSseListeners(document);
        initVoiceControls(document);
    });

    // call_panel.js reuses these three for its live transcript bubbles and
    // its inline errors — lifted from here, not copied there.
    window.pfChat = { escapeHtml, renderInlineError, scrollHistoryToBottom };
})();
