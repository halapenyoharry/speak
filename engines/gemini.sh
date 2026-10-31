#!/usr/bin/env bash
# engines/gemini.sh — Gemini native-TTS adapter for `speak`.
#
# Conforms EXACTLY to engines/CONTRACT.md. One job: render ONE chunk of text in
# ONE realization to ONE playable WAV at --output, and report tokens/errors via
# sidecars. No chunking, no concat, no optimize, no play, no fallback.
#
#   engines/gemini.sh --realization <R> --output <WAV> [--model <M>] [--text <T>]   (text on stdin)
#
# Load-bearing behavior extracted verbatim from the original tts_one_chunk Gemini
# branch (speak ~lines 402-457):
#   - generateContent request: responseModalities [AUDIO],
#     speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName=<realization>
#   - decode inlineData.data (base64 PCM s16le/24000/mono) → .pcm
#   - transcode: ffmpeg -y -loglevel error -f s16le -ar 24000 -ac 1 -i <pcm> <out.wav>
#     (this exact invocation is load-bearing for gemini ONLY)
#   - rm the .pcm
#   - parse usageMetadata.promptTokenCount,candidatesTokenCount → <output>.tokens
#   - on failure: <output>.err prefixed "CHUNK_ERROR:", nonzero exit, no WAV, no .tokens
#
# Auth/env (driver pre-resolves, adapter consumes — CONTRACT §6): GEMINI_API_KEY,
# sent as header x-goog-api-key. Empty key → CHUNK_ERROR + nonzero.

set -euo pipefail

# Endpoint base for the Gemini generateContent TTS call (engines.yaml gemini.endpoint).
API_BASE="https://generativelanguage.googleapis.com/v1beta/models"
# Engine default model when --model is absent (engines.yaml gemini.models.default).
DEFAULT_MODEL="gemini-3.1-flash-tts-preview"

# ── Argument parsing ──────────────────────────────────────────────────
REALIZATION=""
OUTPUT=""
TEXT=""
TEXT_GIVEN=false
MODEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --realization)    REALIZATION="${2:-}"; shift 2 ;;
    --output)         OUTPUT="${2:-}"; shift 2 ;;
    --text)           TEXT="${2:-}"; TEXT_GIVEN=true; shift 2 ;;
    --model)          MODEL="${2:-}"; shift 2 ;;
    # CONTRACT §1: ignore unknown flags only when harmless. --language-code does
    # not apply to gemini; accept-and-discard so a uniform driver invocation is OK.
    --language-code)  shift 2 ;;
    *)                shift ;;
  esac
done

# ── fail(): write a CHUNK_ERROR sidecar and exit nonzero, WITHOUT killing the
# parent (CONTRACT §5). One line, "CHUNK_ERROR:" prefix (the driver greps it).
# Ensures no partial/garbage WAV and no .tokens remain at --output. ──────
fail() {
  local msg="$1"
  if [[ -n "$OUTPUT" ]]; then
    printf 'CHUNK_ERROR:%s\n' "$msg" > "${OUTPUT}.err"
    rm -f "$OUTPUT" "${OUTPUT%.wav}.pcm" "${OUTPUT}.tokens" 2>/dev/null || true
  else
    printf 'CHUNK_ERROR:%s\n' "$msg" >&2
  fi
  exit 1
}

# ── Required-argument validation ──────────────────────────────────────
[[ -n "$OUTPUT" ]]      || { printf 'CHUNK_ERROR:--output is required\n' >&2; exit 1; }
[[ -n "$REALIZATION" ]] || fail "--realization is required"

# A fresh run must not be contaminated by a prior failure's sidecar (idempotency,
# CONTRACT §5.5; parity with gcloud.sh).
rm -f "${OUTPUT}.err" 2>/dev/null || true

# Resolve model: --model wins (it is a literal engine model id per CONTRACT §1
# rule 2, never a key); otherwise the engine default.
MODEL="${MODEL:-$DEFAULT_MODEL}"

# ── Input text: --text escape hatch, else all of stdin (CONTRACT §2) ────
if [[ "$TEXT_GIVEN" == true ]]; then
  CHUNK_TEXT="$TEXT"
else
  CHUNK_TEXT="$(cat)"
fi
# Fail fast on empty text rather than burning a billed round-trip (parity with gcloud.sh).
[[ -n "$CHUNK_TEXT" ]] || fail "empty chunk text"

# ── Auth (CONTRACT §6): GEMINI_API_KEY from env, sent as x-goog-api-key ──
API_KEY="${GEMINI_API_KEY:-}"
[[ -n "$API_KEY" ]] || fail "GEMINI_API_KEY not set"

# Precondition: python3 handles all JSON (request body build + response parse).
command -v python3 >/dev/null 2>&1 || fail "python3 not found (required for JSON handling)"
command -v curl    >/dev/null 2>&1 || fail "curl not found"
command -v ffmpeg  >/dev/null 2>&1 || fail "ffmpeg not found (required to transcode PCM → WAV)"

# ── Build the request body via python3 (json.dumps escapes text + voice
# name safely; matches the original responseModalities/prebuiltVoiceConfig
# shape). ──────────────────────────────────────────────────────────────
REQUEST_BODY=$(
  python3 -c '
import json, sys
text, voice = sys.argv[1], sys.argv[2]
body = {
    "contents": [{"parts": [{"text": text}]}],
    "generationConfig": {
        "responseModalities": ["AUDIO"],
        "speechConfig": {
            "voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": voice}
            }
        }
    }
}
sys.stdout.write(json.dumps(body))
' "$CHUNK_TEXT" "$REALIZATION"
) || fail "failed to build request body"

# ── Call the Gemini generateContent TTS endpoint ──────────────────────
RESPONSE=$(
  curl -s "${API_BASE}/${MODEL}:generateContent" \
    -H "x-goog-api-key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$REQUEST_BODY"
) || fail "curl_failed"

# ── Parse the inlineData base64 audio (or surface the API error). On the
# happy path stdout is exactly the base64 string; on failure it is a single
# "CHUNK_ERROR:<msg>" line (preserves the original finishMessage extraction). ──
AUDIO_DATA=$(
  printf '%s' "$RESPONSE" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception as e:
    print("CHUNK_ERROR:json parse or unexpected error: " + str(e), end="")
    sys.exit(0)
# Surface a top-level API error if present.
if isinstance(data, dict) and "error" in data:
    print("CHUNK_ERROR:" + data["error"].get("message", "unknown error"), end="")
    sys.exit(0)
try:
    audio = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    print(audio, end="")
except (KeyError, IndexError, TypeError):
    msg = "unknown error"
    try:
        msg = data.get("candidates", [{}])[0].get("finishMessage", "unknown error")
    except Exception:
        pass
    print("CHUNK_ERROR:" + msg, end="")
'
) || fail "json parse failed"

# ── Branch on whether parsing yielded audio or an error sentinel ──────
if [[ "$AUDIO_DATA" == CHUNK_ERROR:* ]]; then
  # Strip the sentinel prefix; fail() re-applies it canonically.
  fail "${AUDIO_DATA#CHUNK_ERROR:}"
fi

if [[ -z "$AUDIO_DATA" ]]; then
  fail "inlineData audio missing"
fi

# ── Decode base64 PCM → .pcm, then the load-bearing ffmpeg transcode to
# WAV (s16le / 24000 Hz / mono), then remove the .pcm. This exact invocation
# is load-bearing for gemini ONLY. ────────────────────────────────────
PCM_PATH="${OUTPUT%.wav}.pcm"

if ! printf '%s' "$AUDIO_DATA" | base64 --decode > "$PCM_PATH" 2>/dev/null; then
  rm -f "$PCM_PATH" 2>/dev/null || true
  fail "base64 decode of audio failed"
fi

if ! ffmpeg -y -loglevel error -f s16le -ar 24000 -ac 1 -i "$PCM_PATH" "$OUTPUT" >&2; then
  rm -f "$PCM_PATH" 2>/dev/null || true
  fail "ffmpeg transcode failed"
fi

rm -f "$PCM_PATH" 2>/dev/null || true

# ── Tokens sidecar (CONTRACT §4.1): <promptTokenCount>,<candidatesTokenCount>
# from the TTS response usageMetadata. Single line "prompt,completion", no
# spaces. (Defaults to 0,0 if usageMetadata absent.) ──────────────────
printf '%s' "$RESPONSE" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    u = data.get("usageMetadata", {})
    print(f"{u.get('"'"'promptTokenCount'"'"', 0)},{u.get('"'"'candidatesTokenCount'"'"', 0)}", end="")
except Exception:
    print("0,0", end="")
' > "${OUTPUT}.tokens" 2>/dev/null || printf '0,0' > "${OUTPUT}.tokens"

# Success: WAV + .tokens written, no .err, exit 0 (CONTRACT §5).
exit 0
