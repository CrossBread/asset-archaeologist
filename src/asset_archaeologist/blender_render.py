"""Runs inside Blender (started by render.py, never imported by the package); renders five
views per model with the Workbench renderer, no window.

    blender -b --factory-startup -P blender_render.py -- jobs.json results.json

jobs.json: {"textures": {basename_lower: path}, "atlas": path | null,
            "materials": {material_name_lower: colour texture path},   # from Unity .mat files
            "items": [{"src": model or .blend file, "out": folder for the view PNGs}]}
Each model gets front/back/left/top (orthographic, 256 px) and iso (perspective
3/4 from the front right, 512 px), transparent PNGs. results.json gets one entry
per item: size in Blender units (metres), triangle count, texture status, or an
error; files holding several models (character outfits on one skeleton, .blend
files with many props) return "variants", one per model, each in its own folder.
It is rewritten after every item so a crash loses nothing done before it.
"""
import json
import math
import os
import re
import sys
import zlib

import bpy
from mathutils import Vector

VIEWS = {  # camera direction from the model's centre (Blender: -Y is front)
    "front": Vector((0, -1, 0)),
    "back": Vector((0, 1, 0)),
    "left": Vector((-1, 0, 0)),
    "top": Vector((0, 0, 1)),
    "iso": Vector((1, -1, 0.8)).normalized(),
}
ORTHO_PX, ISO_PX = 256, 512
ISO_FOV = math.radians(40)
# helper meshes packs ship next to the visible one: shadow casters, collision, lower LODs
HELPER = re.compile(r"(_shadow(?![a-z0-9])|collision|convexhull|_collider|^ucx_|^ubx_|^ucp_|^usp_|_lod[1-9](?![0-9]))", re.I)
NOT_COLOUR = re.compile(r"(emiss|normal|_nrm|_n\.|rough|metal|spec|mask|_ao|occlusion|height|alpha)", re.I)
TEX_EXTS = (".png", ".tga", ".jpg", ".jpeg", ".tif", ".psd", ".bmp")
MAX_PARTS = 400  # a .blend with more top-level objects than this is a scene, rendered whole


# ---------- loading ----------

def load_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
        return
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".dae":
        bpy.ops.wm.collada_import(filepath=path)
    else:
        raise ValueError(f"unsupported format {ext}")


# ---------- textures ----------

def base_color_texture(mat):
    """The image node feeding Base Color, else the first that is not a normal/emissive/etc. map."""
    if not mat.node_tree:
        return None
    images = [n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image]
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED" and n.inputs["Base Color"].is_linked:
            src = n.inputs["Base Color"].links[0].from_node
            if src in images:
                return src
    return next((n for n in images if not NOT_COLOUR.search(n.image.name + " " + n.image.filepath)), None)


def find_texture(textures, name):
    """The pack's copy of a referenced image: same name, then the same stem with another
    extension, then its '_A' colour variant (Synty FBX point at Texture_01.psd, ship Texture_01_A.png)."""
    stem = os.path.splitext(name)[0].lower()
    for cand in [name.lower()] + [stem + e for e in TEX_EXTS] + [stem + "_a" + e for e in TEX_EXTS]:
        if cand in textures:
            return textures[cand]
    return None


def add_colour_map(mat, path):
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if not bsdf:
        return None
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(path, check_existing=True)
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return tex


def fix_textures(textures, atlas, materials):
    """Point missing image files at the pack's copies; give bare materials their Unity colour
    map or the pack atlas. Returns textured | missing | unity | atlas | untextured."""
    for img in bpy.data.images:
        if img.source != "FILE" or img.packed_file:
            continue
        path = bpy.path.abspath(img.filepath)
        if os.path.isfile(path):
            continue
        found = find_texture(textures, os.path.basename(path))
        if found:
            img.filepath = found
            img.reload()
    mats = {s.material for o in bpy.data.objects if o.type == "MESH" for s in o.material_slots if s.material}
    status = set()
    for m in mats:
        tex = base_color_texture(m)
        if not tex and materials:
            path = materials.get(re.sub(r"\.\d{3}$", "", m.name).lower())
            if path:
                tex = add_colour_map(m, path)
                if tex:
                    status.add("unity")
        if tex and not (tex.image.packed_file or os.path.isfile(bpy.path.abspath(tex.image.filepath))):
            # the referenced colour map is not in the pack: try the Unity material's, else drop it
            # (a missing image renders magenta; plain material colour is closer to the real thing)
            path = materials.get(re.sub(r"\.\d{3}$", "", m.name).lower()) if materials else None
            if path:
                tex.image = bpy.data.images.load(path, check_existing=True)
                status.add("unity")
            else:
                m.node_tree.nodes.remove(tex)
                tex = None
                status.add("missing")
        if tex:
            m.node_tree.nodes.active = tex  # Workbench shows the active image node
            status.add("textured")
    if "missing" in status:
        return "missing"
    if "unity" in status:
        return "unity"
    if status:
        return "textured"
    if not atlas or not mats:
        return "untextured"
    for m in mats:
        add_colour_map(m, atlas)
    return "atlas"


# ---------- which models are in the file ----------

def is_helper(o):
    return HELPER.search(o.name) is not None


def mesh_tree(o):
    return [x for x in [o, *o.children_recursive] if x.type == "MESH" and not is_helper(x)]


def is_variant_set(meshes):
    """Two or more meshes, all deformed by the same armature (Synty-style character outfits)."""
    if len(meshes) < 2:
        return False
    arms = set()
    for o in meshes:
        mods = [m for m in o.modifiers if m.type == "ARMATURE" and m.object]
        if not mods:
            return False
        arms.add(mods[0].object.name)
    return len(arms) == 1


def blend_groups():
    """Models in a .blend: assets marked for the Asset Browser, else each top-level object
    (with its children), else the whole scene."""
    groups = []
    for c in bpy.data.collections:
        if c.asset_data:
            ms = [o for o in c.all_objects if o.type == "MESH" and not is_helper(o)]
            if ms:
                groups.append((c.name, ms))
    for o in bpy.data.objects:
        if o.asset_data and (o.type == "MESH" or o.children):
            ms = mesh_tree(o)
            if ms:
                groups.append((o.name, ms))
    scene = bpy.context.scene
    roots = [o for o in scene.objects if o.parent is None and mesh_tree(o) and o.visible_get()]
    # trust Asset Browser marks only when they cover the file (some files mark just one small part)
    if groups and (len(groups) * 2 >= len(roots) or len(roots) > MAX_PARTS):
        return groups
    if 2 <= len(roots) <= MAX_PARTS:
        return [(o.name, mesh_tree(o)) for o in roots]
    return [("", [o for o in scene.objects if o.type == "MESH" and not is_helper(o) and o.visible_get()])]


# ---------- rendering ----------

def world_bounds(meshes):
    bpy.context.view_layer.update()  # child transforms are stale straight after import
    deps = bpy.context.evaluated_depsgraph_get()
    lo, hi, tris = Vector((1e9,) * 3), Vector((-1e9,) * 3), 0
    for o in meshes:
        ev = o.evaluated_get(deps)
        for corner in ev.bound_box:
            p = ev.matrix_world @ Vector(corner)
            lo = Vector(map(min, lo, p))
            hi = Vector(map(max, hi, p))
        mesh = ev.to_mesh()
        mesh.calc_loop_triangles()
        tris += len(mesh.loop_triangles)
        ev.to_mesh_clear()
    if not meshes:
        raise ValueError("no mesh in file")
    return lo, hi, tris


def setup_scene():
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.film_transparent = True
    try:  # a .blend saved for video output has to be switched back to still images first
        scene.render.image_settings.media_type = "IMAGE"
    except (AttributeError, TypeError):
        pass
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.display.render_aa = "8"
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "TEXTURE"
    shading.show_cavity = True
    shading.cavity_type = "WORLD"
    shading.show_backface_culling = False
    cam = bpy.data.objects.new("thumb_cam", bpy.data.cameras.new("thumb_cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    return scene, cam


def render_views(scene, cam, lo, hi, out):
    centre, size = (lo + hi) / 2, hi - lo
    radius = max(size.length / 2, 1e-4)
    extent = {"front": max(size.x, size.z), "back": max(size.x, size.z),
              "left": max(size.y, size.z), "top": max(size.x, size.y)}
    for view, d in VIEWS.items():
        if view == "iso":
            cam.data.type = "PERSP"
            cam.data.angle = ISO_FOV
            dist = radius / math.sin(ISO_FOV / 2) * 1.02
            px = ISO_PX
        else:
            cam.data.type = "ORTHO"
            cam.data.ortho_scale = max(extent[view], 1e-4) * 1.08
            dist = radius * 3
            px = ORTHO_PX
        cam.location = centre + d * dist
        cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
        cam.data.clip_start = max(dist - radius * 1.5, radius * 0.001)
        cam.data.clip_end = dist + radius * 1.5
        scene.render.resolution_x = scene.render.resolution_y = px
        scene.render.filepath = os.path.join(out, f"{view}.png")
        bpy.ops.render.render(write_still=True)


def render_groups(scene, cam, groups, out_root):
    """Render each (name, meshes) group on its own; everything else is hidden."""
    show = bpy.data.collections.new("thumb_show")  # makes marked assets render even if not in the scene
    scene.collection.children.link(show)
    every = set(scene.objects)
    variants = []
    for name, meshes in groups:
        for o in meshes:
            if o.name not in show.objects:
                show.objects.link(o)
        keep = set(meshes)
        for o in every | keep:
            o.hide_render = o not in keep and o is not cam
            if o in keep:
                o.hide_viewport = False
        lo, hi, tris = world_bounds(meshes)
        out = os.path.join(out_root, safe_name(name)) if name else out_root
        os.makedirs(out, exist_ok=True)
        render_views(scene, cam, lo, hi, out)
        variants.append({"name": safe_name(name) if name else "", "size_m": [round(x, 4) for x in hi - lo], "tris": tris})
        for o in meshes:
            show.objects.unlink(o)
    return variants


def render_one(item, jobs):
    load_file(item["src"])
    status = fix_textures(jobs["textures"], jobs.get("atlas"), jobs.get("materials") or {})
    scene, cam = setup_scene()
    if item["src"].lower().endswith(".blend"):
        groups = blend_groups()
    else:
        meshes = [o for o in bpy.data.objects if o.type == "MESH" and not is_helper(o)]
        for o in bpy.data.objects:
            if o.type == "MESH" and is_helper(o):
                o.hide_render = True
        groups = [(o.name, [o]) for o in meshes] if is_variant_set(meshes) else [("", meshes)]
    variants = render_groups(scene, cam, groups, item["out"])
    if len(variants) == 1 and not variants[0]["name"]:
        v = variants[0]
        return {"size_m": v["size_m"], "tris": v["tris"], "texture": status}
    return {"variants": variants, "texture": status}


def safe_name(name):
    """File-system-safe and short: Windows paths stop at 260 characters."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "mesh"
    return s if len(s) <= 48 else s[:40].rstrip("._") + "_" + format(zlib.crc32(s.encode()) % 16**6, "06x")


def main():
    jobs_path, results_path = sys.argv[sys.argv.index("--") + 1:][:2]
    jobs = json.load(open(jobs_path, encoding="utf-8"))
    results = []
    for item in jobs["items"]:
        try:
            r = render_one(item, jobs)
        except Exception as e:  # one bad file must not stop the pack
            r = {"error": f"{type(e).__name__}: {e}"[:300]}
        results.append({"src": item["src"], **r})
        with open(results_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh)


main()
