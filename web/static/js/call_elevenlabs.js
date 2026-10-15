// The voice call's vendor adapter for ElevenLabs (ADR-0137 §2) — an ES module.
//
// Responsibilities:
//
//   1. Register ``window.pfCallAdapters.elevenlabs``, the factory the call
//      panel (call_panel.js) looks up by the ``vendor`` the session endpoint
//      names. The panel knows only the interface below; this file is the
//      one place the vendor SDK is named.
//   2. Open the vendor session over WebRTC with the one-session token the
//      server minted, and translate the SDK's callbacks into the panel's
//      handlers: status, mode, final transcript lines, corrections,
//      interruptions, the ``ask_shirley`` client tool, errors.
//   3. Close the session, mute the microphone, report the input volume.
//
// The adapter holds no PortfoliFLOW knowledge: no URLs but the SDK's, no
// CSRF, no history. The token is passed straight to the SDK and never
// logged.
//
// Interface: ``createElevenLabsAdapter(handlers)`` returns
// ``{ start(credentials), stop(), setMuted(flag), inputVolume() }`` with
// ``handlers = { onStatus(status, details?), onMode(mode),
// onTranscript({role, text, eventId, responseId}),
// onCorrection({eventId, responseId, text}), onInterruption(),
// onToolCall(name, params) -> Promise<string>, onError(message) }``.

// The one place the vendor SDK is named. Loaded lazily on the first start so
// the 600 KB bundle is never paid on a page that makes no call.
const SDK_URL = "/static/vendor/elevenlabs-client-1.28.0/elevenlabs-client.esm.js";
const WORKLETS = {
    rawAudioProcessor: "/static/vendor/elevenlabs-client-1.28.0/rawAudioProcessor.js",
    audioConcatProcessor: "/static/vendor/elevenlabs-client-1.28.0/audioConcatProcessor.js",
};

function createElevenLabsAdapter(handlers) {
    let conversation = null;

    async function start(credentials) {
        const { Conversation } = await import(SDK_URL);
        conversation = await Conversation.startSession({
            conversationToken: credentials.token,
            connectionType: "webrtc",
            workletPaths: WORKLETS,
            clientTools: { ask_shirley: (params) => handlers.onToolCall("ask_shirley", params) },
            onConnect: () => handlers.onStatus("connected"),
            onDisconnect: (details) => {
                handlers.onStatus("disconnected", details);
                // A dropped connection is an error as well as an end.
                if (details && details.reason === "error") {
                    handlers.onError(String(details.message || ""));
                }
            },
            onStatusChange: ({ status }) => handlers.onStatus(status),
            onModeChange: ({ mode }) => handlers.onMode(mode),
            onMessage: ({ message, role, event_id, response_id }) =>
                handlers.onTranscript({
                    role: role === "user" ? "user" : "assistant",
                    text: message,
                    eventId: event_id,
                    responseId: response_id,
                }),
            onAgentResponseCorrection: (e) =>
                handlers.onCorrection({
                    eventId: e.event_id,
                    responseId: e.response_id,
                    text: e.corrected_agent_response,
                }),
            onInterruption: () => handlers.onInterruption(),
            onError: (message) => handlers.onError(String(message || "")),
        });
    }

    async function stop() {
        const current = conversation;
        conversation = null;
        if (!current) return;
        try {
            await current.endSession();
        } catch (err) {
            // The session is gone either way; ending it twice is harmless.
        }
    }

    function setMuted(flag) {
        if (conversation) conversation.setMicMuted(Boolean(flag));
    }

    function inputVolume() {
        return conversation ? conversation.getInputVolume() : 0;
    }

    return { start, stop, setMuted, inputVolume };
}

window.pfCallAdapters = window.pfCallAdapters || {};
window.pfCallAdapters.elevenlabs = createElevenLabsAdapter;
