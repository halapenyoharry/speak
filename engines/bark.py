#!/usr/bin/env python3
"""
engines/bark.py — Suno Bark neural audio generator adapter for speak.

Conforms to engines/CONTRACT.md.
Invoked by the driver as:
    engines/bark.py --realization <R> --output <WAV> [--model <M>] [--language-code <LC>] (text on stdin)

Bark generates human speech with natural prosody and supports audio tags:
[laughs], [sighs], [whispers], [gasp], [clears throat], [music], etc.

Sidecars:
  <output>.tokens = "local" (written on success)
  <output>.err    = "CHUNK_ERROR:<msg>" (written on failure)
"""

import os
import sys
import shutil
import argparse
import warnings
import numpy as np

warnings.filterwarnings("ignore")
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"


def eprint(*a, **k):
    """Diagnostics to stderr only — stdout is the driver's reserved channel."""
    print(*a, file=sys.stderr, **k)


def get_device():
    """Detect optimal device: MPS (Apple Silicon GPU), CUDA, or CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def check_disk_space(min_gb_required=4.0):
    """Ensure at least min_gb_required GB free before downloading/generating."""
    try:
        stat = shutil.disk_usage(os.path.expanduser("~"))
        free_gb = stat.free / (1024 ** 3)
        if free_gb < min_gb_required:
            raise RuntimeError(
                f"Low disk space ({free_gb:.1f} GB available). "
                f"Bark requires at least {min_gb_required:.1f} GB free disk space."
            )
    except Exception as e:
        if "Low disk space" in str(e):
            raise


def main():
    p = argparse.ArgumentParser(description="Suno Bark TTS adapter (speak)")
    p.add_argument("--realization", required=True,
                   help="Bark speaker preset, e.g. 'v2/en_speaker_6' or path to custom .npz")
    p.add_argument("--output", required=True, help="output WAV path")
    p.add_argument("--language-code", dest="language_code", default="en-US",
                   help="locale (defaults to en-US)")
    p.add_argument("--model", default="suno/bark-small",
                   help="model checkpoint: 'suno/bark-small' (default) or 'suno/bark'")
    p.add_argument("--text", default=None,
                   help="escape hatch; otherwise text is read from stdin")
    args = p.parse_args()

    out = args.output
    err_path = out + ".err"
    tokens_path = out + ".tokens"

    def fail(msg):
        try:
            with open(err_path, "w") as f:
                f.write(f"CHUNK_ERROR:{msg}\n")
            if os.path.exists(out):
                os.remove(out)
            if os.path.exists(tokens_path):
                os.remove(tokens_path)
        except Exception:
            pass
        eprint(f"  \033[31m✗ {msg}\033[0m")
        sys.exit(1)

    # Text on stdin (CONTRACT §2); --text is the escape hatch
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        fail("empty chunk text")

    # Map model shortcut alias if provided
    model_id = args.model
    if model_id in ("default", "small"):
        model_id = "suno/bark-small"
    elif model_id in ("large", "full"):
        model_id = "suno/bark"

    # Fast path: check if persistent Bark daemon is running
    sock_path = os.path.expanduser("~/.config/speak/bark.sock")
    if os.path.exists(sock_path):
        try:
            import socket, json
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(120.0)
            s.connect(sock_path)
            req = {
                "text": text.strip(),
                "realization": args.realization.strip(),
                "output": out,
                "model": model_id,
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
                eprint(f"  \033[33m[bark] daemon error: {resp.get('message')}, falling back to standalone...\033[0m")
        except Exception:
            # Daemon not running or stale socket; fall back to standalone
            pass

    # Pre-flight disk check
    try:
        check_disk_space(min_gb_required=4.0)
    except Exception as e:
        fail(str(e))

    device = get_device()
    eprint(f"  \033[90m[bark] loading {model_id} on {device}...\033[0m")

    # Import dependencies
    try:
        import torch
        import transformers
        from transformers import AutoProcessor, BarkModel
        transformers.logging.set_verbosity_error()
        try:
            from huggingface_hub.utils import logging as hf_logging
            hf_logging.set_verbosity_error()
        except Exception:
            pass
        import soundfile as sf
    except ImportError as e:
        fail(f"missing python dependencies: {e}. Run: pip3 install transformers torch soundfile scipy")

    # Load processor and model
    try:
        processor = AutoProcessor.from_pretrained(model_id)

        if device == "cuda":
            model = BarkModel.from_pretrained(model_id, torch_dtype=torch.float16).to(device)
        elif device == "mps":
            model = BarkModel.from_pretrained(model_id).to(device)
        else:
            model = BarkModel.from_pretrained(model_id).to(device)
        model.eval()
    except Exception as e:
        fail(f"failed to load Bark model '{model_id}': {e}")

    # Prepare inputs
    try:
        voice_preset = args.realization.strip()
        inputs = processor(text.strip(), voice_preset=voice_preset)
        inputs = {k: v.to(device) for k, v in inputs.items()}
    except Exception as e:
        fail(f"failed to tokenize text for voice preset '{args.realization}': {e}")

    # Generate speech
    eprint(f"  \033[90m[bark] generating speech...\033[0m")
    try:
        with torch.no_grad():
            audio_array = model.generate(**inputs)
            audio_array = audio_array.squeeze().cpu().numpy()
    except Exception as e:
        fail(f"Bark audio generation failed: {e}")

    # Normalize audio array
    if audio_array.dtype != np.float32:
        audio_array = audio_array.astype(np.float32)

    # Sample rate is fixed at 24000 Hz for Bark
    sample_rate = model.generation_config.sample_rate if hasattr(model, "generation_config") else 24000

    # Write output WAV file
    try:
        sf.write(out, audio_array, sample_rate, subtype="PCM_16")
    except Exception as e:
        fail(f"failed to write WAV to {out}: {e}")

    # Write .tokens sidecar (CONTRACT §4.1)
    try:
        with open(tokens_path, "w") as f:
            f.write("local\n")
    except Exception as e:
        fail(f"failed to write tokens sidecar: {e}")

    # Success: ensure no .err file remains
    if os.path.exists(err_path):
        os.remove(err_path)

    sys.exit(0)


if __name__ == "__main__":
    main()
