# speak

Gemini-powered text-to-speech with narration intelligence.

`speak` takes text, runs it through Gemini to optimize it for spoken delivery (adding pacing, audio tags, emotional cues), then renders it to audio through Gemini's native TTS model. Two API calls, one command.

## Install

```bash
# Clone and symlink
git clone https://github.com/haroldyoung/speak.git
ln -s "$(pwd)/speak/speak" ~/.local/bin/speak

# Or just copy
cp speak/speak ~/.local/bin/speak
chmod +x ~/.local/bin/speak
```

### Dependencies

- `bash` 4+
- `curl`
- `python3` (for JSON handling)
- `ffmpeg` / `ffprobe` (audio conversion)
- A Gemini API key — get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

```bash
export GEMINI_API_KEY="your-key-here"
```

## Usage

```bash
# Speak text directly
speak "Information has shape, and the shape persists across substrate."

# Speak clipboard contents
speak -c

# Choose a voice
speak -v Kore "This is the Kore voice."

# Skip narration optimization (raw mode)
speak -r "Already has [whispers] audio tags [pause] in it."

# Save to file instead of playing
speak -o chapter1.wav < chapter1.txt

# Pipe from stdin
cat essay.md | speak -v Charon

# Combine flags
speak -rc                    # raw + clipboard
speak -c -v Orus -o out.wav  # clipboard, specific voice, save to file

# Pipe audio to another process
speak -r --stdout "hello" | mpv -

# Get structured JSON output (metadata + base64 audio)
speak --json "some text" | jq .duration_seconds
speak -r --json "text" | jq -r .audio_wav_base64 | base64 -d > out.wav
```

## Long text and chunking

For text over ~500 words, `speak` automatically splits at paragraph boundaries, generates audio for each chunk in parallel, and concatenates the results. This avoids API size limits and quality drift on long outputs.

```bash
# Narrate a full chapter (auto-chunks)
speak < chapter.md

# Fast mode for quicker generation on long text
speak --fast < chapter.md

# Save a narrated chapter
speak -o chapter.wav < chapter.md
```

If individual chunks hit the API's content filter, `speak` reports which failed and concatenates the rest. The token count shown is the aggregate across all chunks.

## Programmatic usage

`speak` is designed to be called from other scripts and tools. All formatted output goes to stderr, keeping stdout clean for data.

```bash
# --stdout writes raw WAV bytes to stdout
speak -r --stdout "alert text" | ffplay -nodisp -autoexit - 2>/dev/null

# --json returns structured metadata with embedded audio
result=$(speak --json "some text")
echo "$result" | jq '.voice, .duration_seconds, .tokens'

# JSON output shape:
# {
#   "voice": "Puck",
#   "duration_seconds": 5,
#   "raw": false,
#   "tokens": { "optimize": "142/89", "tts": "89/432" },
#   "transcript": "the optimized transcript...",
#   "audio_wav_base64": "UklGR..."
# }
```

## Voices

30 built-in voices with distinct character. Run `speak voices` to see them all:

```
◇  Zephyr          Bright
◆  Puck            Upbeat        ← default
▣  Charon          Informative
◈  Kore            Firm
△  Fenrir          Excitable
...
```

Change your default:

```bash
speak set voice Kore
```

## Audio tags

The narration optimizer adds these automatically, but you can also use them directly with `-r`:

| Tag | Effect |
|-----|--------|
| `[whispers]` | Whispered delivery |
| `[excited]` | High energy |
| `[serious]` | Measured, grave |
| `[warmly]` | Warm tone |
| `[slowly]` | Reduced pace |
| `[pause]` | Brief silence |
| `[laughs]` | Laughter |
| `[sighs]` | Audible sigh |
| `[shouting]` | Raised volume |
| `[sarcastic]` | Sarcastic delivery |

Tags are not limited to this list. The model interprets natural language, so `[like a cartoon villain]` or `[as if revealing a secret]` also work.

## Configuration

Config lives at `~/.config/speak/config`:

```ini
voice=Puck
tts_model=gemini-3.1-flash-tts-preview
optimize_model=gemini-2.5-flash
```

```bash
speak set                    # view current config
speak set voice Charon       # change default voice
```

## How it works

1. **Optimize** — Your text is sent to Gemini 2.5 Flash with a narration director prompt. It returns the same text with audio tags, pacing cues, and style direction added.
2. **Synthesize** — The optimized transcript is sent to Gemini 3.1 Flash TTS, which returns raw PCM audio.
3. **Convert** — ffmpeg wraps the PCM in a WAV container.
4. **Play** — `afplay` (macOS), `paplay`/`aplay` (Linux), or `mpv`.

Skip step 1 with `-r` if your text is already tagged or you want the model's default interpretation.

## Terminal output

`speak` shows a formatted summary while it works:

```
speak v0.1.0
  voice  ◈ Kore
  ──────────────────────────────────
  [serious] Information has topological properties...
  ──────────────────────────────────
  duration  12s    tokens  opt:142/89 tts:89/1240
  ✓ done
```

Token counts are shown as `prompt/completion` for both the optimization and TTS calls.

## Platform support

| Platform | Audio player | Clipboard |
|----------|-------------|-----------|
| macOS | `afplay` | `pbpaste` |
| Linux (PulseAudio) | `paplay` | `xclip` / `xsel` |
| Linux (PipeWire) | `paplay` | `xclip` / `xsel` / `wl-paste` |
| Linux (ALSA) | `aplay` | `xclip` / `xsel` |
| Any | `mpv` (fallback) | — |

## License

MIT
