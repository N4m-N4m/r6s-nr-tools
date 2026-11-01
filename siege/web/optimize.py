"""
Siege map web-export optimization operator.

This is a separate post-process step from the importer. The importer's job is
accurate map data; this operator tags an already-imported scene with size and
instance metadata so a later LOD-generation / glTF-export step can make
informed decisions (which objects to instance, which to replace with proxies,
which to leave alone).

Phase 2 only: classification pass. No geometry is modified. Phase 3 (LOD
generation) and Phase 5 (glTF export hints) will read the custom properties
this operator writes.

Custom properties written per object:
    web_size_class   : 'shell' | 'large_prop' | 'small_prop' | 'tiny'
    web_diagonal_m   : float, world-space AABB diagonal in metres
    web_instance_n   : int, how many objects in the scene share this datablock

Scene-level summary printed to console.
"""

import math

import bpy

try:
    # Shared, cached scene-tag index. The panel needs "does this scene carry
    # Space floor tags?" on every redraw, and answering it by walking
    # scene.objects froze the sidebar on a full map.
    from ...operators.attribute_filter import has_floor_tags
except Exception:  # standalone install of the siege package only
    def has_floor_tags(scene):
        return any(o.get('siege_floor') for o in scene.objects)


SHELL_DIAGONAL_M = 10.0
LARGE_PROP_DIAGONAL_M = 2.0
TINY_DIAGONAL_M = 0.5

# Entity categories that are always shell regardless of size — they form the
# playable map volume and should never be replaced with proxies.
SHELL_CATEGORIES = {'FLOOR_GROUP', 'REINFORCEMENT'}


def _world_diagonal(obj):
    """Return the world-space AABB diagonal length in metres.

    obj.dimensions is local-space; we multiply by the absolute scale factors
    of the world matrix so non-uniform parent scales are accounted for.
    """
    sx, sy, sz = obj.matrix_world.to_scale()
    dx, dy, dz = obj.dimensions
    wx, wy, wz = dx * abs(sx), dy * abs(sy), dz * abs(sz)
    return math.sqrt(wx * wx + wy * wy + wz * wz)


def _classify(obj, diagonal, instance_count):
    """Pick a size class for a single mesh object.

    Shell rules first (category-based + diagonal), then prop tiers by size.
    Unique high-poly props stay large_prop — instance_count alone doesn't
    demote them to shell because that would hide real geometry from later
    optimization passes.
    """
    cat = obj.get('entity_category')
    if cat in SHELL_CATEGORIES:
        return 'shell'
    if diagonal >= SHELL_DIAGONAL_M:
        return 'shell'
    if diagonal >= LARGE_PROP_DIAGONAL_M:
        return 'large_prop'
    if diagonal >= TINY_DIAGONAL_M:
        return 'small_prop'
    return 'tiny'


def classify_scene(context, scope='SCENE'):
    """Walk mesh objects, write web_* custom props, return summary counts.

    scope: 'SCENE' = every mesh object in the current scene,
           'SELECTED' = only selected mesh objects.
    """
    if scope == 'SELECTED':
        objs = [o for o in context.selected_objects if o.type == 'MESH']
    else:
        objs = [o for o in context.scene.objects if o.type == 'MESH']

    # First pass: count instances per mesh datablock. A "linked duplicate" in
    # Blender means multiple objects share one bpy.types.Mesh; that count is
    # exactly what a web exporter needs to decide InstancedMesh vs Mesh.
    datablock_users = {}
    for obj in objs:
        if obj.data is None:
            continue
        datablock_users[obj.data.name] = datablock_users.get(obj.data.name, 0) + 1

    summary = {'shell': 0, 'large_prop': 0, 'small_prop': 0, 'tiny': 0}
    total_tris = 0
    unique_tris = 0
    seen_data = set()

    for obj in objs:
        if obj.data is None:
            continue
        diagonal = _world_diagonal(obj)
        instances = datablock_users.get(obj.data.name, 1)
        size_class = _classify(obj, diagonal, instances)

        obj['web_size_class'] = size_class
        obj['web_diagonal_m'] = round(diagonal, 3)
        obj['web_instance_n'] = instances

        summary[size_class] += 1
        tris = len(obj.data.polygons)
        total_tris += tris
        if obj.data.name not in seen_data:
            seen_data.add(obj.data.name)
            unique_tris += tris

    return summary, total_tris, unique_tris, len(seen_data), len(objs)


class SIEGE_OT_web_classify(bpy.types.Operator):
    """Tag mesh objects with web-export size class and instance count.

    Read-only with respect to geometry: only writes custom properties. Run
    this after import; later LOD / glTF passes branch on web_size_class.
    """
    bl_idname = "siege.web_classify"
    bl_label = "Siege: Classify for Web Export"
    bl_options = {'REGISTER', 'UNDO'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=(
            ('SCENE', "Whole Scene", "Classify every mesh object in the scene"),
            ('SELECTED', "Selected Only", "Classify only selected mesh objects"),
        ),
        default='SCENE',
    )

    def execute(self, context):
        summary, total_tris, unique_tris, unique_data, obj_count = (
            classify_scene(context, self.scope))

        dedup_ratio = (1.0 - unique_tris / total_tris) if total_tris else 0.0
        print(f"[SiegeWeb] Classified {obj_count} mesh objects across "
              f"{unique_data} unique datablocks")
        print(f"[SiegeWeb]   shell:      {summary['shell']}")
        print(f"[SiegeWeb]   large_prop: {summary['large_prop']}")
        print(f"[SiegeWeb]   small_prop: {summary['small_prop']}")
        print(f"[SiegeWeb]   tiny:       {summary['tiny']}")
        print(f"[SiegeWeb] Triangles: {total_tris:,} placed / "
              f"{unique_tris:,} unique  (dedup saves {dedup_ratio*100:.1f}%)")

        self.report({'INFO'},
                    f"Classified {obj_count} objects — "
                    f"shell={summary['shell']} "
                    f"large={summary['large_prop']} "
                    f"small={summary['small_prop']} "
                    f"tiny={summary['tiny']}")
        return {'FINISHED'}


class SIEGE_PT_web_panel(bpy.types.Panel):
    """The web-export pipeline, nested under the map tab's root panel.

    Clean an imported map for web, bake, and export an optimized per-floor GLB
    set. Importing lives on the parent panel (`VIEW3D_PT_r6nr_map`) — this one
    starts where the import finishes.
    """
    bl_idname = "SIEGE_PT_web_panel"
    bl_label = "Web Export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "R6S Map"
    bl_parent_id = "VIEW3D_PT_r6nr_map"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False

        # --- The web-viewer pipeline, top to bottom in run order ---
        col = layout.column(align=True)
        col.label(text="Prepare", icon='MODIFIER')
        col.operator("siege.web_clean", text="1.  Clean for Web", icon='BRUSH_DATA')
        col.label(text="size-aware weld + planar decimate + separates decals", icon='BLANK1')

        # --- Decals (optional review tools) ---
        # The flat-plane detector is only a heuristic, so nothing here deletes:
        # "Separate" quarantines suspects into a '_Decals' collection for review,
        # and "Delete + Instances" is the reliable manual removal (pick one, wipe
        # every copy). The final delete happens at export.
        col = layout.column(align=True)
        col.separator()
        col.label(text="Decals  (optional)", icon='TRASH')
        row = col.row(align=True)
        op = row.operator("siege.web_cleanup_decals", text="Separate Decals",
                          icon='SELECT_SET')
        op.dry_run = False
        op.action = 'TAG'
        # small preview button beside it (dry run = just count)
        row.operator("siege.web_cleanup_decals", text="", icon='VIEWZOOM').dry_run = True
        col.operator("siege.delete_with_instances",
                     text="Delete Selected + All Instances", icon='X')
        col.label(text="select a decal → wipes every instance", icon='BLANK1')

        # --- Floors (optional): assign objects to explicit floor collections ---
        # Guarded so a partial registration (siege_floors prop missing) can't
        # break the whole panel.
        scene = context.scene
        if hasattr(scene, "siege_floors"):
            col = layout.column(align=True)
            col.separator()
            col.label(text="Floors  (optional)", icon='OUTLINER_COLLECTION')
            row = col.row()
            row.template_list("SIEGE_UL_floors", "", scene, "siege_floors",
                              scene, "siege_floors_index", rows=3)
            side = row.column(align=True)
            side.operator("siege.floor_add", text="", icon='ADD').from_selected = False
            side.operator("siege.floor_remove", text="", icon='REMOVE')
            side.separator()
            side.operator("siege.floor_move", text="", icon='TRIA_UP').direction = 'UP'
            side.operator("siege.floor_move", text="", icon='TRIA_DOWN').direction = 'DOWN'
            sub = col.row(align=True)
            sub.operator("siege.floor_assign_selected", text="Assign Sel",
                         icon='LINKED')
            sub.operator("siege.floor_select_members", text="Select",
                         icon='RESTRICT_SELECT_OFF')
            col.operator("siege.floor_add", text="New Floor From Selected",
                         icon='COLLECTION_NEW').from_selected = True
            col.operator("siege.floors_from_spaces", text="Auto Floors From Spaces",
                         icon='HOME')
            col.operator("siege.floors_from_height", text="Auto Floors From Height",
                         icon='SORTSIZE')
            if not any(it.collection for it in scene.siege_floors):
                # Say which fallback the export will actually use, so "None set"
                # doesn't read as "no floors" on a tagged scene.
                if has_floor_tags(scene):
                    col.label(text="None set -> export uses Space floor tags",
                              icon='INFO')
                else:
                    col.label(text="None set -> export auto-bands by height",
                              icon='INFO')

        col = layout.column(align=True)
        col.separator()
        col.label(text="Bake AO  (optional)", icon='SHADING_RENDERED')
        col.operator("siege.web_bake_ao", text="2.  Bake AO to Vertex Colors",
                     icon='OUTLINER_OB_LIGHT').scope = 'SCENE'
        col.label(text="Export bakes for you if skipped", icon='BLANK1')

        col = layout.column(align=True)
        col.separator()
        col.label(text="Export", icon='EXPORT')
        ex = col.column(align=True)
        ex.scale_y = 1.35
        ex.operator("siege.web_export_map", text="3.  Export Web Map",
                    icon='WORLD')
        col.label(text="→ per-floor GLBs + manifest for the viewer",
                  icon='BLANK1')


class SIEGE_PT_web_advanced(bpy.types.Panel):
    """Advanced / legacy web tools, tucked under the main Siege panel."""
    bl_idname = "SIEGE_PT_web_advanced"
    bl_label = "Advanced"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "R6S Map"
    bl_parent_id = "SIEGE_PT_web_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout

        col = layout.column(align=True)
        col.label(text="Single-file export", icon='EXPORT')
        col.operator("siege.web_export", text="Export Web GLB (single)")
        col.label(text="Draco + dedup, no manifest/floors.", icon='INFO')
        col.label(text="meshopt: web/node_meshopt/optimize.mjs")

        col = layout.column(align=True)
        col.separator()
        col.label(text="Classify (dev metadata)", icon='OBJECT_DATA')
        col.operator(SIEGE_OT_web_classify.bl_idname,
                     text="Classify Scene").scope = 'SCENE'
        col.operator(SIEGE_OT_web_classify.bl_idname,
                     text="Classify Selected").scope = 'SELECTED'

        obj = context.active_object
        if obj is not None and obj.get('web_size_class') is not None:
            sub = layout.box()
            sub.label(text=f"Active: {obj.name}", icon='OBJECT_DATA')
            sub.label(text=f"Class: {obj['web_size_class']}")
            sub.label(text=f"Diagonal: {obj['web_diagonal_m']} m")
            sub.label(text=f"Instances: {obj['web_instance_n']}")


_classes = (
    SIEGE_OT_web_classify,
    SIEGE_PT_web_panel,
    SIEGE_PT_web_advanced,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
