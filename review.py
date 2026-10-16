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
import threading
import traceback
import webbrowser
from pathlib import Path

import library
import stemsplit

ROOT = Path(__file__).resolve().parent

MODELS = {
    "BS-Roformer-SW.ckpt": "BS-RoFormer SW: best quality (recommended)",
    "htdemucs_6s.yaml": "Demucs 6-stem: a little faster, weaker guitar and keys",
}

_jobs = {}
_job_ids = itertools.count(1)
_remix_lock = threading.Lock()


def _run_remix(job_id, staging, folder, fill, gate):
    with _remix_lock:  # one at a time: each uses a few GB of RAM for long songs
        _jobs[job_id]["status"] = "running"
        try:
            _jobs[job_id].update(status="done", folder=stemsplit.remix(staging / folder, fill, gate))
        except Exception as e:
            traceback.print_exc()
            _jobs[job_id].update(status="error", error=f"{type(e).__name__}: {e}")


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
    looks_like_song = (p / "song.ogg").exists() or (p / "song.ini").exists()
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
                        files=sorted(p.stem for p in path.glob("*.ogg")),
                        truth=[], raw=sorted(p.stem for p in (self.staging / raw_folder / "raw").glob("*.ogg")))
            songs.append(data)
        for m in sorted(self.staging.glob("BENCH - */manifest.json")):
            data = stemsplit.read_manifest(m.parent)
            path = m.parent
            data.update(folder=path.name, raw_folder=path.name,
                        files=sorted(p.stem for p in path.glob("*.ogg")),
                        truth=sorted(p.stem for p in (path / "truth").glob("*.ogg")),
                        raw=sorted(p.stem for p in (path / "raw").glob("*.ogg")))
            songs.append(data)
        return {"songs": songs, "state": library.load_state(self.staging)["songs"],
                "has_settings": bool(library.load_settings(self.staging)["roots"]), "queue_running": QUEUE.running}

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
                job_id = str(next(_job_ids))
                _jobs[job_id] = {"status": "queued"}
                threading.Thread(target=_run_remix, args=(job_id, self.staging, folder, fill, gate), daemon=True).start()
                return self.send_json({"job": job_id})
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
                found, todo = library.scan(s["roots"], self.staging, s["retry_failed"])
                return self.send_json({"found": found, "todo": len(todo)})
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
