import bpy  # type: ignore
import bmesh  # type: ignore
from bpy.types import Panel # type: ignore

class NODE_PT_AutoSetupPanel(Panel):
    """
    UI Panel for DynaTools-R6
    """
    bl_label = "DynaTools-R6"
    bl_idname = "VIEW3D_PT_dyna_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'DynaTools-R6'

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

    # Access the AlignmentSettings property group
        align_props = scene.align_props

    # Create Section for Multi Rip Cleanup
        row = box.row()
        row.label(text="Multi Rip Cleanup:")
        row = box.row()
        row.operator("object.merge_duplicate_materials", text="Merge Duplicate Materials")
        row = box.row()
        row.operator("object.delete_duplicate_objects", text="Delete Duplicate Objects")

    # Create a box for Alignment Tools
        box = layout.box()
        box.label(text="Alignment Tools:")
        obj = context.object
        if obj and obj.type == 'MESH':
            row = box.row()
            row.prop(align_props, "align_plane", text="Target Plane")
            row = box.row()
            row.operator("object.align_to_plane", text="Align On Target Plane")
            # Check if any vertices are selected
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            selected_verts = any(v.select for v in bm.verts)
            bm.free()
            row = box.row()
            row.enabled = selected_verts
            row.operator("object.move_to_gizmo", text="Move to Gizmo")
            if not selected_verts:
                box.label(text="No vertices selected.", icon='ERROR')
        else:
            box.label(text="Select a valid mesh object.", icon='ERROR')

    # Create a box for Material and Lighting Setup
        box = layout.box()
        box.label(text="Material and Lighting Setup")
        row = box.row()
        row.prop(scene.uv_settings, "layer_name", text="UV Layer Name")
        row = box.row()
        row.operator("object.set_active_uv", text="Set Active UV").uv_name = scene.uv_settings.layer_name
        row = box.row()
        row.scale_y = 2.0
        row.operator("object.create_lights_from_material", text="Create Lights From Material")
        
        row = box.row()
        row.prop(scene.default_config_settings, "default_config", text="Default Config")
        row.operator("node.auto_setup_config_adjustment", text="", icon="GREASEPENCIL")
        row = box.row()
        row.scale_y = 2.0
        row.operator("node.auto_setup_node_group", text="Auto Setup Node Group")

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


class NODE_MT_MaterialSelectionPopup(bpy.types.Menu):
    bl_label = "Select By Material"
    bl_idname = "select_by_material_menu"

    def draw(self, context):
        layout = self.layout
        layout.operator("object.select_objects_containing_materials", text="Every Material On Selected")
        layout.operator("object.select_objects_containging_selected_material", text="Active Material")

# Define the property group for the dropdown.
class DefaultConfigSettings(bpy.types.PropertyGroup):
    default_config: bpy.props.EnumProperty(
        name="Default Config",
        description="Select a default configuration",
        items=[
            ("MAP", "Map Material Setup", "Default config for map materials"),
            ("CHAR", "Character Mat Setup", "Default config for character materials"),
            ("GUN", "Gun/Gadget Setup", "Default config for guns/gadget materials")
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


def register():
    bpy.utils.register_class(NODE_PT_AutoSetupPanel)
    bpy.utils.register_class(DefaultConfigSettings)
    bpy.utils.register_class(NODE_MT_MaterialSelectionPopup)


    bpy.types.Scene.default_config_settings = bpy.props.PointerProperty(type=DefaultConfigSettings)
    bpy.utils.register_class(TextureImportSettings)
    bpy.types.Scene.texture_import_settings = bpy.props.PointerProperty(type=TextureImportSettings)
    bpy.utils.register_class(AlignmentSettings)
    bpy.types.Scene.align_props = bpy.props.PointerProperty(type=AlignmentSettings)
    bpy.utils.register_class(UvNamePropperty)
    bpy.types.Scene.uv_settings = bpy.props.PointerProperty(type=UvNamePropperty)


def unregister():
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
