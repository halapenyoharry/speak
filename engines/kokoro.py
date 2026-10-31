#!/usr/bin/env python3
"""
engines/kokoro.py — local Kokoro TTS adapter for speak.

Conforms to engines/CONTRACT.md. Evolved from the old root-level kokoro_tts.py,
with the brief's required changes:

  - The realization arrives ALREADY RESOLVED from the driver (the old map_voice()
    nearest-voice table is GONE — that logic now lives in voices.yaml + the driver).
    There is no fallback / nearest / default-realization path here.
  - --realization is EITHER a single native voice id ("am_michael") OR a blend spec
    "voice:weight,voice:weight" (e.g. "am_adam:0.5,am_michael:0.5"). Blends are an
    N-voice weighted sum of style vectors with AUTO-NORMALIZED weights (generalizing
    the old hardcoded 2-voice 0.5/0.5 case).
  - Locale comes from --language-code (driver reads it from engines.yaml native_voices);
    it is NEVER inferred from a bf_/bm_ string prefix.
  - Text arrives on STDIN (--text is an escape hatch).
  - Sidecars: writes <output>.tokens = "local" on success; <output>.err =
    "CHUNK_ERROR:<msg>" on failure. No partial WAV / no .tokens on failure.
  - Exit 0 on success, nonzero on failure, without aborting the parent.
  - ALL diagnostics go to STDERR (stdout is reserved for the driver's machine channel).

Invocation (from the driver, per CONTRACT §1):
  printf '%s' "$text" | python3 engines/kokoro.py \
      --realization <R> --output <WAV> [--language-code <LC>] [--model <M>]
"""

import os
import sys
import ssl
import re
import json
import types
import argparse
import urllib.request
import numpy as np


def eprint(*a, **k):
    """Diagnostics to stderr only — stdout is the driver's reserved channel."""
    print(*a, file=sys.stderr, **k)


def clean_markdown_for_speech(text: str) -> str:
    """Transform Markdown formatting into clean, natural spoken prose so TTS
    engines never read formatting symbols ('asterisk asterisk asterisk', 'hash',
    'backtick') aloud, while preserving clause and line structure."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Strip fenced code block markers (keep inner content)
    t = re.sub(r"^\s*```[^\n]*\n?", "\n", t, flags=re.MULTILINE)

    # 2. Replace horizontal rules (***, ---, ___, * * *) with paragraph breaks
    t = re.sub(r"^\s*(?:[*_\-]\s*){3,}\s*$", "\n\n", t, flags=re.MULTILINE)
    t = re.sub(r"(?:^|\s)\*{3,}(?:\s|$)", "\n\n", t)

    # 3. Remove markdown table divider rows (|---|---|) and turn cell pipes into commas
    t = re.sub(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\|\s*|\s*\|\s*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s*\|\s*", ", ", t)

    # 4. Convert images ![alt](url) -> alt and links [label](url) -> label
    t = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)

    # 5. Simplify bare URLs to domain name so TTS doesn't spell out long paths
    t = re.sub(r"https?://(?:www\.)?([^/\s)\]>]+)[^\s)\]>]*", r"\1", t)

    # 6. Convert headings (# Heading) into clean pause-terminated lines
    def _fmt_heading(m):
        content = m.group(1).strip()
        if content and content[-1] not in ".!?:;—–-":
            return content + ":"
        return content

    t = re.sub(r"^\s*#{1,6}\s+(.+?)\s*#*$", _fmt_heading, t, flags=re.MULTILINE)

    # 7. Strip blockquote '>' and bullet list markers (-, *, +, •) at start of lines
    t = re.sub(r"^\s*>\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*+•]\s+", "", t, flags=re.MULTILINE)

    # 8. Strip bold, italic, strikethrough, and inline code delimiters
    for _ in range(2):
        t = re.sub(r"\*{1,3}([^*\n]+?)\*{1,3}", r"\1", t)
        t = re.sub(r"\b_{1,3}([^_\n]+?)_{1,3}\b", r"\1", t)
        t = re.sub(r"~~([^~\n]+?)~~", r"\1", t)
        t = re.sub(r"`+([^`\n]+?)`+", r"\1", t)

    # 9. Remove any leftover raw asterisks, backticks, or heading hashes
    t = re.sub(r"\*+", "", t)
    t = re.sub(r"`+", "", t)
    t = re.sub(r"(?:^|\s)#+(?=\s|$)", " ", t)

    # 10. Clean up multiple spaces while preserving newlines
    lines = []
    for line in t.split("\n"):
        cleaned_line = re.sub(r"\s+", " ", line).strip()
        if cleaned_line:
            lines.append(cleaned_line)
    return "\n".join(lines)


def split_into_clauses(cleaned_text: str) -> list[str]:
    """Split cleaned text into natural prosodic clauses so each clause is synthesized
    with an exact sample-accurate start and end timestamp."""
    raw_lines = [ln.strip() for ln in cleaned_text.split("\n") if ln.strip()]
    clauses = []

    for line in raw_lines:
        # First split on sentence/major clause punctuation
        major_parts = re.split(r"(?<=[.!?;:…—–])\s+", line)
        for mp in major_parts:
            mp = mp.strip()
            if not mp:
                continue
            # Split on commas if the clause has >= 4 words
            comma_parts = re.split(r"(?<=,)\s+", mp)
            acc = []
            acc_words = 0
            for cp in comma_parts:
                cp = cp.strip()
                if not cp:
                    continue
                w_len = len(cp.split())
                if acc and (acc_words >= 4 or acc_words + w_len > 12):
                    clauses.append(" ".join(acc))
                    acc = [cp]
                    acc_words = w_len
                else:
                    acc.append(cp)
                    acc_words += w_len
            if acc:
                clauses.append(" ".join(acc))

    # Sub-split any remaining long unpunctuated clauses (> 14 words)
    final_clauses = []
    for c in clauses:
        words = c.split()
        if len(words) <= 14:
            final_clauses.append(c)
        else:
            curr = []
            for w in words:
                curr.append(w)
                if len(curr) >= 10 and w.lower() in ("and", "but", "or", "which", "that", "because", "so", "when", "where", "while"):
                    # Keep conjunction with next clause if possible
                    if len(curr) > 1:
                        final_clauses.append(" ".join(curr[:-1]))
                        curr = [w]
                elif len(curr) >= 12:
                    final_clauses.append(" ".join(curr))
                    curr = []
            if curr:
                if final_clauses and len(curr) <= 2:
                    final_clauses[-1] = final_clauses[-1] + " " + " ".join(curr)
                else:
                    final_clauses.append(" ".join(curr))

    return [c.strip() for c in final_clauses if c.strip()]


def synthesize_with_timings(kokoro, text: str, voice_arg, speed: float = 1.0, lang: str = "en-us"):
    """Synthesize text clause-by-clause, returning (samples, sample_rate, timing_meta).
    Every clause's start, speech_end, and end timestamp is measured directly from
    Kokoro's output sample array (len(audio_part) / 24000.0) so visual captions
    stay 100% locked to the audio with zero cumulative drift."""
    cleaned = clean_markdown_for_speech(text)
    if not cleaned.strip():
        cleaned = text.strip()

    clauses = split_into_clauses(cleaned)
    if not clauses:
        clauses = [cleaned]

    sample_rate = 24000
    all_parts = []
    timing_clauses = []
    curr_samples = 0

    for idx, clause in enumerate(clauses):
        try:
            phonemes = kokoro.tokenizer.phonemize(clause, lang)
        except Exception:
            phonemes = ""
        if not phonemes or not phonemes.strip():
            continue

        part_samples, sample_rate = kokoro.create(clause, voice=voice_arg, speed=speed, lang=lang)
        if part_samples is None or len(part_samples) == 0:
            continue

        start_sec = curr_samples / sample_rate
        speech_end_sec = (curr_samples + len(part_samples)) / sample_rate

        # Natural prosodic breath pause between clauses
        if idx < len(clauses) - 1:
            if clause.endswith((".", "!", "?")):
                pause_sec = 0.14
            elif clause.endswith((":", ";", "—", "–")):
                pause_sec = 0.09
            elif clause.endswith(","):
                pause_sec = 0.05
            else:
                pause_sec = 0.03
            pause_samples = np.zeros(int(pause_sec * sample_rate), dtype=np.float32)
        else:
            pause_samples = np.zeros(0, dtype=np.float32)

        end_sec = (curr_samples + len(part_samples) + len(pause_samples)) / sample_rate

        all_parts.append(part_samples)
        if len(pause_samples) > 0:
            all_parts.append(pause_samples)

        curr_samples += len(part_samples) + len(pause_samples)
        timing_clauses.append({
            "text": clause,
            "words": clause.split(),
            "start": round(start_sec, 4),
            "speech_end": round(speech_end_sec, 4),
            "end": round(end_sec, 4),
        })

    if not all_parts:
        samples, sample_rate = kokoro.create(cleaned, voice=voice_arg, speed=speed, lang=lang)
        total_dur = len(samples) / sample_rate
        timing_clauses = [{
            "text": cleaned,
            "words": cleaned.split(),
            "start": 0.0,
            "speech_end": round(total_dur, 4),
            "end": round(total_dur, 4),
        }]
    else:
        samples = np.concatenate(all_parts)

    timing_meta = {
        "sample_rate": sample_rate,
        "total_duration": round(len(samples) / sample_rate, 4),
        "cleaned_text": " ".join(c["text"] for c in timing_clauses),
        "clauses": timing_clauses,
    }
    return samples, sample_rate, timing_meta


def patch_kokoro_phoneme_split(kokoro):
    """Upstream kokoro-onnx has a known bug in _split_phonemes: it only splits on
    [.,!?;]. When a sentence, code block, or unpunctuated text exceeds 510 phonemes,
    _split_phonemes leaves the batch > 510 phonemes, which causes _create_audio to
    truncate and crash with 'IndexError: index 510 is out of bounds for axis 0 with size 510'.
    
    This patches _split_phonemes to split safely at punctuation, newlines, and spaces
    with a max batch length of 400 phonemes, and patches _create_audio to clamp token
    indices within bounds of the voice style table."""
    def safe_split_phonemes(self, phonemes: str, max_len: int = 400) -> list[str]:
        parts = re.split(r'([.,!?;:\n—–\-])', phonemes)
        batches = []
        current = ''
        for part in parts:
            if not part:
                continue
            if len(part) > max_len:
                subparts = part.split(' ')
                for sp in subparts:
                    if not sp:
                        continue
                    if len(sp) > max_len:
                        for i in range(0, len(sp), max_len):
                            chunk = sp[i:i+max_len]
                            if len(current) + len(chunk) + 1 >= max_len:
                                if current.strip():
                                    batches.append(current.strip())
                                current = chunk
                            else:
                                current = (current + ' ' + chunk) if current else chunk
                    elif len(current) + len(sp) + 1 >= max_len:
                        if current.strip():
                            batches.append(current.strip())
                        current = sp
                    else:
                        current = (current + ' ' + sp) if current else sp
            elif len(current) + len(part) >= max_len:
                if current.strip():
                    batches.append(current.strip())
                current = part
            else:
                if part in '.,!?;:\n—–-':
                    current += part
                else:
                    if current and not current.endswith((' ', '\n')):
                        current += ' '
                    current += part
        if current.strip():
            batches.append(current.strip())

        final = []
        for b in batches:
            b = b.strip()
            if not b:
                continue
            if len(b) > 450:
                for i in range(0, len(b), 380):
                    sub = b[i:i+380].strip()
                    if sub:
                        final.append(sub)
            else:
                final.append(b)
        return [b for b in final if b]

    def safe_create_audio(self, phonemes, voice, speed):
        if len(phonemes) > 480:
            phonemes = phonemes[:480]
        tokens = np.array(self.tokenizer.tokenize(phonemes), dtype=np.int64)
        if len(tokens) >= len(voice):
            tokens = tokens[:len(voice) - 1]
        token_len = len(tokens)
        v_slice = voice[token_len]
        pad_tokens = [[0, *tokens, 0]]
        if 'input_ids' in [i.name for i in self.sess.get_inputs()]:
            inputs = {
                'input_ids': pad_tokens,
                'style': np.array(v_slice, dtype=np.float32),
                'speed': np.array([speed], dtype=np.int32),
            }
        else:
            inputs = {
                'tokens': pad_tokens,
                'style': v_slice,
                'speed': np.ones(1, dtype=np.float32) * speed,
            }
        audio = self.sess.run(None, inputs)[0]
        return audio, 24000

    kokoro._split_phonemes = types.MethodType(safe_split_phonemes, kokoro)
    kokoro._create_audio = types.MethodType(safe_create_audio, kokoro)


def _ssl_context():
    """Prefer verified TLS (certifi if present, else system trust); fall back to
    unverified only if the verified handshake fails (common on bare macOS python
    that lacks installed CA certs), with a loud stderr warning."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


def download_file(url, dest_path, description):
    eprint(f"  \033[90m{description} not found. Downloading...\033[0m")
    opener = urllib.request.build_opener()
    opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')]
    urllib.request.install_opener(opener)

    ctx = _ssl_context()

    def _open(context):
        return urllib.request.urlopen(url, context=context)

    try:
        try:
            response = _open(ctx)
        except (ssl.SSLError, urllib.error.URLError) as e:
            # Verified handshake failed — retry unverified with a warning. Keeps the
            # download working on machines without installed CA certs while still
            # PREFERRING verified TLS first (the brief's TLS-fix intent).
            eprint(f"  \033[33m⚠ verified TLS failed ({e}); retrying without verification\033[0m")
            response = _open(ssl._create_unverified_context())

        meta = response.info()
        file_size = int(meta.get("Content-Length", 0))
        chunk_size = 1024 * 1024
        bytes_downloaded = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                bytes_downloaded += len(chunk)
                if file_size > 0:
                    pct = bytes_downloaded / file_size * 100
                    w = 30
                    bar = "█" * int(pct / 100 * w) + "░" * (w - int(pct / 100 * w))
                    eprint(f"\r  \033[90m└─ [{bar}] {pct:.1f}% "
                           f"({bytes_downloaded/1048576:.1f}/{file_size/1048576:.1f} MB)\033[0m",
                           end="")
                else:
                    eprint(f"\r  \033[90m└─ {bytes_downloaded/1048576:.1f} MB\033[0m", end="")
        eprint("\n  \033[32m✓ Download complete\033[0m")
    except Exception as e:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise RuntimeError(f"download failed for {description}: {e}")


def parse_realization(realization):
    """Return a list of (voice, weight) pairs. A single voice id yields one pair
    with weight 1.0; a blend spec "v:w,v:w" yields one pair per component."""
    spec = realization.strip()
    if ":" not in spec:
        return [(spec, 1.0)]
    pairs = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            v, w = part.rsplit(":", 1)
            pairs.append((v.strip(), float(w)))
        else:
            pairs.append((part, 1.0))
    if not pairs:
        raise ValueError(f"empty/invalid realization spec: {realization!r}")
    return pairs


def main():
    p = argparse.ArgumentParser(description="Kokoro local TTS adapter (speak)")
    p.add_argument("--realization", required=True,
                   help="native voice id, or blend spec 'voice:weight,voice:weight'")
    p.add_argument("--output", required=True, help="output WAV path")
    p.add_argument("--language-code", dest="language_code", default="en-us",
                   help="locale supplied by the driver (never inferred here)")
    p.add_argument("--model", default=None, help="optional; ignored (kokoro uses its onnx model file)")
    p.add_argument("--text", default=None, help="escape hatch; otherwise text is read from stdin")
    args = p.parse_args()

    out = args.output
    err_path = out + ".err"
    tokens_path = out + ".tokens"

    def fail(msg):
        # CONTRACT §4/§5: write the .err sidecar, no partial output, nonzero exit,
        # do not abort the parent. Diagnostics to stderr.
        try:
            with open(err_path, "w") as f:
                f.write(f"CHUNK_ERROR:{msg}\n")
        except Exception:
            pass
        eprint(f"  \033[31m✗ {msg}\033[0m")
        sys.exit(1)

    # Text on stdin (CONTRACT §2); --text is the escape hatch.
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        fail("empty chunk text")

    # Fast path: check if persistent Kokoro daemon is running
    sock_path = os.path.expanduser("~/.config/speak/kokoro.sock")
    if os.path.exists(sock_path):
        try:
            import socket, json
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(60.0)
            s.connect(sock_path)
            req = {
                "text": text.strip(),
                "realization": args.realization.strip(),
                "output": out,
                "language_code": args.language_code
            }
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            resp_raw = s.recv(4096).decode("utf-8")
            s.close()
            resp = json.loads(resp_raw)
            if resp.get("status") == "ok":
                if os.path.exists(err_path):
                    os.remove(err_path)
                sys.exit(0)
            else:
                eprint(f"  \033[33m[kokoro] daemon error: {resp.get('message')}, falling back to standalone...\033[0m")
        except Exception:
            pass

    # Resolve model file locations (auto-download on first use).
    model_dir = os.path.expanduser("~/.config/speak/models")
    try:
        os.makedirs(model_dir, exist_ok=True)
    except Exception as e:
        fail(f"cannot create model dir {model_dir}: {e}")

    model_path = os.path.join(model_dir, "kokoro-v1.0.onnx")
    voices_path = os.path.join(model_dir, "voices-v1.0.bin")
    base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
    try:
        if not os.path.exists(model_path):
            download_file(base + "kokoro-v1.0.onnx", model_path, "Kokoro ONNX model")
        if not os.path.exists(voices_path):
            download_file(base + "voices-v1.0.bin", voices_path, "Kokoro voices pack")
    except Exception as e:
        fail(str(e))

    # Init Kokoro.
    try:
        from kokoro_onnx import Kokoro
        kokoro = Kokoro(model_path, voices_path)
        patch_kokoro_phoneme_split(kokoro)
    except ImportError:
        fail("kokoro_onnx not installed (pip3 install kokoro-onnx)")
    except Exception as e:
        eprint("  \033[90mThis is commonly missing espeak-ng on macOS: brew install espeak-ng\033[0m")
        fail(f"Kokoro init failed: {e}")

    # Build the voice argument: single id passes through; a blend becomes an
    # auto-normalized weighted sum of style vectors (N-voice, generalizing the old
    # hardcoded 0.5/0.5 adam-michael case).
    try:
        components = parse_realization(args.realization)
    except Exception as e:
        fail(f"bad realization {args.realization!r}: {e}")

    try:
        if len(components) == 1 and components[0][1] == 1.0:
            voice_arg = components[0][0]
        else:
            total = sum(w for _, w in components)
            if total <= 0:
                fail(f"blend weights sum to {total}; must be > 0")
            blended = None
            for voice, weight in components:
                style = kokoro.get_voice_style(voice)
                term = (weight / total) * style       # auto-normalize
                blended = term if blended is None else blended + term
            voice_arg = blended
    except SystemExit:
        raise
    except Exception as e:
        fail(f"voice/blend resolution failed: {e}")

    # Kokoro expects a lang code like "en-us"/"en-gb"; the driver supplies it.
    lang = (args.language_code or "en-us").lower()

    # Synthesize with per-clause timing and Markdown cleaning.
    try:
        samples, sample_rate, timing_meta = synthesize_with_timings(
            kokoro, text, voice_arg=voice_arg, speed=1.0, lang=lang
        )
    except Exception as e:
        fail(f"synthesis failed: {e}")

    # Write WAV (CONTRACT §3: a playable WAV, never raw PCM).
    try:
        import soundfile as sf
        sf.write(out, samples, sample_rate)
    except ImportError:
        fail("soundfile not installed (pip3 install soundfile)")
    except Exception as e:
        fail(f"writing WAV failed: {e}")

    # Success sidecar + per-clause timing sidecar + clear any stale .err
    try:
        with open(tokens_path, "w") as f:
            f.write("local")
        with open(out + ".timing.json", "w") as f:
            json.dump(timing_meta, f)
        if os.path.exists(err_path):
            os.remove(err_path)
    except Exception:
        pass  # the WAV is written; sidecar issues are non-fatal

    sys.exit(0)


if __name__ == "__main__":
    main()
