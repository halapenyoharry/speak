#!/usr/bin/env python3
"""
web_ui.py — Non-Invasive Web Front-End Sidecar for `speak`.

Core Rule: Zero Changes to Existing CLI.
Wraps `/Users/harold/Projects/speak/speak` via:
  1. GET  /api/schema  — Introspects `./speak --help` + read-only YAML registries (`voices.yaml`, `engines.yaml`, `~/.config/speak/config`).
  2. POST /api/run     — Executes `./speak` via `asyncio.create_subprocess_exec` (never `shell=True`) and streams live stdout/stderr over SSE (`text/event-stream`).
  3. GET  /api/outputs — Lists generated `.wav` artifacts with duration, sample rate, and companion metadata for in-browser playback and captioning.
"""

import asyncio
import codecs
import datetime
import json
import os
import re
import sys
import time
import wave
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
SPEAK_BIN = BASE_DIR / "speak"
VOICES_YAML = BASE_DIR / "voices.yaml"
ENGINES_YAML = BASE_DIR / "engines.yaml"
STATIC_DIR = BASE_DIR / "static"
OUTPUTS_DIR = BASE_DIR / "outputs"
USER_CONFIG = Path.home() / ".config" / "speak" / "config"

OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07")

ANSI_COLOR_MAP = {
    31: "#ff6b6b",  # Red
    32: "#51cf66",  # Green
    33: "#fcc419",  # Yellow
    34: "#5c7cfa",  # Blue
    35: "#cc5de8",  # Magenta
    36: "#00f5d4",  # Cyan
}

app = FastAPI(
    title="speak Web UI Sidecar",
    description="Non-invasive web front-end sidecar for the speak CLI",
    version="0.2.0",
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def read_user_config() -> Dict[str, str]:
    cfg: Dict[str, str] = {
        "engine": "kokoro",
        "persona": "puck",
        "emphasis": "bold",
        "daemon_prompt": "true",
    }
    if USER_CONFIG.exists():
        try:
            for line in USER_CONFIG.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                cfg[k.strip()] = v.strip()
        except Exception:
            pass
    return cfg


def load_yaml_readonly(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


async def parse_cli_help() -> Dict[str, Any]:
    """Run `./speak --help` via create_subprocess_exec and parse subcommands & options."""
    proc = await asyncio.create_subprocess_exec(
        str(SPEAK_BIN),
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    raw_help = (stdout_bytes + stderr_bytes).decode("utf-8", errors="replace")
    clean_help = strip_ansi(raw_help)

    subcommands: List[Dict[str, str]] = []
    options: List[Dict[str, Any]] = []
    audio_tags: List[str] = []

    section = ""
    for line in clean_help.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("USAGE", "OPTIONS", "EXAMPLES", "DAEMON (FAST RESPONSES)", "AUDIO TAGS"):
            section = stripped
            continue
        if stripped.startswith("DAEMON"):
            section = "DAEMON"
            continue
        if stripped.startswith("AUDIO TAGS"):
            section = "AUDIO TAGS"
            continue

        if section == "USAGE" and line.startswith("    speak"):
            parts = re.split(r"\s{2,}", stripped, maxsplit=1)
            cmd_sig = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            subcommands.append({"signature": cmd_sig, "description": desc})

        elif section == "OPTIONS" and line.startswith("    "):
            parts = re.split(r"\s{2,}", stripped, maxsplit=1)
            flag_spec = parts[0]
            desc = parts[1] if len(parts) > 1 else ""
            short_flag = None
            long_flag = None
            arg_name = None

            for tok in [t.strip() for t in flag_spec.split(",")]:
                tok_parts = tok.split(" ", 1)
                flag_part = tok_parts[0]
                if len(tok_parts) > 1:
                    arg_name = tok_parts[1].strip()
                if flag_part.startswith("--"):
                    long_flag = flag_part
                elif flag_part.startswith("-"):
                    short_flag = flag_part

            default_match = re.search(r"\(default:\s*([^)]+)\)", desc)
            default_val = default_match.group(1).strip() if default_match else None

            options.append(
                {
                    "short": short_flag,
                    "long": long_flag,
                    "arg": arg_name,
                    "description": desc,
                    "default": default_val,
                    "is_boolean": arg_name is None,
                }
            )

        elif section == "AUDIO TAGS":
            tags = re.findall(r"\[[^\]]+\]", stripped)
            audio_tags.extend(tags)

    return {
        "raw_help": clean_help,
        "subcommands": subcommands,
        "options": options,
        "audio_tags": audio_tags,
    }


@app.get("/api/schema")
async def get_schema() -> JSONResponse:
    """Introspect CLI `--help` and read-only YAML registries."""
    help_data = await parse_cli_help()
    voices_data = load_yaml_readonly(VOICES_YAML)
    engines_data = load_yaml_readonly(ENGINES_YAML)
    user_cfg = read_user_config()

    personas_list = []
    for pid, pinfo in (voices_data.get("personas") or {}).items():
        ansi_code = int(pinfo.get("color", 36))
        realizations = list((pinfo.get("realizations") or {}).keys())
        personas_list.append(
            {
                "id": pid,
                "name": pid.capitalize(),
                "intent": pinfo.get("intent", ""),
                "style": pinfo.get("style", ""),
                "glyph": pinfo.get("glyph", "◇"),
                "ansi_color": ansi_code,
                "hex_color": ANSI_COLOR_MAP.get(ansi_code, "#00f5d4"),
                "realizations": realizations,
                "realization_details": pinfo.get("realizations", {}),
            }
        )

    engines_list = []
    for eid, einfo in (engines_data.get("engines") or {}).items():
        engines_list.append(
            {
                "id": eid,
                "label": einfo.get("label", eid),
                "deployment": einfo.get("deployment", ""),
                "capabilities": einfo.get("capabilities", []),
                "models": list((einfo.get("models") or {}).keys()),
                "default_persona": einfo.get("default_persona", "puck"),
            }
        )

    return JSONResponse(
        {
            "cli_path": str(SPEAK_BIN),
            "user_config": user_cfg,
            "help": help_data,
            "engines": engines_list,
            "personas": personas_list,
            "highlights": ["word", "line"],
            "emphasis_styles": ["bold", "underline", "both", "inverse"],
        }
    )


def probe_wav_file(wav_path: Path) -> Dict[str, Any]:
    """Read WAV header metadata safely using standard library `wave`."""
    info: Dict[str, Any] = {
        "duration_seconds": 0.0,
        "sample_rate": 24000,
        "channels": 1,
    }
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            info["sample_rate"] = rate
            info["channels"] = wf.getnchannels()
            info["duration_seconds"] = round(frames / float(rate), 2) if rate > 0 else 0.0
    except Exception:
        pass
    return info


@app.get("/api/outputs")
async def list_outputs() -> JSONResponse:
    """List generated WAV files in `outputs/` and workspace root."""
    items: List[Dict[str, Any]] = []
    search_dirs = [OUTPUTS_DIR, BASE_DIR]

    seen_paths = set()
    for d in search_dirs:
        if not d.exists():
            continue
        for wav_file in d.glob("*.wav"):
            resolved = wav_file.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                stat = wav_file.stat()
            except OSError:
                continue

            wav_meta = probe_wav_file(wav_file)
            sidecar_json = wav_file.with_suffix(".json")
            meta_extra: Dict[str, Any] = {}
            if sidecar_json.exists():
                try:
                    meta_extra = json.loads(sidecar_json.read_text(encoding="utf-8"))
                except Exception:
                    pass

            rel_source = "outputs" if d == OUTPUTS_DIR else "root"
            url_path = f"/outputs/{wav_file.name}" if d == OUTPUTS_DIR else f"/root-outputs/{wav_file.name}"

            items.append(
                {
                    "filename": wav_file.name,
                    "source": rel_source,
                    "url": url_path,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "mtime": stat.st_mtime,
                    "duration_seconds": wav_meta["duration_seconds"],
                    "sample_rate": wav_meta["sample_rate"],
                    "voice": meta_extra.get("voice", ""),
                    "engine": meta_extra.get("engine", ""),
                    "transcript": meta_extra.get("transcript", ""),
                    "command": meta_extra.get("command", ""),
                }
            )

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return JSONResponse({"outputs": items})


@app.get("/outputs/{filename}")
async def serve_output_file(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = OUTPUTS_DIR / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(target, media_type="audio/wav", filename=safe_name)


@app.get("/root-outputs/{filename}")
async def serve_root_output_file(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = BASE_DIR / safe_name
    if not target.exists() or not target.is_file() or target.suffix.lower() != ".wav":
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(target, media_type="audio/wav", filename=safe_name)


@app.post("/api/run")
async def run_cli(request: Request) -> StreamingResponse:
    """
    Execute `./speak` via `asyncio.create_subprocess_exec` (never `shell=True`)
    and stream stdout/stderr in real-time over Server-Sent Events (`text/event-stream`).
    """
    body = await request.json()
    mode = body.get("mode", "narrate")

    args: List[str] = []
    output_wav_path: Optional[Path] = None
    transcript_text = str(body.get("text", "")).strip()
    voice = str(body.get("voice", "")).strip()
    engine = str(body.get("engine", "")).strip()

    if mode == "subcommand":
        # Direct subcommand execution: e.g., ["voices", "--all"], ["daemon", "status"], ["set"]
        raw_sub_args = body.get("subcommand_args", [])
        if isinstance(raw_sub_args, list):
            args = [str(a) for a in raw_sub_args if str(a).strip()]
    else:
        # Narration mode
        if engine:
            args.extend(["-e", engine])
        if voice:
            args.extend(["-v", voice])
        model = str(body.get("model", "")).strip()
        if model:
            args.extend(["-m", model])
        if body.get("raw"):
            args.append("-r")
        if body.get("clipboard"):
            args.append("-c")
        if body.get("quiet"):
            args.append("-q")
        if body.get("lines"):
            args.extend(["-l", str(int(body["lines"]))])
        if body.get("highlight"):
            args.extend(["--highlight", str(body["highlight"])])
        if body.get("lead") is not None and str(body.get("lead")) != "":
            args.extend(["--lead", str(int(body["lead"]))])

        playback_target = body.get("playback_target", "browser")
        if playback_target == "browser":
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            v_slug = re.sub(r"[^a-zA-Z0-9_-]", "", voice or "voice").lower()
            custom_name = str(body.get("output_filename", "")).strip()
            if custom_name:
                safe_custom = re.sub(r"[^a-zA-Z0-9_.-]", "_", Path(custom_name).name)
                if not safe_custom.endswith(".wav"):
                    safe_custom += ".wav"
                output_wav_path = OUTPUTS_DIR / safe_custom
            else:
                output_wav_path = OUTPUTS_DIR / f"speak_{ts}_{v_slug}.wav"
            args.extend(["-o", str(output_wav_path)])

        if transcript_text and not body.get("clipboard"):
            args.append(transcript_text)

    async def sse_stream() -> AsyncGenerator[str, None]:
        def format_sse(event: str, data: Dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        cmd_display = " ".join(["./speak"] + [repr(a) if " " in a else a for a in args])
        yield format_sse(
            "start",
            {
                "command": cmd_display,
                "args": args,
                "timestamp": time.time(),
            },
        )

        proc = await asyncio.create_subprocess_exec(
            str(SPEAK_BIN),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
        )

        queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()

        async def read_stream(stream: Optional[asyncio.StreamReader], stream_name: str) -> None:
            if stream is None:
                return
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            buf = ""
            while True:
                chunk = await stream.read(128)
                if not chunk:
                    buf += decoder.decode(b"", final=True)
                    if buf.strip():
                        await queue.put(
                            {
                                "stream": stream_name,
                                "line": strip_ansi(buf),
                                "raw": buf,
                                "is_cr": False,
                            }
                        )
                    break
                text_chunk = decoder.decode(chunk, final=False)
                buf += text_chunk
                while "\n" in buf or "\r" in buf:
                    idx_n = buf.find("\n")
                    idx_r = buf.find("\r")
                    if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
                        part = buf[:idx_n]
                        buf = buf[idx_n + 1 :]
                        cleaned = strip_ansi(part).strip()
                        if cleaned:
                            await queue.put(
                                {
                                    "stream": stream_name,
                                    "line": cleaned,
                                    "is_cr": False,
                                }
                            )
                    else:
                        part = buf[:idx_r]
                        buf = buf[idx_r + 1 :]
                        cleaned = strip_ansi(part).strip()
                        if cleaned:
                            await queue.put(
                                {
                                    "stream": stream_name,
                                    "line": cleaned,
                                    "is_cr": True,
                                }
                            )

        task_out = asyncio.create_task(read_stream(proc.stdout, "stdout"))
        task_err = asyncio.create_task(read_stream(proc.stderr, "stderr"))

        async def wait_completion() -> None:
            await asyncio.gather(task_out, task_err)
            await proc.wait()
            await queue.put(None)

        wait_task = asyncio.create_task(wait_completion())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield format_sse("output", item)
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
            raise
        finally:
            if not wait_task.done():
                wait_task.cancel()

        exit_code = proc.returncode if proc.returncode is not None else -1
        output_info = None

        if exit_code == 0 and output_wav_path and output_wav_path.exists():
            wav_meta = probe_wav_file(output_wav_path)
            sidecar_data = {
                "filename": output_wav_path.name,
                "voice": voice or read_user_config().get("persona", "puck"),
                "engine": engine or read_user_config().get("engine", "kokoro"),
                "transcript": transcript_text,
                "command": cmd_display,
                "duration_seconds": wav_meta["duration_seconds"],
                "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            try:
                output_wav_path.with_suffix(".json").write_text(
                    json.dumps(sidecar_data, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

            output_info = {
                "filename": output_wav_path.name,
                "url": f"/outputs/{output_wav_path.name}",
                "duration_seconds": wav_meta["duration_seconds"],
                "voice": sidecar_data["voice"],
                "engine": sidecar_data["engine"],
                "transcript": transcript_text,
            }

        yield format_sse(
            "done",
            {
                "exit_code": exit_code,
                "output": output_info,
            },
        )

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def serve_index() -> FileResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    return FileResponse(index_file, media_type="text/html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the non-invasive speak Web UI Sidecar")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=2420, help="Bind port (default: 2420)")
    args = parser.parse_args()

    print(f"  speak Web UI Sidecar listening on http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
