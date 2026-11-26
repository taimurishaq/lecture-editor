"""Cut a long video into vertical short clips with word-by-word captions.

Used by edit.py:
    python edit.py clips input/<file>.mp4            # transcribe, pick moments, render
    python edit.py clips input/<file>.mp4 --plan-only
    python edit.py render-clips <file> [--only 1,3]  # render from work/<file>_clips.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from edit import (ROOT, WORK, OUTPUT, EditError, say, step, info, warn, fmt_duration,
                  is_on, probe, run_ffmpeg, filter_path, require_ffmpeg, ass_color)

CLIP_DEFAULTS = {
    "count": 5,
    "min_seconds": 20,
    "max_seconds": 60,
    "caption_task": "translate",
    "picker": "auto",
    "gemini_model": "gemini-2.5-flash",
    "layout": "blur",
    "crop_position": "center",
    "cut_pauses_over": 0.8,
    "pause_noise_db": -30,
    "title_overlay": True,
    "loudness_target": -14,
    "default_hashtags": ["#shorts"],
    "brand": {                      # used by layout: branded (navy card with name, tag and big title)
        "name": "",
        "tag": "",
        "background": "#0b1b3a",
        "accent": "#c9a24b",
        "name_font": "Georgia",
    },
    "captions": {
        "font": "Arial",
        "size": 82,
        "words_per_line": 3,
        "uppercase": True,
        "color": "white",
        "highlight_color": "yellow",
        "outline_color": "black",
        "outline_width": 6,
        "position_from_bottom": 480,
    },
}

W, H = 1080, 1920
HOOK_WORDS = {
    "important", "remember", "mistake", "mistakes", "never", "always", "secret", "trick", "tip",
    "tips", "exam", "examiner", "marks", "easy", "simple", "why", "how", "what", "wrong", "common",
    "best", "worst", "must", "rule", "formula", "shortcut", "actually", "problem", "students",
    "note", "careful", "difference", "example", "first", "step", "key",
}


def clip_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(CLIP_DEFAULTS))
    for key, value in (cfg.get("clips") or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    saved = ROOT / "caption_style.json"       # written by the "Clip style" window in the app
    if saved.exists():
        try:
            data = json.loads(saved.read_text(encoding="utf-8"))
            if "captions" in data or "look" in data:
                out["captions"].update(data.get("captions") or {})
                look = dict(data.get("look") or {})
                if look.get("layout"):
                    out["layout"] = look["layout"]
                out["brand"].update({k: look[k] for k in ("name", "tag") if k in look})
            else:
                out["captions"].update(data)
        except ValueError:
            warn("caption_style.json is damaged, so the settings in config.yaml are used.")
    if float(out["min_seconds"]) >= float(out["max_seconds"]):
        raise EditError("config.yaml: clips.min_seconds must be smaller than clips.max_seconds.")
    if str(out["layout"]).lower() not in {"blur", "crop", "branded"}:
        raise EditError("config.yaml: clips.layout must be 'blur', 'crop' or 'branded'.")
    return out


# ----------------------------------------------------------------------------
# Transcript with word timings (cached in work/<stem>_words.json)
# ----------------------------------------------------------------------------

def transcribe(src: Path, cfg: dict, cc: dict, model_name: str, fresh: bool) -> list[dict]:
    cache = WORK / f"{src.stem}_words.json"
    task = str(cc["caption_task"]).lower()
    if task not in {"translate", "transcribe"}:
        raise EditError("config.yaml: clips.caption_task must be 'translate' or 'transcribe'.")
    key = {"source": str(src.resolve()), "size": src.stat().st_size, "model": model_name, "task": task}
    if cache.exists() and not fresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key:
                info(f"Using the saved transcript {cache.relative_to(ROOT)} (add --fresh to redo it).")
                return data["segments"]
        except (ValueError, KeyError):
            pass

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise EditError("faster-whisper is not installed. Run:  pip install -r requirements.txt")
    language = str(cfg.get("speech_language") or "auto").lower()
    duration = probe(src)["duration"]
    info(f"Loading Whisper model '{model_name}' on CPU (the first run downloads it)...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    info("Writing English captions..." if task == "translate" else "Transcribing speech...")
    segments, _ = model.transcribe(
        str(src), task=task, language=None if language == "auto" else language,
        vad_filter=True, beam_size=5, condition_on_previous_text=False, word_timestamps=True,
    )
    out: list[dict] = []
    last_pct, started = -1, time.time()
    for seg in segments:
        words = [[round(w.start, 3), round(w.end, 3), w.word.strip()] for w in (seg.words or []) if w.word.strip()]
        if not words and seg.text.strip():
            parts = seg.text.split()
            step_len = (seg.end - seg.start) / max(1, len(parts))
            words = [[round(seg.start + i * step_len, 3), round(seg.start + (i + 1) * step_len, 3), p]
                     for i, p in enumerate(parts)]
        if words:
            out.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                        "text": seg.text.strip(), "words": words})
        if duration:
            pct = min(100, int(seg.end / duration * 100))
            if pct != last_pct:
                last_pct = pct
                print(f"\r      Transcribing: {pct}%   ", end="", flush=True)
    if last_pct >= 0:
        print(f"\r      Transcribing: done in {fmt_duration(time.time() - started)}      ", flush=True)
    WORK.mkdir(exist_ok=True)
    cache.write_text(json.dumps({"key": key, "segments": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    info(f"Saved transcript to {cache.relative_to(ROOT)}")
    return out


def all_words(segments: list[dict]) -> list[tuple[float, float, str]]:
    return [(w[0], w[1], w[2]) for s in segments for w in s["words"]]


# ----------------------------------------------------------------------------
# Picking the moments
# ----------------------------------------------------------------------------

def gemini_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    path = ROOT / "gemini_key.txt"
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    return None


def pick_with_gemini(segments: list[dict], cc: dict, count: int, key: str) -> list[dict]:
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments]
    prompt = (
        "You are an expert short-form video editor. Below is a timestamped transcript of a long video "
        "(times in seconds). Pick the {n} best self-contained moments to post as YouTube Shorts, "
        "Instagram Reels and TikToks.\n\n"
        "Rules:\n"
        "- Each clip is between {lo} and {hi} seconds long.\n"
        "- Start where a sentence starts, ideally on a strong hook (a question, a bold claim, a common "
        "mistake, a surprising fact). End on a complete thought.\n"
        "- The clip must make sense to someone who has not seen the rest of the video.\n"
        "- Prefer clear tips, step-by-step explanations, mistakes to avoid, exam advice, stories and strong opinions.\n"
        "- Clips must not overlap.\n\n"
        "Return only JSON in this shape:\n"
        '{{"clips":[{{"start":12.3,"end":55.0,"title":"hook title, max 60 characters",'
        '"description":"one or two sentences for the post","hashtags":["#example"],'
        '"score":87,"reason":"why this works"}}]}}\n\n'
        "Transcript:\n{t}"
    ).format(n=count, lo=int(cc["min_seconds"]), hi=int(cc["max_seconds"]), t="\n".join(lines))
    model = str(cc["gemini_model"])
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        clips = json.loads(text)
        clips = clips.get("clips", clips) if isinstance(clips, dict) else clips
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise EditError(f"Gemini returned HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise EditError(f"Could not reach Gemini: {e}")
    except (KeyError, IndexError, ValueError) as e:
        raise EditError(f"Gemini's answer could not be read ({e}).")
    if not isinstance(clips, list):
        raise EditError("Gemini's answer did not contain a list of clips.")
    out = []
    for c in clips:
        try:
            out.append({
                "start": float(c["start"]), "end": float(c["end"]),
                "title": str(c.get("title", "")).strip()[:80],
                "description": str(c.get("description", "")).strip(),
                "hashtags": [str(h) for h in c.get("hashtags", [])][:8],
                "score": int(float(c.get("score", 0))),
                "reason": str(c.get("reason", "")).strip(),
                "picked_by": "gemini",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


PICK_RULES = (
    "Pick the {n} best self-contained moments of this video to post as YouTube Shorts, Instagram Reels "
    "and TikToks.\n"
    "- Each clip is between {lo} and {hi} seconds long (times in the transcript are in seconds).\n"
    "- Start where a sentence starts, ideally on a strong hook: a question, a bold claim, a common "
    "mistake, a surprising fact. End on a complete thought.\n"
    "- The clip must make sense to someone who has not seen the rest of the video.\n"
    "- Prefer clear tips, step-by-step explanations, mistakes to avoid, exam advice, stories and strong opinions.\n"
    "- Clips must not overlap.\n"
    "- title: a hook for the top of the clip, under 60 characters, no emoji.\n"
    "- description: one or two sentences to post with the clip.\n"
    "- hashtags: 3 to 6, each starting with #.\n"
    "- score: 0-100, how likely the clip is to do well.\n"
    "The captions may be machine-translated from mixed Urdu and English speech; ignore small wording errors."
)


def pick_with_claude(segments: list[dict], cc: dict, count: int, cfg: dict) -> list[dict]:
    """Ask Claude for the moments. Raises LookupError when no Claude credentials are set up."""
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        raise LookupError("anthropic package not installed")

    class Clip(BaseModel):
        start: float
        end: float
        title: str
        description: str
        hashtags: list[str]
        score: int
        reason: str

    class ClipPicks(BaseModel):
        clips: list[Clip]

    lines = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)
    rules = PICK_RULES.format(n=count, lo=int(cc["min_seconds"]), hi=int(cc["max_seconds"]))
    about = str(cfg.get("youtube_channel_info") or "").strip()
    model = str(cc.get("claude_model") or cfg.get("youtube_model") or "claude-opus-5-5")
    info(f"Trying Claude ({model}) to pick the best {count} moments...")
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            system="You are an expert short-form video editor." + (f" About the channel: {about}" if about else ""),
            messages=[{"role": "user", "content": f"<transcript>\n{lines}\n</transcript>\n\n{rules}"}],
            output_format=ClipPicks,
            output_config={"effort": "medium"},
            # If the model declines (rare), the API retries on a suitable model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise EditError("Claude rejected the API key. Check ANTHROPIC_API_KEY.")
    except anthropic.NotFoundError:
        raise EditError(f"Unknown Claude model '{model}' (clips.claude_model in config.yaml).")
    except anthropic.RateLimitError:
        raise EditError("Claude is rate-limiting this key right now.")
    except anthropic.APIStatusError as e:
        raise EditError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise EditError("Could not reach the Claude API (check the internet connection).")
    except (anthropic.AnthropicError, TypeError) as e:
        # With no key at all the SDK raises TypeError("Could not resolve authentication method ...").
        if isinstance(e, TypeError) and "authentication" not in str(e):
            raise
        raise LookupError("no Claude API key")
    if response.stop_reason == "refusal":
        raise EditError("Claude declined to pick clips for this video.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise EditError("Claude's answer was cut off or unreadable.")
    return [{
        "start": c.start, "end": c.end, "title": c.title.strip()[:80],
        "description": c.description.strip(), "hashtags": c.hashtags[:8],
        "score": c.score, "reason": c.reason.strip(), "picked_by": "claude",
    } for c in response.parsed_output.clips]


def first_sentence(text: str, limit: int = 60) -> str:
    text = text.strip()
    m = re.match(r"(.+?[.?!])(\s|$)", text)
    sentence = m.group(1) if m else text
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return sentence


# Words that mean "this is teaching content" or "this is chatter", used by the no-AI picker.
EMPHASIS = {"remember", "important", "mistake", "mistakes", "never", "always", "secret", "trick", "tip",
            "exam", "examiner", "marks", "formula", "rule", "shortcut", "common", "wrong", "key", "note",
            "careful", "difference", "example", "because", "therefore", "means", "definition", "step"}
CHATTER = {"salam", "assalam", "assalamualaikum", "hello", "welcome", "everyone", "okay", "ok", "hmm",
           "um", "uh", "umm", "hmmm", "haan", "acha", "achha", "chalo", "toh", "tou"}
CHATTER_PHRASES = ("can you hear", "hear me", "welcome to", "today we", "today i", "let's start", "lets start",
                   "let us start", "last class", "last time", "thank you", "thanks for", "see you", "next class",
                   "next lecture", "homework", "any question", "any doubt", "subscribe", "share screen",
                   "open your", "turn to page", "page number", "take out", "attendance")
WEAK_STARTS = {"and", "so", "but", "or", "because", "also", "then", "which", "that", "this", "it", "he", "she",
               "they", "these", "those", "its", "there", "thus", "however", "yes", "no"}


def _norm(word: str) -> str:
    return re.sub(r"[^a-z]", "", word.lower())


def pick_locally(segments: list[dict], cc: dict, count: int) -> list[dict]:
    """Score every sentence-aligned window and keep the best ones, spread across the whole video.

    No AI is used. A good window starts at a fresh sentence, teaches something (key words, numbers,
    examples), is spoken at a steady pace without many fillers, and is not the greeting, the
    class admin or the sign-off. Chosen clips are kept apart so they do not all come from one place.
    """
    lo, hi = float(cc["min_seconds"]), float(cc["max_seconds"])
    target = (lo + hi) / 2
    total = segments[-1]["end"] if segments else 0.0
    edge = min(90.0, total * 0.04) if total > 600 else 0.0       # skip the greeting and the sign-off

    def chatter_hits(seg: dict) -> int:
        low = seg["text"].lower()
        return sum(p in low for p in CHATTER_PHRASES)

    candidates = []
    for i, first in enumerate(segments):
        if first["start"] < edge or (total and first["end"] > total - edge):
            continue
        prev_text = segments[i - 1]["text"].rstrip() if i else "."
        fresh_start = prev_text.endswith((".", "?", "!", "؟", "۔")) or i == 0
        fw = [_norm(w[2]) for w in first["words"][:3]]
        weak = bool(fw) and fw[0] in WEAK_STARTS
        speech, words, emph, filler, chat, numbers = 0.0, 0, 0, 0, 0, 0
        uniq: set[str] = set()
        parts: list[dict] = []
        for j in range(i, len(segments)):
            seg = segments[j]
            dur = seg["end"] - first["start"]
            if dur > hi or (total and seg["end"] > total - edge):
                break
            parts.append(seg)
            speech += sum(w[1] - w[0] for w in seg["words"])
            words += len(seg["words"])
            chat += chatter_hits(seg)
            for w in seg["words"]:
                n = _norm(w[2])
                uniq.add(n)
                emph += n in EMPHASIS or n in HOOK_WORDS
                filler += n in CHATTER
                numbers += any(ch.isdigit() for ch in w[2])
            if dur < lo:
                continue
            density = min(1.0, speech / dur)
            wps = words / dur
            pace = max(0.0, 1.0 - abs(wps - 2.6) / 2.6)               # steady, not slow, not rushed
            variety = min(1.0, len(uniq) / max(1, words) / 0.55)       # repeated words = rambling
            teach = min(1.0, (emph + min(numbers, 4)) / max(3.0, dur / 6))
            filler_rate = filler / max(1, words)
            hook = 1.0 if ("?" in first["text"] or any(_norm(w[2]) in HOOK_WORDS for w in first["words"][:6])) else 0.0
            clean_end = 1.0 if seg["text"].rstrip().endswith((".", "?", "!", "؟", "۔")) else 0.0
            closeness = 1.0 - abs(dur - target) / (hi - lo)
            score = (25 * density + 10 * pace + 12 * variety + 25 * teach + 10 * hook
                     + 10 * clean_end + 8 * (1.0 if fresh_start else 0.0) + 4 * closeness)
            score -= 40 * min(1.0, filler_rate / 0.12)                 # lots of "um", "okay", "acha"
            score -= 15 * min(chat, 3)                                  # greetings, homework, sign-offs
            score -= 12 if weak else 0                                   # starts mid-thought ("and so...")
            candidates.append((score, first["start"], seg["end"], parts[:]))
    candidates.sort(key=lambda c: -c[0])
    gap = 20.0
    chosen: list[tuple] = []
    for cand in candidates:
        if all(cand[2] + gap <= c[1] or cand[1] >= c[2] + gap for c in chosen):
            chosen.append(cand)
        if len(chosen) >= count:
            break
    if len(chosen) < count:                                              # short video: allow touching clips
        for cand in candidates:
            if cand not in chosen and all(cand[2] <= c[1] or cand[1] >= c[2] for c in chosen):
                chosen.append(cand)
            if len(chosen) >= count:
                break

    def best_sentence(parts: list[dict]) -> str:
        """Title: the sentence in the clip that sounds most like a teaching point."""
        best, best_score = parts[0]["text"], -1.0
        for seg in parts:
            toks = [_norm(w[2]) for w in seg["words"]]
            if len(toks) < 4:
                continue
            sc = sum(t in EMPHASIS or t in HOOK_WORDS for t in toks) + (2 if "?" in seg["text"] else 0)
            sc -= 3 * sum(t in CHATTER for t in toks)
            sc -= 1 if toks[0] in WEAK_STARTS else 0
            if sc > best_score:
                best, best_score = seg["text"], sc
        return best

    out = []
    for c in chosen:
        text = " ".join(p["text"] for p in c[3])
        out.append({
            "start": c[1], "end": c[2], "title": first_sentence(best_sentence(c[3])),
            "description": first_sentence(text, 200), "hashtags": list(cc["default_hashtags"]),
            "score": max(1, min(100, int(round(c[0])))),
            "reason": "teaching words, steady speech, clean start (no AI)", "picked_by": "local",
        })
    return out


def snap(clip: dict, words: list[tuple[float, float, str]], cc: dict, duration: float) -> dict | None:
    """Move a clip's edges onto word boundaries and keep it within min/max length."""
    lo, hi = float(cc["min_seconds"]), float(cc["max_seconds"])
    inside = [w for w in words if w[1] > clip["start"] - 0.5 and w[0] < clip["end"] + 0.5]
    if not inside:
        return None
    start = inside[0][0]
    end_words = [w for w in inside if w[1] - start <= hi]
    end = end_words[-1][1] if end_words else inside[0][1]
    if end - start < lo:                        # too short: extend with following words
        later = [w for w in words if w[0] >= end and w[1] - start <= hi]
        for w in later:
            end = w[1]
            if end - start >= lo:
                break
    clip = dict(clip)
    clip["start"] = round(max(0.0, start - 0.15), 2)
    clip["end"] = round(min(duration, end + 0.35), 2)
    return clip


def make_plan(src: Path, segments: list[dict], cc: dict, count: int, cfg: dict) -> dict:
    duration = probe(src)["duration"]
    words = all_words(segments)
    if not words:
        raise EditError("No speech was found in this video, so there is nothing to cut into clips.")
    picker = str(cc["picker"]).lower()
    if picker not in {"auto", "claude", "gemini", "local"}:
        raise EditError("config.yaml: clips.picker must be auto, claude, gemini or local.")
    key = gemini_key()
    clips: list[dict] = []
    if picker in {"auto", "claude"}:
        try:
            clips = pick_with_claude(segments, cc, count, cfg)
        except LookupError:
            if picker == "claude":
                raise EditError("clips.picker is 'claude' but no Claude API key was found. Set ANTHROPIC_API_KEY.")
        except EditError as e:
            if picker == "claude":
                raise
            warn(f"{e}\n      Trying the next picker.")
    if clips:
        pass
    elif picker in {"auto", "gemini"} and key:
        info(f"Asking Gemini ({cc['gemini_model']}) to pick the best {count} moments...")
        try:
            clips = pick_with_gemini(segments, cc, count, key)
        except EditError as e:
            if picker == "gemini":
                raise
            warn(f"{e}\n      Falling back to the built-in picker.")
    elif picker == "gemini":
        raise EditError("clips.picker is 'gemini' but no key was found. Set GEMINI_API_KEY or create gemini_key.txt.")
    if not clips:
        if picker != "local":
            info("No AI key found (ANTHROPIC_API_KEY, GEMINI_API_KEY or gemini_key.txt), "
                 "so the built-in picker is used.")
        clips = pick_locally(segments, cc, count)

    snapped = [c for c in (snap(c, words, cc, duration) for c in clips) if c]
    snapped.sort(key=lambda c: -c.get("score", 0))
    final: list[dict] = []
    for c in snapped:                           # drop overlaps, keep the higher score
        if all(c["end"] <= f["start"] or c["start"] >= f["end"] for f in final):
            final.append(c)
    final = sorted(final[:count], key=lambda c: c["start"])
    for n, c in enumerate(final, 1):
        c["n"] = n
    return {"source": str(src.resolve()), "clips": final}


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

def detect_silences(src: Path, start: float, end: float, min_pause: float, noise_db: float) -> list[tuple[float, float]]:
    """Silences inside [start, end], in seconds relative to start."""
    ffmpeg, _ = require_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
           "-vn", "-af", f"silencedetect=noise={noise_db:g}dB:d={min_pause:g}", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    silences, open_at = [], None
    for line in result.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            open_at = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and open_at is not None:
            silences.append((open_at, float(m.group(1))))
            open_at = None
    if open_at is not None:
        silences.append((open_at, end - start))
    return silences


def keep_ranges(length: float, silences: list[tuple[float, float]], pad: float = 0.2) -> list[tuple[float, float]]:
    """The parts to keep once each silence (shrunk by pad on both sides) is removed, on a 1/30 s grid."""
    grid = lambda t: round(t * 30) / 30
    keep, pos = [], 0.0
    for a, b in silences:
        a, b = a + pad, b - pad
        if b - a < 0.2:
            continue
        if a > pos:
            keep.append((grid(pos), grid(a)))
        pos = b
    if pos < length:
        keep.append((grid(pos), grid(length)))
    return [(a, b) for a, b in keep if b - a >= 1 / 30]


def remap(t: float, keep: list[tuple[float, float]]) -> float:
    """Clip-relative time -> time in the output after pauses are removed."""
    out = 0.0
    for a, b in keep:
        if t < a:
            return out
        if t <= b:
            return out + (t - a)
        out += b - a
    return out


def ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(words: list[tuple[float, float, str]], title: str, total: float, cc: dict, layout: str) -> str:
    cap = cc["captions"]
    upper = is_on(cap.get("uppercase", True))
    color, hl = ass_color(cap["color"]), ass_color(cap["highlight_color"])
    outline = ass_color(cap["outline_color"])
    hl_inline = "&H" + hl[4:] + "&"            # \c override takes &HBBGGRR&
    font = re.sub(r"[,:']", "", str(cap["font"]))
    size = int(float(cap["size"]))
    bold = -1 if is_on(cap.get("bold", True)) else 0
    pop = is_on(cap.get("highlight_word", True))
    margin_v = int(cap["position_from_bottom"])
    title_margin = 300 if layout == "blur" else 170
    branded = layout == "branded"
    brand = cc.get("brand") or {}
    accent = ass_color(brand.get("accent", "#c9a24b"))
    if branded:
        margin_v = 360                           # captions sit under the video card
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},{color},{color},{outline},&H80000000,{bold},0,0,0,100,100,0,0,1,"
        f"{float(cap['outline_width']):g},3,2,70,70,{margin_v},1",
        f"Style: Title,{font},62,&H00FFFFFF,&H00FFFFFF,&H50000000,&H00000000,-1,0,0,0,100,100,0,0,3,"
        f"16,0,8,90,90,{title_margin},1",
    ]
    if branded:
        bfont = re.sub(r"[,:']", "", str(brand.get("name_font", "Georgia")))
        lines += [
            f"Style: Brand,{bfont},60,{accent},{accent},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,8,60,60,110,1",
            f"Style: Tag,{font},34,{accent},{accent},&H00000000,&H00000000,-1,0,0,0,100,100,5,0,1,0,0,8,60,60,250,1",
            f"Style: Head,{font},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,8,70,70,330,1",
        ]
    lines += ["", "[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if branded:
        if brand.get("name"):
            lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Brand,,0,0,0,,{ass_text(str(brand['name']))}")
        if brand.get("tag"):
            lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Tag,,0,0,0,,{ass_text(str(brand['tag']).upper())}")
        if title:
            lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Head,,0,0,0,,{ass_text(str(title).upper())}")
    elif title and is_on(cc.get("title_overlay", True)):
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Title,,0,0,0,,{ass_text(title)}")

    # Group words into short lines; a pause or the end of a sentence also starts a new line.
    per = max(1, int(cap.get("words_per_line", 3)))
    groups: list[list[tuple[float, float, str]]] = [[]]
    for w in words:
        g = groups[-1]
        if g and (len(g) >= per or w[0] - g[-1][1] > 0.6 or g[-1][2].endswith((".", "?", "!"))):
            groups.append([])
        groups[-1].append(w)
    groups = [g for g in groups if g]

    def show(word: str) -> str:
        word = word.strip(",;:\"")
        word = word.rstrip(".") if not word.endswith("...") else word
        return ass_text(word.upper() if upper else word)

    if not is_on(cap.get("show", True)):
        groups = []
    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0][0] if gi + 1 < len(groups) else min(total, g[-1][1] + 0.4)
        g_end = min(g_end, g[-1][1] + 0.6)
        for i, w in enumerate(g):
            a = g[0][0] if i == 0 else w[0]
            b = g[i + 1][0] if i + 1 < len(g) else g_end
            if b - a < 0.02:
                continue
            parts = []
            for k, x in enumerate(g):
                if k == i and pop:
                    parts.append(f"{{\\c{hl_inline}\\fscx112\\fscy112}}{show(x[2])}{{\\r}}")
                else:
                    parts.append(show(x[2]))
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def crop_x(cc: dict) -> float:
    pos = str(cc.get("crop_position", "center")).lower()
    named = {"left": 0.0, "center": 0.5, "centre": 0.5, "right": 1.0}
    if pos in named:
        return named[pos]
    try:
        return max(0.0, min(1.0, float(pos)))
    except ValueError:
        raise EditError("config.yaml: clips.crop_position must be left, center, right or a number from 0 to 1.")


def render_clip(src: Path, clip: dict, segments: list[dict], cc: dict, cfg: dict, dest: Path) -> float:
    facts = probe(src)
    start, end = float(clip["start"]), float(clip["end"])
    length = end - start
    min_pause = float(cc["cut_pauses_over"] or 0)
    keep = [(0.0, round(length * 30) / 30)]
    if min_pause > 0:
        silences = detect_silences(src, start, end, min_pause, float(cc["pause_noise_db"]))
        keep = keep_ranges(length, silences) or keep
    total = sum(b - a for a, b in keep)

    words = []
    for w in all_words(segments):
        if w[1] <= start or w[0] >= end:
            continue
        a, b = remap(max(0.0, w[0] - start), keep), remap(min(length, w[1] - start), keep)
        if b - a < 0.05:
            b = a + 0.05
        words.append((a, b, w[2]))

    layout = str(cc["layout"]).lower()
    vertical = facts["height"] > facts["width"]
    if layout == "crop" and facts["width"] < facts["height"] * 9 / 16:
        layout = "blur"
    ass_path = WORK / "_clip.ass"
    ass_path.write_text(build_ass(words, str(clip.get("title", "")), total, cc, layout), encoding="utf-8-sig")
    subs = f"subtitles={filter_path(ass_path)}"

    # Remove pauses: trim each kept piece (sample-accurate for audio) and join them.
    chains = []
    if len(keep) > 1:
        k = len(keep)
        chains.append(f"[0:v:0]fps=30,split={k}" + "".join(f"[sv{i}]" for i in range(k)))
        chains.append(f"[0:a:0]asplit={k}" + "".join(f"[sa{i}]" for i in range(k)))
        for i, (a, b) in enumerate(keep):
            chains.append(f"[sv{i}]trim={a:.4f}:{b:.4f},setpts=PTS-STARTPTS[tv{i}]")
            chains.append(f"[sa{i}]atrim={a:.4f}:{b:.4f},asetpts=PTS-STARTPTS[ta{i}]")
        chains.append("".join(f"[tv{i}][ta{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[vsrc][asrc]")
        vin, ain = "[vsrc]", "[asrc]"
    else:
        chains.append("[0:v:0]fps=30[vsrc]")
        vin, ain = "[vsrc]", "[0:a:0]"

    if layout == "branded":
        brand = cc.get("brand") or {}
        bgc = str(brand.get("background", "#0b1b3a")).lstrip("#")
        ac = str(brand.get("accent", "#c9a24b")).lstrip("#")
        chains.append(f"{vin}split[bg0][fg0]")
        chains.append(f"[bg0]scale={W}:{H},drawbox=x=0:y=0:w=iw:h=ih:color=0x{bgc}:t=fill,"
                      f"drawbox=x=({W}-220)/2:y=222:w=220:h=4:color=0x{ac}:t=fill[bg]")
        chains.append(f"[fg0]scale=1000:640:force_original_aspect_ratio=decrease[fg]")
        chains.append(f"[bg][fg]overlay=(W-w)/2:700+(640-h)/2,setsar=1,format=yuv420p,{subs}[v]")
    elif vertical:
        chains.append(f"{vin}scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,{subs}[v]")
    elif layout == "crop":
        x = crop_x(cc)
        chains.append(f"{vin}crop=ih*9/16:ih:(iw-ih*9/16)*{x:g}:0,scale={W}:{H},"
                      f"setsar=1,format=yuv420p,{subs}[v]")
    else:
        chains.append(f"{vin}split[bg0][fg0]")
        chains.append(f"[bg0]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
                      f"scale={W}:{H},eq=brightness=-0.12[bg]")
        chains.append(f"[fg0]scale={W}:-2[fg]")
        chains.append(f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p,{subs}[v]")
    rate = int(cfg["output"]["audio_rate"])
    chains.append(f"{ain}highpass=f=70,"
                  f"loudnorm=I={float(cc['loudness_target']):g}:TP=-1.5:LRA=11,aresample={rate}[a]")
    graph = ";".join(chains)

    out = cfg["output"]
    run_ffmpeg(
        ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         "-c:v", str(out["video_codec"]), "-crf", str(out["crf"]), "-preset", str(out["preset"]),
         "-pix_fmt", "yuv420p", "-r", "30",
         "-c:a", "aac", "-b:a", str(out["audio_bitrate"]), "-ar", str(rate), "-ac", "2",
         "-movflags", "+faststart", str(dest)],
        total, f"Clip {clip['n']}",
    )
    ass_path.unlink(missing_ok=True)
    return total


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].rstrip("-") or "clip"


def plan_path(stem: str) -> Path:
    return WORK / f"{stem}_clips.json"


def print_plan(plan: dict) -> None:
    for c in plan["clips"]:
        info(f"{c['n']}. {fmt_duration(c['start'])} - {fmt_duration(c['end'])} "
             f"({c['end'] - c['start']:.0f}s, score {c.get('score', 0)})  {c.get('title', '')}")


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def cmd_clips(src: Path, cfg: dict, count: int | None, model: str | None, fresh: bool, plan_only: bool,
              picker: str | None = None) -> None:
    require_ffmpeg()
    cc = clip_config(cfg)
    if picker:
        cc["picker"] = picker
    count = int(count or cc["count"])
    WORK.mkdir(exist_ok=True)
    facts = probe(src)
    say(f"Making short clips from {src.name}  ({fmt_duration(facts['duration'])}, "
        f"{facts['width']}x{facts['height']})")

    step(1, 3, "Transcribing with word timings")
    segments = transcribe(src, cfg, cc, model or str(cfg["whisper_model"]), fresh)

    step(2, 3, f"Picking the best {count} moments")
    plan = make_plan(src, segments, cc, count, cfg)
    if not plan["clips"]:
        raise EditError("No clip-sized moments were found. Try lowering clips.min_seconds in config.yaml.")
    path = plan_path(src.stem)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print_plan(plan)
    info(f"Plan saved to {path.relative_to(ROOT)}")
    if plan_only:
        say(f"\nStopped before rendering. Edit {path.relative_to(ROOT)} if you like "
            f"(start, end, title), then run:\n  python edit.py render-clips {src.stem}")
        return

    step(3, 3, "Rendering vertical clips with captions")
    render_plan(src.stem, plan, segments, cc, cfg, None)


def cmd_render_clips(name: str, cfg: dict, only: str | None, model: str | None) -> None:
    require_ffmpeg()
    cc = clip_config(cfg)
    stem = Path(name).stem
    path = plan_path(stem)
    if not path.exists():
        raise EditError(f"{path.relative_to(ROOT)} not found.\n"
                        f"      Run this first:  python edit.py clips input/{stem}.mp4")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise EditError(f"{path.name} is not valid JSON any more ({e}). Check your edits.")
    src = Path(plan["source"])
    if not src.exists():
        raise EditError(f"The source video in the plan no longer exists: {src}")
    segments = transcribe(src, cfg, cc, model or str(cfg["whisper_model"]), False)
    wanted = None
    if only:
        try:
            wanted = {int(x) for x in only.split(",") if x.strip()}
        except ValueError:
            raise EditError("--only takes clip numbers separated by commas, e.g. --only 1,3")
    render_plan(stem, plan, segments, cc, cfg, wanted)


def render_plan(stem: str, plan: dict, segments: list[dict], cc: dict, cfg: dict, wanted: set[int] | None) -> None:
    src = Path(plan["source"])
    folder = OUTPUT / f"{stem}_clips"
    folder.mkdir(parents=True, exist_ok=True)
    if wanted is None:                          # full run: clear clips left over from an earlier plan
        for old in folder.glob("[0-9][0-9]_*.mp4"):
            old.unlink(missing_ok=True)
    started = time.time()
    made = []
    for c in plan["clips"]:
        if wanted and c["n"] not in wanted:
            continue
        if float(c["end"]) <= float(c["start"]):
            warn(f"Clip {c['n']} ends before it starts; skipped.")
            continue
        for old in folder.glob(f"{c['n']:02d}_*.mp4"):
            old.unlink(missing_ok=True)
        dest = folder / f"{c['n']:02d}_{slug(c.get('title', ''))}.mp4"
        length = render_clip(src, c, segments, cc, cfg, dest)
        made.append((c, dest, length))

    notes = folder / "clips.txt"
    with open(notes, "w", encoding="utf-8") as f:
        for c in plan["clips"]:
            files = sorted(folder.glob(f"{c['n']:02d}_*.mp4"))
            f.write(f"Clip {c['n']}: {files[0].name if files else '(not rendered)'}\n")
            f.write(f"  From {fmt_duration(c['start'])} to {fmt_duration(c['end'])} of the original\n")
            f.write(f"  Title: {c.get('title', '')}\n")
            if c.get("description"):
                f.write(f"  Caption: {c['description']}\n")
            if c.get("hashtags"):
                f.write(f"  Hashtags: {' '.join(c['hashtags'])}\n")
            f.write("\n")
    say(f"\nDone in {fmt_duration(time.time() - started)}. {len(made)} clip(s) in {folder}")
    for c, dest, length in made:
        info(f"{dest.name}  ({length:.0f}s)")
    info(f"Titles, captions and hashtags: {notes.name}")
