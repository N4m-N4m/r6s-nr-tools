import bpy # type: ignore


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


def register():
    bpy.utils.register_class(NODE_PT_MergeDuplicateMaterials)

def unregister():
    bpy.utils.unregister_class(NODE_PT_MergeDuplicateMaterials)

