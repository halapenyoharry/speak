# Google TTS State of the Art for Long-Form Narration — Post-I/O 2026 Report
*Compiled May 20, 2026, immediately following Google I/O 2026 (May 19–20)*

## TL;DR
- **I/O 2026 shipped nothing new for TTS.** No new TTS model, no GA promotion for `gemini-3.1-flash-tts-preview`, no Chirp 4, no streaming for Gemini TTS, no narration-specific session API. The week's audio-adjacent news was Gemini Omni (video gen), Gemini Live UI tweaks, Docs/Gmail/Keep voice **input**, and Android XR "audio glasses." The Cloud TTS release notes' most recent entry is still December 30, 2025; the Gemini API changelog's most recent TTS entry is still the April 15, 2026 launch of 3.1 Flash TTS Preview.
- **For a `speak` CLI doing long-form narration today, the right architecture is a two-track design**: (1) **Cloud TTS bidirectional streaming with Chirp 3: HD voices** as the production default (true streaming, sub-300 ms TTFB, name-pinned voice identity that does not drift, MP3/OGG/LINEAR16 output), and (2) **`gemini-3.1-flash-tts-preview` as an opt-in "expressive" mode** for short, emotionally-directed passages, with manual chunking and forced retries because long-form drift is a documented and observable failure mode.
- **Cross-chunk voice consistency is still NOT natively solved by Google.** There is no session-stateful narration API. The two real anchors you have are (a) the named voice constant (e.g. `Kore`, `en-US-Chirp3-HD-Charon`), which is by far the stronger anchor, and (b) a verbatim-replayed system/style prompt prepended to each chunk. The Gemini Live API's `session_resumption` and `sliding_window` compression exist for **conversational** sessions, not for stateless narration synthesis — using them for narration is an off-label hack that will not give you a single coherent audio stream.

## Key Findings

**1. Nothing TTS-specific landed at I/O 2026.** Across `blog.google`, `developers.googleblog.com`, `cloud.google.com/blog`, and `ai.google.dev/changelog` for May 18–22, 2026, there is **zero** TTS product news. The keynote's audio content was: per Google's "Making it easier to understand how content was created and edited" post, "we've integrated SynthID into our generative media models and products, watermarking over 100 billion images and videos and 60,000 years of audio"; "audio glasses" ship this fall; Docs Live lets users "verbally 'brain dump'" into a draft (STT, not TTS). The Gemini API models index, last updated May 18, 2026, still lists the same TTS lineup as April: `gemini-2.5-flash-tts` (GA), `gemini-2.5-pro-tts` (GA), and `gemini-3.1-flash-tts-preview` (Preview).

**2. The real "newest" Google TTS is the April 15, 2026 `gemini-3.1-flash-tts-preview`.** Google's launch blog reports the model "achieved an impressive Elo score of 1,211" on the Artificial Analysis TTS leaderboard at launch — Google's best to date, though the live board as of May 20, 2026 shows 1,205 as new votes accumulate, and Inworld TTS 1.5 Max currently sits at Elo 1,206 (down from its ~1,236 launch snapshot per Inworld's promotional materials). 30 prebuilt voices named after astronomical objects (Kore, Charon, Aoede, Puck, Zephyr, Fenrir, Leda, Orus, etc.), 70+ languages auto-detected, 200+ inline audio tags (e.g. `[whispers]`, `[excited]`, `[short pause]`), native two-speaker dialogue, SynthID watermark on all outputs. Pricing: **$1.00 / 1M input tokens, $20.00 / 1M output tokens**; audio tokens convert at 25 tokens/sec, so one minute of audio output costs roughly **$0.03**, with Batch API at ~50% off (per OpenRouter and NemoVideo cost analyses).

**3. The documented failure modes of `gemini-3.1-flash-tts-preview` are dispositive for long-form narration.** Google's own docs list, verbatim: *"TTS does not support streaming"*, *"A TTS session has a context window limit of 32k tokens"*, *"Speech quality and consistency may begin to drift with generated outputs that are longer than a few minutes. We recommend splitting your transcripts into smaller chunks"*, *"Voice inconsistency with prompt instructions: The model's output may not always strictly match the selected speaker"*, and *"The model occasionally returns text tokens instead of audio tokens, causing the server to fail the request with a 500 error… implement automated retry logic."* TTSAudit's independent testing reported ~90% of generations longer than one minute degraded noticeably, with mumbled consonants and swallowed word endings by minute three. The Cloud TTS API enforces a hard cap of **4,000 bytes** on the `text` field and another **4,000 bytes** on the `prompt` field per request, and the Vertex AI endpoint truncates audio output at **~655 seconds** (~11 minutes) per call. These limits force chunking on anything longer than ~600 words.

**4. Chirp 3: HD voices remain the better production choice for narration consistency.** Chirp 3 HD is GA, supports **true bidirectional streaming** via `texttospeech.StreamingSynthesizeConfig`, ships in global, us, eu, asia-southeast1 (plus asia-northeast1, europe-west2) regions, and supports SSML for synchronous requests (`<phoneme>`, `<say-as>`, `<sub>`, `<p>`, `<s>`). 30 named voices across 31 locales, plus pace control (0.25x–2.0x), pause control, and custom pronunciations. The named voice ID (e.g. `en-US-Chirp3-HD-Charon`) is a hard timbre anchor that does not drift across calls — the same voice ID produces the same voice character every time. Pricing: **$30/1M characters**, with a 1M char/month free tier. Output formats: LINEAR16, MP3, OGG Opus, MULAW (with WAV headers, unlike Vertex Gemini-TTS which is raw PCM 24kHz/16-bit).

**5. Chirp 3 Instant Custom Voice gives you a real "voice anchor" for narration.** 10 seconds of consent + 10 seconds of reference audio produces a `voice_cloning_key` (stored client-side) that is reusable across unlimited calls, supports streaming, multilingual transfer (an en-US key can synthesize de-DE, es-ES, es-US, fr-CA, fr-FR, pt-BR), and inherits all the HD voice controls. Pricing: **$60/1M characters**. This is currently the only Google offering that gives you a stable named identity that you control — it is the closest thing to ElevenLabs Voice Cloning that Google ships.

**6. Gemini Live API is *not* a narration API.** Google's docs are explicit: *"The TTS capability differs from speech generation provided through the Live API, which is designed for interactive, unstructured audio… While the Live API excels in dynamic conversational contexts, TTS through the Gemini API is tailored for scenarios that require exact text recitation."* Live API limits: 15 min audio-only sessions (extendable indefinitely via `sliding_window` context compression, but compression discards history and degrades context), 10 min connection lifetime (reconnect via `session_resumption` handles, valid 2 hr post-disconnect on AI Studio, 24 hr on Vertex), 128k token context, 1,000 concurrent sessions/project. The model IDs are `gemini-3.1-flash-live-preview` (AI Studio) and `gemini-live-2.5-flash-native-audio` (Vertex GA). Using Live for narration means you are paying audio-token rates (~25 tokens/sec) for a model trained on conversation, with voice consistency tied to the same 30 prebuilt voices.

**7. Cross-chunk consistency: what actually works.** Empirically, the best stack today is: pin a named voice ID + replay a 3–5 sentence style preamble at each chunk boundary + split at sentence/paragraph boundaries (not mid-sentence) + use the same temperature (0.7–1.0) and the same seed-equivalents (Google doesn't expose seeds for TTS, but deterministic-ish output comes from identical prompts). DEV community contributor reports "breaking scripts into ~200-word chunks and re-injecting a short style preamble at each chunk boundary." Scenario AI's production guidance: *"Use the same voice preset for the same character across all sessions. The model is not stateful, but consistent voice selection produces consistent output across batches."* This is the same pattern every serious user is converging on; Google has not given us anything better.

**8. Streaming, properly speaking.** Only **Chirp 3 HD** (via Cloud Text-to-Speech API) and **Gemini Live API** stream natively. `gemini-3.1-flash-tts-preview` does NOT stream — there is a `:streamGenerateContent?alt=sse` SSE endpoint that *appears* to stream but Google's developer forum confirms a known bug returning truncated 20s/1,280,000-char base64 chunks. The Cloud TTS streaming API uses bidirectional gRPC: first message in the stream carries `StreamingSynthesizeConfig`, subsequent messages carry text, and the server emits audio chunks back. TTFB for Chirp 3 HD is sub-300 ms in benchmarks. For "stream to file AND play simultaneously," the cleanest pattern is to tee the chunked audio bytes into both a `wave.open()` writer and a real-time PCM player (sounddevice/pyaudio) as they arrive.

**9. Pricing landscape and "what one hour of narration costs."** Using the 25 tokens/sec conversion for Gemini TTS audio: 1 hour of audio ≈ 90,000 output tokens ≈ **$1.80** at standard rates, **$0.90** with Batch API. Chirp 3 HD at $30/1M characters: 1 hour of speech (~10,000 spoken words ≈ 50,000 characters) ≈ **$1.50**. ElevenLabs Multilingual v2 character pricing varies by plan — per Cekura and Toolradar third-party breakdowns (2026), the Creator plan rate is ~$0.30/1K chars and Pro is ~$0.24/1K chars (the $0.18/min overage figure applies only on the Business plan at $1,320/mo). So an hour of narration on a typical paid ElevenLabs tier lands around $12–15 per hour. **For pure cost-per-hour of narration, Chirp 3 HD and Gemini TTS Batch API are at the bottom of the market.**

**10. Competitive context.** As of May 2026, independent leaderboards (Artificial Analysis Speech Arena, TTS-Arena2) place **Inworld TTS-1.5 Max** at the top of public Elo rankings (currently 1,206, peaked at ~1,236 at March 2026 launch), **Gemini 3.1 Flash TTS** at 1,205–1,211, and **Fish Audio S2 Pro** at the top of EmergentTTS-Eval — per the Fish Audio S2 Technical Report (arXiv:2603.08823v2, March 2026), "Fish Audio S2 achieves the highest overall win rate at 81.88%, outperforming all listed systems and exceeding the 50% baseline margin by +31.88 points." **ElevenLabs v3** is still widely considered the gold standard for English audiobook naturalness despite not topping every Elo board. **For long-form narration consistency specifically**, ElevenLabs Multilingual v2 and Inworld retain a meaningful lead over both Gemini TTS models in published reviews and blind tests — Google's models drift more, ElevenLabs and Inworld drift less. Cartesia Sonic 3 leads on latency — per Inworld's independent benchmark page (April 2026), Sonic 3 achieves "40 ms TTFB on the Turbo variant and sub-100 ms on the standard model." MARS8-Flash claims ~100 ms TTFB. No one has published a rigorous benchmark explicitly named "narration consistency over N minutes" — TTSAudit's 90% degradation finding for 3.1 Flash TTS is the closest thing to a public benchmark.

## Details

**Authoritative model strings as of May 20, 2026**
- Cloud TTS API (recommended for `speak`): `en-US-Chirp3-HD-{Charon|Kore|Aoede|Puck|Leda|Zephyr|Fenrir|Orus|...}` via `texttospeech.TextToSpeechClient` (Python SDK ≥ 2.31.0 for Gemini TTS support).
- Vertex AI Gemini-TTS GA: `gemini-2.5-flash-tts`, `gemini-2.5-pro-tts`, `gemini-2.5-flash-lite-preview-tts`.
- Gemini API (AI Studio): `gemini-3.1-flash-tts-preview` (Preview, no GA date announced as of I/O 2026).
- Gemini Live API: `gemini-3.1-flash-live-preview` (AI Studio), `gemini-live-2.5-flash-native-audio` (Vertex GA).

**Authentication patterns**
- Cloud TTS streaming requires a service account or Application Default Credentials; the streaming gRPC endpoint is not callable with a plain `GEMINI_API_KEY`.
- `gemini-3.1-flash-tts-preview` via AI Studio is callable with `GEMINI_API_KEY` against `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-tts-preview:generateContent` (or `:streamGenerateContent?alt=sse`, with the truncation caveat).
- Vertex variant: `https://aiplatform.googleapis.com/v1beta1/projects/$PROJECT_ID/locations/us-central1/publishers/google/models/gemini-3.1-flash-tts-preview:generateContent` with `gcloud auth application-default print-access-token`.

**Recommended architecture for the `speak` CLI**

A two-track pipeline, voice-tier-selectable at the command line:

```
speak --voice chirp3-hd-charon  --stream  document.md       →  default narration path
speak --voice gemini-kore  --style "warm, slow, documentary"  document.md  →  expressive path
speak --voice cloned:my-key.txt  --stream  document.md      →  Instant Custom Voice
```

Track A — **Default narration (production)**: Cloud TTS bidirectional streaming with Chirp 3: HD voice. Chunk text at sentence boundaries into ~3,000-byte chunks (well under the 5,000-byte sync cap and aligned with streaming semantics). Feed sequential chunks into a single `streaming_synthesize` generator so audio is emitted continuously. Tee output bytes into both a `wave.open()` writer (LINEAR16, mono, 24 kHz) AND a real-time audio sink (sounddevice/pyaudio). Voice timbre is anchored by the voice name — `en-US-Chirp3-HD-Charon` produces the same Charon every call.

Track B — **Expressive narration**: `gemini-3.1-flash-tts-preview` for short, emotionally-directed passages. Hard-chunk to ≤ ~600 words / ≤ 90 seconds of audio output per request (i.e. below the documented drift threshold). For each chunk: send the same 5-component system instruction (Audio Profile, Scene, Director's Notes, Sample Context, Transcript) verbatim — this is the only "anchor" mechanism available. Implement exponential-backoff retry on 500s (the docs explicitly warn about random "text tokens instead of audio" returns). Convert PCM 16-bit 24 kHz mono to MP3/M4A client-side with ffmpeg. Do NOT attempt to use `:streamGenerateContent` for production — the truncation bug is unresolved.

Track C — **Voice cloning (one-time setup)**: Use `Chirp 3: Instant Custom Voice` to generate a `voice_cloning_key` once (10 sec consent statement + 10 sec reference). Store the key locally. Pass via `VoiceCloneParams(voice_cloning_key=...)` in every subsequent streaming or batch call. This gives the user the same voice on every chunk, in every language the en-US key supports (de-DE, es-ES, es-US, fr-CA, fr-FR, pt-BR).

**Python skeleton for Track A (Chirp 3 streaming with tee-to-file-and-play)**

```python
from google.cloud import texttospeech
import sounddevice as sd
import wave

def narrate_streaming(chunks, voice="en-US-Chirp3-HD-Charon", out_path="out.wav"):
    client = texttospeech.TextToSpeechClient()
    cfg = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            name=voice, language_code="en-US",
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )
    def gen():
        yield texttospeech.StreamingSynthesizeRequest(streaming_config=cfg)
        for c in chunks:
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=c))
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000)
        stream = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16")
        stream.start()
        for resp in client.streaming_synthesize(gen()):
            wf.writeframes(resp.audio_content)
            stream.write(resp.audio_content)
        stream.stop(); stream.close()
```

**Why not Live API for narration?** Live is positioned for sub-second conversational turn-taking. Using it for narration means: (a) you pay audio output tokens (~25 tokens/sec) without ever using the input modalities that justify the price; (b) the model is trained to handle interruption and partial turns, not extended monologue; (c) session compression discards context, which can affect prosody continuity in subtle ways; (d) the voices are the same 30 Gemini voices, so you don't gain a better timbre anchor than just calling Gemini TTS directly. The only narrow case where Live is interesting is if you want voice-modulated streaming feedback from a Gemini reasoning model in the same loop (e.g. "narrate this document with live commentary"), which is not the `speak` use case.

**Honest list of what is still unsolved**
- **No session-stateful narration API.** Every TTS call is stateless. There is no `narration_session_id` you can pass to chunk 2 to tell the model "render the same voice and prosody you used in chunk 1." The voice name is your only anchor.
- **No SSML on Chirp 3 streaming.** SSML works in synchronous Chirp 3 calls; it is silently ignored on streaming calls. This is a meaningful limitation if you need precise pause and pronunciation control at scale.
- **No Gemini TTS streaming.** Promised by the docs ("Gemini TTS now supports synthesis for streaming requests" in Cloud TTS release notes), but the `gemini-3.1-flash-tts-preview` SSE endpoint has the truncated-output bug, and the Vertex variant supports only single-request/multi-response streaming, not true bidirectional input streaming.
- **Long-form drift on Gemini TTS.** Independent testing shows ~90% of >1-minute generations degrade. Mitigation: chunk hard, retry on quality. Do not try to fight it.
- **No public benchmark for narration consistency.** TTSAudit is the closest thing; Artificial Analysis measures naturalness on short clips. If consistency matters to your users, A/B-test on your own content.
- **Gemini 3.1 Flash TTS is still Preview.** No GA date, no SLA, API surface can change. Build behind an abstraction.
- **Audio tags must be in English** even when the spoken text is in another language. Cloud-blog guidance: "*Please note that the tags are in English only, but English-language tags can be combined with text in other languages.*"

## Recommendations

**Stage 1 — Ship now (this week):**
1. Build `speak` against **Cloud Text-to-Speech bidirectional streaming with Chirp 3: HD voices** as the default. Use `en-US-Chirp3-HD-Charon` or `en-US-Chirp3-HD-Kore` for general narration; let the user override via `--voice`.
2. Implement sentence-boundary chunking (use `pysbd` or `spaCy` sentencizer; never split mid-sentence) at ~3,000-byte chunks.
3. Tee output to both a WAV file and a real-time PCM sink as audio arrives. Use `sounddevice.RawOutputStream` on macOS — it's the cleanest path for an Apple developer audience.
4. Use Application Default Credentials (`gcloud auth application-default login`) rather than service-account JSON for a CLI — better UX for the developer-tool audience.

**Stage 2 — Add expressive mode (next 2–4 weeks):**
5. Add a `--style "..."` flag that routes through `gemini-3.1-flash-tts-preview` via the AI Studio endpoint (`GEMINI_API_KEY` only, no GCP project required). Enforce a hard chunk limit of 600 words and document the long-form drift caveat in `--help`.
6. Implement exponential-backoff retry on 500 errors and a quality-check pass that detects text-instead-of-audio returns (response shape inspection).
7. Convert PCM output to MP3 with embedded ffmpeg.

**Stage 3 — Voice cloning (optional, when a user asks):**
8. Add a `speak voice clone <reference.wav> <consent.wav>` subcommand that registers a `voice_cloning_key` and stores it in `~/.config/speak/voices/`.
9. Route `--voice cloned:<name>` through `Chirp 3: Instant Custom Voice` streaming. Multilingual transfer is free; advertise it.

**Don't do:**
- Don't build on `gemini-3.1-flash-tts-preview:streamGenerateContent?alt=sse` until Google fixes the 20-second truncation bug (open issue in the Google AI Developers Forum as of this writing).
- Don't try to use Gemini Live API session resumption to "stitch" narration chunks. It's the wrong tool; the session is a conversational context, not a TTS continuation primitive.
- Don't roll your own SSML escaping for Gemini TTS — it doesn't take SSML, it takes inline tags and natural-language style prompts. Pass them through.

**Benchmarks that would change these recommendations:**
- If `gemini-3.1-flash-tts-preview` ships GA with documented long-form stability fixes → make it the default expressive mode.
- If Cloud TTS adds true bidirectional streaming for `gemini-2.5-pro-tts` (currently single-request streaming on Vertex only) → reconsider as default for cost-sensitive narration.
- If TTSAudit or Artificial Analysis publishes a narration-consistency benchmark and ElevenLabs/Inworld widen the gap, add a `--provider elevenlabs` fallback.
- If Google announces `Gemini 3.5 Flash TTS` or `Chirp 4` (neither exists as of May 20, 2026), re-architect the abstraction.

## Caveats
- **Search coverage caveat:** Some third-party "review" sites (chatforest.com, tokenmix.ai, almcorp.com) are explicitly AI-authored research aggregations, not hands-on testing — I relied on them only where their numbers cross-checked with Google's official docs.
- **I/O 2026 caveat:** I/O sessions through May 20 were largely agentic and Omni-focused. Google has historically pushed audio announcements to later events (Cloud Next, separate DeepMind posts). Expect a TTS-related drop sometime between now and Cloud Next '26 — but build for what exists today.
- **Preview-product caveat:** `gemini-3.1-flash-tts-preview` is Preview. The pricing, the model ID, and the response schema are all subject to change with as little as 2 weeks' notice per Google's preview-model policy.
- **Voice consistency caveat:** "Same voice name produces same voice character" is true at a perceptual level, not a bit-exact level. There is no seed parameter exposed for TTS. Run your own A/B to validate that Charon-on-chunk-1 sounds like Charon-on-chunk-17 in your specific content domain.
- **Regional availability caveat:** Chirp 3 HD streaming is in global, us, eu, asia-southeast1, asia-northeast1, europe-west2 — not every region. If you operate in a different region you may face higher latency. The user is at Apple (US-based), so this is a non-issue.
- **Leaderboard volatility caveat:** Elo numbers on Artificial Analysis and TTS-Arena2 move daily as votes accumulate. The Gemini 3.1 Flash TTS 1,211 figure was the April 15 launch snapshot; live boards as of May 20 show 1,205. Treat any single Elo number as an estimate within ±15.
- **The most important caveat:** Google did not move on TTS at I/O 2026. If your roadmap assumed an I/O announcement would solve cross-chunk consistency or unlock streaming on Gemini TTS, **that assumption is now falsified**. Plan accordingly.