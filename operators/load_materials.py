import bpy  # type: ignore
import os


def get_shader_file():
    """
    Return the absolute path to the bundled shader_groups.blend file.
    """
    addon_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(addon_path, "shader_groups.blend")


class NODE_OT_LoadAllMaterials(bpy.types.Operator):
    """
    Append all materials from the bundled shader_groups.blend file.
    """
    bl_idname = "object.load_all_materials"
    bl_label = "Load All Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        shader_file = get_shader_file()

        if not os.path.exists(shader_file):
            self.report({'ERROR'}, f"shader_groups.blend not found at {shader_file}")
            return {'CANCELLED'}

        existing = {mat.name for mat in bpy.data.materials}

        with bpy.data.libraries.load(shader_file, link=False) as (data_from, data_to):
            if not data_from.materials:
                self.report({'WARNING'}, "No materials found in shader_groups.blend.")
                return {'CANCELLED'}

            # Only append materials not already present, to avoid duplicates.
            to_append = [name for name in data_from.materials if name not in existing]
            skipped = len(data_from.materials) - len(to_append)
            data_to.materials = to_append

        appended = len(to_append)
        if appended and skipped:
            self.report({'INFO'}, f"Loaded {appended} material(s); {skipped} already present.")
        elif appended:
            self.report({'INFO'}, f"Loaded {appended} material(s) from shader_groups.blend.")
        else:
            self.report({'INFO'}, "All materials from shader_groups.blend are already present.")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(NODE_OT_LoadAllMaterials)


def unregister():
    bpy.utils.unregister_class(NODE_OT_LoadAllMaterials)
