#!/usr/bin/env python3
"""
kokoro_tts.py — Local TTS synthesis helper for speak
Part of the speak CLI project
"""

import os
import sys
import argparse
import urllib.request
import ssl

def download_file(url, dest_path, description):
    print(f"  \033[90m{description} not found. Downloading...\033[0m")
    
    # Custom opener with a user-agent to prevent any blockings
    opener = urllib.request.build_opener()
    opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')]
    urllib.request.install_opener(opener)
    
    # Create unverified SSL context to bypass macOS CA certificate issues
    ssl_context = ssl._create_unverified_context()
    
    try:
        response = urllib.request.urlopen(url, context=ssl_context)
        meta = response.info()
        file_size = int(meta.get("Content-Length", 0))
        
        chunk_size = 1024 * 1024  # 1MB
        bytes_downloaded = 0
        
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                bytes_downloaded += len(chunk)
                if file_size > 0:
                    percent = (bytes_downloaded / file_size) * 100
                    # Premium terminal progress bar
                    bar_width = 30
                    filled_width = int(percent / 100 * bar_width)
                    bar = "█" * filled_width + "░" * (bar_width - filled_width)
                    mb_downloaded = bytes_downloaded / (1024 * 1024)
                    mb_total = file_size / (1024 * 1024)
                    sys.stdout.write(f"\r  \033[90m└─ Downloading: [{bar}] {percent:.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)\033[0m")
                    sys.stdout.flush()
                else:
                    sys.stdout.write(f"\r  \033[90m└─ Downloading: {bytes_downloaded / (1024 * 1024):.1f} MB downloaded\033[0m")
                    sys.stdout.flush()
        print("\n  \033[32m✓ Download complete\033[0m")
    except Exception as e:
        print(f"\n  \033[31m✗ Download failed: {e}\033[0m")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        sys.exit(1)

def map_voice(voice_name):
    # Normalize to lowercase
    v = voice_name.lower()
    
    # Valid native Kokoro voices
    valid_voices = {
        "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
        "am_adam", "am_michael", "bf_emma", "bf_isabella", "bm_george", "bm_lewis",
        "adam-michael"
    }
    if v in valid_voices:
        return v
        
    # Map Gemini voices to nearest Kokoro counterparts
    mappings = {
        "puck": "adam-michael",   # Balanced custom upbeat/smooth male
        "charon": "am_michael",   # Informative male
        "kore": "am_michael",     # Firm male
        "zephyr": "am_adam",      # Bright male
        "fenrir": "am_adam",      # Excitable male
        "leda": "af_bella",       # Youthful female
        "orus": "am_michael",     # Firm male
        "aoede": "af_sarah",      # Breezy female
        "callirrhoe": "af_sky",   # Easy-going female
    }
    
    if v in mappings:
        return mappings[v]
        
    if v.startswith("af_") or v.startswith("am_") or v.startswith("bf_") or v.startswith("bm_"):
        return v
        
    return "adam-michael"

def main():
    parser = argparse.ArgumentParser(description="Kokoro Local TTS Synthesizer")
    parser.add_argument("--text", required=True, help="Text to speak")
    parser.add_argument("--voice", default="am_adam", help="Voice ID")
    parser.add_argument("--output", required=True, help="Output WAV file path")
    args = parser.parse_args()

    # Determine local model storage paths
    model_dir = os.path.expanduser("~/.config/speak/models")
    os.makedirs(model_dir, exist_ok=True)
    
    model_path = os.path.join(model_dir, "kokoro-v1.0.onnx")
    voices_path = os.path.join(model_dir, "voices-v1.0.bin")

    # Download model files if they do not exist
    model_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
    voices_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

    if not os.path.exists(model_path):
        download_file(model_url, model_path, "Kokoro ONNX Model")
        
    if not os.path.exists(voices_path):
        download_file(voices_url, voices_path, "Kokoro Voices Pack")

    # Map voice name
    local_voice = map_voice(args.voice)

    # Initialize Kokoro
    try:
        from kokoro_onnx import Kokoro
        kokoro = Kokoro(model_path, voices_path)
    except Exception as e:
        print(f"\n  \033[31m✗ Error initializing Kokoro-ONNX model: {e}\033[0m")
        print("  \033[90mThis is commonly due to missing espeak-ng on macOS.\033[0m")
        print("  \033[90mPlease run: brew install espeak-ng\033[0m\n")
        sys.exit(1)

    # Choose language code and handle custom voice blending
    lang = "en-us"
    if local_voice == "adam-michael":
        try:
            style_adam = kokoro.get_voice_style("am_adam")
            style_michael = kokoro.get_voice_style("am_michael")
            voice_arg = 0.5 * style_adam + 0.5 * style_michael
        except Exception as e:
            print(f"\n  \033[31m✗ Error blending voice styles am_adam and am_michael: {e}\033[0m")
            sys.exit(1)
    else:
        voice_arg = local_voice
        if local_voice.startswith("bf_") or local_voice.startswith("bm_"):
            lang = "en-gb"

    # Synthesize audio
    try:
        samples, sample_rate = kokoro.create(
            args.text,
            voice=voice_arg,
            speed=1.0,
            lang=lang
        )
    except Exception as e:
        print(f"\n  \033[31m✗ Speech synthesis failed: {e}\033[0m")
        sys.exit(1)

    # Save output to wav
    try:
        import soundfile as sf
        sf.write(args.output, samples, sample_rate)
    except Exception as e:
        print(f"\n  \033[31m✗ Saving audio file failed: {e}\033[0m")
        sys.exit(1)

if __name__ == "__main__":
    main()
