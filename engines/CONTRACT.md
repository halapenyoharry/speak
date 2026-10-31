# speak — engine adapter contract (CANONICAL)

This file is **the law** that every adapter must satisfy. The driver (`speak`) and the
four adapters (`engines/gemini.sh`, `engines/gcloud.sh`, `engines/kokoro.py`, `engines/bark.py`) agree on
exactly the interface specified here. If an adapter and this document disagree, the
adapter is wrong.

Scope: there are **four** engines — `gemini`, `gcloud`, `kokoro`, `bark`. No others.
`.wrangler/` is not an engine and is out of scope.

The contract is **language-agnostic**: a `.sh` adapter and a `.py` adapter satisfy the
identical `text + realization → playable WAV + sidecars` convention. The driver invokes
all four the same way; polymorphism lives only in the dispatch (`engines/<engine>.{sh,py}`),
never in the calling convention.

---

## 0. One-line summary (the convention)

```
engines/<engine>.{sh,py} --realization <R> --output <WAV> [--model <M>] [--language-code <LC>]   (text on stdin)
```

- `<R>` is the **already-resolved realization** the driver computed via `sc:resolve-voice`.
  For `gemini`/`gcloud` it is a single voice id. For `kokoro` it is EITHER a single voice id
  OR a blend spec `voice:weight,voice:weight,...`.
- Chunk text arrives on **stdin** (see §2 — `--text` is also accepted as an escape hatch).
- The adapter writes exactly one playable WAV to `<WAV>` and the sidecars in §4.
- Exit `0` on success, nonzero on failure, and **never** kills the parent (§5).

The driver does all resolution, gating, chunking, auth pre-resolution, concatenation,
duration probing, badging, and output routing. The adapter's sole job is: **render one
chunk of text in one realization to one playable WAV, and report tokens/errors via
sidecars.** Adapters do not chunk, do not concatenate, do not optimize, do not play.

---

## 1. Invocation convention (exact)

Every adapter MUST accept these flags, with these exact long names. Adapters MUST ignore
unknown flags they don't use only if doing so is harmless; otherwise treat an unrecognized
required combination as a failure (§5). Order of flags is not significant.

| Flag | Required | Meaning | Applies to |
|---|---|---|---|
| `--realization <R>` | **yes** | The resolved realization (voice id or blend spec). See §1.1. | all |
| `--output <WAV>` | **yes** | Absolute path where the adapter writes its single playable WAV. | all |
| `--text <T>` | no | Chunk text passed as an argument (escape hatch). If absent, read text from **stdin**. See §2. | all |
| `--model <M>` | no | Concrete model **id** (already resolved by the driver — never a key like `fast`). If absent, the adapter uses its own engine default. | gemini, gcloud, bark (kokoro ignores) |
| `--language-code <LC>` | no | BCP-47 locale (e.g. `en-US`, `en-GB`). Driver passes it from the realization/native-voice data. If absent, adapter falls back to its engine default. | gcloud, kokoro, bark |

Rules:

1. The driver passes `--realization` **post-resolution**. The adapter receives a concrete
   voice/blend and renders it verbatim. **An adapter MUST NOT contain any fallback,
   nearest-voice, or default-realization logic.** If `--realization` names something the
   engine cannot render, that is a `CHUNK_ERROR` (§4/§5), not a substitution.
2. `--model` is always a **literal engine model id**, never a key (`fast`, `default`).
   Key→id resolution and the precedence chain (`--model` flag > `model_overrides[engine]` >
   `engines.yaml models.default`) all happen in the driver before invocation.
3. The driver pre-resolves and exports auth/env (§6). Adapters read those env vars; they do
   NOT re-shell `gcloud` per chunk when the env is already populated (load-bearing
   optimization — see §6).
4. Adapters write all human-facing chrome to **stderr** and write **nothing** to stdout
   except, optionally, nothing at all. The WAV goes to `--output`, not stdout. (The driver
   owns the `--stdout`/`--json` machine-output contract; an adapter writing to stdout would
   corrupt it.) See §7.

### 1.1 Realization argument grammar

- **gemini:** `<R>` is a bare Gemini prebuilt voice name, e.g. `Puck`, `Sulafat`. Passed to
  the API as `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName`.
- **gcloud:** `<R>` is a fully-qualified Chirp 3 HD voice name, e.g. `en-US-Chirp3-HD-Puck`.
  The driver passes the complete name; the adapter does **not** synthesize the
  `en-US-Chirp3-HD-` prefix. (Prefix construction now lives in resolution/data, not the
  adapter.) `--language-code` accompanies it (default `en-US`).
- **kokoro:** `<R>` is EITHER:
  - a single native voice id — `am_michael`, `af_bella`, `bm_george`; OR
  - a **blend spec**: comma-separated `voice:weight` pairs, e.g.
    `am_adam:0.5,am_michael:0.5` or `am_adam:0.3,am_michael:0.7`.
  Grammar: `<spec> := <pair>("," <pair>)*` , `<pair> := <voice_id> ":" <float>`. A spec with
  no comma and no colon is the single-voice form. Weights are **auto-normalized** by the
  adapter (divide by their sum); the adapter MUST NOT reject weights that don't sum to 1.0.
  The blend vector is `Σ (weight_i / Σweights) · style(voice_i)` — see behavior #18. Locale
  comes from `--language-code` (driver supplies it from `engines.yaml native_voices`), never
  from a `bf_`/`bm_` string-prefix guess.
- **bark:** `<R>` is a Bark speaker preset string, e.g. `v2/en_speaker_6`, or path to a
  custom voice embedding file (`.npz`). `--model` specifies checkpoint (`suno/bark-small` default).

---

## 2. Input

- **Primary:** chunk text on **stdin**. The driver pipes one chunk's text to the adapter's
  stdin. This avoids `ARG_MAX` limits and shell-escaping hazards on large chunks.
- **Escape hatch:** `--text <T>`. If `--text` is present, the adapter uses it and ignores
  stdin. If `--text` is absent, the adapter reads all of stdin as the chunk text.
- The text is a **single chunk** (already ≤ 500 words after the driver's chunking, or the
  whole input if under threshold). The adapter never re-chunks.
- The driver passes the **post-optimizer transcript** (or the raw text under `-r`/kokoro).
  The adapter does not optimize and does not interpret audio tags beyond what the engine
  natively does (Gemini consumes bracketed tags as prosody; gcloud/kokoro pass them through
  as literal text — that gating is the driver's concern via `capabilities`).

---

## 3. Output

- The adapter writes **exactly one playable WAV** to the `--output` path. **Never raw PCM.**
  Each adapter normalizes its own engine's native format to WAV:

  | Engine | Native bytes | Normalization (load-bearing) |
  |---|---|---|
  | gemini | base64 PCM `s16le`, 24000 Hz, mono | decode b64 → `.pcm`, then **exactly** `ffmpeg -y -loglevel error -f s16le -ar 24000 -ac 1 -i <pcm> <out.wav>`, then remove the `.pcm`. This exact invocation is load-bearing **for gemini only** and MUST NOT be applied to the other two. |
  | gcloud | base64 `LINEAR16` | decode b64 straight to `<out.wav>` (Chirp3 LINEAR16 is already a WAV container). |
  | kokoro | float samples in memory | write `<out.wav>` via `soundfile` (e.g. `sf.write(out, audio, sample_rate)`). |
  | bark   | float samples in memory | write `<out.wav>` via `soundfile` at 24000 Hz PCM_16. |

- All chunk WAVs from a single run come from the **same engine**, so they share codec /
  sample-rate and the driver can `ffmpeg ... -c copy` concat them. The adapter must produce a
  WAV the engine's other chunks will concat with byte-for-byte (don't randomly vary
  sample-rate within an engine).
- The WAV at `--output` must be the **only** artifact at that path on success (the adapter
  cleans up any intermediate `.pcm`/temp files it created).

---

## 4. Sidecars

Adapters communicate metadata and failure back to the driver **only** through sidecar files
next to `--output`. The driver reads these after `wait`; the adapter never prints them to
stdout.

### 4.1 `<output>.tokens` (written on SUCCESS)

Single line, **exactly** `prompt,completion` — two comma-separated fields, no spaces, no
trailing newline required (the driver `cut -d,`s it). Per-engine convention (load-bearing,
behavior #9):

| Engine | `<output>.tokens` content | Source |
|---|---|---|
| gemini | `<promptTokenCount>,<candidatesTokenCount>` | the TTS response `usageMetadata` (real token counts). |
| gcloud | `<charcount>,0` | character count of the chunk text (gcloud bills by character; char-count is the proxy, completion is always `0`). |
| kokoro / bark | `local` | the literal string `local` (offline, no billing). The driver tolerates this non-numeric form and surfaces it verbatim. |

The driver sums numeric `prompt`/`completion` across chunks for the `tts:<p>/<c>` summary
and the `--json` `tokens.tts` field; for kokoro it passes `local` through unchanged.

### 4.2 `<output>.err` (written on FAILURE)

On any failure to produce a valid WAV, the adapter writes a single-line message to
`<output>.err`, **prefixed with `CHUNK_ERROR:`**, e.g.:

```
CHUNK_ERROR:gcloud authentication failed
CHUNK_ERROR:audioContent missing
CHUNK_ERROR:json parse or unexpected error: <detail>
CHUNK_ERROR:realization 'en-US-Chirp3-HD-Nonexistent' not available
```

- The message after the prefix is human-readable; the driver prints it under the failed-chunk
  count. Keep it to one line.
- On failure the adapter MUST NOT leave a partial/garbage WAV at `--output` (write the `.err`
  and ensure no playable-but-broken WAV remains). Do **not** write `<output>.tokens` on failure.
- The `CHUNK_ERROR:` prefix is the contract the driver greps for (behavior #4). Do not change it.

---

## 5. Exit codes and process semantics

1. **Exit `0`** on success (WAV written + `.tokens` written).
2. **Exit nonzero** on any failure (`.err` written, no WAV). Any nonzero code is treated as
   failure; the specific value is not interpreted.
3. **Never kill the parent.** Adapters run as backgrounded children (`adapter ... &`) under
   bounded-parallel batches; a failing adapter must exit nonzero quietly and let the driver
   account for it. Adapters MUST NOT `kill` the parent, `exit` the parent's shell, or emit
   signals beyond their own process. (Cloud adapters run with `set -euo pipefail` but must
   still convert internal failures into a clean `.err` + nonzero exit, not an uncaught abort
   that would propagate.)
4. **Partial-failure tolerance (behavior #4) is the driver's job and depends on this:** the
   driver continues with whatever WAVs succeeded and only aborts if *all* chunks fail. The
   adapter's responsibility is solely to report its own per-chunk success/failure honestly.
5. **Idempotent and self-contained.** Re-invoking with the same args reproduces the same
   `<output>` (overwriting any prior file at that path). The adapter does not depend on or
   mutate state outside `--output`, its sidecars, and engine-side API state. It does not read
   config files, `voices.yaml`, or `engines.yaml` — all needed values arrive as flags/env.
6. **Order/independence.** Adapters are stateless w.r.t. each other and may run concurrently
   (cloud `max_in_flight=3`) or serially (kokoro `max_in_flight=1`). Order preservation across
   chunks is the driver's concern (chunk-index filenames + concat manifest); an adapter must
   not assume it is chunk N or write to any path other than the `--output` it was given.

---

## 6. Auth / environment (driver pre-resolves, adapter consumes)

The driver resolves credentials **once** before fanning out and exports them into the
adapters' environment. Adapters read env vars; they re-resolve **only** if a required var is
empty (defensive, but the hot path is env-supplied — re-shelling `gcloud` per chunk across 3
parallel children is the load-bearing anti-pattern this avoids, behavior #11).

| Engine | Env the driver exports | Adapter behavior |
|---|---|---|
| gemini | `GEMINI_API_KEY` | Sent as header `x-goog-api-key`. If empty → `CHUNK_ERROR:` + nonzero. |
| gcloud | `GCLOUD_ACCESS_TOKEN`, `GCLOUD_PROJECT` | `Authorization: Bearer $GCLOUD_ACCESS_TOKEN`, `X-Goog-User-Project: $GCLOUD_PROJECT`. If `GCLOUD_ACCESS_TOKEN` empty, may re-resolve via `gcloud auth application-default print-access-token` (fallback `gcloud auth print-access-token`); if `GCLOUD_PROJECT` empty, may re-resolve via `gcloud config get-value project` with hardcoded fallback **`g-drive-474022`**. If still no token → `CHUNK_ERROR:gcloud authentication failed` + nonzero. |
| kokoro | none required (fully offline) | No API key. May read its own model cache under `~/.config/speak/models/`. Auto-download of model binaries (if missing) is permitted but should be surfaced on stderr; downloads MUST use a verified TLS context (system trust / `certifi`), not an unverified SSL context. |
| bark   | none required (fully offline) | No API key. Uses Hugging Face cache. Auto-download is permitted but checked against available disk space (requires >= 2.0 GB). |

The optimizer (a separate Gemini or OpenRouter call) is **never** invoked by an adapter. Optimization is a
driver concern gated on `capabilities` containing `audio-tags`; under kokoro it is skipped
entirely, so a bare kokoro run needs no API key. A bare bark run with `-r` needs no key.

---

## 7. stderr / stdout discipline (load-bearing, behavior #19)

- **stdout from an adapter: reserved.** The driver owns the machine-output channel
  (`--stdout` raw WAV bytes, `--json` payload). An adapter MUST NOT write to stdout. Its WAV
  goes to `--output`; its metadata goes to sidecars.
- **stderr: allowed for diagnostics only.** Progress, warnings, the download notice —
  all to stderr. The driver's own progress/badges also go to stderr; adapter stderr should be
  minimal so it doesn't clutter the driver's chrome.

---

## 8. Conformance checklist (all adapters must pass)

- [ ] Accepts `--realization`, `--output`, optional `--text`/`--model`/`--language-code`; reads
      stdin when `--text` absent.
- [ ] Writes exactly one **playable WAV** (never raw PCM) at `--output`; cleans up temp files.
- [ ] gemini uses the exact `ffmpeg -f s16le -ar 24000 -ac 1` transcode; gcloud decodes
      LINEAR16 directly; kokoro/bark write via soundfile. No engine uses another's path.
- [ ] On success writes `<output>.tokens` = `prompt,completion` per the per-engine convention
      (gemini real tokens, gcloud `<chars>,0`, kokoro/bark `local`); writes no `.err`.
- [ ] On failure writes `<output>.err` = `CHUNK_ERROR:<msg>` (single line), no WAV, no `.tokens`.
- [ ] Exit `0` success / nonzero failure; never kills the parent; idempotent; touches only
      `--output` + sidecars.
- [ ] Contains **no** fallback / nearest-voice / default-realization code path.
- [ ] Reads auth from env (gemini key / gcloud token+project); kokoro/bark need none.
- [ ] Writes nothing to stdout; diagnostics to stderr only.
