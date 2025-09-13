import bpy # type: ignore
import bmesh # type: ignore
from mathutils import Vector # type: ignore

class NODE_PT_MoveToGizmo(bpy.types.Operator):
    bl_idname = "object.move_to_gizmo"
    bl_label = "Move to Gizmo"
    bl_description = "Move object so selected vertices' midpoint aligns with 3D gizmo"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "No valid mesh object selected")
            return {'CANCELLED'}

        # Switch to Edit mode to access vertex selection
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)

        # Retrieve the coordinates of the selected vertices
        selected_verts = [v.co for v in bm.verts if v.select]
        if not selected_verts:
            self.report({'ERROR'}, "No vertices selected")
            bpy.ops.object.mode_set(mode='OBJECT')
            return {'CANCELLED'}

        # Calculate the midpoint of the selected vertices
        midpoint = sum(selected_verts, Vector()) / len(selected_verts)

        # Return to Object mode
        bpy.ops.object.mode_set(mode='OBJECT')

        # Calculate the offset to move the object
        gizmo_position = context.scene.cursor.location
        offset = gizmo_position - obj.matrix_world @ midpoint

        # Move the object
        for obj in context.selected_objects:
            obj.location += offset

        return {'FINISHED'}

class NODE_PT_AlignToPlane(bpy.types.Operator):
    bl_idname = "object.align_to_plane"
    bl_label = "Align to Plane"
    bl_description = "Align the active objects to the selected plane based on the active face normal"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        bpy.ops.object.rotation_clear(clear_delta=False)

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "No mesh object selected")
            return {'CANCELLED'}

        # Ensure we're in Edit Mode
        bpy.ops.object.mode_set(mode='EDIT')
        mesh = bmesh.from_edit_mesh(obj.data)

        # Get the active face
        active_face = mesh.faces.active
        if active_face is None:
            self.report({'ERROR'}, "Please select the face on which you want to align the objects.")
            return {'CANCELLED'}

        # Determine the target normal based on the selected plane
        plane = context.scene.align_props.align_plane
        target_normals = {
            'XY': Vector((0, 0, 1)),
            'YZ': Vector((1, 0, 0)),
            'XZ': Vector((0, 1, 0)),
            '-XY': Vector((0, 0, -1)),
            '-YZ': Vector((-1, 0, 0)),
            '-XZ': Vector((0, -1, 0)),
        }
        target_normal = target_normals.get(plane)

        # Calculate the rotation needed to align the face normal with the target normal
        face_normal = active_face.normal
        rotation_quat = face_normal.rotation_difference(target_normal)

        # Apply the rotation to the objects
        bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.selected_objects:
            obj.rotation_mode = 'QUATERNION'
            obj.rotation_quaternion = rotation_quat @ obj.rotation_quaternion

        # Update the mesh
        bpy.context.view_layer.update()
        self.report({'INFO'}, f"Objects aligned to {plane} plane")
        return {'FINISHED'}

def register():
    bpy.utils.register_class(NODE_PT_MoveToGizmo)
    bpy.utils.register_class(NODE_PT_AlignToPlane)

def unregister():
    bpy.utils.unregister_class(NODE_PT_MoveToGizmo)
    bpy.utils.unregister_class(NODE_PT_AlignToPlane)