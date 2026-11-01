"""Non-destructive floor / room / category filtering for imported Siege maps.

The map importer tags every object with the storey and room it sits in
(`siege_floor`, `siege_room`, `entity_category`), read from the engine's own
Space partition — see `siege/forge/space.py`.

This panel turns those tags into a **selection**, never a move. Pick a floor,
press Select, and assign the result wherever you like with Blender's normal
`M`. That keeps floor separation reversible: an object can live in any
collection, be in several, or be re-filtered differently five minutes later,
because the floor lives on the object rather than in the scene layout.

The older `object.separate_by_floor_plane` operator (which moves objects into
Floor_Above / Floor_Below) is still there for scenes with no tags.
"""

import bpy  # type: ignore


ANY = '__ANY__'

# Blender does NOT copy the strings returned by a dynamic EnumProperty `items`
# callback — it stores borrowed pointers. If the list is built fresh each call
# and then garbage-collected, Blender reads freed memory and hard-crashes (no
# Python traceback, the process just dies). Keeping the last returned list
# referenced here is the standard workaround; do not inline these lists.
_ENUM_CACHE = {}


# ---------------------------------------------------------------------------
# scene tag index
# ---------------------------------------------------------------------------
# Everything below reads ONE cached scan instead of walking the scene.
#
# This panel and its three dynamic enums used to scan `scene.objects` five times
# per redraw, and Blender redraws the sidebar on mouse-move. On a Siege map —
# tens of thousands of objects, each needing Python-level custom-property
# lookups — that is seconds per frame, i.e. clicking the tab hangs Blender. The
# scan is now done once and keyed on a cheap fingerprint, so a redraw costs
# O(distinct tags) rather than O(objects).
#
# Objects appearing or disappearing is the only thing that can change the tag
# set, and it's detected in a depsgraph handler rather than in the fingerprint:
# counting objects is itself ~1.7 ms at 20k (`len(scene.objects)` walks the
# collection tree), which is far too expensive to do several times per redraw.
# The handler runs on scene edits, not on redraws, so paying it there is free.
# `Refresh` covers tags edited by hand, which nothing can detect.

_SCAN = {'key': None}
_GEN = 0            # bumped whenever the object set changes, or by Refresh
_LAST_OBJECT_N = None


def _scan_key(scene):
    return (scene.name, str(scene.get('siege_floor_table', '')), _GEN)


def _scan(scene):
    """`{floors, rooms, categories, combos, has_tags}` for the current scene.

    `combos` is `{(floor, room, category, is_space): count}` — the whole scene
    collapsed to a few hundred buckets, which is what lets the panel show a live
    match count without re-walking the objects.
    """
    key = _scan_key(scene)
    if _SCAN.get('key') == key:
        return _SCAN

    floors, rooms, cats = {}, {}, {}
    combos = {}
    for obj in scene.objects:
        floor = obj.get('siege_floor')
        room = obj.get('siege_room')
        cat = obj.get('entity_category')
        floor = floor if isinstance(floor, str) and floor else None
        room = room if isinstance(room, str) and room else None
        cat = cat if isinstance(cat, str) and cat else None
        if floor is None and room is None and cat is None:
            continue
        if floor:
            floors[floor] = floors.get(floor, 0) + 1
        if room:
            rooms[room] = rooms.get(room, 0) + 1
        if cat:
            cats[cat] = cats.get(cat, 0) + 1
        k = (floor, room, cat, cat == 'SPACE')
        combos[k] = combos.get(k, 0) + 1

    _SCAN.clear()
    _SCAN.update(key=key, floors=floors, rooms=rooms, categories=cats,
                 combos=combos, has_tags=bool(floors))
    return _SCAN


def invalidate():
    """Force the next read to rescan (used by Refresh)."""
    global _GEN
    _GEN += 1
    _SCAN['key'] = None
    _COUNTS.clear()
    # _ENUM_CACHE is deliberately NOT cleared — Blender may still hold borrowed
    # pointers into the last returned lists. The key mismatch already forces a
    # rebuild on the next call.


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph=None):
    """Invalidate when the object set changes — and only then.

    Every depsgraph update would be far too eager: selecting an object fires one,
    and a full rescan is ~50 ms on a map. Comparing the object count instead
    means selections and transforms cost one C-side count and nothing more.
    """
    global _LAST_OBJECT_N
    n = len(bpy.data.objects)
    if n != _LAST_OBJECT_N:
        _LAST_OBJECT_N = n
        invalidate()


@bpy.app.handlers.persistent
def _on_load(*_args):
    global _LAST_OBJECT_N
    _LAST_OBJECT_N = None
    invalidate()


def has_floor_tags(scene):
    """True when this scene carries Space floor tags. O(1) after the first call.

    Exported because the web-export panel needs the same answer every redraw.
    """
    return _scan(scene)['has_tags']


_COUNTS = {}


def _count(scene, floor, room, category, include_spaces):
    """Objects matching a filter, from the bucket table — no object walk.

    Memoized per filter combination as well, since the panel asks for the same
    one on every redraw and only the user changes it.
    """
    scan = _scan(scene)
    ck = (scan['key'], floor, room, category, include_spaces)
    hit = _COUNTS.get(ck)
    if hit is not None:
        return hit
    n = 0
    for (f, r, c, is_space), k in scan['combos'].items():
        if is_space and not include_spaces:
            continue
        if floor != ANY and f != floor:
            continue
        if room != ANY and r != room:
            continue
        if category != ANY and c != category:
            continue
        n += k
    if len(_COUNTS) > 256:  # only the current scan's keys are ever hit again
        _COUNTS.clear()
    _COUNTS[ck] = n
    return n


# ---------------------------------------------------------------------------
# enum sources — built from the tags actually present in the scene
# ---------------------------------------------------------------------------

def _floor_order(context):
    """Storey label -> display order, taken from the imported floor table.

    Falls back to alphabetical when the table is absent (a scene imported
    before floor tagging existed, or a hand-tagged one).
    """
    import json
    raw = context.scene.get('siege_floor_table')
    if not raw:
        return {}
    try:
        return {f['label']: f['index'] for f in json.loads(raw)}
    except Exception:
        return {}


def _cached_items(slot, key, build):
    """Memoized enum items, rebuilt only when `key` changes.

    Two jobs at once. It keeps the returned list alive (see `_ENUM_CACHE` above
    — Blender borrows the strings, so a list that gets collected is a crash),
    and it stops a 200-room map from re-formatting 200 label strings on every
    sidebar redraw.
    """
    entry = _ENUM_CACHE.get(slot)
    if entry is not None and entry[0] == key:
        return entry[1]
    items = build()
    _ENUM_CACHE[slot] = (key, items)
    return items


def _floor_items(self, context):
    if context is None:
        return _cached_items('floor_none', None, lambda: [(ANY, "All Floors", "")])
    scan = _scan(context.scene)

    def build():
        order = _floor_order(context)
        counts = scan['floors']
        items = [(ANY, "All Floors", "Every floor")]
        for label in sorted(counts, key=lambda lb: (order.get(lb, 999), lb)):
            n = counts[label]
            items.append((label, "%s  (%d)" % (label, n),
                          "%d objects on %s" % (n, label)))
        return items

    return _cached_items('floor', scan['key'], build)


def _room_items(self, context):
    if context is None:
        return _cached_items('room_none', None, lambda: [(ANY, "All Rooms", "")])
    # Only offer rooms on the selected floor, otherwise the list is unusable on
    # a 200-room map — so the selected floor is part of the cache key.
    scan = _scan(context.scene)
    floor = context.scene.siege_filter_floor

    def build():
        seen = {}
        for (f, r, _c, _s), n in scan['combos'].items():
            if not r or (floor != ANY and f != floor):
                continue
            seen[r] = seen.get(r, 0) + n
        items = [(ANY, "All Rooms", "Every room on the selected floor")]
        for room in sorted(seen):
            items.append((room, "%s  (%d)" % (room, seen[room]), ""))
        return items

    return _cached_items('room', (scan['key'], floor), build)


def _category_items(self, context):
    if context is None:
        return _cached_items('category_none', None, lambda: [(ANY, "All Types", "")])
    scan = _scan(context.scene)

    def build():
        items = [(ANY, "All Types", "Every entity type")]
        counts = scan['categories']
        for cat in sorted(counts):
            items.append((cat, "%s  (%d)" % (cat, counts[cat]),
                          "%d objects" % counts[cat]))
        return items

    return _cached_items('category', scan['key'], build)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def _matches(obj, floor, room, category, include_spaces):
    if obj.get('entity_category') == 'SPACE' and not include_spaces:
        return False
    if floor != ANY and obj.get('siege_floor') != floor:
        return False
    if room != ANY and obj.get('siege_room') != room:
        return False
    if category != ANY and obj.get('entity_category') != category:
        return False
    return True


def _filtered(context):
    scene = context.scene
    return [o for o in scene.objects
            if _matches(o, scene.siege_filter_floor, scene.siege_filter_room,
                        scene.siege_filter_category,
                        scene.siege_filter_include_spaces)]


def _describe(context):
    scene = context.scene
    bits = []
    if scene.siege_filter_floor != ANY:
        bits.append(scene.siege_filter_floor)
    if scene.siege_filter_room != ANY:
        bits.append(scene.siege_filter_room)
    if scene.siege_filter_category != ANY:
        bits.append(scene.siege_filter_category)
    return ' / '.join(bits) if bits else 'everything'


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

class SIEGE_OT_filter_select(bpy.types.Operator):
    """Select the objects matching the filter.

    `mode` decides whether the current selection is replaced, added to, or
    reduced. Nothing is moved or relinked — assign the result to a collection
    yourself (M) if you want one.
    """
    bl_idname = "siege.filter_select"
    bl_label = "Select Filtered"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(('SET', "Select", "Replace the selection"),
               ('EXTEND', "Extend", "Add to the selection"),
               ('SUBTRACT', "Deselect", "Remove from the selection")),
        default='SET',
    )  # type: ignore

    def execute(self, context):
        objs = _filtered(context)
        if self.mode == 'SET':
            for o in context.scene.objects:
                o.select_set(False)
        for o in objs:
            # A hidden object cannot be selected; skip rather than fail so a
            # filter that spans hidden geometry still selects what it can.
            if o.visible_get():
                o.select_set(self.mode != 'SUBTRACT')
        if self.mode != 'SUBTRACT' and objs:
            for o in objs:
                if o.visible_get():
                    context.view_layer.objects.active = o
                    break
        verb = {'SET': 'Selected', 'EXTEND': 'Added',
                'SUBTRACT': 'Deselected'}[self.mode]
        self.report({'INFO'}, f"{verb} {len(objs)} object(s): {_describe(context)}")
        return {'FINISHED'}


class SIEGE_OT_filter_isolate(bpy.types.Operator):
    """Show only the objects matching the filter, hiding everything else.

    Viewport visibility only — nothing is moved, so `Show All` restores the
    scene exactly.
    """
    bl_idname = "siege.filter_isolate"
    bl_label = "Isolate Filtered"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        shown = 0
        for o in context.scene.objects:
            hit = _matches(o, context.scene.siege_filter_floor,
                           context.scene.siege_filter_room,
                           context.scene.siege_filter_category,
                           context.scene.siege_filter_include_spaces)
            o.hide_set(not hit)
            shown += hit
        self.report({'INFO'}, f"Isolated {shown} object(s): {_describe(context)}")
        return {'FINISHED'}


class SIEGE_OT_filter_show_all(bpy.types.Operator):
    """Unhide every object in the scene (undo an Isolate)."""
    bl_idname = "siege.filter_show_all"
    bl_label = "Show All"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for o in context.scene.objects:
            o.hide_set(False)
        return {'FINISHED'}


class SIEGE_OT_filter_refresh(bpy.types.Operator):
    """Re-read the floor / room / type tags from the scene.

    The tag index is cached because rebuilding it on every sidebar redraw is
    what made this panel hang on a full map. It rebuilds by itself whenever
    objects are added or removed; press this after editing tags by hand.
    """
    bl_idname = "siege.filter_refresh"
    bl_label = "Refresh Tags"

    def execute(self, context):
        invalidate()
        n = len(_scan(context.scene)['combos'])
        self.report({'INFO'}, f"Re-indexed {n} tag combination(s)")
        return {'FINISHED'}


class SIEGE_OT_filter_to_collection(bpy.types.Operator):
    """Link the filtered objects into a named collection, without unlinking
    them from where they already are.

    This is the additive convenience version of "assign to collection": an
    object can sit in its category collection AND a floor collection at once,
    so building floor collections never destroys the import's organisation.
    """
    bl_idname = "siege.filter_to_collection"
    bl_label = "Link Filtered To Collection"
    bl_options = {'REGISTER', 'UNDO'}

    name: bpy.props.StringProperty(
        name="Collection",
        description="Target collection; created if it doesn't exist",
        default="")  # type: ignore

    def execute(self, context):
        name = (self.name or '').strip() or _describe(context).replace(' / ', '_')
        col = bpy.data.collections.get(name)
        if col is None:
            col = bpy.data.collections.new(name)
            context.scene.collection.children.link(col)
        n = 0
        for o in _filtered(context):
            if o.name not in col.objects:
                col.objects.link(o)
                n += 1
        self.report({'INFO'}, f"Linked {n} object(s) into '{name}'")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_siege_floor_filter(bpy.types.Panel):
    bl_label = "Floors / Rooms"
    bl_idname = "VIEW3D_PT_siege_floor_filter"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'R6S Map'
    bl_parent_id = "VIEW3D_PT_r6nr_map"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        if not has_floor_tags(scene):
            col = layout.column(align=True)
            col.label(text="No floor tags in this scene.", icon='INFO')
            col.label(text="Import a map (v29-v33) to get them.")
            col.separator()
            col.operator("siege.filter_refresh", text="Refresh Tags",
                         icon='FILE_REFRESH')
            return

        box = layout.box()
        row = box.row(align=True)
        row.label(text="Filter", icon='FILTER')
        row.operator("siege.filter_refresh", text="", icon='FILE_REFRESH')
        box.prop(scene, "siege_filter_floor", text="Floor")
        box.prop(scene, "siege_filter_room", text="Room")
        box.prop(scene, "siege_filter_category", text="Type")
        box.prop(scene, "siege_filter_include_spaces")
        box.label(text="%d match(es)" % _count(
            scene, scene.siege_filter_floor, scene.siege_filter_room,
            scene.siege_filter_category, scene.siege_filter_include_spaces))

        col = layout.column(align=True)
        row = col.row(align=True)
        row.operator("siege.filter_select", text="Select").mode = 'SET'
        row.operator("siege.filter_select", text="Extend").mode = 'EXTEND'
        row.operator("siege.filter_select", text="Deselect").mode = 'SUBTRACT'

        row = col.row(align=True)
        row.operator("siege.filter_isolate", text="Isolate", icon='HIDE_OFF')
        row.operator("siege.filter_show_all", text="Show All")

        col.separator()
        col.operator("siege.filter_to_collection",
                     text="Link To Collection", icon='OUTLINER_COLLECTION')


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

_classes = (
    SIEGE_OT_filter_select,
    SIEGE_OT_filter_isolate,
    SIEGE_OT_filter_show_all,
    SIEGE_OT_filter_to_collection,
    SIEGE_OT_filter_refresh,
    VIEW3D_PT_siege_floor_filter,
)


def register():
    invalidate()
    for cls in _classes:
        bpy.utils.register_class(cls)
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    bpy.types.Scene.siege_filter_floor = bpy.props.EnumProperty(
        name="Floor", items=_floor_items,
        description="Storey to filter by, from the imported floor table")
    bpy.types.Scene.siege_filter_room = bpy.props.EnumProperty(
        name="Room", items=_room_items,
        description="Room (Space) to filter by, within the selected floor")
    bpy.types.Scene.siege_filter_category = bpy.props.EnumProperty(
        name="Type", items=_category_items,
        description="Entity category to filter by")
    bpy.types.Scene.siege_filter_include_spaces = bpy.props.BoolProperty(
        name="Include Space Volumes", default=False,
        description="Also match the diagnostic room wireframes in the "
                    "'Spaces' collection")


def unregister():
    invalidate()
    for handlers, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
                         (bpy.app.handlers.load_post, _on_load)):
        if fn in handlers:
            handlers.remove(fn)
    del bpy.types.Scene.siege_filter_include_spaces
    del bpy.types.Scene.siege_filter_category
    del bpy.types.Scene.siege_filter_room
    del bpy.types.Scene.siege_filter_floor
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
