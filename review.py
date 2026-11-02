"""Local web app for the stem separator: pick library folders, split, listen, commit / skip / undo.

  python review.py [--port 8765] [--staging DIR] [--open]

Then open http://localhost:8765/ in a browser (--open does that for you).
"""

import argparse
import collections
import functools
import http.server
import itertools
import json
import os
import socket
import string
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import library
import stemsplit
import vocals

ROOT = Path(__file__).resolve().parent

MODELS = {
    "BS-Roformer-SW.ckpt": "BS-RoFormer SW: best quality (recommended)",
    "htdemucs_6s.yaml": "Demucs 6-stem: a little faster, weaker guitar and keys",
}

_device = None


def device_info():
    """What splitting runs on, for the page. Imports torch once (a couple of seconds)."""
    global _device
    if _device is None:
        import torch
        marker = Path(sys.prefix) / "stemsep-backend.json"
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        installed, fallback_from = info.get("device"), info.get("fallback_from")
        if torch.cuda.is_available():
            kind = "amd" if getattr(torch.version, "hip", None) else "nvidia"
            _device = {"kind": kind, "name": torch.cuda.get_device_name(0), "gpu": True}
        else:
            _device = {"kind": "cpu", "name": "CPU", "gpu": False}
        _device["installed_for"] = installed
        _device["experimental"] = kind_is_experimental = _device["kind"] == "amd" and os.name == "nt"
        _device["minutes_per_song"] = 0.75 if _device["gpu"] else 13
        _device["note"] = (
            "AMD on Windows is experimental and untested; please report how it goes." if kind_is_experimental
            else "An AMD graphics card was found but PyTorch couldn't use it, so the CPU version was installed."
            if fallback_from == "amd"
            else f"An {installed.upper()} build is installed but PyTorch can't use the GPU, so splitting runs on the CPU."
            if installed in ("nvidia", "amd") and not _device["gpu"]
            else "")
    return _device


_jobs = {}
_job_ids = itertools.count(1)
_remix_lock = threading.Lock()


def _run_job(job_id, work):
    """Run work() -> staged folder name as a background job the page polls."""
    with _remix_lock:  # one at a time: each uses a few GB of RAM for long songs
        _jobs[job_id]["status"] = "running"
        try:
            _jobs[job_id].update(status="done", folder=work())
        except Exception as e:
            traceback.print_exc()
            _jobs[job_id].update(status="error", error=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")


def start_job(work):
    job_id = str(next(_job_ids))
    _jobs[job_id] = {"status": "queued"}
    threading.Thread(target=_run_job, args=(job_id, lambda: work(_jobs[job_id])), daemon=True).start()
    return job_id


_vocals_ok = None


def vocals_available():
    global _vocals_ok
    if _vocals_ok is None:
        ok, why = vocals.available()
        _vocals_ok = {"ok": ok, "why": why}
    return _vocals_ok


def add_vocals(job, staging, folder, lyrics):
    base = vocals.base_folder(staging, folder)
    source = stemsplit.read_manifest(base)["source"]
    with library.edit_state(staging) as state:  # kept so a re-split can offer them again
        library.set_song(state, source, lyrics=lyrics, instrumental=False)
    vocals.add_vocals(base, lyrics, log=lambda step: job.update(step=step))
    return base.name


def remove_vocals(job, staging, folder):
    base = vocals.base_folder(staging, folder)
    vocals.remove_vocals(base)
    return base.name


def check_vocals_in_background(staging):
    """Look for committed songs with no vocals charted (slow the first time; see library)."""
    def run():
        try:
            library.check_committed_vocals(staging)
        except Exception:
            traceback.print_exc()
    threading.Thread(target=run, daemon=True).start()


_chart_lyrics = {}


def chart_lyrics(song_dir):
    """The lyric events of a song's notes.chart as text, to start the lyrics box from ("" if none)."""
    import chartfile
    path = stemsplit.chart_file(song_dir)
    if path is None or path.suffix != ".chart":
        return ""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _chart_lyrics:
        try:
            _chart_lyrics[key] = chartfile.lyrics_text(chartfile.parse(path))
        except (OSError, ValueError):
            _chart_lyrics[key] = ""
    return _chart_lyrics[key]


class QueueRunner:
    """Runs library.run_queue in a background thread and keeps its progress for the page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.stats = {}
        self.log = collections.deque(maxlen=300)

    def start(self, staging, settings):
        with self.lock:
            if self.running:
                raise ValueError("splitting is already running")
            if not settings["roots"]:
                raise ValueError("add a library folder first")
            self.running, self.stop_requested, self.stats = True, False, {}
            self.log.clear()
        threading.Thread(target=self._run, args=(staging, settings), daemon=True).start()

    def _run(self, staging, settings):
        try:
            library.run_queue(settings["roots"], staging, settings["model"], settings["retry_failed"],
                              auto_variants=settings["auto_variants"], log=self._log,
                              progress=self._progress, should_stop=lambda: self.stop_requested)
        except Exception as e:
            traceback.print_exc()
            self._log(f"Stopped because of an error: {type(e).__name__}: {e}")
        finally:
            self.running = False

    def _log(self, line):
        print(line, flush=True)
        self.log.append(line)

    def _progress(self, stats):
        self.stats = stats

    def status(self):
        return {"running": self.running, "stopping": self.running and self.stop_requested,
                "stats": self.stats, "log": list(self.log)[-80:]}


QUEUE = QueueRunner()


def list_dirs(path):
    """Folder browser data: drives when path is empty, otherwise the subfolders of path."""
    if not path:
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")] if os.name == "nt" else ["/"]
        return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in drives]}
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"not a folder: {path}")
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.is_dir() and not child.name.startswith((".", "$")):
                dirs.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else ""
    looks_like_song = stemsplit.song_audio(p) is not None or (p / "song.ini").exists()
    return {"path": str(p), "parent": parent, "dirs": dirs, "is_song": looks_like_song}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, staging, port, **kwargs):
        self.staging = staging
        self.port = port
        super().__init__(*args, directory=str(staging), **kwargs)

    def local_host(self):
        host = (self.headers.get("Host") or "").lower()
        return host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}")

    # ------------------------------------------------------------ reads
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send_bytes((ROOT / "review.html").read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/") and not self.local_host():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/api/library":
            return self.send_json(self.library())
        if self.path == "/api/settings":
            return self.send_json({"settings": library.load_settings(self.staging), "models": MODELS,
                                   "device": device_info(),
                                   "queue": QUEUE.status()})
        if self.path == "/api/queue":
            return self.send_json(QUEUE.status())
        if self.path.startswith("/api/job/"):
            job = _jobs.get(self.path.rsplit("/", 1)[-1])
            return self.send_json(job or {"status": "error", "error": "unknown job"})
        return super().do_GET()

    def library(self):
        songs = []
        for folder, data in library.staged_folders(self.staging).items():
            path = self.staging / folder
            raw_folder = folder if (path / "raw").exists() else data.get("raw_from", folder)
            data.update(folder=folder, raw_folder=raw_folder,
                        files=sorted(p.stem for p in path.glob("*.ogg") if p.stem != "original"),
                        original_file=data.get("original_file", "original.ogg"),
                        truth=[], raw=sorted(p.stem for p in (self.staging / raw_folder / "raw").glob("*.ogg")))
            songs.append(data)
        for folder, data in library.staged_folders(self.staging, kinds=("vocals",)).items():
            data.update(folder=folder, raw_folder=folder, raw=[], truth=[],
                        files=sorted(p.stem for p in (self.staging / folder).glob("*.ogg") if p.stem != "original"))
            songs.append(data)
        for m in sorted(self.staging.glob("BENCH - */manifest.json")):
            data = stemsplit.read_manifest(m.parent)
            path = m.parent
            data.update(folder=path.name, raw_folder=path.name,
                        files=sorted(p.stem for p in path.glob("*.ogg")),
                        truth=sorted(p.stem for p in (path / "truth").glob("*.ogg")),
                        raw=sorted(p.stem for p in (path / "raw").glob("*.ogg")))
            songs.append(data)
        state = library.load_state(self.staging)
        con_counts = {}
        for info in state.get("cons", {}).values():
            con_counts[info["status"]] = con_counts.get(info["status"], 0) + 1
        skipped_cons = sorted(
            ({"song": " - ".join(x for x in (info.get("artist"), info.get("name")) if x) or Path(path).name,
              "file": path, "reason": info.get("reason", info["status"])}
             for path, info in state.get("cons", {}).items() if info["status"] != "convertible"),
            key=lambda c: c["song"].lower())
        # A .chart's own lyric events start the lyrics box, for songs that will be asked for lyrics.
        lyrics = {}
        for s in songs:
            if s.get("kind") != "split":
                continue
            chart = stemsplit.chart_file(s.get("chart_source") or s.get("chart_dir") or s["source"])
            s["chart_file"] = chart.name if chart else None
            if not s.get("variant_of") and s.get("charted", {}).get("vocals") is False:
                lyrics[s["source"]] = chart_lyrics(s.get("chart_dir") or s["source"])
        for source, e in state["songs"].items():
            if e.get("status") == "committed" and e.get("needs_vocals"):
                lyrics[source] = chart_lyrics(e.get("folder") or source)
        return {"songs": songs, "state": state["songs"], "chart_lyrics": {k: v for k, v in lyrics.items() if v},
                "cons_text": library.describe_cons(con_counts) if con_counts else "",
                "skipped_cons": skipped_cons,
                "con_backups": str(library.con_backup_dir(self.staging)),
                "has_settings": bool(library.load_settings(self.staging)["roots"]), "queue_running": QUEUE.running,
                "vocals_checking": library.vocals_check_running(),
                "vocals": vocals_available()}

    # ------------------------------------------------------------ writes
    def do_POST(self):
        # Only this page may change things: a custom header can't be sent cross-site without
        # a CORS preflight (which this server never approves), and the Host must be local.
        if self.headers.get("X-Stem-Review") != "1" or not self.local_host():
            return self.send_json({"error": "forbidden"}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            action = self.path.removeprefix("/api/")
            if action == "remix":
                folder = self.staged_folder(body.get("folder"))
                fill = [s for s in body.get("fill", []) if s in stemsplit.STEM_MAP]
                gate = body.get("gate")
                gate = None if gate is None else max(-60.0, min(0.0, float(gate)))
                if not fill and gate is None:
                    raise ValueError("choose at least one option")
                return self.send_json({"job": start_job(lambda job: stemsplit.remix(self.staging / folder, fill, gate))})
            if action == "vocals":
                folder, lyrics = self.staged_folder(body.get("folder")), str(body.get("lyrics") or "")
                if not lyrics.strip():
                    raise ValueError("paste the lyrics first")
                if len(lyrics) > 50000:
                    raise ValueError("that's too long for one song's lyrics")
                return self.send_json({"job": start_job(lambda job: add_vocals(job, self.staging, folder, lyrics))})
            if action == "vocals/remove":
                folder = self.staged_folder(body.get("folder"))
                return self.send_json({"job": start_job(lambda job: remove_vocals(job, self.staging, folder))})
            if action == "vocals/committed":
                source, lyrics = self.known_source(body.get("source")), str(body.get("lyrics") or "")
                if not lyrics.strip():
                    raise ValueError("paste the lyrics first")
                if len(lyrics) > 50000:
                    raise ValueError("that's too long for one song's lyrics")
                return self.send_json({"job": start_job(lambda job: library.stage_committed_vocals(
                    self.staging, source, lyrics, log=lambda step: job.update(step=step)))})
            if action == "vocals/commit":
                return self.send_json({"ok": True, "record": library.commit_vocals(self.staging, self.staged_folder(body.get("folder")))})
            if action == "vocals/discard":
                library.discard_vocals(self.staging, self.staged_folder(body.get("folder")))
                return self.send_json({"ok": True})
            if action == "vocals/undo":
                library.undo_vocals(self.staging, self.known_source(body.get("source")))
                return self.send_json({"ok": True})
            if action == "instrumental":
                library.set_instrumental(self.staging, self.known_source(body.get("source")), body.get("value", True))
                return self.send_json({"ok": True})
            if action == "commit":
                record = library.commit(self.staging, self.staged_folder(body.get("folder")))
                return self.send_json({"ok": True, "record": record})
            if action == "commit-ready":
                committed, errors = library.commit_ready(self.staging)
                return self.send_json({"ok": True, "committed": committed, "errors": errors})
            if action == "settings":
                return self.send_json({"ok": True, "settings": self.save_settings(body)})
            if action == "dirs":
                return self.send_json(list_dirs(body.get("path") or ""))
            if action == "scan":
                s = library.load_settings(self.staging)
                found, todo, cons, held = library.scan(s["roots"], self.staging, s["retry_failed"])
                check_vocals_in_background(self.staging)
                return self.send_json({"found": found, "todo": len(todo), "cons": cons,
                                       "cons_text": library.describe_cons(cons) if cons else "",
                                       "held_text": library.describe_held(held)})
            if action == "queue/start":
                QUEUE.start(self.staging, library.load_settings(self.staging))
                return self.send_json({"ok": True})
            if action == "queue/stop":
                QUEUE.stop_requested = True
                return self.send_json({"ok": True})
            source = self.known_source(body.get("source"))
            if action == "skip":
                library.skip(self.staging, source)
            elif action == "undo":
                library.undo(self.staging, source)
            elif action == "requeue":
                library.requeue(self.staging, source)
            else:
                return self.send_json({"error": "unknown action"}, 404)
            return self.send_json({"ok": True})
        except (ValueError, OSError, TimeoutError) as e:
            return self.send_json({"error": str(e)}, 400)

    def save_settings(self, body):
        settings = library.load_settings(self.staging)
        if "roots" in body:
            roots = []
            for r in body["roots"]:
                p = Path(str(r)).expanduser()
                if not p.is_dir():
                    raise ValueError(f"not a folder: {r}")
                if str(p.resolve()) not in roots:
                    roots.append(str(p.resolve()))
            settings["roots"] = roots
        if body.get("model") in MODELS:
            settings["model"] = body["model"]
        for key in ("auto_variants", "retry_failed"):
            if key in body:
                settings[key] = bool(body[key])
        library.save_settings(self.staging, settings)
        return settings

    def staged_folder(self, folder):
        path = (self.staging / str(folder or "")).resolve()
        if path.parent != self.staging or not (path / "manifest.json").exists():
            raise ValueError("unknown staged folder")
        return path.name

    def known_source(self, source):
        if not source or source not in library.load_state(self.staging)["songs"]:
            raise ValueError("unknown song")
        return source

    # ------------------------------------------------------------ helpers
    def send_json(self, data, code=200):
        self.send_bytes(json.dumps(data).encode(), "application/json", code)

    def send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    # Windows lets a second process bind a port in use when SO_REUSEADDR is set; claim it exclusively
    # so a second launch notices the first one instead of silently sharing the port.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # The browser closing a connection early (page reload, tab closed) isn't worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--staging", default=str(ROOT / "staging"))
    ap.add_argument("--open", dest="open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--no-open", dest="open", action="store_false")
    args = ap.parse_args()
    staging = Path(args.staging).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    url = f"http://localhost:{args.port}/"
    handler = functools.partial(Handler, staging=staging, port=args.port)
    try:
        server = Server(("127.0.0.1", args.port), handler)
    except OSError:
        print(f"Already running at {url}")
        if args.open:
            webbrowser.open(url)
        return
    library.sync_staged(staging)
    threading.Thread(target=device_info, daemon=True).start()  # load PyTorch now, not on the first page view
    check_vocals_in_background(staging)
    print(f"Stem separator is running at {url}", flush=True)
    print("Keep this window open while you use it; close it (or press Ctrl+C) to stop.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
