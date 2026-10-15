// The voice call panel — the browser's side of a call (ADR-0137 §3, §5, §10).
//
// Responsibilities:
//
//   1. Start: the dock's call button POSTs /chat/call/session and hands the
//      one-session token to the vendor adapter the response names
//      (``window.pfCallAdapters[vendor]``, call_elevenlabs.js). This file
//      knows only the adapter interface, never the vendor SDK.
//   2. The panel (``_partials/call_panel.html``): it replaces .chat-composer
//      for the call's duration and shows one state at a time — connecting,
//      listening, speaking, working (busy, overrun), muted, error, ended.
//   3. The live transcript: every final line is appended to #chat-history as
//      it arrives and relayed to /chat/call/events in ``seq`` order; the
//      server ignores a line it already has, so a failed relay is retried.
//   4. The ``ask_shirley`` client tool: a job on /chat/call/ask, polled on
//      /chat/call/jobs/{id}, cancelled on barge-in; its answer is rendered
//      by re-fetching the history, so bubble, chip, chart and pin read
//      exactly as after a reload.
//   5. End: /chat/call/end appends the end line and returns the history,
//      which replaces the pane; a closing tab sends the last relay and the
//      end with ``keepalive``.
//
// The helpers come from chat.js (``window.pfChat``); every string the panel
// shows is in COPY below.

(function () {
    "use strict";

    const COPY = {
        connecting: "Connecting…",
        connectingSub: "Audio goes to the voice vendor for this call.",
        listening: "Listening",
        speaking: "Shirley is speaking",
        working: "Looking that up…",
        workingBusySub: "Shirley is busy with another request — hold on.",
        overrun: "This is taking longer",
        overrunSub: "The answer will appear in the chat.",
        muted: "Microphone muted",
        mute: "Mute",
        unmute: "Unmute",
        startFailed: "The call could not be started.",
        interrupted: "The call was interrupted.",
        jobFailed: "Shirley could not answer.",
        // The error sub lines.
        serverUnreachable: "The server could not be reached.",
        noAdapter: "No adapter for this voice vendor.",
        vendorFailed: "The vendor session could not be opened.",
        vendorClosed: "The voice vendor closed the session.",
        connectionLost: "(connection lost)",
        // The sentence the agent speaks when an answer takes longer than the poll.
        fallbackDe: "Das dauert länger — ich schreibe die Antwort in den Chat.",
        fallbackEn: "This is taking longer — I'll put the answer in the chat.",
        ended: function (from, to) {
            return "Call ended · " + from + "–" + to + " · see the conversation above";
        },
    };

    const POLL_MS = 1000;
    const SLOW_POLL_MS = 3000;
    const BUSY_AFTER_MS = 8000;
    const OVERRUN_AFTER_MS = 75000;
    const SLOW_POLL_LIMIT_MS = 10 * 60 * 1000;
    const LEVEL_MS = 120;
    const ENDED_HOLD_MS = 4000;
    const ERROR_DETAIL_MAX = 120;
    // The server's bounds on one relayed batch and one line (_CallEventsBody).
    const BATCH_MAX = 200;
    const TEXT_MAX = 8000;
    const QUESTION_MAX = 4000;
    const LEVEL_SHAPE = [0.4, 0.8, 0.6, 1.0, 0.5];

    // What each displayed state shows: dot modifier ("" is the static dot),
    // title, sub line and its tone, and whether the level and timer show.
    const VIEWS = {
        connecting: { dot: "connect", title: COPY.connecting, sub: COPY.connectingSub },
        listening: { dot: "listen", title: COPY.listening, level: true, timer: true },
        speaking: { dot: "speak", title: COPY.speaking, timer: true },
        working: { dot: "work", title: COPY.working, timer: true },
        "working-busy": {
            dot: "work",
            title: COPY.working,
            sub: COPY.workingBusySub,
            tone: "warn",
            timer: true,
        },
        "working-overrun": {
            dot: "work",
            title: COPY.overrun,
            sub: COPY.overrunSub,
            timer: true,
        },
        muted: { dot: "", title: COPY.muted, timer: true },
    };

    // The one call of this page; ``null`` when idle.
    let call = null;
    // A session request is in flight; a second click is ignored.
    let starting = false;

    // ---- Small helpers ---------------------------------------------

    function chat() {
        return window.pfChat;
    }

    function inlineError(message) {
        if (chat()) chat().renderInlineError(message);
    }

    function pad(n) {
        return (n < 10 ? "0" : "") + n;
    }

    // The browser's clock as HH:MM, 24 h.
    function hhmm(date) {
        const d = date ? new Date(date) : new Date();
        return pad(d.getHours()) + ":" + pad(d.getMinutes());
    }

    function mss(ms) {
        const total = Math.max(0, Math.floor(ms / 1000));
        return Math.floor(total / 60) + ":" + pad(total % 60);
    }

    function csrfToken() {
        const el = document.querySelector('#chat-form input[name="csrf_token"]');
        return el ? el.value : "";
    }

    // JSON POST with the CSRF header. Resolves ``{status, json, text}``; an
    // unparsable body is ``{}``. Rejects only on a network failure.
    function post(url, body, keepalive) {
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            keepalive: Boolean(keepalive),
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
            body: JSON.stringify(body),
        }).then(function (resp) {
            return resp.text().then(function (text) {
                let json = {};
                try {
                    json = text ? JSON.parse(text) : {};
                } catch (err) {
                    json = {};
                }
                return { status: resp.status, json: json || {}, text: text };
            });
        });
    }

    function detailOf(res) {
        return res && res.json && typeof res.json.detail === "string" ? res.json.detail : "";
    }

    function fallbackSentence(language) {
        const code = String(language || "").trim().toLowerCase();
        return code === "de" || code.indexOf("de-") === 0 ? COPY.fallbackDe : COPY.fallbackEn;
    }

    function el(selector) {
        return document.querySelector(selector);
    }

    function historyEl() {
        return document.getElementById("chat-history");
    }

    function composerEl() {
        return el("#pf-chat .chat-composer");
    }

    function templateNode(selector) {
        const tpl = el(selector);
        return tpl && tpl.content ? tpl.content.cloneNode(true) : null;
    }

    // ---- The panel -------------------------------------------------

    function displayState(c) {
        if (c.phase !== "live") return c.phase;
        if (c.job) return c.job.busy ? "working-busy" : "working";
        if (c.overrun) return "working-overrun";
        if (c.muted) return "muted";
        return c.mode === "speaking" ? "speaking" : "listening";
    }

    function viewFor(c, name) {
        if (name === "error") {
            return { dot: "error", title: c.errorTitle, sub: c.errorSub, tone: "error" };
        }
        if (name === "ended") return { dot: "", title: c.endedTitle };
        return VIEWS[name];
    }

    function render(c) {
        if (call !== c) return;
        const panel = el("[data-pf-call-panel]");
        if (!panel) return;
        const name = displayState(c);
        const view = viewFor(c, name);
        panel.dataset.pfCallState = name;
        panel.classList.toggle("chat-call--error", name === "error");
        panel.classList.toggle("chat-call--ended", name === "ended");

        const dot = panel.querySelector("[data-pf-call-dot]");
        if (dot) {
            dot.className = "chat-call__dot" + (view.dot ? " chat-call__dot--" + view.dot : "");
        }
        const title = panel.querySelector("[data-pf-call-title]");
        if (title) title.textContent = view.title || "";
        const sub = panel.querySelector("[data-pf-call-sub]");
        if (sub) {
            sub.textContent = view.sub || "";
            sub.hidden = !view.sub;
            sub.className = "chat-call__sub" + (view.tone ? " chat-call__sub--" + view.tone : "");
        }
        const level = panel.querySelector("[data-pf-call-level]");
        if (level) level.hidden = !view.level;
        const timer = panel.querySelector("[data-pf-call-timer]");
        if (timer) {
            timer.hidden = !(view.timer && c.startedAt);
            if (c.startedAt) timer.textContent = mss(Date.now() - c.startedAt);
        }

        const actions = panel.querySelector(".chat-call__actions");
        if (actions) actions.hidden = name === "ended";
        const muteBtn = panel.querySelector("[data-pf-call-mute]");
        const endBtn = panel.querySelector("[data-pf-call-end]");
        if (muteBtn) {
            muteBtn.hidden = name === "error" || name === "ended";
            muteBtn.disabled = name === "connecting";
            renderMuteButton(muteBtn, c.muted);
        }
        if (endBtn) endBtn.disabled = name === "connecting";
    }

    function renderMuteButton(button, muted) {
        const pressed = muted ? "true" : "false";
        if (button.getAttribute("aria-pressed") === pressed && button.dataset.pfCallFace) return;
        const label = muted ? COPY.unmute : COPY.mute;
        button.setAttribute("aria-pressed", pressed);
        button.setAttribute("aria-label", label);
        button.setAttribute("title", label);
        const icon = templateNode(muted ? "[data-pf-call-mute-icon]" : "[data-pf-call-mic-icon]");
        if (icon) button.replaceChildren(icon);
        button.dataset.pfCallFace = pressed;
    }

    function showPanel() {
        const panel = el("[data-pf-call-panel]");
        const composer = composerEl();
        if (composer) composer.hidden = true;
        if (panel) panel.hidden = false;
    }

    // Back to the composer: the call is over on every side.
    function toIdle(c) {
        if (call !== c) return;
        stopTimers(c);
        call = null;
        const panel = el("[data-pf-call-panel]");
        if (panel) {
            panel.hidden = true;
            panel.dataset.pfCallState = "idle";
            panel.classList.remove("chat-call--error", "chat-call--ended");
            const muteBtn = panel.querySelector("[data-pf-call-mute]");
            if (muteBtn) renderMuteButton(muteBtn, false);
        }
        const composer = composerEl();
        if (composer) composer.hidden = false;
        const input = document.getElementById("chat-input");
        if (input) input.focus();
    }

    function showError(c, title, sub) {
        c.phase = "error";
        c.errorTitle = title;
        c.errorSub = sub;
        stopTimers(c);
        showPanel();
        render(c);
    }

    // ---- Timers ----------------------------------------------------

    function startTimers(c) {
        stopTimers(c);
        c.timer = setInterval(function () {
            if (call !== c || !c.startedAt) return;
            const elapsed = Date.now() - c.startedAt;
            const timer = el("[data-pf-call-timer]");
            if (timer) timer.textContent = mss(elapsed);
            // The vendor ends the session at the same bound.
            if (c.maxDuration && elapsed >= c.maxDuration * 1000) endCall(c);
        }, 1000);
        c.levelTimer = setInterval(function () {
            if (call !== c || displayState(c) !== "listening") return;
            const level = el("[data-pf-call-level]");
            if (!level || !c.adapter) return;
            let volume = 0;
            try {
                volume = Number(c.adapter.inputVolume()) || 0;
            } catch (err) {
                volume = 0;
            }
            const scale = 0.3 + 0.7 * Math.min(1, Math.max(0, volume));
            level.querySelectorAll("i").forEach(function (bar, index) {
                bar.style.height = Math.round(LEVEL_SHAPE[index % 5] * scale * 100) + "%";
            });
        }, LEVEL_MS);
    }

    function stopTimers(c) {
        if (c.timer) clearInterval(c.timer);
        if (c.levelTimer) clearInterval(c.levelTimer);
        c.timer = null;
        c.levelTimer = null;
    }

    // ---- The live transcript and its relay -------------------------

    function wire(entry) {
        return { seq: entry.seq, role: entry.role, text: entry.text, at: entry.at };
    }

    function appendBubble(c, entry) {
        const history = historyEl();
        if (!history || !chat()) return;
        const empty = history.querySelector(".chat-empty");
        if (empty) empty.remove();
        const article = document.createElement("article");
        article.className = "chat-message chat-message--" + entry.role;
        article.dataset.pfCallSeq = String(entry.seq);
        article.innerHTML =
            '<div class="chat-message__bubble">' +
            chat().escapeHtml(entry.text) +
            '</div><div class="chat-message__meta"></div>';
        const chip = templateNode("[data-pf-call-chip]");
        if (chip) article.querySelector(".chat-message__meta").appendChild(chip);
        history.appendChild(article);
        c.liveBubbles.set(entry.seq, article);
        chat().scrollHistoryToBottom();
    }

    // The lines the next relay may carry. None while a history refresh is in
    // flight: a line relayed then could miss the rendered fragment and, no
    // longer pending, would not be appended again after the swap.
    function relayable(c, force) {
        return c.refreshing && !force ? [] : c.pending.slice(0, BATCH_MAX);
    }

    // POST the unrelayed lines, at most a batch at a time. Resolves true when
    // nothing is left pending; a failure keeps the lines for the next flush.
    // ``keepalive`` is the closing tab's one last attempt.
    function flush(c, keepalive) {
        const batch = relayable(c, keepalive);
        if (!batch.length) return Promise.resolve(c.pending.length === 0);
        return post("/chat/call/events", { call_id: c.callId, events: batch.map(wire) }, keepalive)
            .then(function (res) {
                if (res.status < 200 || res.status >= 300) return false;
                markRelayed(c, batch);
                return c.pending.length && !keepalive ? flush(c) : true;
            })
            .catch(function () {
                return false;
            });
    }

    function markRelayed(c, batch) {
        c.pending = c.pending.filter(function (entry) {
            return batch.indexOf(entry) === -1;
        });
    }

    function onTranscript(c, line) {
        const text = String((line && line.text) || "").trim();
        if (!text) return; // the server takes no empty line
        c.seq += 1;
        const entry = {
            seq: c.seq,
            role: line.role === "user" ? "user" : "assistant",
            text: text.slice(0, TEXT_MAX),
            at: hhmm(),
            eventId: line.eventId,
            responseId: line.responseId,
        };
        c.entries.push(entry);
        c.pending.push(entry);
        appendBubble(c, entry);
    }

    // An interrupted agent line: the vendor's corrected text is what was
    // actually spoken (ADR-0137 §5). A line already relayed stays on the
    // server as relayed; the next flush does not resend it.
    function onCorrection(c, correction) {
        const text = String((correction && correction.text) || "").trim();
        if (!text) return;
        const assistant = c.entries.filter(function (entry) {
            return entry.role === "assistant";
        });
        let target = null;
        if (correction.responseId != null) {
            target = assistant.find(function (entry) {
                return entry.responseId === correction.responseId;
            });
        }
        if (!target) {
            for (let i = assistant.length - 1; i >= 0; i -= 1) {
                if (!(Number(assistant[i].eventId) > Number(correction.eventId))) {
                    target = assistant[i];
                    break;
                }
            }
        }
        if (!target) return;
        target.text = text.slice(0, TEXT_MAX);
        const bubble = c.liveBubbles.get(target.seq);
        const body = bubble ? bubble.querySelector(".chat-message__bubble") : null;
        if (body) body.textContent = target.text;
    }

    // Replace the pane with the server's history — bubble, chip, chart and
    // pin as a reload renders them — after relaying what is pending. Lines
    // that could not be relayed are appended again below it.
    function refreshHistory() {
        const c = call;
        const flushed = c && c.callId ? flush(c) : Promise.resolve(true);
        const done = flushed
            .then(function () {
                if (c && call === c) c.refreshing = done;
                if (!window.htmx || !historyEl()) return null;
                return window.htmx.ajax("GET", "/chat/history", {
                    target: "#chat-history",
                    swap: "innerHTML",
                });
            })
            .then(function () {
                if (!c || call !== c) return;
                c.liveBubbles.clear();
                c.pending.forEach(function (entry) {
                    appendBubble(c, entry);
                });
            })
            .catch(function (err) {
                console.warn("call_panel.js: history refresh failed", err);
            })
            .then(function () {
                if (c && c.refreshing === done) c.refreshing = null;
            });
        return done;
    }

    // The end's response is the history fragment; swap it in as HTMX would.
    function swapHistory(c, html) {
        const history = historyEl();
        if (!history) return;
        history.innerHTML = html;
        if (window.htmx) window.htmx.process(history);
        if (window.pfChartSnapshot) window.pfChartSnapshot.init(history);
        c.liveBubbles.clear();
        if (chat()) chat().scrollHistoryToBottom();
    }

    // ---- The ask_shirley job ---------------------------------------

    function settle(job, value) {
        if (job.settled) return;
        job.settled = true;
        if (job.resolve) job.resolve(value);
    }

    function stopPolling(job) {
        job.finished = true;
        if (job.pollTimer) clearTimeout(job.pollTimer);
        if (job.overrunTimer) clearTimeout(job.overrunTimer);
        if (job.limitTimer) clearTimeout(job.limitTimer);
    }

    // Without an id the ask is still in flight; its answer sends the cancel.
    function sendCancel(job) {
        if (!job.id) return;
        post("/chat/call/jobs/" + encodeURIComponent(job.id) + "/cancel", {}).catch(function () {
            /* the job runs out on the server; its answer is discarded */
        });
    }

    function cancelJob(c, job) {
        if (!job || job.finished) return;
        job.cancelled = true;
        stopPolling(job);
        settle(job, "");
        if (c.job === job) c.job = null;
        sendCancel(job);
        render(c);
    }

    // From here on the job is only followed for the display: a slow poll that
    // renders its answer when it lands, for at most ten minutes.
    function goSlow(c, job) {
        job.slow = true;
        if (c.job === job) c.job = null;
        if (job.overrunTimer) clearTimeout(job.overrunTimer);
        job.limitTimer = setTimeout(function () {
            release(c, job);
        }, SLOW_POLL_LIMIT_MS);
    }

    // At 75 s the agent gets the fallback sentence now; the job runs on on
    // the server and lands in the chat (ADR-0137 §3 step 5).
    function overrun(c, job) {
        if (job.finished || job.settled) return;
        settle(job, fallbackSentence(c.language));
        goSlow(c, job);
        c.overrun = job;
        render(c);
    }

    // The job ended (or will not be followed any more): clear its slot.
    function release(c, job) {
        stopPolling(job);
        if (c.job === job) c.job = null;
        if (c.overrun === job) c.overrun = null;
        render(c);
    }

    function poll(c, job) {
        if (job.finished) return;
        fetch("/chat/call/jobs/" + encodeURIComponent(job.id), { credentials: "same-origin" })
            .then(function (resp) {
                if (resp.status === 404) return { state: "lost" };
                return resp.ok ? resp.json() : {};
            })
            .catch(function () {
                return {}; // transient; the next tick asks again
            })
            .then(function (data) {
                if (job.finished) return;
                const state = data && data.state;
                if (state === "waiting_for_turn") {
                    const busy = Date.now() - job.startedAt >= BUSY_AFTER_MS;
                    if (busy !== job.busy) {
                        job.busy = busy;
                        render(c);
                    }
                } else if (state === "running") {
                    if (job.busy) {
                        job.busy = false;
                        render(c);
                    }
                } else if (state === "done") {
                    settle(job, String(data.speakable || "") || fallbackSentence(c.language));
                    release(c, job);
                    refreshHistory();
                    return;
                } else if (state === "failed") {
                    settle(job, fallbackSentence(c.language));
                    release(c, job);
                    inlineError(String(data.error || COPY.jobFailed));
                    return;
                } else if (state === "cancelled") {
                    settle(job, "");
                    release(c, job);
                    return;
                } else if (state === "lost") {
                    settle(job, fallbackSentence(c.language));
                    release(c, job);
                    return;
                }
                job.pollTimer = setTimeout(function () {
                    poll(c, job);
                }, job.slow ? SLOW_POLL_MS : POLL_MS);
            });
    }

    function askShirley(c, params) {
        const question = String((params && params.question) || "").trim();
        if (!question) {
            console.warn("call_panel.js: ask_shirley without a question");
            return Promise.resolve("");
        }
        // A second question while one runs supersedes it.
        if (c.job) cancelJob(c, c.job);
        const job = {
            id: null,
            startedAt: Date.now(),
            cancelled: false,
            settled: false,
            finished: false,
            busy: false,
            slow: false,
            resolve: null,
        };
        c.job = job;
        render(c);

        const answer = new Promise(function (resolve) {
            job.resolve = resolve;
        });
        // The unrelayed lines ride the ask body, so the history is in order
        // before the question is answered (ADR-0137 §3 step 1).
        const events = relayable(c);
        post("/chat/call/ask", {
            call_id: c.callId,
            question: question.slice(0, QUESTION_MAX),
            language: c.language,
            events: events.map(wire),
        })
            .then(function (res) {
                if (res.status !== 202 || !res.json.job_id) {
                    console.warn("call_panel.js: ask_shirley refused", res.status, detailOf(res));
                    settle(job, fallbackSentence(c.language));
                    release(c, job);
                    return;
                }
                markRelayed(c, events);
                job.id = String(res.json.job_id);
                if (job.cancelled) {
                    sendCancel(job); // barge-in while the ask was in flight
                    return;
                }
                job.overrunTimer = setTimeout(function () {
                    overrun(c, job);
                }, Math.max(0, OVERRUN_AFTER_MS - (Date.now() - job.startedAt)));
                poll(c, job);
            })
            .catch(function (err) {
                console.warn("call_panel.js: ask_shirley failed", err);
                settle(job, fallbackSentence(c.language));
                release(c, job);
            });
        return answer;
    }

    // ---- Vendor callbacks ------------------------------------------

    function handlersFor(c) {
        return {
            onStatus: function (status, details) {
                if (call !== c) return;
                if (status === "connected") onConnected(c);
                else if (status === "disconnected") {
                    // onDisconnect carries the reason, onStatusChange does not;
                    // gather both in this tick, then decide once.
                    if (details && details.reason) c.disconnectReason = details.reason;
                    if (!c.disconnectTimer) {
                        c.disconnectTimer = setTimeout(function () {
                            onDisconnected(c);
                        }, 0);
                    }
                }
            },
            onMode: function (mode) {
                if (call !== c) return;
                c.mode = mode === "speaking" ? "speaking" : "listening";
                if (c.mode === "listening") flush(c);
                render(c);
            },
            onTranscript: function (line) {
                if (call === c) onTranscript(c, line);
            },
            onCorrection: function (correction) {
                if (call === c) onCorrection(c, correction);
            },
            onInterruption: function () {
                if (call === c && c.job) cancelJob(c, c.job);
            },
            onToolCall: function (name, params) {
                if (call !== c || name !== "ask_shirley") return Promise.resolve("");
                return askShirley(c, params);
            },
            onError: function (message) {
                // Transient vendor errors are reported while the session
                // lives; the disconnect path decides the panel's state.
                console.warn("call_panel.js: voice vendor error", message);
            },
        };
    }

    function onConnected(c) {
        if (c.startedAt) return; // onConnect and onStatusChange both report it
        c.startedAt = Date.now();
        c.phase = "live";
        startTimers(c);
        render(c);
    }

    function onDisconnected(c) {
        c.disconnectTimer = null;
        if (call !== c || c.ending || c.disconnected) return;
        // Before the start resolved, its own failure path decides — or, if it
        // resolves after all, the start's success path replays this.
        if (c.phase === "connecting" && !c.adapterStarted) {
            c.disconnectPending = true;
            return;
        }
        c.disconnected = true;
        let sub = COPY.vendorClosed;
        if (c.disconnectReason === "error") sub += " " + COPY.connectionLost;
        showError(c, COPY.interrupted, sub);
        closeCall(c);
    }

    // ---- Start and end ---------------------------------------------

    function newCall(fields) {
        return Object.assign(
            {
                callId: null,
                language: "",
                maxDuration: 0,
                seq: 0,
                entries: [],
                pending: [],
                liveBubbles: new Map(),
                job: null,
                overrun: null,
                muted: false,
                mode: "listening",
                phase: "connecting",
                createdAt: Date.now(),
                startedAt: null,
                timer: null,
                levelTimer: null,
                adapter: null,
                adapterStarted: false,
                disconnectPending: false,
                disconnectReason: "",
                disconnectTimer: null,
                disconnected: false,
                refreshing: null,
                ending: false,
                ended: false,
                closing: null,
            },
            fields
        );
    }

    function startFailed(sub) {
        call = newCall({});
        showError(call, COPY.startFailed, sub);
    }

    function startCall(button) {
        if (call || starting) return;
        starting = true;
        if (button) button.disabled = true;
        post("/chat/call/session", { at: hhmm() })
            .then(function (res) {
                if (res.status === 404 || res.status === 503) {
                    // Not offered or incomplete: the composer stays.
                    inlineError(detailOf(res) || COPY.startFailed);
                    return;
                }
                if (res.status !== 200) {
                    startFailed(detailOf(res) || COPY.serverUnreachable);
                    return;
                }
                beginCall(res.json);
            })
            .catch(function () {
                startFailed(COPY.serverUnreachable);
            })
            .then(function () {
                starting = false;
                if (button) button.disabled = false;
            });
    }

    function beginCall(session) {
        const c = newCall({
            callId: String(session.call_id || ""),
            language: String(session.language || ""),
            maxDuration: Number(session.max_duration_seconds) || 0,
        });
        call = c;
        const factory = window.pfCallAdapters && window.pfCallAdapters[session.vendor];
        if (typeof factory !== "function") {
            showError(c, COPY.startFailed, COPY.noAdapter);
            closeCall(c);
            return;
        }
        c.adapter = factory(handlersFor(c));
        showPanel();
        render(c);
        // The session endpoint appended "Call started"; show it now.
        refreshHistory();
        Promise.resolve()
            .then(function () {
                return c.adapter.start({ token: session.token });
            })
            .then(function () {
                c.adapterStarted = true;
                // A session that closed while it was opening.
                if (c.disconnectPending && call === c) onDisconnected(c);
            })
            .catch(function (err) {
                if (call !== c || c.ending) return;
                const message = String((err && err.message) || "");
                const sub =
                    message && message.length < ERROR_DETAIL_MAX ? message : COPY.vendorFailed;
                showError(c, COPY.startFailed, sub);
                closeCall(c);
            });
    }

    // Close every side of a call once: the vendor session, the last relay,
    // the end line. Resolves when the history shows the end.
    function closeCall(c) {
        if (c.closing) return c.closing;
        // A question still being answered lands in the chat by itself.
        if (c.job) {
            settle(c.job, "");
            goSlow(c, c.job);
        }
        stopTimers(c);
        const adapter = c.adapter;
        // A refresh in flight lands first, so the end's fragment is the last.
        c.closing = Promise.resolve(c.refreshing)
            .then(function () {
                return adapter ? adapter.stop() : null;
            })
            .catch(function () {
                /* the session is gone either way */
            })
            .then(function () {
                return flush(c);
            })
            .then(function () {
                if (!c.callId) return null;
                return post("/chat/call/end", { call_id: c.callId, at: hhmm() });
            })
            .then(function (res) {
                if (res && res.status === 200) swapHistory(c, res.text);
            })
            .catch(function (err) {
                console.warn("call_panel.js: the end could not be sent", err);
            })
            .then(function () {
                c.ended = true;
            });
        return c.closing;
    }

    // End: the End button, or the call's duration elapsed.
    function endCall(c) {
        if (!c || call !== c) return;
        if (c.phase === "error") {
            // The error panel stays until End is clicked; the server side is
            // already closed, or closing.
            closeCall(c);
            toIdle(c);
            return;
        }
        if (c.ending) return;
        c.ending = true;
        closeCall(c).then(function () {
            if (call !== c) return;
            c.phase = "ended";
            c.endedTitle = COPY.ended(hhmm(c.startedAt || c.createdAt), hhmm());
            render(c);
            setTimeout(function () {
                toIdle(c);
            }, ENDED_HOLD_MS);
        });
    }

    function toggleMute(c) {
        if (!c || call !== c || c.phase !== "live" || !c.adapter) return;
        const next = !c.muted;
        try {
            c.adapter.setMuted(next);
        } catch (err) {
            console.warn("call_panel.js: mute failed", err);
            return;
        }
        c.muted = next;
        render(c);
    }

    // ---- Wiring ----------------------------------------------------

    // Delegated on the document: the dock arrives in a later swap and
    // shirley.js moves it between the dock and the stage.
    const CONTROLS = "[data-pf-call-start], [data-pf-call-mute], [data-pf-call-end]";
    document.addEventListener("click", function (event) {
        const target =
            event.target && event.target.closest ? event.target.closest(CONTROLS) : null;
        if (!target) return;
        event.preventDefault();
        if (target.hasAttribute("data-pf-call-start")) startCall(target);
        else if (target.hasAttribute("data-pf-call-mute")) toggleMute(call);
        else endCall(call);
    });

    // A closing tab: the last relay and the end, both with keepalive; the
    // vendor session dies with the tab (ADR-0137 §10 accepts a lost last line).
    window.addEventListener("pagehide", function () {
        const c = call;
        if (!c || !c.callId || c.ended || c.closing) return;
        flush(c, true);
        post("/chat/call/end", { call_id: c.callId, at: hhmm() }, true).catch(function () {
            /* nothing more can be done from a closing page */
        });
    });
})();
