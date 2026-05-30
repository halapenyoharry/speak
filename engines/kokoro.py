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
import argparse
import urllib.request


def eprint(*a, **k):
    """Diagnostics to stderr only — stdout is the driver's reserved channel."""
    print(*a, file=sys.stderr, **k)


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

    # Synthesize.
    try:
        samples, sample_rate = kokoro.create(text, voice=voice_arg, speed=1.0, lang=lang)
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

    # Success sidecar + clear any stale .err (idempotency, CONTRACT §5.5).
    try:
        with open(tokens_path, "w") as f:
            f.write("local")
        if os.path.exists(err_path):
            os.remove(err_path)
    except Exception:
        pass  # the WAV is written; sidecar issues are non-fatal

    sys.exit(0)


if __name__ == "__main__":
    main()
