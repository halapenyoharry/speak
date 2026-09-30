#!/usr/bin/env python3
"""
engines/kokoro_daemon.py — Persistent resident model daemon for Kokoro TTS.

Keeps Kokoro ONNX model and voices binary warm in memory.
Listens on a Unix domain socket: ~/.config/speak/kokoro.sock
Writes PID to: ~/.config/speak/kokoro.pid

Protocol:
- Accepts connection over Unix domain socket.
- Receives JSON payload:
    {"text": "...", "realization": "...", "output": "...", "language_code": "..."}
  or ping:
    {"ping": true}
- Synthesizes speech using the warm in-memory model.
- Writes WAV to output and sidecar <output>.tokens = "local".
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
import argparse
import numpy as np


def eprint(*a, **k):
    print(*a, file=sys.stderr, **k)


def parse_realization(realization):
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


class KokoroDaemon:
    def __init__(self, sock_path=None, pid_path=None):
        config_dir = os.path.expanduser("~/.config/speak")
        os.makedirs(config_dir, exist_ok=True)
        self.sock_path = sock_path or os.path.join(config_dir, "kokoro.sock")
        self.pid_path = pid_path or os.path.join(config_dir, "kokoro.pid")
        self.running = False
        self.server_socket = None
        self.kokoro = None

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
            eprint(f"\n  \033[90m[kokoro daemon] caught signal {sig}, shutting down...\033[0m")
            self.running = False
            self.cleanup()
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def load_model(self):
        model_dir = os.path.expanduser("~/.config/speak/models")
        model_path = os.path.join(model_dir, "kokoro-v1.0.onnx")
        voices_path = os.path.join(model_dir, "voices-v1.0.bin")

        eprint(f"  \033[90m[kokoro daemon] pre-loading Kokoro ONNX into memory...\033[0m")
        try:
            from kokoro_onnx import Kokoro
            self.kokoro = Kokoro(model_path, voices_path)
            eprint(f"  \033[32m✓\033[0m \033[90m[kokoro daemon] model warm and resident in memory\033[0m")
        except Exception as e:
            eprint(f"  \033[31m✗ [kokoro daemon] failed to load model: {e}\033[0m")
            self.cleanup()
            sys.exit(1)

    def handle_request(self, client_sock):
        try:
            import soundfile as sf

            raw_data = client_sock.recv(65536)
            if not raw_data:
                return

            data = json.loads(raw_data.decode("utf-8"))

            if data.get("ping"):
                resp = {"status": "ok", "pong": True, "engine": "kokoro"}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            text = data.get("text", "").strip()
            realization = data.get("realization", "am_puck").strip()
            out_path = data.get("output", "")
            lang = (data.get("language_code") or "en-us").lower()

            if not text:
                client_sock.sendall(json.dumps({"status": "error", "message": "empty chunk text"}).encode("utf-8") + b"\n")
                return

            if not out_path:
                client_sock.sendall(json.dumps({"status": "error", "message": "missing output path"}).encode("utf-8") + b"\n")
                return

            # Voice / blend resolution
            components = parse_realization(realization)
            if len(components) == 1 and components[0][1] == 1.0:
                voice_arg = components[0][0]
            else:
                total = sum(w for _, w in components)
                if total <= 0:
                    client_sock.sendall(json.dumps({"status": "error", "message": f"blend weights sum to {total}"}).encode("utf-8") + b"\n")
                    return
                blended = None
                for voice, weight in components:
                    style = self.kokoro.get_voice_style(voice)
                    term = (weight / total) * style
                    blended = term if blended is None else blended + term
                voice_arg = blended

            # Synthesize
            samples, sample_rate = self.kokoro.create(text, voice=voice_arg, speed=1.0, lang=lang)

            # Write WAV
            sf.write(out_path, samples, sample_rate)

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
            eprint(f"  \033[31m✗ [kokoro daemon] error handling request: {e}\033[0m")
            try:
                resp = {"status": "error", "message": str(e)}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            except Exception:
                pass

    def run(self):
        self.cleanup()
        self.setup_signals()

        with open(self.pid_path, "w") as f:
            f.write(f"{os.getpid()}\n")

        self.load_model()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.sock_path)
        os.chmod(self.sock_path, 0o700)
        self.server_socket.listen(5)
        self.running = True

        eprint(f"  \033[32m✓\033[0m \033[90m[kokoro daemon] listening on {self.sock_path} (pid: {os.getpid()})\033[0m")

        while self.running:
            try:
                client_sock, _ = self.server_socket.accept()
                self.handle_request(client_sock)
                client_sock.close()
            except socket.error:
                break
            except Exception as e:
                eprint(f"  \033[33m[kokoro daemon] accept error: {e}\033[0m")

        self.cleanup()


def main():
    p = argparse.ArgumentParser(description="Kokoro Persistent Model Daemon")
    p.add_argument("--socket", default=None, help="Custom Unix domain socket path")
    args = p.parse_args()

    daemon = KokoroDaemon(sock_path=args.socket)
    daemon.run()


if __name__ == "__main__":
    main()
