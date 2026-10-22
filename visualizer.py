#!/usr/bin/env python3
"""
visualizer.py — Video-game HUD audio visualizer for speak.
Calculates acoustic RMS amplitude from WAV samples and renders a tactical
sci-fi HUD with breathing room (spacing above and below captions),
multi-line scrolling lyrics (1-5 lines, default 5) with highlighting by line (default) or word,
vocal rhythm beacon, signal VU meter, and progress track.
Zero scrollback buffer leak (static borders, in-place line updates).
"""
import sys
import os
import re
import time
import math
import wave
import struct
import signal
import shutil
import subprocess
import unicodedata

# ANSI Color Codes
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
BRIGHT_GREEN = "\033[1;32m"
CYAN = "\033[36m"
BRIGHT_CYAN = "\033[1;36m"
YELLOW = "\033[33m"
BRIGHT_YELLOW = "\033[1;33m"
GRAY = "\033[90m"
WHITE_ON_CYAN = "\033[1;46;30m"
WHITE_ON_GREEN = "\033[1;42;30m"

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def visual_width(s):
    clean = ANSI_RE.sub("", s)
    w = 0
    for c in clean:
        if unicodedata.east_asian_width(c) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w

def pad_to(content, target_w):
    curr = visual_width(content)
    pad = max(0, target_w - curr)
    return content + (" " * pad)

def wrap_text_into_lines(words, max_chars=54):
    """
    Greedy wrap of words into lines with a maximum visual character width.
    """
    lines = []
    curr_line = []
    curr_len = 0
    for w in words:
        w_len = len(w)
        if curr_line and (curr_len + 1 + w_len > max_chars):
            lines.append(curr_line)
            curr_line = [w]
            curr_len = w_len
        else:
            curr_line.append(w)
            curr_len += (w_len + 1 if curr_line else w_len)
    if curr_line:
        lines.append(curr_line)
    return lines

def get_scrolling_caption_lines(words, ratio, max_chars=54, num_lines=5, highlight_mode="line"):
    """
    Renders num_lines of closed captioning text centered on the active spoken line:
    - Lines in past: soft dimmed green
    - Active line:
        - If highlight_mode == "line": entire line illuminated in bold bright cyan
        - If highlight_mode == "word": word-by-word karaoke style (past words green, active word bold cyan underlined, future words gray)
    - Lines in future: dim gray
    """
    if not words:
        return [""] * num_lines

    total_words = len(words)
    curr_word_idx = min(total_words - 1, max(0, int(ratio * total_words)))

    line_groups = wrap_text_into_lines(words, max_chars)
    if not line_groups:
        return [""] * num_lines

    # Locate which line contains curr_word_idx
    word_counter = 0
    active_line_idx = 0
    active_word_in_line = 0
    for l_idx, line in enumerate(line_groups):
        line_word_count = len(line)
        if word_counter + line_word_count > curr_word_idx:
            active_line_idx = l_idx
            active_word_in_line = curr_word_idx - word_counter
            break
        word_counter += line_word_count
        if l_idx == len(line_groups) - 1:
            active_line_idx = l_idx
            active_word_in_line = line_word_count - 1

    # Center active_line_idx in the window
    half = num_lines // 2
    start_line_idx = active_line_idx - half

    rendered_lines = []
    for slot in range(num_lines):
        target_idx = start_line_idx + slot
        if target_idx < 0 or target_idx >= len(line_groups):
            rendered_lines.append("")
        else:
            line_words = line_groups[target_idx]
            if target_idx < active_line_idx:
                # Past line: soft dimmed green
                rendered_lines.append(f"{GREEN}{DIM}" + " ".join(line_words) + f"{RESET}")
            elif target_idx > active_line_idx:
                # Future line: dim gray
                rendered_lines.append(f"{GRAY}" + " ".join(line_words) + f"{RESET}")
            else:
                # Active line
                if highlight_mode == "line":
                    # Full line illumination (like standard Apple Music lyrics)
                    rendered_lines.append(f"{BRIGHT_CYAN}{BOLD}" + " ".join(line_words) + f"{RESET}")
                else:
                    # Word-by-word karaoke highlighting
                    styled_tokens = []
                    for w_i, w in enumerate(line_words):
                        if w_i < active_word_in_line:
                            styled_tokens.append(f"{GREEN}{DIM}{w}{RESET}")
                        elif w_i == active_word_in_line:
                            styled_tokens.append(f"{BRIGHT_CYAN}{BOLD}\033[4m{w}{RESET}")
                        else:
                            styled_tokens.append(f"{GRAY}{w}{RESET}")
                    rendered_lines.append(" ".join(styled_tokens))

    return rendered_lines

def play_with_hud(wav_path, voice_name="speak", transcript="", num_lines=5, highlight_mode="line", mock_audio=False):
    if not os.path.exists(wav_path):
        print(f"File not found: {wav_path}", file=sys.stderr)
        return 1

    try:
        with wave.open(wav_path, "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            duration = nframes / framerate
            raw_bytes = wf.readframes(nframes)
    except Exception as e:
        print(f"Error reading audio file: {e}", file=sys.stderr)
        return 1

    fmt = f"<{nframes * nchannels}h"
    try:
        samples = struct.unpack(fmt, raw_bytes)
    except Exception as e:
        if not mock_audio:
            subprocess.run(["afplay", wav_path])
        return 0

    if nchannels == 2:
        samples = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples), 2)]

    fps = 20
    chunk_samples = int(framerate / fps)
    total_chunks = int(duration * fps) + 1

    rms_values = []
    max_rms = 1.0
    for i in range(total_chunks):
        start = i * chunk_samples
        end = min(start + chunk_samples, len(samples))
        if start >= len(samples):
            rms = 0.0
        else:
            s_data = samples[start:end]
            if s_data:
                mean_sq = sum(s * s for s in s_data) / len(s_data)
                rms = math.sqrt(mean_sq)
            else:
                rms = 0.0
        rms_values.append(rms)
        if rms > max_rms:
            max_rms = rms

    words = transcript.strip().split() if transcript else []

    player = None
    if not mock_audio:
        if subprocess.run(["which", "afplay"], capture_output=True).returncode == 0:
            player = subprocess.Popen(["afplay", wav_path])
        elif subprocess.run(["which", "paplay"], capture_output=True).returncode == 0:
            player = subprocess.Popen(["paplay", wav_path])
        elif subprocess.run(["which", "aplay"], capture_output=True).returncode == 0:
            player = subprocess.Popen(["aplay", "-q", wav_path])
        elif subprocess.run(["which", "mpv"], capture_output=True).returncode == 0:
            player = subprocess.Popen(["mpv", "--no-video", "--really-quiet", wav_path])

    start_time = time.monotonic()

    # Determine safe box width based on terminal size
    term_cols = shutil.get_terminal_size((80, 24)).columns
    box_w = min(68, max(52, term_cols - 4))
    inner_w = box_w - 2
    caption_char_limit = inner_w - 4

    # Clean voice display name
    clean_voice = voice_name
    if clean_voice.lower().startswith("af_") or clean_voice.lower().startswith("am_"):
        clean_voice = clean_voice[3:]

    # Set terminal tab title and hide cursor
    sys.stderr.write(f"\033]0;🔊 COMMS: {clean_voice.upper()}\007")
    sys.stderr.write("\033[?25l")

    # TOP BORDER — PRINTED EXACTLY ONCE
    badge = f" 🔊 COMMS : {clean_voice.upper()} "
    top_left = f"{CYAN}╭──[{WHITE_ON_CYAN}{badge}{RESET}{CYAN}]"
    top_dashes = max(2, box_w - visual_width(top_left) - 1)
    top_line = top_left + ("─" * top_dashes) + f"╮{RESET}\n"
    sys.stderr.write(top_line)
    sys.stderr.flush()

    # Total in-place refresh lines:
    # 1 (pad top) + num_lines (captions) + 1 (pad bot) + 1 (telemetry)
    total_refresh_lines = 1 + num_lines + 1 + 1

    def cleanup_signal(signum, frame):
        if player and player.poll() is None:
            player.terminate()
        sys.stderr.write(f"\n\033[{total_refresh_lines}B\033[?25h\033]0;\007\n")
        sys.stderr.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup_signal)
    signal.signal(signal.SIGTERM, cleanup_signal)

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration or (player and player.poll() is not None):
                break

            ratio = min(1.0, elapsed / duration) if duration > 0 else 0
            chunk_idx = int(elapsed * fps)
            cur_rms = rms_values[chunk_idx] if chunk_idx < len(rms_values) else 0.0
            norm_rms = min(1.0, cur_rms / (max_rms * 0.85 + 1e-5))

            frame_lines = []

            # 1. Padding space ABOVE closed captioning
            pad_top = f"{CYAN}│{RESET}{' ' * inner_w}{CYAN}│{RESET}"
            frame_lines.append(pad_top)

            # 2. Scrolling closed captioning lines (1-5 lines)
            caption_lines = get_scrolling_caption_lines(
                words, ratio, max_chars=caption_char_limit, num_lines=num_lines, highlight_mode=highlight_mode
            )
            for c_line in caption_lines:
                content = f"{CYAN}│{RESET}  {c_line}"
                frame_lines.append(pad_to(content, box_w - 1) + f"{CYAN}│{RESET}")

            # 3. Padding space BELOW closed captioning
            pad_bot = f"{CYAN}│{RESET}{' ' * inner_w}{CYAN}│{RESET}"
            frame_lines.append(pad_bot)

            # 4. Telemetry Line (Vocal Beacon + VU Meter + Progress Track + Time)
            if norm_rms > 0.45:
                beacon = f"{BRIGHT_GREEN}● ACTIVE{RESET}"
            elif norm_rms > 0.15:
                beacon = f"{GREEN}● TALK  {RESET}"
            else:
                beacon = f"{DIM}○ IDLE  {RESET}"

            meter_len = 10
            filled = int(norm_rms * meter_len)
            if norm_rms > 0.8:
                meter_col = BRIGHT_YELLOW
            elif norm_rms > 0.3:
                meter_col = BRIGHT_GREEN
            else:
                meter_col = GREEN
            meter_bar = meter_col + ("█" * filled) + DIM + ("░" * (meter_len - filled)) + RESET

            track_len = 14
            p_filled = int(ratio * track_len)
            track = (
                BRIGHT_CYAN
                + ("━" * p_filled)
                + ("╸" if p_filled < track_len else "")
                + DIM
                + ("─" * max(0, track_len - p_filled - 1))
                + RESET
            )

            cur_m, cur_s = divmod(int(elapsed), 60)
            tot_m, tot_s = divmod(int(duration), 60)
            pct = int(ratio * 100)
            time_str = f"{cur_m:02d}:{cur_s:02d}/{tot_m:02d}:{tot_s:02d}"

            telem_content = f"{CYAN}│{RESET}  {beacon} [{meter_bar}]  [{track}] {pct:2d}% {time_str}"
            frame_lines.append(pad_to(telem_content, box_w - 1) + f"{CYAN}│{RESET}")

            # Write all lines in place (NO trailing newline after last line!)
            output_str = "\n".join([f"\r\033[K{line}" for line in frame_lines])
            sys.stderr.write(output_str)
            # Move cursor back up to Line 1 (pad_top). Zero terminal scrolling!
            sys.stderr.write(f"\033[{total_refresh_lines - 1}A\r")
            sys.stderr.flush()

            target_time = start_time + (chunk_idx + 1) / fps
            sleep_dur = target_time - time.monotonic()
            if sleep_dur > 0:
                time.sleep(sleep_dur)

        if player and player.poll() is None:
            player.wait()

        # FINAL COMPLETION FRAME
        tot_m, tot_s = divmod(int(duration), 60)
        time_str = f"{tot_m:02d}:{tot_s:02d}"

        fin_lines = []
        fin_lines.append(f"{GREEN}│{RESET}{' ' * inner_w}{GREEN}│{RESET}")

        # Show final lines in finished green state
        fin_caption_lines = get_scrolling_caption_lines(
            words, 1.0, max_chars=caption_char_limit, num_lines=num_lines, highlight_mode=highlight_mode
        )
        for c_line in fin_caption_lines:
            content = f"{GREEN}│{RESET}  {c_line}"
            fin_lines.append(pad_to(content, box_w - 1) + f"{GREEN}│{RESET}")

        fin_lines.append(f"{GREEN}│{RESET}{' ' * inner_w}{GREEN}│{RESET}")

        fin_telem = f"{GREEN}│{RESET}  {GREEN}● DONE{RESET}    STATUS: TRANSMISSION FINISHED ({time_str})"
        fin_lines.append(pad_to(fin_telem, box_w - 1) + f"{GREEN}│{RESET}")

        bot_tag = f"[{GREEN}✓ COMPLETE{RESET}{GREEN}]──╯{RESET}\n"
        bot_dashes = max(2, box_w - visual_width(bot_tag) - 1)
        comp_bot = f"{GREEN}╰" + ("─" * bot_dashes) + bot_tag

        output_fin = "\n".join([f"\r\033[K{line}" for line in fin_lines])
        sys.stderr.write(f"{output_fin}\n\r\033[K{comp_bot}")
        sys.stderr.flush()

    finally:
        # Restore terminal cursor and clear title
        sys.stderr.write("\033[?25h")
        sys.stderr.write("\033]0;\007")
        sys.stderr.flush()

    return 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: visualizer.py <audio.wav> [voice_name] [transcript] [-l|--lines 1-5] [--highlight line|word] [--mock]", file=sys.stderr)
        sys.exit(1)

    wav = sys.argv[1]
    name = "droid"
    transcript_arg = ""
    lines_val = 5
    hl_mode = "line"
    mock = "--mock" in sys.argv

    # Parse positional and flagged arguments
    skip_next = False
    for idx in range(2, len(sys.argv)):
        if skip_next:
            skip_next = False
            continue
        arg = sys.argv[idx]
        if arg in ("-l", "--lines") and idx + 1 < len(sys.argv):
            try:
                lines_val = max(1, min(5, int(sys.argv[idx + 1])))
            except ValueError:
                lines_val = 5
            skip_next = True
        elif arg.startswith("--lines="):
            try:
                lines_val = max(1, min(5, int(arg.split("=", 1)[1])))
            except ValueError:
                lines_val = 5
        elif arg in ("-H", "--highlight") and idx + 1 < len(sys.argv):
            hl_mode = sys.argv[idx + 1].lower()
            if hl_mode not in ("line", "word"):
                hl_mode = "line"
            skip_next = True
        elif arg.startswith("--highlight="):
            hl_mode = arg.split("=", 1)[1].lower()
            if hl_mode not in ("line", "word"):
                hl_mode = "line"
        elif arg.startswith("--"):
            continue
        elif name == "droid" and not transcript_arg:
            name = arg
        elif not transcript_arg:
            transcript_arg = arg

    sys.exit(play_with_hud(wav, name, transcript=transcript_arg, num_lines=lines_val, highlight_mode=hl_mode, mock_audio=mock))
