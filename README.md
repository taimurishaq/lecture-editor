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

## Usage

Put your recording in `input/`, then:

```powershell
python edit.py process input/lecture1.mp4
```

This cleans the audio, speeds up the silences and writes `work/lecture1.srt`. The tool then stops so you can open the `.srt` file in any text editor and fix caption mistakes. Change only the text lines, not the numbers or the times.

When the captions look right:

```powershell
python edit.py finalize lecture1
```

The finished video is saved as `output/lecture1_final.mp4`.

To do both steps in one go without reviewing the captions:

```powershell
python edit.py all input/lecture1.mp4 --no-review
```

Without `--no-review`, `all` pauses after the captions are written and waits for you to press Enter.

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
| `silence_min_seconds` | `2` | Only silences longer than this are sped up |
| `silence_speed` | `3` | Playback speed for silent parts |
| `silent_audio` | `mute` | `mute` silences the sped-up parts, `keep` leaves their audio in |
| `speech_margin_seconds` | `0.3` | Normal-speed padding around speech so words are not clipped |
| `silence_threshold` | `0.04` | Raise it if background noise stops silences from being detected |
| `caption_style` | white, black outline, bottom center, medium | Font, size, colors and position of the captions |
| `logo_watermark` | `off` | `on` adds `assets/logo.png` |
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

## License

MIT. See [LICENSE](LICENSE).
