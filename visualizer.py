#!/usr/bin/env python3
"""
visualizer.py — In-terminal audio visualizer and teleprompter for speak.
Calculates acoustic RMS amplitude from WAV samples and renders an in-place HUD with:
- Breathing room (spacing above and below captions)
- Multi-line scrolling lyrics (1-8 lines, default: 8)
- Weighted phonetic timing: line illuminates precisely as its first word begins
- Multi-level focal gradient shading (active line brightest, fading gracefully in both directions)
- Beacon hysteresis & VU smoothing (no rapid flickering between words)
- Zero scrollback buffer leak (static borders, in-place line updates)
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
BRIGHT_CYAN = "\033[1;96m"
YELLOW = "\033[33m"
BRIGHT_YELLOW = "\033[1;33m"
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

def estimate_syllables(w):
    clean = re.sub(r"[^a-zA-Z]", "", w).lower()
    if not clean:
        return 1
    if len(clean) <= 3:
        return 1
    vowels = re.findall(r"[aeiouy]+", clean)
    count = len(vowels)
    if clean.endswith("e") and not clean.endswith(("le", "ee")):
        count = max(1, count - 1)
    if clean.endswith("ed") and not clean.endswith(("ted", "ded")):
        count = max(1, count - 1)
    return max(1, count)

def compute_line_timings(words, line_groups, total_duration, speech_start=0.0):
    """
    Computes [start_time, end_time] for each line using per-word syllable
    estimation and punctuation pause anchors.
    Ensures zero cumulative drift against the audio playback.
    """
    if not words or not line_groups:
        return []

    word_weights = []
    pause_weights = []
    for w in words:
        syl = estimate_syllables(w)
        word_weights.append(syl * 1.0)
        p_weight = 0.0
        if w.endswith((".", "!", "?")):
            p_weight = 3.5  # ~600-800ms sentence boundary pause in TTS
        elif w.endswith((",", ";", ":", "—", "-")):
            p_weight = 1.8  # ~300-400ms clause boundary pause in TTS
        pause_weights.append(p_weight)

    total_w = sum(word_weights) + sum(pause_weights)
    available_dur = max(0.1, total_duration - speech_start)
    sec_per_weight = available_dur / total_w if total_w > 0 else 0.0

    word_timings = []
    curr_t = speech_start
    for i in range(len(words)):
        w_dur = word_weights[i] * sec_per_weight
        p_dur = pause_weights[i] * sec_per_weight
        word_timings.append((curr_t, curr_t + w_dur))
        curr_t += w_dur + p_dur

    line_timings = []
    w_idx = 0
    for line in line_groups:
        st = word_timings[w_idx][0] if w_idx < len(word_timings) else total_duration
        w_idx += len(line)
        et = word_timings[w_idx][0] if w_idx < len(word_timings) else total_duration
        line_timings.append((st, et))

    return line_timings

def get_scrolling_caption_lines(words, line_timings, line_groups, elapsed, num_lines=8, highlight_mode="line", lead_ms=65):
    """
    Renders num_lines of closed captioning text in a pure monochromatic single-hue palette.
    Only shades of cyan are used, strictly brighter closer to the spoken line.
    """
    if not words or not line_groups or not line_timings:
        return [""] * num_lines

    # Apply anticipation lead so color changes right before first word begins
    lead_sec = max(0.0, lead_ms / 1000.0)
    check_time = elapsed + lead_sec

    # Find active line by timing
    active_line_idx = len(line_timings) - 1
    for idx, (t_start, t_end) in enumerate(line_timings):
        if check_time < t_end:
            active_line_idx = idx
            break

    # Center active_line_idx in the num_lines window
    half = num_lines // 2
    start_line_idx = active_line_idx - half

    rendered_lines = []
    for slot in range(num_lines):
        target_idx = start_line_idx + slot
        if target_idx < 0 or target_idx >= len(line_groups):
            rendered_lines.append("")
        else:
            line_words = line_groups[target_idx]
            dist = abs(target_idx - active_line_idx)
            text_raw = " ".join(line_words)

            if dist == 0:
                # SPOKEN LINE: Brightest (Bold Electric Cyan)
                if highlight_mode == "word":
                    t_start, t_end = line_timings[target_idx]
                    line_dur = max(0.1, t_end - t_start)
                    line_ratio = min(1.0, max(0.0, (elapsed - t_start) / line_dur))
                    active_w_idx = min(len(line_words) - 1, int(line_ratio * len(line_words)))
                    styled = []
                    for w_i, w in enumerate(line_words):
                        if w_i < active_w_idx:
                            styled.append(f"\033[38;2;0;215;225m{w}{RESET}")
                        elif w_i == active_w_idx:
                            styled.append(f"\033[1;4;38;2;0;255;255m{w}{RESET}")
                        else:
                            styled.append(f"\033[38;2;0;170;185m{w}{RESET}")
                    rendered_lines.append(" ".join(styled))
                else:
                    rendered_lines.append(f"\033[1;38;2;0;255;255m{text_raw}{RESET}")
            elif dist == 1:
                # 1 line away: Bright Cyan (~84% luminance)
                rendered_lines.append(f"\033[38;2;0;215;225m{text_raw}{RESET}")
            elif dist == 2:
                # 2 lines away: Medium Cyan (~67% luminance)
                rendered_lines.append(f"\033[38;2;0;170;185m{text_raw}{RESET}")
            elif dist == 3:
                # 3 lines away: Soft Cyan (~51% luminance)
                rendered_lines.append(f"\033[38;2;0;130;145m{text_raw}{RESET}")
            else:
                # 4+ lines away: Dim Muted Cyan (~37% luminance, clearly readable)
                rendered_lines.append(f"\033[38;2;0;95;110m{text_raw}{RESET}")

    return rendered_lines

def play_with_hud(wav_path, voice_name="speak", transcript="", num_lines=8, highlight_mode="line", lead_ms=65, mock_audio=False):
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

    # Detect acoustic speech onset from audio samples
    speech_start = 0.0
    for idx, val in enumerate(rms_values):
        if val > 0.04 * max_rms:
            speech_start = idx / fps
            break

    # Pre-calculate line groups and per-word phonetic timings
    line_groups = wrap_text_into_lines(words, max_chars=caption_char_limit) if words else []
    line_timings = compute_line_timings(words, line_groups, duration, speech_start=speech_start) if line_groups else []

    # Clean voice display name
    clean_voice = voice_name
    if clean_voice.lower().startswith("af_") or clean_voice.lower().startswith("am_"):
        clean_voice = clean_voice[3:]

    # Set terminal tab title and hide cursor
    sys.stderr.write(f"\033]0;🔊 VOICE: {clean_voice.upper()}\007")
    sys.stderr.write("\033[?25l")

    # TOP BORDER — PRINTED EXACTLY ONCE
    badge = f" 🔊 VOICE : {clean_voice.upper()} "
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

    # State variables for hysteresis & smoothing
    last_talk_time = 0.0
    smoothed_rms = 0.0
    HOLD_TIME = 0.35  # 350ms hold window to prevent flickering between words

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration or (player and player.poll() is not None):
                break

            ratio = min(1.0, elapsed / duration) if duration > 0 else 0
            chunk_idx = int(elapsed * fps)
            cur_rms = rms_values[chunk_idx] if chunk_idx < len(rms_values) else 0.0
            norm_rms = min(1.0, cur_rms / (max_rms * 0.85 + 1e-5))

            # VU meter smoothing (smooth peak decay)
            smoothed_rms = max(norm_rms, smoothed_rms * 0.72)

            # Beacon state with 350ms hold hysteresis
            if norm_rms > 0.45:
                beacon = f"{BRIGHT_GREEN}● ACTIVE{RESET}"
                last_talk_time = elapsed
            elif norm_rms > 0.15:
                beacon = f"{GREEN}● TALK  {RESET}"
                last_talk_time = elapsed
            else:
                # Inter-word silence: hold TALK state if within hold window
                if elapsed - last_talk_time < HOLD_TIME:
                    beacon = f"{GREEN}● TALK  {RESET}"
                else:
                    beacon = f"{DIM}○ IDLE  {RESET}"

            frame_lines = []

            # 1. Padding space ABOVE closed captioning
            pad_top = f"{CYAN}│{RESET}{' ' * inner_w}{CYAN}│{RESET}"
            frame_lines.append(pad_top)

            # 2. Scrolling closed captioning lines (with focal gradient shading)
            caption_lines = get_scrolling_caption_lines(
                words, line_timings, line_groups, elapsed, num_lines=num_lines, highlight_mode=highlight_mode, lead_ms=lead_ms
            )
            for c_line in caption_lines:
                content = f"{CYAN}│{RESET}  {c_line}"
                frame_lines.append(pad_to(content, box_w - 1) + f"{CYAN}│{RESET}")

            # 3. Padding space BELOW closed captioning
            pad_bot = f"{CYAN}│{RESET}{' ' * inner_w}{CYAN}│{RESET}"
            frame_lines.append(pad_bot)

            # 4. Telemetry Line (Beacon + Smoothed VU Meter + Progress Track + Time)
            meter_len = 10
            filled = int(smoothed_rms * meter_len)
            if smoothed_rms > 0.8:
                meter_col = BRIGHT_YELLOW
            elif smoothed_rms > 0.3:
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
            words, line_timings, line_groups, duration, num_lines=num_lines, highlight_mode=highlight_mode
        )
        for c_line in fin_caption_lines:
            content = f"{GREEN}│{RESET}  {c_line}"
            fin_lines.append(pad_to(content, box_w - 1) + f"{GREEN}│{RESET}")

        fin_lines.append(f"{GREEN}│{RESET}{' ' * inner_w}{GREEN}│{RESET}")

        fin_telem = f"{GREEN}│{RESET}  {GREEN}● DONE{RESET}    PLAYBACK FINISHED ({time_str})"
        fin_lines.append(pad_to(fin_telem, box_w - 1) + f"{GREEN}│{RESET}")

        bot_tag = f"[{GREEN}✓ DONE{RESET}{GREEN}]──╯{RESET}\n"
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
        print("Usage: visualizer.py <audio.wav> [voice_name] [transcript] [-l|--lines 1-8] [--highlight line|word] [--mock]", file=sys.stderr)
        sys.exit(1)

    wav = sys.argv[1]
    name = "droid"
    transcript_arg = ""
    lines_val = 8
    hl_mode = "line"
    lead_val = 65
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
                lines_val = max(1, min(8, int(sys.argv[idx + 1])))
            except ValueError:
                lines_val = 8
            skip_next = True
        elif arg.startswith("--lines="):
            try:
                lines_val = max(1, min(8, int(arg.split("=", 1)[1])))
            except ValueError:
                lines_val = 8
        elif arg in ("-H", "--highlight") and idx + 1 < len(sys.argv):
            hl_mode = sys.argv[idx + 1].lower()
            if hl_mode not in ("line", "word"):
                hl_mode = "line"
            skip_next = True
        elif arg.startswith("--highlight="):
            hl_mode = arg.split("=", 1)[1].lower()
            if hl_mode not in ("line", "word"):
                hl_mode = "line"
        elif arg == "--lead" and idx + 1 < len(sys.argv):
            try:
                lead_val = max(0, int(sys.argv[idx + 1]))
            except ValueError:
                lead_val = 65
            skip_next = True
        elif arg.startswith("--lead="):
            try:
                lead_val = max(0, int(arg.split("=", 1)[1]))
            except ValueError:
                lead_val = 65
        elif arg.startswith("--"):
            continue
        elif name == "droid" and not transcript_arg:
            name = arg
        elif not transcript_arg:
            transcript_arg = arg

    sys.exit(play_with_hud(wav, name, transcript=transcript_arg, num_lines=lines_val, highlight_mode=hl_mode, lead_ms=lead_val, mock_audio=mock))
