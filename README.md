# yarg-stem-separator

Splits single-file YARG / Clone Hero songs (`song.ogg`, `.opus`, `.mp3` or `.wav`) into instrument stems
(`vocals`, `guitar`, `rhythm` (bass), `drums`, `keys`) with the BS-Roformer-SW model,
lets you listen and choose, then writes the result into the song folder (undoable).

![Review page: per-track mute/solo, chart notes vs audio timelines, and commit/variant controls](docs/review-page.webp)

- Only instruments charted in `song.ini` / `notes.mid` get a stem; the new `song.ogg`
  is everything else, so all files played together reproduce the original.
- Nothing in your library changes until you press **Commit**.
- It only works on audio you already have; it doesn't download songs or audio of any kind.
- Needs Windows and an NVIDIA graphics card (the separation is done by an AI model).
- Works with song folders whose audio is `song.ogg`, `song.opus`, `song.mp3` or `song.wav`.
  New tracks are always written as `.ogg`. Most testing has been on `.ogg` songs.
- Rock Band CON packages are supported when they hold a single song whose audio is
  **unencrypted** and is one full mix in the backing track (common for customs that
  aren't multitrack). Encrypted CON audio is never decrypted; those, multi-song packs and
  CONs that already have separate tracks are skipped and counted on the page.

## CON packages

The scan recognises CON files by their header (any file name). A usable one is staged
like any other song: the backing audio, chart (`notes.mid`), a `song.ini` built from its
`songs.dta` and the album art are extracted into `staging/_con/`. Nothing changes in the
library until you commit. Then:

- a folder `Artist - Title` is created next to the CON with the stems, chart, `song.ini`
  and `album.png`;
- the CON file is moved to `con-backups/` (next to `staging/`, outside your library) so
  YARG doesn't list the song twice;
- **Undo** moves the CON back and removes the files the conversion created.

Difficulties in `song.ini` are mapped from the CON's Rock Band difficulty ratings.

## Quick start

1. **Download:** [click here to download the latest version as a ZIP](https://github.com/sidasta/yarg-stem-separator/archive/refs/heads/main.zip)
   (or, on this page, click the green **Code** button, then **Download ZIP**).
   Right-click the ZIP, choose **Extract All…**, and put the folder somewhere
   *outside* your YARG songs folder (e.g. `Documents`). Don't run it from inside the ZIP.
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (needed once, for setup).
3. Double-click **`Start Stem Separator.bat`** in the extracted folder. If Windows shows a
   security warning (it does for any downloaded script), choose **Run**, or on a
   "Windows protected your PC" screen click **More info**, then **Run anyway**. The first run sets up Python and the
   separation packages (a few GB, several minutes); after that it starts in seconds.
   Your browser opens at http://localhost:8765/. Keep the black window open while you
   use it; close it to stop. Running the .bat again while it's open just reopens the page.
4. In **⚙ Library & splitting**, add your song folder (Browse… or paste a path), then
   **Start splitting**. Progress shows on the page; you can review finished songs while
   it runs, and stop/start any time. It carries on where it left off, and later runs only
   pick up new songs. The separation model (~300 MB) downloads on the first split.

## Reviewing

- **Needs a closer listen**: songs where a charted part is mostly missing from its stem
  (the model put it in "other"); the reason is shown. With *prepare a variant for flagged
  songs* on (default), a "fill from other + gated song" version is already built for these.
  **Ready to review**: everything else.
- Listen with mute/solo, **A** to switch to the original, hold **M** to "miss" guitar.
  Timelines show charted notes (purple) against what's audible in each stem (green).
- **Make a variant**: *fill from other* gives a charted instrument the model's unclassified
  audio while it's charted (for processed guitars and synth keys). *Gate song.ogg*
  mutes leftover echoes and moves them into the instruments (full mix unchanged); don't
  use it when song.ogg holds real uncharted music.
- **Commit this version** (the plain split or any variant) adds the stems to the song folder,
  renames the original audio to `song.<ext>.orig` (e.g. `song.mp3.orig`), and writes `stemsplit-commit.json` listing
  what was added. **Skip song** leaves the folder untouched. Both clear that song's
  staged files and move on to the next song.
- **Commit all unflagged songs** (under Library & splitting) commits the plain split of
  every song in "Ready to review" at once.
- Committed songs can be **undone** from the page: the added files are removed and
  the original song audio is restored. After committing, rescan songs in YARG.

Songs that already have stems are never touched. Staging takes about 60–90 MB per song
until it is committed or skipped (`staging/`).

## Manual setup and command line

The .bat does this on first run; to do it by hand (Windows, NVIDIA GPU):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv/Scripts/python.exe "audio-separator[gpu]" soundfile imageio-ffmpeg mido audioread
cp .venv/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe .venv/Scripts/ffmpeg.exe
```

The onnxruntime CUDA 13 warnings are harmless (RoFormer runs on PyTorch). Everything the
page does is also available from the command line:

```bash
.venv/Scripts/python.exe review.py --open                               # the web page
.venv/Scripts/python.exe stemsplit.py queue "D:/Games/YARG"             # split all new songs
.venv/Scripts/python.exe stemsplit.py split "<song folder>"             # one song, no queue
.venv/Scripts/python.exe stemsplit.py remix "staging/<song>" --fill guitar,piano --gate-song
.venv/Scripts/python.exe stemsplit.py undo "<song folder>"
.venv/Scripts/python.exe stemsplit.py bench "<folder that already has real stems>"
```

`bench` mixes a song's real stems, separates the mix, and scores each stem (SDR)
so models can be compared on your own music; results show under "Accuracy tests".
