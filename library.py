"""Library state, bulk queue, and commit / skip / undo for staged splits.

State lives in staging/state.json, keyed by song folder path:
  pending    found by a scan, waiting to be split
  split      staged and waiting for review
  failed     splitting raised an error (see "error")
  committed  stems written into the song folder (song.ogg backed up as song.ogg.orig)
  skipped    reviewed and rejected; song folder untouched

Committing writes stemsplit-commit.json into the song folder listing the files it added,
so undo removes exactly those and restores song.ogg from the backup.
"""

import contextlib
import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import stemsplit

BACKUP_NAME = "song.ogg.orig"
RECORD_NAME = "stemsplit-commit.json"
TMP_SUFFIX = ".stemsplit-tmp"


def state_path(staging):
    return Path(staging) / "state.json"


@contextlib.contextmanager
def _lock(staging, timeout=30):
    """Cross-process lock so the queue and the review server can both update state."""
    lock = Path(staging) / "state.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > 60:  # left behind by a crashed process
                    lock.unlink()
                    continue
            if time.time() > deadline:
                raise TimeoutError(f"state is locked: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            lock.unlink()


def load_state(staging):
    try:
        return json.loads(state_path(staging).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"songs": {}}


def _save_state(staging, state):
    path = state_path(staging)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def edit_state(staging):
    """Load, let the caller modify, and atomically save the state under the lock."""
    Path(staging).mkdir(parents=True, exist_ok=True)
    with _lock(staging):
        state = load_state(staging)
        yield state
        _save_state(staging, state)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def set_song(state, source, **fields):
    entry = state["songs"].setdefault(str(source), {"name": Path(source).name})
    entry.update(fields, updated=_now())
    return entry


def staged_folders(staging, source=None):
    """{folder name: manifest} for staged (non-benchmark) splits, optionally for one song."""
    out = {}
    for m in sorted(Path(staging).glob("*/manifest.json")):
        data = stemsplit.read_manifest(m.parent)
        if data and data.get("kind") == "split" and (source is None or data.get("source") == str(source)):
            out[m.parent.name] = data
    return out


def sync_staged(staging):
    """Adopt staged splits that the state file doesn't know about (e.g. made with `split`)."""
    with edit_state(staging) as state:
        for folder, m in staged_folders(staging).items():
            if m.get("variant_of"):
                continue
            entry = state["songs"].get(m["source"])
            if entry is None or entry.get("status") in ("pending", "failed"):
                set_song(state, m["source"], status="split", staged=folder, flags=m.get("flags", []))


def find_songs(roots):
    """Song folders under roots that have a single song.ogg and no separate stems yet."""
    for root in map(Path, roots):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            names = {f.lower() for f in filenames}
            if "song.ogg" not in names or not names & {"song.ini", "notes.mid", "notes.chart"}:
                continue
            song_dir = Path(dirpath).resolve()
            if not stemsplit.existing_stems(song_dir):
                yield song_dir


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {"roots": [], "model": stemsplit.DEFAULT_MODEL, "auto_variants": True, "retry_failed": False}


def load_settings(staging):
    try:
        saved = json.loads((Path(staging) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}


def save_settings(staging, settings):
    path = Path(staging) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- queue

def scan(roots, staging, retry_failed=False):
    """Register newly found songs as pending. Returns (found count, list of song folders to split)."""
    sync_staged(staging)
    found = list(find_songs(roots))
    with edit_state(staging) as state:
        for song_dir in found:
            if str(song_dir) not in state["songs"]:
                set_song(state, song_dir, status="pending")
        todo = [Path(s) for s, e in state["songs"].items()
                if e["status"] == "pending" or (retry_failed and e["status"] == "failed")]
    return len(found), [s for s in todo if (s / "song.ogg").exists()]


def suggested_fill(report):
    """Charted stems the model mostly missed, in fill priority order."""
    low = {stem for stem, e in report["stems"].items()
           if report["charted"].get(stem) and e.get("chart_coverage") is not None
           and e["chart_coverage"] < stemsplit.FLAG_COVERAGE}
    return [s for s in stemsplit.FILL_ORDER if s in low]


def run_queue(roots, staging, model=stemsplit.DEFAULT_MODEL, retry_failed=False, limit=0, auto_variants=True,
              log=print, progress=None, should_stop=lambda: False):
    """Split every pending song under roots. Safe to interrupt; re-running carries on.

    auto_variants: for flagged songs, also build a "fill from other + gated song" variant.
    progress(dict) is called before each song and at the end; should_stop() is checked between songs.
    """
    staging = Path(staging)
    found, todo = scan(roots, staging, retry_failed)
    if limit:
        todo = todo[:limit]
    log(f"Found {found} single-file songs; {len(todo)} to split now.")
    stats = {"total": len(todo), "done": 0, "flagged": 0, "failed": 0, "current": None, "eta_min": None}
    t0 = time.time()
    for i, song_dir in enumerate(todo, 1):
        if should_stop():
            log("Stopped. Start again any time to carry on where it left off.")
            break
        if stats["done"] + stats["failed"]:
            per_song = (time.time() - t0) / (stats["done"] + stats["failed"])
            stats["eta_min"] = round(per_song * (len(todo) - i + 1) / 60)
        stats["current"] = song_dir.name
        if progress:
            progress(dict(stats))
        eta = f", ~{stats['eta_min']} min left" if stats["eta_min"] is not None else ""
        log(f"[{i}/{len(todo)}{eta}] {song_dir.name}")
        try:
            folder, report = stemsplit.split_song(song_dir, model, staging)
        except Exception as e:  # keep going; the song is marked failed with the reason
            stats["failed"] += 1
            log(f"   FAILED: {e}")
            with edit_state(staging) as state:
                set_song(state, song_dir, status="failed", error=f"{type(e).__name__}: {e}")
            continue
        stats["done"] += 1
        kept = [e["file"] for e in report["stems"].values() if e.get("kept") and e["file"] != "song"]
        log(f"   {report['seconds']}s, stems: {', '.join(kept) or 'none'}")
        for flag in report["flags"]:
            log(f"   ! {flag}")
        stats["flagged"] += bool(report["flags"])
        with edit_state(staging) as state:
            set_song(state, song_dir, status="split", staged=folder, flags=report["flags"], error=None)
        fill = suggested_fill(report) if auto_variants and report["flags"] else []
        if fill:
            try:
                stemsplit.remix(staging / folder, fill, -15.0)
                log(f"   prepared variant: {'+'.join(stemsplit.FILL_LABEL[s] for s in fill)} from other, song gated")
            except Exception as e:
                log(f"   (could not prepare variant: {e})")
    stats["current"] = None
    if progress:
        progress(dict(stats))
    log(f"Split {stats['done']} songs ({stats['flagged']} flagged for a closer listen), {stats['failed']} failed.")
    return stats


def cmd_queue(args):
    try:
        run_queue(args.roots, args.staging, args.model, args.retry_failed, args.limit,
                  auto_variants=not args.no_variants, log=lambda m: print(m, flush=True))
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to carry on where it left off.")


def commit_ready(staging):
    """Commit the plain split of every unflagged song waiting for review. Returns (committed, errors)."""
    committed, errors = [], []
    for source, e in list(load_state(staging)["songs"].items()):
        if e.get("status") != "split" or e.get("flags") or not e.get("staged"):
            continue
        try:
            commit(staging, e["staged"])
            committed.append(e["name"])
        except (ValueError, OSError) as err:
            errors.append(f"{e['name']}: {err}")
    return committed, errors


# ---------------------------------------------------------------- commit / skip / undo

def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_staged(staging, source):
    for folder in staged_folders(staging, source):
        shutil.rmtree(Path(staging) / folder, ignore_errors=True)


def commit(staging, folder):
    """Write a staged split (base or variant) into its song folder."""
    staging = Path(staging)
    staged = staging / folder
    m = stemsplit.read_manifest(staged)
    if not m or m.get("kind") != "split":
        raise ValueError("not a staged split")
    song_dir = Path(m["source"])
    song = song_dir / "song.ogg"
    if not song.exists():
        raise ValueError(f"{song} no longer exists")
    if (song_dir / BACKUP_NAME).exists() or (song_dir / RECORD_NAME).exists():
        raise ValueError(f"{song_dir.name} already has a backup or commit record; undo or tidy it first")
    if _sha1(song) != _sha1(staged / "original.ogg"):
        raise ValueError("song.ogg has changed since it was split; re-split it first")
    new_files = sorted(p.name for p in staged.glob("*.ogg") if p.name != "original.ogg")
    clash = [n for n in new_files if n != "song.ogg" and (song_dir / n).exists()]
    if clash or stemsplit.existing_stems(song_dir):
        raise ValueError(f"song folder already has stem files: {', '.join(clash or stemsplit.existing_stems(song_dir))}")

    # Copy to temporary names first, so a failure part-way leaves the song folder as it was.
    tmp = []
    try:
        for name in new_files:
            shutil.copy2(staged / name, song_dir / (name + TMP_SUFFIX))
            tmp.append(name)
        song.rename(song_dir / BACKUP_NAME)
    except Exception:
        for name in tmp:
            (song_dir / (name + TMP_SUFFIX)).unlink(missing_ok=True)
        raise
    for name in new_files:
        os.replace(song_dir / (name + TMP_SUFFIX), song_dir / name)
    variant = m.get("variant") or "plain split"
    if not m.get("variant") and m.get("variant_of"):  # variants made before the label was stored
        variant = m["song"][len(m["variant_of"]):].strip().strip("[]") or variant
    record = {"committed": _now(), "variant": variant, "model": m.get("model"),
              "files": [n for n in new_files if n != "song.ogg"], "replaced": "song.ogg", "backup": BACKUP_NAME}
    (song_dir / RECORD_NAME).write_text(json.dumps(record, indent=2), encoding="utf-8")
    with edit_state(staging) as state:
        set_song(state, song_dir, status="committed", staged=None, variant=record["variant"],
                 files=record["files"], flags=m.get("flags", []))
    _remove_staged(staging, song_dir)
    return record


def undo(staging, source):
    """Remove the stems a commit added and restore the original song.ogg."""
    song_dir = Path(source)
    record_path = song_dir / RECORD_NAME
    if not record_path.exists() or not (song_dir / BACKUP_NAME).exists():
        raise ValueError(f"no commit to undo in {song_dir.name}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    for name in record["files"]:
        if Path(name).name == name:  # only plain file names from our own record
            (song_dir / name).unlink(missing_ok=True)
    os.replace(song_dir / BACKUP_NAME, song_dir / "song.ogg")
    record_path.unlink()
    with edit_state(staging) as state:
        set_song(state, song_dir, status="pending", staged=None, variant=None, files=None, flags=[])
    return record


def skip(staging, source):
    with edit_state(staging) as state:
        set_song(state, source, status="skipped", staged=None)
    _remove_staged(staging, source)


def requeue(staging, source):
    """Un-skip or retry: the next queue run will split it again."""
    with edit_state(staging) as state:
        set_song(state, source, status="pending", error=None)
