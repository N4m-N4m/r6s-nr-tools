import bpy # type: ignore

class NODE_OT_DeleteObjectsWithoutTexture(bpy.types.Operator):
    """
    Operator to delete objects without materials or textures.
    """
    bl_idname = "object.delete_without_texture"
    bl_label = "Delete Objects Without Texture"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Get a list of all objects in the current scene
        all_objects = bpy.context.scene.objects

        # Track the number of deleted objects
        deleted_count = 0

        # Loop through each object in the scene
        for obj in list(all_objects):
            # Check if the object has any material slots
            if len(obj.material_slots) == 0:
                # If the object doesn't have any materials, delete it
                bpy.data.objects.remove(obj, do_unlink=True)
                deleted_count += 1

        # Report how many objects were deleted
        if deleted_count > 0:
            self.report({'INFO'}, f"Deleted {deleted_count} objects without materials or textures.")
        else:
            self.report({'INFO'}, "No objects without materials or textures were found.")

        return {'FINISHED'}

class NODE_OT_DeleteFlatArtifactObjects(bpy.types.Operator):
    """Delete all objects that are flat on the Z dimension"""
    bl_idname = "object.delete_flat_artifacts"
    bl_label = "Delete Flat Artifacts"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):

        count = 0

        bpy.ops.object.select_all(action='DESELECT')

        for obj in bpy.data.objects:

            if obj.type == 'MESH' and obj.visible_get():

                if obj.dimensions.z == 0:

                    bpy.data.objects.remove(obj)
                    count += 1
            
        self.report({'INFO'}, f"Deleted {count} flat artifacts.")
        return {'FINISHED'}

def register():
    bpy.utils.register_class(NODE_OT_DeleteObjectsWithoutTexture)
    bpy.utils.register_class(NODE_OT_DeleteFlatArtifactObjects)

def unregister():
    bpy.utils.unregister_class(NODE_OT_DeleteObjectsWithoutTexture)
    bpy.utils.unregister_class(NODE_OT_DeleteFlatArtifactObjects)
