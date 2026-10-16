# yarg-stem-splitter

Splits single-file YARG / Clone Hero songs (`song.ogg`) into instrument stems
(`vocals`, `guitar`, `rhythm` (bass), `drums`, `keys`) with the BS-Roformer-SW model,
lets you listen and choose, then writes the result into the song folder (undoable).

- Only instruments charted in `song.ini` / `notes.mid` get a stem; the new `song.ogg`
  is everything else, so all files played together reproduce the original.
- Nothing in your library changes until you press **Commit** on the review page.

## Workflow

```bash
# 1. Split every single-file song under the library (resumable; Ctrl+C any time, re-run to continue)
.venv/Scripts/python.exe stemsplit.py queue "D:/Games/YARG"

# 2. Review in the browser
.venv/Scripts/python.exe review.py          # then open http://localhost:8765/
```

On the review page:

- **Needs a closer listen**: songs where a charted part is mostly missing from its stem
  (the model put it in "other"); the reason is shown. **Ready to review**: everything else.
- Listen with mute/solo, **A** to switch to the original, hold **M** to "miss" guitar.
  Timelines show charted notes (purple) against what's audible in each stem (green).
- **Make a variant**: *fill from other* gives a charted instrument the model's unclassified
  audio while it's charted (for processed guitars and synth keys). *Gate song.ogg*
  mutes leftover echoes and moves them into the instruments (full mix unchanged); don't
  use it when song.ogg holds real uncharted music.
- **Commit this version** (the base split or any variant) adds the stems to the song folder,
  renames the original to `song.ogg.orig`, and writes `stemsplit-commit.json` listing
  what was added. **Skip song** leaves the folder untouched. Both clear that song's
  staged files. Committed songs can be **undone** from the page or with
  `stemsplit.py undo "<song folder>"`.
- After committing, rescan songs in YARG.

Songs that already have stems are never touched. Staging takes about 60–90 MB per song
until it is committed or skipped (`staging/`).

## Setup (Windows, NVIDIA GPU)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv/Scripts/python.exe "audio-separator[gpu]" soundfile imageio-ffmpeg mido audioread
cp .venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe .venv/Scripts/ffmpeg.exe
```

The model (~300 MB) downloads to `models/` on first run. The onnxruntime CUDA 13
warnings are harmless (RoFormer runs on PyTorch).

## Other commands

```bash
.venv/Scripts/python.exe stemsplit.py split "<song folder>"            # one song, no queue
.venv/Scripts/python.exe stemsplit.py remix "staging/<song>" --fill guitar,piano --gate-song
.venv/Scripts/python.exe stemsplit.py queue "D:/Games/YARG" --retry-failed
.venv/Scripts/python.exe stemsplit.py bench "<folder that already has real stems>"
```

`bench` mixes a song's real stems, separates the mix, and scores each stem (SDR)
so models can be compared on your own music; results show under "Accuracy tests".
