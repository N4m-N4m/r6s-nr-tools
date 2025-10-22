import bpy # type: ignore
import bmesh # type: ignore

class MESH_OT_mark_boundary_and_merge(bpy.types.Operator):
    bl_idname = "mesh.mark_boundary_and_merge"
    bl_label = "Merge & Keep Sharp"
    bl_description = "Merge Split Objects by Distance while keeping boundary edges sharp"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
         # Get all selected mesh objects
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "No mesh objects selected")
            return {'CANCELLED'}
        
        # Get merge distance from scene property
        merge_distance = context.scene.boundary_merge_distance
        
        # Store the original mode and active object
        original_mode = context.object.mode if context.object else 'OBJECT'
        original_active = context.active_object
        
        total_boundary_count = 0
        processed_count = 0
        
        # PASS 1: Mark all boundary edges on all objects
        for obj in selected_objects:
            # Set as active object
            context.view_layer.objects.active = obj
            
            # Switch to edit mode
            bpy.ops.object.mode_set(mode='EDIT')
            
            # Get the mesh data
            me = obj.data
            bm = bmesh.from_edit_mesh(me)
            
            # Deselect all first
            bpy.ops.mesh.select_all(action='DESELECT')
            
            # Select boundary edges
            boundary_count = 0
            for edge in bm.edges:
                if edge.is_boundary:
                    edge.select = True
                    boundary_count += 1
            
            total_boundary_count += boundary_count
            
            # Update the mesh
            bmesh.update_edit_mesh(me)
            
            # Mark the selected edges as sharp
            if boundary_count > 0:
                bpy.ops.mesh.mark_sharp()
            
            # Free the bmesh
            bm.free()
            
            # Switch back to object mode
            bpy.ops.object.mode_set(mode='OBJECT')
        
        # PASS 2: Merge by distance on all objects
        for obj in selected_objects:
            # Set as active object
            context.view_layer.objects.active = obj
            
            # Switch to edit mode
            bpy.ops.object.mode_set(mode='EDIT')
            
            # Select all vertices for merge by distance
            bpy.ops.mesh.select_all(action='SELECT')
            
            # Merge by distance
            bpy.ops.mesh.remove_doubles(threshold=merge_distance)
            
            # Switch back to object mode
            bpy.ops.object.mode_set(mode='OBJECT')
            
            processed_count += 1
        
        # Restore original active object if it still exists
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active
        
        # Restore original mode
        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=original_mode)
        
        self.report({'INFO'}, f"Processed {processed_count} objects. Marked {total_boundary_count} boundary edges and merged by distance {merge_distance}")
        
        return {'FINISHED'}

def register():
    bpy.utils.register_class(MESH_OT_mark_boundary_and_merge)

def unregister():
    bpy.utils.unregister_class(MESH_OT_mark_boundary_and_merge)


    