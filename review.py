"""Local review page for staged stems: listen, build variants, commit / skip / undo.

  python review.py [--port 8765] [--staging DIR]

Then open http://localhost:8765/ in a browser.
"""

import argparse
import functools
import http.server
import itertools
import json
import threading
import traceback
from pathlib import Path

import library
import stemsplit

ROOT = Path(__file__).resolve().parent

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


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, staging, port, **kwargs):
        self.staging = staging
        self.port = port
        super().__init__(*args, directory=str(staging), **kwargs)

    # ------------------------------------------------------------ reads
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send_bytes((ROOT / "review.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/library":
            return self.send_json(self.library())
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
        return {"songs": songs, "state": library.load_state(self.staging)["songs"]}

    # ------------------------------------------------------------ writes
    def do_POST(self):
        # Only this page may change things: a custom header can't be sent cross-site without
        # a CORS preflight (which this server never approves), and the Host must be local.
        host = (self.headers.get("Host") or "").lower()
        if self.headers.get("X-Stem-Review") != "1" or host not in (f"localhost:{self.port}", f"127.0.0.1:{self.port}"):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--staging", default=str(ROOT / "staging"))
    args = ap.parse_args()
    staging = Path(args.staging).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    library.sync_staged(staging)
    handler = functools.partial(Handler, staging=staging, port=args.port)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Review page: http://localhost:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
