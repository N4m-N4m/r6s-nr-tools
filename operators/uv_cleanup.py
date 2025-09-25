import bpy # type: ignore

class OBJECT_OT_CleanUpUVs(bpy.types.Operator):
    bl_idname = "object.uv_cleanup"
    bl_label = "Delete Unused UVs"
    bl_description = "Delete all unused (non active render) UV layers from selected mesh objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        
        total_removed_count = 0
        processed_count = 0
        
        # Process each selected object
        for obj in bpy.context.selected_objects:
            if obj.type != 'MESH':
                continue
                
            mesh = obj.data
            if not mesh.uv_layers:
                continue
                
            print(f"Processing object: {obj.name}")
            
            # Get the active render UV map (the one with the camera icon)
            active_render_uv = get_active_render_uv_map(obj)
            
            if not active_render_uv:
                print(f"  No UV maps found on this object")
                continue
                        
            # Remove all UV maps except the active render one
            uv_layers_to_remove = []
            for uv_layer in mesh.uv_layers:
                if uv_layer.name != active_render_uv:
                    uv_layers_to_remove.append(uv_layer.name)
            
            removed_count = 0
            for uv_name in uv_layers_to_remove:
                uv_layer = mesh.uv_layers.get(uv_name)
                if uv_layer:
                    mesh.uv_layers.remove(uv_layer)
                    removed_count += 1

            total_removed_count += removed_count
            print(f"  Removed {removed_count} UV maps")
            
            # Rename the remaining UV map to "UVMap"
            if mesh.uv_layers:
                remaining_uv = mesh.uv_layers.get(active_render_uv)
                if remaining_uv:
                    old_name = remaining_uv.name
                    if old_name != "UVMap":
                        remaining_uv.name = "UVMap"
                        print(f"  Renamed '{old_name}' to 'UVMap'")
                        
                        # Update material nodes that reference the old UV map name
                        updated_nodes = 0
                        for slot in obj.material_slots:
                            if slot.material and slot.material.use_nodes:
                                for node in slot.material.node_tree.nodes:
                                    if node.type == 'UVMAP' and node.uv_map == old_name:
                                        node.uv_map = "UVMap"
                                        updated_nodes += 1
                        
                        if updated_nodes > 0:
                            print(f"    Updated {updated_nodes} material node references")
                    else:
                        print(f"  UV map already named 'UVMap'")
            
            processed_count += 1
        
        self.report({'INFO'}, f"UV cleanup completed! {total_removed_count} UV maps removed from {processed_count} objects.")
        return {'FINISHED'}



def get_active_render_uv_map(obj):
    """Get the UV map that has active_render = True (the one with the camera icon)"""
    mesh = obj.data
    
    # Find the UV layer with active_render = True
    for uv_layer in mesh.uv_layers:
        if uv_layer.active_render:
            return uv_layer.name
    
    # Fallback: if no active_render found, return the first UV map
    if mesh.uv_layers:
        return mesh.uv_layers[0].name
    
    return None  


def register():
    bpy.utils.register_class(OBJECT_OT_CleanUpUVs)

def unregister():
    bpy.utils.unregister_class(OBJECT_OT_CleanUpUVs)