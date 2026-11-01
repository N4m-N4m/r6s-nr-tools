"""
In-Blender map cleanup for web export.

Port of the headless `r6_map_clean.py` pipeline into an operator that runs on the
already-imported scene (instead of importing a file). Deviation-bounded and
silhouette-preserving:

  1. delete non-render entities by name pattern (collision, triggers, volumes...)
  2. delete lights/cameras/curves etc., keep empties (hierarchy transforms)
  3. separate decal / flat-plane meshes (posters, grime, bullet holes) into a
     '_Decals' collection for review — the detector is a heuristic, so cleanup
     quarantines rather than deletes; export removes the collection
  4. per UNIQUE mesh datablock (preserves entity instancing!):
       - clear materials, UV layers, color attrs, custom split normals
       - merge by distance (weld) — the weld distance scales UP for props with a
         small bounding box (size-aware, distance-based)
       - limited dissolve (planar decimation, boundaries locked) — uniform angle
       - triangulate (BEAUTY)

Operates on bpy.data so linked-duplicate instancing is preserved: each unique
mesh datablock is cleaned once no matter how many objects share it.

Size-aware weld: the viewer frames the whole map from a distance, so a prop with
a small bounding box covers few pixels and can take a heavier merge-by-distance
weld without a visible change. "Small" is the LONGEST bounding-box side, so a
thin-but-large object keeps the gentle base weld. The threshold is RELATIVE to a
robust map-scale reference (a high percentile of object sizes, so a lone skybox
shell can't skew it), holding whether the map imported in metres or centimetres.
Everything is bmesh — no per-object modifier evaluation — so the pass stays fast.
"""

import time
from math import radians

import bpy
import bmesh

# Name substrings that mark non-render gameplay entities. Matches the
# DEFAULT_STRIP list from r6_map_clean.py. The Siege importer also tags objects
# with has_<component> custom props, which the operator additionally honours.
DEFAULT_STRIP = [
    "collision", "collider", "_col", "hitbox", "physic", "phys_",
    "trigger", "volume", "occluder", "occlusion", "portal",
    "navmesh", "nav_", "bullet", "destruct_helper", "gameplay",
    "spawn", "blocker", "clip_", "_clip",
]

# Non-render object types removed wholesale (empties are kept — they carry the
# entity hierarchy transforms).
DELETE_TYPES = ("LIGHT", "CAMERA", "CURVE", "SURFACE", "META", "FONT",
                "ARMATURE", "LATTICE", "GPENCIL", "SPEAKER", "LIGHT_PROBE")


def _name_matches(name, patterns):
    n = name.lower()
    return any(p in n for p in patterns)


def _collect_with_children(obj, out):
    out.add(obj)
    for c in obj.children:
        _collect_with_children(c, out)


def filter_entities(strip_patterns, keep_patterns, use_component_tags=True):
    """Delete non-render / pattern-matched objects. Returns the deleted count."""
    doomed = set()
    for obj in bpy.data.objects:
        if keep_patterns and _name_matches(obj.name, keep_patterns):
            continue
        if obj.type in DELETE_TYPES:
            doomed.add(obj)
        elif _name_matches(obj.name, strip_patterns):
            _collect_with_children(obj, doomed)
        elif use_component_tags and obj.type == 'MESH':
            # Siege-imported objects carry has_<component> tags. Pure
            # trigger/spawn volumes with no visual component are non-render.
            cat = obj.get('entity_category')
            if cat in ('SPAWN', 'RUNOUT_BLOCKER', 'SOUND') and not obj.get('has_visual'):
                _collect_with_children(obj, doomed)

    doomed = {o for o in doomed
              if not (keep_patterns and _name_matches(o.name, keep_patterns))}
    for obj in doomed:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    # orphans_purge() kwargs changed across Blender versions (4.x had
    # do_linked/do_recursive; 5.0 dropped them). Try the modern no-arg form
    # first and fall back so this works on both.
    try:
        bpy.data.orphans_purge(do_recursive=True)
    except TypeError:
        bpy.data.orphans_purge()
    return len(doomed)


def _clear_custom_split_normals(me):
    # Blender 4.x stores custom normals as a 'custom_normal' attribute.
    try:
        attr = me.attributes.get("custom_normal")
        if attr is not None:
            me.attributes.remove(attr)
            return
    except Exception:
        pass
    try:
        if getattr(me, "has_custom_normals", False):
            me.normals_split_custom_set([(0.0, 0.0, 0.0)] * len(me.loops))
    except Exception:
        pass
    if hasattr(me, "use_auto_smooth"):
        me.use_auto_smooth = False


def _strip_mesh_extras(me, keep_uvs):
    me.materials.clear()
    if not keep_uvs:
        while me.uv_layers:
            me.uv_layers.remove(me.uv_layers[0])
    try:
        while me.color_attributes:
            me.color_attributes.remove(me.color_attributes[0])
    except Exception:
        pass
    _clear_custom_split_normals(me)


def _planar_clean_mesh(me, merge_dist, angle_deg, triangulate):
    bm = bmesh.new()
    bm.from_mesh(me)
    # weld -- split verts break dissolving & decimation
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_dist)
    # limited dissolve: merges only coplanar geometry within angle limit;
    # boundaries are NOT dissolved -> silhouette stays intact.
    bmesh.ops.dissolve_limit(
        bm, angle_limit=radians(angle_deg),
        use_dissolve_boundaries=False,
        verts=bm.verts, edges=bm.edges, delimit=set())
    bmesh.ops.dissolve_degenerate(bm, dist=merge_dist, edges=bm.edges)
    if triangulate:
        bmesh.ops.triangulate(bm, faces=bm.faces,
                              quad_method="BEAUTY", ngon_method="BEAUTY")
    bm.to_mesh(me)
    bm.free()
    me.update()


# --- decal removal ------------------------------------------------------------

def _separate_decals(context, log):
    """Quarantine flat single-facing decal meshes into a '_Decals' collection.

    Non-destructive: the flat-plane detector is only a heuristic, so cleanup
    moves suspects aside for review instead of deleting them (export removes the
    collection). Returns the number of objects separated.
    """
    from . import decals   # lazy: avoids a web<->web import cycle at load time
    scene = (context.scene if context is not None else bpy.context.scene)
    objs = [o for o in bpy.data.objects if o.type == 'MESH' and o.data]
    hits = decals.find_decals(objs, decals.DEFAULT_FLATNESS_RATIO,
                              decals.DEFAULT_NORMAL_TOL_DEG,
                              decals.DEFAULT_MAX_FACES)
    if not hits:
        log("no decals detected to separate")
        return 0
    col = bpy.data.collections.get(decals.DECAL_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(decals.DECAL_COLLECTION)
        scene.collection.children.link(col)
    for obj in hits:
        obj["web_decal"] = True
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        col.objects.link(obj)
    log(f"separated {len(hits)} decal / flat-plane meshes into "
        f"'{decals.DECAL_COLLECTION}'")
    return len(hits)


# --- size-aware distance merge (fast, bmesh-only) -----------------------------
#
# The web viewer frames the whole map from a distance, so a prop with a SMALL
# bounding box covers few pixels and can take a heavier merge-by-distance weld
# without a visible change. "Small" is judged by the LONGEST bounding-box side,
# so a thin-but-large object (large in two axes, flat in one) is NOT treated as
# small and keeps the gentle base weld.
#
# Thresholds are RELATIVE to a robust map-scale reference (a high percentile of
# object sizes, so a single skybox / environment shell can't skew them) and so
# hold whether the map imported in metres or centimetres. Everything here runs
# in bmesh (no per-object modifier evaluation), which keeps the pass fast.
SMALL_REL = 0.05       # longest side < 5% of map scale -> start boosting the weld
TINY_REL = 0.015       # <= 1.5% of map scale -> full boost
MAX_WELD_FRAC = 0.03   # full-boost weld = 3% of the prop's own longest side
SIZE_REF_PERCENTILE = 0.90   # robust "map scale": 90th pct of object longest sides


def _obj_longest_side(obj):
    """Longest world-space bounding-box side of a mesh object (accounts for scale)."""
    from mathutils import Vector
    m = obj.matrix_world
    corners = [m @ Vector(c) for c in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _datablock_sizes(meshes):
    """{mesh.name: max longest-bbox-side across its instance objects}.

    MAX over instances so a datablock placed large *anywhere* keeps its detail
    (instancing means one datablock may sit at several scales).
    """
    sizes = {m.name: 0.0 for m in meshes}
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        name = obj.data.name
        if name in sizes:
            s = _obj_longest_side(obj)
            if s > sizes[name]:
                sizes[name] = s
    return sizes


def _percentile(values, q):
    """q-quantile (0..1) of a list, nearest-rank. 0.0 for an empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _weld_for_size(size, ref, base):
    """Effective weld distance for an object whose longest side is `size`.

    At/above SMALL_REL of the map-scale reference: the gentle `base` weld. Below
    it, ramp toward a weld of MAX_WELD_FRAC * size as the object shrinks to
    TINY_REL — a merge distance PROPORTIONAL to the prop, so a small prop loses
    fine detail regardless of whether the map imported in metres or centimetres,
    while never welding across more than MAX_WELD_FRAC of the prop itself.
    """
    if ref <= 0.0 or size <= 0.0:
        return base
    rel = size / ref
    if rel >= SMALL_REL:
        return base
    t = (SMALL_REL - rel) / max(SMALL_REL - TINY_REL, 1e-6)
    t = min(max(t, 0.0), 1.0)
    return max(base, size * MAX_WELD_FRAC * t)


def clean_scene(merge_dist=0.0005, angle=3.0, triangulate=True,
                keep_uvs=False, strip_patterns=None, keep_patterns=None,
                delete_non_render=True, separate_decals=True,
                size_aware=True, context=None, log=print):
    """Run the full cleanup on the current blend's mesh datablocks.

    Returns a dict of before/after vert+face totals and the deleted-object count.
    """
    strip = list(DEFAULT_STRIP) + [s.strip().lower()
                                   for s in (strip_patterns or []) if s.strip()]
    keep = [s.strip().lower() for s in (keep_patterns or []) if s.strip()]

    deleted = 0
    if delete_non_render:
        deleted = filter_entities(strip, keep)
        log(f"deleted {deleted} non-render / pattern-matched objects")

    decals_separated = 0
    if separate_decals:
        decals_separated = _separate_decals(context, log)

    # Clear per-object blockers (shape keys / vertex groups / modifiers).
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            if obj.data.shape_keys:
                obj.shape_key_clear()
            obj.vertex_groups.clear()
            obj.modifiers.clear()

    meshes = [m for m in bpy.data.meshes if m.users > 0]
    n = len(meshes)
    placements = sum(m.users for m in meshes)
    log(f"{n} unique meshes, {placements} placements (instancing preserved)")

    # Per-datablock longest side -> a heavier merge-by-distance weld for small
    # props. Measured before the loop so deletions above don't skew it. The
    # reference is a robust percentile so a lone skybox shell can't skew it.
    sizes = _datablock_sizes(meshes) if size_aware else {}
    size_ref = _percentile(list(sizes.values()), SIZE_REF_PERCENTILE)
    if size_aware and size_ref <= 0.0:
        log("size-aware weld off: could not measure map scale")
    elif size_aware:
        log(f"size-aware weld on: map scale ref {size_ref:.1f} units "
            f"(small < {size_ref * SMALL_REL:.2f})")

    boosted = 0
    v_before = t_before = v_after = t_after = 0
    t0 = time.time()
    for i, me in enumerate(meshes):
        v_before += len(me.vertices)
        t_before += len(me.polygons)
        _strip_mesh_extras(me, keep_uvs)

        eff_merge = merge_dist
        if size_aware and size_ref > 0.0:
            eff_merge = _weld_for_size(sizes.get(me.name, 0.0), size_ref, merge_dist)
            if eff_merge > merge_dist:
                boosted += 1

        try:
            _planar_clean_mesh(me, eff_merge, angle, triangulate)
        except Exception as e:
            log(f"  WARN mesh '{me.name}' cleanup failed, kept as-is: {e}")

        v_after += len(me.vertices)
        t_after += len(me.polygons)
        if i and i % 250 == 0:
            log(f"  {i}/{n} meshes... ({time.time()-t0:.0f}s)")

    if size_aware and size_ref > 0.0:
        log(f"size-aware weld boosted {boosted}/{n} small meshes")

    def pct(a, b):
        return f"{(1 - a / b) * 100:.1f}%" if b else "n/a"
    log(f"verts: {v_before:,} -> {v_after:,}  (-{pct(v_after, v_before)})")
    log(f"faces: {t_before:,} -> {t_after:,}  (-{pct(t_after, t_before)})")

    return {
        'deleted': deleted, 'decals': decals_separated, 'boosted': boosted,
        'meshes': n, 'placements': placements,
        'v_before': v_before, 'v_after': v_after,
        't_before': t_before, 't_after': t_after,
    }


class SIEGE_OT_web_clean(bpy.types.Operator):
    """Deviation-bounded, silhouette-preserving cleanup of the imported map.

    Per unique mesh datablock: strip materials/UVs/custom normals, weld, limited
    dissolve (planar decimation with locked boundaries), triangulate. Deletes
    non-render gameplay objects (collision/trigger/spawn volumes, lights, etc.)
    and quarantines flat decal meshes into a '_Decals' collection. Small-bbox
    props get a heavier merge-by-distance weld than large structure (size-aware),
    since the web viewer shows the map from a distance; thin-but-large objects
    are judged by their longest side and kept. All in bmesh — no per-object
    modifier evaluation. Instancing is preserved — each datablock is cleaned once.
    """
    bl_idname = "siege.web_clean"
    bl_label = "Siege: Clean Map for Web"
    bl_options = {'REGISTER', 'UNDO'}

    merge_dist: bpy.props.FloatProperty(
        name="Merge Distance", default=0.0005, min=0.0, max=1.0, precision=5,
        description="Weld threshold in scene units (merge-by-distance)")
    angle: bpy.props.FloatProperty(
        name="Dissolve Angle", default=3.0, min=0.0, max=30.0,
        description="Limited-dissolve (planar decimation) angle in degrees "
                    "(1-5 typical); applied uniformly to every mesh")
    triangulate: bpy.props.BoolProperty(
        name="Triangulate", default=True,
        description="Triangulate ngons (BEAUTY) after dissolve")
    keep_uvs: bpy.props.BoolProperty(
        name="Keep UVs", default=False,
        description="Keep UV layers (off for depth-only web shells)")
    delete_non_render: bpy.props.BoolProperty(
        name="Delete Non-Render", default=True,
        description="Delete collision/trigger/spawn/light objects by name + component tag")
    separate_decals: bpy.props.BoolProperty(
        name="Separate Decals", default=True,
        description="Move flat single-facing decal meshes (posters, grime, "
                    "bullet holes) into a '_Decals' collection for review "
                    "(non-destructive; export removes them)")
    size_aware: bpy.props.BoolProperty(
        name="Size-Aware Weld", default=True,
        description="Weld small-bounding-box props harder (merge-by-distance) "
                    "than large structure. The viewer frames the map from a "
                    "distance, so small props cover few pixels. Thin-but-large "
                    "objects are judged by their longest side and left alone")

    def execute(self, context):
        stats = clean_scene(
            merge_dist=self.merge_dist, angle=self.angle,
            triangulate=self.triangulate, keep_uvs=self.keep_uvs,
            delete_non_render=self.delete_non_render,
            separate_decals=self.separate_decals,
            size_aware=self.size_aware,
            context=context,
            log=lambda m: print(f"[SiegeClean] {m}"))
        self.report(
            {'INFO'},
            f"Cleaned {stats['meshes']} meshes: "
            f"{stats['v_before']:,}->{stats['v_after']:,} verts, "
            f"{stats['t_before']:,}->{stats['t_after']:,} faces, "
            f"-{stats['deleted']} objs, {stats['decals']} decals separated, "
            f"{stats['boosted']} small welded")
        return {'FINISHED'}


_classes = (SIEGE_OT_web_clean,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
