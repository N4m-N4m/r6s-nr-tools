import bpy # type: ignore
import os
import re

class NODE_OT_FindMissingTextures(bpy.types.Operator):
    """Find missing textures for selected objects from NinjaRipper log file"""
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

        # Extract frame folder from texture_folder path (e.g., "frame_0", "frame_1")
        frame_folder = extract_frame_folder(texture_folder)
        if not frame_folder:
            self.report({'WARNING'}, f"Could not detect frame folder (e.g., 'frame_0') in texture path: {texture_folder}")
            # Continue anyway, but warn the user

        total_count = 0

        # Iterate over selected objects
        for obj in bpy.context.selected_objects:

            if obj.type == "MESH":

                for mat_slot in obj.material_slots:

                    mat = mat_slot.material

                    if not mat: 
                        continue

                    # Gather object name and textures
                    # Trims .001, .002, etc. from the object name
                    object_name = obj.name.split('.')[0]

                    textures_from_log = get_textures_for_object(log_file_path, texture_folder, object_name, frame_folder)

                    # Ensure textures are added to the material
                    count = ensure_textures_in_material(mat, textures_from_log, texture_folder)
                    total_count += count

        self.report({'INFO'}, f"{total_count} missing textures appended.")
        return {'FINISHED'}


def extract_frame_folder(path):
    """
    Extracts the frame folder name (e.g., 'frame_0', 'frame_1') from a path.
    
    Example inputs:
        'D:\\moved_rips\\frame_0\\' -> 'frame_0'
        'C:/NinjaRipper/session/frame_1' -> 'frame_1'
        '/home/user/rips/frame_10/textures' -> 'frame_10'
    """
    # Normalize path separators
    normalized_path = path.replace('\\', '/')
    
    # Look for frame_N pattern in the path
    match = re.search(r'(frame_\d+)', normalized_path)
    if match:
        return match.group(1)
    return None


def extract_mesh_info_from_line(line):
    """
    Extracts the mesh filename and frame folder from a Mesh(s) saved line.
    
    Example input: "000033B4:0116/133656  Mesh(s) saved. File: G:\\NinjaRipper\\...\\frame_0\\mesh_10.nr grp0Id=0..."
    Returns: ("mesh_10", "frame_0") or (None, None) if not found
    """
    # Look for the pattern: File: <path>\<meshname>.nr or File: <path>/<meshname>.nr
    match = re.search(r'File:\s*(.+?\.nr)', line)
    if match:
        file_path = match.group(1).strip()
        # Replace Windows backslashes with forward slashes for cross-platform compatibility
        file_path = file_path.replace('\\', '/')
        
        # Extract the frame folder from the path
        frame_folder = extract_frame_folder(file_path)
        
        # Extract just the filename without extension
        filename = os.path.basename(file_path)
        # Remove the .nr extension
        mesh_name = os.path.splitext(filename)[0]
        
        return mesh_name, frame_folder
    return None, None


def get_textures_for_object(log_file_path, texture_folder, object_name, target_frame_folder):
    """
    Extracts all texture names associated with the given object in the log file.
    
    The log file format has:
    - "---Gathered textures---" followed by texture File= lines
    - Then "Mesh(s) saved. File: <full_path>\\frame_N\\mesh_N.nr ..." 
    
    We need to find the mesh line that matches our object_name AND frame folder,
    then collect all File= entries above it until we hit "---Gathered textures---"
    
    Args:
        log_file_path: Path to the NinjaRipper log file
        texture_folder: Path to the texture folder (used for context)
        object_name: The mesh name to search for (e.g., "mesh_10")
        target_frame_folder: The frame folder to match (e.g., "frame_0"), or None to match any
    """
    
    # Read the log file and extract relevant lines
    lines = []
    with open(log_file_path, "r", encoding='utf-8', errors='ignore') as log_file:
        for line in log_file:
            # Strip carriage return for Windows compatibility
            line = line.rstrip('\r\n')
            current_line = line.split(' ')
            
            # Match Mesh(s) lines
            if len(current_line) > 3 and "Mesh(s)" == current_line[2]:
                lines.append(line)
            # Match ---Gathered textures--- lines
            elif len(current_line) > 3 and "---Gathered" == current_line[2]:
                lines.append(line)
            # Match File= lines (texture entries under ---Gathered textures---)
            elif len(current_line) >= 4 and current_line[3].startswith("File="):
                lines.append(line)
    
    # Locate the section for the specific object
    relevant_textures = []
    section_found = False
    start_index = -1

    # Find the mesh section by comparing extracted mesh names AND frame folders
    for idx, line in enumerate(lines):
        if "Mesh(s)" in line:
            mesh_name, frame_folder = extract_mesh_info_from_line(line)
            
            # Check if mesh name matches
            if mesh_name and mesh_name == object_name:
                # If we have a target frame folder, also verify it matches
                if target_frame_folder is None or frame_folder == target_frame_folder:
                    section_found = True
                    start_index = idx
                    break

    if not section_found:
        if target_frame_folder:
            print(f"Section not found for object: {object_name} in {target_frame_folder}")
        else:
            print(f"Section not found for object: {object_name}")
        return []

    # Search upward from the starting point to collect textures
    for i in range(start_index - 1, -1, -1):
        if "---Gathered textures---" in lines[i]:
            break
        if "File=" in lines[i]:
            # Extract just the filename from the File= entry
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
    
    existing_texture_names = []
    try:
        existing_texture_names = [
            node.image.name.split(".")[0] for node in nodes if node.type == "TEX_IMAGE" and node.image
        ]
    except Exception as e:
        print(f"Error getting existing textures: {e}")

    count = 0

    for tex_file in textures_from_log:
        tex_name = tex_file.split(".")[0]

        if tex_name not in existing_texture_names:
            texture_path = os.path.join(texture_folder, tex_file)

            if os.path.exists(texture_path):
                # Ensure the texture is imported into Blender
                if tex_name in bpy.data.images:
                    img = bpy.data.images[tex_name]
                else:
                    img = bpy.data.images.load(texture_path)

                # Add a new image texture node to the material
                tex_node = nodes.new("ShaderNodeTexImage")
                tex_node.image = img

                count += 1
            else:
                print(f"Texture file not found: {texture_path}")

    return count
                    

def register():
    bpy.utils.register_class(NODE_OT_FindMissingTextures)


def unregister():
    bpy.utils.unregister_class(NODE_OT_FindMissingTextures)