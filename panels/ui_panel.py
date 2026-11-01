"""Sidebar (N-panel) layout for R6S-NR-Tools.

The tooling is split across **three sidebar tabs**, one per workflow, because a
single tab had grown long enough that map work and character work buried each
other:

    R6S-NR-Tools    generic Ninja-Ripper rip cleanup — mesh/UV/material tools
                    that apply to anything, whatever it was ripped from
    R6S Map         .forge map import, Space floor/room filtering, web export
    R6S Character   Prism character group + animation import, Rigify IK rig

Each tab has one root panel here; the map sub-panels (`SIEGE_PT_web_panel` in
`siege/web/optimize.py`, `VIEW3D_PT_siege_floor_filter` in
`operators/attribute_filter.py`) parent to `VIEW3D_PT_r6nr_map`. Blender
resolves `bl_parent_id` at *registration* time, so this module must register
before those two — see the module order in the addon's `__init__.py`.

Panel `draw()` runs on every sidebar redraw, i.e. on mouse-move. Nothing in here
may walk the scene or copy mesh data: an imported Siege map is tens of thousands
of objects and a per-draw O(scene) scan reads as a hard freeze.
"""

import bpy  # type: ignore
from bpy.types import Panel  # type: ignore

# Sidebar tab names. Changing one moves every panel that uses it.
CAT_TOOLS = 'R6S-NR-Tools'
CAT_MAP = 'R6S Map'
CAT_CHAR = 'R6S Character'


# ---------------------------------------------------------------------------
# Tab 1 — generic rip cleanup
# ---------------------------------------------------------------------------

class NODE_PT_AutoSetupPanel(Panel):
    """Rip cleanup tools that apply to any Ninja Ripper capture.

    Map- and character-specific tooling lives on its own tab; keep this one to
    things that are useful regardless of what was ripped.
    """
    bl_label = "R6S-NR-Tools"
    bl_idname = "VIEW3D_PT_r6nr_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = CAT_TOOLS

    def draw(self, context):
        layout = self.layout
        scene = context.scene

    # Create a box for mesh cleanup
        box = layout.box()
        box.label(text="Mesh Cleanup")
        row = box.row()
        row.operator("object.delete_flat_artifacts", text="Delete Flat Artifacts")
        row = box.row()
        row.operator("object.delete_without_texture", text="Delete Objects Without Texture")
        row = box.row()
        row.prop(scene, "boundary_merge_distance", text="Merge Distance")
        row = box.row()
        row.operator("mesh.mark_boundary_and_merge", text="Merge & Keep Normals")
        row = box.row()
        row.prop(scene, "proxy_target_percentage", text="Target %")
        row = box.row()
        row.operator("mesh.create_proxy", text="Create Proxy Mesh")

    # Create Section for Multi Rip Cleanup
        row = box.row()
        row.label(text="Multi Rip Cleanup:")
        row = box.row()
        row.operator("object.merge_duplicate_materials", text="Merge Duplicate Materials")
        row = box.row()
        row.operator("object.delete_duplicate_objects", text="Delete Duplicate Objects")

    # Create a box for Similar Selection and Instancing
        box = layout.box()
        box.label(text="Similar Mesh Tools")
        row = box.row()
        row.operator("object.select_similar_vertex_material", text="Select Similar (Verts + Mats)")
        row = box.row()
        row.operator("object.instance_similar_by_obb", text="Instance Similar (OBB Rotation)")
        row = box.row()
        row.operator("object.create_smallest_bounding_box", text="Create Smallest Bounding Box")

    # Create a box for Alignment Tools
        box = layout.box()
        box.label(text="Alignment Tools:")
        obj = context.object
        if obj and obj.type == 'MESH':
            align_props = scene.align_props
            row = box.row()
            row.prop(align_props, "align_plane", text="Target Plane")
            row = box.row()
            row.operator("object.align_to_plane", text="Align On Target Plane")
            # Vertex-selection state, WITHOUT touching the mesh. `total_vert_sel`
            # is a counter Blender already maintains, so it stays O(1) on a
            # million-vert map object; building a bmesh here (as this once did)
            # copied the whole mesh on every redraw and froze the sidebar.
            # It only tracks edit-mode selection, so in object mode leave the
            # button live — `object.move_to_gizmo` reports for itself when
            # nothing is selected.
            row = box.row()
            if obj.mode == 'EDIT':
                selected_verts = obj.data.total_vert_sel > 0
                row.enabled = selected_verts
                row.operator("object.move_to_gizmo", text="Move to Gizmo")
                if not selected_verts:
                    box.label(text="No vertices selected.", icon='ERROR')
            else:
                row.operator("object.move_to_gizmo", text="Move to Gizmo")
        else:
            box.label(text="Select a valid mesh object.", icon='ERROR')

    # Create a box for UV Tools
        box = layout.box()
        box.label(text="UV tools")
        row = box.row()
        row.prop(scene.uv_settings, "layer_name", text="UV Layer Name")
        row = box.row()
        row.operator("object.set_active_uv", text="Set Active UV").uv_name = scene.uv_settings.layer_name
        row = box.row()
        row.operator("object.uv_cleanup", text="Delete unsued UV-s", icon='ERROR')

    # Create a box for Finding Missing Textures
        box = layout.box()
        box.label(text="Find Missing Textures For Selected:")
        settings = scene.texture_import_settings
        row = box.row()
        row.prop(settings, "log_file_path")
        row = box.row()
        row.prop(settings, "texture_folder")
        row = box.row()
        row.enabled = bool(settings.log_file_path and settings.texture_folder)
        row.operator("texture.find_missing_textures")

    # Create a box for Material and Lighting Setup
        box = layout.box()
        box.label(text="Material and Lighting Setup")

        row = box.row()
        row.scale_y = 2.0
        row.operator("object.create_lights_from_material", text="Create Lights From Material")

        row = box.row()
        row.prop(scene.default_config_settings, "default_config", text="Material Setup")
        row.operator("node.auto_setup_config_adjustment", text="", icon="GREASEPENCIL")
        row = box.row()
        row.scale_y = 2.0
        row.operator("node.auto_setup_node_group", text="Auto Setup Node Group")

        row = box.row()
        row.operator("object.load_all_materials", text="Load All Materials", icon='MATERIAL')

    # Create a box for Override Color Assignment
        box = layout.box()
        box.label(text="Assign Override Color:")
        row = box.row()
        button = row.operator("wm.call_menu", text="Select Mesh By Material")
        button.name = "select_by_material_menu"
        row = box.row()
        row.prop(context.scene, "override_color", text="Color")
        row = box.row()
        row.operator("object.set_override_color")
        row = box.row()
        row.operator("object.copy_color")


# ---------------------------------------------------------------------------
# Tab 2 — Siege maps
# ---------------------------------------------------------------------------

class VIEW3D_PT_r6nr_map(Panel):
    """Root of the map tab; the web-export and floor-filter panels nest under it.

    Only the entry points live here — importing a .forge, and the legacy manual
    plane split for scenes that carry no Space tags. Everything downstream of
    the import is a sub-panel so the tab reads top-to-bottom in run order.
    """
    bl_label = "Siege Map"
    bl_idname = "VIEW3D_PT_r6nr_map"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = CAT_MAP

    def draw(self, context):
        layout = self.layout

        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("import_scene.siege_map",
                     text="Import Siege Map (.forge)", icon='IMPORT')
        col.label(text="v29-v34, incl. Y11S2", icon='BLANK1')

        box = layout.box()
        box.label(text="Floors", icon='HOME')
        col = box.column(align=True)
        col.scale_y = 1.3
        col.operator("object.auto_separate_floors",
                     text="Auto Separate Floors", icon='OUTLINER_COLLECTION')
        col.label(text="retag by height + rebuild floor collections",
                  icon='BLANK1')
        if not context.scene.get('siege_floor_table'):
            box.label(text="Import a map first (needs floor tags).", icon='INFO')

        box = layout.box()
        box.label(text="Manual Floor Split", icon='MOD_BEVEL')
        active = context.active_object
        if active is not None:
            box.label(text=f"Plane: {active.name}", icon='MESH_PLANE')
            box.operator("object.separate_by_floor_plane",
                         text="Separate By Floor Plane")
        else:
            box.label(text="Select a plane object first.", icon='ERROR')
        box.label(text="For scenes with no Space floor tags.", icon='INFO')


# ---------------------------------------------------------------------------
# Tab 3 — Prism character assets
# ---------------------------------------------------------------------------

class VIEW3D_PT_r6nr_character(Panel):
    """Prism character group + animation import, and the material setup for them.

    The material buttons are the same operators as the main tab's — they read
    `scene.default_config_settings`, so setting the config here sets it there.
    They are repeated because a character import is followed by a material setup
    every single time.
    """
    bl_label = "Prism Character"
    bl_idname = "VIEW3D_PT_r6nr_character"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = CAT_CHAR

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Import", icon='IMPORT')
        col = box.column(align=True)
        col.scale_y = 1.5
        col.operator("import_scene.prism_group",
                     text="Import Character Group", icon='OUTLINER_OB_ARMATURE')
        col.label(text="a Prism export folder (mesh + rig + materials)",
                  icon='BLANK1')

        col = box.column(align=True)
        col.separator()
        col.scale_y = 1.2
        col.operator("import_scene.prism_anim",
                     text="Import Animation (.prismanim)", icon='ANIM')
        col.label(text="onto the selected armature", icon='BLANK1')

        # IK rig (operators/rig_character.py). poll() on these looks only at the active object
        # and the armature it points to, never the scene, so drawing them stays cheap.
        box = layout.box()
        box.label(text="IK Rig (Rigify)", icon='CON_KINEMATIC')
        col = box.column(align=True)
        col.scale_y = 1.5
        col.operator("object.prism_build_ik_rig", text="Build IK Rig", icon='OUTLINER_OB_ARMATURE')
        col = box.column(align=True)
        row = col.row(align=True)
        row.operator("object.prism_regenerate_ik_rig", text="Regenerate", icon='FILE_REFRESH')
        row.operator("object.prism_toggle_ik_rig", text="Mute / Unmute", icon='HIDE_OFF')
        row = col.row(align=True)
        row.operator("object.prism_bake_ik_rig", text="Bake to Skeleton", icon='ACTION')
        row.operator("object.prism_remove_ik_rig", text="", icon='TRASH')

        box = layout.box()
        box.label(text="Materials", icon='MATERIAL')
        row = box.row()
        row.prop(scene.default_config_settings, "default_config", text="")
        row.operator("node.auto_setup_config_adjustment", text="", icon="GREASEPENCIL")
        row = box.row()
        row.scale_y = 1.5
        row.operator("node.auto_setup_node_group", text="Auto Setup Node Group")
        row = box.row()
        row.operator("object.load_all_materials", text="Load All Materials")

        arm = context.active_object
        if arm is not None and arm.type == 'ARMATURE':
            box = layout.box()
            box.label(text=f"Rig: {arm.name}", icon='ARMATURE_DATA')
            box.label(text=f"{len(arm.data.bones)} bones", icon='BONE_DATA')


class NODE_MT_MaterialSelectionPopup(bpy.types.Menu):
    bl_label = "Select By Material"
    bl_idname = "select_by_material_menu"

    def draw(self, context):
        layout = self.layout
        layout.operator("object.select_objects_containing_materials", text="Every Material On Selected")
        layout.operator("object.select_objects_containging_selected_material", text="Active Material")


class DefaultConfigSettings(bpy.types.PropertyGroup):
    default_config: bpy.props.EnumProperty(
        name="Material Setup",
        description="Select material setup type",
        items=[
            ("MAP", "Map Material", "Map materials with Siege Object BSDF"),
            ("CHAR", "Character Material", "Character materials with Siege Character BSDF"),
            ("GUN", "Gun/Gadget Material", "Gun/Gadget materials with Siege Weapon BSDF")
        ],
        default="MAP"
    ) # type: ignore


class TextureImportSettings(bpy.types.PropertyGroup):
    log_file_path: bpy.props.StringProperty(
        name="Log File Path",
        description="Path to the log file",
        subtype='FILE_PATH',
        default=""
    )# type: ignore
    texture_folder: bpy.props.StringProperty(
        name="Texture Folder",
        description="Path to the folder containing textures",
        subtype='DIR_PATH',
        default=""
    )# type: ignore


plane_items = [
    ('XY', "XY", "Align to XY plane"),
    ('YZ', "YZ", "Align to YZ plane"),
    ('XZ', "XZ", "Align to XZ plane"),
    ('-XY', "-XY", "Align to -XY plane"),
    ('-YZ', "-YZ", "Align to -YZ plane"),
    ('-XZ', "-XZ", "Align to -XZ plane"),
]

class AlignmentSettings(bpy.types.PropertyGroup):
    align_plane: bpy.props.EnumProperty(
        name="Align Plane",
        description="Choose the plane to align the face to",
        items=plane_items,
        default='XY'
    )# type: ignore


class UvNamePropperty(bpy.types.PropertyGroup):
    layer_name: bpy.props.StringProperty(
        name="UV Layer Name",
        description="Name of the UV layer to set as active render",
        default="uv_2"
    )# type: ignore

class ShaderTypeSettings(bpy.types.PropertyGroup):
    shader_type: bpy.props.EnumProperty(
        name="Shader Type",
        description="Choose the shader type for material setup",
        items=[
            ('Siege Object BSDF', "Siege Object BSDF", "Standard object shader"),
            ('Siege Character BSDF', "Siege Character BSDF", "Character shader"),
            ('Siege Weapon BSDF', "Siege Weapon BSDF", "Weapon shader"),
        ],
        default='Siege Object BSDF',
    ) # type: ignore



def register():

    bpy.types.Scene.proxy_target_percentage = bpy.props.FloatProperty(
        name="Target %",
        description="Target percentage of faces to keep per object",
        default=5.0,
        min=0.1,
        max=100.0,
        precision=1,
    )

    bpy.types.Scene.boundary_merge_distance = bpy.props.FloatProperty(
        name="Merge Distance",
        description="Distance threshold for merging vertices",
        default=0.0001,
        min=0.0,
        max=1.0,
        precision=4,
        step=0.01
    )

    bpy.utils.register_class(NODE_PT_AutoSetupPanel)
    bpy.utils.register_class(VIEW3D_PT_r6nr_map)
    bpy.utils.register_class(VIEW3D_PT_r6nr_character)
    bpy.utils.register_class(DefaultConfigSettings)
    bpy.utils.register_class(NODE_MT_MaterialSelectionPopup)


    bpy.types.Scene.default_config_settings = bpy.props.PointerProperty(type=DefaultConfigSettings)
    bpy.utils.register_class(TextureImportSettings)
    bpy.types.Scene.texture_import_settings = bpy.props.PointerProperty(type=TextureImportSettings)
    bpy.utils.register_class(AlignmentSettings)
    bpy.types.Scene.align_props = bpy.props.PointerProperty(type=AlignmentSettings)
    bpy.utils.register_class(UvNamePropperty)
    bpy.types.Scene.uv_settings = bpy.props.PointerProperty(type=UvNamePropperty)
    bpy.utils.register_class(ShaderTypeSettings)
    bpy.types.Scene.shader_settings = bpy.props.PointerProperty(type=ShaderTypeSettings)


def unregister():

    del bpy.types.Scene.proxy_target_percentage
    del bpy.types.Scene.boundary_merge_distance

    bpy.utils.unregister_class(VIEW3D_PT_r6nr_character)
    bpy.utils.unregister_class(VIEW3D_PT_r6nr_map)
    bpy.utils.unregister_class(NODE_PT_AutoSetupPanel)
    bpy.utils.unregister_class(DefaultConfigSettings)
    bpy.utils.unregister_class(NODE_MT_MaterialSelectionPopup)


    del bpy.types.Scene.default_config_settings
    bpy.utils.unregister_class(TextureImportSettings)
    del bpy.types.Scene.texture_import_settings
    bpy.utils.unregister_class(AlignmentSettings)
    del bpy.types.Scene.align_props
    bpy.utils.unregister_class(UvNamePropperty)
    del bpy.types.Scene.uv_settings
    bpy.utils.unregister_class(ShaderTypeSettings)
    del bpy.types.Scene.shader_settings
