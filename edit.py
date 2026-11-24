#!/usr/bin/env python
"""lecture-editor: clean audio, speed up silences, caption and finalize lecture recordings.

Usage:
    python edit.py process input/<file>.mp4
    python edit.py finalize <file> [<file> ...]
    python edit.py all input/<file>.mp4 --no-review
    python edit.py all input/            (every video in the folder)
    python edit.py youtube <file>        (redo the YouTube title/description/chapters)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
OUTPUT = ROOT / "output"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

DEFAULTS = {
    "whisper_model": "medium",
    "speech_language": "ur",
    "max_caption_chars": 84,
    "glossary": [],
    "silence_min_seconds": 2,
    "silence_speed": 3,
    "silent_audio": "mute",
    "speech_margin_seconds": 0.3,
    "silence_threshold": 0.04,
    "rnnoise_model": "assets/rnnoise.rnnn",
    "loudness_target": -16,
    "caption_style": {
        "font": "Arial",
        "size": "medium",
        "color": "white",
        "outline_color": "black",
        "outline_width": 2,
        "position": "bottom_center",
        "margin": 20,
    },
    "logo_watermark": False,
    "logo_path": "assets/logo.png",
    "logo_height_percent": 10,
    "intro_path": "assets/intro.mp4",
    "outro_path": "assets/outro.mp4",
    "export_srt": True,
    "youtube_package": True,
    "youtube_mode": "prompt",
    "youtube_model": "claude-opus-5-5",
    "youtube_language": "English",
    "youtube_channel_info": "",
    "youtube_footer": "",
    "output": {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "video_codec": "libx264",
        "crf": 20,
        "preset": "medium",
        "audio_rate": 48000,
        "audio_bitrate": "192k",
    },
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

class EditError(Exception):
    """An error with a message meant for the person running the tool."""


def say(message: str = "") -> None:
    print(message, flush=True)


def step(number: int, total: int, message: str) -> None:
    say(f"\n[{number}/{total}] {message}")


def info(message: str) -> None:
    say(f"      {message}")


def warn(message: str) -> None:
    say(f"      WARNING: {message}")


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def is_on(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"on", "true", "yes", "1"}


def resolve(path_value: str) -> Path:
    """Config paths are relative to the project folder unless absolute."""
    p = Path(str(path_value))
    return p if p.is_absolute() else ROOT / p


def load_config(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise EditError("PyYAML is not installed. Run:  pip install -r requirements.txt")
    if not path.exists():
        raise EditError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        user = yaml.safe_load(f) or {}
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    if str(cfg["silent_audio"]).lower() not in {"mute", "keep"}:
        raise EditError("config.yaml: silent_audio must be 'mute' or 'keep'.")
    if float(cfg["silence_speed"]) < 1:
        raise EditError("config.yaml: silence_speed must be 1 or more.")
    return cfg


# ----------------------------------------------------------------------------
# External tools
# ----------------------------------------------------------------------------

def find_tool(name: str) -> str | None:
    """Look next to the running Python first (virtual environment), then on PATH."""
    scripts = Path(sys.executable).parent
    for candidate in (scripts / f"{name}.exe", scripts / name):
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise EditError(
            "FFmpeg was not found.\n"
            "      Install it with:   winget install Gyan.FFmpeg\n"
            "      then close and reopen the terminal so 'ffmpeg' is on your PATH."
        )
    return ffmpeg, ffprobe


def require_auto_editor() -> str:
    exe = find_tool("auto-editor")
    if not exe:
        raise EditError(
            "auto-editor was not found.\n"
            "      Install it with:   pip install -r requirements.txt\n"
            "      (make sure the virtual environment is activated first)."
        )
    return exe


def probe(path: Path) -> dict:
    """Return basic facts about a media file, or raise EditError if unreadable."""
    _, ffprobe = require_ffmpeg()
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise EditError(f"FFmpeg could not read this file: {path}\n      {result.stderr.strip()[-300:]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = [s for s in streams
             if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        duration = float(data.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        duration = 0.0
    return {
        "has_video": bool(video),
        "has_audio": bool(audio),
        "duration": duration,
        "width": int(video[0].get("width", 0)) if video else 0,
        "height": int(video[0].get("height", 0)) if video else 0,
    }


def run_ffmpeg(args: list[str], total_seconds: float | None, label: str) -> None:
    """Run FFmpeg from the project folder, showing a percentage while it works."""
    ffmpeg, _ = require_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"] + args
    started = time.time()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_file:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=err_file,
                                text=True, encoding="utf-8", errors="replace")
        last = -1
        for line in proc.stdout:
            if not total_seconds or not line.startswith("out_time_us="):
                continue
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            pct = max(0, min(100, int(done / total_seconds * 100)))
            if pct != last:
                last = pct
                print(f"\r      {label}: {pct}%   ", end="", flush=True)
        proc.wait()
        if last >= 0:
            print(f"\r      {label}: done in {fmt_duration(time.time() - started)}      ", flush=True)
        if proc.returncode != 0:
            err_file.seek(0)
            tail = err_file.read().strip()[-1500:]
            raise EditError(f"FFmpeg failed while: {label}\n{tail}")


def filter_path(path: Path) -> str:
    """Write a file path so it is safe inside an FFmpeg filter (Windows drive colons etc.)."""
    try:
        text = path.resolve().relative_to(ROOT).as_posix()   # FFmpeg runs from ROOT
    except ValueError:
        text = path.resolve().as_posix()
    text = text.replace("\\", "/").replace(":", "\\:").replace("'", "")
    return f"'{text}'"


# ----------------------------------------------------------------------------
# Input checks
# ----------------------------------------------------------------------------

def check_input_video(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        hint = ""
        candidates = sorted(p.name for p in (ROOT / "input").glob("*") if p.suffix.lower() in VIDEO_EXTENSIONS)
        if candidates:
            hint = "\n      Videos found in input/: " + ", ".join(candidates[:10])
        raise EditError(f"Input file not found: {path}{hint}")
    if not path.is_file():
        raise EditError(f"Input is a folder, not a video file: {path}")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise EditError(
            f"'{path.name}' does not look like a video (expected one of: "
            + ", ".join(sorted(VIDEO_EXTENSIONS)) + ")."
        )
    facts = probe(path)
    if not facts["has_video"]:
        raise EditError(f"'{path.name}' has no video track, so it cannot be processed.")
    if not facts["has_audio"]:
        raise EditError(f"'{path.name}' has no audio track, so there is nothing to clean or caption.")
    return path


# ----------------------------------------------------------------------------
# Step 1: clean audio
# ----------------------------------------------------------------------------

def clean_audio(src: Path, dst: Path, cfg: dict, duration: float) -> None:
    rate = int(cfg["output"]["audio_rate"])
    model = resolve(cfg["rnnoise_model"])
    if model.exists():
        denoise = f"arnndn=m={filter_path(model)}"
        info(f"Noise reduction: arnndn ({model.name})")
    else:
        denoise = "afftdn=nf=-25"
        info(f"Noise reduction: afftdn (no model at {cfg['rnnoise_model']}, using the built-in fallback)")
    target = float(cfg["loudness_target"])
    info(f"Loudness: normalizing to {target:g} LUFS")
    chain = f"aresample=48000,highpass=f=70,{denoise},loudnorm=I={target:g}:TP=-1.5:LRA=11,aresample={rate}"
    run_ffmpeg(
        ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
         "-af", chain, "-c:a", "aac", "-b:a", str(cfg["output"]["audio_bitrate"]),
         "-ar", str(rate), "-ac", "2", str(dst)],
        duration, "Cleaning audio",
    )


# ----------------------------------------------------------------------------
# Step 2: speed up silences with auto-editor
# ----------------------------------------------------------------------------

def auto_editor_flags(exe: str, cfg: dict) -> list[str]:
    """Build the edit flags for whichever auto-editor version is installed."""
    result = subprocess.run([exe, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    help_text = result.stdout + result.stderr
    speed = f"{float(cfg['silence_speed']):g}"
    margin = f"{float(cfg['speech_margin_seconds']):g}s"
    threshold = f"{float(cfg['silence_threshold']):g}"
    min_silence = f"{float(cfg['silence_min_seconds']):g}s"

    # mincut: silences shorter than the minimum count as speech (left at normal speed).
    flags = ["--edit", f"audio:threshold={threshold},mincut={min_silence}", "--margin", margin]
    if "--when-silent" in help_text:
        flags += ["--when-silent", f"speed:{speed}", "--when-normal", "nil"]
    elif "--silent-speed" in help_text:
        flags += ["--silent-speed", speed, "--video-speed", "1"]
    else:
        raise EditError(
            "This auto-editor version has neither --when-silent nor --silent-speed.\n"
            "      Run 'auto-editor --help' and update auto_editor_flags() in edit.py."
        )
    return flags


def clip_speed(clip: dict) -> float:
    """Speed of one timeline clip (auto-editor stores it as an effect like 'speed:3.0')."""
    for effect in clip.get("effects", []):
        kind, _, value = str(effect).partition(":")
        if kind in {"speed", "rate"}:
            try:
                return float(value)
            except ValueError:
                pass
    return float(clip.get("speed", 1.0))


def run_auto_editor(exe: str, args: list[str], label: str) -> None:
    say(f"      {label} (auto-editor prints its own progress below)")
    result = subprocess.run([exe] + args, cwd=ROOT)
    if result.returncode != 0:
        raise EditError(f"auto-editor failed while: {label}\n      Command: auto-editor " + " ".join(args))


def read_silent_ranges(timeline_path: Path) -> tuple[list[tuple[float, float]], float]:
    """From an auto-editor v3 timeline, return the output-time ranges that are sped up."""
    with open(timeline_path, "r", encoding="utf-8") as f:
        tl = json.load(f)
    tb = str(tl.get("timebase", "30/1"))
    num, _, den = tb.partition("/")
    fps = float(num) / float(den or 1)
    ranges: list[tuple[float, float]] = []
    end = 0.0
    tracks = tl.get("a") or tl.get("v") or []
    for clip in (tracks[0] if tracks else []):
        start = clip["start"] / fps
        stop = (clip["start"] + clip["dur"]) / fps
        end = max(end, stop)
        if abs(clip_speed(clip) - 1.0) > 1e-6:
            ranges.append((start, stop))
    return ranges, end


def mute_ranges(src_video: Path, dst_video: Path, ranges: list[tuple[float, float]],
                cfg: dict, duration: float) -> None:
    """Silence the audio inside the given time ranges; the video is copied untouched."""
    rate = int(cfg["output"]["audio_rate"])
    raw_wav = WORK / "_mute_in.wav"
    muted_wav = WORK / "_mute_out.wav"
    try:
        run_ffmpeg(["-i", str(src_video), "-vn", "-c:a", "pcm_s16le", "-ar", str(rate), "-ac", "2", str(raw_wav)],
                   duration, "Extracting audio")
        with wave.open(str(raw_wav), "rb") as win, wave.open(str(muted_wav), "wb") as wout:
            wout.setparams(win.getparams())
            frame_bytes = win.getsampwidth() * win.getnchannels()
            spans = sorted((int(a * rate), int(b * rate)) for a, b in ranges)
            idx, pos = 0, 0
            while True:
                chunk = win.readframes(rate)  # one second at a time
                if not chunk:
                    break
                n = len(chunk) // frame_bytes
                while idx < len(spans) and spans[idx][1] <= pos:
                    idx += 1
                j, buf = idx, None
                while j < len(spans) and spans[j][0] < pos + n:
                    a, b = max(spans[j][0], pos) - pos, min(spans[j][1], pos + n) - pos
                    if b > a:
                        if buf is None:
                            buf = bytearray(chunk)
                        buf[a * frame_bytes:b * frame_bytes] = bytes((b - a) * frame_bytes)
                    j += 1
                wout.writeframes(bytes(buf) if buf is not None else chunk)
                pos += n
        run_ffmpeg(["-i", str(src_video), "-i", str(muted_wav), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", str(cfg["output"]["audio_bitrate"]),
                    "-movflags", "+faststart", str(dst_video)],
                   duration, "Writing muted audio")
    finally:
        for tmp in (raw_wav, muted_wav):
            tmp.unlink(missing_ok=True)


def speed_up_silences(clean: Path, fast: Path, cfg: dict) -> None:
    exe = require_auto_editor()
    flags = auto_editor_flags(exe, cfg)
    mute = str(cfg["silent_audio"]).lower() == "mute"
    info(f"Silences longer than {float(cfg['silence_min_seconds']):g}s play at "
         f"{float(cfg['silence_speed']):g}x ({'muted' if mute else 'audio kept'}); nothing is cut out.")

    timeline = WORK / f"{fast.stem}_timeline.v3"      # auto-editor always names this export .v3
    timeline.unlink(missing_ok=True)
    run_auto_editor(exe, [str(clean)] + flags + ["--export", "v3", "--output", str(timeline), "--no-open"],
                    "Finding silences")
    if not timeline.exists():
        raise EditError("auto-editor did not write a timeline file, so silences could not be located.")
    ranges, new_length = read_silent_ranges(timeline)
    timeline.unlink(missing_ok=True)
    info(f"Found {len(ranges)} silent section(s) to speed up.")

    render_to = WORK / f"{fast.stem}_render.mp4" if (mute and ranges) else fast
    run_auto_editor(exe, [str(clean)] + flags + ["--output", str(render_to), "--no-open"],
                    "Rendering sped-up video")
    if not render_to.exists():
        raise EditError(f"auto-editor finished but did not create {render_to.name}.")
    if render_to != fast:
        mute_ranges(render_to, fast, ranges, cfg, new_length or probe(render_to)["duration"])
        render_to.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# Step 3: captions with faster-whisper
# ----------------------------------------------------------------------------

def srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_caption(words: list[tuple[float, float, str]], max_chars: int) -> list[tuple[float, float, str]]:
    """Turn timed words into captions of similar length, none longer than max_chars.

    A pause of more than a second (for example a sped-up silence) also starts a new caption.
    """
    groups: list[list[tuple[float, float, str]]] = [[]]
    for word in words:
        if groups[-1] and word[0] - groups[-1][-1][1] > 1.0:
            groups.append([])
        groups[-1].append(word)

    out: list[tuple[float, float, str]] = []
    for group in groups:
        total = sum(len(w[2]) + 1 for w in group)
        pieces = max(1, -(-total // max_chars))
        target = total / pieces
        current: list[tuple[float, float, str]] = []
        length = 0
        for word in group:
            size = len(word[2]) + 1
            if current and (length + size > max_chars or length >= target):
                out.append((current[0][0], current[-1][1], " ".join(w[2] for w in current)))
                current, length = [], 0
            current.append(word)
            length += size
        if current:
            out.append((current[0][0], current[-1][1], " ".join(w[2] for w in current)))
    return out


def segment_words(seg) -> list[tuple[float, float, str]]:
    """Timed words of a Whisper segment; falls back to spreading time evenly over the text."""
    words = [(w.start, w.end, w.word.strip()) for w in (seg.words or []) if w.word.strip()]
    if words:
        return words
    parts = seg.text.split()
    total = sum(len(p) for p in parts) or 1
    out, t = [], seg.start
    for part in parts:
        length = (seg.end - seg.start) * len(part) / total
        out.append((t, t + length, part))
        t += length
    return out


def glossary_terms(cfg: dict) -> list[str]:
    """Subject words from config.yaml, as a list or a comma-separated string."""
    raw = cfg.get("glossary") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        raise EditError("config.yaml: glossary must be a list of words, e.g. [debit, credit, IFRS].")
    terms, seen = [], set()
    for item in raw:
        term = str(item).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def make_captions(video: Path, srt_path: Path, cfg: dict, duration: float) -> int:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise EditError("faster-whisper is not installed. Run:  pip install -r requirements.txt")

    model_name = str(cfg["whisper_model"])
    language = str(cfg.get("speech_language") or "auto").lower()
    info(f"Loading Whisper model '{model_name}' on CPU (the first run downloads it; this can take a while)...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    extra = {}
    terms = glossary_terms(cfg)
    if terms:
        # hotwords are given to Whisper for every 30-second window, unlike initial_prompt
        # which (with condition_on_previous_text off) only helps the first one.
        extra["hotwords"] = ", ".join(terms)
        info(f"Glossary: {len(terms)} word(s) to help spelling ({', '.join(terms[:6])}"
             f"{', ...' if len(terms) > 6 else ''})")
    info("Translating speech to English captions...")
    segments, _ = model.transcribe(
        str(video),
        task="translate",
        language=None if language == "auto" else language,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
        word_timestamps=True,
        **extra,
    )
    captions: list[tuple[float, float, str]] = []
    last_pct, started = -1, time.time()
    for seg in segments:
        if seg.text.strip():
            captions.extend(split_caption(segment_words(seg), int(cfg["max_caption_chars"])))
        if duration:
            pct = min(100, int(seg.end / duration * 100))
            if pct != last_pct:
                last_pct = pct
                print(f"\r      Captioning: {pct}%   ", end="", flush=True)
    if last_pct >= 0:
        print(f"\r      Captioning: done in {fmt_duration(time.time() - started)}      ", flush=True)

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(captions, 1):
            f.write(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")
    return len(captions)


# ----------------------------------------------------------------------------
# Command: process
# ----------------------------------------------------------------------------

def cmd_process(src: Path, cfg: dict) -> str:
    require_ffmpeg()
    require_auto_editor()
    WORK.mkdir(exist_ok=True)
    stem = src.stem
    facts = probe(src)
    say(f"Processing {src.name}  ({fmt_duration(facts['duration'])}, {facts['width']}x{facts['height']})")

    clean = WORK / f"{stem}_clean.mkv"
    fast = WORK / f"{stem}_fast.mp4"
    srt = WORK / f"{stem}.srt"

    step(1, 3, "Cleaning audio (noise reduction + loudness)")
    clean_audio(src, clean, cfg, facts["duration"])

    step(2, 3, "Speeding up silences")
    speed_up_silences(clean, fast, cfg)
    fast_facts = probe(fast)
    saved = facts["duration"] - fast_facts["duration"]
    info(f"Length: {fmt_duration(facts['duration'])} -> {fmt_duration(fast_facts['duration'])} "
         f"(saved {fmt_duration(max(0, saved))})")

    step(3, 3, "Creating English captions")
    if srt.exists():
        backup = srt.with_suffix(".srt.bak")
        shutil.copyfile(srt, backup)
        info(f"An older caption file existed; saved a copy as {backup.name}")
    count = make_captions(fast, srt, cfg, fast_facts["duration"])
    info(f"Wrote {count} caption(s) to {srt.relative_to(ROOT)}")
    if count == 0:
        warn("No speech was detected, so the caption file is empty.")
    clean.unlink(missing_ok=True)
    return stem


# ----------------------------------------------------------------------------
# Command: finalize
# ----------------------------------------------------------------------------

ASS_COLORS = {
    "white": "FFFFFF", "black": "000000", "yellow": "FFFF00", "red": "FF0000",
    "green": "00FF00", "blue": "0000FF", "cyan": "00FFFF", "orange": "FFA500",
}
ASS_SIZES = {"small": 14, "medium": 18, "large": 24}
ASS_ALIGN = {"bottom_left": 1, "bottom_center": 2, "bottom_right": 3,
             "top_left": 7, "top_center": 8, "top_right": 9}


def ass_color(value) -> str:
    text = str(value).strip().lower()
    rgb = ASS_COLORS.get(text, text.lstrip("#"))
    if not re.fullmatch(r"[0-9a-f]{6}", rgb.lower()):
        raise EditError(f"config.yaml: unknown caption color '{value}'. Use a name like white or a hex like #FFCC00.")
    rgb = rgb.upper()
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"   # ASS wants blue-green-red


def caption_style(cfg: dict) -> str:
    style = cfg["caption_style"]
    size = style.get("size", "medium")
    size = ASS_SIZES.get(str(size).lower(), size)
    try:
        size = float(size)
    except (TypeError, ValueError):
        raise EditError("config.yaml: caption size must be small, medium, large or a number.")
    position = str(style.get("position", "bottom_center")).lower()
    if position not in ASS_ALIGN:
        raise EditError("config.yaml: caption position must be one of: " + ", ".join(ASS_ALIGN))
    font = re.sub(r"[,:']", "", str(style.get("font", "Arial")))
    margin = int(style.get("margin", 20))
    return ",".join([
        f"FontName={font}", f"FontSize={size:g}",
        f"PrimaryColour={ass_color(style.get('color', 'white'))}",
        f"OutlineColour={ass_color(style.get('outline_color', 'black'))}",
        "BorderStyle=1", f"Outline={float(style.get('outline_width', 2)):g}", "Shadow=0",
        f"Alignment={ASS_ALIGN[position]}", f"MarginV={margin}", f"MarginL={margin}", f"MarginR={margin}",
    ])


SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def shift_srt(text: str, offset: float) -> str:
    """Move every caption time later by offset seconds (to make room for the intro)."""
    def move(m: re.Match) -> str:
        h, mi, s, ms = (int(g) for g in m.groups())
        return srt_time(h * 3600 + mi * 60 + s + ms / 1000 + offset)
    return SRT_TIME.sub(move, text)


def srt_has_captions(path: Path) -> bool:
    return "-->" in path.read_text(encoding="utf-8-sig", errors="replace")


def cmd_finalize(name: str, cfg: dict, youtube: bool = True) -> Path:
    require_ffmpeg()
    stem = Path(name).stem
    for suffix in ("_fast", "_final"):
        if stem.endswith(suffix) and not (WORK / f"{stem}_fast.mp4").exists():
            stem = stem[: -len(suffix)]
    lecture = WORK / f"{stem}_fast.mp4"
    srt = WORK / f"{stem}.srt"
    if not lecture.exists():
        raise EditError(f"{lecture.relative_to(ROOT)} not found.\n"
                        f"      Run this first:  python edit.py process input/{stem}.mp4")
    if not srt.exists():
        raise EditError(f"{srt.relative_to(ROOT)} not found.\n"
                        f"      Run this first:  python edit.py process input/{stem}.mp4")
    OUTPUT.mkdir(exist_ok=True)
    out = cfg["output"]
    W, H, fps, rate = int(out["width"]), int(out["height"]), out["fps"], int(out["audio_rate"])
    say(f"Finalizing {stem}")

    step(1, 3, "Preparing captions, logo, intro and outro")
    burn_srt = None
    if srt_has_captions(srt):
        burn_srt = WORK / "_burn.srt"          # fixed simple name keeps the FFmpeg filter safe
        burn_srt.write_text(srt.read_text(encoding="utf-8-sig", errors="replace"), encoding="utf-8")
        info(f"Captions: {srt.relative_to(ROOT)}")
    else:
        warn(f"{srt.name} has no captions; the video will have none burned in.")

    logo = None
    if is_on(cfg["logo_watermark"]):
        logo = resolve(cfg["logo_path"])
        if logo.exists():
            info(f"Logo: {cfg['logo_path']} (top-right)")
        else:
            warn(f"logo_watermark is on but {cfg['logo_path']} is missing; continuing without a logo.")
            logo = None

    parts: list[tuple[str, Path]] = []
    for label, key in (("intro", "intro_path"), ("lecture", None), ("outro", "outro_path")):
        if key is None:
            parts.append((label, lecture))
            continue
        path = resolve(cfg[key])
        if not path.exists():
            warn(f"{cfg[key]} is missing; skipping the {label}.")
            continue
        facts = probe(path)
        if not facts["has_video"]:
            warn(f"{cfg[key]} has no video track; skipping the {label}.")
            continue
        info(f"{label.capitalize()}: {cfg[key]} ({fmt_duration(facts['duration'])})")
        parts.append((label, path))

    step(2, 3, f"Encoding to {W}x{H}, {fps}fps, H.264, {rate // 1000}kHz audio")
    inputs: list[str] = []
    graph: list[str] = []
    total = 0.0
    lecture_start = 0.0
    for i, (label, path) in enumerate(parts):
        facts = probe(path)
        if label == "lecture":
            lecture_start = total
        total += facts["duration"]
        inputs += ["-i", str(path)]
        video = (f"[{i}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")
        if label == "lecture" and burn_srt:
            video += f",subtitles={filter_path(burn_srt)}:force_style='{caption_style(cfg)}'"
        if label == "lecture" and logo:
            logo_index = len(parts)
            logo_h = max(2, int(H * float(cfg["logo_height_percent"]) / 100) // 2 * 2)
            graph.append(f"{video}[base{i}]")
            graph.append(f"[{logo_index}:v:0]scale=-2:{logo_h}[logo]")
            graph.append(f"[base{i}][logo]overlay=W-w-30:30[v{i}]")
        else:
            graph.append(f"{video}[v{i}]")
        if facts["has_audio"]:
            graph.append(f"[{i}:a:0]aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            graph.append(f"anullsrc=r={rate}:cl=stereo,atrim=duration={facts['duration']:.3f}[a{i}]")
    if logo:
        inputs += ["-i", str(logo)]
    n = len(parts)
    graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vout][aout]")

    final = OUTPUT / f"{stem}_final.mp4"
    run_ffmpeg(
        inputs + ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
                  "-c:v", str(out["video_codec"]), "-crf", str(out["crf"]), "-preset", str(out["preset"]),
                  "-pix_fmt", "yuv420p", "-r", str(fps),
                  "-c:a", "aac", "-b:a", str(out["audio_bitrate"]), "-ar", str(rate), "-ac", "2",
                  "-movflags", "+faststart", str(final)],
        total, "Encoding final video",
    )
    if burn_srt:
        burn_srt.unlink(missing_ok=True)

    step(3, 3, "Checking the result")
    facts = probe(final)
    size_mb = final.stat().st_size / 1_048_576
    info(f"{final.relative_to(ROOT)}  ({fmt_duration(facts['duration'])}, "
         f"{facts['width']}x{facts['height']}, {size_mb:.0f} MB)")
    if burn_srt and is_on(cfg.get("export_srt", True)):
        srt_out = final.with_suffix(".srt")
        text = srt.read_text(encoding="utf-8-sig", errors="replace")
        srt_out.write_text(shift_srt(text, lecture_start), encoding="utf-8")
        note = f", shifted {lecture_start:.1f}s for the intro" if lecture_start else ""
        info(f"{srt_out.relative_to(ROOT)}  (caption file for YouTube{note})")

    if youtube and is_on(cfg.get("youtube_package", True)):
        if burn_srt:
            text = shift_srt(srt.read_text(encoding="utf-8-sig", errors="replace"), lecture_start)
            try:
                make_youtube(stem, text, facts["duration"], lecture_start, cfg)
            except EditError as e:
                warn(f"YouTube package skipped: {e}")
        else:
            warn("No captions, so there is no transcript to write a YouTube description from.")
    say(f"\nDone. Your video is ready: {final}")
    return final


# ----------------------------------------------------------------------------
# YouTube package: title, description, chapters and tags written by Claude
# ----------------------------------------------------------------------------

YOUTUBE_SYSTEM = """You prepare YouTube upload text for recorded lectures.
You get the lecture's captions with timestamps (already matching the final video) and
write what the teacher pastes into YouTube Studio.

- Titles: three options, each under 70 characters, specific to what this lecture teaches.
  Plain wording a student would search for; no clickbait, no ALL CAPS, no emoji.
- Description: 2 to 4 short paragraphs saying what the lecture covers and who it helps.
  Do not include chapters, links, hashtags or a call to subscribe; those are added separately.
- Chapters: one per real change of topic, in time order, using times from the captions.
  At least 3 if the lecture is long enough, each at least 30 seconds after the previous one.
  Short titles (2 to 6 words). Use the start time of the caption where the topic begins.
- Tags: 8 to 15 search phrases, most specific first.
- Hashtags: 2 or 3, without spaces, each starting with #.
The captions were machine-translated from mixed Urdu and English speech, so ignore small
wording errors and fix obvious misspellings of subject terms in what you write."""


def youtube_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def srt_to_transcript(text: str) -> str:
    """Turn SRT text into compact '[m:ss] words' lines for the model."""
    lines = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        rows = [r for r in block.split("\n") if r.strip()]
        stamp = next((r for r in rows if "-->" in r), None)
        if not stamp:
            continue
        m = SRT_TIME.search(stamp)
        if not m:
            continue
        h, mi, s, _ = (int(g) for g in m.groups())
        words = " ".join(r.strip() for r in rows[rows.index(stamp) + 1:])
        if words:
            lines.append(f"[{youtube_time(h * 3600 + mi * 60 + s)}] {words}")
    return "\n".join(lines)


def clean_chapters(chapters: list[tuple[float, str]], duration: float,
                   lecture_start: float) -> list[tuple[float, str]]:
    """Apply YouTube's chapter rules: first at 0:00, at least 3, each 10s or longer."""
    out: list[tuple[float, str]] = []
    if lecture_start >= 10:
        out.append((0.0, "Intro"))
    for start, title in sorted(chapters):
        title = title.strip()
        if not title or start >= duration - 10:
            continue
        if not out:
            start = 0.0
        elif start < lecture_start or start - out[-1][0] < 10:
            continue
        out.append((float(start), title))
    if out and out[0][0] != 0:
        out[0] = (0.0, out[0][1])
    return out if len(out) >= 3 else []


def ask_claude_for_youtube(transcript: str, duration: float, lecture_start: float, cfg: dict):
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        raise EditError("The anthropic package is not installed. Run:  pip install -r requirements.txt")

    class Chapter(BaseModel):
        start_seconds: int
        title: str

    class YouTubePackage(BaseModel):
        titles: list[str]
        description: str
        chapters: list[Chapter]
        tags: list[str]
        hashtags: list[str]

    about = str(cfg.get("youtube_channel_info") or "").strip()
    intro = (f"The video opens with a {youtube_time(lecture_start)} intro; the lecture starts at "
             f"{youtube_time(lecture_start)}.\n" if lecture_start else "")
    prompt = (
        (f"About the channel: {about}\n" if about else "")
        + f"Write everything in {cfg.get('youtube_language') or 'English'}.\n"
        + f"Final video length: {youtube_time(duration)}.\n" + intro
        + f"\n<captions>\n{transcript}\n</captions>"
    )
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=str(cfg.get("youtube_model") or "claude-opus-5-5"),
            max_tokens=16000,
            system=YOUTUBE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=YouTubePackage,
            output_config={"effort": "medium"},
            # If the model declines (rare), the API retries on a suitable model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise EditError("Claude rejected the API key. Check ANTHROPIC_API_KEY (see README: YouTube package).")
    except anthropic.PermissionDeniedError as e:
        raise EditError(f"The API key is not allowed to use this model: {e.message}")
    except anthropic.NotFoundError:
        raise EditError(f"Unknown youtube_model '{cfg.get('youtube_model')}' in config.yaml.")
    except anthropic.RateLimitError:
        raise EditError("Claude is rate-limiting this key right now. Try later:  python edit.py youtube <name>")
    except anthropic.APIStatusError as e:
        raise EditError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise EditError("Could not reach the Claude API. Check the internet connection, then run:"
                        "  python edit.py youtube <name>")
    except (anthropic.AnthropicError, TypeError) as e:
        # With no key at all the SDK raises TypeError("Could not resolve authentication method ...").
        if isinstance(e, TypeError) and "authentication" not in str(e):
            raise
        raise EditError("No Claude API key found. Set ANTHROPIC_API_KEY (see README: YouTube package).")
    if response.stop_reason == "refusal":
        raise EditError("Claude declined to write this description.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise EditError("Claude's answer was cut off or unreadable. Try again:  python edit.py youtube <name>")
    return response.parsed_output


def make_youtube_package(stem: str, srt_text: str, duration: float, lecture_start: float, cfg: dict) -> Path:
    transcript = srt_to_transcript(srt_text)
    if not transcript:
        raise EditError("the caption file is empty.")
    info(f"Asking Claude ({cfg.get('youtube_model')}) using {len(transcript.split())} words of captions...")
    pkg = ask_claude_for_youtube(transcript, duration, lecture_start, cfg)

    chapters = clean_chapters([(c.start_seconds, c.title) for c in pkg.chapters], duration, lecture_start)
    hashtags = " ".join(h if h.startswith("#") else "#" + h for h in (t.replace(" ", "") for t in pkg.hashtags) if h)
    footer = str(cfg.get("youtube_footer") or "").strip()

    desc = [pkg.description.strip()]
    if chapters:
        desc.append("Chapters:\n" + "\n".join(f"{youtube_time(t)} {title}" for t, title in chapters))
    if footer:
        desc.append(footer)
    if hashtags:
        desc.append(hashtags)

    out = [
        "TITLE (pick one)",
        *[f"  {i}. {t.strip()}" for i, t in enumerate(pkg.titles, 1)],
        "",
        "DESCRIPTION (paste everything between the lines)",
        "-" * 60,
        "\n\n".join(desc),
        "-" * 60,
        "",
        "TAGS (paste into YouTube Studio > Show more > Tags)",
        ", ".join(t.strip() for t in pkg.tags if t.strip()),
        "",
    ]
    if not chapters:
        out.append("Note: no chapters were added; YouTube needs at least 3, each 10 seconds or longer.\n")
    path = OUTPUT / f"{stem}_youtube.txt"
    path.write_text("\n".join(out), encoding="utf-8")
    info(f"{path.relative_to(ROOT)}  ({len(pkg.titles)} titles, {len(chapters)} chapters, {len(pkg.tags)} tags)")
    return path


def write_youtube_prompt(stem: str, srt_text: str, duration: float, lecture_start: float, cfg: dict) -> Path:
    """Free route: a ready-made prompt to paste into ChatGPT, Gemini or Claude.ai in the browser."""
    transcript = srt_to_transcript(srt_text)
    if not transcript:
        raise EditError("the caption file is empty.")
    about = str(cfg.get("youtube_channel_info") or "").strip()
    footer = str(cfg.get("youtube_footer") or "").strip()
    language = cfg.get("youtube_language") or "English"
    last = youtube_time(max(0.0, duration - 10))

    if lecture_start >= 10:
        first = (f'- the first chapter is "0:00 Intro"; the next one is {youtube_time(lecture_start)} '
                 "or later, where the lecture begins")
    else:
        first = "- the first chapter starts at 0:00"
    footer_rule = f"Then a blank line and this text exactly as written:\n{footer}\n" if footer else ""

    prompt = f"""Write the YouTube upload text for my recorded lecture. Write it in {language}.
{f"About my channel: {about}" + chr(10) if about else ""}
The video is {youtube_time(duration)} long.{f" It starts with a {youtube_time(lecture_start)} intro." if lecture_start else ""}
Below are the lecture's captions with timestamps that match the final video. They were
machine-translated from mixed Urdu and English speech, so ignore small wording errors and
spell subject terms correctly in what you write.

Reply with exactly these three parts and nothing else.

TITLES
Three title options, each under 70 characters, specific to what this lecture teaches, in
words a student would search for. No clickbait, no ALL CAPS, no emoji.

DESCRIPTION
Put this whole part inside one code block so I can copy it in one go:
2 to 4 short paragraphs on what the lecture covers and who it helps.
Then a blank line, the line "Chapters:", and the chapter list.
{footer_rule}Then a blank line and 2 or 3 hashtags.

Chapter rules (YouTube ignores all chapters if one rule is broken):
- one line per chapter: the time, a space, a short title of 2 to 6 words, e.g. "4:35 Accrual basis"
{first}
- at least 3 chapters, each at least 30 seconds after the one before
- use the caption times below where each new topic starts; no chapter after {last}

TAGS
8 to 15 search phrases separated by commas, most specific first.

<captions>
{transcript}
</captions>
"""
    path = OUTPUT / f"{stem}_youtube_prompt.txt"
    path.write_text(prompt, encoding="utf-8")
    info(f"{path.relative_to(ROOT)}  (copy it into free ChatGPT, Gemini or Claude.ai; "
         f"{len(transcript.split())} words of captions)")
    return path


def make_youtube(stem: str, srt_text: str, duration: float, lecture_start: float, cfg: dict) -> Path:
    mode = str(cfg.get("youtube_mode") or "prompt").strip().lower()
    if mode == "prompt":
        say("\n[+] Writing the YouTube prompt (title, description, chapters)")
        return write_youtube_prompt(stem, srt_text, duration, lecture_start, cfg)
    if mode == "claude":
        say("\n[+] Writing YouTube title, description and chapters")
        return make_youtube_package(stem, srt_text, duration, lecture_start, cfg)
    raise EditError("config.yaml: youtube_mode must be 'prompt' (free) or 'claude' (paid API).")


def cmd_youtube(name: str, cfg: dict) -> Path:
    """Write (or rewrite) the YouTube prompt or text from an already finished video."""
    stem = Path(name).stem
    if stem.endswith("_final"):
        stem = stem[:-len("_final")]
    final, srt_out = OUTPUT / f"{stem}_final.mp4", OUTPUT / f"{stem}_final.srt"
    if not final.exists():
        raise EditError(f"{final.relative_to(ROOT)} not found. Finish the video first:  python edit.py finalize {stem}")
    if not srt_out.exists():
        raise EditError(f"{srt_out.relative_to(ROOT)} not found. Run finalize again with export_srt: on.")
    intro = resolve(cfg["intro_path"])
    lecture_start = probe(intro)["duration"] if intro.exists() else 0.0
    say(f"YouTube package for {stem}")
    return make_youtube(stem, srt_out.read_text(encoding="utf-8-sig", errors="replace"),
                        probe(final)["duration"], lecture_start, cfg)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def collect_videos(raws: list[str]) -> list[Path]:
    """Videos given directly, plus every video directly inside any folder given (sorted by name)."""
    videos: list[Path] = []
    for raw in raws:
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_dir():
            videos.append(check_input_video(raw))
            continue
        found = sorted((p for p in path.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
                       key=lambda p: p.name.lower())
        if not found:
            raise EditError(f"No videos found in {path} (expected: " + ", ".join(sorted(VIDEO_EXTENSIONS)) + ").")
        videos += found
    return videos


def is_batch(raws: list[str]) -> bool:
    return len(raws) > 1 or any(Path(r).is_dir() for r in raws)


def quoted(names: list[str]) -> str:
    return " ".join(f'"{n}"' if " " in n else n for n in names)


def run_batch(items: list, action, label, verb: str, many: bool) -> tuple[list, list[tuple[str, str]]]:
    """Run action on each item. In batch mode (many) one failure does not stop the rest."""
    done, failed = [], []
    for n, item in enumerate(items, 1):
        if many:
            say(f"\n{'=' * 70}\n  {verb} video {n} of {len(items)}: {label(item)}\n{'=' * 70}")
        try:
            done.append(action(item))
        except EditError as e:
            if not many:
                raise
            say(f"\nERROR ({label(item)}): {e}\n      Moving on to the next video.")
            failed.append((label(item), str(e).splitlines()[0]))
    return done, failed


def print_failures(failed: list[tuple[str, str]]) -> None:
    if failed:
        say(f"\n{len(failed)} video(s) failed:")
        for name, reason in failed:
            say(f"  - {name}: {reason}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="edit.py", description="Clean, speed up, caption and finalize lecture recordings.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="settings file (default: config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("process", help="clean audio, speed up silences, write captions for review")
    p.add_argument("video", nargs="+", help="lectures (input/lecture1.mp4) and/or folders of lectures (input/)")
    p = sub.add_parser("finalize", help="burn captions, add intro/outro, write output/<file>_final.mp4")
    p.add_argument("name", nargs="+", help="one or more lecture names, e.g. lecture1 lecture2")
    p.add_argument("--no-youtube", action="store_true", help="skip the YouTube title/description/chapters")
    p = sub.add_parser("all", help="process then finalize")
    p.add_argument("video", nargs="+", help="lectures (input/lecture1.mp4) and/or folders of lectures (input/)")
    p.add_argument("--no-review", action="store_true", help="do not pause to review the captions")
    p.add_argument("--no-youtube", action="store_true", help="skip the YouTube title/description/chapters")
    p.add_argument("--redo", action="store_true",
                   help="with a folder: also redo videos that already have output/<name>_final.mp4")
    p = sub.add_parser("youtube", help="write the YouTube prompt (or text) for finished videos")
    p.add_argument("name", nargs="+", help="one or more lecture names, e.g. lecture1 lecture2")
    p = sub.add_parser("clips", help="cut a long video into vertical short clips with captions")
    p.add_argument("video", nargs="+", help="videos (input/lecture1.mp4) and/or folders of videos (input/)")
    p.add_argument("--count", type=int, help="how many clips per video (default: clips.count in config.yaml)")
    p.add_argument("--model", help="Whisper model for this run, e.g. base or small")
    p.add_argument("--fresh", action="store_true", help="transcribe again instead of using the saved transcript")
    p.add_argument("--plan-only", action="store_true", help="pick the moments but do not render yet")
    p = sub.add_parser("render-clips", help="render clips from work/<file>_clips.json (after editing it)")
    p.add_argument("name", help="the video name, e.g. lecture1")
    p.add_argument("--only", help="render only these clip numbers, e.g. 1,3")
    p.add_argument("--model", help="Whisper model the transcript was made with, if not the config one")
    args = parser.parse_args()

    started = time.time()
    by_name = lambda v: v.name
    try:
        cfg = load_config(Path(args.config))
        if args.command in {"clips", "render-clips"}:
            sys.modules.setdefault("edit", sys.modules[__name__])   # clips.py imports helpers from here
            import clips
            if args.command == "render-clips":
                clips.cmd_render_clips(args.name, cfg, args.only, args.model)
                say(f"Total time: {fmt_duration(time.time() - started)}")
                return 0
            _, failed = run_batch(
                collect_videos(args.video),
                lambda v: clips.cmd_clips(v, cfg, args.count, args.model, args.fresh, args.plan_only),
                by_name, "Clips for", is_batch(args.video))
            say(f"Total time: {fmt_duration(time.time() - started)}")
            print_failures(failed)
            return 1 if failed else 0

        if args.command == "process":
            stems, failed = run_batch(collect_videos(args.video), lambda v: cmd_process(v, cfg), by_name,
                                      "Processing", is_batch(args.video))
            if stems:
                say(f"\nStopped for review ({fmt_duration(time.time() - started)} so far).")
                say("  1. Open these caption files in a text editor and fix any mistakes:")
                for stem in stems:
                    say(f"       work/{stem}.srt")
                say(f"  2. Then run:  python edit.py finalize {quoted(stems)}")
            print_failures(failed)
            return 1 if failed else 0

        if args.command == "finalize":
            _, failed = run_batch(args.name, lambda n: cmd_finalize(n, cfg, not args.no_youtube), str,
                                  "Finalizing", len(args.name) > 1)
            print_failures(failed)
            return 1 if failed else 0

        if args.command == "youtube":
            _, failed = run_batch(args.name, lambda n: cmd_youtube(n, cfg), str, "YouTube for", len(args.name) > 1)
            print_failures(failed)
            return 1 if failed else 0

        videos = collect_videos(args.video)
        many = is_batch(args.video)
        if many and any(Path(r).is_dir() for r in args.video) and not args.redo:
            finished = [v for v in videos if (OUTPUT / f"{v.stem}_final.mp4").exists()]
            if finished:
                say(f"Skipping {len(finished)} video(s) already in output/ (use --redo to make them again): "
                    + ", ".join(v.name for v in finished))
                videos = [v for v in videos if v not in finished]
            if not videos:
                say("Nothing left to do.")
                return 0
        stems, failed = run_batch(videos, lambda v: cmd_process(v, cfg), by_name, "Processing", many)
        if stems and not args.no_review:
            files = ", ".join(f"work/{s}.srt" for s in stems)
            say(f"\nReview {files} now, save, then press Enter to continue (Ctrl+C to stop).")
            input()
        _, failed_final = run_batch(stems, lambda n: cmd_finalize(n, cfg, not args.no_youtube), str,
                                    "Finalizing", many)
        failed += failed_final
        say(f"\nTotal time: {fmt_duration(time.time() - started)}")
        if many:
            say(f"Finished {len(stems) - len(failed_final)} of {len(videos)} video(s).")
        print_failures(failed)
        return 1 if failed else 0
    except EditError as e:
        say(f"\nERROR: {e}")
        return 1
    except KeyboardInterrupt:
        say("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
