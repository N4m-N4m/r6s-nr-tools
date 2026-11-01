# r6_map_clean.py
# Headless Blender cleanup for Rainbow Six Siege forge map exports.
#
# Pipeline (deviation-bounded, silhouette-preserving):
#   1. import (fbx/obj/gltf/glb/dae/ply/stl autodetected)
#   2. delete non-render entities by name pattern (collision, triggers, volumes...)
#   3. delete lights/cameras/curves, keep empties (hierarchy transforms)
#   4. per UNIQUE mesh datablock (preserves entity instancing!):
#        - clear shape keys, materials, UV layers, color attrs, vertex groups
#        - clear custom split normals  (these block dissolving!)
#        - merge by distance (weld)
#        - limited dissolve (planar decimation, boundaries locked)
#        - triangulate (BEAUTY, avoids skinny tris)
#   5. export GLB, no materials, optionally no normals
#
# Usage:
#   blender --background --factory-startup --python r6_map_clean.py -- \
#       --input  "D:/maps/raw/oregon.fbx" \
#       --output "D:/maps/clean/oregon.glb" \
#       --angle 3.0 --merge-dist 0.0005
#
# Optional flags:
#   --keep-normals          export vertex normals (default: stripped, depth shading needs none)
#   --no-triangulate        leave ngons after dissolve
#   --strip-patterns a,b,c  extra name substrings to delete (case-insensitive)
#   --keep-patterns a,b     never delete objects containing these substrings

import bpy
import bmesh
import argparse
import sys
import time
from math import radians

# ---------------------------------------------------------------- args

def get_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--angle", type=float, default=3.0,
                   help="limited dissolve angle limit in degrees (1-5 typical)")
    p.add_argument("--merge-dist", type=float, default=0.0005,
                   help="merge-by-distance threshold in scene units")
    p.add_argument("--keep-normals", action="store_true")
    p.add_argument("--no-triangulate", action="store_true")
    p.add_argument("--strip-patterns", type=str, default="")
    p.add_argument("--keep-patterns", type=str, default="")
    return p.parse_args(argv)

ARGS = get_args()

DEFAULT_STRIP = [
    "collision", "collider", "_col", "hitbox", "physic", "phys_",
    "trigger", "volume", "occluder", "occlusion", "portal",
    "navmesh", "nav_", "bullet", "destruct_helper", "gameplay",
    "spawn", "blocker", "clip_", "_clip",
]
STRIP = DEFAULT_STRIP + [s.strip().lower() for s in ARGS.strip_patterns.split(",") if s.strip()]
KEEP = [s.strip().lower() for s in ARGS.keep_patterns.split(",") if s.strip()]

def log(msg):
    print(f"[r6clean] {msg}", flush=True)

# ---------------------------------------------------------------- import

def fresh_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)

def import_file(path):
    ext = path.lower().rsplit(".", 1)[-1]
    log(f"importing {path} ({ext})")
    if ext == "fbx":
        bpy.ops.import_scene.fbx(filepath=path, use_custom_normals=True)
    elif ext == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in ("gltf", "glb"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == "dae":
        bpy.ops.wm.collada_import(filepath=path)
    elif ext == "ply":
        op = getattr(bpy.ops.wm, "ply_import", None) or bpy.ops.import_mesh.ply
        op(filepath=path)
    elif ext == "stl":
        op = getattr(bpy.ops.wm, "stl_import", None) or bpy.ops.import_mesh.stl
        op(filepath=path)
    else:
        raise ValueError(f"unsupported input format: .{ext}")

# ---------------------------------------------------------------- filtering

def name_matches(name, patterns):
    n = name.lower()
    return any(p in n for p in patterns)

def collect_with_children(obj, out):
    out.add(obj)
    for c in obj.children:
        collect_with_children(c, out)

def filter_entities():
    doomed = set()
    for obj in bpy.data.objects:
        if KEEP and name_matches(obj.name, KEEP):
            continue
        if obj.type in ("LIGHT", "CAMERA", "CURVE", "SURFACE", "META", "FONT",
                        "ARMATURE", "LATTICE", "GPENCIL", "SPEAKER", "LIGHT_PROBE"):
            doomed.add(obj)
        elif name_matches(obj.name, STRIP):
            collect_with_children(obj, doomed)
    doomed = {o for o in doomed if not (KEEP and name_matches(o.name, KEEP))}
    log(f"deleting {len(doomed)} non-render / pattern-matched objects")
    for obj in doomed:
        bpy.data.objects.remove(obj, do_unlink=True)
    # drop now-orphaned data
    bpy.data.orphans_purge(do_linked=False, do_recursive=True)

# ---------------------------------------------------------------- mesh cleanup

def clear_custom_split_normals(me):
    # Blender 4.x: stored as a 'custom_normal' attribute
    try:
        attr = me.attributes.get("custom_normal")
        if attr is not None:
            me.attributes.remove(attr)
            return
    except Exception:
        pass
    # Blender 3.x fallback
    try:
        if getattr(me, "has_custom_normals", False):
            me.normals_split_custom_set([(0.0, 0.0, 0.0)] * len(me.loops))
    except Exception:
        pass
    if hasattr(me, "use_auto_smooth"):
        me.use_auto_smooth = False

def strip_mesh_extras(me):
    me.materials.clear()
    while me.uv_layers:
        me.uv_layers.remove(me.uv_layers[0])
    try:
        while me.color_attributes:
            me.color_attributes.remove(me.color_attributes[0])
    except Exception:
        pass
    clear_custom_split_normals(me)

def planar_clean_mesh(me, merge_dist, angle_deg, triangulate):
    bm = bmesh.new()
    bm.from_mesh(me)

    # 1. weld -- split verts are what break dissolving & decimation
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_dist)

    # 2. limited dissolve: ONLY merges coplanar geometry within angle limit,
    #    boundaries are NOT dissolved -> silhouette stays intact.
    bmesh.ops.dissolve_limit(
        bm,
        angle_limit=radians(angle_deg),
        use_dissolve_boundaries=False,
        verts=bm.verts,
        edges=bm.edges,
        delimit=set(),  # materials/UVs already stripped, nothing to delimit
    )

    # 3. drop degenerate leftovers
    bmesh.ops.dissolve_degenerate(bm, dist=merge_dist, edges=bm.edges)

    # 4. triangulate ngons with BEAUTY to avoid long skinny triangles
    if triangulate:
        bmesh.ops.triangulate(bm, faces=bm.faces,
                              quad_method="BEAUTY", ngon_method="BEAUTY")

    bm.to_mesh(me)
    bm.free()
    me.update()

def process_meshes():
    # clear shape keys per-object first (they block bmesh edits)
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            if obj.data.shape_keys:
                obj.shape_key_clear()
            obj.vertex_groups.clear()
            obj.modifiers.clear()

    meshes = list(bpy.data.meshes)
    n = len(meshes)
    users_total = sum(m.users for m in meshes)
    log(f"{n} unique meshes, {users_total} placements (instancing preserved)")

    v_before = t_before = v_after = t_after = 0
    t0 = time.time()
    for i, me in enumerate(meshes):
        if me.users == 0:
            continue
        v_before += len(me.vertices)
        t_before += len(me.polygons)

        strip_mesh_extras(me)
        try:
            planar_clean_mesh(me, ARGS.merge_dist, ARGS.angle,
                              not ARGS.no_triangulate)
        except Exception as e:
            log(f"  WARN mesh '{me.name}' cleanup failed, kept as-is: {e}")

        v_after += len(me.vertices)
        t_after += len(me.polygons)

        if i % 250 == 0:
            log(f"  {i}/{n} meshes... ({time.time()-t0:.0f}s)")

    def pct(a, b): return f"{(1 - a / b) * 100:.1f}%" if b else "n/a"
    log(f"verts: {v_before:,} -> {v_after:,}  (-{pct(v_after, v_before)})")
    log(f"faces: {t_before:,} -> {t_after:,}  (-{pct(t_after, t_before)})")

# ---------------------------------------------------------------- export

def export_glb(path):
    kwargs = dict(
        filepath=path,
        export_format="GLB",
        export_materials="NONE",
        export_normals=ARGS.keep_normals,
        export_texcoords=False,
        export_colors=False,
        export_tangents=False,
        export_skins=False,
        export_animations=False,
        export_morph=False,
        export_apply=True,
        export_yup=True,
        use_selection=False,
    )
    # filter kwargs against the installed exporter version's accepted props
    props = bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
    kwargs = {k: v for k, v in kwargs.items() if k in props or k == "filepath"}
    log(f"exporting {path}")
    bpy.ops.export_scene.gltf(**kwargs)

# ---------------------------------------------------------------- main

def main():
    t0 = time.time()
    fresh_scene()
    import_file(ARGS.input)
    filter_entities()
    process_meshes()
    export_glb(ARGS.output)
    log(f"done in {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
