import bpy  # type: ignore
import mathutils  # type: ignore


# Collection names the operator sorts meshes into. Created on demand under the
# scene's master collection.
ABOVE_COLLECTION = "Floor_Above"
BELOW_COLLECTION = "Floor_Below"


def _get_or_create_collection(context, name):
    """Return the named collection, creating + linking it to the scene if new."""
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        context.scene.collection.children.link(col)
    elif name not in context.scene.collection.children:
        # Exists but isn't linked to this scene's master collection — link it
        # so its objects stay visible.
        try:
            context.scene.collection.children.link(col)
        except RuntimeError:
            pass
    return col


def _move_to_collection(obj, target):
    """Unlink obj from all its current collections and link it to target only."""
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    target.objects.link(obj)


def _bbox_center_world(obj):
    """World-space center of the object's local bounding box.

    obj.bound_box is 8 local corners; averaging them gives the local center,
    which matrix_world maps to world space. This is robust for objects whose
    origin sits away from the geometry (common on imported map props).
    """
    local = sum((mathutils.Vector(c) for c in obj.bound_box),
                mathutils.Vector()) / 8.0
    return obj.matrix_world @ local


class OBJECT_OT_SeparateByFloorPlane(bpy.types.Operator):
    """Sort meshes above/below the active object's plane into floor collections.

    Pick a plane object (it's the cutting plane: its origin is a point on the
    plane, its local +Z is the plane normal — so a tilted plane works too), then
    every OTHER mesh is classified by its bounding-box center and moved into the
    'Floor_Above' or 'Floor_Below' collection. The plane object itself is left
    where it is. Straddling objects are sorted whole by their center, never split.
    """
    bl_idname = "object.separate_by_floor_plane"
    bl_label = "Separate Meshes By Floor Plane"
    bl_options = {'REGISTER', 'UNDO'}

    use_local_z: bpy.props.BoolProperty(
        name="Use Plane Orientation",
        description=(
            "Use the plane object's local +Z as the cut normal (handles tilted "
            "planes). When off, cut on world Z at the plane's height"
        ),
        default=True,
    )  # type: ignore
    include_hidden: bpy.props.BoolProperty(
        name="Include Hidden",
        description="Also sort meshes that are currently hidden",
        default=False,
    )  # type: ignore
    epsilon: bpy.props.FloatProperty(
        name="On-Plane Tolerance",
        description=(
            "Objects whose center is within this distance of the plane are "
            "treated as ON the plane and sent to the Above side"
        ),
        default=0.001,
        min=0.0,
        precision=4,
    )  # type: ignore

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        plane = context.active_object
        if plane is None:
            self.report({'ERROR'}, "No active object to use as the cutting plane")
            return {'CANCELLED'}

        # Plane point + normal in world space.
        point = plane.matrix_world.translation.copy()
        if self.use_local_z:
            # Local +Z transformed to world, ignoring translation, normalized.
            normal = (plane.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, 1.0)))
            if normal.length < 1e-9:
                normal = mathutils.Vector((0.0, 0.0, 1.0))
            normal.normalize()
        else:
            normal = mathutils.Vector((0.0, 0.0, 1.0))

        above_col = _get_or_create_collection(context, ABOVE_COLLECTION)
        below_col = _get_or_create_collection(context, BELOW_COLLECTION)

        above = below = 0
        for obj in list(context.scene.objects):
            if obj.type != 'MESH':
                continue
            if obj == plane:
                continue
            if not self.include_hidden and not obj.visible_get():
                continue

            center = _bbox_center_world(obj)
            # Signed distance from the plane: positive = above (same side as
            # the normal), negative = below.
            dist = (center - point).dot(normal)

            if dist >= -self.epsilon:
                _move_to_collection(obj, above_col)
                above += 1
            else:
                _move_to_collection(obj, below_col)
                below += 1

        self.report(
            {'INFO'},
            f"Floor split: {above} above -> '{ABOVE_COLLECTION}', "
            f"{below} below -> '{BELOW_COLLECTION}'")
        return {'FINISHED'}


FLOOR_TOLERANCE = 0.7      # matches forge/space.py FLOOR_BELOW_TOLERANCE
ROOF_LABEL = 'Roof'


def _floor_bands(scene):
    """`[{index,label,z}]` for the scene, with a synthetic Roof band appended.

    Read from the tables the importer published, so this needs no forge.
    """
    import json
    raw = scene.get('siege_floor_table')
    if not raw:
        return []
    try:
        table = [dict(f) for f in json.loads(raw)]
    except Exception:
        return []
    table = [f for f in table if not f.get('synthetic')]
    if not table:
        return []
    table.sort(key=lambda f: f['z'])
    for i, f in enumerate(table):
        f['index'] = i
    gaps = sorted(table[i + 1]['z'] - table[i]['z'] for i in range(len(table) - 1))
    storey = gaps[len(gaps) // 2] if gaps else 3.0
    table.append({'index': len(table), 'label': ROOF_LABEL,
                  'z': table[-1]['z'] + max(storey, 2.0), 'synthetic': True})
    return table


def _band(bands, z):
    idx = bands[0]['index']
    for f in bands:
        if z >= f['z'] - FLOOR_TOLERANCE:
            idx = f['index']
    return idx


class OBJECT_OT_AutoSeparateFloors(bpy.types.Operator):
    """Retag every object with the storey it visually sits on, then rebuild the
    floor collections.

    Re-runnable on an already-imported scene — it reads the floor and room
    tables the importer left on the scene, so it needs no .forge.

    Two things the raw Space tags get wrong on their own, both fixed here:

    * **Rooms that span more than one storey.** Atria, stairwells and
      double-height halls are the norm, not the exception (134 of Kanal's 193
      rooms cross a slab). Handing such a room's label to everything inside it
      tags the storey-above's floor slab and walls with the storey below, so
      hiding that storey leaves them behind and you cannot see in. Inside a
      multi-storey room the object's own resting height decides instead, and
      never below the room's own floor.
    * **No roof band.** The top storey's ceiling has nothing above it to belong
      to, so it stays visible when you hide the top storey. A synthetic 'Roof'
      band one storey above the top slab gives it somewhere to go.

    Height is measured from the BOTTOM of an object's geometry: a prop rests on
    its floor, and an entity origin can sit anywhere in the mesh (for a wall it
    is often at the top).
    """
    bl_idname = "object.auto_separate_floors"
    bl_label = "Auto Separate Floors"
    bl_options = {'REGISTER', 'UNDO'}

    rebuild_collections: bpy.props.BoolProperty(
        name="Rebuild Collections",
        description=("Move objects into one collection per storey, category "
                     "inside. Off just retags them, leaving the layout alone"),
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return bool(context.scene.get('siege_floor_table'))

    def execute(self, context):
        import json
        scene = context.scene
        bands = _floor_bands(scene)
        if not bands:
            self.report({'ERROR'}, "No floor table on this scene — import a map first")
            return {'CANCELLED'}
        by_label = {f['label']: f['index'] for f in bands}

        rooms = {}
        try:
            for r in json.loads(scene.get('siege_room_table') or '[]'):
                rooms[r['id']] = r
        except Exception:
            pass

        retagged = 0
        counts = {}
        # Empties are markers (spawns, sounds, cameras) and belong to a storey
        # too, or they stay behind in the old collection when a floor is hidden.
        # The Space debug volumes are an overlay and stay out of it.
        targets = [o for o in scene.objects
                   if o.type in {'MESH', 'EMPTY'}
                   and o.get('entity_category') != 'SPACE']
        for obj in targets:
            if obj.type == 'MESH' and obj.data and obj.data.vertices:
                base = min((obj.matrix_world @ mathutils.Vector(c)).z
                           for c in obj.bound_box)
            else:
                base = obj.matrix_world.translation.z
            room = rooms.get(obj.get('siege_room'))
            if room is None:
                idx = _band(bands, base)
            else:
                idx = by_label.get(room['floor'], _band(bands, base))
                z0, z1 = room['aabb'][2], room['aabb'][5]
                if _band(bands, z1) > _band(bands, z0):
                    idx = max(idx, _band(bands, base))
            label = bands[idx]['label']
            if obj.get('siege_floor') != label:
                retagged += 1
            obj['siege_floor'] = label
            obj['siege_floor_index'] = idx
            counts[label] = counts.get(label, 0) + 1

        scene['siege_floor_table'] = json.dumps(
            [{'index': f['index'], 'label': f['label'], 'z': f['z'],
              'rooms': f.get('rooms', 0),
              'synthetic': bool(f.get('synthetic'))} for f in bands])

        made = 0
        if self.rebuild_collections:
            made = _rebuild_floor_collections(scene, targets)

        self.report({'INFO'},
                    f"Retagged {retagged}/{len(targets)} objects into "
                    + ", ".join(f"{k} ({v})" for k, v in sorted(counts.items()))
                    + (f"; {made} collections" if made else ""))
        return {'FINISHED'}


def _rebuild_floor_collections(scene, objects):
    """Move `objects` into `<map>_Floor_<label>` / `..._<Category>`.

    Objects have to be MOVED, not linked: Blender shows an object if ANY of its
    collections is visible, so an additive floor collection hides nothing.
    """
    root = None
    for col in scene.collection.children:
        if any(o.get('entity_category') for o in col.all_objects):
            root = col
            break
    if root is None:
        root = scene.collection
    prefix = root.name if root is not scene.collection else 'Siege'

    made = {}

    def sub(label, cat):
        top_name = f"{prefix}_Floor_{label}"
        top = made.get(top_name)
        if top is None:
            top = bpy.data.collections.get(top_name) or \
                bpy.data.collections.new(top_name)
            if top.name not in root.children:
                try:
                    root.children.link(top)
                except RuntimeError:
                    pass
            made[top_name] = top
        name = f"{top_name}_{cat.title().replace('_', '')}"
        child = made.get(name)
        if child is None:
            child = bpy.data.collections.get(name) or \
                bpy.data.collections.new(name)
            if child.name not in top.children:
                try:
                    top.children.link(child)
                except RuntimeError:
                    pass
            made[name] = child
        return child

    for obj in objects:
        label = obj.get('siege_floor')
        if not isinstance(label, str) or not label:
            continue
        target = sub(label, str(obj.get('entity_category') or 'OTHER'))
        if target.name in {c.name for c in obj.users_collection}:
            continue
        for col in list(obj.users_collection):
            col.objects.unlink(obj)
        target.objects.link(obj)

    # Drop collections the move emptied, so the outliner doesn't fill with
    # dead nodes from the previous layout.
    for col in list(root.children):
        if col.name in made:
            continue
        if not col.all_objects:
            try:
                root.children.unlink(col)
                bpy.data.collections.remove(col)
            except Exception:
                pass
    return len(made)


def register():
    bpy.utils.register_class(OBJECT_OT_SeparateByFloorPlane)
    bpy.utils.register_class(OBJECT_OT_AutoSeparateFloors)


def unregister():
    bpy.utils.unregister_class(OBJECT_OT_AutoSeparateFloors)
    bpy.utils.unregister_class(OBJECT_OT_SeparateByFloorPlane)
