#!/usr/bin/env python3
"""
engines/bark_daemon.py — Persistent resident model daemon for Suno Bark TTS.

Keeps BarkModel and AutoProcessor warm in Apple Silicon unified memory (MPS).
Listens on a Unix domain socket: ~/.config/speak/bark.sock
Writes PID to: ~/.config/speak/bark.pid

Protocol:
- Accepts connection over Unix domain socket.
- Receives JSON payload:
    {"text": "...", "realization": "...", "output": "...", "model": "...", "language_code": "..."}
  or ping:
    {"ping": true}
- Synthesizes speech using the warm in-memory model on MPS/CUDA/CPU.
- Writes 24kHz 16-bit PCM WAV to output and sidecar <output>.tokens = "local".
- Replies with JSON:
    {"status": "ok"}
  or
    {"status": "error", "message": "..."}
- Closes connection.
"""

import os
import sys
import json
import signal
import socket
import warnings
import argparse
import numpy as np

warnings.filterwarnings("ignore")
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"


def eprint(*a, **k):
    print(*a, file=sys.stderr, **k)


def get_device():
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class BarkDaemon:
    def __init__(self, model_id="suno/bark-small", sock_path=None, pid_path=None):
        self.model_id = model_id
        config_dir = os.path.expanduser("~/.config/speak")
        os.makedirs(config_dir, exist_ok=True)
        self.sock_path = sock_path or os.path.join(config_dir, "bark.sock")
        self.pid_path = pid_path or os.path.join(config_dir, "bark.pid")
        self.device = get_device()
        self.running = False
        self.server_socket = None

        self.processor = None
        self.model = None

    def cleanup(self):
        try:
            if self.server_socket:
                self.server_socket.close()
        except Exception:
            pass
        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except Exception:
                pass
        if os.path.exists(self.pid_path):
            try:
                os.remove(self.pid_path)
            except Exception:
                pass

    def setup_signals(self):
        def handler(sig, frame):
            eprint(f"\n  \033[90m[bark daemon] caught signal {sig}, shutting down...\033[0m")
            self.running = False
            self.cleanup()
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGHUP, handler)

    def load_model(self):
        eprint(f"  \033[90m[bark daemon] loading {self.model_id} on {self.device}...\033[0m")
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

            self.processor = AutoProcessor.from_pretrained(self.model_id)

            if self.device == "cuda":
                self.model = BarkModel.from_pretrained(self.model_id, torch_dtype=torch.float16).to(self.device)
            elif self.device == "mps":
                self.model = BarkModel.from_pretrained(self.model_id).to(self.device)
            else:
                self.model = BarkModel.from_pretrained(self.model_id).to(self.device)
            self.model.eval()
            eprint(f"  \033[32m✓\033[0m \033[90m[bark daemon] model warm and resident in memory\033[0m")
        except Exception as e:
            eprint(f"  \033[31m✗ [bark daemon] failed to load model: {e}\033[0m")
            self.cleanup()
            sys.exit(1)

    def handle_request(self, client_sock):
        try:
            import torch
            import soundfile as sf

            raw_data = client_sock.recv(65536)
            if not raw_data:
                return

            data = json.loads(raw_data.decode("utf-8"))

            if data.get("ping"):
                resp = {"status": "ok", "pong": True, "device": self.device, "model": self.model_id}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            text = data.get("text", "").strip()
            realization = data.get("realization", "v2/en_speaker_6").strip()
            out_path = data.get("output", "")
            req_model = data.get("model", self.model_id)

            if not text:
                client_sock.sendall(json.dumps({"status": "error", "message": "empty chunk text"}).encode("utf-8") + b"\n")
                return

            if not out_path:
                client_sock.sendall(json.dumps({"status": "error", "message": "missing output path"}).encode("utf-8") + b"\n")
                return

            # Tokenize
            inputs = self.processor(text, voice_preset=realization)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            # Generate
            with torch.no_grad():
                audio_array = self.model.generate(**inputs)
                audio_array = audio_array.squeeze().cpu().numpy()

            if audio_array.dtype != np.float32:
                audio_array = audio_array.astype(np.float32)

            sample_rate = self.model.generation_config.sample_rate if hasattr(self.model, "generation_config") else 24000

            # Write WAV file
            sf.write(out_path, audio_array, sample_rate, subtype="PCM_16")

            # Write tokens sidecar
            tokens_path = out_path + ".tokens"
            with open(tokens_path, "w") as f:
                f.write("local\n")

            # Clean up any leftover .err
            err_path = out_path + ".err"
            if os.path.exists(err_path):
                os.remove(err_path)

            resp = {"status": "ok"}
            client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
        except Exception as e:
            eprint(f"  \033[31m✗ [bark daemon] error handling request: {e}\033[0m")
            try:
                resp = {"status": "error", "message": str(e)}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            except Exception:
                pass

    def run(self):
        self.cleanup()
        self.setup_signals()

        # Write PID
        with open(self.pid_path, "w") as f:
            f.write(f"{os.getpid()}\n")

        self.load_model()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.sock_path)
        os.chmod(self.sock_path, 0o700)
        self.server_socket.listen(5)
        self.running = True

        eprint(f"  \033[32m✓\033[0m \033[90m[bark daemon] listening on {self.sock_path} (pid: {os.getpid()})\033[0m")

        while self.running:
            try:
                client_sock, _ = self.server_socket.accept()
                self.handle_request(client_sock)
                client_sock.close()
            except socket.error:
                break
            except Exception as e:
                eprint(f"  \033[33m[bark daemon] accept error: {e}\033[0m")

        self.cleanup()


def main():
    p = argparse.ArgumentParser(description="Bark Persistent Model Daemon")
    p.add_argument("--model", default="suno/bark-small", help="Bark checkpoint to keep warm")
    p.add_argument("--socket", default=None, help="Custom Unix domain socket path")
    args = p.parse_args()

    daemon = BarkDaemon(model_id=args.model, sock_path=args.socket)
    daemon.run()


if __name__ == "__main__":
    main()
