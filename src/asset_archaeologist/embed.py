"""SigLIP embeddings, tags and ranking for the renders from render.py.

Model: ViT-B-16-SigLIP-384 (open_clip, pretrained "webli", Apache-2.0; ~800 MB, downloaded once
from Hugging Face). Each model gets one embedding per view (iso, front, back, left, top) and a
search embedding: the mean of iso, front and left (top and back make many models look alike).
Its file name is embedded too ("a 3D model of pizza cutter"), and a search ranks by picture and
by name together (reciprocal rank fusion): picture-only search misses things like "pizza" on
simple low-poly renders.

Stored per pack in <output>/packs/<pack>/_embeddings.npz; tags go into the pack's _index.json.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Config, PACKAGE_DIR, config_path
from .render import view_dir

MODEL, PRETRAINED = "ViT-B-16-SigLIP-384", "webli"
HF_REPO = "timm/ViT-B-16-SigLIP-384"
VIEWS = ("iso", "front", "back", "left", "top")
SEARCH_VIEWS = (0, 1, 3)  # iso, front, left
PROMPT = "a 3D model of {}"
RRF = 20  # rank fusion constant: score = 1/(RRF + image rank) + 1/(RRF + name rank)
# common asset-store name codes -> words
CODES = {"bld": "building", "wep": "weapon", "env": "environment", "chr": "character", "veh": "vehicle",
         "prop": "prop", "fx": "effect", "sm": "", "sk": "", "fbx": "", "glb": ""}
TOP = {"object": 3, "material": 2, "setting": 1}
DIM = 768

LOCK = threading.RLock()  # one model shared by request threads and a scan thread
_model = None
DEVICE = None


def pick_device(want: str = "auto") -> str:
    import torch
    if want == "cuda" or (want == "auto" and torch.cuda.is_available()):
        return "cuda"
    if want == "mps" or (want == "auto" and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()):
        return "mps"
    return "cpu"


def load(cfg: Config | None = None):
    global _model, DEVICE
    with LOCK:
        if _model is None:
            if cfg:
                cfg.apply_env()
            import open_clip
            DEVICE = pick_device(cfg.device if cfg else "auto")
            model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
            model = model.to(DEVICE).eval()
            if DEVICE == "cuda":
                model = model.half()
            _model = (model, preprocess, open_clip.get_tokenizer(MODEL))
        return _model


def loaded() -> bool:
    return _model is not None


def norm(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True).clip(1e-8)


def embed_images(paths, batch=32):
    import torch
    model, preprocess, _ = load()
    out = []
    for i in range(0, len(paths), batch):
        ims = torch.stack([preprocess(Image.open(p).convert("RGB")) for p in paths[i:i + batch]]).to(DEVICE)
        with LOCK, torch.no_grad():
            f = model.encode_image(ims.half() if DEVICE == "cuda" else ims)
        out.append(f.float().cpu().numpy())
    return norm(np.concatenate(out))


def embed_text(texts):
    import torch
    model, _, tok = load()
    with LOCK, torch.no_grad():
        f = model.encode_text(tok(texts).to(DEVICE))
        scale, bias = float(model.logit_scale.exp()), float(model.logit_bias)
    return norm(f.float().cpu().numpy()), scale, bias


def name_words(model: str) -> str:
    """'FBX/Weapons/SM_Wep_Sword_02.fbx' -> 'weapons weapon sword'; 'Models/GLB format/pizza-cutter.glb' -> 'pizza cutter'."""
    model, _, variant = model.partition("#")
    if variant:  # a character outfit: its own mesh name says what it is
        model = model.rsplit("/", 1)[0] + "/" + variant
    parts = model.replace("\\", "/").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    folders = [f for f in parts[:-1] if not re.fullmatch(r"(models?|(fbx|glb|gltf|obj|dae)( format)?)", f, re.I)][-1:]
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", " ".join(folders + [stem]))
    words = [CODES.get(w.lower(), w.lower()) for w in re.split(r"[^A-Za-z]+", words)]
    return " ".join(w for w in words if w)


def fuse(image_scores, name_scores):
    """Reciprocal rank fusion of the picture match and the file-name match."""
    rank = lambda s: np.argsort(np.argsort(-s))
    return 1 / (RRF + rank(image_scores)) + 1 / (RRF + rank(name_scores))


def tag_set_path() -> Path:
    own = config_path().parent / "tag_set.json"
    return own if own.exists() else PACKAGE_DIR / "tag_set.json"


def embed_pack(cfg: Config, index_file: Path):
    """Embed a rendered pack (needs its work folder with the per-view tiles) and tag it."""
    index = json.loads(index_file.read_text("utf-8"))
    key = index["pack"]
    work = Path(index["work"])
    paths, ids = [], []
    for e in index["models"]:
        if "thumb" not in e:
            continue
        d = view_dir(work, e["model"])
        tiles = [d / f"{v}_tile.png" for v in VIEWS]
        if all(t.exists() for t in tiles):
            paths += tiles
            ids.append(e["model"])
    if not ids:
        return 0
    load(cfg)
    print(f"{key}: embedding {len(ids)} models x {len(VIEWS)} views on {DEVICE}", flush=True)
    views = embed_images([str(p) for p in paths]).reshape(len(ids), len(VIEWS), -1)
    search = norm(views[:, SEARCH_VIEWS].mean(axis=1))
    out = index_file.parent / "_embeddings.npz"
    names, _, _ = embed_text([PROMPT.format(name_words(m)) for m in ids])
    if out.exists():
        # a partial render embeds only its own models: keep the rest of the pack,
        # dropping old rows for files that were redone (a character file may have become variants)
        old = np.load(out)
        redone = {m.split("#")[0] for m in ids}
        keep = [i for i, m in enumerate(old["ids"]) if str(m).split("#")[0] not in redone]
        if keep:
            old_ids = old["ids"]  # each old[...] access decompresses the whole array
            ids = [str(old_ids[i]) for i in keep] + ids
            views = np.concatenate([old["views"][keep].astype(np.float32), views])
            search = np.concatenate([old["search"][keep].astype(np.float32), search])
            names = np.concatenate([old["names"][keep].astype(np.float32), names])
    np.savez_compressed(out, ids=np.array(ids), views=views.astype(np.float16), search=search.astype(np.float16),
                        names=names.astype(np.float16))
    tag_pack(index_file, index, ids, search)
    return len(ids)


def tag_pack(index_file: Path, index, ids, search):
    tags = json.loads(tag_set_path().read_text("utf-8"))
    pos = {m: i for i, m in enumerate(ids)}
    scored = {}
    for group, labels in tags.items():
        if group == "note" or not isinstance(labels, list):
            continue
        t, scale, bias = embed_text([PROMPT.format(lb) for lb in labels])
        logits = search @ t.T * scale + bias
        scored[group] = (labels, 1 / (1 + np.exp(-logits)), logits)
    for e in index["models"]:
        if e["model"] not in pos:
            continue
        i, out = pos[e["model"]], {}
        for group, (labels, probs, logits) in scored.items():
            best = np.argsort(-logits[i])[:TOP.get(group, 2)]
            out[group] = [{"label": labels[j], "p": round(float(probs[i, j]), 3)} for j in best]
        e["tags"] = out
    index_file.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", "utf-8")


def retag_all(cfg: Config):
    load(cfg)
    n = 0
    for npz in sorted(cfg.packs_dir.rglob("_embeddings.npz")):
        ix = npz.parent / "_index.json"
        if ix.exists():
            d = np.load(npz)
            tag_pack(ix, json.loads(ix.read_text("utf-8")), [str(x) for x in d["ids"]], d["search"].astype(np.float32))
            print(f"tagged {json.loads(ix.read_text('utf-8'))['pack']}", flush=True)
            n += 1
    return n


def weights_cached() -> bool:
    """True when the SigLIP weights are already in the Hugging Face cache."""
    try:
        from huggingface_hub import try_to_load_from_cache
        for name in ("open_clip_model.safetensors", "open_clip_pytorch_model.bin"):
            p = try_to_load_from_cache(HF_REPO, name)
            if isinstance(p, str):
                return True
    except Exception:
        pass
    return False
