import bpy # type: ignore
import os

class NODE_OT_FindMissingTextures(bpy.types.Operator):
    """Find missing textures for selected objects form NinjaRipper log file"""
    bl_idname = "texture.find_missing_textures"
    bl_label = "Find Missing Textures"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.texture_import_settings
        log_file_path = settings.log_file_path
        texture_folder = settings.texture_folder

        if not os.path.exists(log_file_path):
            self.report({'ERROR'}, f"Log file not found at {log_file_path}")
            return {'CANCELLED'}
        if not os.path.exists(texture_folder):
            self.report({'ERROR'}, f"Texture folder not found at {texture_folder}")
            return {'CANCELLED'}

        count = 0

        # Iterate over selected objects
        for obj in bpy.context.selected_objects:

            if obj.type == "MESH":

                for mat_slot in obj.material_slots:

                    mat = mat_slot.material

                    if not mat: continue

                    # Gather object name and textures
                    object_name = obj.name.split('.')[0]  # Trims .001, .002, etc. from the object name

                    textures_from_log = get_textures_for_object(log_file_path,texture_folder, object_name)

                    # Ensure textures are added to the material
                    count = ensure_textures_in_material(mat, textures_from_log, texture_folder)

        self.report({'INFO'}, f"{count} missing textures appended.")
        return {'FINISHED'}


def get_textures_for_object(log_file_path, texture_folder, object_name):
    """
    Extracts all texture names associated with the given object in the log file.
    """
    # Normalize paths for comparison
    normalized_texture_folder = os.path.normpath(texture_folder)

    # Read the log file and extract relevant lines
    lines = []
    with open(log_file_path, "r") as log_file:
            for line in log_file:
                current_line = line.split(' ')
                if len(current_line) > 3 and "Mesh(s)" == current_line[2]:
                    lines.append(line)
                elif len(current_line) > 3 and "---Gathered" == current_line[2]:
                    lines.append(line)
                elif len(current_line) >= 4 and "File" == current_line[3].split('=')[0]:
                    lines.append(line)
    
    # Locate the section for the specific object
    relevant_textures = []
    section_found = False
    start_index = -1

    # Find the mesh section
    for idx, line in enumerate(lines):
        if normalized_texture_folder in line and object_name in line:
            section_found = True
            start_index = idx
            break

    if not section_found:
        print("Section not found")

    # Search upward from the starting point
    for i in range(start_index - 1, -1, -1):
        if "---Gathered textures---" in lines[i]:
            break
        if "File=" in lines[i]:
            texture_name = lines[i].split("File=")[1].strip()
            relevant_textures.append(texture_name)

    return list(set(relevant_textures))


def ensure_textures_in_material(material, textures_from_log, texture_folder):
        """
        Ensures missing textures are added as image texture nodes to the material.
        """

        if not material.use_nodes:
            material.use_nodes = True

        nodes = material.node_tree.nodes
        try:
            existing_texture_names = [
                node.image.name.split(".")[0] for node in nodes if node.type == "TEX_IMAGE" and node.image
            ]
        except:
            print("No image textures found in material")

        count = 0

        for tex_file in textures_from_log:
            tex_name = tex_file.split(".")[0]

            if tex_name not in existing_texture_names:
                texture_path = os.path.join(texture_folder, tex_file)

                if os.path.exists(texture_path):
                    # Ensure the texture is imported into Blender
                    img = bpy.data.images.load(texture_path) if tex_name not in bpy.data.images else bpy.data.images[tex_name]

                    # Add a new image texture node to the material
                    tex_node = nodes.new("ShaderNodeTexImage")
                    tex_node.image = img

                    count += 1

        return count
                    

def register():
    bpy.utils.register_class(NODE_OT_FindMissingTextures)


def unregister():
    bpy.utils.unregister_class(NODE_OT_FindMissingTextures)