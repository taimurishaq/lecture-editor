#!/usr/bin/env python
"""lecture-editor window: pick lectures, press Start, watch the progress.

Double-click "Lecture Editor.bat" to open it. It runs edit.py underneath, so every
setting still comes from config.yaml and the command line keeps working as before.
"""
from __future__ import annotations

import codecs
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent
EDIT = ROOT / "edit.py"
WORK, OUTPUT, INPUT = ROOT / "work", ROOT / "output", ROOT / "input"
VIDEO_TYPES = [("Videos", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PCT = re.compile(r"(\d{1,3}(?:\.\d)?)%")
VIDEO_N = re.compile(r"(Processing|Finalizing|YouTube for|Clips for) video (\d+) of (\d+): (.+)")
STEP = re.compile(r"^\[(\d+|\+)(?:/(\d+))?\] (.+)")


def python_for_children() -> str:
    """Use python.exe (not pythonw.exe) so edit.py has a working stdout."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.exists() else exe)


def saved_api_key() -> str:
    """ANTHROPIC_API_KEY from this process, or from the user's saved Windows variables."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key or sys.platform != "win32":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            return str(winreg.QueryValueEx(reg, "ANTHROPIC_API_KEY")[0])
    except OSError:
        return ""


def youtube_mode() -> str:
    """'prompt' (free copy-paste, the default) or 'claude' (paid API), from config.yaml."""
    try:
        import yaml
        with open(ROOT / "config.yaml", encoding="utf-8") as f:
            return str((yaml.safe_load(f) or {}).get("youtube_mode") or "prompt").strip().lower()
    except Exception:
        return "prompt"


BG, CARD, LINE = "#f3f5f9", "#ffffff", "#dfe4ec"
INK, MUTED = "#1c2430", "#5f6b7a"
ACCENT, VIOLET, HEADER = "#2563eb", "#7c3aed", "#111827"

CHAT_SITES = [("ChatGPT", "https://chatgpt.com/"), ("Gemini", "https://gemini.google.com/app"),
              ("Claude.ai", "https://claude.ai/new")]


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening the user's own file or folder
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.videos: list[Path] = []
        self.proc: subprocess.Popen | None = None
        self.events: queue.Queue = queue.Queue()
        self.after_process: list[str] = []      # stems waiting for caption review
        self.started_at = 0.0
        self.phase = ""
        self.stopped = False
        self.review_proc: subprocess.Popen | None = None   # caption review page server
        self.review_url = ""

        root.title("Granite by Taimur Ishaq")
        root.geometry("900x940")
        root.minsize(780, 720)
        root.configure(bg=BG)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=CARD, foreground=INK, fieldbackground=CARD,
                        bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, troughcolor="#e8ecf3",
                        focuscolor=CARD)
        style.configure("TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=INK)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Card.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("TCheckbutton", background=CARD, foreground=INK, padding=(0, 3))
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TSpinbox", padding=3, arrowsize=12)
        style.configure("TButton", padding=(12, 6), background="#eef1f6", foreground=INK, borderwidth=1,
                        bordercolor=LINE, relief="flat")
        style.map("TButton", background=[("active", "#e1e6ef"), ("disabled", "#f4f6fa")],
                  foreground=[("disabled", "#9aa5b4")])
        for name, colour, hover in (("Primary", ACCENT, "#1d4ed8"), ("Clips", VIOLET, "#6d28d9")):
            style.configure(f"{name}.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 10),
                            background=colour, foreground="#ffffff", borderwidth=0)
            style.map(f"{name}.TButton", background=[("active", hover), ("disabled", "#c3cad6")],
                      foreground=[("disabled", "#ffffff")])
        style.configure("Link.TButton", background=CARD, foreground=ACCENT, borderwidth=0, padding=(2, 2))
        style.map("Link.TButton", background=[("active", CARD)], foreground=[("active", "#1d4ed8")])
        style.configure("Bar.Horizontal.TProgressbar", background=ACCENT, troughcolor="#e8ecf3",
                        borderwidth=0, thickness=10)

        def card(parent, title: str, hint: str = ""):
            """A white box with a thin border, a bold title and an optional grey line under it."""
            frame = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
            inner = ttk.Frame(frame, padding=16)
            inner.pack(fill="both", expand=True)
            head = ttk.Frame(inner)
            head.pack(fill="x")
            ttk.Label(head, text=title, style="Card.TLabel").pack(side="left")
            if hint:
                ttk.Label(inner, text=hint, style="Muted.TLabel", wraplength=380,
                          justify="left").pack(anchor="w", pady=(2, 8))
            return frame, inner, head

        # Header band
        header = tk.Frame(root, bg=HEADER)
        header.pack(fill="x")
        hrow = tk.Frame(header, bg=HEADER)
        hrow.pack(fill="x", padx=22, pady=16)
        titles = tk.Frame(hrow, bg=HEADER)
        titles.pack(side="left")
        tk.Label(titles, text="Granite by Taimur Ishaq", bg=HEADER, fg="#ffffff",
                 font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(titles, text="Get your lecture videos ready to post, and cut short clips.", bg=HEADER,
                 fg="#aab4c5", font=("Segoe UI", 10)).pack(anchor="w")
        tk.Button(hrow, text="Settings", command=lambda: open_path(ROOT / "config.yaml"), bg="#1f2937",
                  fg="#e5e9f0", activebackground="#374151", activeforeground="#ffffff", relief="flat",
                  bd=0, padx=14, pady=6, font=("Segoe UI", 9), cursor="hand2").pack(side="right")

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=14)

        # 1. Videos
        videos_card, vin, vhead = card(body, "Your videos")
        videos_card.pack(fill="x")
        self.count_label = ttk.Label(vhead, text="", style="Muted.TLabel")
        self.count_label.pack(side="left", padx=10)
        vrow = ttk.Frame(vin)
        vrow.pack(fill="x", pady=(8, 0))
        left = ttk.Frame(vrow)
        left.pack(side="left", fill="both", expand=True)
        self.listbox = tk.Listbox(left, height=4, selectmode="extended", activestyle="none",
                                  font=("Segoe UI", 10), relief="flat", bg="#fbfcfe", fg=INK,
                                  highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                                  selectbackground="#dbe7ff", selectforeground=INK)
        self.listbox.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        bar.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=bar.set)
        self.list_hint = tk.Label(self.listbox, text="No videos yet.\nPress “Add videos…” to begin.",
                                  bg="#fbfcfe", fg=MUTED, font=("Segoe UI", 10), justify="center")
        self.list_hint.place(relx=0.5, rely=0.5, anchor="center")
        buttons = ttk.Frame(vrow)
        buttons.pack(side="left", fill="y", padx=(12, 0))
        self.add_btns = [
            ttk.Button(buttons, text="Add videos…", command=self.add_videos),
            ttk.Button(buttons, text="Add a folder…", command=self.add_folder),
            ttk.Button(buttons, text="Remove", command=self.remove_selected),
            ttk.Button(buttons, text="Clear", command=self.clear_videos),
        ]
        for b in self.add_btns:
            b.pack(fill="x", pady=2)

        # 2. Two ways to use them, side by side
        modes = tk.Frame(body, bg=BG)
        modes.pack(fill="x", pady=14)
        modes.columnconfigure((0, 1), weight=1, uniform="modes")

        self.review = tk.BooleanVar(value=True)
        self.youtube = tk.BooleanVar(value=True)
        full_card, fin, _ = card(modes, "Full lecture edit",
                                 "Fixes the sound, speeds up the quiet parts, and adds captions, an intro and an outro.")
        full_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ttk.Checkbutton(fin, text="Let me check the captions first", variable=self.review).pack(anchor="w")
        yt_claude = youtube_mode() == "claude"
        ttk.Checkbutton(fin, text="Make a YouTube title and description",
                        variable=self.youtube).pack(anchor="w")
        ttk.Label(fin, style="Muted.TLabel", wraplength=380, justify="left",
                  text=("Uses your Claude key." if yt_claude else
                        "Free. You copy some text into ChatGPT or Gemini.")).pack(anchor="w", padx=(22, 0))
        keyrow = ttk.Frame(fin)
        keyrow.pack(anchor="w", padx=(22, 0))
        self.key_label = ttk.Label(keyrow, style="Muted.TLabel")
        self.key_label.pack(side="left")
        ttk.Button(keyrow, text="Add Claude key…", style="Link.TButton", command=self.set_api_key,
                   cursor="hand2").pack(side="left", padx=4)
        self.start_btn = ttk.Button(fin, text="Start full edit", style="Primary.TButton", command=self.start)
        self.start_btn.pack(fill="x", pady=(14, 0), side="bottom")

        clips_card, cin, _ = card(modes, "Short clips",
                                  "Short up-and-down videos for Shorts, Reels and TikTok, with captions.")
        clips_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        crow = ttk.Frame(cin)
        crow.pack(anchor="w", pady=(0, 2))
        ttk.Label(crow, text="Clips per video").pack(side="left")
        self.clip_count = tk.IntVar(value=5)
        ttk.Spinbox(crow, from_=1, to=20, width=4, textvariable=self.clip_count).pack(side="left", padx=8)
        ttk.Label(crow, text="20–60 seconds each", style="Muted.TLabel").pack(side="left")
        has_key = bool(saved_api_key()) or (ROOT / "gemini_key.txt").exists()
        self.clips_ai = tk.BooleanVar(value=has_key)
        ttk.Checkbutton(cin, text="Use AI to choose the best parts", variable=self.clips_ai).pack(anchor="w")
        ttk.Label(cin, style="Muted.TLabel", wraplength=380, justify="left",
                  text="Off: the program picks the parts with the most talking. "
                       "On: smarter choices and better titles.").pack(anchor="w", padx=(22, 0))
        self.clips_btn = ttk.Button(cin, text="Make clips", style="Clips.TButton", command=self.make_clips)
        self.clips_btn.pack(fill="x", pady=(14, 0), side="bottom")
        self.refresh_key_label()

        # 3. Progress
        run_card, run, _ = card(body, "Progress")
        run_card.pack(fill="both", expand=True)
        self.overall = ttk.Label(run, text="First add your videos. Then press Start full edit or Make clips.",
                                 style="Status.TLabel", wraplength=800, justify="left")
        self.overall.pack(anchor="w", pady=(4, 2))
        self.step = ttk.Label(run, text="", style="Muted.TLabel")
        self.step.pack(anchor="w")
        self.bar = ttk.Progressbar(run, mode="determinate", maximum=100, style="Bar.Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(6, 10))
        row = ttk.Frame(run)
        row.pack(fill="x", pady=(0, 8))
        self.stop_btn = ttk.Button(row, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Button(row, text="Open finished videos", command=self.open_output).pack(side="right")
        ttk.Button(row, text="YouTube prompts", command=lambda: self.show_youtube_box(None)).pack(side="right",
                                                                                              padx=6)

        # Caption review panel (shown only while waiting for review)
        self.review_box = ttk.Frame(run)
        ttk.Label(self.review_box, text="Check the captions in your browser, next to the video. Your changes save by themselves. "
                  "When you are done, press Finish videos here.", wraplength=680).pack(anchor="w")
        rrow = ttk.Frame(self.review_box)
        rrow.pack(fill="x", pady=4)
        self.srt_list = tk.Listbox(rrow, height=3, font=("Segoe UI", 10), activestyle="none")
        self.srt_list.pack(side="left", fill="x", expand=True)
        self.srt_list.bind("<Double-Button-1>", lambda _e: self.open_srt())
        rbtn = ttk.Frame(rrow)
        rbtn.pack(side="left", padx=(8, 0))
        ttk.Button(rbtn, text="Check captions", command=self.review_captions).pack(fill="x", pady=2)
        ttk.Button(rbtn, text="Open in Notepad", command=self.open_srt).pack(fill="x", pady=2)
        ttk.Button(rbtn, text="Finish videos", style="Accent.TButton", command=self.finish).pack(fill="x", pady=2)

        # YouTube prompt panel (free route): copy the prompt, paste it into a chat site
        self.yt_box = ttk.Frame(run)
        ttk.Label(self.yt_box, text="To get the YouTube text: press Copy prompt, open a chat site, paste (Ctrl+V) and send. "
                  "Then copy the answer into YouTube Studio.", wraplength=680).pack(anchor="w")
        yrow = ttk.Frame(self.yt_box)
        yrow.pack(fill="x", pady=4)
        self.yt_list = tk.Listbox(yrow, height=3, font=("Segoe UI", 10), activestyle="none", exportselection=False)
        self.yt_list.pack(side="left", fill="x", expand=True)
        self.yt_list.bind("<Double-Button-1>", lambda _e: self.copy_prompt())
        ybtn = ttk.Frame(yrow)
        ybtn.pack(side="left", padx=(8, 0))
        ttk.Button(ybtn, text="Copy prompt", style="Accent.TButton", command=self.copy_prompt).pack(fill="x", pady=2)
        sites = ttk.Frame(ybtn)
        sites.pack(fill="x", pady=2)
        for name, url in CHAT_SITES:
            ttk.Button(sites, text=name, width=9, command=lambda u=url: webbrowser.open(u)).pack(side="left", padx=1)

        self.log = ScrolledText(run, height=10, font=("Consolas", 9), wrap="word", state="disabled",
                                background="#fbfbfb")
        self.log.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.drain_events)

    # ------------------------------------------------------------------ videos
    def add_videos(self) -> None:
        files = filedialog.askopenfilenames(title="Choose lecture videos", filetypes=VIDEO_TYPES,
                                            initialdir=str(INPUT if INPUT.exists() else ROOT))
        self.add_paths(Path(f) for f in files)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder of lectures",
                                         initialdir=str(INPUT if INPUT.exists() else ROOT))
        if folder:
            found = sorted((p for p in Path(folder).iterdir()
                            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS), key=lambda p: p.name.lower())
            if not found:
                messagebox.showinfo("Granite", "No videos were found in that folder.")
            self.add_paths(found)

    def add_paths(self, paths) -> None:
        for p in paths:
            p = p.resolve()
            if p not in self.videos:
                self.videos.append(p)
                self.listbox.insert("end", p.name)
        self.refresh_list_hint()
        if self.videos and not self.busy():
            self.overall.config(text=f"{len(self.videos)} video(s) ready. Press Start full edit or Make clips.")

    def remove_selected(self) -> None:
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.videos[i]
        self.refresh_list_hint()

    def clear_videos(self) -> None:
        self.listbox.delete(0, "end")
        self.videos.clear()
        self.refresh_list_hint()

    def refresh_list_hint(self) -> None:
        n = len(self.videos)
        self.count_label.config(text="" if not n else f"{n} video" + ("s" if n != 1 else ""))
        if n:
            self.list_hint.place_forget()
        else:
            self.list_hint.place(relx=0.5, rely=0.5, anchor="center")

    # ------------------------------------------------------------------ API key
    def refresh_key_label(self) -> None:
        self.key_label.config(text="Claude key: saved" if saved_api_key() else "Claude key: not added (you can skip this)")

    def set_api_key(self) -> None:
        key = simpledialog.askstring(
            "Claude API key",
            "Paste your Claude API key (starts with sk-ant-).\n"
            "Get one at console.anthropic.com > API keys. It is saved for your Windows user only.",
            show="*", parent=self.root)
        if not key:
            return
        key = key.strip()
        if sys.platform == "win32":
            result = subprocess.run(["setx", "ANTHROPIC_API_KEY", key], capture_output=True, text=True,
                                    creationflags=NO_WINDOW)
            if result.returncode != 0:
                messagebox.showerror("Granite", "Could not save the key:\n" + result.stderr.strip())
                return
        os.environ["ANTHROPIC_API_KEY"] = key
        self.refresh_key_label()
        messagebox.showinfo("Granite", "API key saved.")

    # ------------------------------------------------------------------ running
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def set_running(self, running: bool) -> None:
        self.start_btn.config(state="disabled" if running else "normal")
        self.clips_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        for b in self.add_btns:
            b.config(state="disabled" if running else "normal")

    def youtube_flags(self) -> list[str]:
        return [] if self.youtube.get() else ["--no-youtube"]

    def start(self) -> None:
        if self.busy():
            return
        if not self.videos:
            messagebox.showinfo("Granite", "Please add a video first.")
            return
        if self.youtube.get() and youtube_mode() == "claude" and not saved_api_key():
            if not messagebox.askyesno(
                    "Granite",
                    "You have not added a Claude key, so the YouTube title and description will be skipped.\n\n"
                    "Continue anyway?"):
                return
        self.review_box.pack_forget()
        self.yt_box.pack_forget()
        self.clear_log()
        self.started_at = time.time()
        files = [str(v) for v in self.videos]
        if self.review.get():
            self.after_process = [v.stem for v in self.videos]
            self.launch(["process", *files], phase="process")
        else:
            self.after_process = []
            self.launch(["all", *files, "--no-review", *self.youtube_flags()], phase="all")

    def make_clips(self) -> None:
        if self.busy():
            return
        if not self.videos:
            messagebox.showinfo("Granite", "Please add a video first.")
            return
        try:
            count = max(1, min(20, int(self.clip_count.get())))
        except (tk.TclError, ValueError):
            messagebox.showinfo("Granite", "Type a number from 1 to 20 for how many clips you want.")
            return
        use_ai = self.clips_ai.get()
        if use_ai and not saved_api_key() and not (ROOT / "gemini_key.txt").exists():
            if not messagebox.askyesno(
                    "Granite",
                    "'Use AI to choose the best parts' is ticked, but you have no AI key.\n\n"
                    "Press Yes to make the clips without AI this time.\n"
                    "Press No to go back and add a key."):
                return
            use_ai = False
        self.review_box.pack_forget()
        self.yt_box.pack_forget()
        self.clear_log()
        self.started_at = time.time()
        picker = [] if use_ai else ["--picker", "local"]
        self.launch(["clips", *[str(v) for v in self.videos], "--count", str(count), *picker], phase="clips")

    def finish(self) -> None:
        stems = [s for s in self.after_process if (WORK / f"{s}_fast.mp4").exists()]
        if not stems:
            messagebox.showinfo("Granite", "There is nothing waiting to be finished.")
            return
        if self.review_proc and self.review_proc.poll() is None:
            time.sleep(1.5)                         # let the page's last autosave land
            self.close_review()
        self.review_box.pack_forget()
        self.yt_box.pack_forget()
        self.launch(["finalize", *stems, *self.youtube_flags()], phase="finalize")

    def launch(self, args: list[str], phase: str) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   HF_HUB_DISABLE_SYMLINKS_WARNING="1")
        key = saved_api_key()
        if key:
            env["ANTHROPIC_API_KEY"] = key
        cmd = [python_for_children(), str(EDIT), *args]
        self.write_log("> python edit.py " + " ".join(f'"{a}"' if " " in a else a for a in args) + "\n")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        self.phase = phase
        self.set_running(True)
        self.bar.config(value=0)
        self.overall.config(text="Working… please wait.")
        self.step.config(text="")
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()

    def read_output(self, proc: subprocess.Popen) -> None:
        """Split the child's output on newlines and carriage returns (progress updates)."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            pending += decoder.decode(chunk)
            parts = re.split(r"(\r\n|\n|\r)", pending)
            pending = parts.pop()                  # incomplete tail
            for i in range(0, len(parts) - 1, 2):
                self.events.put(("line" if parts[i + 1] != "\r" else "progress", parts[i]))
        if pending:
            self.events.put(("line", pending))
        self.events.put(("exit", proc.wait()))

    def drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "exit":
                    self.on_exit(value)
                else:
                    self.handle_text(ANSI.sub("", value), kind == "progress")
        except queue.Empty:
            pass
        self.root.after(100, self.drain_events)

    def handle_text(self, text: str, is_progress: bool) -> None:
        line = text.strip()
        if not line:
            if not is_progress:
                self.write_log("\n")
            return
        if m := VIDEO_N.search(line):
            verb = {"Processing": "Preparing", "Finalizing": "Finishing", "YouTube for": "YouTube text for",
                    "Clips for": "Making clips for"}[m[1]]
            self.overall.config(text=f"{verb} video {m[2]} of {m[3]}: {m[4]}")
        elif line.startswith(("Processing ", "Finalizing ", "YouTube package for ", "Making short clips from ")) \
                and "video" not in line:
            self.overall.config(text=line.split("  (")[0])
        if m := STEP.match(line):
            self.step.config(text=(f"Step {m[1]} of {m[2]}: " if m[2] else "") + m[3])
            self.bar.config(value=0)
        if pcts := PCT.findall(line):
            self.bar.config(value=min(100.0, float(pcts[-1])))
        if "done in" in line:
            self.bar.config(value=100)
        if is_progress or line.startswith("====="):
            return                                  # keep percentages and separators out of the log
        self.write_log(text.rstrip() + "\n")

    def on_exit(self, code: int) -> None:
        self.proc = None
        self.set_running(False)
        took = time.time() - self.started_at
        mins = f"{int(took // 60)}m {int(took % 60):02d}s"
        if self.stopped:
            self.stopped = False
            self.overall.config(text="Stopped.")
            self.step.config(text="")
            return
        if self.phase == "process":
            ready = [s for s in self.after_process if (WORK / f"{s}.srt").exists()
                     and (WORK / f"{s}.srt").stat().st_mtime >= self.started_at - 1]
            self.after_process = ready
            if ready:
                self.srt_list.delete(0, "end")
                for s in ready:
                    self.srt_list.insert("end", f"{s}.srt")
                self.srt_list.selection_set(0)
                self.review_box.pack(fill="x", pady=(0, 8), before=self.log)
                self.overall.config(text=f"Captions are ready for {len(ready)} video(s) ({mins}). Check them in "
                                         "your browser, then press Finish videos.")
                self.review_captions()
                self.step.config(text="")
                self.bar.config(value=100)
            else:
                self.overall.config(text="Something went wrong. Read the details below.")
            return
        if code == 0 and self.phase == "clips":
            self.overall.config(text=f"Your clips are ready ({mins}). Opening the folder…")
            self.step.config(text="")
            self.bar.config(value=100)
            folders = [OUTPUT / f"{v.stem}_clips" for v in self.videos]
            existing = [f for f in folders if f.exists()]
            open_path(existing[0] if len(existing) == 1 else OUTPUT)
        elif code == 0:
            self.overall.config(text=f"All done in {mins}. Press Open finished videos to see them.")
            self.step.config(text="")
            self.bar.config(value=100)
            if self.youtube.get() and self.phase in {"all", "finalize"}:
                self.show_youtube_box(self.started_at - 1, quiet=True)
        else:
            self.overall.config(text=f"Finished, but something went wrong ({mins}). Read the details below.")

    def stop(self) -> None:
        if not self.busy():
            return
        if not messagebox.askyesno("Granite", "Stop now? The video being worked on will need to start again."):
            return
        self.stopped = True
        if sys.platform == "win32":                 # also stops FFmpeg / auto-editor started by edit.py
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW)
        else:
            self.proc.terminate()

    def show_youtube_box(self, since: float | None, quiet: bool = False) -> None:
        """List YouTube prompt files: those written since a time, or all of them (newest first)."""
        prompts = sorted(OUTPUT.glob("*_youtube_prompt.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        if since is not None:
            prompts = [p for p in prompts if p.stat().st_mtime >= since]
        if not prompts:
            if not quiet:
                messagebox.showinfo("Granite", "No YouTube prompts yet. They appear after a video is "
                                                      "finished with the YouTube box ticked.")
            return
        self.yt_list.delete(0, "end")
        for p in prompts:
            self.yt_list.insert("end", p.name[:-len("_youtube_prompt.txt")])
        self.yt_list.selection_set(0)
        self.review_box.pack_forget()
        self.yt_box.pack(fill="x", pady=(0, 8), before=self.log)

    def copy_prompt(self) -> None:
        sel = self.yt_list.curselection()
        if not sel:
            return
        name = self.yt_list.get(sel[0])
        text = (OUTPUT / f"{name}_youtube_prompt.txt").read_text(encoding="utf-8")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()                          # keep it on the clipboard after this call
        self.overall.config(text=f"Copied for {name}. Open a chat site and paste it (Ctrl+V). "
                                 "Keep this window open until you have pasted.")

    def review_captions(self) -> None:
        """Open the caption review page (edit.py review) for every lecture waiting to be finished."""
        stems = [s for s in self.after_process if (WORK / f"{s}.srt").exists()]
        if not stems:
            return
        if self.review_proc and self.review_proc.poll() is None:
            if self.review_url:
                webbrowser.open(self.review_url)
            return
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.review_url = ""
        self.review_proc = subprocess.Popen(
            [python_for_children(), str(EDIT), "review", *stems], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, text=True,
            encoding="utf-8", errors="replace")
        threading.Thread(target=self.read_review_output, args=(self.review_proc,), daemon=True).start()
        self.write_log("> Caption review page opened in your browser.\n")
        self.root.after(1000, self.watch_review)

    def read_review_output(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            if m := re.search(r"http://127\.0\.0\.1:\d+/", line):
                self.review_url = m[0]
            elif line.strip().startswith(("ERROR", "Traceback")) or "Error" in line:
                self.events.put(("line", line.rstrip()))

    def watch_review(self) -> None:
        proc = self.review_proc
        if proc is None:
            return
        if proc.poll() is None:
            self.root.after(1000, self.watch_review)
            return
        self.review_proc = None
        if proc.returncode == 0 and self.review_box.winfo_ismapped():
            self.overall.config(text="Captions checked. Press Finish videos.")

    def close_review(self) -> None:
        proc, self.review_proc = self.review_proc, None
        if proc and proc.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                proc.terminate()

    def open_srt(self) -> None:
        sel = self.srt_list.curselection()
        if sel:
            open_path(WORK / self.srt_list.get(sel[0]))

    def open_output(self) -> None:
        OUTPUT.mkdir(exist_ok=True)
        open_path(OUTPUT)

    # ------------------------------------------------------------------ log
    def clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def write_log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def on_close(self) -> None:
        if self.busy():
            if not messagebox.askyesno("Granite", "Work is still running. Stop it and close?"):
                return
            self.stopped = True
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                self.proc.terminate()
        self.close_review()
        self.root.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:                                       # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
