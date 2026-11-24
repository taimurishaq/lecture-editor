# lecture-editor

A command-line tool for Windows that turns a raw lecture recording into a finished video:

1. Cleans the audio (noise reduction and loudness normalization).
2. Plays long silences at high speed instead of cutting them, so nothing on screen is lost.
3. Writes English captions from mixed Urdu and English speech.
4. Burns the captions in and joins your intro and outro.

It runs on the CPU. No Nvidia graphics card is needed.

## Install (Windows)

Open PowerShell and run these once:

```powershell
winget install Python.Python.3.11
winget install Gyan.FFmpeg
```

Close and reopen PowerShell, then from the project folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell refuses to run `Activate.ps1`, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Every time you open a new terminal, activate the environment again with `.venv\Scripts\Activate.ps1` before using the tool.

The first caption run downloads the Whisper model (about 1.5 GB for `medium`). After that it works offline.

## The easy way: the app window

Double-click **`Lecture Editor.bat`** in the project folder. A window opens:

1. **Add videos** (or **Add a folder**) to pick your lectures.
2. Tick the options you want:
   - *Let me check the captions*: the tool stops after the captions are written so you can fix them, then you press **Finish videos**.
   - *Write a YouTube title, description and chapters*: needs a Claude API key (see [YouTube package](#youtube-package)); press **Set API key...** once to save it.
3. Press **Start** and watch the progress. **Open finished videos** shows the results.

The window runs the same tool described below, so all settings still come from `config.yaml` (**Open settings** opens it).

## Usage (command line)

Put your recording in `input/`, then:

```powershell
python edit.py process input/lecture1.mp4
```

This cleans the audio, speeds up the silences and writes `work/lecture1.srt`. The tool then stops so you can open the `.srt` file in any text editor and fix caption mistakes. Change only the text lines, not the numbers or the times.

When the captions look right:

```powershell
python edit.py finalize lecture1
```

The finished video is saved as `output/lecture1_final.mp4`, with a caption file next to it, `output/lecture1_final.srt`. The captions are already burned into the video. Upload the `.srt` to YouTube as well (Subtitles > Add > Upload file > With timing) so the captions are searchable, can be switched off, and can be auto-translated. Its times already allow for the intro. Set `export_srt: off` if you do not want it.

To do both steps in one go without reviewing the captions:

```powershell
python edit.py all input/lecture1.mp4 --no-review
```

Without `--no-review`, `all` pauses after the captions are written and waits for you to press Enter.

### Many lectures at once

Give a folder instead of a file to handle every video in it:

```powershell
python edit.py all input/ --no-review      # leave it running overnight
python edit.py process input/              # or: captions for all, review them, then...
python edit.py finalize lecture1 lecture2  # ...finalize the ones you have checked
```

- Videos are done in name order. If one fails, the tool notes why and moves on to the next; a list of failures is shown at the end.
- `all` with a folder skips videos that already have a finished file in `output/`, so you can run it again after adding new recordings. Add `--redo` to make them all again.
- Without `--no-review`, `all` processes every video first, then pauses once so you can review all the caption files, then finalizes them.

## YouTube package

When a video is finished, the tool can also ask Claude to write `output/lecture1_youtube.txt` with:

- three title options,
- a description with **chapters** (clickable timestamps on YouTube) already in the right places for the final video, your `youtube_footer` and hashtags,
- tags to paste into YouTube Studio.

It reads the captions, so fix those first. It costs roughly US$0.10 to $0.20 per hour of lecture with the default model, paid from your Anthropic account.

**Set up once:**

1. Create an API key at <https://console.anthropic.com> (Settings > API keys) and add some credit.
2. Save it for your Windows user, either with **Set API key...** in the app window, or in PowerShell:

   ```powershell
   setx ANTHROPIC_API_KEY "sk-ant-..."
   ```

   then close and reopen PowerShell. Never put the key in `config.yaml` or commit it to Git.
3. Optionally fill in `youtube_channel_info` and `youtube_footer` in `config.yaml`.

If there is no key or no internet, the video is still finished; only the YouTube text is skipped. To make it later, or again after editing the captions:

```powershell
python edit.py youtube lecture1
```

Turn it off with `youtube_package: off`, or for one run with `--no-youtube`.

## Short clips (Shorts, Reels, TikTok)

Cut one long video into several vertical 1080x1920 clips with big word-by-word captions:

```powershell
python edit.py clips input/lecture1.mp4
```

Each clip gets:

- the best moments of the video, 20 to 60 seconds each (5 clips by default),
- captions three words at a time, with the word being spoken highlighted in yellow,
- the clip's title at the top,
- pauses longer than 0.8 seconds cut out, and the audio levelled for phones.

The clips are saved in `output/lecture1_clips/`, with `clips.txt` holding a title, caption and hashtags for each one to paste when you post.

**How the moments are picked.** If you set up the Claude API key from the YouTube package section, Claude reads the transcript and picks the moments, writing a hook title, a caption and hashtags for each. A free Gemini key also works: save it in a file called `gemini_key.txt` in the project folder. Without any key, a built-in picker chooses the parts with the most continuous speech and words like "mistake", "remember" or "exam". It works, but its choices and titles are much weaker than an AI's.

**Change the picks before rendering:**

```powershell
python edit.py clips input/lecture1.mp4 --plan-only
```

Open `work/lecture1_clips.json`, change any `start`, `end` (in seconds) or `title`, or delete a clip, then:

```powershell
python edit.py render-clips lecture1            # all clips
python edit.py render-clips lecture1 --only 2   # just clip 2 again
```

The transcript is saved in `work/lecture1_words.json`, so later runs skip the slow transcription. Add `--fresh` to transcribe again, `--count 8` for more clips, or `--model small` for a faster but weaker transcript.

**Settings** are in the `clips:` section of `config.yaml`. The ones you are most likely to change:

| Setting | Default | Meaning |
| --- | --- | --- |
| `layout` | `blur` | `blur` shows the whole frame in the middle with a blurred copy behind it, so whiteboards and slides stay readable. `crop` fills the screen with a vertical slice of the video, which suits a face-to-camera recording |
| `crop_position` | `center` | Which slice `crop` keeps: `left`, `center`, `right` or a number from 0 to 1 |
| `min_seconds` / `max_seconds` | `20` / `60` | Clip length range |
| `cut_pauses_over` | `0.8` | Remove pauses longer than this; `0` keeps every pause |
| `caption_task` | `translate` | `translate` writes English captions; `transcribe` writes them in the spoken language |
| `captions` | white, yellow highlight | Font, size, colours, words per line and height of the captions |

If words get cut off at the edges of removed pauses, lower `pause_noise_db` to `-35`, or set `cut_pauses_over: 0`.

## Intro, outro and logo

| File | Purpose |
| --- | --- |
| `assets/intro.mp4` | Played before the lecture |
| `assets/outro.mp4` | Played after the lecture |
| `assets/logo.png` | Shown in the top-right corner when `logo_watermark: on` |

All of these are optional. A missing intro or outro is skipped with a warning. They can be any resolution or frame rate; everything is re-encoded to the output settings so the parts join cleanly.

## Settings

Every option lives in `config.yaml`. The ones you are most likely to change:

| Setting | Default | Meaning |
| --- | --- | --- |
| `whisper_model` | `medium` | `small` is faster and less accurate, `large-v3` is the most accurate and very slow on a CPU |
| `speech_language` | `ur` | Best for mixed Urdu and English. Use `auto` to let Whisper guess |
| `glossary` | empty | Subject words to spell correctly, e.g. `[debit, credit, IFRS]` |
| `silence_min_seconds` | `2` | Only silences longer than this are sped up |
| `silence_speed` | `3` | Playback speed for silent parts |
| `silent_audio` | `mute` | `mute` silences the sped-up parts, `keep` leaves their audio in |
| `speech_margin_seconds` | `0.3` | Normal-speed padding around speech so words are not clipped |
| `silence_threshold` | `0.04` | Raise it if background noise stops silences from being detected |
| `caption_style` | white, black outline, bottom center, medium | Font, size, colors and position of the captions |
| `logo_watermark` | `off` | `on` adds `assets/logo.png` |
| `export_srt` | `on` | Also save `output/<name>_final.srt` for YouTube |
| `youtube_package` | `on` | Write `output/<name>_youtube.txt` (needs a Claude API key) |
| `youtube_channel_info`, `youtube_footer` | empty | A line about your channel for Claude, and text added to every description |
| `output` | 1920x1080, 30 fps, H.264, 48 kHz | Final video format |

To try different settings without touching the main file, copy it and pass `--config`:

```powershell
python edit.py --config my-settings.yaml process input/lecture1.mp4
```

## Better noise reduction (optional)

By default the tool uses FFmpeg's built-in `afftdn` filter. For stronger speech denoising, download an RNNoise model file (`.rnnn`, for example from <https://github.com/GregorR/rnnoise-models>) and save it as `assets/rnnoise.rnnn`. The tool uses it automatically when the file exists.

## Folders

| Folder | Contents |
| --- | --- |
| `input/` | Your raw recordings |
| `work/` | Intermediate files and the `.srt` captions you review |
| `output/` | Finished videos |
| `assets/` | Intro, outro, logo and the optional noise model |

Videos, audio and model files are excluded from Git by `.gitignore`, so only the code is stored on GitHub. Back up your recordings separately.

## How long it takes

On a laptop CPU, expect the whole run to take roughly one to two times the length of the lecture with the `medium` model. Captioning is the slowest part. `small` roughly halves the captioning time.

## Troubleshooting

- **"FFmpeg was not found"**: run `winget install Gyan.FFmpeg`, then reopen the terminal.
- **"auto-editor was not found"**: activate the environment (`.venv\Scripts\Activate.ps1`) and run `pip install -r requirements.txt`.
- **Silences are not sped up**: the room noise is probably above the threshold. Raise `silence_threshold` to `0.06` or `0.08`.
- **Words are clipped at the start or end**: raise `speech_margin_seconds` to `0.5`.
- **Captions are in the wrong language or garbled**: keep `speech_language: ur`, or try a larger `whisper_model`.
- **"No Claude API key found"**: see [YouTube package](#youtube-package). After `setx`, reopen PowerShell (or the app window) so it sees the key.
- **The same subject words keep coming out wrong**: add them to `glossary` in `config.yaml`, spelled the way you want them.

## License

MIT. See [LICENSE](LICENSE).
