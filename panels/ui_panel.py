import bpy # type: ignore
import bmesh # type: ignore
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

        # Button for Delete Flat Artifacts
        row = box.row()
        row.operator("object.delete_flat_artifacts", text="Delete Flat Artifacts")

        # Button for Delete Objects Without Texture
        row = box.row()
        row.operator("object.delete_without_texture", text="Delete Objects Without Texture")


        # Access the AlignmentSettings property group
        align_props = scene.align_props

        # Create Section for Multi Rip Cleanup
        row = box.row()
        row.label(text="Multi Rip Cleanup:")

        # Button for Material Merge
        row = box.row()
        row.operator("object.merge_duplicate_materials", text="Merge Duplicate Materials")

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

            # Enable button only if there are selected vertices
            row = box.row()
            row.enabled = selected_verts
            row.operator("object.move_to_gizmo", text="Move to Gizmo")
            if not selected_verts:
                box.label(text="No vertices selected.", icon='ERROR')
        else:
            box.label(text="Select a valid mesh object.", icon='ERROR')


    #Create a box for Material and Lighting Setup
        box = layout.box()
        box.label(text="Material and Lighting Setup")

        row = box.row()
        row.prop(scene.uv_settings, "layer_name", text="UV Layer Name")
        row = box.row()
        row.operator("object.set_active_uv", text="Set Active UV").uv_name = scene.uv_settings.layer_name

        # Button for Auto Setup Node Group
        row = box.row()
        row.scale_y = 2.0  # Set the scale to make the button larger
        row.operator("node.auto_setup_node_group", text="Auto Setup Node Group")

        # Button for Create Lights From Material
        row = box.row()
        row.scale_y = 2.0  # Set the scale to make the button larger
        row.operator("object.create_lights_from_material", text="Create Lights From Material")


    #Create a box for Findig Missing Textures
        box = layout.box()
        box.label(text="Find Missing Textures For Selected:")

        # Settings for Find Missing Textures
        row = box.row()
        settings = scene.texture_import_settings

        # Property for log file path
        row = box.row()
        row.prop(settings, "log_file_path")

        # Property for log file path
        row = box.row()
        row.prop(settings, "texture_folder")

        # Button for Find Missing Textures
        row = box.row()
        row.enabled = bool(settings.log_file_path and settings.texture_folder)# Disable the button if the input fields are not set
        row.operator("texture.find_missing_textures")

class TextureImportSettings(bpy.types.PropertyGroup):
    log_file_path: bpy.props.StringProperty(
        name="Log File Path",
        description="Path to the log file",
        subtype='FILE_PATH',
        default=""
    ) # type: ignore

    texture_folder: bpy.props.StringProperty(
        name="Texture Folder",
        description="Path to the folder containing textures",
        subtype='DIR_PATH',
        default=""
    ) # type: ignore


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
        default='XY',
    ) # type: ignore

class UvNamePropperty(bpy.types.PropertyGroup):
    layer_name: bpy.props.StringProperty(
        name="UV Layer Name",
        description="Name of the UV layer to set as active render",
        default="uv_2",
    ) # type: ignore

def register():
    bpy.utils.register_class(NODE_PT_AutoSetupPanel)

    bpy.utils.register_class(TextureImportSettings)
    bpy.types.Scene.texture_import_settings = bpy.props.PointerProperty(type=TextureImportSettings)

    bpy.utils.register_class(AlignmentSettings)
    bpy.types.Scene.align_props = bpy.props.PointerProperty(type=AlignmentSettings)

    bpy.utils.register_class(UvNamePropperty)
    bpy.types.Scene.uv_settings  = bpy.props.PointerProperty(type=UvNamePropperty)

def unregister():
    bpy.utils.unregister_class(NODE_PT_AutoSetupPanel)

    bpy.utils.unregister_class(TextureImportSettings)
    del bpy.types.Scene.texture_import_settings

    bpy.utils.unregister_class(AlignmentSettings)
    del bpy.types.Scene.align_props

    bpy.utils.unregister_class(UvNamePropperty)
    del bpy.types.Scene.uv_settings 
