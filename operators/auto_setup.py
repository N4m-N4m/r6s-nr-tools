import bpy  # type: ignore
import os
import json

# Global default config dictionaries.
DEFAULT_CONFIG_MAP = {
    2: {
    "USE-Premul": False, "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": 0, "PBR Multi": None,"Normal Base": 1, 
    "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None,
    "Invert Base": True,
    "Invert Base 2": True,
    "Image_Properties": {
        0: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        1: {"colorspace": "Non-Color", "alpha_mode": "NONE"}
    }},
    3: {"USE-Premul": True,  "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": 0, "PBR Multi": 2,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None,
        "Invert Base": True,
        "Invert Base 2": True,
        "Image_Properties": {
        0: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        1: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        2: {"colorspace": "sRGB", "alpha_mode": "NONE"}
        }},
    4: {"USE-Premul": True,  "Override Color": 0,    "Override Strength": 3,    "Diffuse": 0, "Alpha Input": 0, "PBR Multi": 2,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None, 
        "Invert Base": True,
        "Invert Base 2": True,
        "Image_Properties": {
        0: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        1: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        2: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        3: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"}
        }},
    5: {"USE-Premul": True,  "Override Color": 0,    "Override Strength": 3,    "Diffuse": 0, "Alpha Input": 0, "PBR Multi": 2,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None,
        "Invert Base": True,
        "Invert Base 2": True,
        "Image_Properties": {
        0: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        1: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        2: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        3: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"}
        }},
    6: {"USE-Premul": True, "Override Color": None, "Override Strength": None, "Diffuse": 3, "Alpha Input": None, "PBR Multi": 5,
        "Normal Base": 4, "Mix Factor": 0, "Diffuse 2": 0, "Alpha Input 2": None, "PBR Multi 2": 2, "Normal Base 2": 1,
        "Invert Base": True,
        "Invert Base 2": True,
        "Image_Properties": {
        0: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        1: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        2: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        3: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        4: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        5: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        }},
    7: {"USE-Premul": True, "Override Color": None, "Override Strength": 6, "Diffuse": 3, "Alpha Input": None, "PBR Multi": 5,
        "Normal Base": 4, "Mix Factor": 0, "Diffuse 2": 0, "Alpha Input 2": None, "PBR Multi 2": 2, "Normal Base 2": 1,
        "Invert Base": True,
        "Invert Base 2": True,
        "Image_Properties": {
        0: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        1: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        2: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        3: {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        4: {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        5: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        6: {"colorspace": "sRGB", "alpha_mode": "NONE"},
        }}
}

DEFAULT_CONFIG_OPERATOR = {
    2: {"USE-Premul": True, "Alpha Input": 0, "Diffuse": 0, "PBR Multi": None, "Normal Base": 1, 
        "Detail Strength": None, "ID map": None,"Normal Detail R": None,  "Normal Detail G": None,  "Normal Detail B": None,
        "Invert Base": True, "Invert Detail R": False, "Invert Detail G": False, "Invert Detail B": False,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"}
        }},
    5: {"USE-Premul": True, "Alpha Input": None, "Diffuse": 1, "PBR Multi": 3, "Normal Base": 2,
        "Detail Strength": 1.00, "ID map": None, "Normal Detail R": 0, "Normal Detail G": 4, "Normal Detail B": None,
        "Invert Base": True, "Invert Detail R": False, "Invert Detail G": False, "Invert Detail B": False,
        "Image_Properties": {
            "0": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "1": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "2": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "3": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "4": {"colorspace": "sRGB", "alpha_mode": "NONE"},
        }},
    7: {"USE-Premul": True, "Alpha Input": None, "Diffuse": 6, "PBR Multi": None, "Normal Base": 7,
        "Detail Strength": 1.00, "ID map": None, "Normal Detail R": 1, "Normal Detail G": 3, "Normal Detail B": 5,
        "Invert Base": True, "Invert Detail R": True, "Invert Detail G": True, "Invert Detail B": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "2": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "3": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "4": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "5": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "6": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "7": {"colorspace": "Non-Color", "alpha_mode": "NONE"}
        }},
    8: {"USE-Premul": True, "Alpha Input": None, "Diffuse": 6, "PBR Multi": None, "Normal Base": 7,
        "Detail Strength": 1.00, "ID map": None, "Normal Detail R": 1, "Normal Detail G": 3, "Normal Detail B": 5,
        "Invert Base": True, "Invert Detail R": True, "Invert Detail G": True, "Invert Detail B": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "2": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "3": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "4": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "5": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "6": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "7": {"colorspace": "Non-Color", "alpha_mode": "NONE"}
        }},
    9: {"USE-Premul": True, "Alpha Input": None, "Diffuse": 6, "PBR Multi": 8, "Normal Base": 7,
        "Detail Strength": 1.00, "ID map": None, "Normal Detail R": 1, "Normal Detail G": 3, "Normal Detail B": 5,
        "Invert Base": True, "Invert Detail R": True, "Invert Detail G": True, "Invert Detail B": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "2": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "3": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "4": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "5": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "6": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "7": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "8": {"colorspace": "sRGB", "alpha_mode": "NONE"}
        }},
    10: {"USE-Premul": True, "Alpha Input": None, "Diffuse": 6, "PBR Multi": 8, "Normal Base": 7,
        "Detail Strength": 1.00, "ID map": 9, "Normal Detail R": 1, "Normal Detail G": 3, "Normal Detail B": 5,
        "Invert Base": True, "Invert Detail R": True, "Invert Detail G": True, "Invert Detail B": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "2": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "3": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "4": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "5": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "6": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "7": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "8": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "9": {"colorspace": "sRGB", "alpha_mode": "NONE"},
        }},


        
}

DEFAULT_CONFIG_GUN = {
    1: {"USE-Premul": True, "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": 0, "PBR Multi": None,
        "Normal Base": None, "Detail Strength": None, "Normal Detail": None,
        "Invert Base": True,
        "Invert Detail": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
        }},
    3: {"USE-Premul": True, "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": None, "PBR Multi": 2,
        "Normal Base": 1, "Detail Strength": None, "Normal Detail": None,
        "Invert Base": True,
        "Invert Detail": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "2": {"colorspace": "sRGB", "alpha_mode": "NONE"},
        }},
    4: {"USE-Premul": True, "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": None, "PBR Multi": 2,
        "Normal Base": 1, "Detail Strength": 1.00, "Normal Detail": 3,
        "Invert Base": True,
        "Invert Detail": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "2": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "3": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        }},
    5: {"USE-Premul": True, "Override Color": None, "Override Strength": None, "Diffuse": 1, "Alpha Input": None, "PBR Multi": 3,
        "Normal Base": 2, "Detail Strength": 1.00, "Normal Detail": 4,
        "Invert Base": True,
        "Invert Detail": True,
        "Image_Properties": {
            "0": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "1": {"colorspace": "sRGB", "alpha_mode": "STRAIGHT"},
            "2": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
            "3": {"colorspace": "sRGB", "alpha_mode": "NONE"},
            "4": {"colorspace": "Non-Color", "alpha_mode": "NONE"},
        }},
}

# Mapping for default config types.
DEFAULT_CONFIG_MAPPING = {
    "MAP": DEFAULT_CONFIG_MAP,
    "CHAR": DEFAULT_CONFIG_OPERATOR,
    "GUN": DEFAULT_CONFIG_GUN
}

# Largest dimension (px) at or below which an unconnected image texture is
# treated as a "ripped" color/constant value and removed during cleanup.
UNUSED_TEXTURE_MAX_SIZE = 16

# Default scale applied to the detail-normal Mapping node (X/Y/Z).
DETAIL_NORMAL_MAPPING_SCALE = 7.0

# Largest dimension (px) at or below which a detail-normal texture is treated as
# a small tiling texture that should be scaled by the shared Mapping node. Larger
# detail normals (e.g. unique face textures like mat_77) are left on their UVs.
TILING_DETAIL_MAX_SIZE = 128

# Group inputs that receive tiling detail-normal textures and therefore share
# the Texture Coordinate -> Mapping node.
DETAIL_NORMAL_INPUTS = ("Normal Detail R", "Normal Detail G", "Normal Detail B")


def append_shader_group(group_name):
    """
    Append a shader group from the shader_groups.blend file.
    """
    if group_name in [ng.name for ng in bpy.data.node_groups]:
        return bpy.data.node_groups[group_name]

    addon_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shader_file = os.path.join(addon_path, "shader_groups.blend")

    print(f"Attempting to load shader group '{group_name}' from {shader_file}...")

    # Append the shader group from the .blend file
    with bpy.data.libraries.load(shader_file, link=False) as (data_from, data_to):
        if group_name in data_from.node_groups:
            data_to.node_groups.append(group_name)
            print(f"Shader group '{group_name}' loaded successfully.")
        else:
            print(f"Shader group '{group_name}' not found in {shader_file}.")

    return bpy.data.node_groups.get(group_name)

def get_shader_type_from_config(config_type):
    """
    Get the corresponding shader type based on config type.
    """
    shader_mapping = {
        'MAP': 'Siege Object BSDF',
        'CHAR': 'Siege Character BSDF V5',
        'GUN': 'Siege Weapon BSDF'
    }
    return shader_mapping.get(config_type, 'Siege Object BSDF')

def load_shader_groups():
    """
    Load all shader groups.
    """
    shader_groups = ["Siege Object BSDF", "Siege Character BSDF V5",
                     "Siege Eye BSDF", "Siege Weapon BSDF"]
    for group_name in shader_groups:
        shader_group = append_shader_group(group_name)
        if shader_group:
            print(f"Shader group '{group_name}' loaded successfully.")
        else:
            print(f"Shader group '{group_name}' could not be loaded.")


def _is_unused_texture(img_node):
    """
    A texture is considered an unused "ripped" color/constant value if it has an
    image whose largest dimension is at or below UNUSED_TEXTURE_MAX_SIZE.
    """
    img = img_node.image
    if img is None:
        return False
    return max(img.size[0], img.size[1]) <= UNUSED_TEXTURE_MAX_SIZE


def _is_tiling_detail(img_node):
    """
    A detail-normal texture is treated as a small tiling texture (and therefore
    scaled by the shared Mapping node) when its largest dimension is at or below
    TILING_DETAIL_MAX_SIZE. Larger detail normals are unique, UV-mapped textures
    (e.g. faces, see mat_77) and must keep their original UVs.
    """
    img = img_node.image
    if img is None:
        return False
    return max(img.size[0], img.size[1]) <= TILING_DETAIL_MAX_SIZE


def organize_character_material(tree, node_group):
    """
    Clean up and organize a character material after auto setup:
      * route the small tiling detail-normal textures through a shared Texture
        Coordinate + Mapping node (scaled by DETAIL_NORMAL_MAPPING_SCALE), while
        leaving large unique detail normals (e.g. faces) on their own UVs,
      * delete the tiny unused "ripped" textures that carry no real data,
      * lay the nodes out in a tidy, readable order.
    """
    nodes = tree.nodes
    links = tree.links

    # Collect the small tiling detail-normal image nodes that feed the shader
    # group. Large, unique detail normals are skipped so they keep their UVs.
    detail_nodes = []
    for input_name in DETAIL_NORMAL_INPUTS:
        if input_name not in node_group.inputs:
            continue
        socket = node_group.inputs[input_name]
        for link in socket.links:
            from_node = link.from_node
            if (from_node.type == 'TEX_IMAGE'
                    and _is_tiling_detail(from_node)
                    and from_node not in detail_nodes):
                detail_nodes.append(from_node)

    # Build (or reuse) the shared Texture Coordinate -> Mapping chain and feed
    # the Vector input of every detail-normal texture.
    if detail_nodes:
        tex_coord = next((n for n in nodes if n.type == 'TEX_COORD'), None)
        if tex_coord is None:
            tex_coord = nodes.new(type='ShaderNodeTexCoord')

        mapping = next((n for n in nodes if n.type == 'MAPPING'), None)
        if mapping is None:
            mapping = nodes.new(type='ShaderNodeMapping')
        mapping.inputs['Scale'].default_value = (
            DETAIL_NORMAL_MAPPING_SCALE,
            DETAIL_NORMAL_MAPPING_SCALE,
            DETAIL_NORMAL_MAPPING_SCALE,
        )

        if not mapping.inputs['Vector'].is_linked:
            links.new(mapping.inputs['Vector'], tex_coord.outputs['UV'])

        for img_node in detail_nodes:
            if not img_node.inputs['Vector'].is_linked:
                links.new(img_node.inputs['Vector'], mapping.outputs['Vector'])

    # Remove the tiny "ripped" textures. Detail normals are 128px+, so they are
    # never caught here; any links the rip may already have are dropped with it.
    for img_node in [n for n in nodes if n.type == 'TEX_IMAGE']:
        if _is_unused_texture(img_node):
            nodes.remove(img_node)

    # Lay everything out: real maps stacked on the left feeding the group,
    # detail normals below them, with the Texture Coordinate/Mapping chain
    # furthest to the left.
    group_input_order = [
        "Diffuse", "Normal Base", "PBR Multi", "ID map",
        "Normal Detail R", "Normal Detail G", "Normal Detail B",
    ]
    ordered = []
    seen = set()
    for input_name in group_input_order:
        if input_name in node_group.inputs:
            for link in node_group.inputs[input_name].links:
                src = link.from_node
                if src.type == 'TEX_IMAGE' and src.name not in seen:
                    ordered.append(src)
                    seen.add(src.name)
    # Any remaining image nodes keep their relative order at the end.
    for n in nodes:
        if n.type == 'TEX_IMAGE' and n.name not in seen:
            ordered.append(n)
            seen.add(n.name)

    for i, img_node in enumerate(ordered):
        img_node.location = (-950, 400 - i * 280)

    node_group.location = (0, 0)
    if "Material Output" in nodes:
        nodes["Material Output"].location = (300, 100)

    tex_coord = next((n for n in nodes if n.type == 'TEX_COORD'), None)
    mapping = next((n for n in nodes if n.type == 'MAPPING'), None)
    if mapping is not None:
        mapping.location = (-1350, -350)
    if tex_coord is not None:
        tex_coord.location = (-1550, -350)


class NODE_OT_AutoSetup(bpy.types.Operator):
    """
    Operator to automatically set up a node group for selected objects.
    """
    bl_idname = "node.auto_setup_node_group"
    bl_label = "Auto Setup Node Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):

        # sets default config to currently selected config
        set_default_config(context)

        # Ensure shader groups are loaded before use.
        load_shader_groups()

        # Get the selected config type and corresponding shader
        config_type = context.scene.default_config_settings.default_config
        selected_shader_type = get_shader_type_from_config(config_type)
        print(f"DEBUG: Config type: {config_type}, Shader type: {selected_shader_type}")  # Add this for debugging
        
        # Prepare the config_switch dictionary.
        text_name = "config_switch.json"
        if text_name in bpy.data.texts:
            config_text = bpy.data.texts[text_name]
            try:
                loaded_config = json.loads(config_text.as_string())
                # Convert keys back to integers.
                config_switch = {int(k): v for k, v in loaded_config.items()}
            except Exception as e:
                self.report({'WARNING'}, f"Error parsing {text_name}: {e}")
                # Fall back to a specific default config, e.g., "MAP"
                config_switch = DEFAULT_CONFIG_MAPPING["MAP"]
        else:
            # No config file found; create one using the "MAP" default.
            config_switch = DEFAULT_CONFIG_MAPPING["MAP"]
            config_text = bpy.data.texts.new(text_name)
            config_text.write(json.dumps(config_switch, indent=4))

        def instantiate_group(nodes, data_block_name):
            group = nodes.new(type='ShaderNodeGroup')
            shader_group = bpy.data.node_groups.get(data_block_name)
            if shader_group:
                group.node_tree = shader_group
            else:
                print(f"Failed to find shader group: {data_block_name}")
            return group

        def dyn_genlink(input_name, output_name, img_node, use_premul=None):
            if use_premul is not None and hasattr(img_node.image, 'alpha_mode'):
                img_node.image.alpha_mode = 'PREMUL' if use_premul else 'NONE'

            tree.links.new(node_group.inputs[input_name], img_node.outputs[output_name])

        for obj in context.selected_objects:
            if obj.type == 'MESH':
                mat = obj.active_material
                if mat is not None:
                    tree = mat.node_tree
                    already_existing = False

                    for node in tree.nodes:
                        if node.type == "GROUP" and node.node_tree and node.node_tree.name == selected_shader_type:
                            already_existing = True

                    if not already_existing:
                        instantiate_group(tree.nodes, selected_shader_type)

                    node_group = None
                    for node in tree.nodes:
                        if "Image Texture" in node.name and node.image is not None:
                            node.image.alpha_mode = 'NONE'
                        if node.type == "GROUP" and node.node_tree and node.node_tree.name == selected_shader_type:
                            node_group = node
                            break

                    if node_group is not None:
                        img_nodes = [node for node in tree.nodes if node.type == "TEX_IMAGE"]
                        output_node = node_group.outputs["BSDF"]
                        input_node = tree.nodes["Material Output"].inputs["Surface"]

                        count = len(img_nodes)
                        config = config_switch.get(count, {})

                        # Apply image properties from config
                        image_props = config.get("Image_Properties", {})
                        for img_index_str, properties in image_props.items():
                            img_index = int(img_index_str)  # Convert string key to int
                            if img_index < count:
                                img_node = img_nodes[img_index]
                                if "colorspace" in properties:
                                    img_node.image.colorspace_settings.name = properties["colorspace"]
                                if "alpha_mode" in properties:
                                    img_node.image.alpha_mode = properties["alpha_mode"]

                        for input_key, img_index in config.items():
                            if input_key != "Image_Properties":
                                # Handle boolean values (like invert flags)
                                if isinstance(img_index, bool):
                                    if input_key in node_group.inputs:
                                        node_group.inputs[input_key].default_value = img_index
                                
                                # Handle float values (like strength settings)
                                elif isinstance(img_index, float):
                                    if input_key in node_group.inputs:
                                        node_group.inputs[input_key].default_value = img_index
                                
                                # Handle image node connections
                                elif isinstance(img_index, int) and img_index < count:
                                    img_node = img_nodes[img_index]
                                    # For characters, never wire the tiny "ripped"
                                    # color/constant textures; they are cleaned up
                                    # by organize_character_material afterwards.
                                    if config_type == "CHAR" and _is_unused_texture(img_node):
                                        continue
                                    use_premul = config.get("USE-Premul", False)
                                    first_word = input_key.split(" ")[0]
                                    if input_key in {"Override Strength", "PBR", "Mix Factor"} or first_word == "Alpha":
                                        dyn_genlink(input_key, "Alpha", img_node, use_premul=use_premul)
                                    elif input_key != "USE-Premul":
                                        dyn_genlink(input_key, "Color", img_node)

                        tree.links.new(input_node, output_node)

                        # Character materials get extra cleanup and tidy layout:
                        # shared detail-normal mapping + removal of unused rips.
                        if config_type == "CHAR":
                            organize_character_material(tree, node_group)

        return {'FINISHED'}


class NODE_OT_AutoSetupConfigAdjustment(bpy.types.Operator):
    """
    Operator to load (or create) the config_switch JSON into a text editor for adjustment.
    """
    bl_idname = "node.auto_setup_config_adjustment"
    bl_label = "Adjust setup Config"
    bl_options = {'REGISTER'}

    def execute(self, context):
        text_name = "config_switch.json"
        if text_name in bpy.data.texts:
            text_block = bpy.data.texts[text_name]
        else:
            # Create the text block with the "MAP" default.
            text_block = bpy.data.texts.new(text_name)
            text_block.write(json.dumps(DEFAULT_CONFIG_MAPPING["MAP"], indent=4))
        # Attempt to set the active text block in a TEXT_EDITOR area.
        text_editor_found = False
        for area in context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces.active.text = text_block
                text_editor_found = True
                break
        if not text_editor_found:
            self.report({'INFO'}, "No Text Editor area found. Open one to view the config.")
        self.report({'INFO'}, f"Config loaded in text block '{text_name}'.")
        return {'FINISHED'}
 

def set_default_config(context):
    scene = context.scene
    # The dropdown property is stored in scene.default_config_settings.default_config.
    config_type = scene.default_config_settings.default_config
    config = DEFAULT_CONFIG_MAPPING.get(config_type, DEFAULT_CONFIG_MAPPING["MAP"])
    text_name = "config_switch.json"
    if text_name in bpy.data.texts:
        text_block = bpy.data.texts[text_name]
        text_block.clear()
    else:
        text_block = bpy.data.texts.new(text_name)
    text_block.write(json.dumps(config, indent=4))

def register():
    bpy.utils.register_class(NODE_OT_AutoSetupConfigAdjustment)
    bpy.utils.register_class(NODE_OT_AutoSetup)

def unregister():
    bpy.utils.unregister_class(NODE_OT_AutoSetupConfigAdjustment)
    bpy.utils.unregister_class(NODE_OT_AutoSetup)