"""Find every pack in the library folders, render and embed each one, resumable.

Packs are zips, .unitypackage files, loose .blend files and folders of loose model files.
One download per product is kept (a pack downloaded as source files + Godot + Unity + Unreal
is rendered once, from the source files). Packs whose index says "complete" and "embedded"
are skipped, so a stopped scan picks up where it left off.

Progress goes to <output>/_scan_status.json (read by the browser page) and <output>/scan.log;
<output>/_scan_state.json keeps every pack's outcome.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path, PurePosixPath

from . import render
from .config import Config

MODEL_EXT = set(render.FORMATS)  # incl. .blend
# which download of a product to render (lower first): source files over engine exports
PREFER = [r"sourcefiles|source_files", r"blender", r"godot", r"fbx|obj", r"\.zip$", r"unreal|ue\d", r"\.unitypackage$"]
IGNORE_DIRS = {"$recycle.bin", "__macosx", "system volume information", ".git", "node_modules"}


def product(path: Path) -> str:
    s = path.name
    s = re.split(r"[_ -](SourceFiles|Source_Files|Unity|Unreal|Godot|UE\d|URP|HDRP|Built_?In)", s, flags=re.I)[0]
    s = re.sub(r"\.(zip|unitypackage|blend)$", "", s, flags=re.I)
    return f"{path.parent.as_posix().lower()}/{s.lower()}"


def rank(path: Path) -> int:
    name = path.name.lower()
    return next((i for i, p in enumerate(PREFER) if re.search(p, name)), len(PREFER))


def zip_models(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as z:
            return sum(1 for n in z.namelist() if PurePosixPath(n).suffix.lower() in MODEL_EXT | {".unitypackage"}
                       and not render.SKIP.search(n))
    except Exception:
        return 0


def _walk(root: Path, skip: set[Path]):
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if n.lower() not in IGNORE_DIRS and (d / n).resolve() not in skip]
        for f in filenames:
            if not f.startswith("._"):
                yield d / f


def discover(cfg: Config) -> list[Path]:
    skip = {cfg.output_dir.resolve(), cfg.work_dir.resolve()}
    found = []
    depth = max(1, int(cfg.folder_pack_depth))
    for root in cfg.library_dirs:
        if not root.exists():
            continue
        files = list(_walk(root, skip))
        zips = {p.with_suffix("").as_posix().lower() for p in files if p.suffix.lower() == ".zip"}
        loose_dirs = {}
        for p in files:
            ext = p.suffix.lower()
            if ext == ".zip":
                if zip_models(p):
                    found.append(p)
            elif ext == ".unitypackage":
                found.append(p)
            elif ext in MODEL_EXT:
                # loose files: skip folders that are an unpacked copy of a zip next to them
                parents = [q for q in [p.parent, *p.parents] if root in q.parents or q == root]
                if any(q.as_posix().lower() in zips for q in parents):
                    continue
                if ext == ".blend":
                    found.append(p)
                else:
                    # a folder of loose models is one pack: <root>/<pack>/... (depth levels down)
                    rel = p.relative_to(root).parts
                    pack_dir = root.joinpath(*rel[:depth]) if len(rel) > depth else p.parent
                    loose_dirs[pack_dir] = loose_dirs.get(pack_dir, 0) + 1
        found += list(loose_dirs)
    best = {}
    for p in found:
        k = product(p)
        if k not in best or rank(p) < rank(best[k]):
            best[k] = p
    packs = sorted(best.values(), key=lambda p: p.as_posix().lower())
    first = [p for pat in cfg.priority for p in packs if re.search(pat, p.as_posix(), re.I)]
    rest = [p for p in packs if p not in first]
    return list(dict.fromkeys(first + rest))


def done(cfg: Config, p: Path) -> bool:
    idx = render.index_path(cfg, render.pack_key(cfg, p))
    if not idx.exists():
        return False
    try:
        ix = json.loads(idx.read_text("utf-8"))
    except Exception:
        return False
    return bool(ix.get("complete") and ix.get("embedded"))


def plan(cfg: Config):
    packs = discover(cfg)
    todo = [p for p in packs if not done(cfg, p)]
    return packs, todo


# ---------- status shared with the browser page ----------

def status_path(cfg: Config) -> Path:
    return cfg.output_dir / "_scan_status.json"


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def read_status(cfg: Config) -> dict:
    try:
        s = json.loads(status_path(cfg).read_text("utf-8"))
    except Exception:
        return {"running": False}
    if s.get("running") and not pid_alive(s.get("pid", 0)):
        s["running"] = False
        s["note"] = "the scan stopped unexpectedly; start it again to continue where it left off"
    return s


class Status:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data = {"running": True, "pid": os.getpid(), "started": time.time(), "packs_total": 0, "pack_index": 0,
                     "pack": "", "pack_models": 0, "pack_rendered": 0, "models_done": 0, "failed_packs": 0,
                     "log": []}
        self.lock = threading.Lock()

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.data["updated"] = time.time()
            self._write()

    def log(self, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        print(line, flush=True)
        with self.lock:
            self.data["log"] = (self.data["log"] + [line])[-30:]
            try:
                with (self.cfg.output_dir / "scan.log").open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
            self._write()

    def _write(self):
        p = status_path(self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), "utf-8")
        os.replace(tmp, p)


def read_redo(cfg: Config, path: Path) -> set[str]:
    """Pack keys from a file with one pack per line: a key as the page shows it
    ('Kenney/kenney_nature-kit') or the pack's path. Blank lines and lines starting with '#' are skipped."""
    keys = set()
    for line in Path(path).read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        keys.add(render.pack_key(cfg, p) if p.exists() else line.replace("\\", "/"))
    return keys


def run(cfg: Config, procs: int = 0, max_packs: int = 0, paths: list[Path] | None = None, force: bool = False,
        limit: int = 0, only: str = "", keep: bool = False, stop: threading.Event | None = None,
        redo: set[str] | None = None) -> dict:
    """Render + embed every pack still to do (or the given paths). Packs whose key is in redo
    are rendered again from scratch, their old output deleted first. Returns a summary."""
    from . import embed  # loads torch

    cur = read_status(cfg)
    if cur.get("running") and cur.get("pid") != os.getpid():
        raise RuntimeError(f"a scan is already running (process {cur.get('pid')})")
    if not cfg.blender_path():
        raise render.RenderError("Blender was not found. Install Blender 4.2 or newer, or set its path in the settings.")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    st = Status(cfg)
    st.update()
    if paths:
        packs = [Path(p) for p in paths]
    else:
        st.log("looking for packs in " + ", ".join(str(p) for p in cfg.library_dirs))
        packs = [p for p in discover(cfg) if force or not done(cfg, p) or (redo and render.pack_key(cfg, p) in redo)]
    packs = packs[: max_packs or None]
    state_file = cfg.output_dir / "_scan_state.json"
    try:
        state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else {}
    except Exception:
        state = {}
    st.update(packs_total=len(packs))
    st.log(f"scan start: {len(packs)} packs to do")
    t0 = time.time()
    models_done = failed = 0
    try:
        for i, p in enumerate(packs, 1):
            if stop is not None and stop.is_set():
                st.log("scan stopped by request")
                break
            key = render.pack_key(cfg, p)
            st.update(pack_index=i, pack=key, pack_models=0, pack_rendered=0)
            t = time.time()
            try:
                if redo and key in redo:  # nothing stale left next to the new pictures
                    render.remove_output(cfg, key)
                ipath = render.do_pack(cfg, p, procs=procs, limit=limit, only=only, progress=lambda d: st.update(**d))
                ix = json.loads(ipath.read_text("utf-8"))
                ok = [e for e in ix["models"] if "thumb" in e]
                if ok:
                    st.update(pack_phase="embedding")
                    embed.embed_pack(cfg, ipath)
                    ix = json.loads(ipath.read_text("utf-8"))
                ix["embedded"] = True
                ipath.write_text(json.dumps(ix, indent=1, ensure_ascii=False) + "\n", "utf-8")
                bad = len(ix["models"]) - len(ok)
                models_done += len(ok)
                state[key] = {"source": str(p), "models": len(ok), "failed": bad, "seconds": round(time.time() - t)}
                st.update(models_done=models_done, pack_phase="")
                st.log(f"[{i}/{len(packs)}] {key}: {len(ok)} models, {bad} failed, {time.time() - t:.0f} s")
            except Exception as e:
                failed += 1
                state[key] = {"source": str(p), "error": f"{type(e).__name__}: {e}"[:300]}
                st.update(failed_packs=failed)
                st.log(f"[{i}/{len(packs)}] {key}: FAILED {type(e).__name__}: {e}")
                tb = traceback.format_exc().strip().splitlines()
                if len(tb) >= 3:
                    st.log(tb[-3].strip())
            finally:
                if not keep:
                    render.cleanup(cfg, key)
                state_file.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", "utf-8")
    finally:
        mins = (time.time() - t0) / 60
        st.log(f"scan end: {models_done} models from {len(packs)} packs in {mins:.1f} min")
        st.update(running=False, finished=time.time())
    return {"packs": len(packs), "models": models_done, "failed_packs": failed}
