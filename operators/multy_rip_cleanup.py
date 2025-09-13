import bpy # type: ignore
import bmesh # type: ignore


class NODE_PT_MergeDuplicateMaterials(bpy.types.Operator):
    bl_idname = "object.merge_duplicate_materials"
    bl_label = "Merge Duplicate Materials"
    bl_description = "Merge materials that use the same image textures"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Get all materials in the scene
        materials = bpy.data.materials
        merged_materials = set()  # To track merged materials
        checked_materials = set()  # To track materials that have already been compared

        count = 0

        # Loop over materials to compare them
        for mat1 in materials:
            
            # Skip if already merged or not using nodes
            if mat1 in merged_materials or not mat1.use_nodes or mat1 in checked_materials:
                continue

            for mat2 in materials:

                # Ensure mat2 has not been checked or merged
                if mat1 != mat2 and mat2 not in merged_materials and mat2.use_nodes:

                    if compare_materials(mat1, mat2):  

                        count += 1

                        # If they have the same image textures, keep the one with more nodes
                        if len(mat1.node_tree.nodes) >= len(mat2.node_tree.nodes):
                            merge_materials(mat1, mat2)
                            merged_materials.add(mat2)
                        else:
                            merge_materials(mat2, mat1)
                            merged_materials.add(mat1)
                            break
                        
                        
            checked_materials.add(mat1)

        for rem_mat in merged_materials:
            bpy.data.materials.remove(rem_mat)
        self.report({'INFO'}, f"{count} materials merged.")
        return {'FINISHED'}


def get_image_textures_from_material(material):
    """Get all image texture nodes used in a material"""

    image_textures = set()
    # Ensure the material has a node tree
    if material.use_nodes and material.node_tree:
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                image_textures.add(node.image.name.split('.')[0])
    return image_textures


def compare_materials(mat1, mat2):
    """Check if two materials use the same image textures"""

    textures1 = get_image_textures_from_material(mat1)
    textures2 = get_image_textures_from_material(mat2)
    return textures1 == textures2


def merge_materials(mat_keep, mat_remove):
    """Merge materials by replacing all instances of mat_remove with mat_keep, then delete mat_remove"""

    # Update all meshes that use the material to use the new one
    print(f"Merged material: {mat_keep.name} (kept), {mat_remove.name} (removed)")

    for obj in bpy.data.objects:
        if obj.type == 'MESH':
            for slot in obj.material_slots:
                if slot.material == mat_remove:
                    slot.material = mat_keep


class NODE_PT_DeleteDuplicateObjects(bpy.types.Operator):
    bl_idname = "object.delete_duplicate_objects"
    bl_label = "Delete Duplicate Objects"
    bl_description = "Delete duplicate objects based on their mesh distance and material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        DISTANCE_THRESHOLD = 0.01
        count = 0

        # Group objects by materials
        materials_dict = {}
        for obj in bpy.data.objects:
            if obj.type == 'MESH' and obj.data.materials and obj.visible_get():
                for mat in obj.data.materials:
                    if mat.name not in materials_dict:
                        materials_dict[mat.name] = []
                    materials_dict[mat.name].append(obj)

        # Process each material group
        for mat_name, objects in materials_dict.items():
            i = 0
            while i < len(objects):
                obj_a = objects[i]
                if obj_a.name not in bpy.data.objects:
                    objects.pop(i)
                    continue

                j = i + 1
                while j < len(objects):
                    obj_b = objects[j]
                    if obj_b.name not in bpy.data.objects:
                        objects.pop(j)
                        continue

                    # Check if vertices are fully matching
                    if are_vertices_fully_matching(obj_a, obj_b, DISTANCE_THRESHOLD):

                        count += 1

                        # Compare the number of material slots (panels) to decide which to keep
                        if len(obj_a.data.materials) >= len(obj_b.data.materials):
                            bpy.data.objects.remove(obj_b, do_unlink=True)
                            objects.pop(j)
                        else:
                            bpy.data.objects.remove(obj_a, do_unlink=True)
                            objects.pop(i)
                            break
                    else:
                        j += 1
                else:
                    i += 1

        self.report({'INFO'}, f"Deleted {count} duplicate objects.")
        return {'FINISHED'}


def are_vertices_fully_matching(obj_a, obj_b, distance_threshold):
    # Ensure both objects have the same vertex count
    if len(obj_a.data.vertices) != len(obj_b.data.vertices):
        return False

    # Convert objects to BMesh for vertex-level operations
    bm_a = bmesh.new()
    bm_b = bmesh.new()
    bm_a.from_mesh(obj_a.data)
    bm_b.from_mesh(obj_b.data)

    # Check that every vertex matches within the threshold
    for v_a, v_b in zip(bm_a.verts, bm_b.verts):
        world_v_a = obj_a.matrix_world @ v_a.co
        world_v_b = obj_b.matrix_world @ v_b.co
        if (world_v_a - world_v_b).length > distance_threshold:
            bm_a.free()
            bm_b.free()
            return False

    bm_a.free()
    bm_b.free()
    return True


def register():
    bpy.utils.register_class(NODE_PT_MergeDuplicateMaterials)
    bpy.utils.register_class(NODE_PT_DeleteDuplicateObjects)

def unregister():
    bpy.utils.unregister_class(NODE_PT_MergeDuplicateMaterials)
    bpy.utils.unregister_class(NODE_PT_DeleteDuplicateObjects)

