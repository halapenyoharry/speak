# speak rewrite — implementation brief (Workflow B)

This brief is the single source of truth for the implementation agents. It encodes the
panel-hardened spec (`speak.target.v2.hypergraph.topothink.json`), the four resolved
decisions, the adapter contract, and the load-bearing behaviors that MUST survive the
strangler refactor.

## Resolved decisions (do not re-litigate)

1. **Missing realization → HARD ERROR.** When the requested persona has no realization in
   the active engine, exit nonzero with an actionable message naming the engines that DO
   realize it. **No `--nearest` flag.** Never substitute, never fall back. The driver MUST
   NOT contain any "default realization" or "nearest voice" code path.
2. **Command name stays `speak`.** Installed at `/opt/homebrew/bin/speak` (symlink → repo).
3. **`.wrangler/` is out of scope.** Cloudflare = deployment target, not a TTS engine. Do
   not touch `.wrangler/`. There are exactly THREE engines: gemini, gcloud, kokoro.
4. **Strangler refactor, not greenfield.** Evolve the current 939-line `speak` in place;
   extract only the synthesis bodies into adapters. Preserve orchestration verbatim.

## Architecture (from target.v2)

```
speak (bash driver)
 ├─ load + VALIDATE voices.yaml + engines.yaml at startup (fail loud, exit nonzero)
 ├─ resolve (persona, active-engine) → realization   [sc:resolve-voice]
 │    └─ no realization? → hard error naming available engines   [policy:missing-realization]
 ├─ optimize?  fires IFF active engine lists `audio-tags` capability AND GEMINI_API_KEY set
 │    └─ under kokoro/gcloud: SKIPPED entirely (kokoro = fully offline, no key needed)
 ├─ chunk if word_count > 500   [engine-agnostic]
 ├─ dispatch to engines/<engine>.{sh,py}  [polymorphism lives here]
 │    └─ bounded-parallel: cloud max_in_flight=3, kokoro=1 (serial)
 ├─ concat WAV chunks if num_chunks > 1
 └─ output route: play | -o file | --stdout | --json | --show wave|spec
       └─ badge MUST name the realization ACTUALLY rendered, not just the persona
```

## Language decision

- `speak` — bash driver (python3 only as a JSON/numeric helper).
- `engines/gemini.sh`, `engines/gcloud.sh` — bash adapters, curl-based.
- `engines/kokoro.py` — python adapter (evolved from `kokoro_tts.py`).
- No venv unless `engine=kokoro`.

## Adapter contract (every adapter conforms)

- **Input:** the chunk text + a realization id/spec. Invocation convention (pick one and
  make all three identical): `engines/<engine>.{sh,py} --text <T> --realization <R> --output <WAV> [--model <M>]`
  where `<R>` is the resolved realization (a voice id, or for kokoro a blend spec).
- **Output:** a *playable WAV* at the engine's stable format. The adapter normalizes its own
  output — it MUST NOT emit raw PCM. (gemini transcodes PCM s16le/24k/mono→WAV via ffmpeg;
  gcloud decodes LINEAR16→WAV; kokoro writes WAV via soundfile.)
- **Sidecars:** write `<WAV>.tokens` as `prompt,completion` (gemini=token counts;
  gcloud=`<charcount>,0`; kokoro=`local`). On failure write `<WAV>.err` with a
  `CHUNK_ERROR:`-prefixed message.
- **Semantics:** return nonzero on failure WITHOUT aborting the parent (so partial-failure
  tolerance works); order-preserving; idempotent.

## Data file schemas

### voices.yaml
- `default_persona: puck`
- `personas:` keyed by lowercase id; each: `{intent, style, glyph, color, default?, realizations}`.
- `realizations:` keyed by engine config-value (`gemini`|`gcloud`|`kokoro`). **A realization is
  present IFF its engine key exists. No fallback key, no default realization.**
- gemini realization: `{voice: Puck}`. gcloud: `{voice: en-US-Chirp3-HD-Puck, language_code: en-US}`.
  kokoro: EITHER `{voice: am_michael}` OR `{blend: [{voice: am_adam, weight: 0.5}, {voice: am_michael, weight: 0.5}]}` — never both.
- Enumerate ALL 30 personas (names/styles/glyphs/colors from the current `speak` VOICES array).
  The 9 personas kokoro_tts.py maps get a kokoro realization (puck→blend, charon→am_michael,
  kore→am_michael, zephyr→am_adam, fenrir→am_adam, leda→af_bella, orus→am_michael,
  aoede→af_sarah, callirrhoe→af_sky). The other 21 OMIT the kokoro key.
- Blend weights: **auto-normalize** (don't reject if they don't sum to 1).
- gcloud realizations: only emit `en-US-Chirp3-HD-<name>` names; flag in a comment that these
  should be verified against the live Chirp3 voice list (some may not exist) — do not guarantee them.

### engines.yaml
- engines keyed by config-value; per-engine fields: `label, deployment, auth, capabilities[],
  default_model, models{}, endpoint, output_format, needs_transcode, voice_prefix,
  default_language_code, billing_unit, max_in_flight, default_persona, preconditions[]`.
- top-level `optimizer:` section: `{model: gemini-2.5-flash, auth: gemini-api-key, provides_capability: audio-tags}`.
- `capabilities` is the SINGLE source of truth the optimizer-gating reads. gemini: `[audio-tags]`;
  gcloud: `[]`; kokoro: `[]`.
- kokoro `native_voices:` map each voice → `{language_code}` (am_adam: en-us, bm_george: en-gb, …)
  so the adapter looks up locale from data, never from bf_/bm_ string-prefix.
- `max_in_flight`: gemini 3, gcloud 3, kokoro 1.
- `project_fallback: g-drive-474022` on gcloud.

## Config migration (`~/.config/speak/config`, runs once, errors on unmappable value)

| old | new |
|---|---|
| `use_gcloud=true` | `engine: gcloud` |
| `use_gcloud=false` | `engine: gemini` |
| `voice=Puck` | `persona: puck` (lowercase-normalize; FAIL if no matching persona) |
| `tts_model=X` | `model_overrides: {gemini: X}` |
| `optimize_model=X` | `optimizer_model: X` |

Model precedence: `--model` flag > `model_overrides[engine]` > `engines.yaml models.default`.
`--model fast` resolves to `engines.yaml gemini.models.fast` (absorbs legacy `--fast`).

## CLI surface (clean slate)

- `-e, --engine gemini|gcloud|kokoro` (single-valued; **deletes** the mutual-exclusion guard)
- `-m, --model NAME` (engine-scoped key or literal id; absorbs `--fast` as `--model fast`)
- `-v, --voice PERSONA` (operates on personas, case-insensitive, canonical-cap storage)
- keep `-r, -c, -o, -q, -s/--show, --stdout, --json` unchanged (audit deemed them clean)
- `speak voices` = personas for the ACTIVE engine, marking realization; unrealized shown dimmed
  with reason. `speak voices --all` = persona×engine coverage grid.
- `speak set engine|persona|model …` mirrors config.
- **DELETE** `-g/--gcloud`, `--no-gcloud`, `-k/--kokoro`, `-f/--fast`.

## Pointers (read these)

- Spec graph: `topology/speak.target.v2.hypergraph.topothink.json` (and its metadata block).
- Current implementation to evolve: `speak` (939 lines) and `kokoro_tts.py`.
- Portability fix: replace BSD-only `sed -i ''` with a portable in-place edit (speak also runs on Pop!_OS/lumen).
- Portability fix: `kokoro_tts.py` uses an unverified-SSL download context — keep working but note it.

---

## Behaviors that MUST survive (verbatim from the critique panel)

1. PCM s16le 24kHz mono -> WAV transcode via ffmpeg, GEMINI PATH ONLY: tts_one_chunk decodes base64 to a .pcm file then runs `ffmpeg -y -loglevel error -f s16le -ar 24000 -ac 1 -i pcm out.wav` and rm's the pcm. Chirp3 emits LINEAR16 base64 decoded straight to .wav; Kokoro writes .wav via soundfile. The rewrite MUST keep the per-engine output-format normalization contract: each adapter is responsible for emitting a playable WAV, NOT raw PCM. The exact ffmpeg invocation (s16le/24000/mono) is load-bearing for Gemini and must not be applied to the other two.

2. Paragraph-then-sentence chunk splitting at 500 words (CHUNK_MAX_WORDS=500): chunk_text() splits on double-newline paragraph boundaries; any single paragraph exceeding 500 words is further split by sentence regex `(?<=[.!?])\s+`; chunks are re-joined with '\n\n'. Word counting via `wc -w`. Threshold gate: chunking only fires when word_count > 500. The exact paragraph-first-then-sentence algorithm and the 500-word number are behavior to preserve.

3. Batch-of-3 parallel synthesis: chunks are launched in batches of batch_size=3 as background jobs (`tts_one_chunk ... &`), each batch waited on before the next starts; this bounds concurrency to 3 simultaneous API calls. The rewrite MUST preserve a concurrency cap (not unbounded fan-out, not serial) — 3 is the load-bearing number that respects API rate limits.

4. Partial-failure tolerance with per-chunk .err files: each chunk that fails writes `<wav>.err` with a CHUNK_ERROR: prefixed message; after all batches, failed chunks are counted and their messages printed, but synthesis CONTINUES with whatever WAVs succeeded; only if ALL chunks fail (valid_wavs empty) does it abort. A single-WAV result is `cp`'d; multiple are concatenated. This graceful-degradation semantics (some audio better than none for long inputs) is load-bearing and currently exists ONLY on the cloud paths — the rewrite must extend it to Kokoro, not drop it.

5. ffmpeg concat of ordered WAV chunks: builds a concat.txt manifest (`file '<path>'` lines in chunk order) and runs `ffmpeg -y -loglevel error -f concat -safe 0 -i concat.txt -c copy out.wav`. Order preservation (chunk_0, chunk_1, ...) is load-bearing — chunks must concatenate in input order. `-c copy` (stream copy, no re-encode) requires all chunk WAVs share a codec/sample-rate; a mixed-engine future would break this, so the invariant 'all chunks come from the same engine/format' must be preserved.

6. Terminal voice badges with ANSI color + glyph: get_voice_entry() looks up name|style|color|glyph; print_voice_badge() emits `\033[<color>m<glyph> <name>\033[0m`. cmd_voices lists all 30 with color+glyph+style and a `← default` marker. Colors auto-disable when stderr is not a TTY (`[[ ! -t 2 ]]`). The rewrite must preserve: (a) per-voice color+glyph identity, (b) the default marker, (c) the not-a-tty color-stripping. CRITICAL BUG TO FIX, NOT PRESERVE: under -k the badge currently prints a hardcoded green ◆ + the REQUESTED name (lines 587-591) while the audio is actually a different mapped/blended voice — the badge lies. The persona/realization split must make the badge show what was actually rendered.

7. Cross-platform clipboard read: -c/--clipboard tries pbpaste, then xclip -selection clipboard -o, then xsel --clipboard, then wl-paste, erroring if none found. All four fallbacks are load-bearing (macOS + X11 + Wayland coverage).

8. Output routing modes --stdout / --json / -o / --show, and their side effects: --stdout writes raw WAV bytes to stdout and sets play=false quiet=true; --json emits a python-built JSON object {voice, duration_seconds, raw, tokens:{optimize,tts}, transcript, audio_wav_base64} and sets play=false quiet=true; -o FILE cp's the WAV and sets play=false; default plays. --show [wave|spec] uses ffplay -showmode 1|2 with window title `speak: <voice>`. The target graph metadata says these were 'already clean — nothing to redesign' and EXPLICITLY OMITS them; the rewrite must carry every one forward verbatim, INCLUDING the json field names and the quiet/play side-effects, or it silently breaks pipelines.

9. opt/tts token accounting display: Gemini optimize call parses usageMetadata.promptTokenCount/candidatesTokenCount into `opt:<p>/<c>`; each TTS chunk writes `<wav>.tokens` as `prompt,completion`; these are summed across chunks into `tts:<p>/<c>`; Chirp3 writes `<charcount>,0` (char-count as a proxy since gcloud bills by character); Kokoro reports the literal string `local`. The final summary line prints duration + tokens. The --json output also surfaces tokens. The per-engine token-accounting convention (Gemini=tokens, gcloud=chars, kokoro=local) is load-bearing and must be preserved as an adapter responsibility.

10. Symlink resolution to find kokoro_tts.py: cmd_speak resolves BASH_SOURCE[0] through a readlink loop to find the real script dir, then invokes `python3 <scriptdir>/kokoro_tts.py`. This is REQUIRED because speak is installed as a symlink at /opt/homebrew/bin/speak -> ~/Projects/speak/speak. Any rewrite that splits into a driver + adapter modules MUST keep robust symlink-resolved path discovery so the driver finds its sibling adapters/voices.yaml/engines.yaml regardless of how it's invoked.

11. gcloud credential pre-resolution and project fallback: before chunked synthesis, cmd_speak resolves an access token via `gcloud auth application-default print-access-token` (fallback `gcloud auth print-access-token`), exports GCLOUD_ACCESS_TOKEN, and resolves project via `gcloud config get-value project` with a hardcoded fallback `g-drive-474022`; tts_one_chunk re-resolves if the env vars are empty. Pre-resolving ONCE before fanning out to 3 parallel chunks (so each chunk doesn't re-shell gcloud) is a load-bearing optimization. The fallback project string is a real value baked in.

12. Per-engine auth gating with raw-mode interaction: GEMINI_API_KEY is required unless (use_kokoro AND raw) — the nested condition at lines 574-582 means: optimization needs Gemini key even when the synth engine is gcloud or kokoro, because the optimizer is a Gemini call. So 'kokoro + raw' is the only fully-offline path; 'kokoro without raw' still hits Gemini for optimization and still needs the key. The rewrite's capability-gated optimizer changes this: under kokoro the optimizer should be SKIPPED (not just key-checked), making kokoro offline by default. This is a deliberate behavior CHANGE the target intends — flag it as such so it's not mistaken for a regression.

13. Config load/save format and BSD-sed dependency: load_config reads `key=value` lines (xargs-trimmed, # comments skipped) for voice/tts_model/optimize_model/use_gcloud with hardcoded defaults; save_config_value uses `sed -i ''` (BSD/macOS in-place syntax — NOT GNU-compatible) to update an existing key or appends. The rewrite changes the config schema (engine/persona/model-overrides) but must preserve (a) the comment-tolerant key=value reader, (b) cross-platform in-place edit (the BSD `sed -i ''` is a latent Linux portability bug to FIX, since the network topology shows speak also expected to run on lumen/Pop!_OS).

14. Voice-name normalization and case-insensitive lookup: get_voice_entry does case-insensitive match (tolower) and `speak set voice` normalizes the stored value to the registry's canonical capitalization. `-v` similarly normalizes. The persona lookup in the rewrite must preserve case-insensitive resolution and canonical-capitalization storage.

15. audio-tag highlighting in transcript display: when not quiet, the optimized transcript is printed with bracketed tags recolored yellow via sed, line-by-line, dimmed, between rule lines. This is the visible payoff of 'narration intelligence'. Preserve the tag-highlight display for engines that run the optimizer.

16. duration probe + human-friendly formatting: ffprobe reads format=duration, integer-truncated, formatted as `<m>m<s>s` or `<s>s`, with `~` fallback when unavailable. Preserve.

17. Kokoro model auto-download with progress bar and macOS SSL workaround: kokoro_tts.py downloads kokoro-v1.0.onnx and voices-v1.0.bin from the thewh1teagle GitHub release into ~/.config/speak/models/ on first use, with a Mozilla user-agent and `ssl._create_unverified_context()` to bypass macOS CA issues, showing a progress bar. NOTE: the unverified SSL context is a security smell (downloads model binaries over an unverified TLS connection) — preserve the auto-download UX but FIX the unverified-SSL by using certifi or the system trust store. Per Harold's global rule, downloading from the internet/CDN should be surfaced explicitly.

18. Kokoro voice blending math: kokoro_tts.py special-cases the 'adam-michael' name by fetching get_voice_style('am_adam') and get_voice_style('am_michael') and computing `0.5*style_adam + 0.5*style_michael` as the voice vector; en-gb voices (bf_/bm_) switch lang to en-gb, else en-us. This blend IS a first-class realization (the target graph correctly models realization:kokoro-puck-blend). Preserve the blend-vector computation and the en-us/en-gb language inference as an adapter capability — and let voices.yaml express blend weights as data so retuning isn't a code edit.

19. stderr/stdout discipline: ALL human-facing chrome (header, badges, progress, summary, errors) goes to stderr (`>&2`); only machine output (--stdout WAV bytes, --json payload) goes to stdout. This separation is what makes `speak --stdout | mpv -` and `speak --json | jq` work. It is absolutely load-bearing and easy to break in a rewrite — every progress/status print in the new driver must target stderr.

20. set -euo pipefail strictness and mktemp tmpdir lifecycle: the script runs under strict mode; a tmpdir is mktemp'd and rm -rf'd on every exit path (stdout/json/play/file/error). The rewrite must preserve guaranteed tmpdir cleanup (ideally via a trap, which the current code lacks — minor improvement opportunity) and strict-mode safety.

21. subcommand dispatch surface: `voices`, `set [key value]` (no-arg = show config), `--help`, `--version`, else treat args as speak. `set` with no args prints current config with a voice badge. The rewrite may change flags (clean-slate granted) but should consciously decide the fate of each subcommand; `voices` and `set` are the discoverability surface.
