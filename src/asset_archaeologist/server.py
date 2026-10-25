"""Local browser page and search API (http://127.0.0.1:<port>/).

Keeps SigLIP and every pack's embeddings in memory, so a search takes milliseconds once the
model has loaded (~25 s the first time). Picks up newly embedded packs on its own.
Listens on 127.0.0.1 only; nothing leaves the machine.

API (JSON):
    GET  /api/search?q=wooden+barrel&k=24[&pack=Kenney][&min_m=0.5&max_m=3][&by=both|image|name]
    POST /api/batch    {"queries": [{"q": "wooden barrel", "k": 8}, {"q": "goblin hut"}]}
    GET  /api/similar?pack=<pack key>&model=<file in pack>&k=24
    GET  /api/packs
    GET  /api/ping     {"ok": true, "ready": <model loaded>, "models": N}
    GET  /llms.txt     how an agent should use this
Settings and scanning (used by the page):
    GET/POST /api/config, POST /api/pick-folder, GET /api/scan/plan,
    POST /api/scan/start, POST /api/scan/stop, GET /api/scan/status
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np

from . import config as config_mod
from . import embed, scan
from .config import Config, PACKAGE_DIR

PAGE = PACKAGE_DIR / "web" / "index.html"


class Library:
    """All embedded packs, stacked; reloaded when an _embeddings.npz changes."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.stamp = None
        self.checked = 0.0
        self.keys, self.ids, self.rows, self.packs = [], [], [], {}
        self.img = self.nam = np.zeros((0, embed.DIM), np.float32)
        self.load()

    def _stamp(self):
        d = self.cfg.packs_dir
        if not d.exists():
            return ()
        return tuple(sorted((str(p), p.stat().st_mtime) for p in d.rglob("_embeddings.npz")))

    def load(self, force=False):
        if not force and time.time() - self.checked < 2:
            return
        self.checked = time.time()
        stamp = self._stamp()
        if stamp == self.stamp:
            return
        keys, ids, img, nam, rows, packs = [], [], [], [], [], {}
        for npz_s, _ in stamp:
            npz = Path(npz_s)
            ipath = npz.parent / "_index.json"
            if not ipath.exists():
                continue
            try:
                index = json.loads(ipath.read_text("utf-8"))
                d = np.load(npz)
            except Exception as e:
                print(f"skipping {npz}: {e}", flush=True)
                continue
            key = index["pack"]
            by_model = {e["model"]: e for e in index["models"]}
            n = 0
            for i, m in enumerate(d["ids"]):
                e = by_model.get(str(m))
                if not e or "thumb" not in e:
                    continue
                keys.append(key)
                ids.append(str(m))
                img.append(d["search"][i])
                nam.append(d["names"][i])
                rows.append(e)
                n += 1
            packs[key] = {"pack": key, "file": index.get("pack_path"), "models": n,
                          "licence_files": index.get("licence_files", []),
                          "contact_sheets": index.get("contact_sheets", []),
                          "contact_sheet_urls": ["/img/" + quote(c) for c in index.get("contact_sheets", [])],
                          "contact_sheet_paths": [str(self.cfg.packs_dir / c) for c in index.get("contact_sheets", [])]}
        with self.lock:
            self.keys, self.ids, self.rows, self.packs = keys, ids, rows, packs
            self.img = np.array(img, dtype=np.float32) if img else np.zeros((0, embed.DIM), np.float32)
            self.nam = np.array(nam, dtype=np.float32) if nam else np.zeros((0, embed.DIM), np.float32)
            self.stamp = stamp
        print(f"library: {len(packs)} packs, {len(ids)} models", flush=True)

    def result(self, j, score):
        e = self.rows[j]
        pd = self.cfg.packs_dir
        return {"score": round(float(score), 4), "name": e["name"], "pack": self.keys[j],
                "pack_file": self.packs[self.keys[j]]["file"],
                "model": self.ids[j], "size_m": e.get("size_m"), "tris": e.get("tris"),
                "texture": e.get("texture"), "tags": e.get("tags"),
                "thumb": str(pd / e["thumb"]), "sheet": str(pd / e["sheet"]),
                "thumb_url": "/img/" + quote(e["thumb"]), "sheet_url": "/img/" + quote(e["sheet"])}

    def mask(self, pack=None, min_m=None, max_m=None):
        ok = np.ones(len(self.ids), bool)
        if pack:
            ok &= np.array([pack.lower() in k.lower() for k in self.keys])
        if min_m is not None or max_m is not None:
            big = np.array([max(e.get("size_m") or [0]) for e in self.rows])
            if min_m is not None:
                ok &= big >= min_m
            if max_m is not None:
                ok &= big <= max_m
        return ok

    def search_many(self, queries):
        self.load()
        texts = [embed.PROMPT.format(q["q"]) for q in queries]
        vecs = embed.embed_text(texts)[0] if texts else np.zeros((0, embed.DIM))
        out = []
        with self.lock:
            for q, v in zip(queries, vecs):
                if not len(self.ids):
                    out.append([])
                    continue
                by = q.get("by", "both")
                img, nam = self.img @ v, self.nam @ v
                score = {"image": img, "name": nam}.get(by)
                if score is None:
                    score = embed.fuse(img, nam)
                score = np.where(self.mask(q.get("pack"), _num(q.get("min_m")), _num(q.get("max_m"))), score, -1e9)
                k = max(1, min(500, int(q.get("k", 24))))
                top = np.argsort(-score)[:k]
                out.append([self.result(j, score[j]) for j in top if score[j] > -1e9])
        return out

    def similar(self, pack, model, k=24):
        self.load()
        with self.lock:
            hit = [j for j in range(len(self.ids)) if self.keys[j] == pack and self.ids[j] == model]
            if not hit:  # also accept the model's display name
                hit = [j for j in range(len(self.ids)) if self.keys[j] == pack and self.rows[j]["name"].lower() == model.lower()]
            if not hit:
                return None
            s = self.img @ self.img[hit[0]]
            top = [j for j in np.argsort(-s) if j != hit[0]][:k]
            return [self.result(j, s[j]) for j in top]


def _num(x):
    return float(x) if x not in (None, "") else None


LLMS = """# Asset Archaeologist (local)

Finds 3D models the user already OWNS (human-made art from asset packs they bought) by
plain-language description. Search here before generating or modelling an asset yourself.
Everything is local: http://127.0.0.1:{port}/

Batch several searches in one request (one model pass, milliseconds):
  POST http://127.0.0.1:{port}/api/batch   (Content-Type: application/json)
  {{"queries": [{{"q": "wooden barrel", "k": 8}}, {{"q": "goblin hut", "k": 8, "pack": "Synty"}}]}}
Single: GET /api/search?q=wooden+barrel&k=12
Filters: pack=<text in the pack key>, min_m / max_m (largest side in metres), by=both|image|name.
Similar: GET /api/similar?pack=<result.pack>&model=<result.model>&k=12
Packs: GET /api/packs (each with its source file, licence/readme files and contact sheets).

Each result: name, pack, pack_file (the zip / .unitypackage / .blend / folder on disk), model
(file inside it; "file#part" when one file holds several models), size_m [x, y, z] in metres,
tris, texture status, tags, thumb + sheet (absolute PNG paths you can open and look at).
Look at the thumbnail or five-view sheet before choosing; scores only rank, they are not confidence.
To use a model, extract `model` from `pack_file` (it is a zip member path for zips).

Command line (starts this server if needed, prints JSON with --json):
  {cli} find "wooden barrel" "goblin hut" -k 8 --json
  {cli} similar "<pack>" "<model>"
Licences differ per pack: read the pack's licence files before shipping anything.
"""


def make_handler(state):
    lib: Library = state["lib"]
    cfg: Config = state["cfg"]
    port = state["port"]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, bytes):
                data = body
            else:
                data = (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if not ctype.startswith("image") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if not ctype.startswith("image") else "max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            # refuse requests that reach us under another name (DNS rebinding)
            return self.headers.get("Host", "") in hosts

        def need_model(self):
            if embed.loaded():
                return False
            err = state.get("model_error")
            self.send(503, {"error": err or "the search model is still loading; try again in a few seconds",
                            "ready": False})
            return True

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, {"error": "bad host"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                return self.route_get(u.path, q)
            except Exception as e:
                return self.send(500, {"error": f"{type(e).__name__}: {e}"})

        def route_get(self, path, q):
            if path in ("/", "/index.html"):
                return self.send(200, PAGE.read_text("utf-8"), "text/html")
            if path == "/llms.txt":
                return self.send(200, LLMS.format(port=port, cli=config_mod.cli_command()), "text/plain")
            if path == "/api/ping":
                return self.send(200, {"ok": True, "ready": embed.loaded(), "models": len(lib.ids),
                                       "model_error": state.get("model_error")})
            if path == "/api/packs":
                lib.load(force=True)
                return self.send(200, sorted(lib.packs.values(), key=lambda p: p["pack"].lower()))
            if path == "/api/search":
                if not q.get("q"):
                    return self.send(400, {"error": "q is required"})
                if self.need_model():
                    return
                return self.send(200, lib.search_many([{"q": q["q"], "k": int(q.get("k", 24)), "pack": q.get("pack"),
                                                        "min_m": q.get("min_m"), "max_m": q.get("max_m"),
                                                        "by": q.get("by", "both")}])[0])
            if path == "/api/similar":
                r = lib.similar(q.get("pack", ""), q.get("model", ""), int(q.get("k", 24)))
                return self.send(200 if r is not None else 404, r if r is not None else {"error": "model not found"})
            if path == "/api/config":
                return self.send(200, config_info(cfg))
            if path == "/api/scan/status":
                return self.send(200, scan_status(state))
            if path == "/api/scan/plan":
                packs, todo = scan.plan(cfg)
                return self.send(200, {"packs": len(packs), "todo": len(todo),
                                       "list": [str(p) for p in todo[:500]]})
            if path.startswith("/img/"):
                rel = unquote(path[5:])
                base = cfg.packs_dir.resolve()
                p = (base / rel).resolve()
                if base not in p.parents or not p.is_file() or p.suffix.lower() != ".png":
                    return self.send(404, {"error": "not found"})
                return self.send(200, p.read_bytes(), "image/png")
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.host_ok():
                return self.send(403, {"error": "bad host"})
            # JSON only: a web page elsewhere cannot send this without a CORS preflight, which we never allow
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send(415, {"error": "send Content-Type: application/json"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in hosts:
                return self.send(403, {"error": "bad origin"})
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception as e:
                return self.send(400, {"error": f"bad JSON: {e}"})
            try:
                return self.route_post(u.path, body)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Exception as e:
                return self.send(500, {"error": f"{type(e).__name__}: {e}"})

        def route_post(self, path, body):
            if path == "/api/batch":
                queries = [q if isinstance(q, dict) else {"q": str(q)} for q in body.get("queries", [])]
                queries = [q for q in queries if q.get("q")]
                if self.need_model():
                    return
                t = time.time()
                res = lib.search_many(queries)
                return self.send(200, {"ms": round((time.time() - t) * 1000), "results": [
                    {"q": q["q"], "hits": r} for q, r in zip(queries, res)]})
            if path == "/api/config":
                if scan_status(state).get("running"):
                    return self.send(409, {"error": "settings cannot change while a scan runs"})
                vals = {k: v for k, v in body.items() if k in ("libraries", "output", "work", "blender", "procs",
                                                               "device", "priority", "folder_pack_depth")}
                config_mod.update(cfg, vals)
                config_mod.save(cfg)
                lib.load(force=True)
                return self.send(200, config_info(cfg))
            if path == "/api/pick-folder":
                return self.send(200, {"path": pick_folder(body.get("title") or "Choose a folder")})
            if path == "/api/scan/start":
                return self.send(*start_scan(state))
            if path == "/api/scan/stop":
                ev = state.get("stop")
                if ev and state.get("thread") and state["thread"].is_alive():
                    ev.set()
                    return self.send(200, {"stopping": True})
                return self.send(409, {"error": "no scan started from this page is running"})
            self.send(404, {"error": "not found"})

    return H


def config_info(cfg: Config) -> dict:
    b = cfg.blender_path()
    ver = config_mod.blender_version(b) if b else None
    out = cfg.to_dict()
    out.update(config_file=str(cfg.path), output_dir=str(cfg.output_dir), work_dir=str(cfg.work_dir),
               blender_found=str(b) if b else None, blender_version=".".join(map(str, ver)) if ver else None,
               blender_ok=bool(ver and tuple(ver[:2]) >= config_mod.MIN_BLENDER), blender_help=config_mod.blender_help(),
               libraries_missing=[p for p in cfg.libraries if not Path(p).expanduser().is_dir()],
               cli=config_mod.cli_command())
    return out


def scan_status(state) -> dict:
    s = scan.read_status(state["cfg"])
    t = state.get("thread")
    s["from_page"] = bool(t and t.is_alive())
    if state.get("scan_error"):
        s["error"] = state["scan_error"]
    return s


def start_scan(state):
    cfg = state["cfg"]
    if scan_status(state).get("running"):
        return 409, {"error": "a scan is already running"}
    if not cfg.library_dirs:
        return 400, {"error": "add at least one library folder first"}
    if not cfg.blender_path():
        return 400, {"error": "Blender was not found. " + config_mod.blender_help()}
    stop = threading.Event()
    state["stop"], state["scan_error"] = stop, None

    def work():
        try:
            if not embed.loaded():
                embed.load(cfg)
            scan.run(cfg, stop=stop)
        except Exception as e:
            state["scan_error"] = f"{type(e).__name__}: {e}"
            print("scan failed:", state["scan_error"], flush=True)
        state["lib"].load(force=True)

    t = threading.Thread(target=work, daemon=True, name="scan")
    state["thread"] = t
    t.start()
    time.sleep(0.3)
    return 200, {"started": True}


PICK = r"""
import sys, tkinter as tk
from tkinter import filedialog
r = tk.Tk(); r.withdraw()
try: r.attributes("-topmost", True)
except Exception: pass
print(filedialog.askdirectory(title=sys.argv[1]) or "")
"""


def pick_folder(title: str) -> str:
    """A native folder chooser (tkinter) in its own process; empty if cancelled or unavailable."""
    try:
        out = subprocess.run([sys.executable, "-c", PICK, title], capture_output=True, text=True, timeout=600)
        return out.stdout.strip()
    except Exception:
        return ""


def serve(cfg: Config, port: int = 0, open_browser: bool = False):
    port = port or cfg.port
    state = {"cfg": cfg, "port": port, "lib": Library(cfg)}
    ThreadingHTTPServer.allow_reuse_address = False
    ThreadingHTTPServer.daemon_threads = True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    except OSError as e:
        from .find import ping
        if ping(port):  # already running: just show it
            print(f"Asset Archaeologist is already running on http://127.0.0.1:{port}/", flush=True)
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{port}/")
            return
        sys.exit(f"port {port} is in use ({e}). Is Asset Archaeologist already running? "
                 f"Open http://127.0.0.1:{port}/ or set another port in {cfg.path}")

    def warm():
        t = time.time()
        try:
            embed.load(cfg)
            embed.embed_text(["warm up"])
            print(f"search model ready on {embed.DEVICE} in {time.time() - t:.0f} s", flush=True)
        except Exception as e:
            state["model_error"] = f"the search model could not load: {type(e).__name__}: {e}"
            print(state["model_error"], flush=True)

    threading.Thread(target=warm, daemon=True, name="model").start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Asset Archaeologist on {url} ({len(state['lib'].ids)} models). Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
