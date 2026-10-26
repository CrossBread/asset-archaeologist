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


def test_find_atlas_names():
    def atlas(*names):
        return render.find_atlas({n.lower(): n for n in names})
    assert atlas("PolygonTown_Texture_01_A.png", "Texture_01_A_Normal.png") == "PolygonTown_Texture_01_A.png"
    assert atlas("MainTexture.png", "MainTexture_Emission.png") == "MainTexture.png"
    assert atlas("T_Main_A.png", "T_Main_Normal.png", "T_Main_Mask.png") == "T_Main_A.png"
    assert atlas("colormap.png") == "colormap.png"
    assert atlas("Palette_LUT.png", "Icon_Atlas.png", "Preview_atlas.png") is None
    assert atlas("wood.png", "stone.png") is None


def test_texture_siblings(tmp_path):
    pack = tmp_path / "kb3d_x.blender.native.zip"
    for n in ["kb3d_x.blender.native.zip", "kb3d_x.png.2k.zip", "kb3d_x.fbx.zip", "kb3d_xtra.png.2k.zip",
              "Nature.zip", "Nature_Textures.zip"]:
        _zip(tmp_path / n, ["a.png"])
    assert [p.name for p in render.texture_siblings(pack)] == ["kb3d_x.png.2k.zip"]
    assert [p.name for p in render.texture_siblings(tmp_path / "Nature.zip")] == ["Nature_Textures.zip"]
    assert render.texture_siblings(tmp_path) == []  # a folder pack has no siblings


def test_remove_output_keeps_other_packs(tmp_path):
    import json
    cfg = config.Config(libraries=[str(tmp_path / "lib")], output=str(tmp_path / "out"), work=str(tmp_path / "w"))
    d = cfg.packs_dir / "lib/Pack"
    (d / "sub").mkdir(parents=True)
    (d / "sub/a.png").write_text("x")
    (d / "sub/a.sheet.png").write_text("x")
    (d / "_contact_01.png").write_text("x")
    (d / "_embeddings.npz").write_text("x")
    (d / "_index.json").write_text(json.dumps({"pack": "lib/Pack", "contact_sheets": ["lib/Pack/_contact_01.png"],
        "models": [{"model": "a.fbx", "thumb": "lib/Pack/sub/a.png", "sheet": "lib/Pack/sub/a.sheet.png"}]}))
    (d / "Inner").mkdir()
    (d / "Inner/_index.json").write_text("{}")  # another pack's output below this one
    render.remove_output(cfg, "lib/Pack")
    assert sorted(p.relative_to(d).as_posix() for p in d.rglob("*")) == ["Inner", "Inner/_index.json"]


def test_read_redo(tmp_path):
    lib = tmp_path / "lib"
    _zip(lib / "Kenney/kit.zip", ["a.glb"])
    cfg = config.Config(libraries=[str(lib)], output=str(tmp_path / "out"), work=str(tmp_path / "w"))
    f = tmp_path / "redo.txt"
    f.write_text(f"# packs to redo\nKenney\\other\n\n{lib / 'Kenney/kit.zip'}\n", "utf-8")
    assert scan.read_redo(cfg, f) == {"Kenney/other", "lib/Kenney/kit"}


def test_library_finds_packs_without_walking_pictures(tmp_path):
    from asset_archaeologist import server
    cfg = config.Config(libraries=[str(tmp_path / "lib")], output=str(tmp_path / "out"), work=str(tmp_path / "w"))
    d = cfg.packs_dir / "lib/Pack"
    d.mkdir(parents=True)
    ids = np.array(["a.glb", "b.glb"])
    vecs = np.zeros((2, server.embed.DIM), np.float16)
    np.savez_compressed(d / "_embeddings.npz", ids=ids, search=vecs, names=vecs, views=vecs)
    (d / "_index.json").write_text('{"pack": "lib/Pack", "models": [{"model": "a.glb", "name": "a", '
                                   '"thumb": "lib/Pack/a.png", "sheet": "lib/Pack/a.sheet.png"}]}', "utf-8")
    (cfg.packs_dir / "lib/Unembedded").mkdir()
    (cfg.packs_dir / "lib/Unembedded/_index.json").write_text('{"pack": "lib/Unembedded", "models": []}', "utf-8")
    lib = server.Library(cfg)
    assert lib.ids == ["a.glb"] and list(lib.packs) == ["lib/Pack"]
    assert len(lib._packs()) == 2
