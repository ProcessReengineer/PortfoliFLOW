# Voice on your own server — local engines behind `openai_compatible` (ADR-0136 §11)

PortfoliFLOW's voice messages run against any server that speaks the OpenAI
audio wire — `POST /v1/audio/transcriptions` for speech-to-text and
`POST /v1/audio/speech` for text-to-speech. Choose the provider
`openai_compatible` on the **Voice — speech-to-text** and/or **Voice —
text-to-speech** card (or `VOICE_STT_PROVIDER` / `VOICE_TTS_PROVIDER` in
`.env`), set the **Model** and the **Base URL** the engine expects (and the
**Voice** for text-to-speech), and leave the key empty: local engines usually
run unauthenticated, and the adapter sends none. Audio then never leaves your
network. Each half is independent — a tenant may run speech-to-text locally and
leave text-to-speech off, or pair a local engine with a cloud provider.

PortfoliFLOW ships **none** of these engines and takes no dependency on any of
them. This page records what was checked on 2026-10-12 (ADR-0136 §11); verify
the engine's own documentation before deploying it.

## Engines

| Engine | Half | Wire | Notes |
|---|---|---|---|
| [Speaches](https://github.com/speaches-ai/speaches) | STT, TTS | `/v1/audio/transcriptions`, `/v1/audio/speech` | MIT. faster-whisper models for transcription; Piper and Kokoro for speech. Lists its models under `/v1/models`. |
| [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) | TTS | `/v1/audio/speech` with `mp3`, `opus`, `wav`, `flac`, `aac`, `pcm` | Apache-2.0 server around Kokoro-82M. **No German voices** (see the licence table). |
| vLLM-Omni | TTS | `POST /v1/audio/speech`, `response_format` `wav`, `mp3`, `flac`, `pcm`, `opus` | Serves Qwen3-TTS, Voxtral TTS and others; the weights' licences apply, not vLLM's. |
| vLLM | STT | `/v1/audio/transcriptions` | Serves transcription models such as Voxtral Transcribe. |

## Licences — "open weights" is not "free for commercial on-premise use"

| Engine / weights | Licence | German |
|---|---|---|
| Voxtral TTS (`voxtral-mini-tts-2603`) | **CC BY-NC 4.0** (Mistral models table) — not for commercial on-premise use without a licence | yes |
| XTTS-v2 | **Coqui Public Model License** (model card) — non-commercial | yes |
| F5-TTS checkpoints | **CC-BY-NC-4.0** (model card) | not stated |
| Kokoro-82M | Apache-2.0 | **no** — en-US/GB, ja, zh, es, fr, hi, it, pt-BR only |
| Voxtral Small / Mini Transcribe Realtime (speech-to-text) | Apache 2.0 | yes |

A financial entity deploying voice on-premise should read the licence of the
weights it serves, not only of the server in front of them. Speech-to-text with
German is well served by Apache-licensed weights today; German text-to-speech
under a commercial-friendly licence is still thin, which is one reason a tenant
may keep speech-to-text local and text-to-speech on a cloud provider, or leave
text-to-speech off.

## Example configurations

Speech-to-text on a Speaches server, keyless (tenant card or `.env`):

```
VOICE_ENABLED=true
VOICE_STT_PROVIDER=openai_compatible
VOICE_STT_MODEL=<a model id the server lists under /v1/models>
VOICE_STT_BASE_URL=http://speaches.lan:8000/v1
```

Text-to-speech on Kokoro-FastAPI, keyless:

```
VOICE_TTS_PROVIDER=openai_compatible
VOICE_TTS_MODEL=kokoro
VOICE_TTS_VOICE=af_bella
VOICE_TTS_BASE_URL=http://kokoro.lan:8880/v1
```

The **Base URL** is the wire's root — the part before `/audio/…`, which for
these engines ends in `/v1`. A wrong model or endpoint is reported at first use
(the web answers with a clear error, the Telegram bot replies with it), never
silently; the Voice card's state line shows *active (openai_compatible)* as soon
as the provider is chosen, because it judges the choice, not the connection.

## Telegram voice notes and `ffmpeg`

Telegram renders a voice note only for an Ogg/Opus container. A local engine
asked for `opus` may deliver Opus in a different container; the bot checks the
first bytes and, when they are not Ogg, transcodes with the host's `ffmpeg` in
memory before sending. Without `ffmpeg` the reply arrives as an audio file
instead. `scripts/install.sh --doctor` reports whether `ffmpeg` is present; the
operator handbook §8 has the install line.

## Not covered here

Shirley's phone mode (fluid conversation) is a separate feature with its own
ADR; it is web-only and uses a cloud provider. Nothing on this page applies to
it.
