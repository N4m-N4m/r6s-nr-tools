"""Decal cleanup operator for the Siege web-export pipeline.

Decals in Siege map geometry are flat "sticker" meshes laid over surfaces —
posters, signage, grime, blood, bullet holes. They are visually noisy at the
zoomed-out scales the web viewer targets and inflate the export for no gain, so
this operator finds and removes (or tags) them.

Detection (conservative by default): a mesh is a decal when ALL hold —
  * near-planar      : its smallest local-AABB dimension is tiny vs the others
                       (a sheet, not a volume),
  * coplanar normals : every face normal points within `normal_tol` degrees of
                       the mesh's average normal (a single-facing card, not a
                       box whose faces cancel out),
  * low poly         : few faces (decals are simple quads / fans).

The geometry test is bpy-free (operates on a small vertex/face snapshot) so it
can be unit-tested without Blender; the operator is a thin bpy wrapper.
"""

import math

try:
    import bpy
    import bmesh
    from mathutils import Vector
except ImportError:  # allow import under plain CPython for testing
    bpy = None


# --- tunables (operator exposes these as properties) ---
DEFAULT_FLATNESS_RATIO = 0.02   # min_dim / max_dim below this => "flat"
DEFAULT_NORMAL_TOL_DEG = 15.0   # max angle of any face normal from the average
DEFAULT_MAX_FACES = 64          # decals are simple; skip dense meshes outright


def _aabb_dims(coords):
    """Return (sorted dims ascending, raw (dx,dy,dz)) for a list of (x,y,z)."""
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    zs = [c[2] for c in coords]
    dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    return sorted((dx, dy, dz)), (dx, dy, dz)


def _is_flat(coords, ratio):
    """True if the thinnest AABB axis is negligible vs the largest."""
    dims, _ = _aabb_dims(coords)
    smallest, largest = dims[0], dims[2]
    if largest <= 1e-9:
        return False
    return (smallest / largest) <= ratio


def _normals_coplanar(face_normals, tol_deg):
    """True if every face normal is within tol_deg of the average normal.

    Single-sided decals all face the same way, so their average normal has near
    unit length and each face hugs it. A box's opposing faces cancel, giving a
    short average and wide angles — correctly rejected.
    """
    if not face_normals:
        return False
    ax = sum(n[0] for n in face_normals)
    ay = sum(n[1] for n in face_normals)
    az = sum(n[2] for n in face_normals)
    mag = math.sqrt(ax * ax + ay * ay + az * az)
    if mag <= 1e-6:
        return False  # normals cancel -> not single-facing
    ax, ay, az = ax / mag, ay / mag, az / mag
    cos_tol = math.cos(math.radians(tol_deg))
    for n in face_normals:
        nm = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        if nm <= 1e-9:
            continue
        dot = (n[0] * ax + n[1] * ay + n[2] * az) / nm
        if dot < cos_tol:
            return False
    return True


def is_decal_geometry(coords, face_normals, face_count,
                      flatness_ratio=DEFAULT_FLATNESS_RATIO,
                      normal_tol_deg=DEFAULT_NORMAL_TOL_DEG,
                      max_faces=DEFAULT_MAX_FACES):
    """Pure-data decal test. coords: local vertex positions; face_normals:
    per-face normals; face_count: number of faces. Returns bool."""
    if face_count == 0 or face_count > max_faces:
        return False
    if len(coords) < 3:
        return False
    if not _is_flat(coords, flatness_ratio):
        return False
    if not _normals_coplanar(face_normals, normal_tol_deg):
        return False
    return True


def _mesh_snapshot(obj):
    """Pull (coords, face_normals, face_count) from a mesh object via bmesh.

    Uses local-space coords (object's own AABB) so detection is independent of
    where the decal sits in the world.
    """
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.normal_update()
    coords = [(v.co.x, v.co.y, v.co.z) for v in bm.verts]
    face_normals = [(f.normal.x, f.normal.y, f.normal.z) for f in bm.faces]
    fc = len(bm.faces)
    bm.free()
    return coords, face_normals, fc


def find_decals(objects, flatness_ratio, normal_tol_deg, max_faces):
    """Return the subset of `objects` whose geometry reads as a decal."""
    hits = []
    for obj in objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        coords, fnormals, fc = _mesh_snapshot(obj)
        if is_decal_geometry(coords, fnormals, fc,
                             flatness_ratio, normal_tol_deg, max_faces):
            hits.append(obj)
    return hits


if bpy is not None:

    DECAL_COLLECTION = "_Decals"

    class SIEGE_OT_web_cleanup_decals(bpy.types.Operator):
        """Detect flat single-facing decal meshes and remove or isolate them."""
        bl_idname = "siege.web_cleanup_decals"
        bl_label = "Siege: Clean Up Decals"
        bl_options = {'REGISTER', 'UNDO'}

        scope: bpy.props.EnumProperty(
            name="Scope",
            items=(('SCENE', "Whole Scene", "All mesh objects"),
                   ('SELECTED', "Selected Only", "Only selected mesh objects")),
            default='SCENE',
        )
        action: bpy.props.EnumProperty(
            name="Action",
            items=(('TAG', "Separate", "Move them to a '_Decals' collection for review"),
                   ('DELETE', "Delete", "Remove the detected decal objects")),
            default='TAG',
        )
        flatness_ratio: bpy.props.FloatProperty(
            name="Flatness", default=DEFAULT_FLATNESS_RATIO,
            min=0.0, max=0.5, precision=4,
            description="Max thin/thick AABB ratio to count as flat")
        normal_tol_deg: bpy.props.FloatProperty(
            name="Normal tolerance", default=DEFAULT_NORMAL_TOL_DEG,
            min=0.0, max=90.0,
            description="Max angle (deg) any face normal may deviate from the average")
        max_faces: bpy.props.IntProperty(
            name="Max faces", default=DEFAULT_MAX_FACES, min=1, max=10000,
            description="Skip meshes with more faces than this")
        dry_run: bpy.props.BoolProperty(
            name="Preview only (count, don't modify)", default=False)

        def _targets(self, context):
            if self.scope == 'SELECTED':
                return [o for o in context.selected_objects if o.type == 'MESH']
            return [o for o in context.scene.objects if o.type == 'MESH']

        def execute(self, context):
            targets = self._targets(context)
            decals = find_decals(targets, self.flatness_ratio,
                                 self.normal_tol_deg, self.max_faces)
            n = len(decals)

            if self.dry_run:
                self.report({'INFO'}, f"Decal preview: {n} of {len(targets)} meshes match")
                print(f"[SiegeWeb] Decal preview: {n}/{len(targets)} match")
                return {'FINISHED'}

            if n == 0:
                self.report({'INFO'}, "No decals matched")
                return {'FINISHED'}

            if self.action == 'TAG':
                col = bpy.data.collections.get(DECAL_COLLECTION)
                if col is None:
                    col = bpy.data.collections.new(DECAL_COLLECTION)
                    context.scene.collection.children.link(col)
                for obj in decals:
                    obj["web_decal"] = True
                    for c in list(obj.users_collection):
                        c.objects.unlink(obj)
                    col.objects.link(obj)
                self.report({'INFO'}, f"Tagged {n} decals into '{DECAL_COLLECTION}'")
            else:  # DELETE
                meshes = {o.data for o in decals}
                for obj in decals:
                    bpy.data.objects.remove(obj, do_unlink=True)
                # purge now-orphaned mesh datablocks
                for me in meshes:
                    if me.users == 0:
                        bpy.data.meshes.remove(me)
                self.report({'INFO'}, f"Deleted {n} decal objects")
            print(f"[SiegeWeb] Decal cleanup ({self.action}): {n}/{len(targets)}")
            return {'FINISHED'}


    class SIEGE_OT_delete_with_instances(bpy.types.Operator):
        """Delete the selected object(s) AND every object sharing their mesh.

        Decals are heavily instanced (one flat card placed hundreds of times), so
        deleting a single object leaves all its copies behind. Pick one instance,
        run this, and every object that shares its mesh datablock is removed — the
        reliable manual counterpart to the heuristic decal detector.
        """
        bl_idname = "siege.delete_with_instances"
        bl_label = "Siege: Delete + All Instances"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            seeds = [o for o in context.selected_objects
                     if o.type == 'MESH' and o.data is not None]
            if not seeds:
                self.report({'WARNING'}, "Select at least one mesh object")
                return {'CANCELLED'}
            target_data = {o.data for o in seeds}
            victims = [o for o in bpy.data.objects
                       if o.type == 'MESH' and o.data in target_data]
            n = len(victims)
            for obj in victims:
                bpy.data.objects.remove(obj, do_unlink=True)
            for me in target_data:
                if me.users == 0:
                    bpy.data.meshes.remove(me)
            self.report({'INFO'},
                        f"Deleted {n} objects across {len(target_data)} datablock(s)")
            print(f"[SiegeWeb] delete+instances: {n} objects, "
                  f"{len(target_data)} datablocks")
            return {'FINISHED'}


    _classes = (SIEGE_OT_web_cleanup_decals, SIEGE_OT_delete_with_instances)

    def register():
        for cls in _classes:
            bpy.utils.register_class(cls)

    def unregister():
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)
