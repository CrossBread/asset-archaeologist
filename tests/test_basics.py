"""Fast checks that need neither Blender nor the SigLIP model."""
import zipfile
from pathlib import Path

import numpy as np

from asset_archaeologist import config, render, scan
from asset_archaeologist.embed import fuse, name_words


def test_choose_models_prefers_glb_and_skips_collision():
    names = ["Models/FBX format/apple.fbx", "Models/GLB format/apple.glb", "Models/OBJ format/apple.obj",
             "Collision/apple.fbx", "__MACOSX/._pear.fbx", "pear.fbx", "notes.txt"]
    assert render.choose_models(names) == ["Models/GLB format/apple.glb", "pear.fbx"]


def test_model_out_rel_drops_format_folders():
    assert render.model_out_rel("Models/GLB format/pizza-cutter.glb") == "pizza-cutter"
    assert render.model_out_rel("FBX/Weapons/SM_Wep_Sword_02.fbx") == "Weapons/SM_Wep_Sword_02"


def test_name_words():
    assert name_words("Models/GLB format/pizza-cutter.glb") == "pizza cutter"
    assert name_words("FBX/Weapons/SM_Wep_Sword_02.fbx") == "weapons weapon sword"
    assert name_words("Characters/SK_Chr_Goblin.fbx#Goblin_Warrior") == "characters goblin warrior"


def test_fuse_rewards_agreement():
    img = np.array([0.9, 0.1, 0.5])
    nam = np.array([0.8, 0.2, 0.1])
    assert int(np.argmax(fuse(img, nam))) == 0


def test_work_name_is_short():
    assert len(render.work_name("a/" + "x" * 300)) <= 41


def test_config_round_trip(tmp_path):
    p = tmp_path / "config.toml"
    cfg = config.load(p)
    config.update(cfg, {"libraries": ['C:\\Assets "quoted"', "/home/me/assets"], "port": "8799", "procs": 3})
    config.save(cfg)
    again = config.load(p)
    assert again.libraries == ['C:\\Assets "quoted"', "/home/me/assets"]
    assert again.port == 8799 and again.procs == 3


def _zip(path: Path, names):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, "x")


def test_discover_keeps_one_download_per_product(tmp_path):
    lib = tmp_path / "lib"
    _zip(lib / "Vendor/POLYGON_Town_SourceFiles.zip", ["FBX/SM_House.fbx"])
    _zip(lib / "Vendor/POLYGON_Town_Unity.zip", ["Town.unitypackage"])
    _zip(lib / "Vendor/POLYGON_Town_Unreal.zip", ["Content/SM_House.fbx"])
    _zip(lib / "Other/textures.zip", ["a.png"])  # no models: not a pack
    (lib / "Loose/Crates/fbx").mkdir(parents=True)
    (lib / "Loose/Crates/fbx/crate.fbx").write_text("x")
    cfg = config.Config(libraries=[str(lib)], output=str(tmp_path / "out"), work=str(tmp_path / "work"))
    found = scan.discover(cfg)
    names = sorted(p.name for p in found)
    assert names == ["Loose", "POLYGON_Town_SourceFiles.zip"]
    assert render.pack_key(cfg, lib / "Vendor/POLYGON_Town_SourceFiles.zip") == "lib/Vendor/POLYGON_Town_SourceFiles"


def test_unpack_zip_records_licence(tmp_path):
    z = tmp_path / "pack.zip"
    _zip(z, ["License.txt", "Models/a.glb", "Textures/a.png", "../evil.glb"])
    members, guids, licences = render.unpack(z, tmp_path / "src")
    assert "Models/a.glb" in members and "../evil.glb" not in members
    assert licences == ["License.txt"]
