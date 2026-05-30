#!/usr/bin/env bash
# engines/gcloud.sh — Google Cloud Chirp 3 HD adapter for `speak`
#
# Conforms to engines/CONTRACT.md (THE LAW). One job: render ONE chunk of text
# in ONE already-resolved realization to ONE playable WAV at --output, and
# report tokens/errors via sidecars. The driver owns resolution, gating,
# chunking, auth pre-resolution, concat, probing, badging, and output routing.
#
# Engine facts (engines.yaml gcloud):
#   - endpoint:        https://texttospeech.googleapis.com/v1/text:synthesize
#   - auth:            gcloud-oauth (Bearer token + X-Goog-User-Project project)
#   - output_format:   linear16  (needs_transcode: false — NO ffmpeg here)
#   - voice_prefix:    en-US-Chirp3-HD-
#   - billing_unit:    characters → .tokens written as `<charcount>,0`
#   - project_fallback: g-drive-474022
#
# This adapter contains NO fallback / nearest-voice / default-realization logic.
# It renders --realization verbatim; an unrenderable name is a CHUNK_ERROR, not
# a substitution (CONTRACT §1 rule 1, policy:missing-realization).

set -euo pipefail

ENDPOINT="https://texttospeech.googleapis.com/v1/text:synthesize"
VOICE_PREFIX="en-US-Chirp3-HD-"
PROJECT_FALLBACK="g-drive-474022"

# ── Argument parsing (CONTRACT §1; order not significant) ─────────────
realization=""
output=""
text=""
have_text=false           # distinguishes empty --text from absent --text
language_code="en-US"      # gcloud default_language_code (engines.yaml)
# --model is accepted but ignored: gcloud's models.default is null (Chirp3 has
# no model id to select). Swallowing it keeps the calling convention identical
# across all three adapters (CONTRACT §1).

while [[ $# -gt 0 ]]; do
  case "$1" in
    --realization)    realization="${2:-}"; shift 2 ;;
    --output)         output="${2:-}"; shift 2 ;;
    --text)           text="${2:-}"; have_text=true; shift 2 ;;
    --model)          shift 2 ;;                       # accepted, ignored
    --language-code)  language_code="${2:-}"; shift 2 ;;
    *)                shift ;;                          # ignore unknown flags harmlessly
  esac
done

# ── Failure helper ────────────────────────────────────────────────────
# Writes a CHUNK_ERROR: sidecar, ensures no partial WAV / .tokens remain at
# --output, and exits nonzero WITHOUT killing the parent (CONTRACT §4.2/§5).
fail() {
  local msg="$1"
  if [[ -n "$output" ]]; then
    printf 'CHUNK_ERROR:%s\n' "$msg" > "${output}.err"
    rm -f "$output" "${output}.tokens" 2>/dev/null || true
  fi
  echo "speak[gcloud]: ${msg}" >&2
  exit 1
}

# ── Validate required flags ───────────────────────────────────────────
[[ -n "$output" ]]      || { echo "speak[gcloud]: --output is required" >&2; exit 1; }
[[ -n "$realization" ]] || fail "no realization supplied (--realization is required)"

# A fresh run must not be contaminated by a prior failure's sidecar.
rm -f "${output}.err" 2>/dev/null || true

# ── Input: --text escape hatch, else stdin (CONTRACT §2) ──────────────
if [[ "$have_text" == false ]]; then
  text="$(cat)"
fi
[[ -n "$text" ]] || fail "empty chunk text"

# ── Auth: consume driver-exported env; re-resolve only if empty ───────
# (CONTRACT §6 / behavior #11 — the hot path is env-supplied so the 3 parallel
# children don't each re-shell gcloud. Re-resolution is the defensive fallback.)
access_token="${GCLOUD_ACCESS_TOKEN:-}"
gcp_project="${GCLOUD_PROJECT:-}"

if [[ -z "$access_token" ]]; then
  access_token=$(gcloud auth application-default print-access-token 2>/dev/null \
                 || gcloud auth print-access-token 2>/dev/null \
                 || echo "")
fi
if [[ -z "$gcp_project" ]]; then
  gcp_project=$(gcloud config get-value project 2>/dev/null || echo "")
  [[ -n "$gcp_project" ]] || gcp_project="$PROJECT_FALLBACK"
fi

[[ -n "$access_token" ]] || fail "gcloud authentication failed"

# ── Realization → fully-qualified Chirp3 voice name ───────────────────
# The driver passes the complete name (en-US-Chirp3-HD-<persona>). We prefix
# defensively only if it isn't already present (task instruction + parity with
# the extracted body). This is NOT nearest-voice/fallback logic — it's name
# normalization on a verbatim realization.
cloud_voice="$realization"
# Only prefix a BARE persona name. A name already carrying a Chirp3-HD locale
# marker (en-US-…, en-GB-…) passes through verbatim — guards against the
# double-prefix bug where a non-en-US name would become en-US-Chirp3-HD-en-GB-…
if [[ "$cloud_voice" != *Chirp3-HD-* ]]; then
  cloud_voice="${VOICE_PREFIX}${realization}"
fi

# ── Build request body (JSON-safe text via python3) ───────────────────
# input.text, voice.name=<cloud_voice>, voice.languageCode=<lc>,
# audioConfig.audioEncoding=LINEAR16.
request_body=$(REQ_TEXT="$text" REQ_VOICE="$cloud_voice" REQ_LC="$language_code" \
  python3 -c '
import json, os
print(json.dumps({
    "input":       {"text": os.environ["REQ_TEXT"]},
    "voice":       {"languageCode": os.environ["REQ_LC"], "name": os.environ["REQ_VOICE"]},
    "audioConfig": {"audioEncoding": "LINEAR16"},
}))
') || fail "failed to build request body"

# ── Call the Text-to-Speech API ───────────────────────────────────────
response=$(curl -s "$ENDPOINT" \
  -H "Authorization: Bearer ${access_token}" \
  -H "X-Goog-User-Project: ${gcp_project}" \
  -H "Content-Type: application/json" \
  -d "$request_body") || fail "curl request failed"

# ── Parse: extract audioContent or a CHUNK_ERROR:-prefixed message ────
# audio_data is EITHER the raw base64 audioContent OR a 'CHUNK_ERROR:...' string.
audio_data=$(printf '%s' "$response" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, dict) and "error" in data:
        print("CHUNK_ERROR:" + str(data["error"].get("message", "unknown error")), end="")
    else:
        audio = data.get("audioContent", "") if isinstance(data, dict) else ""
        if audio:
            print(audio, end="")
        else:
            print("CHUNK_ERROR:audioContent missing", end="")
except Exception as e:
    print("CHUNK_ERROR:json parse or unexpected error: " + str(e), end="")
') || fail "response parse failed"

if [[ "$audio_data" == CHUNK_ERROR:* ]]; then
  # Strip the prefix; fail() re-adds it so the .err format is canonical.
  fail "${audio_data#CHUNK_ERROR:}"
fi

# ── Decode LINEAR16 base64 straight to WAV (NO ffmpeg transcode) ──────
# Chirp3 LINEAR16 is already a WAV container. Decode to a temp file first so a
# decode failure never leaves a partial WAV at --output (CONTRACT §3/§4.2).
tmp_wav="${output}.part.$$"
if ! printf '%s' "$audio_data" | base64 --decode > "$tmp_wav" 2>/dev/null; then
  rm -f "$tmp_wav" 2>/dev/null || true
  fail "base64 decode of audioContent failed"
fi
# A valid WAV is non-empty and begins with the RIFF magic.
if [[ ! -s "$tmp_wav" ]] || [[ "$(head -c 4 "$tmp_wav")" != "RIFF" ]]; then
  rm -f "$tmp_wav" 2>/dev/null || true
  fail "decoded audio is not a valid WAV"
fi
mv -f "$tmp_wav" "$output"

# ── Tokens sidecar: gcloud bills by character → `<charcount>,0` ───────
# (CONTRACT §4.1 / behavior #9). Character count of the chunk text; completion
# is always 0. Written ONLY on success.
char_count=${#text}
printf '%s,0' "$char_count" > "${output}.tokens"

exit 0
