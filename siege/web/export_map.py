"""Multi-floor web map export: per-(floor, kind) GLBs + manifest.json.

Companion to web/export.py (which writes a single GLB). This operator splits the
scene into one GLB per floor and kind so the web viewer can hide floors and
colour categories independently, and writes the manifest.json + maps/index.json
the viewer reads.

Reuses web/export.py's infrastructure: sanitize_scene (drop non-finite/oversized
transforms) and _supported_gltf_kwargs (version-safe exporter kwargs), plus its
GPU-instancing + Draco settings.

Orientation: export_yup=True does the single Z->Y conversion; the manifest says
up:"Y" so the viewer does not rotate again (a Y-up GLB loaded as Z-up is what
tipped the model 90 deg on X).

Floors: explicit floor collections the user assigns in the Floors panel
(web/floors.py) win — each collection exports as one floor, in list order. With
none defined we fall back to world-Z banding (importer floor-group entities are
only marker volumes, not a per-object floor index), using gap detection.

AO: optionally bake scene AO into vertex colours first (web/ao_bake.py); the
GLBs then carry it as COLOR_0 and the manifest sets ao.bakedAO=true so the
viewer skips its realtime GTAO pass.
"""

import json
import math
import os

import bpy

from . import export as web_export   # sanitize_scene, _supported_gltf_kwargs


# entity_category (importer) -> viewer kind. Unmapped -> 'walls'.
CATEGORY_TO_KIND = {
    'FLOOR_GROUP':      'floor',
    'NON_DESTRUCTIBLE': 'walls',
    'VISUAL':           'walls',
    'DESTRUCTIBLE':     'softwalls',
    'REINFORCEMENT':    'reinforcement',
    'OBJECTIVE':        'objective',
    'INTERACTIVE':      'interactive',
}
KIND_COLORS = {
    'floor': '#3a3f4a', 'walls': '#5a6a82', 'softwalls': '#ff3a3a',
    'reinforcement': '#c8a24a', 'objective': '#37c871', 'interactive': '#4a90ff',
}
KIND_LABELS = {
    'floor': 'Floors', 'walls': 'Walls', 'softwalls': 'Soft walls',
    'reinforcement': 'Reinforcements', 'objective': 'Objectives',
    'interactive': 'Interactive',
}
# Not visible geometry -> never exported.
SKIP_CATEGORIES = {'SOUND', 'LIGHT', 'SPAWN', 'RUNOUT_BLOCKER', 'OTHER'}


def object_kind(obj):
    cat = obj.get('entity_category')
    if cat in SKIP_CATEGORIES:
        return None
    return CATEGORY_TO_KIND.get(cat, 'walls')


def band_floors(zvals, min_gap_ratio=0.15, min_gap_abs=1.5, max_floors=12):
    """Cluster sorted Z centres into floor bands by gap detection.

    A new band starts where the gap exceeds max(min_gap_ratio*span, min_gap_abs
    metres) — the absolute floor stops small maps being sliced into bogus floors.
    Pure function for testability.
    """
    if not zvals:
        return []
    zs = sorted(zvals)
    span = zs[-1] - zs[0]
    if span <= 1e-6:
        return [(zs[0], zs[-1])]
    threshold = max(span * min_gap_ratio, min_gap_abs)
    bands = []
    start = prev = zs[0]
    for z in zs[1:]:
        if (z - prev) > threshold:
            bands.append((start, prev))
            start = z
        prev = z
    bands.append((start, prev))
    while len(bands) > max_floors:
        gaps = [(bands[i + 1][0] - bands[i][1], i) for i in range(len(bands) - 1)]
        _, i = min(gaps)
        bands[i] = (bands[i][0], bands[i + 1][1])
        del bands[i + 1]
    return bands


def floor_label(index, total):
    return "All geometry" if total == 1 else f"Floor {index + 1}"


def _safe_name(label):
    """Filename-safe version of a (possibly user-typed) floor label."""
    out = ''.join(ch if (ch.isalnum() or ch in '-_') else '_'
                  for ch in label.strip()).strip('_')
    return out or 'floor'


def _center_z(obj):
    from mathutils import Vector
    zs = [(obj.matrix_world @ Vector((c[0], c[1], c[2]))).z for c in obj.bound_box]
    return (min(zs) + max(zs)) * 0.5


def _scene_extent(objs):
    """Largest world-space XYZ dimension of the meshes (for scale detection)."""
    from mathutils import Vector
    lo = [float('inf')] * 3
    hi = [float('-inf')] * 3
    for o in objs:
        m = o.matrix_world
        for c in o.bound_box:
            w = m @ Vector((c[0], c[1], c[2]))
            for i in range(3):
                lo[i] = min(lo[i], w[i]); hi[i] = max(hi[i], w[i])
    if lo[0] == float('inf'):
        return 1.0
    return max(hi[i] - lo[i] for i in range(3))


def _auto_scale(extent):
    """Pick a manifest scale that brings the map to a sensible metre-ish size.

    Siege maps usually export in centimetres (~3000 units across); the viewer's
    fog + camera expect metres. If the map is clearly cm-scale (very large),
    return 0.01; otherwise leave it at 1.0. Returns (scale, fog_near, fog_far).

    IMPORTANT: feed this the PLAYABLE extent (building), not the whole scene — a
    large skybox/environment shell would otherwise trip the >500 branch and
    shrink the actual building 100x (the "map too small" bug).
    """
    if extent > 500.0:          # cm-scale map (e.g. Casino ~3000 units)
        scale = 0.01
    else:
        scale = 1.0
    sized = extent * scale       # extent in viewer units after scaling
    fog_near = max(sized * 0.05, 1.0)
    fog_far = max(sized * 2.0, 20.0)
    return scale, round(fog_near, 1), round(fog_far, 1)


def _objs_world_bbox(objs):
    """World-space AABB (lo, hi) over the mesh objects, Blender Z-up. None if
    empty."""
    from mathutils import Vector
    lo = [float('inf')] * 3
    hi = [float('-inf')] * 3
    n = 0
    for o in objs:
        if o.type != 'MESH':
            continue
        m = o.matrix_world
        for c in o.bound_box:
            w = m @ Vector((c[0], c[1], c[2]))
            for i in range(3):
                lo[i] = min(lo[i], w[i]); hi[i] = max(hi[i], w[i])
        n += 1
    return (lo, hi) if n else None


def _playable_bounds(ordered, log=print):
    """Union AABB (Blender Z-up) of the 'playable' floors — every floor whose
    size is NOT a gross outlier. A skybox / environment shell dwarfs the
    building (e.g. Casino: ~3000-unit shell vs ~250-unit building); including it
    both fools scale detection and makes the viewer frame empty space. Drop any
    floor more than 3x the median floor size. Returns (lo, hi) or None.
    """
    boxes = []
    for label, _order, objs in ordered:
        vis = [o for o in objs if o.type == 'MESH' and object_kind(o)]
        bb = _objs_world_bbox(vis)
        if bb:
            lo, hi = bb
            boxes.append((label, lo, hi, max(hi[i] - lo[i] for i in range(3))))
    if not boxes:
        return None
    dims = sorted(b[3] for b in boxes)
    median = dims[len(dims) // 2]
    playable = [b for b in boxes if len(boxes) == 1 or b[3] <= median * 3.0]
    dropped = [b[0] for b in boxes if b not in playable]
    if dropped:
        log(f"bounds/scale exclude oversized floor(s): {', '.join(dropped)}")
    lo = [min(b[1][i] for b in playable) for i in range(3)]
    hi = [max(b[2][i] for b in playable) for i in range(3)]
    return lo, hi


def _bounds_to_yup(lo, hi, scale):
    """Blender Z-up AABB -> viewer Y-up manifest bounds, in the exported (baked)
    space. glTF Y-up conversion is (x, y, z)_gltf = (x, z, -y)_blender, so the
    Y range comes from Blender Z (height) and the Z range from negated Blender
    Y. Multiply by the baked scale so bounds match the exported geometry.
    """
    return {
        "min": [round(lo[0] * scale, 2), round(lo[2] * scale, 2), round(-hi[1] * scale, 2)],
        "max": [round(hi[0] * scale, 2), round(hi[2] * scale, 2), round(-lo[1] * scale, 2)],
    }


def _rooms_to_yup(scene, scale):
    """The importer's room table, converted to the manifest's Y-up baked space.

    The GLBs go through `(x, y, z)_blender -> (x, z, -y)_gltf` and are baked by
    `scale`, so the room volumes have to make the same trip or they won't line up
    with the geometry. For a half-space `dot(n, p) <= d` under that map the
    normal transforms exactly like a position (it is a rotation) and only the
    offset picks up the scale:

        n' = (nx, nz, -ny)        d' = d * scale

    Returns `[]` when the scene carries no table — a hand-built scene, or a v34
    map, where Spaces aren't recovered yet.
    """
    raw = scene.get('siege_room_table')
    if not raw:
        return []
    try:
        rooms = json.loads(raw)
    except Exception:
        return []

    def snap(v):
        # Hull normals come back as 0.99999 / -1e-05; the cells are mostly
        # axis-aligned, so snapping keeps the file small and the planes exact.
        r = round(v, 4)
        for t in (-1.0, 0.0, 1.0):
            if abs(r - t) < 1e-3:
                return t
        return r

    out = []
    for r in rooms:
        # Planes flattened to a stride-4 number list: [nx,ny,nz,d, nx,ny,nz,d...].
        # Half the punctuation of nested arrays, and a viewer walks it directly.
        cells = []
        for cell in r.get('cells', ()):
            flat = []
            for nx, ny, nz, d in cell:
                flat += [snap(nx), snap(nz), snap(-ny), round(d * scale, 3)]
            cells.append(flat)
        lo = r['aabb'][:3]
        hi = r['aabb'][3:]
        out.append({
            "id": r['id'],
            "name": r['name'],
            # 'compass' == the name the in-game compass shows, read from the map
            # forge. 'entity' == the level-designer entity name, only present if
            # the user supplied a Heated Metal EntityIDMap. 'generated' == ours.
            # Expect a mix; a viewer may want to style a generated label
            # differently, and 'compass' is the one players actually say.
            "nameSource": r.get('nameSource', 'generated'),
            "floor": r['floor'],
            "floorIndex": r['floorIndex'],
            "exterior": r.get('exterior', False),
            "exact": r.get('exact', True),
            "bounds": _bounds_to_yup(lo, hi, scale),
            "cells": cells,
        })
    return out


def _tagged_floors(scene, objs):
    """Floors from the importer's `siege_floor` tags, or None if absent.

    These come from the engine's own Space (room) partition, so they place
    stairwells, mezzanines and sloped roofs the way the level designers did —
    which is exactly what world-Z banding cannot do. Ordering follows the
    imported floor table (basement first) rather than label sort order, so
    'B' precedes '1F' and '10F' doesn't sort before '2F'.

    Returns `[(label, order_index, [objs])]`.
    """
    import json

    order = {}
    raw = scene.get('siege_floor_table')
    if raw:
        try:
            order = {f['label']: f['index'] for f in json.loads(raw)}
        except Exception:
            order = {}

    buckets = {}
    for o in objs:
        if not object_kind(o):
            continue
        label = o.get('siege_floor')
        if isinstance(label, str) and label:
            buckets.setdefault(label, []).append(o)
    if not buckets:
        return None
    labels = sorted(buckets, key=lambda l: (order.get(l, 999), l))
    return [(label, i, buckets[label]) for i, label in enumerate(labels)]


def _assign_floor(objs):
    """Return ({floor_index: [objs]}, bands) keyed by Z-band membership."""
    meshes = [o for o in objs if o.type == 'MESH' and object_kind(o)]
    if not meshes:
        return {}, []
    zc = {o: _center_z(o) for o in meshes}
    bands = band_floors(list(zc.values()))
    buckets = {i: [] for i in range(len(bands))}
    for o, z in zc.items():
        idx = 0
        for i, (lo, hi) in enumerate(bands):
            if z <= hi + 1e-6:
                idx = i
                break
            idx = i
        buckets[idx].append(o)
    return buckets, bands


def _select_only(objs):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def _sorted_parents_first(objs):
    """Topological order: a parent always precedes its children. Needed because
    setting matrix_world on a child derives its local basis from the parent's
    CURRENT world, so parents must be updated first (else a scaled parent
    double-counts into the child)."""
    objset = set(objs)
    ordered = []
    seen = set()

    def visit(o):
        if o in seen:
            return
        p = o.parent
        if p is not None and p in objset:
            visit(p)
        seen.add(o)
        ordered.append(o)

    for o in objs:
        visit(o)
    return ordered


def _bake_world_scale(objs, factor):
    """Uniformly scale every object's world transform by `factor` about the
    origin — baking the map to metres so the exported GLB IS metres and needs no
    runtime scale. Returns saved (obj, matrix_world) pairs for restoration.
    Applied parents-first so child bases resolve against already-scaled parents.
    """
    from mathutils import Matrix
    S = Matrix.Scale(factor, 4)
    order = _sorted_parents_first(objs)
    saved = [(o, o.matrix_world.copy()) for o in order]
    for o in order:
        o.matrix_world = S @ o.matrix_world
    return saved


def _restore_world(saved):
    """Undo _bake_world_scale — same parents-first order, absolute matrices."""
    for o, m in saved:
        o.matrix_world = m


# Draco quantization bit-depths for the web viewer. Defaults are far finer than
# a stylized, depth-shaded shell viewer can resolve — Blender's exporter defaults
# are position=14 (~sub-mm on a 30 m part), normal=10, color=10, generic=12.
# Dropping them is the single biggest safe size win: position 14->11 alone is
# ~1.5 cm resolution and typically cuts 30-40% off the position stream. The
# viewer re-shapes baked AO (COLOR_0) live, so 8-bit colour is plenty.
DRACO_POSITION_BITS = 11
DRACO_NORMAL_BITS = 8
DRACO_COLOR_BITS = 8
DRACO_GENERIC_BITS = 8


# Per-node glTF `extras`, kept to two integer keys. The viewer reads them from
# `object.userData` and looks the codes up in the manifest legends. Short keys
# because they are repeated once per node: on a 12k-object map the difference
# between {"k":1,"r":12} and {"entity_category":"walls","siege_room":"1F_Room_046"}
# is a few hundred KB of uncompressed JSON in the GLB.
EXTRA_KIND = 'k'      # index into manifest.kinds
EXTRA_ROOM = 'r'      # index into the rooms table, -1 when untagged


def _stage_extras(objs, kind_index, room_index):
    """Replace each object's custom properties with the two export keys.

    glTF exports *every* ID property as node extras, and the importer leaves a
    dozen per object (uid, uid_hex, siege_space, web_size_class...). Exporting
    them all would bloat the GLB for no viewer benefit, so the properties are
    swapped for the compact pair and restored afterwards.

    Returns the saved state for `_restore_extras`; always restore in a finally.
    """
    saved = []
    for o in objs:
        props = {k: o[k] for k in o.keys()}
        # Both lookups must happen BEFORE the properties are removed — they are
        # what `object_kind` and the room join read.
        kind = kind_index.get(object_kind(o), 0)
        room = room_index.get(props.get('siege_room'), -1)
        saved.append((o, props))
        for k in props:
            del o[k]
        o[EXTRA_KIND] = kind
        o[EXTRA_ROOM] = room
    return saved


def _restore_extras(saved):
    for o, props in saved:
        for k in list(o.keys()):
            del o[k]
        for k, v in props.items():
            o[k] = v


def _export_part(filepath, use_draco, draco_level, export_colors,
                 pos_bits=DRACO_POSITION_BITS, normal_bits=DRACO_NORMAL_BITS,
                 color_bits=DRACO_COLOR_BITS, generic_bits=DRACO_GENERIC_BITS):
    """Export the current selection to GLB (Y-up). Returns True on success."""
    kwargs = dict(
        filepath=filepath,
        export_format="GLB",
        export_materials="NONE",
        export_normals=True,            # AO/shading want normals
        export_texcoords=False,
        export_colors=export_colors,    # COLOR_0 carries baked AO
        export_tangents=False,
        export_skins=False,
        export_animations=False,
        export_morph=False,
        export_apply=True,
        export_yup=True,
        use_selection=True,
        export_gpu_instances=True,
        export_draco_mesh_compression_enable=bool(use_draco),
        export_draco_mesh_compression_level=int(draco_level),
        # Quantization: coarser than the exporter defaults, tuned for the viewer.
        # Unknown kwargs are dropped by _supported_gltf_kwargs, so this stays
        # safe on exporter versions that name these props differently.
        export_draco_position_quantization=int(pos_bits),
        export_draco_normal_quantization=int(normal_bits),
        export_draco_color_quantization=int(color_bits),
        export_draco_generic_quantization=int(generic_bits),
        # Node extras carry the per-object kind/room codes (see _stage_extras).
        export_extras=True,
    )
    kwargs = web_export._supported_gltf_kwargs(kwargs)
    bpy.ops.export_scene.gltf(**kwargs)
    return os.path.exists(filepath)


def _upsert_index(out_dir, map_name):
    """Add/update this map in maps/index.json without dropping others."""
    index_path = os.path.join(out_dir, "index.json")
    data = {"maps": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            data = {"maps": []}
    maps = data.setdefault("maps", [])
    entry = {"id": map_name, "name": map_name, "dir": map_name}
    for i, m in enumerate(maps):
        if m.get("dir") == map_name or m.get("id") == map_name:
            maps[i] = entry
            break
    else:
        maps.append(entry)
    with open(index_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)


def export_map(context, out_dir, map_name, use_draco=True, draco_level=6,
               bake_ao=False, clean_decals=False,
               draco_position_bits=DRACO_POSITION_BITS, split_by_kind=False,
               log=print):
    """Export the scene to out_dir/<map>/ as per-floor GLBs + manifest.

    Default is ONE GLB per floor, with each node carrying its entity kind and
    room as glTF `extras` (see `_stage_extras`). `split_by_kind=True` restores
    the older per-(floor, kind) file split, which trades more requests for the
    ability to fetch a single category.

    Returns (n_files, manifest_path).
    """
    map_dir = os.path.join(out_dir, map_name)
    os.makedirs(map_dir, exist_ok=True)

    web_export.sanitize_scene(use_selection=False, log=log)

    if use_draco:
        log(f"draco level {draco_level}, quantization "
            f"pos={draco_position_bits} nrm={DRACO_NORMAL_BITS} "
            f"col={DRACO_COLOR_BITS} gen={DRACO_GENERIC_BITS} bits")

    if clean_decals:
        from . import decals
        scene_meshes = [o for o in context.scene.objects if o.type == 'MESH']
        # Straight-up removal at export: the heuristic detector's hits PLUS
        # anything already quarantined (the '_Decals' collection or web_decal tag)
        # by the Clean/Separate step. Export is the definitive delete.
        hits = set(decals.find_decals(scene_meshes, decals.DEFAULT_FLATNESS_RATIO,
                                      decals.DEFAULT_NORMAL_TOL_DEG,
                                      decals.DEFAULT_MAX_FACES))
        quarantine = bpy.data.collections.get(decals.DECAL_COLLECTION)
        if quarantine:
            hits.update(o for o in quarantine.objects if o.type == 'MESH')
        hits.update(o for o in scene_meshes if o.get('web_decal'))
        dead_data = {o.data for o in hits if o.data}
        for o in hits:
            bpy.data.objects.remove(o, do_unlink=True)
        for me in dead_data:
            if me.users == 0:
                bpy.data.meshes.remove(me)
        log(f"removed {len(hits)} decal/flat-plane meshes before export")

    from . import ao_bake
    scene_meshes = [o for o in context.scene.objects if o.type == 'MESH']
    # Already baked via the panel's "Bake AO" step? Don't re-bake — just export
    # the existing web_ao vertex colours. (Avoids a slow double bake.)
    already_baked = any(
        o.data and o.data.color_attributes.get(ao_bake.AO_LAYER_NAME)
        for o in scene_meshes)
    if already_baked:
        log("existing baked AO found (web_ao) — exporting it, skipping re-bake")
        # Make web_ao the active/render color attribute so it exports as COLOR_0.
        for o in scene_meshes:
            ca = o.data.color_attributes if o.data else None
            if ca:
                idx = ca.find(ao_bake.AO_LAYER_NAME)
                if idx >= 0:
                    ca.active_color_index = idx
                    ca.render_color_index = idx
        bake_ao = True
    elif bake_ao:
        n_baked = ao_bake.bake_scene_ao(context, scene_meshes)
        log(f"AO baked into vertex colours on {n_baked} objects")
        bake_ao = n_baked > 0

    scene_objs = [o for o in context.scene.objects if o.type == 'MESH']

    # Floor partition FIRST — the playable measurement below reads per-floor
    # geometry. Three sources, in descending order of authority:
    #   1. explicit floor collections (Floors panel) — the user said so;
    #   2. `siege_floor` tags written at import from the engine's own Space
    #      (room) partition — the game's own answer, so it gets stairwells and
    #      mezzanines right;
    #   3. automatic world-Z banding — a guess, kept for untagged scenes.
    # `ordered` is a list of (label, order_index, [objs]) in every case.
    from . import floors as floors_mod
    manual = floors_mod.manual_floors(context.scene)
    if manual is not None:
        ordered = [(label, i, objs) for i, (label, objs) in enumerate(manual)]
        assigned = {o.name for _, _, objs in ordered for o in objs}
        stray = [o for o in scene_objs
                 if object_kind(o) and o.name not in assigned]
        if stray:
            log(f"{len(stray)} exportable mesh(es) in no floor collection - "
                f"skipped; assign them in the Floors panel")
    else:
        tagged = _tagged_floors(context.scene, scene_objs)
        if tagged is not None:
            ordered = tagged
            log(f"floors from Space tags: "
                + ", ".join(f"{lbl} ({len(objs)})" for lbl, _i, objs in ordered))
        else:
            buckets, bands = _assign_floor(scene_objs)
            total_floors = max(len(bands), 1)
            ordered = [(floor_label(fi, total_floors), fi, buckets[fi])
                       for fi in sorted(buckets.keys())]

    # Scale + bounds come from the PLAYABLE geometry (skybox/environment shell
    # excluded). Measuring the whole scene lets a huge shell force a 0.01 shrink
    # that dwarfs the building (the "map too small" bug); and the viewer frames
    # manifest.bounds, so excluding the shell keeps the camera on the map.
    playable = _playable_bounds(ordered, log=log)
    extent = (max(playable[1][i] - playable[0][i] for i in range(3))
              if playable else _scene_extent(scene_objs))
    scale, fog_near, fog_far = _auto_scale(extent)
    log(f"playable extent {extent:.0f} units -> scale {scale}, "
        f"fog {fog_near}..{fog_far}")

    # Bake the detected scale into the geometry so the GLB is native-unit and
    # the manifest carries no runtime scale (stable coords for 3D pins). No-op
    # at scale 1.0. Detect BEFORE baking; always restore (finally).
    saved_matrices = None
    if scale != 1.0:
        bakeable = [o for o in context.scene.objects
                    if o.type in {'MESH', 'EMPTY'}]
        saved_matrices = _bake_world_scale(bakeable, scale)
        context.view_layer.update()

    # Playable box -> Y-up manifest bounds, in the baked space (frames the map).
    bounds = _bounds_to_yup(playable[0], playable[1], scale) if playable else None

    # Room table first: the per-object extras index into it, so it has to exist
    # before anything is written.
    rooms = _rooms_to_yup(context.scene, scale)
    room_index = {r['id']: i for i, r in enumerate(rooms)}

    floors_manifest = []
    kinds_seen = set()
    n_files = 0

    if not split_by_kind:
        # --- one GLB per floor -------------------------------------------
        # Fewer requests and better Draco batching than a file per (floor,kind),
        # and nothing is lost: the kind rides on each node as `extras.k` instead
        # of being implied by the filename. Both indexes are resolved against
        # the manifest legends below.
        kind_names = sorted({k for _l, _o, objs in ordered for o in objs
                             for k in (object_kind(o),) if k})
        kind_index = {k: i for i, k in enumerate(kind_names)}
        kinds_seen.update(kind_names)
        try:
            for label, order, objs in ordered:
                vis = [o for o in objs if object_kind(o)]
                if not vis:
                    continue
                fname = f"{map_name}_{_safe_name(label)}.glb"
                staged = _stage_extras(vis, kind_index, room_index)
                try:
                    _select_only(vis)
                    ok = _export_part(os.path.join(map_dir, fname), use_draco,
                                      draco_level, export_colors=bake_ao,
                                      pos_bits=draco_position_bits)
                finally:
                    _restore_extras(staged)
                if ok:
                    floors_manifest.append({
                        "label": label, "order": order,
                        "parts": [{"file": fname, "kind": "mixed"}],
                        "objects": len(vis),
                    })
                    n_files += 1
                    log(f"  wrote {fname} ({len(vis)} objs, kinds in extras)")
        finally:
            if saved_matrices is not None:
                _restore_world(saved_matrices)
                context.view_layer.update()
                saved_matrices = None

    try:
        for label, order, objs in (ordered if split_by_kind else ()):
            if not objs:
                continue
            by_kind = {}
            for o in objs:
                k = object_kind(o)
                if k is None:           # non-visible category (sound/light/...)
                    continue
                by_kind.setdefault(k, []).append(o)
            if not by_kind:
                continue

            parts = []
            safe = _safe_name(label)
            for kind, kobjs in sorted(by_kind.items()):
                fname = f"{map_name}_{safe}_{kind}.glb"
                _select_only(kobjs)
                if _export_part(os.path.join(map_dir, fname), use_draco,
                                draco_level, export_colors=bake_ao,
                                pos_bits=draco_position_bits):
                    parts.append({"file": fname, "kind": kind})
                    kinds_seen.add(kind)
                    n_files += 1
                    log(f"  wrote {fname} ({len(kobjs)} objs)")
            if parts:
                floors_manifest.append({"label": label, "order": order, "parts": parts})
    finally:
        # Always restore the scene, even if an export step raised.
        if saved_matrices is not None:
            _restore_world(saved_matrices)
            context.view_layer.update()

    categories = {k: {"color": KIND_COLORS.get(k, '#808890'),
                      "label": KIND_LABELS.get(k, k)}
                  for k in sorted(kinds_seen)}

    manifest = {
        "name": map_name,
        "up": "Y",
        # Geometry is baked to native units above (skybox-safe scale detection),
        # so the viewer needs no runtime scale. (Detected factor was applied to
        # the GLBs when != 1, not carried here.)
        "scale": 1.0,
        "fog": {"color": "#0a0e16", "near": fog_near, "far": fog_far, "enabled": True},
        "background": {"top": "#10151f", "bottom": "#05070b"},
        "depth": {"nearColor": "#0b1018", "midColor": "#dfe7f2",
                  "farColor": "#0b1018", "midPos": 0.5, "tint": 0.4, "auto": True},
        # Baked AO is shaped live in the viewer shader (strength/contrast/farBoost),
        # no realtime pass. bakedAO=true tells the viewer COLOR_0 carries it.
        "ao": {"bakedAO": bool(bake_ao),
               "strength": 1.0, "contrast": 1.2, "farBoost": 0.6},
        "floors": floors_manifest,
        "categories": categories,
        # Legend for the per-node extras written by _stage_extras. A node's
        # extras.k indexes `kinds`; extras.r indexes the rooms table (-1 = none).
        "kinds": sorted(kinds_seen),
        "extras": {"kind": EXTRA_KIND, "room": EXTRA_ROOM},
    }
    # Room volumes from the engine's own Space partition: name, floor, and the
    # convex cells to test a point against. Written to a sibling file rather than
    # inlined — pretty-printed it is ~25x the rest of the manifest, and the
    # viewer needs the manifest to parse before first paint but only needs rooms
    # when it labels something. Absent on maps with no Spaces (and on v34, where
    # Spaces aren't recovered yet), so treat it as optional.
    if rooms:
        rooms_name = f"{map_name}_rooms.json"
        with open(os.path.join(map_dir, rooms_name), 'w', encoding='utf-8') as fh:
            json.dump(rooms, fh, separators=(',', ':'))
        n_cells = sum(len(r["cells"]) for r in rooms)
        n_approx = sum(1 for r in rooms if not r["exact"])
        n_compass = sum(1 for r in rooms if r["nameSource"] == 'compass')
        n_named = sum(1 for r in rooms if r["nameSource"] != 'generated')
        manifest["rooms"] = {
            "file": rooms_name,
            "count": len(rooms),
            "cells": n_cells,
            "named": n_named,
            "compassNamed": n_compass,
            # planes are [nx,ny,nz,d] quads; inside == dot(n,p) <= d for all
            "cellFormat": "planes4",
        }
        log(f"wrote {rooms_name} ({len(rooms)} rooms, {n_cells} convex cells, "
            f"{n_named} named ({n_compass} compass)"
            + (f", {n_approx} approximated" if n_approx else "") + ")")
    # Playable-area framing box (skybox excluded). The viewer frames + limits the
    # camera to this; without it a big environment shell makes the map tiny.
    if bounds is not None:
        manifest["bounds"] = bounds
        log(f"bounds {bounds['min']} .. {bounds['max']}")
    manifest_path = os.path.join(map_dir, "manifest.json")
    with open(manifest_path, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)

    _upsert_index(out_dir, map_name)
    return n_files, manifest_path


class SIEGE_OT_web_export_map(bpy.types.Operator):
    """Export the map as per-floor GLBs + manifest.json for the web viewer.

    One GLB per floor by default, each node carrying its entity kind and room as
    glTF extras. Enable "Split By Category" for the older per-(floor, kind) file
    layout. Splits the scene by floor (Space tags, else Z-banding),
    writes one GLB per part plus manifest.json and updates maps/index.json.
    Optionally bakes AO into vertex colours first. Run AFTER 'Clean Map for Web'.
    """
    bl_idname = "siege.web_export_map"
    bl_label = "Siege: Export Web Map (manifest)"
    bl_options = {'REGISTER'}

    # Folder picker: choose the viewer's maps/ directory.
    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

    map_name: bpy.props.StringProperty(
        name="Map name", default="Map",
        description="Folder + manifest name under the maps/ directory")
    use_draco: bpy.props.BoolProperty(name="Draco Compression", default=True)
    draco_level: bpy.props.IntProperty(name="Draco Level", default=6, min=0, max=10)
    draco_position_bits: bpy.props.IntProperty(
        name="Position Precision (bits)", default=DRACO_POSITION_BITS,
        min=8, max=16,
        description="Draco position quantization. 11 ~= 1.5cm on a 30m part — "
                    "invisible in the depth-shaded viewer. Lower = smaller files; "
                    "raise toward 14 only if you see faceting/warping")
    split_by_kind: bpy.props.BoolProperty(
        name="Split By Category",
        description=("Write one GLB per floor AND category instead of one per "
                     "floor. Off is smaller and fewer requests — the category "
                     "rides on each object as glTF extras instead of being "
                     "implied by the filename. Turn on only if the viewer needs "
                     "to fetch a single category on its own"),
        default=False,
    )

    bake_ao: bpy.props.BoolProperty(
        name="Bake AO to vertex colours", default=True,
        description="Fast geometric AO into COLOR_0 (cavity + grounding, no "
                    "Cycles; zero runtime cost in the viewer)")
    clean_decals: bpy.props.BoolProperty(
        name="Remove decals/flat planes", default=True,
        description="Strip flat single-facing meshes (decals, the stray centre "
                    "plane) before export")

    def invoke(self, context, event):
        if self.map_name == "Map":
            for c in bpy.data.collections:
                if c.children and not c.name.startswith('_'):
                    self.map_name = c.name
                    break
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            self.report({'ERROR'}, "No output maps/ directory")
            return {'CANCELLED'}
        try:
            n, mpath = export_map(
                context, bpy.path.abspath(self.directory), self.map_name,
                use_draco=self.use_draco, draco_level=self.draco_level,
                bake_ao=self.bake_ao, clean_decals=self.clean_decals,
                draco_position_bits=self.draco_position_bits,
                split_by_kind=self.split_by_kind,
                log=lambda m: print(f"[SiegeWeb] {m}"))
        except Exception as e:
            self.report({'ERROR'}, f"Web map export failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
        if n == 0:
            self.report({'WARNING'}, "Nothing exported (no visible geometry?)")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Exported {n} GLB parts + manifest -> {mpath}")
        return {'FINISHED'}


_classes = (SIEGE_OT_web_export_map,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
