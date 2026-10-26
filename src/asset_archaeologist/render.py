"""Thumbnails and five-view sheets for every 3D model in a pack.

A pack is a .zip (Unity packages inside it are opened too), a .unitypackage, a single
model or .blend file, or a folder of loose model files.

Per pack, the model files and images are unpacked to the work folder, one file per model is
chosen (GLB over glTF over FBX over OBJ over DAE over .blend; the same model in several
formats is rendered once; collision meshes are skipped), Blender renders front, back, left,
top and a 3/4 view of each (blender_render.py, run inside Blender) and this module crops and
assembles them under <output>/packs/<pack key>/:

    <model>.png          256 px 3/4 view
    <model>.sheet.png    1024 x 512: 3/4 + front / back / left / top
    _contact_NN.png      every model in the pack with its name
    _index.json          per model: file in the pack, images, size in metres, triangles,
                         texture status (textured | unity | atlas | colour | missing |
                         untextured), image names it could not find (and tags once embedded)
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import re
import shutil
import subprocess
import tarfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from PIL import Image, ImageDraw, ImageFont

from .config import Config, PACKAGE_DIR

BLENDER_SCRIPT = PACKAGE_DIR / "blender_render.py"
FORMATS = {".glb": 0, ".gltf": 1, ".fbx": 2, ".obj": 3, ".dae": 4, ".blend": 5}  # lower wins
IMAGES = {".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".bmp", ".psd", ".exr"}
# what rendering needs: models, images, gltf/obj sidecars, Unity materials
KEEP = set(FORMATS) | IMAGES | {".bin", ".mtl", ".mat"}
SKIP = re.compile(r"(^|/)(__macosx|collision|collisions)(/|$)|(^|/)\._", re.I)
LICENCE = re.compile(r"(^|/)[^/]*(licen[cs]e|readme|terms|eula|copying)[^/]*\.(txt|md|pdf|html?|rtf)$", re.I)
CHUNK = 40          # models per Blender process (one start-up per chunk)
BG = (236, 236, 236)
PAD = 0.06          # margin around the cropped model, share of the tile
CONTACT_COLS, CONTACT_ROWS, CONTACT_PX = 6, 8, 200
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RenderError(RuntimeError):
    pass


def pack_key(cfg: Config, source: Path) -> str:
    """'<library folder name>/<path below it>' without the file extension."""
    p = source.resolve()
    for root in cfg.library_dirs:
        r = root.resolve()
        if r == p or r in p.parents:
            name = r.name or re.sub(r"[^A-Za-z0-9]", "", r.drive) or "library"
            rel = p.relative_to(r)
            if p.is_file():
                rel = rel.with_suffix("")
            return f"{name}/{rel.as_posix()}" if rel.parts else name
    return f"{p.parent.name or 'other'}/{p.stem if p.is_file() else p.name}"


def index_path(cfg: Config, key: str) -> Path:
    return cfg.packs_dir / key / "_index.json"


def choose_models(names):
    """One member per model: same stem in several formats -> the preferred format."""
    best = {}
    for n in names:
        p = PurePosixPath(n)
        ext = p.suffix.lower()
        if ext not in FORMATS or SKIP.search(n):
            continue
        key = p.stem.lower()
        if key not in best or FORMATS[ext] < FORMATS[PurePosixPath(best[key]).suffix.lower()]:
            best[key] = n
    return sorted(best.values(), key=str.lower)


# a pack's shared colour texture, most specific name first
ATLAS_NAMES = [r"texture_0?1_?a?\.(png|tga)$",  # Synty
               r"main_?tex(ture)?[^/]*\.(png|tga|jpg)$", r"atlas[^/]*\.(png|tga|jpg)$",
               r"palette[^/]*\.(png|tga|jpg)$", r"colou?r_?map[^/]*\.(png|tga|jpg)$",
               r"(^|[_-])main(_[a-z0-9])?\.(png|tga|jpg)$"]  # Animpic characters: T_Main_A..F
NOT_ATLAS = re.compile(r"(normal|_nrm|emiss|mask|rough|metal|spec|_ao|height|lut|alpha|icon|preview|thumb|logo"
                       r"|screenshot)", re.I)
# texture-only downloads next to a pack (KitBash: kb3d_x.png.2k.zip next to kb3d_x.blender.native.zip)
TEXTURE_ZIP = re.compile(r"(\.png\.|textures?|\.tga\.|\.jpg\.)", re.I)


def find_atlas(textures):
    """A pack's shared colour texture (Synty 'Texture_01_A', Animpic 'MainTexture', 'atlas',
    'palette', 'colormap'), used for materials that have none or point at a file the pack lacks.
    textures: {lower-case file name: path}."""
    for pat in ATLAS_NAMES:
        cands = [p for b, p in textures.items() if re.search(pat, b) and not NOT_ATLAS.search(b)]
        if cands:
            return sorted(cands, key=len)[0]
    return None


def texture_siblings(source: Path) -> list[Path]:
    """Texture-only zips next to a pack file: the same name up to the first dot and 'png',
    'tga', 'jpg' or 'textures' in the name."""
    if not source.is_file():
        return []
    stem = source.name.split(".")[0]
    out = []
    for z in source.parent.glob(f"{glob.escape(stem)}*.zip"):
        rest = z.name[len(stem):]  # '.png.2k.zip', '_Textures.zip'; not 'Kit.zip' (another pack)
        if (z != source and z.name.lower().startswith(stem.lower()) and re.match(r"[._ -]", rest)
                and TEXTURE_ZIP.search(rest)):
            out.append(z)
    return sorted(out)


def model_out_rel(member: str) -> str:
    """Model path inside the pack, minus format folders like 'FBX format' or 'GLB'."""
    parts = [x for x in PurePosixPath(member).with_suffix("").parts
             if not re.fullmatch(r"(models?|(fbx|glb|gltf|obj|dae)( format)?)", x, re.I)]
    return "/".join(parts) or PurePosixPath(member).stem


def work_name(key: str) -> str:
    """Short work folder name (Windows paths stop at 260 characters)."""
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", key.rsplit("/", 1)[-1])[:32]
    return f"{tail}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def view_dir(work: Path, model: str) -> Path:
    """Where the Blender step left a model's view PNGs ('file#variant' -> its own subfolder)."""
    member, _, variant = model.partition("#")
    d = work / "views" / model_out_rel(member)
    return d / variant if variant else d


def _safe_member(n: str) -> bool:
    p = PurePosixPath(n)
    return not p.is_absolute() and ".." not in p.parts and not re.match(r"^[A-Za-z]:", n)


def unpack(source: Path, src_dir: Path):
    """Copy what rendering needs into src_dir. Handles zips (also .unitypackage files inside
    them), .unitypackage, single model or .blend files and folders.
    Returns (member paths, Unity guid map, licence/readme files)."""
    src_dir.mkdir(parents=True, exist_ok=True)
    ext = source.suffix.lower()
    members, guids, licences = [], {}, []
    if source.is_dir():  # a folder of loose model files
        for f in source.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(source).as_posix()
            if LICENCE.search(rel):
                licences.append(str(f))
            # .blend files and zips in the folder are packs of their own
            if f.suffix.lower() in KEEP - {".blend"} and not SKIP.search(rel):
                dest = src_dir / rel
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                members.append(rel)
    elif ext == ".zip":
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                n = info.filename
                if n.endswith("/") or SKIP.search(n) or not _safe_member(n):
                    continue
                if LICENCE.search(n):
                    licences.append(n)
                e = PurePosixPath(n).suffix.lower()
                if e == ".unitypackage":
                    prefix = PurePosixPath(n).stem
                    with z.open(info) as fh:
                        m, g = unpack_unity(fh, src_dir, prefix)
                    members += m
                    guids.update(g)
                elif e in KEEP:
                    if not (src_dir / n).exists():
                        z.extract(info, src_dir)
                    members.append(n)
    elif ext == ".unitypackage":
        with source.open("rb") as fh:
            members, guids = unpack_unity(fh, src_dir, "")
    elif ext in FORMATS:
        dest = src_dir / source.name
        if not dest.exists():
            shutil.copy2(source, dest)
        members = [source.name]
    else:
        raise RenderError(f"cannot read {source.name}")
    return members, guids, licences


def unpack_unity(fh, src_dir: Path, prefix: str):
    """A .unitypackage is a gzipped tar: <guid>/asset holds the file, <guid>/pathname its
    project path. Streams it once and keeps models, images and materials (no Unity needed)."""
    raw = src_dir / "_unity_raw"
    raw.mkdir(parents=True, exist_ok=True)
    paths, metas = {}, {}
    with tarfile.open(fileobj=fh, mode="r|gz") as t:
        for m in t:
            if not m.isfile():
                continue
            guid, _, part = m.name.lstrip("./").partition("/")
            if not re.fullmatch(r"[0-9a-fA-F]{32}", guid):
                continue
            if part == "pathname":
                paths[guid] = t.extractfile(m).read().decode("utf-8", "replace").splitlines()[0].strip()
            elif part == "asset":
                with (raw / guid).open("wb") as out:
                    shutil.copyfileobj(t.extractfile(m), out)
            elif part == "asset.meta" and m.size < 2_000_000:
                metas[guid] = t.extractfile(m).read().decode("utf-8", "replace")
    members, guids = [], {}
    for guid, p in paths.items():
        rel = f"{prefix}/{p}" if prefix else p
        guids[guid] = rel
        if (PurePosixPath(p).suffix.lower() in KEEP and (raw / guid).exists() and not SKIP.search(rel)
                and _safe_member(rel)):
            dest = src_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(raw / guid), dest)
            members.append(rel)
            # a model's import settings remap its own material names to project .mat files
            if guid in metas and "externalObjects" in metas[guid]:
                (dest.parent / (dest.name + ".meta")).write_text(metas[guid], "utf-8")
    shutil.rmtree(raw, ignore_errors=True)
    return members, guids


UNITY_COLOUR = re.compile(
    r"-\s*(_BaseColorMap|_BaseMap|_MainTex|_Albedo\w*|_Diffuse\w*|_BaseColor\w*)\s*:\s*\n\s*"
    r"m_Texture:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-f]{32})")
UNITY_REMAP = re.compile(r"type:\s*UnityEngine:Material\s*\n(?:\s+\w+:.*\n)*?\s+name:\s*(.+?)\s*\n\s+second:\s*\{[^}]*guid:\s*([0-9a-f]{32})")


def unity_materials(src_dir: Path, members, guids):
    """Unity .mat files name their colour texture by guid; map material name -> image path.
    Model .meta files add their own remaps (e.g. an FBX says MI_Barrel_01 while the colour map
    is set in M_Barrel_01.mat)."""
    by_name, by_guid = {}, {}
    rel_to_guid = {v: k for k, v in guids.items()}
    for m in members:
        if not m.lower().endswith(".mat"):
            continue
        text = (src_dir / m).read_text("utf-8", errors="replace")
        for _, guid in UNITY_COLOUR.findall(text):
            tex = guids.get(guid)
            if tex and PurePosixPath(tex).suffix.lower() in IMAGES and (src_dir / tex).exists():
                by_name.setdefault(PurePosixPath(m).stem.lower(), str(src_dir / tex))
                if m in rel_to_guid:
                    by_guid[rel_to_guid[m]] = str(src_dir / tex)
                break
    remap = {}
    for meta in src_dir.rglob("*.meta"):
        for name, guid in UNITY_REMAP.findall(meta.read_text("utf-8", errors="replace")):
            if guid in by_guid:
                remap.setdefault(name.strip().lower(), by_guid[guid])
    for name, tex in list(by_name.items()):  # MI_ instance names often match an M_ material
        if name.startswith("m_"):
            remap.setdefault("mi_" + name[2:], tex)
    return {**by_name, **remap}


def _count_results(path: Path) -> int:
    try:
        return len(json.loads(path.read_text("utf-8")))
    except Exception:
        return 0


def run_blender(blender: Path, items, textures, atlas, work: Path, n: int, extra=None, tick=None):
    """One Blender process for a chunk of models. tick(n_done) is called as models finish."""
    jobs, results = work / f"jobs_{n:03d}.json", work / f"results_{n:03d}.json"
    jobs.write_text(json.dumps({"textures": textures, "atlas": atlas, "items": items, **(extra or {})}), "utf-8")
    with (work / f"blender_{n:03d}.log").open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen([str(blender), "-b", "--factory-startup", "-P", str(BLENDER_SCRIPT),
                                 "--", str(jobs), str(results)], stdout=log, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        while True:
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if tick:
                    tick(_count_results(results))
    done = json.loads(results.read_text("utf-8")) if results.exists() else []
    seen = {r["src"] for r in done}
    # a hard crash ends the process; whatever it had not reached is reported, not lost silently
    return done + [{"src": i["src"], "error": "Blender stopped before this model"} for i in items if i["src"] not in seen]


def fit(img: Image.Image, px: int) -> Image.Image:
    """Crop to the model, centre it on the background at px x px."""
    tile = Image.new("RGB", (px, px), BG)
    box = img.getchannel("A").getbbox()
    if not box:
        return tile
    crop = img.crop(box)
    inner = px * (1 - 2 * PAD)
    scale = inner / max(crop.size)
    crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.LANCZOS)
    tile.paste(crop, ((px - crop.width) // 2, (px - crop.height) // 2), crop)
    return tile


def label(img: Image.Image, text: str, size=13):
    d = ImageDraw.Draw(img)
    d.text((6, img.height - size - 6), text, fill=(110, 110, 110), font=ImageFont.load_default(size))


def compose(vdir: Path, thumb: Path, sheet: Path):
    views = {v: Image.open(vdir / f"{v}.png").convert("RGBA") for v in ("iso", "front", "back", "left", "top")}
    tiles = {v: fit(im, 512 if v == "iso" else 256) for v, im in views.items()}
    for v, t in tiles.items():  # read by the embedding step
        t.save(vdir / f"{v}_tile.png")
    thumb.parent.mkdir(parents=True, exist_ok=True)
    tiles["iso"].resize((256, 256), Image.LANCZOS).save(thumb, optimize=True)
    out = Image.new("RGB", (1024, 512), BG)
    out.paste(tiles["iso"], (0, 0))
    for (v, x, y) in (("front", 512, 0), ("back", 768, 0), ("left", 512, 256), ("top", 768, 256)):
        t = tiles[v].copy()
        label(t, v)
        out.paste(t, (x, y))
    d = ImageDraw.Draw(out)
    d.line([(512, 0), (512, 512)], fill=(210, 210, 210))
    d.line([(768, 0), (768, 512)], fill=(210, 210, 210))
    d.line([(512, 256), (1024, 256)], fill=(210, 210, 210))
    out.save(sheet, optimize=True)


def contact_sheets(cfg: Config, entries, out_dir: Path, title: str):
    for old in out_dir.glob("_contact_*.png"):
        old.unlink()
    ok = [e for e in entries if "thumb" in e]
    per = CONTACT_COLS * CONTACT_ROWS
    font = ImageFont.load_default(12)
    head = 28
    pages = math.ceil(len(ok) / per)
    for page in range(pages):
        chunk = ok[page * per:(page + 1) * per]
        rows = math.ceil(len(chunk) / CONTACT_COLS)
        sheet = Image.new("RGB", (CONTACT_COLS * CONTACT_PX, head + rows * (CONTACT_PX + 18)), (255, 255, 255))
        d = ImageDraw.Draw(sheet)
        d.text((8, 7), f"{title}  ({page + 1}/{pages}, {len(ok)} models)", fill=(40, 40, 40),
               font=ImageFont.load_default(14))
        for i, e in enumerate(chunk):
            x, y = (i % CONTACT_COLS) * CONTACT_PX, head + (i // CONTACT_COLS) * (CONTACT_PX + 18)
            im = Image.open(cfg.packs_dir / e["thumb"]).resize((CONTACT_PX - 8, CONTACT_PX - 8), Image.LANCZOS)
            sheet.paste(im, (x + 4, y + 4))
            name = e["name"] if len(e["name"]) <= 30 else e["name"][:29] + "…"
            d.text((x + 6, y + CONTACT_PX - 2), name, fill=(60, 60, 60), font=font)
        sheet.save(out_dir / f"_contact_{page + 1:02d}.png", optimize=True)
    return pages


def do_pack(cfg: Config, source: Path, procs: int = 0, limit: int = 0, only: str = "", progress=None) -> Path:
    """Render one pack; returns its _index.json. The work folder is left for the embedding
    step; delete it with cleanup(cfg, key) afterwards."""
    blender = cfg.blender_path()
    if not blender:
        raise RenderError("Blender was not found. Install Blender 4.2 or newer, or set its path in the settings.")
    procs = procs or cfg.procs
    key = pack_key(cfg, source)
    work = work_dir(cfg, key)
    src_dir = work / "src"
    names, guids, licences = unpack(source, src_dir)
    for extra_zip in texture_siblings(source):  # images only, where texture lookup finds them
        with zipfile.ZipFile(extra_zip) as z:
            for info in z.infolist():
                n = info.filename
                if (PurePosixPath(n).suffix.lower() in IMAGES and _safe_member(n)
                        and not (src_dir / "_tex" / n).exists()):
                    z.extract(info, src_dir / "_tex")
    models = [m for m in choose_models(names) if not only or re.search(only, m, re.I)][: limit or None]
    textures = {}
    for p in src_dir.rglob("*"):
        if p.suffix.lower() in IMAGES:
            textures.setdefault(p.name.lower(), str(p))
    atlas = find_atlas(textures)
    materials = unity_materials(src_dir, names, guids)
    items = [{"src": str(src_dir / m), "out": str(work / "views" / model_out_rel(m)), "member": m} for m in models]
    chunks = [items[i:i + CHUNK] for i in range(0, len(items), CHUNK)]
    print(f"{key}: {len(models)} model files, {len(textures)} images, atlas={'yes' if atlas else 'no'}, "
          f"{len(materials)} Unity materials, {len(chunks)} Blender runs", flush=True)
    if progress:
        progress({"pack_models": len(models), "pack_rendered": 0})
    extra = {"materials": materials}
    results = []
    per_chunk = [0] * len(chunks)
    lock = threading.Lock()

    def tick(n, count):
        with lock:
            per_chunk[n] = count
            total = sum(per_chunk)
        if progress:
            progress({"pack_rendered": total})

    with ThreadPoolExecutor(max(1, procs)) as pool:
        futs = [pool.submit(run_blender, blender, c, textures, atlas, work, n, extra, lambda k, n=n: tick(n, k))
                for n, c in enumerate(chunks)]
        for f in as_completed(futs):
            rs = f.result()
            results += rs
    if progress:
        progress({"pack_rendered": len(items)})
    by_src = {r["src"]: r for r in results}

    out_dir = cfg.packs_dir / key
    entries = []
    for it in items:
        r = by_src.get(it["src"], {"error": "no result"})
        rel = model_out_rel(it["member"])
        if "error" in r:
            entries.append({"model": it["member"], "name": PurePosixPath(rel).name, "error": r["error"]})
            continue
        # a character file with several outfits on one skeleton comes back as variants: one entry each
        if "variants" in r:
            parts = [(f"{it['member']}#{v['name']}", f"{rel}/{v['name']}", v) for v in r["variants"]]
        else:
            parts = [(it["member"], rel, r)]
        for model, out_rel, v in parts:
            e = {"model": model, "name": PurePosixPath(out_rel).name}
            if r.get("unresolved"):  # image names the file asks for that the pack does not have
                e["unresolved"] = r["unresolved"]
            thumb, sheet = out_dir / f"{out_rel}.png", out_dir / f"{out_rel}.sheet.png"
            try:
                compose(view_dir(work, model), thumb, sheet)
                e.update(thumb=thumb.relative_to(cfg.packs_dir).as_posix(),
                         sheet=sheet.relative_to(cfg.packs_dir).as_posix(),
                         size_m=v["size_m"], tris=v["tris"], texture=r["texture"])
            except Exception as ex:  # missing view files etc.
                e["error"] = f"compose: {ex}"
            entries.append(e)
    out_dir.mkdir(parents=True, exist_ok=True)
    ipath = index_path(cfg, key)
    complete = not (only or limit)
    if (only or limit) and ipath.exists():
        # a partial run replaces its own models and keeps the rest of the pack
        old_index = json.loads(ipath.read_text("utf-8"))
        complete = bool(old_index.get("complete"))
        new = {e["model"]: e for e in entries}
        redone = {m.split("#")[0] for m in new}
        old = [e for e in old_index["models"]
               if e["model"] in new or e["model"].split("#")[0] not in redone]
        entries = [new.pop(e["model"], e) for e in old] + list(new.values())
    pages = contact_sheets(cfg, entries, out_dir, key)
    index = {"pack": key, "pack_path": str(source.resolve()), "work": str(work), "complete": complete,
             "licence_files": licences[:20], "models": entries,
             "contact_sheets": [f"{key}/_contact_{i + 1:02d}.png" for i in range(pages)]}
    ipath.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", "utf-8")
    bad = [e for e in entries if "error" in e]
    tex = {}
    for e in entries:
        if "texture" in e:
            tex[e["texture"]] = tex.get(e["texture"], 0) + 1
    print(f"{key}: {len(entries) - len(bad)} rendered, {len(bad)} failed, textures {tex}, {pages} contact sheets",
          flush=True)
    return ipath


def work_dir(cfg: Config, key: str) -> Path:
    return cfg.work_dir / work_name(key)


def remove_output(cfg: Config, key: str):
    """Delete a pack's pictures, index and embeddings, so a fresh render leaves nothing stale
    (a file that became variants, a model renamed). Only the files its index lists, so another
    pack's output in a folder below this one is kept."""
    out_dir = cfg.packs_dir / key
    ipath = index_path(cfg, key)
    if not ipath.exists():
        return
    try:
        index = json.loads(ipath.read_text("utf-8"))
    except Exception:
        index = {}
    files = [c for c in index.get("contact_sheets", [])]
    files += [e[k] for e in index.get("models", []) for k in ("thumb", "sheet") if e.get(k)]
    for rel in files:
        (cfg.packs_dir / rel).unlink(missing_ok=True)
    for name in ("_embeddings.npz", "_index.json"):
        (out_dir / name).unlink(missing_ok=True)
    for d in sorted((d for d in out_dir.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()


def cleanup(cfg: Config, key: str):
    shutil.rmtree(work_dir(cfg, key), ignore_errors=True)
