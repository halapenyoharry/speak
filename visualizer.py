#!/usr/bin/env python3
"""
visualizer.py — Borderless, in-terminal teleprompter and audio telemetry for speak.
Calculates acoustic RMS amplitude from WAV samples and renders a clean, borderless display with:
- Breathing room (vertical spacing above and below captions)
- Multi-line scrolling lyrics (1-8 lines, default: 3)
- Prosodic pause-aligned chunking: lines break at natural vocal breath points
- Word-anchored phonetic timing: syllable weighting + audio speech onset calibration
- Monochromatic single-hue cyan gradient (active line bold electric cyan, fading smoothly)
- Beacon hysteresis & VU smoothing (no rapid flickering between words)
- Borderless, adaptive layout: auto-scales to thin/narrow windows with zero line wrap
"""
import sys
import os
import re
import json
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

def clean_markdown_for_speech(text):
    """Strip Markdown formatting tokens (***, **, #, `, links, tables) so visual
    captions and spoken words match 1-to-1 without raw syntax noise."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^\s*```[^\n]*\n?", "\n", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*(?:[*_\-]\s*){3,}\s*$", "\n\n", t, flags=re.MULTILINE)
    t = re.sub(r"(?:^|\s)\*{3,}(?:\s|$)", "\n\n", t)
    t = re.sub(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\|\s*|\s*\|\s*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s*\|\s*", ", ", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://(?:www\.)?([^/\s)\]>]+)[^\s)\]>]*", r"\1", t)

    def _fmt_heading(m):
        content = m.group(1).strip()
        if content and content[-1] not in ".!?:;—–-":
            return content + ":"
        return content

    t = re.sub(r"^\s*#{1,6}\s+(.+?)\s*#*$", _fmt_heading, t, flags=re.MULTILINE)
    t = re.sub(r"^\s*>\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*+•]\s+", "", t, flags=re.MULTILINE)
    for _ in range(2):
        t = re.sub(r"\*{1,3}([^*\n]+?)\*{1,3}", r"\1", t)
        t = re.sub(r"\b_{1,3}([^_\n]+?)_{1,3}\b", r"\1", t)
        t = re.sub(r"~~([^~\n]+?)~~", r"\1", t)
        t = re.sub(r"`+([^`\n]+?)`+", r"\1", t)
    t = re.sub(r"\*+", "", t)
    t = re.sub(r"`+", "", t)
    t = re.sub(r"(?:^|\s)#+(?=\s|$)", " ", t)

    lines = []
    for line in t.split("\n"):
        cleaned_line = re.sub(r"\s+", " ", line).strip()
        if cleaned_line:
            lines.append(cleaned_line)
    return "\n".join(lines)

def wrap_text_into_lines(words, max_chars=54):
    """
    Prosodic / pause-aligned chunking:
    Breaks lines at natural breath and punctuation pauses (., !, ?, ,, ;, :, —),
    ensuring audio pauses align with line breaks rather than falling mid-line.
    Falls back to character width wrapping when a single phrase exceeds max_chars.
    """
    if not words:
        return []

    lines = []
    curr_line = []
    curr_len = 0

    for w in words:
        w_len = len(w)
        # Check if adding this word exceeds max_chars
        if curr_line and (curr_len + 1 + w_len > max_chars):
            lines.append(curr_line)
            curr_line = [w]
            curr_len = w_len
        else:
            if curr_line:
                curr_len += 1 + w_len
            else:
                curr_len = w_len
            curr_line.append(w)

        # Natural pause boundary: break line after punctuation if the line has substantive content
        clean_w = w.strip("\"'`)]}*~_")
        if clean_w.endswith((".", "!", "?")):
            lines.append(curr_line)
            curr_line = []
            curr_len = 0
        elif clean_w.endswith((",", ";", ":", "—")) and (len(curr_line) >= 3 or curr_len >= 16):
            lines.append(curr_line)
            curr_line = []
            curr_len = 0

    if curr_line:
        lines.append(curr_line)

    return lines

def estimate_syllables(w):
    clean = w.strip("\"'`*~_[]()")
    if not clean:
        return 1
    # Split paths or URLs by slash to account for technical spoken tokens
    if "/" in clean:
        parts = [p for p in clean.split("/") if p]
        return max(1, sum(estimate_syllables(p) for p in parts))
    # Account for spoken numbers (e.g. 102 -> "one hundred two", 406 -> "four hundred six")
    digits = re.findall(r"\d+", clean)
    if digits:
        total_digits = sum(len(d) for d in digits)
        return max(1, int(total_digits * 1.3))
    # Alpha word syllables
    clean_alpha = re.sub(r"[^a-zA-Z]", "", clean).lower()
    if not clean_alpha:
        return 1
    if len(clean_alpha) <= 3:
        return 1
    vowels = re.findall(r"[aeiouy]+", clean_alpha)
    count = len(vowels)
    if clean_alpha.endswith("e") and not clean_alpha.endswith(("le", "ee")):
        count = max(1, count - 1)
    if clean_alpha.endswith("ed") and not clean_alpha.endswith(("ted", "ded")):
        count = max(1, count - 1)
    return max(1, count)

def compute_word_timings_fallback(words, total_duration, speech_start=0.0):
    """Fallback per-word [start_time, end_time] when no engine .timing.json sidecar exists."""
    if not words:
        return []

    word_weights = []
    pause_weights = []
    for w in words:
        syl = estimate_syllables(w)
        word_weights.append(syl * 1.0)
        p_weight = 0.0
        if w.endswith((".", "!", "?")):
            p_weight = 3.2
        elif w.endswith((",", ";", ":", "—", "-")):
            p_weight = 1.6
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

    return word_timings


def build_clauses_and_timings_from_sidecar(timing_path, caption_char_limit, total_duration, speech_start=0.0, raw_transcript=""):
    """Load exact per-clause timestamps from <wav>.timing.json (written by Kokoro)
    so every clause's words are anchored to the exact sample boundaries in the WAV."""
    if os.path.exists(timing_path):
        try:
            with open(timing_path, "r") as f:
                meta = json.load(f)
            clauses = meta.get("clauses") or []
            if clauses:
                words = []
                line_groups = []
                word_timings = []
                for c in clauses:
                    c_words = c.get("words") or (c.get("text", "").split())
                    if not c_words:
                        continue
                    c_start = float(c.get("start", 0.0))
                    c_end = float(c.get("speech_end", c.get("end", c_start + 0.1)))
                    c_dur = max(0.02, c_end - c_start)

                    # Wrap this clause into lines that fit the terminal width
                    c_lines = wrap_text_into_lines(c_words, max_chars=caption_char_limit)
                    line_groups.extend(c_lines)
                    words.extend(c_words)

                    # Distribute clause's exact [c_start, c_end] across its words by syllable weight
                    weights = [max(1.0, float(estimate_syllables(w))) for w in c_words]
                    tot_w = sum(weights)
                    t_cursor = c_start
                    for w_weight in weights:
                        w_dur = c_dur * (w_weight / tot_w)
                        word_timings.append((t_cursor, t_cursor + w_dur))
                        t_cursor += w_dur

                if words and line_groups and word_timings:
                    return words, line_groups, word_timings
        except Exception:
            pass

    # Fallback: clean markdown from raw_transcript and estimate timings
    cleaned = clean_markdown_for_speech(raw_transcript) if raw_transcript else ""
    words = cleaned.split() if cleaned else []
    line_groups = wrap_text_into_lines(words, max_chars=caption_char_limit) if words else []
    word_timings = compute_word_timings_fallback(words, total_duration, speech_start=speech_start)
    return words, line_groups, word_timings


def find_active_word_position(words, line_groups, word_timings, elapsed, duration):
    """Locate (curr_word_idx, active_line_idx, active_word_in_line) from exact word_timings."""
    if not words or not line_groups:
        return 0, 0, 0

    total_words = len(words)
    curr_word_idx = 0

    if word_timings and len(word_timings) == total_words:
        # Find the latest word whose start_time <= elapsed
        for idx, (w_start, w_end) in enumerate(word_timings):
            if elapsed >= w_start:
                curr_word_idx = idx
            else:
                break
    else:
        ratio = min(1.0, elapsed / duration) if duration > 0 else 0
        curr_word_idx = min(total_words - 1, max(0, int(ratio * total_words)))

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

    return curr_word_idx, active_line_idx, active_word_in_line


def render_active_line(line_words, active_word_in_line, highlight_mode="word", emphasis="bold"):
    active_style = EMPHASIS_STYLES.get(emphasis.lower(), EMPHASIS_STYLES["bold"])
    if highlight_mode == "line":
        return f"\033[1;38;2;0;255;255m{' '.join(line_words)}{RESET}"
    styled = []
    for w_i, w in enumerate(line_words):
        if w_i < active_word_in_line:
            styled.append(f"\033[38;2;0;190;205m{w}{RESET}")
        elif w_i == active_word_in_line:
            styled.append(f"{active_style}{w}{RESET}")
        else:
            styled.append(f"\033[38;2;0;105;120m{w}{RESET}")
    return " ".join(styled)


EMPHASIS_STYLES = {
    "bold": "\033[1;38;2;0;255;255m",
    "underline": "\033[4;38;2;0;255;255m",
    "both": "\033[1;4;38;2;0;255;255m",
    "inverse": "\033[7;1;38;2;0;255;255m",
}

def get_voice_color(voice_name):
    """Resolve ANSI color code for persona from voices.yaml."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vy = os.path.join(script_dir, "voices.yaml")
    if os.path.exists(vy):
        try:
            import yaml
            with open(vy, "r") as f:
                data = yaml.safe_load(f)
            personas = data.get("personas", {})
            v = voice_name.lower()
            if v in personas and "color" in personas[v]:
                return f"\033[{personas[v]['color']}m"
            for p, pdata in personas.items():
                if p == v:
                    return f"\033[{pdata.get('color', 36)}m"
        except Exception:
            pass
    return "\033[36m"

def get_scrolling_caption_lines(words, line_groups, word_timings, elapsed, duration, num_lines=3, highlight_mode="word", emphasis="bold"):
    """
    Renders num_lines of closed captioning text centered on the active spoken word (window mode).
    """
    if not words or not line_groups:
        return [""] * num_lines

    _, active_line_idx, active_word_in_line = find_active_word_position(
        words, line_groups, word_timings, elapsed, duration
    )

    half = num_lines // 2
    start_line_idx = active_line_idx - half

    rendered_lines = []
    for slot in range(num_lines):
        target_idx = start_line_idx + slot
        if target_idx < 0 or target_idx >= len(line_groups):
            rendered_lines.append("")
        else:
            line_words = line_groups[target_idx]
            text_raw = " ".join(line_words)

            if target_idx < active_line_idx:
                rendered_lines.append(f"\033[38;2;0;190;205m{text_raw}{RESET}")
            elif target_idx > active_line_idx:
                rendered_lines.append(f"\033[38;2;0;105;120m{text_raw}{RESET}")
            else:
                rendered_lines.append(
                    render_active_line(line_words, active_word_in_line, highlight_mode=highlight_mode, emphasis=emphasis)
                )

    return rendered_lines

def play_with_hud(wav_path, voice_name="speak", transcript="", num_lines=3, highlight_mode="word", emphasis="bold", view_mode="scroll", mock_audio=False):
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

    # Determine safe wrap width based on terminal size (adaptive to narrow panes)
    term_cols = shutil.get_terminal_size((80, 24)).columns
    caption_char_limit = max(24, min(72, term_cols - 4))

    # Detect acoustic speech onset from audio samples
    speech_start = 0.0
    for idx, val in enumerate(rms_values):
        if val > 0.04 * max_rms:
            speech_start = idx / fps
            break

    # Load exact per-clause timings from <wav_path>.timing.json if available, else fallback
    words, line_groups, word_timings = build_clauses_and_timings_from_sidecar(
        wav_path + ".timing.json",
        caption_char_limit=caption_char_limit,
        total_duration=duration,
        speech_start=speech_start,
        raw_transcript=transcript,
    )

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

    # Clean voice display name and color badge
    clean_voice = voice_name
    if clean_voice.lower().startswith("af_") or clean_voice.lower().startswith("am_"):
        clean_voice = clean_voice[3:]
    v_color = get_voice_color(clean_voice)
    voice_badge = f"{BOLD}{v_color}{clean_voice.upper()}{RESET}"

    # Set terminal tab title (clean, no emoji) and hide cursor
    sys.stderr.write(f"\033]0;VOICE: {clean_voice.upper()}\007")
    sys.stderr.write("\033[?25l")
    sys.stderr.flush()

    is_scroll_view = (view_mode.lower() == "scroll")
    # In scroll view: active line + 1 lookahead line + 1 blank line + 1 telemetry line = 4 lines
    # In window view: 1 top pad + num_lines + 1 bot pad + 1 telemetry line
    total_refresh_lines = 4 if is_scroll_view else (1 + num_lines + 1 + 1)

    def cleanup_signal(signum, frame):
        if player and player.poll() is None:
            player.terminate()
        sys.stderr.write(f"\n\033[{total_refresh_lines}B\033[?25h\033]0;\007\n")
        sys.stderr.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup_signal)
    signal.signal(signal.SIGTERM, cleanup_signal)

    last_talk_time = 0.0
    smoothed_rms = 0.0
    HOLD_TIME = 0.35
    printed_up_to = 0

    if is_scroll_view:
        # Initial breathing line before the scrolling transcript begins
        sys.stderr.write("\n")
        sys.stderr.flush()

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration or (player and player.poll() is not None):
                break

            ratio = min(1.0, elapsed / duration) if duration > 0 else 0
            chunk_idx = int(elapsed * fps)
            cur_rms = rms_values[chunk_idx] if chunk_idx < len(rms_values) else 0.0
            norm_rms = min(1.0, cur_rms / (max_rms * 0.85 + 1e-5))

            smoothed_rms = max(norm_rms, smoothed_rms * 0.72)

            if norm_rms > 0.05 or elapsed - last_talk_time < HOLD_TIME:
                beacon = f"{GREEN}● PLAY{RESET}"
                if norm_rms > 0.05:
                    last_talk_time = elapsed
            else:
                beacon = f"{DIM}○ IDLE{RESET}"

            # Telemetry Line (Adaptive width for narrow terminal panes)
            meter_len = 10 if term_cols >= 65 else (8 if term_cols >= 50 else 6)
            track_len = 14 if term_cols >= 70 else (10 if term_cols >= 55 else 8)

            filled = int(smoothed_rms * meter_len)
            if smoothed_rms > 0.8:
                meter_col = BRIGHT_YELLOW
            elif smoothed_rms > 0.3:
                meter_col = BRIGHT_GREEN
            else:
                meter_col = GREEN
            meter_bar = meter_col + ("█" * filled) + DIM + ("░" * (meter_len - filled)) + RESET

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
            if term_cols >= 50:
                time_str = f"{cur_m:02d}:{cur_s:02d}/{tot_m:02d}:{tot_s:02d}"
            else:
                time_str = f"{cur_m:02d}:{cur_s:02d}"

            telem_content = f"  {voice_badge}  {beacon} [{meter_bar}]  [{track}] {pct:2d}% {time_str}"

            if is_scroll_view:
                if line_groups:
                    _, active_line_idx, active_word_in_line = find_active_word_position(
                        words, line_groups, word_timings, elapsed, duration
                    )
                    # Permanently commit any completed lines to the terminal's native scrollback
                    while printed_up_to < active_line_idx:
                        done_line = " ".join(line_groups[printed_up_to])
                        sys.stderr.write(f"\r\033[K  \033[38;2;0;190;205m{done_line}{RESET}\n")
                        printed_up_to += 1

                    active_rendered = render_active_line(
                        line_groups[active_line_idx],
                        active_word_in_line,
                        highlight_mode=highlight_mode,
                        emphasis=emphasis,
                    )
                    if active_line_idx + 1 < len(line_groups):
                        next_rendered = f"\033[38;2;0;105;120m{' '.join(line_groups[active_line_idx + 1])}{RESET}"
                    else:
                        next_rendered = ""
                else:
                    active_rendered = ""
                    next_rendered = ""

                frame_lines = [
                    f"  {active_rendered}",
                    f"  {next_rendered}",
                    "",
                    telem_content,
                ]
                output_str = "\n".join([f"\r\033[K{line}" for line in frame_lines])
                sys.stderr.write(output_str)
                sys.stderr.write(f"\033[{total_refresh_lines - 1}A\r")
                sys.stderr.flush()
            else:
                frame_lines = [""]
                caption_lines = get_scrolling_caption_lines(
                    words, line_groups, word_timings, elapsed, duration, num_lines=num_lines, highlight_mode=highlight_mode, emphasis=emphasis
                )
                for c_line in caption_lines:
                    frame_lines.append(f"  {c_line}")
                frame_lines.append("")
                frame_lines.append(telem_content)

                output_str = "\n".join([f"\r\033[K{line}" for line in frame_lines])
                sys.stderr.write(output_str)
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

        if is_scroll_view:
            while printed_up_to < len(line_groups):
                done_line = " ".join(line_groups[printed_up_to])
                sys.stderr.write(f"\r\033[K  \033[38;2;0;190;205m{done_line}{RESET}\n")
                printed_up_to += 1
            sys.stderr.write(f"\r\033[K\n\r\033[K  {voice_badge}  {GREEN}● DONE{RESET}  PLAYBACK FINISHED ({time_str})\n")
            sys.stderr.flush()
        else:
            fin_lines = [""]
            fin_caption_lines = get_scrolling_caption_lines(
                words, line_groups, word_timings, duration, duration, num_lines=num_lines, highlight_mode=highlight_mode, emphasis=emphasis
            )
            for c_line in fin_caption_lines:
                fin_lines.append(f"  {c_line}")
            fin_lines.append("")
            fin_lines.append(f"  {voice_badge}  {GREEN}● DONE{RESET}  PLAYBACK FINISHED ({time_str})")

            output_fin = "\n".join([f"\r\033[K{line}" for line in fin_lines])
            sys.stderr.write(f"{output_fin}\n")
            sys.stderr.flush()

    finally:
        # Restore terminal cursor and clear title
        sys.stderr.write("\033[?25h")
        sys.stderr.write("\033]0;\007")
        sys.stderr.flush()

    return 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: visualizer.py <audio.wav> [voice_name] [transcript] [-l|--lines 1-8] [--highlight line|word] [--view scroll|window] [--mock]", file=sys.stderr)
        sys.exit(1)

    wav = sys.argv[1]
    name = "droid"
    transcript_arg = ""
    lines_val = 3
    hl_mode = "word"
    emphasis_val = os.environ.get("SPEAK_EMPHASIS", "bold").lower()
    view_val = os.environ.get("SPEAK_VIEW", "scroll").lower()
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
                lines_val = 3
            skip_next = True
        elif arg.startswith("--lines="):
            try:
                lines_val = max(1, min(8, int(arg.split("=", 1)[1])))
            except ValueError:
                lines_val = 3
        elif arg in ("-H", "--highlight") and idx + 1 < len(sys.argv):
            hl_mode = sys.argv[idx + 1].lower()
            if hl_mode not in ("line", "word"):
                hl_mode = "line"
            skip_next = True
        elif arg.startswith("--highlight="):
            hl_mode = arg.split("=", 1)[1].lower()
            if hl_mode not in ("line", "word"):
                hl_mode = "line"
        elif arg in ("-e", "--emphasis") and idx + 1 < len(sys.argv):
            emphasis_val = sys.argv[idx + 1].lower()
            if emphasis_val not in EMPHASIS_STYLES:
                emphasis_val = "bold"
            skip_next = True
        elif arg.startswith("--emphasis="):
            emphasis_val = arg.split("=", 1)[1].lower()
            if emphasis_val not in EMPHASIS_STYLES:
                emphasis_val = "bold"
        elif arg == "--view" and idx + 1 < len(sys.argv):
            view_val = sys.argv[idx + 1].lower()
            if view_val not in ("scroll", "window"):
                view_val = "scroll"
            skip_next = True
        elif arg.startswith("--view="):
            view_val = arg.split("=", 1)[1].lower()
            if view_val not in ("scroll", "window"):
                view_val = "scroll"
        elif arg == "--scroll":
            view_val = "scroll"
        elif arg == "--window":
            view_val = "window"
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

    sys.exit(play_with_hud(wav, name, transcript=transcript_arg, num_lines=lines_val, highlight_mode=hl_mode, emphasis=emphasis_val, view_mode=view_val, mock_audio=mock))
