"""Checks that everything needed is in place, in plain words."""
from __future__ import annotations

import shutil
import socket
import sys
from pathlib import Path

from . import config as config_mod
from .config import Config

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _free_gb(p: Path) -> float | None:
    q = p
    while not q.exists() and q.parent != q:
        q = q.parent
    try:
        return shutil.disk_usage(q).free / 1e9
    except OSError:
        return None


def checks(cfg: Config, fetch_model: bool = False):
    out = []
    add = lambda status, what, detail="": out.append((status, what, detail))

    add(OK, "Settings file", str(cfg.path))
    v = sys.version_info
    add(OK if v >= (3, 11) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})")

    # libraries
    if not cfg.libraries:
        add(WARN, "Library folders", "none set yet: add them on the Settings screen of the page, or run "
                                     "'asset-archaeologist config --add-library <folder>'")
    for p in cfg.libraries:
        d = Path(p).expanduser()
        add(OK if d.is_dir() else FAIL, "Library folder", f"{d}" + ("" if d.is_dir() else "  (not found)"))

    # output and work folders
    for label, d, need in (("Output folder", cfg.output_dir, 1.0), ("Work folder", cfg.work_dir, 5.0)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            t = d / ".write_test"
            t.write_text("x")
            t.unlink()
            free = _free_gb(d)
            st = OK if free is None or free >= need else WARN
            add(st, label, f"{d}  ({free:.0f} GB free)" if free is not None else str(d))
        except OSError as e:
            add(FAIL, label, f"{d}: cannot write ({e})")

    # Blender
    b = cfg.blender_path()
    if not b:
        add(FAIL, "Blender", ("the configured path does not exist: " + cfg.blender + ". ") if cfg.blender else
            "not found. " + config_mod.blender_help())
    else:
        ver = config_mod.blender_version(b)
        if not ver:
            add(FAIL, "Blender", f"{b} did not report a version (is it a Blender executable?)")
        elif tuple(ver[:2]) < config_mod.MIN_BLENDER:
            add(FAIL, "Blender", f"{b} is {'.'.join(map(str, ver))}; 4.2 or newer is needed. " + config_mod.blender_help())
        else:
            add(OK, "Blender", f"{'.'.join(map(str, ver))} at {b}")

    # Python packages
    for mod in ("numpy", "PIL"):
        try:
            m = __import__(mod)
            add(OK, mod if mod != "PIL" else "Pillow", getattr(m, "__version__", ""))
        except Exception as e:
            add(FAIL, mod, f"not importable: {e}. Run the setup script again.")
    try:
        import torch
        dev = "CPU only (works, scans and searches are slower)"
        if torch.cuda.is_available():
            dev = f"NVIDIA GPU: {torch.cuda.get_device_name(0)} (CUDA {torch.version.cuda})"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            dev = "Apple GPU (MPS)"
        add(OK, "torch", f"{torch.__version__}; {dev}; device setting: {cfg.device}")
        if cfg.device == "cuda" and not torch.cuda.is_available():
            add(FAIL, "Device", 'device = "cuda" but no CUDA GPU is usable; set device = "auto"')
    except Exception as e:
        add(FAIL, "torch", f"not importable: {e}. Run the setup script again.")
    try:
        import open_clip
        add(OK, "open_clip", open_clip.__version__)
    except Exception as e:
        add(FAIL, "open_clip", f"not importable: {e}. Run the setup script again.")

    # SigLIP weights
    cfg.apply_env()
    from . import embed
    if fetch_model and not embed.weights_cached():
        print("downloading the SigLIP model (~800 MB, once)...", flush=True)
        try:
            embed.load(cfg)
        except Exception as e:
            add(FAIL, "SigLIP weights", f"download failed: {e}")
    try:
        import open_clip
        open_clip.get_tokenizer(embed.MODEL)(["a wooden barrel"])
        add(OK, "SigLIP tokenizer", "loads")
    except Exception as e:
        add(FAIL, "SigLIP tokenizer", f"does not load: {type(e).__name__}: {e}. Run the setup script again.")
    if fetch_model and embed.weights_cached() and not embed.loaded():
        try:  # a full load proves the weights and the device work
            embed.load(cfg)
            embed.embed_text(["a wooden barrel"])
            add(OK, "Search model", f"loads and runs on {embed.DEVICE}")
        except Exception as e:
            add(FAIL, "Search model", f"does not load: {type(e).__name__}: {e}")
    if embed.weights_cached():
        add(OK, "SigLIP weights", f"{embed.HF_REPO} downloaded")
    elif not any(s == FAIL and w == "SigLIP weights" for s, w, _ in out):
        add(WARN, "SigLIP weights", "not downloaded yet (~800 MB); fetched on first use, or now with "
                                    "'asset-archaeologist doctor --fetch-model'")

    # port
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", cfg.port))
        add(OK, "Port", f"{cfg.port} is free")
    except OSError:
        add(WARN, "Port", f"{cfg.port} is in use (fine if Asset Archaeologist is already running: "
                          f"http://127.0.0.1:{cfg.port}/)")
    finally:
        s.close()
    return out


def run(cfg: Config, fetch_model: bool = False) -> int:
    rows = checks(cfg, fetch_model)
    w = max(len(r[1]) for r in rows)
    for status, what, detail in rows:
        print(f"[{status:>4}] {what:<{w}}  {detail}")
    bad = [r for r in rows if r[0] == FAIL]
    print()
    if bad:
        print(f"{len(bad)} problem(s) to fix (marked FAIL above).")
        return 1
    print("All set. Start the page with 'asset-archaeologist serve --open' (or the start-* launcher).")
    return 0
