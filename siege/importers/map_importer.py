"""
Siege map importer — operator + orchestrator.

This module hosts `import_siege_map`, the high-level function that pulls
every forge parser together to populate a Blender scene, plus the
`IMPORT_OT_siege_map` operator that exposes the function to the UI.

Option A organization: the orchestrator function and its inner closures
were intentionally left intact during the package split. The pure parsers
(forge.*, common.constants) are imported from their new homes, but the
mesh/material/object-building logic stays in this module because nearly
every helper closes over a shared dict of caches (CMO index, material
cache, texture cache, etc.). Decomposing those closures into separate
modules would be a meaningful regression risk for what is otherwise a
stable, well-tested function — defer it until a concrete need appears.
"""

import json
import math
import os
import struct
import sys

import bpy
import bmesh
import mathutils
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy_extras.io_utils import ImportHelper

from ..common import oodle_loader, zstd_loader
from ..common.constants import (
    CMO_FILE_TYPE,
    DDS_FOURCC_ATI2,
    DDS_FOURCC_DX10,
    DDS_FOURCC_DXT1,
    DDS_FOURCC_DXT5,
    DXGI_FORMAT_BC4_UNORM,
    DXGI_FORMAT_BC6H_UF16,
    DXGI_FORMAT_BC7_UNORM,
    ENTITY_TYPE_CATEGORIES,
    LOADUNIT_MAGIC,
    MATERIAL_MAGIC,
    TEXFORMAT_HAS_ALPHA,
)
from ..forge.cmo import parse_compiled_mesh_object
from ..forge.decompress import decompress_entry
from ..forge.entity import (
    find_entity_child_transforms,
    find_entity_mesh_links,
    find_light_definitions,
    parse_entities_from_data,
    scan_entity_components,
    scan_entity_light_refs,
    scan_entity_material_overrides,
    scan_entity_material_tints,
)
from ..forge.entity_names import (
    clean_space_name,
    find_map_file,
    load_entity_names,
)
from ..forge.room_names import (
    STRING_FIELD_MAGIC,
    link_room_names,
    prettify,
)
from ..forge.space import (
    NEAR_ROOM_RADIUS,
    build_lookup,
    derive_floors,
    roof_band,
    trim_ceilings,
    floor_for_point,
    parse_spaces,
    summarize,
)
from ..forge.file_io import (
    infer_file_type_from_name,
    read_forge_entries,
    read_forge_entry_cache,
    read_forge_version,
)
from ..forge.material import (
    _pair_scan,
    build_dds_file,
    build_texture_forge_index,
    parse_compiled_texture,
    parse_material_records,
)
from ..forge.mesh_records import (
    parse_dmg_entries_from_data,
    parse_mesh_entries_from_data,
)


# Hidden collection holding the Space (room) debug volumes.
SPACES_COLLECTION_NAME = "Spaces"

def _solid_mesh(mesh, solids):
    """Fill `mesh` with the convex hull of each cell in `solids`.

    Each cell is convex, so its hull IS the cell — exact for the boxes, wedges
    and prisms the decomposition actually uses, and watertight by construction.
    Hulling beats emitting AABBs: cells tile a room without overlapping, while
    their bounding boxes visibly overlap wherever a cell is not axis-aligned.

    Coplanar hull triangles are dissolved afterwards, so a box comes out as six
    quads rather than twelve triangles — far easier to read as an overlay, and
    it keeps the vertex count down on a 200-room map.
    """
    bm = bmesh.new()
    try:
        for pts in solids:
            if len(pts) < 4:
                continue
            sub = bmesh.new()
            try:
                for p in pts:
                    sub.verts.new(p)
                sub.verts.ensure_lookup_table()
                bmesh.ops.convex_hull(sub, input=sub.verts, use_existing_faces=False)
                # Drop anything the hull left outside the surface, else stray
                # interior verts survive as loose geometry.
                bmesh.ops.delete(
                    sub,
                    geom=[v for v in sub.verts if not v.link_faces],
                    context='VERTS')
                bmesh.ops.dissolve_limit(
                    sub, angle_limit=math.radians(1.0),
                    verts=list(sub.verts), edges=list(sub.edges))
                bmesh.ops.recalc_face_normals(sub, faces=list(sub.faces))
                tmp = bpy.data.meshes.new("_hull_tmp")
                sub.to_mesh(tmp)
                bm.from_mesh(tmp)
                bpy.data.meshes.remove(tmp)
            finally:
                sub.free()
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return len(mesh.polygons)


def _wire_geometry(edges):
    """`(verts, edges)` for the exact stored edge records of one Space.

    Not a bounding-box approximation — the edges ARE the stored geometry, so an
    L-shaped room draws as an L. They are a triangulated convex-cell soup rather
    than a closed surface, which is why this is the wireframe fallback and the
    boxes are the default.
    """
    verts, out, index = [], [], {}
    for a, b in edges:
        pair = []
        for p in (a, b):
            key = (round(p[0], 3), round(p[1], 3), round(p[2], 3))
            if key not in index:
                index[key] = len(verts)
                verts.append(p)
            pair.append(index[key])
        if pair[0] != pair[1]:
            out.append((pair[0], pair[1]))
    return verts, out


def _separate_floor_collections(map_collection, map_name, log=print):
    """Reorganise the map into `Floor_<label>` collections, category inside.

    Blender shows an object if **any** collection holding it is visible, so
    additive linking cannot hide a storey — the objects have to be moved. Each
    one lands in `<map>_Floor_<label>` / `<map>_Floor_<label>_<Category>`, which
    keeps the category split available inside every floor and makes hiding a
    storey a single checkbox in the outliner.

    Only floor-tagged objects move. The Space debug volumes stay in their own
    hidden collection — they are an overlay, not part of the model, and pulling
    them into a floor would make them appear whenever that floor is shown.
    """
    floors = {}

    def _sub(label, cat):
        top_name = f"{map_name}_Floor_{label}"
        top = floors.get(label)
        if top is None:
            top = bpy.data.collections.get(top_name)
            if top is None:
                top = bpy.data.collections.new(top_name)
            if top.name not in map_collection.children:
                try:
                    map_collection.children.link(top)
                except RuntimeError:
                    pass
            floors[label] = top
        sub_name = f"{top_name}_{cat.title().replace('_', '')}"
        sub = bpy.data.collections.get(sub_name)
        if sub is None:
            sub = bpy.data.collections.new(sub_name)
        if sub.name not in top.children:
            try:
                top.children.link(sub)
            except RuntimeError:
                pass
        return sub

    moved = 0
    for obj in list(bpy.data.objects):
        label = obj.get('siege_floor')
        if not isinstance(label, str) or not label:
            continue
        if obj.get('entity_category') == 'SPACE':
            continue
        target = _sub(label, str(obj.get('entity_category') or 'OTHER'))
        if target.name in {c.name for c in obj.users_collection}:
            continue
        for col in list(obj.users_collection):
            col.objects.unlink(obj)
        target.objects.link(obj)
        moved += 1

    # Category collections emptied by the move would otherwise litter the
    # outliner with dead nodes.
    for col in list(map_collection.children):
        if col.name.startswith(f"{map_name}_Floor_"):
            continue
        if not col.objects and not col.children:
            map_collection.children.unlink(col)
            bpy.data.collections.remove(col)

    if moved:
        log(f"[Siege] Floor separation: {moved} objects -> "
            + ", ".join(f"{lb} ({len(c.all_objects)})"
                        for lb, c in sorted(floors.items())))
    return moved, len(floors)


def _hull_planes(pts, ndigits=5):
    """Face planes of the convex hull of `pts`, as `[nx, ny, nz, d]`.

    Convention is `dot(n, p) <= d` for inside, with `n` outward and unit length.
    Coplanar hull triangles collapse to one plane (rounded then de-duplicated),
    so a box yields 6 planes rather than 12.
    """
    bm = bmesh.new()
    try:
        for p in pts:
            bm.verts.new(p)
        bm.verts.ensure_lookup_table()
        bmesh.ops.convex_hull(bm, input=bm.verts, use_existing_faces=False)
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces],
                         context='VERTS')
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        seen = {}
        for f in bm.faces:
            n = f.normal
            if n.length_squared < 1e-12:
                continue
            n = n.normalized()
            plane = (round(n.x, ndigits), round(n.y, ndigits), round(n.z, ndigits),
                     round(n.dot(f.verts[0].co), 4))
            seen[plane[:3]] = plane
        return [list(p) for p in seen.values()]
    finally:
        bm.free()


def _build_room_table(spaces, floors, space_floor, room_labels,
                      display_names=None, raw_names=None, compass_uids=None):
    """The room list the web manifest publishes, in Blender world coordinates.

    One entry per Space, carrying its display name, its floor, and its convex
    cells as half-space planes. Planes rather than boxes because a cell that is
    not axis-aligned has a bounding box noticeably larger than itself, and the
    point of this table is telling a viewer *exactly* which room a point is in.

    `name` is the engine's own room name when a Heated Metal EntityIDMap.bin
    supplied one, else a generated "2F Room 03" numbered per floor.
    `nameSource` says which, so a viewer can style a guessed label differently.
    `id` stays the raw `siege_room` tag, which is what the exported objects are
    tagged with — join on that, display `name`.
    """
    display_names = display_names or {}
    raw_names = raw_names or {}
    per_floor = {}
    rooms = []
    for suid in sorted(spaces, key=lambda u: (space_floor.get(u, 0),
                                              spaces[u]['aabb'][0],
                                              spaces[u]['aabb'][1])):
        sp = spaces[suid]
        idx = space_floor.get(suid, 0)
        label = floors[idx]['label'] if floors else 'F'
        n = per_floor[label] = per_floor.get(label, 0) + 1
        cells = [pl for pl in (_hull_planes(c) for c in (sp.get('solids') or ()))
                 if len(pl) >= 4]
        if not cells:
            continue
        engine = display_names.get(suid)
        rooms.append({
            'id': room_labels.get(suid, f"Room_{suid:016X}"),
            'name': engine or f"{label} Room {n:02d}",
            # 'compass' is the name the in-game compass shows, read from the map
            # forge; 'entity' is the level-designer entity name from an optional
            # Heated Metal table; 'generated' is ours.
            'nameSource': (('compass' if suid in (compass_uids or ()) else 'entity')
                           if engine else 'generated'),
            'engineName': raw_names.get(suid, ''),
            'floor': label,
            'floorIndex': idx,
            'uid': f"0x{suid:016X}",
            'exterior': bool(sp['exterior']),
            'exact': bool(sp.get('exact', True)),
            'aabb': [round(v, 3) for v in sp['aabb']],
            'cells': cells,
        })
    return rooms


def _build_space_volumes(parent_collection, spaces, floors, space_floor,
                         room_labels, style='SOLID', display_names=None,
                         raw_names=None):
    """Draw each Space (room) as one diagnostic object.

    `style` is 'SOLID' — the room's exact convex cells, hulled and merged into a
    single watertight mesh — or 'WIRE' for the raw stored edge records. Purely
    diagnostic either way: it lets you see the room and floor boundaries the
    tags came from. The collection is hidden by default and its objects carry
    the same siege_floor / siege_room tags, so the filter panel treats them like
    anything else.

    Rooms whose edge stream doesn't fully segment fall back to welded-component
    boxes and are tagged `siege_space_exact = False`, so an approximated room is
    identifiable in the viewport rather than silently wrong.
    """
    col = bpy.data.collections.get(SPACES_COLLECTION_NAME)
    if col is None:
        col = bpy.data.collections.new(SPACES_COLLECTION_NAME)
        parent_collection.children.link(col)

    display_names = display_names or {}
    raw_names = raw_names or {}
    made = 0
    inexact = 0
    for suid, sp in spaces.items():
        idx = space_floor.get(suid, 0)
        label = floors[idx]['label'] if floors else 'F'
        room = room_labels.get(suid, f"Room_{suid:016X}")
        # Object name prefers the engine's own room name — that is the whole
        # point of having it — but stays uniquely suffixed so two rooms sharing
        # a name don't collide into Blender's .001 renaming.
        pretty = display_names.get(suid)
        obj_name = (f"Space_{pretty} [{room}]" if pretty else f"Space_{room}")
        mesh = bpy.data.meshes.new(f"SpaceVol_{room}")

        if style == 'WIRE':
            verts, edges = _wire_geometry(sp['edges'])
            if not verts:
                bpy.data.meshes.remove(mesh)
                continue
            mesh.from_pydata(verts, edges, [])
            mesh.update()
        elif not _solid_mesh(mesh, sp.get('solids') or []):
            bpy.data.meshes.remove(mesh)
            continue
        if not sp.get('exact', True):
            inexact += 1

        obj = bpy.data.objects.new(obj_name, mesh)
        obj["uid"] = str(suid)
        obj["uid_hex"] = f"0x{suid:016X}"
        obj["entity_category"] = 'SPACE'
        obj["siege_floor"] = label
        obj["siege_floor_index"] = idx
        obj["siege_space"] = f"0x{suid:016X}"
        obj["siege_room"] = room
        obj["siege_room_name"] = pretty or f"{label} {room.rsplit('_', 1)[-1]}"
        obj["siege_space_name"] = raw_names.get(suid, '')
        obj["siege_room_named"] = bool(pretty)
        obj["siege_space_exterior"] = bool(sp['exterior'])
        obj["siege_space_exact"] = bool(sp.get('exact', True))
        obj["siege_space_cells"] = len(sp.get('solids') or [])
        obj.display_type = 'WIRE' if style == 'WIRE' else 'SOLID'
        obj.show_wire = style != 'WIRE'
        obj.hide_render = True
        col.objects.link(obj)
        made += 1

    # Hidden by default — it's a diagnostic overlay, not part of the model.
    col.hide_viewport = True
    for lc in _find_layer_collections(bpy.context.view_layer.layer_collection, col):
        lc.hide_viewport = True
    return made, inexact


def _find_layer_collections(root, target):
    """Every LayerCollection node pointing at `target` (for visibility toggles)."""
    out = []
    if root.collection is target:
        out.append(root)
    for child in root.children:
        out.extend(_find_layer_collections(child, target))
    return out


def build_cmo_index(forge_dir, progress_callback=None):
    """
    Build an index of CompiledMeshObject entries across all _mesh.forge files.
    Returns dict: cmo_uid -> (forge_path, entry).
    """
    # Only banks that actually hold CompiledMeshObjects. `'_mesh' in name` also
    # matched `*_meshshape.forge` (collision shapes, never CMOs) — harmless when
    # the FAT carried a real file_type, but on v31+ every entry in those banks
    # now costs a container parse + meta-block decode to resolve its uid, and the
    # meshshape banks are hundreds of MB. The filename is authoritative here.
    mesh_forges = []
    for f in os.listdir(forge_dir):
        if not f.lower().endswith('.forge'):
            continue
        path = os.path.join(forge_dir, f)
        if infer_file_type_from_name(path) == CMO_FILE_TYPE:
            mesh_forges.append(path)

    cmo_index = {}
    cache_hits = 0
    for idx, mf_path in enumerate(mesh_forges):
        if progress_callback:
            progress_callback(f"Indexing {os.path.basename(mf_path)}... ({idx+1}/{len(mesh_forges)})")
        try:
            # Prefer the Prism entry-summary cache for v31-v33 banks. Their FAT
            # gives a physical uid, but mesh records reference the StaticUid; the
            # legacy FAT reader also mis-reads the file_type (0) and can't decode
            # the File3 containers these banks use. The cache supplies the correct
            # StaticUid + offset + file_type, and '_file3' routes decompression
            # through the File3 path. v34 banks fall through to read_v34_entries,
            # which already carries the parsed asset block decompression needs.
            cached = read_forge_entry_cache(mf_path)
            if cached is not None and cached[1] < 34:
                for e in cached[0]:
                    if e['file_type'] == CMO_FILE_TYPE:
                        cmo_index.setdefault(e['uid'], (mf_path, e))
                cache_hits += 1
                continue
            mf_entries, _ = read_forge_entries(mf_path)
            for e in mf_entries:
                if e['file_type'] == CMO_FILE_TYPE:
                    cmo_index.setdefault(e['uid'], (mf_path, e))
        except Exception as e:
            print(f"[Siege][DIAG] Failed to index mesh forge {os.path.basename(mf_path)}: {e}")

    if cache_hits:
        print(f"[Siege] CMO index: used Prism entry-summary cache for {cache_hits} v31-v33 bank(s)")
    return cmo_index


# A joined compound may be at most this many times the extent of its own largest
# sub-mesh. Real compounds are assemblies of parts that overlap; anything past
# this is geometry that does not belong to the entity.
COMPOUND_SPAN_RATIO = 6.0
# ...and never gets gated below this absolute size, so small multi-part props
# (a chair, a light fixture) are always left alone.
COMPOUND_SPAN_FLOOR = 20.0


def _submesh_extent(mesh_result, lod_idx):
    """Largest XYZ extent of one sub-mesh's LOD geometry, or 0."""
    faces = mesh_result['lods_faces'].get(lod_idx) or []
    if not faces:
        return 0.0
    verts = mesh_result['vertices']
    used = {i for f in faces for i in f}
    pts = [verts[i] for i in used if i < len(verts)]
    if not pts:
        return 0.0
    return max(max(p[a] for p in pts) - min(p[a] for p in pts) for a in range(3))


def _reject_scattered_submeshes(submesh_plan, lod_idx):
    """Drop sub-meshes that sit far outside the compound's own cluster.

    Keeps the plan unchanged when the union is compact, which is the normal
    case. Returns the same list object when nothing was dropped so the caller
    can tell cheaply.
    """
    if len(submesh_plan) < 2:
        return submesh_plan
    boxes = []
    for mesh_uid, cmo_uid, mesh_result in submesh_plan:
        faces = mesh_result['lods_faces'].get(lod_idx) or []
        if not faces:
            continue
        verts = mesh_result['vertices']
        used = {i for f in faces for i in f}
        pts = [verts[i] for i in used if i < len(verts)]
        if not pts:
            continue
        lo = [min(p[a] for p in pts) for a in range(3)]
        hi = [max(p[a] for p in pts) for a in range(3)]
        boxes.append(((mesh_uid, cmo_uid, mesh_result), lo, hi))
    if len(boxes) < 2:
        return submesh_plan

    union_span = max(max(b[2][a] for b in boxes) - min(b[1][a] for b in boxes)
                     for a in range(3))
    biggest = max(max(hi[a] - lo[a] for a in range(3)) for _e, lo, hi in boxes)
    limit = max(biggest * COMPOUND_SPAN_RATIO, COMPOUND_SPAN_FLOOR)
    if union_span <= limit:
        return submesh_plan

    # Keep the cluster around the primary sub-mesh (the first, non-MOSD one).
    ref_lo, ref_hi = boxes[0][1], boxes[0][2]
    ref_c = [(ref_lo[a] + ref_hi[a]) * 0.5 for a in range(3)]
    kept = []
    for entry, lo, hi in boxes:
        c = [(lo[a] + hi[a]) * 0.5 for a in range(3)]
        if max(abs(c[a] - ref_c[a]) for a in range(3)) <= limit:
            kept.append(entry)
    return kept or [boxes[0][0]]


# A decal is a visual-only entity whose geometry is a flat sheet: paint, grime,
# posters, bullet holes. `is_decal_like` alone (visual, no collision/destruction)
# is far too broad — plenty of real props are visual-only — so flatness is what
# actually distinguishes one, and flatness is only knowable once the mesh exists.
# That is why decals are classified AFTER the build rather than in the entity
# type filter, which runs before any geometry is read.
DECAL_THICKNESS = 0.06      # metres; thinner than this in one axis is a sheet
DECAL_MAX_ASPECT = 0.05     # ...and thin RELATIVE to its own width


def _is_flat_sheet(obj):
    """True when the object's geometry is a flat sheet (a decal), not a solid."""
    me = obj.data
    if not me or len(me.vertices) < 3 or len(me.polygons) < 1:
        return False
    dims = sorted(obj.dimensions)
    if dims[2] <= 1e-6:
        return False
    return dims[0] <= DECAL_THICKNESS or (dims[0] / dims[2]) <= DECAL_MAX_ASPECT


# Local bounding box per MESH DATABLOCK, cached. Map meshes are heavily shared
# between instances, so computing this once per datablock rather than once per
# object keeps floor tagging off the hot path even on a 250k-vert mesh.
_LOCAL_BBOX_CACHE = {}


def _local_bbox(me):
    """`(minx,miny,minz,maxx,maxy,maxz)` in mesh-local space, or None."""
    if me is None:
        return None
    key = me.name_full
    bb = _LOCAL_BBOX_CACHE.get(key)
    if bb is None:
        n = len(me.vertices)
        if not n:
            return None
        co = [0.0] * (3 * n)
        me.vertices.foreach_get('co', co)
        xs, ys, zs = co[0::3], co[1::3], co[2::3]
        bb = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
        _LOCAL_BBOX_CACHE[key] = bb
    return bb


def import_siege_map(context, filepath, import_meshes=True, import_empties=True,
                     max_entities=0, empty_size=0.3, lod_level='AUTO',
                     import_materials=True, import_textures=False,
                     texture_cache_dir=None, texture_quality='ULTRA',
                     type_filter=None, import_space_volumes=True,
                     space_volume_style='SOLID', ceiling_trim=0.15,
                     near_room_radius=NEAR_ROOM_RADIUS,
                     separate_floors=False, entity_name_map='',
                     max_object_span=0.0):
    """
    Main import function. Reads a .forge map file and creates Blender objects.

    - Deduplicates entities by UID across LoadUnits (first occurrence wins)
    - Shares Blender mesh data across entities that reference the same CMO
    - Skips entities already present in the blend file (by UID custom property)
    - Joins compound entities (multiple sub-meshes) into a single object per
      entity, with one Blender material slot per referenced material UID

    lod_level:
        'AUTO'         : highest-detail LOD present (LOD0)
        '0'..'3'       : specific LOD, fall back to highest available
        'ALL'          : import every LOD as separate child objects

    import_materials:
        When True, creates a Blender Material per unique Mesh-record material
        UID and assigns face material indices according to each island's
        mat_id.

    import_textures:
        When True (requires import_materials), walks the
        Material -> TextureMapSpec -> TextureMap chain inside each LoadUnit,
        extracts the highest-resolution compiled texture available across the
        forge dir's *_textures*.forge files, decodes it to a DDS file in
        texture_cache_dir, and wires it into the Blender material's
        Principled BSDF shader graph (Base Color / Normal / Specular slots).

    texture_cache_dir:
        Directory to write extracted .dds files to. Defaults to a
        "siege_textures" folder next to the imported .forge.
    """
    if zstd_loader.zstd is None:
        raise ImportError("zstandard module not available")

    forge_dir = os.path.dirname(filepath)
    map_name = os.path.splitext(os.path.basename(filepath))[0]

    print(f"[Siege] Parsing forge: {filepath}")

    # Y9S2+ (forge v31 and up, which includes the v32 "Siege X" builds and v34
    # Y11S2) Oodle-compress their asset chunks. Check BEFORE parsing: v31-v33
    # entry resolution has to decode each container's meta block to recover the
    # uid/file_type, so without the DLL every entry silently resolves to
    # file_type 0 and the map imports as an empty scene instead of reporting why.
    version = read_forge_version(filepath)
    if version >= 31 and not oodle_loader.is_available():
        raise RuntimeError(
            f"This map is forge v{version}, which uses Oodle compression. "
            "The Oodle library (oo2core_8_win64.dll) was not found. Set the "
            "OODLE2_8_PATH environment variable to its path, or place it at "
            "C:\\oodle\\oo2core_8_win64.dll, then retry.")

    # Parse forge entries
    entries, version = read_forge_entries(filepath)

    # Y11S2 (v34) re-hashed the map magics, encrypted the FAT, and switched to
    # Oodle. The v34 map parse is isolated in forge.v34_map and fills the same
    # entity/mesh dicts the Blender-build phase below consumes; the v33 LoadUnit
    # filter finds nothing on v34 (its LoadUnit magic differs), so we route
    # around Phases 1-2 and run the v34 parser just before the type filter.
    is_v34 = version >= 34
    if is_v34:
        lu_entries = []
        print(f"[Siege] v34 (Y11S2) map detected — using v34 reader/parser (version {version})")
    else:
        lu_entries = [e for e in entries if e['file_type'] == LOADUNIT_MAGIC]
        print(f"[Siege] Found {len(lu_entries)} LoadUnit entries (version {version})")

    # Find or create collection
    collection = None
    for col in bpy.data.collections:
        if col.name == map_name:
            collection = col
            break
    if collection is None:
        collection = bpy.data.collections.new(map_name)
        context.scene.collection.children.link(collection)

    # Per-category child collections, created on demand. Each entity type
    # category gets its own sub-collection under the main map collection.
    # Floor groups still get their own dedicated collection as before.
    CATEGORY_COLLECTION_NAMES = {
        'VISUAL':           f"{map_name}_Visual",
        'NON_DESTRUCTIBLE': f"{map_name}_NonDestructible",
        'DESTRUCTIBLE':     f"{map_name}_Destructible",
        'REINFORCEMENT':    f"{map_name}_Reinforcement",
        'SOUND':            f"{map_name}_Sound",
        'CAMERA':           f"{map_name}_Camera",
        'LIGHT':            f"{map_name}_Light",
        'SPAWN':            f"{map_name}_Spawn",
        'OBJECTIVE':        f"{map_name}_Objective",
        'INTERACTIVE':      f"{map_name}_Interactive",
        'RUNOUT_BLOCKER':   f"{map_name}_RunoutBlockers",
        'OTHER':            f"{map_name}_Other",
        'FLOOR_GROUP':      f"{map_name}_FloorGroups",
        'DECAL':            f"{map_name}_Decals",
        # Empties split out of the mesh-expected categories below: entities that
        # reference a mesh whose CMO isn't installed go to MissingGeometry (they
        # SHOULD be meshes); entities with no mesh reference at all (runtime-
        # instanced clusters, bare transforms) go to NonMesh (confirmed meshless).
        'MISSING_GEOMETRY': f"{map_name}_MissingGeometry",
        'NON_MESH':         f"{map_name}_NonMesh",
    }
    # Categories that are expected to carry geometry. When an entity in one of
    # these ends up as an empty, we reroute it to NonMesh / MissingGeometry so the
    # mesh collections stay clean and the floating empties are corralled. Marker
    # categories (LIGHT, CAMERA, SPAWN, OBJECTIVE, INTERACTIVE, RUNOUT_BLOCKER,
    # SOUND, FLOOR_GROUP) are legitimately meshless and keep their group.
    MESH_EXPECTED_CATEGORIES = {'VISUAL', 'NON_DESTRUCTIBLE', 'DESTRUCTIBLE',
                                'REINFORCEMENT', 'OTHER'}
    category_collections = {}  # category_key -> bpy.types.Collection

    def _get_category_collection(cat_key):
        """Get or create the child collection for a category."""
        if cat_key in category_collections:
            return category_collections[cat_key]
        col_name = CATEGORY_COLLECTION_NAMES.get(cat_key, f"{map_name}_{cat_key}")
        col = bpy.data.collections.get(col_name)
        if col is None:
            col = bpy.data.collections.new(col_name)
            collection.children.link(col)
        elif col.name not in collection.children:
            try:
                collection.children.link(col)
            except RuntimeError:
                pass  # already linked
        category_collections[cat_key] = col
        return col

    def _classify_category(components):
        """Classify an entity's components into a collection category key."""
        # Destructible check MUST come before floor_group: Y5S4 Oregon soft
        # walls (and Border's equivalents) carry BOTH `destruction` and
        # `floor_group` tags — they mark the floor partition volume, but
        # they're functionally breakable and belong with destructibles.
        if components.intersection({'destruction', 'deco_destruction',
                                     'pilot_destructible', 'rigidbody'}):
            return 'DESTRUCTIBLE'
        if 'floor_group' in components:
            return 'FLOOR_GROUP'
        if components.intersection({'bomb_objective', 'bomb_marker', 'counter_defuser'}):
            return 'OBJECTIVE'
        if components.intersection({'reinforcement'}):
            return 'REINFORCEMENT'
        # Cameras (in-game security CCTVs + the default `camera_tool`
        # observation cameras) are mesh props in their own right — keep them
        # out of LIGHT so the light volumes below stay clean and selectable.
        if components.intersection({'security_camera', 'camera_ui', 'camera_tool'}):
            return 'CAMERA'
        # Light entities (sky/spot/omni light volumes the engine bakes lighting
        # into). Route every light-tagged entity here — including meshed lamp
        # fixtures — so the Light collection holds all of them in one place.
        if components.intersection({'spot_light', 'omni_light', 'sky_light'}):
            return 'LIGHT'
        # Interactive gadget props (ammo refills, biohazard containers,
        # hostages, window barricades, etc.) — carry a gameplay gadget
        # component that distinguishes them from pure spawn/trigger volumes.
        if components.intersection({'interactive_gadget', 'interactive_window',
                                     'interactive_rope'}):
            return 'INTERACTIVE'
        # Runout blockers: plain trigger volume with no interactive gadget
        # or objective attached. Shows up as inert+trigger+visual on Oregon.
        if 'trigger' in components and not components.intersection(
                {'trigger_event', 'space', 'interactive_gadget',
                 'interactive_window', 'interactive_rope'}):
            return 'RUNOUT_BLOCKER'
        if components.intersection({'trigger_event', 'space'}):
            return 'SPAWN'
        if components.intersection({'sound', 'sound_bank'}) and 'visual' not in components:
            return 'SOUND'
        if 'inert' in components:
            return 'NON_DESTRUCTIBLE'
        if 'visual' in components:
            return 'VISUAL'
        return 'OTHER'

    # Build set of entity UIDs already in the blend file (for skip-on-reimport)
    existing_uids = set()
    for obj in bpy.data.objects:
        uid_hex = obj.get("uid_hex")
        if uid_hex:
            existing_uids.add(uid_hex)
    if existing_uids:
        print(f"[Siege] Found {len(existing_uids)} existing entities in blend file, will skip duplicates")

    # Phase 0: Build CMO index up front. Older forges (v<=29) have a
    # different inner Mesh body layout, so the deterministic cmo_uid offset
    # used for v>=30 misses. parse_mesh_entries_from_data uses the index
    # keys to validate the parsed cmo_uid and trigger a fallback scan.
    cmo_index = None
    cmo_uid_set = None
    if import_meshes:
        print("[Siege] Indexing mesh forge files...")
        cmo_index = build_cmo_index(forge_dir, lambda msg: print(f"[Siege] {msg}"))
        cmo_uid_set = set(cmo_index.keys())
        print(f"[Siege] CMO index: {len(cmo_index)} entries")

    # Phase 1: Process all LoadUnits, collect entities and mesh data
    # Deduplicate entities by UID (first occurrence wins)
    unique_entities = {}  # uid -> entity dict
    all_mesh_to_cmo = {}
    all_mesh_to_materials = {}  # mesh_uid -> list[material_uid]
    all_mesh_uids = set()  # global set across all LUs
    all_light_uids = {}  # light-definition UID -> kind ('spot_light'/'omni_light'/'sky_light')
    all_dmg_to_mesh = {}  # DMG UID -> Mesh UID (destructible resolution table)
    # material_uid -> {tex_type_name: [file_uid, ...]} aggregated across LUs
    all_material_to_textures = {}
    all_material_selector_counts = {}  # material_uid -> int (non-null selector count)
    all_material_uids_set = set()  # every Material UID found via pair scan
    lu_data_list = []  # (lu_entry, data, entities) for phase 2

    # We only need to walk Material/Spec/Map records when the user actually
    # asked for textures. Materials without textures still get a placeholder
    # via _get_or_create_material below.
    want_material_chain = import_materials and import_textures

    for lu_idx, lu_entry in enumerate(lu_entries):
        print(f"[Siege] Processing LoadUnit {lu_idx+1}/{len(lu_entries)}...")
        data = decompress_entry(filepath, lu_entry)
        if data is None:
            continue

        # Parse entities
        entities = parse_entities_from_data(data)

        # Deduplicate: keep first occurrence of each UID
        new_entities = []
        for ent in entities:
            if ent['uid'] not in unique_entities:
                unique_entities[ent['uid']] = ent
                new_entities.append(ent)

        # Collect light-definition UIDs from this LoadUnit's lighting block.
        # Lights aren't entities; placement entities reference these UIDs, which
        # Phase 2b uses to tag those entities as LIGHT (see scan_entity_light_refs).
        all_light_uids.update(find_light_definitions(data))

        if import_meshes:
            # Parse mesh entries (Mesh UID -> CMO UID) and material UID lists
            mesh_to_cmo, mesh_to_materials = parse_mesh_entries_from_data(
                data, cmo_uid_set=cmo_uid_set, version=version)
            all_mesh_to_cmo.update(mesh_to_cmo)
            all_mesh_to_materials.update(mesh_to_materials)
            all_mesh_uids.update(mesh_to_cmo.keys())

            # Parse DestructionMeshGroup records (DMG UID -> Mesh UID).
            # Destructibles reference DMG UIDs in their payload, not Mesh UIDs.
            dmg_to_mesh = parse_dmg_entries_from_data(data)
            all_dmg_to_mesh.update(dmg_to_mesh)

        if want_material_chain:
            # Collect every Material UID present in this LU (needed for
            # the entity material override scanner to validate candidates).
            for _, mu in _pair_scan(data, MATERIAL_MAGIC):
                all_material_uids_set.add(mu)

            # Material -> TextureMapSpec -> TextureMap -> file UIDs.
            # First-occurrence wins across LUs (materials are usually unique
            # per LU but the same UID can show up in shared LU sets).
            try:
                lu_mats, lu_sel_counts = parse_material_records(data)
            except Exception as e:
                print(f"[Siege][TEX] Material parse failed in LU{lu_idx}: {e}")
                lu_mats, lu_sel_counts = {}, {}
            for mu, type_map in lu_mats.items():
                if mu not in all_material_to_textures:
                    all_material_to_textures[mu] = type_map
            for mu, count in lu_sel_counts.items():
                if mu not in all_material_selector_counts:
                    all_material_selector_counts[mu] = count

        lu_data_list.append((lu_entry, data, new_entities))

    print(f"[Siege] Unique entities: {len(unique_entities)}")
    if import_meshes:
        print(f"[Siege] Mesh records:        {len(all_mesh_to_cmo)}")
        print(f"[Siege] DMG (destructible) records: {len(all_dmg_to_mesh)}")

    # Phase 2: Link entities to meshes using global mesh UID set + DMG UID set
    all_entity_to_mesh = {}
    all_entity_to_submeshes = {}
    all_entity_mesh_offsets = {}  # entity_uid -> absolute data offset of mesh UID
    all_entity_mat_overrides = {}  # entity_uid -> {orig_mat: override_mat}
    all_entity_mat_tints = {}  # entity_uid -> [(R,G,B,A), ...] per material slot
    if import_meshes:
        all_dmg_uids = set(all_dmg_to_mesh.keys())
        for lu_entry, data, entities in lu_data_list:
            if not entities:
                continue
            entity_to_mesh, entity_to_submeshes, entity_mesh_offsets = \
                find_entity_mesh_links(
                    data, entities, all_mesh_uids, all_dmg_uids
                )
            all_entity_to_mesh.update(entity_to_mesh)
            all_entity_to_submeshes.update(entity_to_submeshes)
            all_entity_mesh_offsets.update(entity_mesh_offsets)

            # Scan for per-entity material overrides. Entity payloads can
            # carry [original_mat][override_mat] pairs before the visual
            # component marker that replace the Mesh record's defaults.
            # Pass dmg_to_mesh so the scanner can resolve DMG UIDs to real
            # Mesh UIDs — entity_to_mesh still contains DMG UIDs at this
            # point (DMG resolution happens after this loop).
            if want_material_chain:
                ent_overrides = scan_entity_material_overrides(
                    data, entities, entity_to_mesh, entity_mesh_offsets,
                    all_mesh_to_materials, all_material_uids_set,
                    all_dmg_to_mesh,
                )
                all_entity_mat_overrides.update(ent_overrides)

            # Scan for per-entity material tint colors. Tint data lives in
            # entity payloads and doesn't depend on the texture chain, so
            # run whenever materials are enabled (not just when textures are).
            if import_materials:
                raw_tints = scan_entity_material_tints(data, entities)
                for ent_uid, mat_tint_map in raw_tints.items():
                    all_entity_mat_tints[ent_uid] = mat_tint_map

        # Resolve DMG refs -> real Mesh UIDs so downstream code only sees
        # Mesh UIDs. Count how many links came through the destructible path
        # for the diagnostic summary.
        destructible_links = 0
        for ent_uid, link_uid in list(all_entity_to_mesh.items()):
            if link_uid in all_dmg_to_mesh:
                all_entity_to_mesh[ent_uid] = all_dmg_to_mesh[link_uid]
                destructible_links += 1

        print(f"[Siege] Entities with mesh links: {len(all_entity_to_mesh)}"
              f" (via DMG: {destructible_links})")

        # Phase 2c: Scan each parent payload for inline child-transform
        # records. These are distinct-mesh props embedded inside a grouping
        # parent (trailers, excavator parts, ceiling light strings,
        # bathroom tiles, door footers, ...). Each child has its own
        # post-matrix payload that carries its OWN Mesh/DMG UID — we must
        # resolve that per-child rather than inheriting the parent's mesh,
        # because the parent is often a soft-wall/floor-group container
        # whose mesh is unrelated to the actual prop geometry.
        child_link_set = all_mesh_uids | set(all_dmg_to_mesh.keys())
        child_recovery_count = 0
        child_mesh_resolved = 0
        for lu_entry, data, entities in lu_data_list:
            if not entities:
                continue
            children = find_entity_child_transforms(data, entities, child_link_set)
            for child in children:
                cuid = child['uid']
                puid = child['parent_uid']
                if cuid in unique_entities:
                    continue  # already known as a real top-level entity
                unique_entities[cuid] = {
                    'uid': cuid,
                    'matrix_raw': child['matrix_raw'],
                    'payload_start': child['payload_start'],
                    'parent_uid': puid,
                    'is_child_transform': True,
                }
                # Prefer the child's own mesh UID extracted from its tail.
                # Resolve DMG -> Mesh on the spot so the downstream builder
                # sees a real Mesh UID.
                mesh_uid = child.get('mesh_uid')
                if mesh_uid is not None:
                    if mesh_uid in all_dmg_to_mesh:
                        mesh_uid = all_dmg_to_mesh[mesh_uid]
                    all_entity_to_mesh[cuid] = mesh_uid
                    child_mesh_resolved += 1
                child_recovery_count += 1
        if child_recovery_count:
            print(f"[Siege] Inline child-transform recovery: "
                  f"{child_recovery_count} (own-mesh resolved: "
                  f"{child_mesh_resolved})")
        if all_entity_mat_overrides:
            print(f"[Siege] Entities with material overrides: "
                  f"{len(all_entity_mat_overrides)}")

        # Resolve raw tint dicts {mat_uid: (R,G,B,A)} into ordered lists
        # matching each entity's material slot order. Material overrides
        # are applied first so the tint lookup uses the final mat UIDs.
        if all_entity_mat_tints:
            resolved_tints = {}
            for ent_uid, mat_tint_map in all_entity_mat_tints.items():
                mesh_uid = all_entity_to_mesh.get(ent_uid)
                if not mesh_uid:
                    continue
                mat_uids = all_mesh_to_materials.get(mesh_uid, [])
                # Apply material overrides so tint lookup matches final mats
                ent_overrides = all_entity_mat_overrides.get(ent_uid, {})
                final_mats = [ent_overrides.get(m, m) for m in mat_uids]
                tint_list = []
                has_any = False
                for mu in final_mats:
                    tint = mat_tint_map.get(mu)
                    tint_list.append(tint)
                    if tint is not None:
                        has_any = True
                if has_any:
                    resolved_tints[ent_uid] = tint_list
            all_entity_mat_tints = resolved_tints
            print(f"[Siege] Entities with material tints: "
                  f"{len(all_entity_mat_tints)}")

        print(f"[Siege] Compound entities with sub-meshes: {len(all_entity_to_submeshes)}")

    # Phase 2b: Scan each entity payload for component-type magics. This
    # classifies entities (visual-only vs destructible vs floor-group etc.)
    # and feeds both the per-object custom properties and the FloorWallGroups
    # collection routing below.
    all_entity_components = {}
    for lu_entry, data, entities in lu_data_list:
        if not entities:
            continue
        all_entity_components.update(scan_entity_components(data, entities))

    # Light placement: lights are standalone definition records (no transform),
    # referenced by an entity that supplies the transform. We only route the
    # *pure* light-volume entities into LIGHT — a bare transform that references a
    # light and has no renderable component. The vast majority of light
    # references sit on emissive props / lamp fixtures (they carry `visual` and/or
    # `inert`, often with material colour overrides) and reference a light only
    # for their emission colour; those must stay in their own mesh categories, not
    # pollute LIGHT. So skip any entity that has a visual/inert/destruction
    # component and only tag the components-less light volumes.
    _RENDERABLE = {'visual', 'inert', 'destruction', 'deco_destruction',
                   'pilot_destructible', 'rigidbody'}
    if all_light_uids:
        light_marked = 0
        skipped_emissive = 0
        for lu_entry, data, entities in lu_data_list:
            if not entities:
                continue
            for euid, lname in scan_entity_light_refs(
                    data, entities, all_light_uids).items():
                if all_entity_components.get(euid, set()) & _RENDERABLE:
                    skipped_emissive += 1
                    continue
                all_entity_components.setdefault(euid, set()).add(lname)
                light_marked += 1
        print(f"[Siege] Light definitions: {len(all_light_uids)}; "
              f"pure light volumes tagged LIGHT: {light_marked} "
              f"(skipped {skipped_emissive} emissive/fixture references)")

    # Inline child-transform entities inherit their parent's component set.
    # The child's 77-byte inline record (uid + magic + unk1 + matrix) doesn't
    # carry its own component magics, so classification would otherwise land
    # them in OTHER and their mesh would be suppressed by the type filter.
    for ent in unique_entities.values():
        if not ent.get('is_child_transform'):
            continue
        puid = ent.get('parent_uid')
        if puid in all_entity_components:
            all_entity_components[ent['uid']] = set(all_entity_components[puid])

    # Summary counters by component profile — useful for sanity checking
    # the scale pipeline against the in-game ground truth (see BorderEntList).
    comp_counts = {}
    for comps in all_entity_components.values():
        for name in comps:
            comp_counts[name] = comp_counts.get(name, 0) + 1
    if comp_counts:
        print(f"[Siege] Components detected: " + ", ".join(
            f"{k}={v}" for k, v in sorted(comp_counts.items())))

    # Archetype detection. In Y3S1, thousands of entities are tag-based
    # spawn placeholders that share an "archetype UID" stored at p2+0x64.
    # Their own payload has no components or mesh ref — the engine resolves
    # behavior at runtime via the WorldDivisionToTagLoadUnitLookup table
    # (magic 0xFD4E0B46) inside the World blob. We detect archetypes by
    # frequency: any u64 that appears at the +0x64 slot of >=50 distinct
    # entities is treated as an archetype, and its children are tagged so
    # the diagnostic CSV can group them.
    entity_archetype_parent = {}  # entity_uid -> archetype_uid
    archetype_freq = {}  # archetype_uid -> child count
    ARCHETYPE_FREQ_THRESHOLD = 50
    parent_at_64_freq = {}
    ent_p64 = {}

    def _is_plausible_archetype_uid(v):
        # A real Anvil asset/entity UID always has nonzero low bytes. On v34
        # the +0x64 slot frequently holds structural constants (0x0,
        # 0x100000000, 0x0000003F80000000 (= 1.0f << ...), 0x02985BC519000000,
        # ...), all of which have their low 24 bits zero. Filtering those out
        # stops the frequency heuristic from inventing bogus archetypes and
        # mislabeling thousands of meshless entities as archetype_child.
        return v != 0 and (v & 0xFFFFFF) != 0

    for lu_entry, data, entities in lu_data_list:
        if not entities:
            continue
        for ent in entities:
            p2 = ent['payload_start']
            if p2 + 0x64 + 8 > len(data):
                continue
            v = struct.unpack_from('<Q', data, p2 + 0x64)[0]
            if not _is_plausible_archetype_uid(v):
                continue
            parent_at_64_freq[v] = parent_at_64_freq.get(v, 0) + 1
            ent_p64[ent['uid']] = v
    for v, n in parent_at_64_freq.items():
        if n >= ARCHETYPE_FREQ_THRESHOLD:
            archetype_freq[v] = n
    for uid, v in ent_p64.items():
        if v in archetype_freq:
            entity_archetype_parent[uid] = v
    if archetype_freq:
        print(f"[Siege] Detected {len(archetype_freq)} runtime-archetype tag(s) "
              f"with {sum(archetype_freq.values())} child entities total:")
        for arch, n in sorted(archetype_freq.items(), key=lambda kv: -kv[1]):
            print(f"[Siege]   archetype 0x{arch:016X}: {n} children")

    # --- Spaces: the engine's own room partition, and the floor table ---
    # A Siege map is divided into Spaces (rooms) joined by SpacePortals; that is
    # what the spectator/replay floor view and the callout system run on. Each
    # room's floor slab (min Z of its volume) is the authoritative storey height,
    # which is why this beats banding per-object Z: a stairwell lands on the
    # floor it starts from, not halfway between two. Validated at 100% against
    # the Heated Metal space names on Y5S4 Oregon — see forge/space.py.
    #
    # Purely additive: every object gets siege_floor / siege_space tags and
    # nothing is moved. If a map has no Spaces the table is empty and the web
    # exporter falls back to its Z-banding as before.
    all_spaces = {}
    for lu_entry, data, entities in lu_data_list:
        if not entities:
            continue
        try:
            for suid, sp in parse_spaces(data, entities).items():
                all_spaces.setdefault(suid, sp)
        except Exception as ex:
            print(f"[Siege] Space parse failed on LoadUnit "
                  f"0x{lu_entry['uid']:016X}: {ex}")
    space_floors, space_floor_index = derive_floors(all_spaces)
    # Drop the roof slab out of each room BEFORE the containment index is built,
    # so a ceiling is tagged with the storey it is the floor of rather than the
    # room underneath it. Derivation above already used zmin, which the trim
    # leaves alone.
    if ceiling_trim:
        trim_ceilings(all_spaces, ceiling_trim)
    space_lookup = build_lookup(all_spaces)
    # --- Room names -------------------------------------------------------
    # Primary source: the map forge itself. It carries the COMPASS callout
    # names ("1F_Kitchen", "B_BlueBunker") in a string table, each tied to a
    # SpaceComponent id — see forge/room_names.py. Only rooms the compass
    # announces are named, which is exactly the set worth labelling.
    space_names = {}
    compass_uids = set()
    if all_spaces:
        try:
            tables = []
            for entry in entries:
                if entry['file_type'] == LOADUNIT_MAGIC:
                    continue
                try:
                    d = decompress_entry(filepath, entry)
                except Exception:
                    d = None
                if d and struct.pack('<I', STRING_FIELD_MAGIC) in d:
                    tables.append(d)
            if tables:
                space_names = link_room_names(
                    tables,
                    [(d, ents) for _lu, d, ents in lu_data_list],
                    set(all_spaces))
                print(f"[Siege] Compass room names: {len(space_names)}/"
                      f"{len(all_spaces)} spaces named from the forge")
                compass_uids = set(space_names)
        except Exception as ex:
            print(f"[Siege] Compass room-name recovery failed: {ex}")

    # Secondary source: a Heated Metal EntityIDMap.bin, which carries the
    # level-designer ENTITY names. Siege ships none of those, so it is optional
    # and only fills rooms the compass table didn't name.
    name_map_path = entity_name_map or find_map_file(filepath)
    if name_map_path and len(space_names) < len(all_spaces):
        try:
            table = load_entity_names(name_map_path)
            extra = {u: table[u] for u in all_spaces
                     if u in table and u not in space_names}
            space_names.update(extra)
            if extra:
                print(f"[Siege] Entity names: +{len(extra)} more from "
                      f"{os.path.basename(name_map_path)} "
                      f"({len(space_names)}/{len(all_spaces)} total)")
        except Exception as ex:
            print(f"[Siege] Entity name map unreadable ({name_map_path}): {ex}")

    # Stable, ordered room label per space. `space_room_label` stays generated
    # and unique because it is the join key everything downstream uses
    # (`siege_room` tags, the web manifest); the engine name rides alongside it
    # as a display name, since it is neither guaranteed present nor unique.
    space_room_label = {}
    space_display_name = {}
    for n, suid in enumerate(sorted(
            all_spaces,
            key=lambda u: (space_floor_index.get(u, 0),
                           all_spaces[u]['aabb'][0], all_spaces[u]['aabb'][1]))):
        fl = space_floors[space_floor_index[suid]]['label'] if space_floors else 'F'
        space_room_label[suid] = f"{fl}_Room_{n:03d}"
        # clean_space_name strips a designer prefix ("PVP02_Oregon_V2_Space_"),
        # a no-op on a compass name; prettify then splits CamelCase, so both
        # sources land on the same shape ("1F Meeting Hall").
        pretty = prettify(clean_space_name(space_names.get(suid, '')))
        if pretty:
            space_display_name[suid] = pretty
    # Object tagging uses an EXTENDED table with a synthetic Roof band, so the
    # top storey's ceiling can be hidden on its own. Space labels stay on the
    # unextended table — a room must never become 'Roof'.
    tag_floors = roof_band(space_floors) if space_floors else []
    if space_floors:
        print(f"[Siege] Spaces: {len(all_spaces)} rooms -> "
              f"{len(space_floors)} floors: {', '.join(summarize(space_floors, all_spaces))}")
    elif is_v34:
        # v34 re-hashes component type magics every patch, so the v33 Space
        # magics match nothing. Recovering them needs the same structural
        # self-harvest used for the v34 visual magic — not wired yet.
        print("[Siege] Spaces: not supported on v34 (component magics "
              "re-hash per patch) — web export falls back to Z-banding")
    else:
        print("[Siege] No Space volumes found — floor tagging skipped "
              "(web export falls back to Z-banding)")

    floor_tag_counts = {}
    # How each object got its storey: contained in a room, snapped to a nearby
    # room, or placed by slab height alone. The last bucket is the one with no
    # room evidence behind it, so it is worth surfacing.
    floor_source_counts = {}

    def _tag_floor(obj, matrix_raw, mat=None):
        """Tag one object with the storey (and room) it sits in.

        Non-destructive by design: these are custom properties only. The
        Floor/Room filter panel selects on them, so the user stays free to put
        objects in whatever collections they like.
        """
        if not space_floors:
            return
        pos = (matrix_raw[12], matrix_raw[13], matrix_raw[14])
        # A prop rests ON its floor, so the bottom of its geometry decides the
        # storey, not its origin — an entity origin can sit anywhere in the
        # mesh, and for a wall it is often at the top.
        # A prop rests ON its floor, so the bottom of its geometry decides the
        # storey, not its origin — an entity origin can sit anywhere in the mesh,
        # and for a wall it is often at the top.
        #
        # The world bottom is computed from the entity matrix directly, NOT from
        # obj.matrix_world. Blender evaluates matrix_world lazily through the
        # depsgraph, so right after the object is built it is still identity no
        # matter where the call sits relative to _apply_transform — reading it
        # bands every object by its mesh-local height, which collapses the whole
        # scene onto one storey.
        base_z = pos[2]
        if mat is not None and obj.type == 'MESH':
            bb = _local_bbox(obj.data)
            if bb is not None:
                base_z = min(
                    (mat @ mathutils.Vector((x, y, z))).z
                    for x in (bb[0], bb[3])
                    for y in (bb[1], bb[4])
                    for z in (bb[2], bb[5]))
        idx, suid = floor_for_point(tag_floors, space_lookup,
                                    space_floor_index, pos, base_z=base_z,
                                    near_radius=near_room_radius,
                                    stats=floor_source_counts)
        label = tag_floors[idx]['label']
        obj["siege_floor"] = label
        obj["siege_floor_index"] = idx
        if suid is not None:
            obj["siege_space"] = f"0x{suid:016X}"
            obj["siege_room"] = space_room_label.get(suid, "")
            # Display name alongside the join key, so selecting a prop in the
            # viewport tells you which room it is in without a lookup.
            pretty = space_display_name.get(suid)
            if pretty:
                obj["siege_room_name"] = pretty
        floor_tag_counts[label] = floor_tag_counts.get(label, 0) + 1

    # v34 (Y11S2) map parse. Fills the same entity/mesh dicts the v33 phases
    # above would have produced, so the type filter, caches, and Blender-build
    # phase below run unchanged. mesh->CMO is resolved from the LoadUnit resource
    # (render-instance) records (the meshUid@-8 / cmoUid@magic+21 rule) which
    # reproduce the depgraph oracle's real-CMO set exactly; every entity-linked
    # prop resolves to real geometry. Static building geometry with no entity
    # reference is placed by the LoadUnit internal-handle scene graph (not yet
    # reconstructed). Component classification, materials, tints, submeshes, and
    # DMG resolution are not yet wired for v34 — entities without a resolvable
    # mesh become empties. See research/y11s2_map_layout.md in the Prism repo.
    if is_v34:
        from ..forge.v34_map import parse_v34_map
        v34 = parse_v34_map(filepath, entries, cmo_uid_set,
                            progress=lambda m: print(f"[Siege] {m}"))
        unique_entities = v34['unique_entities']
        all_mesh_to_cmo = v34['mesh_to_cmo']
        all_entity_to_mesh = v34['entity_to_mesh']
        all_mesh_uids = v34['mesh_uids']
        # v34 entity component classification. The v33 scan_entity_components
        # finds nothing on v34 (component magics re-hashed); parse_v34_map
        # recovers them from the inline component objects. Presence of the
        # VISUAL component marks a geometry prop; its absence marks a "logic"
        # node (vending/slot machine, trigger, sound emitter).
        all_entity_components = v34.get('entity_components', {})
        v34_visual = sum(1 for c in all_entity_components.values() if 'visual' in c)
        print(f"[Siege] v34 components: {v34_visual} visual entities, "
              f"{len(unique_entities) - v34_visual} logic/other")
        resource_records = v34.get('resource_records', [])
        resource_cmos = set(r['cmo_uid'] for r in resource_records)
        resolvable = sum(1 for m in all_entity_to_mesh.values()
                         if m in all_mesh_to_cmo)
        print(f"[Siege] v34 map: {len(unique_entities)} entities, "
              f"{len(all_entity_to_mesh)} mesh-linked, {resolvable} with geometry, "
              f"{len(all_mesh_to_cmo)}/{len(all_mesh_uids)} meshes->CMO resolved")
        print(f"[Siege] v34 resource records: {len(resource_records)} "
              f"({len(resource_cmos)} distinct bank CMOs = the LoadUnits' real "
              f"geometry set)")

    # Apply entity type filter. When type_filter is not None, only entities
    # that match at least one enabled category pass through. An entity with
    # no detected components is included only when VISUAL is enabled (it
    # likely has a visual component we didn't detect, or is a bare transform).
    def _entity_passes_type_filter(uid):
        if type_filter is None:
            return True
        components = all_entity_components.get(uid, set())
        if not components:
            # No components detected — include if VISUAL is enabled
            return 'VISUAL' in type_filter
        for cat_key in type_filter:
            cat = ENTITY_TYPE_CATEGORIES[cat_key]
            if cat['include'] & components:
                if not cat['exclude'] or not (cat['exclude'] & components):
                    return True
        return False

    if type_filter is not None:
        pre_filter_count = len(unique_entities)
        unique_entities = {uid: ent for uid, ent in unique_entities.items()
                          if _entity_passes_type_filter(uid)}
        print(f"[Siege] Type filter: {pre_filter_count} -> {len(unique_entities)} entities "
              f"(enabled: {', '.join(sorted(type_filter))})")

    # Apply max_entities limit
    entity_list = list(unique_entities.values())
    if max_entities > 0:
        entity_list = entity_list[:max_entities]

    # CMO index was built in Phase 0; just report the unique-CMO count here
    # for visibility.
    if import_meshes and cmo_index is not None and (all_entity_to_mesh or all_entity_to_submeshes):
        unique_cmos = set()
        for ent_uid, mesh_uid in all_entity_to_mesh.items():
            if mesh_uid in all_mesh_to_cmo:
                unique_cmos.add(all_mesh_to_cmo[mesh_uid])
        for ent_uid, submesh_list in all_entity_to_submeshes.items():
            for mesh_uid in submesh_list:
                if mesh_uid in all_mesh_to_cmo:
                    unique_cmos.add(all_mesh_to_cmo[mesh_uid])
        print(f"[Siege] Unique CMOs to load: {len(unique_cmos)}")

    # Phase 3b: Build texture forge index when textures are requested.
    # The cache directory defaults to "<map_basename>_textures" next to the
    # imported .forge so reimports reuse already-decoded DDS files.
    texture_index = None
    tex_cache_dir = None
    if want_material_chain:
        print("[Siege] Indexing texture forge files...")
        tex_quality_rank = {'LOW': 0, 'MED': 1, 'HIGH': 2, 'ULTRA': 3}.get(
            texture_quality, 3)
        texture_index = build_texture_forge_index(
            forge_dir, lambda msg: print(f"[Siege] {msg}"),
            max_rank=tex_quality_rank)
        print(f"[Siege] Texture index: {len(texture_index)} entries")
        tex_cache_dir = (texture_cache_dir or
                         os.path.join(forge_dir, f"{map_name}_textures"))
        os.makedirs(tex_cache_dir, exist_ok=True)
        print(f"[Siege] Texture cache: {tex_cache_dir}")

    # Blender mesh cache. Keyed by (cmo_uid, lod_idx, mat_sig) so that two
    # entities pointing at the same CMO but different material lists still
    # share data when the material list matches. mat_sig is a tuple of the
    # material UIDs in mat_id order (or () when materials are disabled).
    # We do NOT repopulate the cache from bpy.data.meshes on reimport because
    # the mat_sig isn't recoverable from the mesh name alone; instead, the
    # existing-uid skip check below handles reimport dedup at the object level.
    blender_mesh_cache = {}  # (cmo_uid, lod_idx, mat_sig) -> bpy.types.Mesh

    # Parallel cache for compound (joined-submesh) entities. Two entities with
    # the same ordered submesh UIDs + materials + tints produce byte-identical
    # joined geometry, so they can share a single datablock as linked dupes.
    # Key: (submesh_uids_tuple, lod_idx, mat_override_sig, tint_sig).
    joined_mesh_cache = {}

    # Cache of Blender Material datablocks, keyed by (material_uid, tint_tuple|None).
    # Reuses existing SiegeMat_<hex> materials across reimports.
    material_cache = {}  # (material_uid, tint_tuple|None) -> bpy.types.Material
    if import_materials:
        for bmat in bpy.data.materials:
            if not bmat.name.startswith("SiegeMat_"):
                continue
            try:
                # Parse name: SiegeMat_<hex> or SiegeMat_<hex>_T<RRGGBB>
                suffix = bmat.name[len("SiegeMat_"):]
                if '_T' in suffix:
                    hex_part, _ = suffix.split('_T', 1)
                    mat_uid = int(hex_part, 16)
                    tint_prop = bmat.get("tint_color")
                    tint_key = tuple(tint_prop) if tint_prop else None
                    material_cache[(mat_uid, tint_key)] = bmat
                else:
                    material_cache[(int(suffix, 16), None)] = bmat
            except (ValueError, TypeError):
                pass
        if material_cache:
            print(f"[Siege] Reusing {len(material_cache)} existing Siege material datablocks")

    # Cache of Blender Image datablocks keyed by texture UID. We dedupe
    # across materials so the same diffuse used by 50 walls only loads once.
    image_cache = {}  # texture_uid -> bpy.types.Image
    tex_format_cache = {}  # texture_uid -> tex_format (int pixel format code)
    tex_extract_stats = {'extracted': 0, 'cached': 0, 'missing': 0, 'failed': 0}

    def _extract_texture_to_cache(tex_uid):
        """
        Resolve a compiled-texture UID to a .dds file on disk and return a
        loaded bpy.types.Image. Returns None on any failure (silently — the
        caller treats missing textures as a non-fatal hole).
        """
        if texture_index is None or tex_cache_dir is None:
            return None
        cached = image_cache.get(tex_uid)
        if cached is not None:
            return cached

        entry_tuple = texture_index.get(tex_uid)
        if entry_tuple is None:
            tex_extract_stats['missing'] += 1
            return None
        forge_path, entry, _rank = entry_tuple

        out_path = os.path.join(tex_cache_dir, f"{tex_uid:016X}.dds")
        if not os.path.exists(out_path):
            try:
                blob = decompress_entry(forge_path, entry)
                if not blob:
                    tex_extract_stats['failed'] += 1
                    return None
                info = parse_compiled_texture(blob)
                if not info:
                    tex_extract_stats['failed'] += 1
                    return None
                tex_format_cache[tex_uid] = info['tex_format']
                surface = blob[info['dds_offset']:info['dds_offset'] + info['dds_size']]
                dds = build_dds_file(info, surface)
                if not dds:
                    tex_extract_stats['failed'] += 1
                    return None
                with open(out_path, 'wb') as fout:
                    fout.write(dds)
                tex_extract_stats['extracted'] += 1
            except Exception as e:
                print(f"[Siege][TEX] Extract failed for 0x{tex_uid:X}: {e}")
                tex_extract_stats['failed'] += 1
                return None
        else:
            tex_extract_stats['cached'] += 1
            # Read DDS header to determine pixel format for cached files.
            if tex_uid not in tex_format_cache:
                try:
                    with open(out_path, 'rb') as fin:
                        fin.seek(84)  # FOURCC offset in DDS header
                        fourcc = fin.read(4)
                        if fourcc == DDS_FOURCC_DXT1:
                            tex_format_cache[tex_uid] = 0x02  # BC1
                        elif fourcc == DDS_FOURCC_DXT5:
                            tex_format_cache[tex_uid] = 0x05  # BC3
                        elif fourcc == DDS_FOURCC_ATI2:
                            tex_format_cache[tex_uid] = 0x06  # BC5
                        elif fourcc == DDS_FOURCC_DX10:
                            dxgi_fmt = struct.unpack('<I', fin.read(4))[0]
                            if dxgi_fmt == DXGI_FORMAT_BC4_UNORM:
                                tex_format_cache[tex_uid] = 0x0E
                            elif dxgi_fmt == DXGI_FORMAT_BC6H_UF16:
                                tex_format_cache[tex_uid] = 0x0F
                            elif dxgi_fmt == DXGI_FORMAT_BC7_UNORM:
                                tex_format_cache[tex_uid] = 0x10
                except Exception:
                    pass

        # Load (or fetch existing) into a Blender image datablock.
        try:
            img = bpy.data.images.load(out_path, check_existing=True)
            img["texture_uid"] = str(tex_uid)
            img["texture_uid_hex"] = f"0x{tex_uid:016X}"
        except Exception as e:
            print(f"[Siege][TEX] Image load failed for {out_path}: {e}")
            return None
        image_cache[tex_uid] = img
        return img

    def _get_or_create_material(mat_uid, tint_color=None):
        """Return a Blender Material for the given Mesh-record material UID.

        When `import_textures` is enabled, walks the texture chain for this
        material and attaches the loaded Blender Image datablocks as
        custom properties on the material:
            mat["tex_diffuse_image"]    -> bpy.types.Image
            mat["tex_normal_image"]     -> bpy.types.Image
            mat["tex_specular_image"]   -> bpy.types.Image
            mat["tex_misc_image"]       -> bpy.types.Image
            mat["tex_colormask_image"]  -> bpy.types.Image
        Plus the source UID hex strings under matching `tex_<type>_uid` keys
        so the user can reconstruct the chain after import without rerunning.
        Automatically sets up a basic Principled BSDF shader graph.

        tint_color: optional (R, G, B, A) tuple with 0-255 integer values.
            When provided, the tint is multiplied onto the diffuse (with
            colormask as an intermediary when available). A non-None tint
            produces a separate Blender material even for the same mat_uid,
            so different entities can have different tints.

        Alpha transparency is automatically enabled when the diffuse texture
        uses a pixel format with a meaningful alpha channel (BC3, BC7, etc.).
        """
        # Cache key includes tint so the same mat_uid with different tints
        # produces separate Blender materials.
        cache_key = (mat_uid, tuple(tint_color) if tint_color else None)
        bmat = material_cache.get(cache_key)
        if bmat is not None:
            return bmat

        if tint_color:
            name = f"SiegeMat_{mat_uid:016X}_T{tint_color[0]:02X}{tint_color[1]:02X}{tint_color[2]:02X}"
        else:
            name = f"SiegeMat_{mat_uid:016X}"
        bmat = bpy.data.materials.new(name=name)
        bmat["material_uid"] = str(mat_uid)
        bmat["material_uid_hex"] = f"0x{mat_uid:016X}"
        if tint_color:
            bmat["tint_color"] = list(tint_color)
        material_cache[cache_key] = bmat

        if want_material_chain:
            type_map = all_material_to_textures.get(mat_uid)
            if type_map:
                loaded_images = {}
                loaded_uids = {}  # type_name -> texture UID
                # Pick the first texture UID per type that resolves to a real
                # file. The texture index already chose the highest-resolution
                # variant per UID family, so we just take whatever the
                # material's TextureMap pointed to first.
                for type_name, uid_list in type_map.items():
                    chosen_img = None
                    chosen_uid = None
                    for u in uid_list:
                        img = _extract_texture_to_cache(u)
                        if img is not None:
                            chosen_img = img
                            chosen_uid = u
                            break
                    if chosen_img is not None:
                        bmat[f"tex_{type_name}_image"] = chosen_img
                        bmat[f"tex_{type_name}_uid"] = f"0x{chosen_uid:016X}"
                        loaded_images[type_name] = chosen_img
                        loaded_uids[type_name] = chosen_uid

                # Wire up Principled BSDF
                if loaded_images:
                    bmat.use_nodes = True
                    nodes = bmat.node_tree.nodes
                    links = bmat.node_tree.links

                    nodes.clear()

                    node_output = nodes.new(type='ShaderNodeOutputMaterial')
                    node_output.location = (300, 0)

                    node_bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
                    node_bsdf.location = (0, 0)
                    links.new(node_bsdf.outputs['BSDF'], node_output.inputs['Surface'])

                    # --- Base Color pipeline ---
                    # With colormask: diffuse * (colormask * tint_color)
                    # With tint but no colormask: diffuse * tint_color
                    # Plain: diffuse straight to Base Color
                    has_colormask = 'colormask' in loaded_images
                    has_diffuse = 'diffuse' in loaded_images

                    # Diffuse texture node (always created if present)
                    node_diff = None
                    if has_diffuse:
                        node_diff = nodes.new(type='ShaderNodeTexImage')
                        node_diff.image = loaded_images['diffuse']
                        node_diff.label = 'Diffuse'
                        node_diff.location = (-600, 300)

                    if has_colormask:
                        # Colormask texture (grayscale)
                        node_cmask = nodes.new(type='ShaderNodeTexImage')
                        node_cmask.image = loaded_images['colormask']
                        node_cmask.label = 'ColorMask'
                        node_cmask.location = (-900, 50)
                        if node_cmask.image.colorspace_settings.name != 'Non-Color':
                            node_cmask.image.colorspace_settings.name = 'Non-Color'

                        # Multiply colormask by tint color
                        node_tint_mul = nodes.new(type='ShaderNodeMixRGB')
                        node_tint_mul.blend_type = 'MULTIPLY'
                        node_tint_mul.label = 'Tint * ColorMask'
                        node_tint_mul.location = (-600, 50)
                        node_tint_mul.inputs['Fac'].default_value = 1.0
                        links.new(node_cmask.outputs['Color'], node_tint_mul.inputs['Color1'])
                        if tint_color:
                            node_tint_mul.inputs['Color2'].default_value = (
                                tint_color[0] / 255.0,
                                tint_color[1] / 255.0,
                                tint_color[2] / 255.0,
                                1.0,
                            )
                        else:
                            node_tint_mul.inputs['Color2'].default_value = (1.0, 1.0, 1.0, 1.0)

                        if has_diffuse:
                            # Multiply diffuse by tinted colormask
                            node_final_mul = nodes.new(type='ShaderNodeMixRGB')
                            node_final_mul.blend_type = 'MULTIPLY'
                            node_final_mul.label = 'Diffuse * Tint'
                            node_final_mul.location = (-300, 250)
                            node_final_mul.inputs['Fac'].default_value = 1.0
                            links.new(node_diff.outputs['Color'], node_final_mul.inputs['Color1'])
                            links.new(node_tint_mul.outputs['Color'], node_final_mul.inputs['Color2'])
                            links.new(node_final_mul.outputs['Color'], node_bsdf.inputs['Base Color'])
                        else:
                            # No diffuse — tinted colormask IS the base color
                            links.new(node_tint_mul.outputs['Color'], node_bsdf.inputs['Base Color'])
                    elif tint_color and has_diffuse:
                        # No colormask but we have a tint — multiply diffuse
                        # directly by the tint color.
                        node_tint_mul = nodes.new(type='ShaderNodeMixRGB')
                        node_tint_mul.blend_type = 'MULTIPLY'
                        node_tint_mul.label = 'Diffuse * Tint'
                        node_tint_mul.location = (-300, 250)
                        node_tint_mul.inputs['Fac'].default_value = 1.0
                        links.new(node_diff.outputs['Color'], node_tint_mul.inputs['Color1'])
                        node_tint_mul.inputs['Color2'].default_value = (
                            tint_color[0] / 255.0,
                            tint_color[1] / 255.0,
                            tint_color[2] / 255.0,
                            1.0,
                        )
                        links.new(node_tint_mul.outputs['Color'], node_bsdf.inputs['Base Color'])
                    elif has_diffuse:
                        # No colormask, no tint — wire diffuse straight
                        node_diff.location = (-300, 250)
                        links.new(node_diff.outputs['Color'], node_bsdf.inputs['Base Color'])

                    # --- Alpha / Transparency ---
                    # Two conditions must be met for alpha to be wired:
                    # 1. The diffuse texture's pixel format carries a meaningful
                    #    alpha channel (BC3, BC7, BC1A, BC2, RGBA8888).
                    # 2. The material has fewer than 6 texture selectors.
                    #    Materials with 6+ selectors are multi-layer blends
                    #    (dual diffuse + PBR sets blended by a mix/mask); their
                    #    diffuse alpha is used for layer blending, not transparency.
                    #    This mirrors the r6s-nr-tools heuristic where configs
                    #    with 2-5 textures wire alpha but 6-7 do not.
                    # blend_method/shadow_method exist in EEVEE (Blender 3.x-4.1);
                    # Blender 4.2+ (EEVEE Next) removed them — alpha works via
                    # the node graph alone.
                    diffuse_uid = loaded_uids.get('diffuse')
                    diffuse_fmt = tex_format_cache.get(diffuse_uid) if diffuse_uid else None
                    diffuse_has_alpha = diffuse_fmt in TEXFORMAT_HAS_ALPHA
                    mat_sel_count = all_material_selector_counts.get(mat_uid, 0)
                    use_alpha = diffuse_has_alpha and mat_sel_count < 6
                    if use_alpha and has_diffuse and node_diff is not None:
                        links.new(node_diff.outputs['Alpha'], node_bsdf.inputs['Alpha'])
                        if hasattr(bmat, 'blend_method'):
                            bmat.blend_method = 'HASHED'
                        if hasattr(bmat, 'shadow_method'):
                            bmat.shadow_method = 'HASHED'

                    # Specular
                    if 'specular' in loaded_images:
                        node_spec = nodes.new(type='ShaderNodeTexImage')
                        node_spec.image = loaded_images['specular']
                        node_spec.label = 'Specular'
                        node_spec.location = (-300, -50)
                        if node_spec.image.colorspace_settings.name != 'Non-Color':
                            node_spec.image.colorspace_settings.name = 'Non-Color'
                        links.new(node_spec.outputs['Color'], node_bsdf.inputs['Specular IOR Level'])

                    # Normal
                    if 'normal' in loaded_images:
                        node_norm_tex = nodes.new(type='ShaderNodeTexImage')
                        node_norm_tex.image = loaded_images['normal']
                        node_norm_tex.label = 'Normal'
                        node_norm_tex.location = (-600, -300)
                        if node_norm_tex.image.colorspace_settings.name != 'Non-Color':
                            node_norm_tex.image.colorspace_settings.name = 'Non-Color'

                        node_norm_map = nodes.new(type='ShaderNodeNormalMap')
                        node_norm_map.location = (-300, -300)

                        links.new(node_norm_tex.outputs['Color'], node_norm_map.inputs['Color'])
                        links.new(node_norm_map.outputs['Normal'], node_bsdf.inputs['Normal'])

        elif tint_color:
            # No texture chain, but we have a tint — set the Principled BSDF
            # Base Color to the tint so the material is visually distinguishable.
            bmat.use_nodes = True
            node_bsdf = bmat.node_tree.nodes.get('Principled BSDF')
            if node_bsdf:
                node_bsdf.inputs['Base Color'].default_value = (
                    tint_color[0] / 255.0,
                    tint_color[1] / 255.0,
                    tint_color[2] / 255.0,
                    1.0,
                )

        return bmat

    # Phase 4: Create Blender objects
    imported_count = 0
    mesh_count = 0
    empty_count = 0
    skipped_count = 0
    submesh_count = 0
    # Lists so the nested builder can mutate them without a `nonlocal`.
    decal_count = [0]
    decal_skipped = [0]
    oversized_skipped = [0]
    decals_enabled = type_filter is None or 'DECAL' in type_filter

    # Diagnostic failure counters
    diag_no_entity_link = 0
    diag_no_mesh_to_cmo = 0
    diag_no_cmo_in_index = 0
    diag_cmo_decompress_fail = 0
    diag_cmo_parse_fail = 0
    diag_cmo_no_geo = 0
    diag_cmo_previously_failed = 0
    failed_cmo_set = set()  # track unique CMOs that failed
    # Per-CMO failure reason (for diagnostic CSV).
    cmo_failure_reason = {}  # cmo_uid -> short reason string
    # Per-entity failure rows: (entity_uid, reason, mesh_uid, cmo_uid, components)
    # Captured for every entity that ends up as an empty (or gets skipped
    # entirely when import_empties is False).
    entity_failures = []
    # Per-category audit rows for non-mesh entities (lights, spawns,
    # objectives, sounds, interactives, etc.) so the user can see what
    # ended up in each category and grep for individual UIDs.
    # Schema: (entity_uid, category, components, has_mesh, mesh_uid_or_none)
    entity_audit = []

    # Determine which LOD indices the user wants per mesh. 'ALL' is resolved
    # against the CMO's actual num_lods; 'AUTO' and numeric choices are
    # resolved per-CMO with fallback to the highest detail available.
    want_all_lods = (lod_level == 'ALL')
    want_lod_idx = None
    if lod_level not in ('AUTO', 'ALL'):
        try:
            want_lod_idx = int(lod_level)
        except ValueError:
            want_lod_idx = None

    # Scale profile counters: track scale statistics separately for visual-only
    # vs destructible entities so we can verify the matrix carries per-instance
    # scale for decals/props that have no collision or destruction component.
    # The in-game QVM dump (BorderEntList.txt) shows decals as Visual-only
    # entities — they must keep the scale encoded in the entity matrix even
    # though collision-capable objects can't be scaled at runtime.
    scale_profile = {
        'visual_only': {'count': 0, 'nonuniform': 0, 'min': None, 'max': None},
        'destructible': {'count': 0, 'nonuniform': 0, 'min': None, 'max': None},
        'inert_only':   {'count': 0, 'nonuniform': 0, 'min': None, 'max': None},
        'other':        {'count': 0, 'nonuniform': 0, 'min': None, 'max': None},
    }

    def _classify(components):
        if 'destruction' in components or 'rigidbody' in components \
                or 'deco_destruction' in components or 'pilot_destructible' in components:
            return 'destructible'
        if 'inert' in components:
            return 'inert_only'
        if 'visual' in components and len(components - {'visual', 'sound'}) == 0:
            return 'visual_only'
        return 'other'

    def _record_scale(bucket, sx, sy, sz):
        s = scale_profile[bucket]
        s['count'] += 1
        lo, hi = min(sx, sy, sz), max(sx, sy, sz)
        if abs(hi - lo) > 1e-4:
            s['nonuniform'] += 1
        s['min'] = lo if s['min'] is None else min(s['min'], lo)
        s['max'] = hi if s['max'] is None else max(s['max'], hi)

    def _apply_transform(obj, mat):
        """
        Apply an entity's transform as explicit location / rotation / scale
        instead of `obj.matrix_world = mat`. Decomposing up front guarantees
        Blender records scale as a first-class channel, which matters for
        visual-only instances (decals, props without collision) that inherit
        their size purely from the entity matrix. Setting matrix_world on an
        unlinked object can be fragile depending on when parent evaluation
        kicks in; explicit TRS avoids that entirely.
        """
        loc, rot, scale = mat.decompose()
        obj.location = loc
        obj.rotation_mode = 'QUATERNION'
        obj.rotation_quaternion = rot
        obj.scale = scale
        return scale

    # Per-CMO cache of the parsed result (vertices + per-LOD face sets) so we
    # only parse each CMO once when producing multiple LOD meshes for it.
    parsed_cmo_cache = {}  # cmo_uid -> parse_compiled_mesh_object() result

    def _parse_cmo(cmo_uid):
        if cmo_uid in parsed_cmo_cache:
            return parsed_cmo_cache[cmo_uid]
        if cmo_uid in failed_cmo_set:
            return None
        if not cmo_index or cmo_uid not in cmo_index:
            cmo_failure_reason[cmo_uid] = 'cmo_not_in_index'
            return None
        forge_path, cmo_entry = cmo_index[cmo_uid]
        cmo_data = decompress_entry(forge_path, cmo_entry)
        if not cmo_data:
            failed_cmo_set.add(cmo_uid)
            cmo_failure_reason[cmo_uid] = 'cmo_decompress_failed'
            return None
        mesh_result = parse_compiled_mesh_object(cmo_data)
        if not mesh_result or not mesh_result['vertices'] or not mesh_result.get('lods_faces'):
            failed_cmo_set.add(cmo_uid)
            cmo_failure_reason[cmo_uid] = 'cmo_parse_returned_none' if not mesh_result else 'cmo_no_verts_or_no_lods'
            return None
        # Reject CMOs with no faces in ANY LOD
        if not any(mesh_result['lods_faces'].get(i) for i in range(mesh_result['num_lods'])):
            failed_cmo_set.add(cmo_uid)
            cmo_failure_reason[cmo_uid] = 'cmo_no_faces_in_any_lod'
            return None
        parsed_cmo_cache[cmo_uid] = mesh_result
        return mesh_result

    def _apply_materials_to_mesh(bl_mesh, mat_uids, face_local_mat_ids,
                                tint_colors=None):
        """
        Attach the given material UID list as Blender material slots on
        bl_mesh and write face.material_index for each polygon.

        mat_uids: list of material UIDs (ordered by mat_id). Duplicates are
                  kept in place so island mat_ids map 1:1 to slot indices.
        face_local_mat_ids: parallel list with one entry per polygon in
                            bl_mesh.polygons, giving the mat_id to assign.
        tint_colors: optional list parallel to mat_uids; each element is
                     either an (R,G,B,A) tuple (0-255) or None.
        """
        if not import_materials or not mat_uids:
            return
        for i, mat_uid in enumerate(mat_uids):
            tint = tint_colors[i] if tint_colors and i < len(tint_colors) else None
            bl_mesh.materials.append(_get_or_create_material(mat_uid, tint_color=tint))
        num_slots = len(bl_mesh.materials)
        if num_slots == 0:
            return
        # Clamp mat_ids into the slot range — a malformed island mat_id
        # would otherwise crash the assignment.
        for poly, mid in zip(bl_mesh.polygons, face_local_mat_ids):
            poly.material_index = mid if 0 <= mid < num_slots else 0

    def _apply_mat_overrides(mat_uids, mat_override_map):
        """Return a new material UID list with overrides applied.

        mat_override_map: {original_mat_uid: override_mat_uid} from the
        entity payload scanner. Each entry in mat_uids that appears as a
        key is replaced by the corresponding override value.
        """
        if not mat_override_map:
            return mat_uids
        return [mat_override_map.get(m, m) for m in mat_uids]

    def _build_bl_mesh(cmo_uid, lod_idx, mesh_result, mesh_uid,
                       mat_override_map=None, tint_colors=None):
        mat_uids = all_mesh_to_materials.get(mesh_uid, []) if import_materials else []
        mat_uids = _apply_mat_overrides(mat_uids, mat_override_map)
        mat_sig = tuple(mat_uids)
        tint_sig = tuple(tuple(t) if t else None for t in tint_colors) if tint_colors else ()
        key = (cmo_uid, lod_idx, mat_sig, tint_sig)
        if key in blender_mesh_cache:
            return blender_mesh_cache[key]
        faces = mesh_result['lods_faces'].get(lod_idx) or []
        if not faces:
            return None
        bl_mesh = bpy.data.meshes.new(f"CMO_{cmo_uid:X}_L{lod_idx}")
        bl_mesh.from_pydata(mesh_result['vertices'], [], faces)

        uvs = mesh_result.get('uvs')
        if uvs:
            uv_layer = bl_mesh.uv_layers.new(name="UVMap")
            for loop in bl_mesh.loops:
                u, v = uvs[loop.vertex_index]
                uv_layer.data[loop.index].uv = (u, 1.0 - v)

        bl_mesh.update()
        face_mat_ids = mesh_result.get('lods_face_mat_ids', {}).get(lod_idx, [])
        _apply_materials_to_mesh(bl_mesh, mat_uids, face_mat_ids, tint_colors)
        blender_mesh_cache[key] = bl_mesh
        return bl_mesh

    def _select_lod_indices(mesh_result):
        """Return the list of LOD indices to materialize for this CMO."""
        num_lods = mesh_result['num_lods']
        if want_all_lods:
            return [i for i in range(num_lods)
                    if mesh_result['lods_faces'].get(i)]
        # AUTO or specific LOD: try requested index, then fall back to the
        # highest-detail LOD that actually contains faces.
        candidate_order = []
        if want_lod_idx is not None and 0 <= want_lod_idx < num_lods:
            candidate_order.append(want_lod_idx)
        for i in range(num_lods):
            if i not in candidate_order:
                candidate_order.append(i)
        for i in candidate_order:
            if mesh_result['lods_faces'].get(i):
                return [i]
        return []

    def _resolve_cmo(mesh_uid, mat_override_map=None, tint_colors=None):
        """
        Resolve a mesh UID to a list of (bl_mesh, lod_idx) tuples plus the
        CMO uid. Returns ([], cmo_uid) on failure.

        mat_override_map: optional {orig_mat: override_mat} from the entity
        material override scanner. Passed through to _build_bl_mesh so the
        Blender mesh gets the correct per-entity materials.
        tint_colors: optional list of (R,G,B,A) tuples or None per material
        slot, from the entity's material tint data.
        """
        cmo_uid = all_mesh_to_cmo.get(mesh_uid)
        if not cmo_uid or not cmo_index or cmo_uid not in cmo_index:
            return [], cmo_uid
        if cmo_uid in failed_cmo_set:
            return [], cmo_uid
        mesh_result = _parse_cmo(cmo_uid)
        if not mesh_result:
            return [], cmo_uid
        lod_indices = _select_lod_indices(mesh_result)
        out = []
        for lod_idx in lod_indices:
            bl_mesh = _build_bl_mesh(cmo_uid, lod_idx, mesh_result, mesh_uid,
                                     mat_override_map, tint_colors)
            if bl_mesh:
                out.append((bl_mesh, lod_idx))
        return out, cmo_uid

    def _build_joined_submesh_bl_mesh(entity_uid, lod_idx, submesh_plan,
                                      mat_override_map=None, tint_colors=None):
        """
        Build a single Blender mesh datablock that contains the joined
        geometry of every sub-mesh CMO in submesh_plan for the given LOD.

        submesh_plan: list of (mesh_uid, cmo_uid, mesh_result) tuples,
                      pre-resolved so we don't re-parse here.
        mat_override_map: optional {orig_mat: override_mat} from the entity
                          material override scanner.
        tint_colors: optional list of (R,G,B,A) tuples or None per material
                     slot, from the entity's material tint data.

        Returns the new mesh datablock, or None if no geometry made it in.
        Identical (submesh_uids, lod, mat_overrides, tints) combinations
        return the same datablock so duplicate placements link rather than
        rebuild — see joined_mesh_cache.
        """
        submesh_sig = tuple((mu, cu) for mu, cu, _ in submesh_plan)
        override_sig = (tuple(sorted(mat_override_map.items()))
                        if mat_override_map else ())
        tint_sig = (tuple(tuple(t) if t else None for t in tint_colors)
                    if tint_colors else ())
        cache_key = (submesh_sig, lod_idx, override_sig, tint_sig)
        cached = joined_mesh_cache.get(cache_key)
        if cached is not None:
            return cached

        merged_verts = []
        merged_uvs = []
        merged_faces = []
        merged_face_mat_slot = []  # slot index per face in merged material list
        slot_uids = []  # ordered unique material UIDs across all sub-meshes
        slot_tints = []  # parallel to slot_uids: tint per merged slot
        slot_index_of = {}  # (material_uid, tint_tuple) -> slot index

        # Geometric sanity gate. Sub-meshes of a real compound (a car, a vending
        # machine) sit on top of each other; a mis-detected one pulls in meshes
        # from elsewhere in the LoadUnit and the union spans the map. Compare the
        # union's extent to the largest single sub-mesh: legitimate compounds are
        # within a small multiple, accidental ones are orders out.
        plan = _reject_scattered_submeshes(submesh_plan, lod_idx)
        if plan is not submesh_plan:
            print(f"[Siege] Compound 0x{entity_uid:016X}: dropped "
                  f"{len(submesh_plan) - len(plan)} scattered sub-mesh(es)")
        submesh_plan = plan

        for mesh_uid, cmo_uid, mesh_result in submesh_plan:
            faces = mesh_result['lods_faces'].get(lod_idx) or []
            if not faces:
                continue
            face_mat_ids = mesh_result.get('lods_face_mat_ids', {}).get(lod_idx, [])
            verts = mesh_result['vertices']
            uvs = mesh_result.get('uvs') or [(0.0, 0.0)] * len(verts)
            base = len(merged_verts)
            merged_verts.extend(verts)
            merged_uvs.extend(uvs)

            # Map this sub-mesh's local mat_ids to the merged slot index.
            # We only assign real slots when materials are enabled; otherwise
            # every face falls back to slot 0 and the lists stay empty.
            sub_mat_uids = (all_mesh_to_materials.get(mesh_uid, [])
                            if import_materials else [])
            sub_mat_uids = _apply_mat_overrides(sub_mat_uids, mat_override_map)
            local_slot_map = {}
            for local_id, mat_uid in enumerate(sub_mat_uids):
                tint = tint_colors[local_id] if tint_colors and local_id < len(tint_colors) else None
                tint_key = tuple(tint) if tint else None
                slot_key = (mat_uid, tint_key)
                if slot_key not in slot_index_of:
                    slot_index_of[slot_key] = len(slot_uids)
                    slot_uids.append(mat_uid)
                    slot_tints.append(tint)
                local_slot_map[local_id] = slot_index_of[slot_key]

            for face_i, (a, b, c) in enumerate(faces):
                merged_faces.append((a + base, b + base, c + base))
                if import_materials and sub_mat_uids:
                    lid = face_mat_ids[face_i] if face_i < len(face_mat_ids) else 0
                    merged_face_mat_slot.append(local_slot_map.get(lid, 0))
                else:
                    merged_face_mat_slot.append(0)

        if not merged_faces:
            return None

        primary_cmo_for_name = submesh_plan[0][1] if submesh_plan else 0
        bl_mesh = bpy.data.meshes.new(
            f"Joined_CMO_{primary_cmo_for_name:X}_x{len(submesh_plan)}_L{lod_idx}")
        bl_mesh.from_pydata(merged_verts, [], merged_faces)

        if merged_uvs:
            uv_layer = bl_mesh.uv_layers.new(name="UVMap")
            for loop in bl_mesh.loops:
                u, v = merged_uvs[loop.vertex_index]
                uv_layer.data[loop.index].uv = (u, 1.0 - v)

        bl_mesh.update()
        if import_materials and slot_uids:
            _apply_materials_to_mesh(bl_mesh, slot_uids, merged_face_mat_slot,
                                     slot_tints)
        joined_mesh_cache[cache_key] = bl_mesh
        return bl_mesh

    def _tag_components(obj, components):
        """Write each detected component as a boolean custom property so the
        user can filter by it in Blender (e.g., select all decals = objects
        where has_visual and not has_inert and not has_destruction)."""
        if not components:
            return
        for name in components:
            obj[f"has_{name}"] = True
        obj["component_count"] = len(components)
        obj["is_decal_like"] = (
            'visual' in components
            and 'destruction' not in components
            and 'rigidbody' not in components
            and 'deco_destruction' not in components
            and 'inert' not in components
        )

    floor_group_count = 0

    for ent in entity_list:
        uid = ent['uid']
        uid_hex = f"0x{uid:016X}"

        # Skip entities already in blend file
        if uid_hex in existing_uids:
            skipped_count += 1
            continue

        m = ent['matrix_raw']
        mat = mathutils.Matrix((
            (m[0], m[4], m[8],  m[12]),
            (m[1], m[5], m[9],  m[13]),
            (m[2], m[6], m[10], m[14]),
            (m[3], m[7], m[11], m[15]),
        ))

        components = all_entity_components.get(uid, set())
        bucket = _classify(components)
        is_floor_group = 'floor_group' in components
        cat_key = _classify_category(components)
        target_col = _get_category_collection(cat_key)

        has_mesh = False
        cmo_uid = None
        if import_meshes and uid in all_entity_to_mesh:
            # Simple entity with direct mesh link
            mesh_uid = all_entity_to_mesh[uid]
            ent_mat_overrides = all_entity_mat_overrides.get(uid)
            ent_tint_colors = all_entity_mat_tints.get(uid)
            lod_meshes, cmo_uid = _resolve_cmo(mesh_uid, ent_mat_overrides,
                                               ent_tint_colors)
            if lod_meshes:
                # First LOD becomes the primary object; any extras (when
                # lod_level == 'ALL') become children of it so the pair
                # moves together.
                primary_bl_mesh, primary_lod = lod_meshes[0]
                primary_name = (f"Entity_{uid:016X}" if len(lod_meshes) == 1
                                else f"Entity_{uid:016X}_L{primary_lod}")
                obj = bpy.data.objects.new(primary_name, primary_bl_mesh)
                obj["lod"] = primary_lod
                has_mesh = True
                mesh_count += 1
                for extra_bl_mesh, extra_lod in lod_meshes[1:]:
                    extra_obj = bpy.data.objects.new(
                        f"Entity_{uid:016X}_L{extra_lod}", extra_bl_mesh)
                    extra_obj["uid"] = str(uid)
                    extra_obj["uid_hex"] = uid_hex
                    extra_obj["lod"] = extra_lod
                    if cmo_uid:
                        extra_obj["cmo_uid"] = f"0x{cmo_uid:X}"
                    extra_obj.parent = obj
                    extra_obj.matrix_parent_inverse.identity()
                    _tag_components(extra_obj, components)
                    target_col.objects.link(extra_obj)
                    _tag_floor(extra_obj, m, mat)
                    imported_count += 1
            else:
                if not cmo_uid:
                    diag_no_mesh_to_cmo += 1
                    reason = 'mesh_to_cmo_missing'
                elif not cmo_index or cmo_uid not in cmo_index:
                    diag_no_cmo_in_index += 1
                    reason = 'cmo_not_in_index'
                elif cmo_uid in failed_cmo_set:
                    diag_cmo_previously_failed += 1
                    reason = cmo_failure_reason.get(cmo_uid, 'cmo_previously_failed')
                else:
                    reason = 'unknown_resolve_failure'
                entity_failures.append((uid, reason, mesh_uid, cmo_uid, components))

        elif import_meshes and uid in all_entity_to_submeshes:
            # Compound/destructible entity with sub-mesh references.
            #
            # We used to emit one Blender object per sub-mesh; now every
            # sub-mesh that survives LOD selection is joined into a single
            # Blender mesh datablock per LOD, with each sub-mesh's islands
            # contributing their own material slots to the merged mesh.
            submesh_uids = all_entity_to_submeshes[uid]
            ent_mat_overrides = all_entity_mat_overrides.get(uid)
            ent_tint_colors = all_entity_mat_tints.get(uid)

            # Pre-resolve each sub-mesh to (mesh_uid, cmo_uid, mesh_result).
            # We skip sub-meshes whose CMO failed to parse.
            resolved_submeshes = []
            primary_cmo_uid = None
            submesh_skip_reasons = []  # parallel to submesh_uids that failed
            for sub_mesh_uid in submesh_uids:
                sub_cmo_uid = all_mesh_to_cmo.get(sub_mesh_uid)
                if not sub_cmo_uid:
                    submesh_skip_reasons.append((sub_mesh_uid, None, 'mesh_to_cmo_missing'))
                    continue
                if not cmo_index or sub_cmo_uid not in cmo_index:
                    submesh_skip_reasons.append((sub_mesh_uid, sub_cmo_uid, 'cmo_not_in_index'))
                    continue
                if sub_cmo_uid in failed_cmo_set:
                    submesh_skip_reasons.append((sub_mesh_uid, sub_cmo_uid,
                        cmo_failure_reason.get(sub_cmo_uid, 'cmo_previously_failed')))
                    continue
                mesh_result = _parse_cmo(sub_cmo_uid)
                if not mesh_result:
                    submesh_skip_reasons.append((sub_mesh_uid, sub_cmo_uid,
                        cmo_failure_reason.get(sub_cmo_uid, 'cmo_parse_failed')))
                    continue
                resolved_submeshes.append((sub_mesh_uid, sub_cmo_uid, mesh_result))
                if primary_cmo_uid is None:
                    primary_cmo_uid = sub_cmo_uid

            if not resolved_submeshes:
                diag_no_entity_link += 1
                # All sub-meshes failed — record a row per failed sub-mesh
                # so the user can see which ones broke and why.
                if submesh_skip_reasons:
                    for sub_mesh_uid, sub_cmo_uid, reason in submesh_skip_reasons:
                        entity_failures.append(
                            (uid, f'submesh_{reason}', sub_mesh_uid, sub_cmo_uid, components))
                else:
                    entity_failures.append(
                        (uid, 'submesh_all_failed', None, None, components))
            else:
                # Pick the LOD set from the first sub-mesh; other sub-meshes
                # that lack the chosen LOD simply contribute no faces to it.
                first_mesh_result = resolved_submeshes[0][2]
                lod_indices = _select_lod_indices(first_mesh_result)

                created_objects = []
                for li, lod_idx in enumerate(lod_indices):
                    joined_bl_mesh = _build_joined_submesh_bl_mesh(
                        uid, lod_idx, resolved_submeshes, ent_mat_overrides,
                        ent_tint_colors)
                    if not joined_bl_mesh:
                        continue
                    # Same size gate as the single-mesh path — compounds are
                    # built here instead, so it has to be applied twice.
                    if max_object_span and joined_bl_mesh.vertices:
                        span = max(
                            max(v.co[a] for v in joined_bl_mesh.vertices)
                            - min(v.co[a] for v in joined_bl_mesh.vertices)
                            for a in range(3))
                        if span > max_object_span:
                            oversized_skipped[0] += 1
                            continue
                    name = (f"Entity_{uid:016X}" if len(lod_indices) == 1
                            else f"Entity_{uid:016X}_L{lod_idx}")
                    sub_obj = bpy.data.objects.new(name, joined_bl_mesh)
                    sub_obj["uid"] = str(uid)
                    sub_obj["uid_hex"] = uid_hex
                    sub_obj["lod"] = lod_idx
                    if primary_cmo_uid:
                        sub_obj["cmo_uid"] = f"0x{primary_cmo_uid:X}"
                    sub_obj["is_compound"] = True
                    sub_obj["submesh_count"] = len(resolved_submeshes)
                    sub_obj["entity_category"] = cat_key
                    _tag_components(sub_obj, components)
                    target_col.objects.link(sub_obj)
                    sx, sy, sz = _apply_transform(sub_obj, mat)
                    _record_scale(bucket, sx, sy, sz)
                    _tag_floor(sub_obj, m, mat)
                    # Extra LODs become children of the first so the stack
                    # transforms as one unit.
                    if li > 0 and created_objects:
                        sub_obj.parent = created_objects[0]
                        sub_obj.matrix_parent_inverse.identity()
                    created_objects.append(sub_obj)
                    submesh_count += 1

                if created_objects:
                    has_mesh = True
                    mesh_count += 1
                    imported_count += len(created_objects)
                    continue  # already added to collection

        elif import_meshes:
            diag_no_entity_link += 1
            # If this entity is a known archetype child, the "no link" is
            # expected (it's a tag-based runtime spawn placeholder), so use
            # a more specific reason that won't be mistaken for a parser
            # bug. Otherwise default to the generic no-link reason.
            archetype_parent = entity_archetype_parent.get(uid)
            if archetype_parent is not None:
                reason = f'archetype_child(0x{archetype_parent:016X})'
            else:
                reason = 'no_entity_to_mesh_link'
            entity_failures.append((uid, reason, None, None, components))

        if not has_mesh:
            if import_empties:
                obj = bpy.data.objects.new(f"Entity_{uid:016X}", None)
                # Floor-group volumes are visualised as cubes so they stand
                # out from ordinary meshless empties; everything else stays
                # a plain-axes marker at the user-chosen empty_size.
                if is_floor_group:
                    obj.empty_display_type = 'CUBE'
                    obj.empty_display_size = max(empty_size, 1.0)
                else:
                    obj.empty_display_type = 'PLAIN_AXES'
                    obj.empty_display_size = empty_size
                empty_count += 1

                # Reroute mesh-expected empties out of the geometry collections.
                # An entity that referenced a mesh but couldn't get its CMO is a
                # missing-geometry case (it SHOULD render); one with no mesh
                # reference at all is confirmed meshless (runtime-instanced
                # cluster / bare transform). Marker categories stay put.
                if import_meshes and cat_key in MESH_EXPECTED_CATEGORIES:
                    had_mesh_ref = (uid in all_entity_to_mesh
                                    or uid in all_entity_to_submeshes)
                    cat_key = 'MISSING_GEOMETRY' if had_mesh_ref else 'NON_MESH'
                    obj["nomesh_reason"] = (
                        'cmo_not_installed' if had_mesh_ref else 'no_mesh_reference')
                    target_col = _get_category_collection(cat_key)
            else:
                continue

        obj["uid"] = str(uid)
        obj["uid_hex"] = uid_hex
        if cmo_uid:
            obj["cmo_uid"] = f"0x{cmo_uid:X}"
        _tag_components(obj, components)

        if is_floor_group:
            obj["is_floor_group"] = True
            floor_group_count += 1
        # Oversized-geometry gate. Backdrop terrain and skydomes are map-scale
        # by design (Kanal's are 2.1 km and 2.6 km across on a ~200 m map) and
        # dwarf the playable area in the viewport. They are real geometry, not a
        # parse error, so this is an opt-in size filter rather than a fix.
        if (max_object_span and obj.type == 'MESH'
                and max(obj.dimensions) > max_object_span):
            oversized_skipped[0] += 1
            bpy.data.objects.remove(obj, do_unlink=True)
            continue

        # Decal reclassification: promote flat visual-only sheets out of VISUAL
        # so the "Decals" import toggle and the category collections mean
        # something. Done here because it needs the built geometry.
        if (cat_key == 'VISUAL' and obj.type == 'MESH'
                and obj.get("is_decal_like") and _is_flat_sheet(obj)):
            if not decals_enabled:
                bpy.data.objects.remove(obj, do_unlink=True)
                decal_skipped[0] += 1
                continue
            cat_key = 'DECAL'
            target_col = _get_category_collection('DECAL')
            decal_count[0] += 1

        obj["entity_category"] = cat_key
        target_col.objects.link(obj)

        # Apply transform AFTER linking so Blender resolves the channels
        # against the scene graph correctly. See _apply_transform docstring.
        sx, sy, sz = _apply_transform(obj, mat)
        _record_scale(bucket, sx, sy, sz)
        # Floor tagging AFTER the transform: it measures the bottom of the
        # object's geometry in WORLD space, and until _apply_transform runs
        # matrix_world is still identity — reading it earlier yields the mesh's
        # local coordinates and bands every object by the wrong height.
        _tag_floor(obj, m, mat)

        # Record one audit row per linked entity (mesh or empty) so the
        # diagnostic dump can show counts and sample UIDs by category.
        # For mesh objects we still capture cat_key + components, which is
        # useful when LIGHT/OBJECTIVE/etc. entities also carry geometry.
        # archetype_parent surfaces tag-based runtime-spawn placeholders
        # (Y3S1 archetypes) so the audit can group them.
        archetype_parent = entity_archetype_parent.get(uid)
        entity_audit.append((uid, cat_key, components, has_mesh,
                             all_entity_to_mesh.get(uid), archetype_parent))
        if archetype_parent is not None:
            obj["archetype_parent"] = f"0x{archetype_parent:016X}"

        imported_count += 1

        if imported_count % 500 == 0:
            print(f"[Siege] Imported {imported_count} objects ({mesh_count} meshes, {empty_count} empties)...")

    print(f"[Siege] Done! Imported {imported_count} objects ({mesh_count} meshes, {empty_count} empties)")
    if submesh_count:
        print(f"[Siege] Compound-entity joined objects: {submesh_count}")
    if skipped_count:
        print(f"[Siege] Skipped {skipped_count} entities already in blend file")
    print(f"[Siege] Unique mesh data blocks: {len(blender_mesh_cache)}")
    if import_materials:
        print(f"[Siege] Unique material datablocks: {len(material_cache)}")
    if want_material_chain:
        print(f"[Siege] Texture images loaded: {len(image_cache)}")
        print(f"[Siege] Texture extract stats: "
              f"extracted={tex_extract_stats['extracted']} "
              f"cached={tex_extract_stats['cached']} "
              f"missing={tex_extract_stats['missing']} "
              f"failed={tex_extract_stats['failed']}")
    if oversized_skipped[0]:
        print(f"[Siege] Oversized geometry: {oversized_skipped[0]} object(s) "
              f"larger than {max_object_span:.0f} units skipped")
    if decal_count[0] or decal_skipped[0]:
        print(f"[Siege] Decals: {decal_count[0]} flat visual-only sheets "
              f"reclassified as DECAL"
              + (f", {decal_skipped[0]} skipped (Decals off)"
                 if decal_skipped[0] else ""))
    if category_collections:
        print(f"[Siege] Entity category collections:")
        for cat_key, col in sorted(category_collections.items()):
            print(f"[Siege]   {cat_key}: {len(col.objects)} objects -> '{col.name}'")

    # Publish the floor AND room tables on the scene. The Floor/Room filter panel
    # and the web exporter both read these from here rather than re-parsing the
    # forge. The room table is written whether or not the debug volumes were
    # imported — the manifest needs it either way.
    if space_floors:
        rooms = _build_room_table(all_spaces, space_floors,
                                  space_floor_index, space_room_label,
                                  space_display_name, space_names,
                                  compass_uids)
        context.scene['siege_room_table'] = json.dumps(rooms, separators=(',', ':'))
        print(f"[Siege] Room table: {len(rooms)} rooms, "
              f"{sum(len(r['cells']) for r in rooms)} convex cells "
              f"-> scene['siege_room_table']")
        # Publish the EXTENDED table (with Roof), since that is what the object
        # tags index into — the filter panel and web exporter order by it.
        context.scene['siege_floor_table'] = json.dumps(
            [{'index': f['index'], 'label': f['label'], 'z': f['z'],
              'rooms': f.get('rooms', 0),
              'synthetic': bool(f.get('synthetic'))} for f in tag_floors])
        print(f"[Siege] Floor tags written: "
              + ", ".join(f"{k}={v}" for k, v in sorted(floor_tag_counts.items())))
        if import_space_volumes:
            n, inexact = _build_space_volumes(
                collection, all_spaces, space_floors,
                space_floor_index, space_room_label, style=space_volume_style,
                display_names=space_display_name, raw_names=space_names)
            kind = ('wireframes' if space_volume_style == 'WIRE'
                    else 'solid volumes')
            cells = sum(len(s.get('solids') or ()) for s in all_spaces.values())
            print(f"[Siege] Space debug volumes: {n} room {kind}, "
                  f"{cells} convex cells "
                  f"(hidden collection '{SPACES_COLLECTION_NAME}')")
            if inexact:
                print(f"[Siege]   {inexact} room(s) fell back to welded boxes "
                      f"(siege_space_exact = False)")
        if ceiling_trim:
            print(f"[Siege] Ceiling trim: top {ceiling_trim * 100:.0f}% removed "
                  f"from every interior room volume")
        _fs = floor_source_counts
        _ft = sum(_fs.values())
        if _ft:
            _pc = lambda k: 100.0 * _fs.get(k, 0) / _ft
            print(f"[Siege] Floor tagging: {_fs.get('contained', 0)} in a room "
                  f"({_pc('contained'):.1f}%), {_fs.get('near', 0)} snapped to "
                  f"a room within {near_room_radius:.1f} m ({_pc('near'):.1f}%), "
                  f"{_fs.get('band', 0)} by height alone ({_pc('band'):.1f}%)")
        if separate_floors:
            _separate_floor_collections(collection, map_name)
    elif separate_floors:
        print("[Siege] Auto Separate Floors: no Space floor tags on this map "
              "— nothing to separate (use the manual plane split instead)")

    # Scale profile: surface whether per-instance scale is actually present
    # in the entity matrices for each component category. If visual-only
    # entities all come back with scale == 1.0 while destructibles have
    # varied scale, we know the matrix offset is wrong for that category.
    print(f"\n[Siege] ===== Scale profile by entity class =====")
    for bucket, s in scale_profile.items():
        if s['count'] == 0:
            continue
        lo = f"{s['min']:.3f}" if s['min'] is not None else "-"
        hi = f"{s['max']:.3f}" if s['max'] is not None else "-"
        print(f"[Siege]   {bucket:<13} count={s['count']:<6} "
              f"nonuniform={s['nonuniform']:<5} "
              f"scale_range=[{lo} .. {hi}]")
    print(f"[Siege] ==========================================\n")

    # Diagnostic summary — always print so we know it ran
    total_fail = (diag_no_entity_link + diag_no_mesh_to_cmo +
                  diag_no_cmo_in_index + diag_cmo_decompress_fail +
                  diag_cmo_parse_fail + diag_cmo_no_geo +
                  diag_cmo_previously_failed)
    print(f"\n[Siege] ===== DIAGNOSTIC: Empty creation breakdown =====")
    print(f"[Siege]   No entity->mesh link found:   {diag_no_entity_link}")
    print(f"[Siege]   Mesh->CMO mapping not found:  {diag_no_mesh_to_cmo}")
    print(f"[Siege]   CMO not in mesh forge index:  {diag_no_cmo_in_index}")
    print(f"[Siege]   CMO decompression failed:     {diag_cmo_decompress_fail}")
    print(f"[Siege]   CMO parse returned None:      {diag_cmo_parse_fail}")
    print(f"[Siege]   CMO had no verts/faces:       {diag_cmo_no_geo}")
    print(f"[Siege]   CMO previously failed (dupe): {diag_cmo_previously_failed}")
    print(f"[Siege]   Unique failed CMOs:           {len(failed_cmo_set)}")
    print(f"[Siege]   Total empties from failures:  {total_fail}")
    print(f"[Siege] ================================================\n")

    # Per-entity failure log: write a CSV next to the imported forge so the
    # user can grep for specific UIDs and investigate. Also surface the top
    # samples per reason in the console for at-a-glance triage.
    if entity_failures:
        from collections import Counter
        reason_counts = Counter(row[1] for row in entity_failures)

        print(f"[Siege] ===== Unimported entities by reason =====")
        for reason, count in reason_counts.most_common():
            print(f"[Siege]   {reason:<35} {count}")
        print()

        # Per-reason sample: first 5 UIDs so the user can pick one to inspect.
        samples_by_reason = {}
        for row in entity_failures:
            uid_v, reason, mesh_uid_v, cmo_uid_v, comps = row
            samples_by_reason.setdefault(reason, []).append(row)
        for reason, rows in samples_by_reason.items():
            print(f"[Siege]   First {min(5, len(rows))} samples ({reason}):")
            for row in rows[:5]:
                uid_v, _, mesh_uid_v, cmo_uid_v, comps = row
                mu = f"0x{mesh_uid_v:016X}" if mesh_uid_v else "-"
                cu = f"0x{cmo_uid_v:016X}" if cmo_uid_v else "-"
                cs = ",".join(sorted(comps)) if comps else "-"
                print(f"[Siege]     entity=0x{uid_v:016X} mesh={mu} cmo={cu} components={cs}")
        print(f"[Siege] ==========================================\n")

        # Write the full list to a CSV next to the forge.
        csv_path = os.path.join(forge_dir, f"{map_name}_unimported.csv")
        try:
            with open(csv_path, 'w', encoding='utf-8') as f:
                f.write("entity_uid_hex,reason,mesh_uid_hex,cmo_uid_hex,components\n")
                for row in entity_failures:
                    uid_v, reason, mesh_uid_v, cmo_uid_v, comps = row
                    mu = f"0x{mesh_uid_v:016X}" if mesh_uid_v else ""
                    cu = f"0x{cmo_uid_v:016X}" if cmo_uid_v else ""
                    cs = "|".join(sorted(comps)) if comps else ""
                    f.write(f"0x{uid_v:016X},{reason},{mu},{cu},{cs}\n")
            print(f"[Siege] Wrote per-entity failure log: {csv_path}")
        except OSError as e:
            print(f"[Siege] Failed to write {csv_path}: {e}")

    # Per-category audit: counts (mesh vs empty) and the most common
    # component signature within each category so it's obvious what kind
    # of entities ended up in LIGHT, SPAWN, OBJECTIVE, etc. Also flags
    # entities with NO detected components (likely scan misses worth
    # investigating) and writes the full breakdown to a second CSV.
    if entity_audit:
        from collections import Counter, defaultdict
        cat_stats = defaultdict(lambda: {'mesh': 0, 'empty': 0, 'no_components': 0,
                                          'comp_sigs': Counter(), 'samples': [],
                                          'archetype_children': 0})
        for uid_v, cat, comps, has_m, mu, arch_parent in entity_audit:
            s = cat_stats[cat]
            if has_m:
                s['mesh'] += 1
            else:
                s['empty'] += 1
            if not comps:
                s['no_components'] += 1
            if arch_parent is not None:
                s['archetype_children'] += 1
            sig = ",".join(sorted(comps)) if comps else "<none>"
            s['comp_sigs'][sig] += 1
            if len(s['samples']) < 5:
                s['samples'].append((uid_v, sig, mu, arch_parent))

        print(f"[Siege] ===== Per-category entity audit =====")
        # Order categories so non-mesh ones surface clearly.
        non_mesh_cats = ('LIGHT', 'SPAWN', 'OBJECTIVE', 'INTERACTIVE',
                         'RUNOUT_BLOCKER', 'SOUND', 'FLOOR_GROUP',
                         'REINFORCEMENT', 'OTHER')
        ordered = [c for c in non_mesh_cats if c in cat_stats] + \
                  [c for c in cat_stats if c not in non_mesh_cats]
        for cat in ordered:
            s = cat_stats[cat]
            total = s['mesh'] + s['empty']
            arch_note = (f" archetype_children={s['archetype_children']}"
                         if s['archetype_children'] else "")
            print(f"[Siege]   {cat:<18} total={total:<5} mesh={s['mesh']:<5} "
                  f"empty={s['empty']:<5} no_components={s['no_components']}"
                  f"{arch_note}")
            # Top 3 component signatures within this category
            top_sigs = s['comp_sigs'].most_common(3)
            for sig, n in top_sigs:
                shown = sig if len(sig) < 70 else sig[:67] + "..."
                print(f"[Siege]     {n:<5}  {shown}")
            # First 3 sample UIDs
            for uid_v, sig, mu, arch in s['samples'][:3]:
                mu_s = f"0x{mu:016X}" if mu else "-"
                arch_s = f" archetype=0x{arch:016X}" if arch else ""
                print(f"[Siege]     sample 0x{uid_v:016X} mesh={mu_s} "
                      f"comps={sig if len(sig) < 50 else sig[:47]+'...'}"
                      f"{arch_s}")
        print(f"[Siege] ==========================================\n")

        # Full per-entity audit CSV.
        audit_csv = os.path.join(forge_dir, f"{map_name}_entities.csv")
        try:
            with open(audit_csv, 'w', encoding='utf-8') as f:
                f.write("entity_uid_hex,category,has_mesh,mesh_uid_hex,components,archetype_parent_hex\n")
                for uid_v, cat, comps, has_m, mu, arch_parent in entity_audit:
                    mu_s = f"0x{mu:016X}" if mu else ""
                    cs = "|".join(sorted(comps)) if comps else ""
                    arch_s = f"0x{arch_parent:016X}" if arch_parent is not None else ""
                    f.write(f"0x{uid_v:016X},{cat},{int(bool(has_m))},{mu_s},{cs},{arch_s}\n")
            print(f"[Siege] Wrote per-entity audit log: {audit_csv}")
        except OSError as e:
            print(f"[Siege] Failed to write {audit_csv}: {e}")

    return {'FINISHED'}


class IMPORT_OT_siege_map(bpy.types.Operator, ImportHelper):
    """Import Rainbow Six: Siege Map"""
    bl_idname = "import_scene.siege_map"
    bl_label = "Import Siege Map"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".forge"

    filter_glob: StringProperty(
        default="*.forge",
        options={'HIDDEN'},
        maxlen=255,
    )

    import_meshes: BoolProperty(
        name="Import Meshes",
        description="Import mesh geometry from _mesh.forge files",
        default=True,
    )

    import_materials: BoolProperty(
        name="Import Materials",
        description=(
            "Create a Blender material per unique Mesh-record material UID "
            "and assign face material indices from each island's mat_id"
        ),
        default=True,
    )

    import_textures: BoolProperty(
        name="Import Textures",
        description=(
            "Walk the Material -> TextureMapSpec -> TextureMap chain, extract "
            "the highest-resolution compiled texture per material slot from "
            "the *_textures*.forge files, decode to .dds in a cache folder "
            "next to the imported map, and load each as a Blender Image "
            "datablock attached to the material via custom properties "
            "(tex_diffuse_image, tex_normal_image, tex_specular_image, "
            "tex_misc_image). Automatically sets up a basic Principled BSDF "
            "shader graph. Requires Import Materials"
        ),
        default=False,
    )

    texture_quality: EnumProperty(
        name="Texture Quality",
        description="Maximum texture resolution to import from *_textures{0..3}.forge files",
        items=(
            ('LOW',   "Low",   "Only _textures0 (lowest resolution)"),
            ('MED',   "Med",   "Up to _textures1"),
            ('HIGH',  "High",  "Up to _textures2"),
            ('ULTRA', "Ultra", "Up to _textures3 (highest resolution)"),
        ),
        default='ULTRA',
    )

    import_empties: BoolProperty(
        name="Import Empties",
        description="Create empties for entities without meshes (or all if meshes disabled)",
        default=True,
    )

    import_space_volumes: BoolProperty(
        name="Space Debug Volumes",
        description=("Draw each Space (room) into a hidden 'Spaces' "
                     "collection. These are the engine's own room volumes that "
                     "floor tags are derived from — useful for checking a "
                     "floor boundary, not part of the model"),
        default=True,
    )

    entity_name_map: StringProperty(
        name="Entity Name Map",
        description=("Optional path to a Heated Metal 'EntityIDMap.bin'. Siege "
                     "ships NO entity names, so this is the only way to get the "
                     "designers' real room names ('1F Kitchen') instead of "
                     "generated ones. Left blank, the importer looks for it "
                     "beside the .forge. Entity UIDs are stable across game "
                     "versions, so a Y5S4-era map still names many rooms on "
                     "newer builds; reworked maps get none"),
        default="",
        subtype='FILE_PATH',
    )

    max_object_span: FloatProperty(
        name="Max Object Size",
        description=("Skip objects whose largest world dimension exceeds this "
                     "(0 = keep everything). Backdrop terrain and skydomes are "
                     "map-scale by design — Kanal's are 2.1 km and 2.6 km across "
                     "on a ~200 m map — and swamp the playable area. Try ~400"),
        default=0.0,
        min=0.0,
        soft_max=5000.0,
    )

    separate_floors: BoolProperty(
        name="Auto Separate Floors",
        description=("Move the imported objects into one collection per storey "
                     "(category kept as a sub-collection), so hiding a floor is "
                     "a single outliner checkbox. Blender shows an object if ANY "
                     "of its collections is visible, so the objects have to be "
                     "moved rather than linked — the per-category collections at "
                     "the top level are replaced by per-floor ones"),
        default=False,
    )

    ceiling_trim: FloatProperty(
        name="Ceiling Trim",
        description=("Fraction cut off the TOP of every room volume. A Space "
                     "runs floor slab to roof slab, so without this the ceiling "
                     "above a room counts as part of that room — isolating a "
                     "storey then draws its own roof and you cannot look down "
                     "into it. 0 keeps the raw volumes"),
        default=0.15,
        min=0.0,
        max=0.9,
        subtype='FACTOR',
    )

    near_room_radius: FloatProperty(
        name="Near-Room Snap",
        description=("Objects that land in no room are attached to the nearest "
                     "room within this distance instead of being placed by "
                     "height alone. Most such objects are indoor props sitting "
                     "just outside a boundary — measured across the v32 maps, "
                     "9-15% of ALL objects — while genuinely outdoor geometry "
                     "sits tens of metres away and is unaffected. 0 disables"),
        default=NEAR_ROOM_RADIUS,
        min=0.0,
        max=10.0,
        subtype='DISTANCE',
    )

    space_volume_style: EnumProperty(
        name="Volume Style",
        description="How to draw the Space debug volumes",
        items=(
            ('SOLID', "Solid Boxes",
             "One closed box per convex piece of the room, merged into a "
             "single mesh. This is the volume the floor tagging actually "
             "tests points against"),
            ('WIRE', "Wireframe",
             "The raw stored edge records. Exact room outline, but an open "
             "wire soup rather than a closed volume"),
        ),
        default='SOLID',
    )

    max_entities: IntProperty(
        name="Max Entities",
        description="Maximum number of entities to import (0 = all)",
        default=0,
        min=0,
    )

    empty_size: FloatProperty(
        name="Empty Size",
        description="Display size of empty objects",
        default=0.3,
        min=0.01,
        max=10.0,
    )

    lod_level: EnumProperty(
        name="LOD",
        description=(
            "Which level of detail to import from each mesh. 'Auto (highest)' "
            "picks the most detailed LOD available per mesh; LOD0/1/2/3 force "
            "a specific level (falling back to the highest if absent); 'All' "
            "imports every LOD as separate child objects"
        ),
        items=(
            ('AUTO', "Auto (highest)", "Pick the highest-detail LOD per mesh (LOD0)"),
            ('0', "LOD0", "Highest detail"),
            ('1', "LOD1", "Medium-high detail"),
            ('2', "LOD2", "Medium-low detail"),
            ('3', "LOD3", "Lowest detail"),
            ('ALL', "All LODs", "Import every LOD as separate child objects"),
        ),
        default='AUTO',
    )

    # --- Entity type filters (based on component classification) ---
    filter_visual: BoolProperty(
        name="Visual",
        description="Import visual-only entities (no destruction/reinforcement/objective/sound)",
        default=True,
    )
    filter_decal: BoolProperty(
        name="Decal",
        description="Import decal/overlay entities (visual-only, no inert or destruction components)",
        default=True,
    )
    filter_non_destructible: BoolProperty(
        name="Non-Destructible",
        description="Import static geometry with InertComponent (non-breakable walls, floors)",
        default=True,
    )
    filter_destructible: BoolProperty(
        name="Destructible",
        description="Import breakable entities (soft walls, props with DestructionComponent)",
        default=True,
    )
    filter_reinforcement: BoolProperty(
        name="Reinforcement",
        description="Import reinforceable surfaces",
        default=True,
    )
    filter_sound: BoolProperty(
        name="Sound",
        description="Import sound emitter entities",
        default=True,
    )
    filter_camera: BoolProperty(
        name="Camera",
        description="Import security cameras (CCTV) and default observation cameras",
        default=True,
    )
    filter_light: BoolProperty(
        name="Light",
        description="Import light volumes (spot/omni/sky lights baked into lighting)",
        default=True,
    )
    filter_spawn: BoolProperty(
        name="Spawn",
        description="Import spawn/trigger/interaction point entities",
        default=True,
    )
    filter_objective: BoolProperty(
        name="Objective",
        description="Import bomb sites, defuser interactions, objective markers",
        default=True,
    )
    filter_interactive: BoolProperty(
        name="Interactive",
        description="Import interactive gameplay props (ammo refills, biohazard, hostages, gadgets)",
        default=True,
    )
    filter_runout_blocker: BoolProperty(
        name="Runout Blocker",
        description="Import out-of-bounds trigger volumes (runout blockers)",
        default=True,
    )

    def _get_type_filter(self):
        """Build a set of enabled type category keys from the filter BoolProperties."""
        enabled = set()
        if self.filter_visual:
            enabled.add('VISUAL')
        if self.filter_decal:
            enabled.add('DECAL')
        if self.filter_non_destructible:
            enabled.add('NON_DESTRUCTIBLE')
        if self.filter_destructible:
            enabled.add('DESTRUCTIBLE')
        if self.filter_reinforcement:
            enabled.add('REINFORCEMENT')
        if self.filter_sound:
            enabled.add('SOUND')
        if self.filter_camera:
            enabled.add('CAMERA')
        if self.filter_light:
            enabled.add('LIGHT')
        if self.filter_spawn:
            enabled.add('SPAWN')
        if self.filter_objective:
            enabled.add('OBJECTIVE')
        if self.filter_interactive:
            enabled.add('INTERACTIVE')
        if self.filter_runout_blocker:
            enabled.add('RUNOUT_BLOCKER')
        # If everything is enabled, return None (no filtering) for efficiency
        if len(enabled) == len(ENTITY_TYPE_CATEGORIES):
            return None
        return enabled

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Import Types", icon='IMPORT')
        box.prop(self, "import_meshes")
        mat_row = box.column()
        mat_row.enabled = self.import_meshes
        mat_row.prop(self, "import_materials")
        tex_row = box.column()
        tex_row.enabled = self.import_meshes and self.import_materials
        tex_row.prop(self, "import_textures")
        tex_qual_row = box.column()
        tex_qual_row.enabled = self.import_meshes and self.import_materials and self.import_textures
        tex_qual_row.prop(self, "texture_quality")
        box.prop(self, "import_empties")
        box.prop(self, "import_space_volumes")
        vol_row = box.column()
        vol_row.enabled = self.import_space_volumes
        vol_row.prop(self, "space_volume_style")

        box = layout.box()
        box.label(text="Floors / Rooms", icon='HOME')
        box.prop(self, "separate_floors")
        box.prop(self, "ceiling_trim", slider=True)
        box.prop(self, "near_room_radius")
        box.prop(self, "max_object_span")
        box.label(text="trim drops each room's roof slab", icon='BLANK1')
        box.prop(self, "entity_name_map")
        box.label(text="real room names; auto-found beside the .forge",
                  icon='BLANK1')

        box = layout.box()
        box.label(text="Entity Types", icon='FILTER')
        col = box.column(align=True)
        col.prop(self, "filter_visual")
        col.prop(self, "filter_decal")
        col.prop(self, "filter_non_destructible")
        col.prop(self, "filter_destructible")
        col.prop(self, "filter_reinforcement")
        col.prop(self, "filter_sound")
        col.prop(self, "filter_camera")
        col.prop(self, "filter_light")
        col.prop(self, "filter_spawn")
        col.prop(self, "filter_objective")
        col.prop(self, "filter_interactive")
        col.prop(self, "filter_runout_blocker")

        box = layout.box()
        box.label(text="Options", icon='PREFERENCES')
        sub = box.column()
        sub.enabled = self.import_meshes
        sub.prop(self, "lod_level")
        box.prop(self, "max_entities")
        box.prop(self, "empty_size")

    def execute(self, context):
        # Auto-install zstandard on first import if Blender's bundled Python
        # doesn't ship it. After install, the loader module's `zstd` global
        # is rebound so subsequent calls go through the fast path.
        if zstd_loader.zstd is None:
            try:
                zstd_loader.zstd = zstd_loader.ensure_zstandard()
            except Exception as e:
                self.report({'ERROR'},
                    f"Failed to auto-install zstandard: {e}\n"
                    f"Install manually: {sys.executable} -m pip install zstandard")
                return {'CANCELLED'}

        try:
            result = import_siege_map(
                context,
                self.filepath,
                import_meshes=self.import_meshes,
                import_empties=self.import_empties,
                max_entities=self.max_entities,
                empty_size=self.empty_size,
                lod_level=self.lod_level,
                import_materials=self.import_materials,
                import_textures=self.import_textures,
                texture_quality=self.texture_quality,
                type_filter=self._get_type_filter(),
                import_space_volumes=self.import_space_volumes,
                space_volume_style=self.space_volume_style,
                ceiling_trim=self.ceiling_trim,
                near_room_radius=self.near_room_radius,
                separate_floors=self.separate_floors,
                max_object_span=self.max_object_span,
                entity_name_map=bpy.path.abspath(self.entity_name_map)
                if self.entity_name_map else '',
            )
            self.report({'INFO'}, "Siege map imported successfully")
            return result
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_siege_map.bl_idname, text="Siege Map (.forge)")


def register():
    bpy.utils.register_class(IMPORT_OT_siege_map)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(IMPORT_OT_siege_map)
