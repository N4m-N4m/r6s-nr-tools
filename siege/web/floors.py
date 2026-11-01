"""Manual floor-collection assignment for the web map exporter.

export_map.py normally derives floors from world-Z banding — fine for a first
pass, but Siege storeys overlap in Z (mezzanines, stairwells, sloped roofs) so
the automatic split is often wrong. This module lets the user define floors
explicitly instead: each floor is a Blender collection, and every mesh inside it
(recursively) becomes that floor in the exported manifest.

Within a floor the objects are still split into viewer *kinds* — walls /
softwalls / reinforcement / objective ... — by their ``entity_category`` custom
property (written by the importer), exactly as before. Floor assignment only
decides *which floor* an object lands on; kind detection is unchanged.

When ``scene.siege_floors`` has at least one collection assigned the exporter
uses these collections, in list order; otherwise it falls back to automatic
Z-banding so nothing changes for scenes that never touch the Floors panel.

The UI lives inside the "Siege Map / Web Export" panel (web/optimize.py); this
module only owns the data + operators.
"""

import bpy


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

class SiegeFloorItem(bpy.types.PropertyGroup):
    """One floor = one Blender collection, exported as a separate floor."""

    collection: bpy.props.PointerProperty(
        name="Collection",
        type=bpy.types.Collection,
        description="Every mesh in this collection (and its children) exports "
                    "as this floor")
    label: bpy.props.StringProperty(
        name="Label",
        default="",
        description="Floor name shown in the web viewer; defaults to the "
                    "collection name when left blank")


# ---------------------------------------------------------------------------
# resolution used by the exporter
# ---------------------------------------------------------------------------

def manual_floors(scene):
    """Resolve user-defined floor collections to ordered ``(label, [mesh objs])``.

    Returns ``None`` when no floor has a collection assigned, so the exporter
    can fall back to automatic Z-banding. Objects are de-duplicated across
    floors (first floor in the list wins) so a mesh linked into two floor
    collections is not exported twice. Empty floor slots are kept in place so
    the manifest ``order`` index lines up with the panel's row numbers.
    """
    items = getattr(scene, 'siege_floors', None)
    if not items:
        return None
    floors = []
    seen = set()
    any_col = False
    for it in items:
        col = it.collection
        if col is None:
            floors.append(((it.label or "Floor").strip() or "Floor", []))
            continue
        any_col = True
        label = (it.label or col.name).strip() or col.name
        objs = []
        for o in col.all_objects:
            if o.type != 'MESH' or o.name in seen:
                continue
            seen.add(o.name)
            objs.append(o)
        floors.append((label, objs))
    return floors if any_col else None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _floor_collections(scene):
    return [it.collection for it in getattr(scene, 'siege_floors', []) if it.collection]


def _active(scene):
    idx = scene.siege_floors_index
    if 0 <= idx < len(scene.siege_floors):
        return scene.siege_floors[idx]
    return None


def _assign_objects(scene, target, objs):
    """Link ``objs`` into the ``target`` floor collection, unlinking them from
    any *other* floor collection so floors stay mutually exclusive. Objects keep
    their category collections (Visual / Destructible / ...) untouched — those
    are organizational and kind detection reads the custom prop, not membership.
    Returns the number of objects newly linked.
    """
    others = [c for c in _floor_collections(scene) if c is not target]
    moved = 0
    for o in objs:
        for c in others:
            if o.name in c.objects:
                c.objects.unlink(o)
        if o.name not in target.objects:
            target.objects.link(o)
            moved += 1
    return moved


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SIEGE_OT_floor_add(bpy.types.Operator):
    """Add a floor: creates a new collection and appends it to the list.

    With *From Selected* on, the selected mesh objects are linked into the new
    floor in one step.
    """
    bl_idname = "siege.floor_add"
    bl_label = "Add Floor"
    bl_options = {'REGISTER', 'UNDO'}

    from_selected: bpy.props.BoolProperty(
        name="From Selected", default=False,
        description="Link the selected mesh objects into the new floor")

    def execute(self, context):
        scene = context.scene
        n = len(scene.siege_floors) + 1
        col = bpy.data.collections.new(f"Floor {n}")
        scene.collection.children.link(col)
        item = scene.siege_floors.add()
        item.collection = col
        item.label = f"F{n}"
        scene.siege_floors_index = len(scene.siege_floors) - 1
        if self.from_selected:
            sel = [o for o in context.selected_objects if o.type == 'MESH']
            moved = _assign_objects(scene, col, sel)
            self.report({'INFO'}, f"New floor '{col.name}' with {moved} object(s)")
        else:
            self.report({'INFO'}, f"Added floor '{col.name}'")
        return {'FINISHED'}


class SIEGE_OT_floor_remove(bpy.types.Operator):
    """Remove the active floor slot. The collection itself is left in the scene
    (delete it from the Outliner if you no longer want it)."""
    bl_idname = "siege.floor_remove"
    bl_label = "Remove Floor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        idx = scene.siege_floors_index
        if not (0 <= idx < len(scene.siege_floors)):
            return {'CANCELLED'}
        scene.siege_floors.remove(idx)
        scene.siege_floors_index = min(idx, len(scene.siege_floors) - 1)
        return {'FINISHED'}


class SIEGE_OT_floor_move(bpy.types.Operator):
    """Reorder the active floor. Order is the floor's export order / manifest
    ``order`` (lowest = bottom storey in the viewer's floor stepper)."""
    bl_idname = "siege.floor_move"
    bl_label = "Move Floor"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=(('UP', "Up", ""), ('DOWN', "Down", "")))

    def execute(self, context):
        scene = context.scene
        idx = scene.siege_floors_index
        n = len(scene.siege_floors)
        if n < 2:
            return {'CANCELLED'}
        new = idx - 1 if self.direction == 'UP' else idx + 1
        if not (0 <= new < n):
            return {'CANCELLED'}
        scene.siege_floors.move(idx, new)
        scene.siege_floors_index = new
        return {'FINISHED'}


class SIEGE_OT_floor_assign_selected(bpy.types.Operator):
    """Link the selected mesh objects into the active floor (removing them from
    any other floor so each object belongs to exactly one floor)."""
    bl_idname = "siege.floor_assign_selected"
    bl_label = "Assign Selected to Floor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        item = _active(scene)
        if item is None or item.collection is None:
            self.report({'ERROR'}, "Active floor has no collection")
            return {'CANCELLED'}
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        if not sel:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        moved = _assign_objects(scene, item.collection, sel)
        self.report({'INFO'}, f"Assigned {moved} object(s) to {item.collection.name}")
        return {'FINISHED'}


class SIEGE_OT_floor_select_members(bpy.types.Operator):
    """Select all mesh objects on the active floor (to inspect or re-assign)."""
    bl_idname = "siege.floor_select_members"
    bl_label = "Select Floor Members"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        item = _active(scene)
        if item is None or item.collection is None:
            return {'CANCELLED'}
        bpy.ops.object.select_all(action='DESELECT')
        n = 0
        for o in item.collection.all_objects:
            if o.type == 'MESH':
                o.select_set(True)
                context.view_layer.objects.active = o
                n += 1
        self.report({'INFO'}, f"Selected {n} object(s)")
        return {'FINISHED'}


class SIEGE_OT_floors_from_spaces(bpy.types.Operator):
    """Build the floor list from the `siege_floor` tags written at import.

    Those tags come from the engine's own Space (room) partition, so this is the
    game's own storey split rather than a Z guess — stairwells land on the floor
    they start from. Only needed if you want real collections; the exporter
    already reads the tags directly, and the Floors/Rooms filter panel selects on
    them without moving anything. Replaces the current list.
    """
    bl_idname = "siege.floors_from_spaces"
    bl_label = "Auto Floors From Spaces"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.get('siege_floor') for o in context.scene.objects)

    def execute(self, context):
        import json
        from . import export_map as em
        scene = context.scene

        order = {}
        raw = scene.get('siege_floor_table')
        if raw:
            try:
                order = {f['label']: f['index'] for f in json.loads(raw)}
            except Exception:
                order = {}

        buckets = {}
        for o in scene.objects:
            if o.type != 'MESH' or not em.object_kind(o):
                continue
            label = o.get('siege_floor')
            if isinstance(label, str) and label:
                buckets.setdefault(label, []).append(o)
        if not buckets:
            self.report({'WARNING'}, "No floor-tagged meshes in this scene")
            return {'CANCELLED'}

        scene.siege_floors.clear()
        for label in sorted(buckets, key=lambda l: (order.get(l, 999), l)):
            col = bpy.data.collections.new(f"Floor {label}")
            scene.collection.children.link(col)
            item = scene.siege_floors.add()
            item.collection = col
            item.label = label
            _assign_objects(scene, col, buckets[label])
        scene.siege_floors_index = 0
        self.report({'INFO'},
                    f"Created {len(buckets)} floor(s) from Space tags")
        return {'FINISHED'}


class SIEGE_OT_floors_from_height(bpy.types.Operator):
    """Seed floor collections from automatic world-Z bands — the same
    clustering the exporter uses when no floors are defined. A starting point
    you then rename, merge, or re-assign by hand. Replaces the current list."""
    bl_idname = "siege.floors_from_height"
    bl_label = "Auto Floors From Height"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import export_map as em
        scene = context.scene
        meshes = [o for o in scene.objects
                  if o.type == 'MESH' and em.object_kind(o)]
        if not meshes:
            self.report({'WARNING'}, "No exportable mesh objects in scene")
            return {'CANCELLED'}
        zc = {o: em._center_z(o) for o in meshes}
        bands = em.band_floors(list(zc.values()))
        if not bands:
            return {'CANCELLED'}
        scene.siege_floors.clear()
        cols = []
        for i in range(len(bands)):
            col = bpy.data.collections.new(f"Floor {i + 1}")
            scene.collection.children.link(col)
            item = scene.siege_floors.add()
            item.collection = col
            item.label = em.floor_label(i, len(bands))
            cols.append(col)
        for o, z in zc.items():
            idx = len(bands) - 1
            for i, (lo, hi) in enumerate(bands):
                if z <= hi + 1e-6:
                    idx = i
                    break
            _assign_objects(scene, cols[idx], [o])
        scene.siege_floors_index = 0
        self.report({'INFO'}, f"Created {len(bands)} floor(s) from height")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI list (drawn by web/optimize.py's panel)
# ---------------------------------------------------------------------------

class SIEGE_UL_floors(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.label(text=f"{index + 1}")
        row.prop(item, "collection", text="", icon='OUTLINER_COLLECTION')
        row.prop(item, "label", text="")
        n = (sum(1 for o in item.collection.all_objects if o.type == 'MESH')
             if item.collection else 0)
        row.label(text=str(n), icon='MESH_DATA')


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

_classes = (
    SiegeFloorItem,
    SIEGE_UL_floors,
    SIEGE_OT_floor_add,
    SIEGE_OT_floor_remove,
    SIEGE_OT_floor_move,
    SIEGE_OT_floor_assign_selected,
    SIEGE_OT_floor_select_members,
    SIEGE_OT_floors_from_spaces,
    SIEGE_OT_floors_from_height,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.siege_floors = bpy.props.CollectionProperty(type=SiegeFloorItem)
    bpy.types.Scene.siege_floors_index = bpy.props.IntProperty(
        name="Active Floor", default=0)


def unregister():
    del bpy.types.Scene.siege_floors_index
    del bpy.types.Scene.siege_floors
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
