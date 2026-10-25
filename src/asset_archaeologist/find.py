"""Search client for agents and terminals. Standard library only (fast to start).

All queries go to the local server in one request; the server is started in the background
if it is not running (first start ~25 s while SigLIP loads, then milliseconds per search).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config


def base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def ping(port: int):
    try:
        with urllib.request.urlopen(base(port) + "/api/ping", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def ensure_server(cfg: Config, port: int, wait: int = 240):
    p = ping(port)
    if p and p.get("ready"):
        return
    if not p:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        log = open(cfg.output_dir / "server.log", "a", encoding="utf-8")
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "asset_archaeologist", "serve", "--port", str(port)],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True, **kw)
        print(f"starting the search server on {base(port)} (first start takes ~25 s)...", file=sys.stderr, flush=True)
    for _ in range(wait):
        time.sleep(1)
        p = ping(port)
        if p and p.get("ready"):
            return
        if p and p.get("model_error"):
            sys.exit(p["model_error"])
    sys.exit(f"the search server did not become ready; see {cfg.output_dir / 'server.log'}")


def post(port, path, body):
    req = urllib.request.Request(base(port) + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def get(port, path, **q):
    try:
        with urllib.request.urlopen(base(port) + path + "?" + urllib.parse.urlencode(q), timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def print_hits(title, hits):
    print(f"\n## {title}")
    if not hits:
        print("  (nothing)")
    for h in hits:
        size = " x ".join(f"{v:.2f}" for v in h.get("size_m") or [])
        print(f"- {h['name']}  [{size} m, {h.get('tris') or 0} tris, {h['pack']}]")
        print(f"    pack: {h['pack_file']}  file: {h['model']}")
        print(f"    thumb: {h['thumb']}")
        print(f"    sheet: {h['sheet']}")


def find(cfg: Config, queries, k=8, pack=None, min_m=None, max_m=None, by="both", as_json=False, port=0):
    port = port or cfg.port
    ensure_server(cfg, port)
    qs = [{key: v for key, v in {"q": q, "k": k, "pack": pack, "min_m": min_m, "max_m": max_m, "by": by}.items()
           if v is not None} for q in queries]
    res = post(port, "/api/batch", {"queries": qs})
    if as_json:
        print(json.dumps(res, indent=1))
        return res
    for r in res["results"]:
        print_hits(r["q"], r["hits"])
    print(f"\n({len(res['results'])} searches in {res['ms']} ms; browse at {base(port)}/)")
    if not any(r["hits"] for r in res["results"]):
        print("No models are indexed yet: run 'asset-archaeologist scan' or use Settings on the page.")
    return res


def similar(cfg: Config, pack, model, k=8, as_json=False, port=0):
    port = port or cfg.port
    ensure_server(cfg, port)
    hits = get(port, "/api/similar", pack=pack, model=model, k=k)
    if hits is None:
        sys.exit(f"no indexed model {model!r} in pack {pack!r} (use the 'pack' and 'model' fields of a search result)")
    if as_json:
        print(json.dumps(hits, indent=1))
    else:
        print_hits(f"looks like {model}", hits)
    return hits
