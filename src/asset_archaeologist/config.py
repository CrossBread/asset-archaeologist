"""Settings: where the asset library is, where renders go, which Blender to use.

Standard library only (the agent CLI imports this without torch).

The settings file is TOML. It is looked for, in order:
  1. the path in the ASSET_ARCHAEOLOGIST_CONFIG environment variable
  2. config.toml in the app folder (a "portable" install; only if that file exists)
  3. the user's app-data folder:
       Windows  %APPDATA%\\AssetArchaeologist\\config.toml
       macOS    ~/Library/Application Support/AssetArchaeologist/config.toml
       Linux    $XDG_CONFIG_HOME/asset-archaeologist/config.toml (~/.config/...)
It is created with defaults the first time it is needed.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_NAME = "AssetArchaeologist"
PACKAGE_DIR = Path(__file__).resolve().parent
APP_DIR = PACKAGE_DIR.parent.parent  # the checkout, for an editable install
DEFAULT_PORT = 8790
MIN_BLENDER = (4, 2)


def _home() -> Path:
    return Path.home()


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or _home() / "AppData/Roaming") / APP_NAME
    if sys.platform == "darwin":
        return _home() / "Library/Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config") / "asset-archaeologist"


def data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData/Local") / APP_NAME
    if sys.platform == "darwin":
        return _home() / "Library/Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local/share") / "asset-archaeologist"


def config_path() -> Path:
    env = os.environ.get("ASSET_ARCHAEOLOGIST_CONFIG")
    if env:
        return Path(env).expanduser()
    portable = APP_DIR / "config.toml"
    if (APP_DIR / "pyproject.toml").exists() and portable.exists():
        return portable
    return config_dir() / "config.toml"


@dataclass
class Config:
    # folders holding your asset packs (zips, .unitypackage, .blend, folders of models)
    libraries: list[str] = field(default_factory=list)
    # where thumbnails, sheets, indexes and embeddings are written (about 230 KB per model)
    output: str = ""
    # scratch space for unpacking packs while they render; emptied after each pack
    work: str = ""
    # Blender executable; empty = find it automatically
    blender: str = ""
    # port of the local browser page and search API
    port: int = DEFAULT_PORT
    # Blender processes at once while scanning
    procs: int = 2
    # "auto" (NVIDIA GPU, Apple GPU, else CPU), "cuda", "mps" or "cpu"
    device: str = "auto"
    # optional: regular expressions; packs whose path matches are scanned first
    priority: list[str] = field(default_factory=list)
    # loose model files (not in a zip) are grouped into packs by their folder this many
    # levels below the library folder (1: Library/<pack>/..., 2: Library/<vendor>/<pack>/...)
    folder_pack_depth: int = 1
    # optional: folder for the downloaded SigLIP weights (sets HF_HOME); empty = default cache
    model_cache: str = ""

    path: Path | None = field(default=None, repr=False, compare=False)

    # ---- resolved paths ----
    @property
    def output_dir(self) -> Path:
        return Path(self.output).expanduser() if self.output else data_dir() / "library"

    @property
    def packs_dir(self) -> Path:
        return self.output_dir / "packs"

    @property
    def work_dir(self) -> Path:
        return Path(self.work).expanduser() if self.work else Path(tempfile.gettempdir()) / "asset-archaeologist"

    @property
    def library_dirs(self) -> list[Path]:
        return [Path(p).expanduser() for p in self.libraries if p]

    def blender_path(self) -> Path | None:
        return find_blender(self.blender)

    def apply_env(self):
        """Environment the model download honours; call before importing open_clip."""
        if self.model_cache:
            os.environ.setdefault("HF_HOME", str(Path(self.model_cache).expanduser()))

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "path"}


def load(path: Path | None = None, create: bool = True) -> Config:
    path = Path(path) if path else config_path()
    cfg = Config()
    if path.exists():
        data = tomllib.loads(path.read_text("utf-8"))
        known = {f.name: f for f in fields(Config)}
        for k, v in data.items():
            if k in known and k != "path":
                setattr(cfg, k, v)
    elif create:
        save(cfg, path)
    cfg.path = path
    return cfg


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


COMMENTS = {
    "libraries": "Folders holding your asset packs (zips, .unitypackage, .blend, folders of models).",
    "output": "Where thumbnails, sheets, indexes and search data go (about 230 KB per model). Empty = app-data folder.",
    "work": "Scratch space for unpacking a pack while it renders; emptied after each pack. Empty = system temp.",
    "blender": "Blender executable (4.2 or newer). Empty = find it automatically.",
    "port": "Port of the local browser page and search API (http://127.0.0.1:<port>/).",
    "procs": "Blender processes at once while scanning (each uses one CPU core and some GPU memory).",
    "device": 'Where the image-text model runs: "auto", "cuda" (NVIDIA), "mps" (Apple) or "cpu".',
    "priority": "Optional regular expressions: packs whose path matches are scanned first.",
    "folder_pack_depth": "Loose model files are grouped into packs by their folder this many levels below a library folder.",
    "model_cache": "Optional folder for the downloaded SigLIP weights (~800 MB). Empty = the usual Hugging Face cache.",
}


def save(cfg: Config, path: Path | None = None) -> Path:
    path = Path(path or cfg.path or config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Asset Archaeologist settings. Edit here or on the Settings screen of the browser page.", ""]
    for k, v in cfg.to_dict().items():
        lines.append(f"# {COMMENTS.get(k, '')}")
        lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    path.write_text("\n".join(lines), "utf-8")
    cfg.path = path
    return path


def update(cfg: Config, values: dict) -> Config:
    """Apply settings from the page or the command line, with type checks."""
    known = {f.name: f for f in fields(Config) if f.name != "path"}
    for k, v in values.items():
        if k not in known:
            raise ValueError(f"unknown setting {k}")
        cur = getattr(cfg, k)
        if isinstance(cur, list):
            if isinstance(v, str):
                v = [x.strip() for x in re.split(r"[;\n]", v) if x.strip()]
            v = [str(x) for x in v]
        elif isinstance(cur, int):
            v = int(v)
        else:
            v = str(v).strip()
        setattr(cfg, k, v)
    if not 1 <= cfg.procs <= 16:
        raise ValueError("procs must be between 1 and 16")
    if cfg.device not in ("auto", "cuda", "mps", "cpu"):
        raise ValueError("device must be auto, cuda, mps or cpu")
    return cfg


# ---------- Blender ----------

def _version_key(p: str):
    m = re.search(r"(\d+)\.(\d+)", p)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def blender_candidates() -> list[str]:
    c = []
    if os.environ.get("BLENDER"):
        c.append(os.environ["BLENDER"])
    w = shutil.which("blender")
    if w:
        c.append(w)
    if sys.platform == "win32":
        for base in [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", ""),
                     os.environ.get("LOCALAPPDATA", "")]:
            if base:
                c += sorted(glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe")),
                            key=_version_key, reverse=True)
        c.append(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                              "Steam", "steamapps", "common", "Blender", "blender.exe"))
        c += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "blender*.exe"))
    elif sys.platform == "darwin":
        for base in ["/Applications", str(_home() / "Applications")]:
            c.append(f"{base}/Blender.app/Contents/MacOS/Blender")
            c += sorted(glob.glob(f"{base}/Blender*.app/Contents/MacOS/Blender"), key=_version_key, reverse=True)
    else:
        c += ["/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender"]
        c += sorted(glob.glob("/opt/blender*/blender") + glob.glob(str(_home() / "blender*/blender")),
                    key=_version_key, reverse=True)
    out = []
    for p in c:
        if p and p not in out and os.path.isfile(p):
            out.append(p)
    return out


def find_blender(configured: str = "") -> Path | None:
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_file() else None
    found = blender_candidates()
    # prefer the newest one that is new enough
    ok = [p for p in found if (blender_version(p) or (0, 0)) >= MIN_BLENDER]
    return Path(ok[0] if ok else found[0]) if found else None


_versions: dict[str, tuple | None] = {}


def blender_version(path) -> tuple | None:
    path = str(path)
    if path not in _versions:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            m = re.search(r"Blender (\d+)\.(\d+)(?:\.(\d+))?", out)
            _versions[path] = tuple(int(x or 0) for x in m.groups()) if m else None
        except Exception:
            _versions[path] = None
    return _versions[path]


BLENDER_HELP = {
    "win32": "Download it from https://www.blender.org/download/ (or run: winget install BlenderFoundation.Blender)",
    "darwin": "Download it from https://www.blender.org/download/ and drag it into Applications "
              "(or run: brew install --cask blender)",
    "linux": "Download it from https://www.blender.org/download/ (distribution packages are often too old; "
             "'sudo snap install blender --classic' gives a current version)",
}


def blender_help() -> str:
    return BLENDER_HELP.get(sys.platform, BLENDER_HELP["linux"])


def cli_command() -> str:
    """How an agent runs the command line tool on this machine (absolute path)."""
    exe_dir = Path(sys.executable).parent
    for name in ("asset-archaeologist.exe", "asset-archaeologist"):
        if (exe_dir / name).exists():
            return str(exe_dir / name)
    w = shutil.which("asset-archaeologist")
    return w or f'"{sys.executable}" -m asset_archaeologist'
