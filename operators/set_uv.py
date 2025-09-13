import bpy # type: ignore

class SetActiveUVOperator(bpy.types.Operator):
    bl_idname = "object.set_active_uv"
    bl_label = "Set Active UV"
    bl_description = "Set specified UV layer as active render and set UV index to 2"
    bl_options = {'REGISTER', 'UNDO'}

    uv_name: bpy.props.StringProperty(
        name="UV Layer Name",
        description="Name of the UV layer to set as active render",
        default="uv_2",
        ) # type: ignore

    def execute(self, context):
        uv_name = self.uv_name

        for obj in context.selected_objects:

            if obj.type == 'MESH' and obj.visible_get():

                # Ensure the object has the specified UV layer
                if uv_name in obj.data.uv_layers:

                    obj.data.uv_layers[uv_name].active_render = True

        self.report({'INFO'}, f"UV settings updated for '{uv_name}' on selected objects.")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SetActiveUVOperator)

def unregister():
    bpy.utils.unregister_class(SetActiveUVOperator)