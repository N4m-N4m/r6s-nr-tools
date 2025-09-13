import bpy  # type: ignore
import os
import json

# Global default config dictionaries.
DEFAULT_CONFIG_MAP = {
    2: {"USE-Premul": False, "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": 0, "PBR Multi": None,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None},
    3: {"USE-Premul": True,  "Override Color": None, "Override Strength": None, "Diffuse": 0, "Alpha Input": 0, "PBR Multi": 2,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None},
    4: {"USE-Premul": True,  "Override Color": 0,    "Override Strength": 3,    "Diffuse": 0, "Alpha Input": 0, "PBR Multi": 2,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None},
    5: {"USE-Premul": True,  "Override Color": 0,    "Override Strength": 3,    "Diffuse": 0, "Alpha Input": 0, "PBR Multi": 2,
        "Normal Base": 1, "Mix Factor": None, "Diffuse 2": None, "Alpha Input 2": None, "PBR Multi 2": None, "Normal Base 2": None},
    6: {"Override Color": None, "Override Strength": None, "Diffuse": 3, "Alpha Input": None, "PBR Multi": 5,
        "Normal Base": 4, "Mix Factor": 0, "Diffuse 2": 0, "Alpha Input 2": None, "PBR Multi 2": 2, "Normal Base 2": 1},
    7: {"Override Color": None, "Override Strength": 6, "Diffuse": 3, "Alpha Input": None, "PBR Multi": 5,
        "Normal Base": 4, "Mix Factor": 0, "Diffuse 2": 0, "Alpha Input 2": None, "PBR Multi 2": 2, "Normal Base 2": 1}
}

DEFAULT_CONFIG_OPERATOR = {
    2: {
        "USE-Premul": True,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": None,
        "Normal Base": 1,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": None
    },
    3: {
        "USE-Premul": True,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": 2,
        "Normal Base": 1,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": None
    },
    4: {
        "USE-Premul": False,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": 2,
        "Normal Base": 1,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": 3
    },
    5: {
        "USE-Premul": True,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": 3,
        "Normal Base": 2,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": 4
    },
    6: {
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 3,
        "Alpha Input": None,
        "PBR Multi": 5,
        "Normal Base": 4,
        "Mix Factor": 0,
        "Diffuse 2": 0,
        "Alpha Input 2": None,
        "PBR Multi 2": 2,
        "Normal Base 2": 1
    },
    7: {
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 3,
        "Alpha Input": None,
        "PBR Multi": 5,
        "Normal Base": 4,
        "Mix Factor": 0,
        "Diffuse 2": 0,
        "Alpha Input 2": None,
        "PBR Multi 2": 2,
        "Normal Base 2": 1
    }
}

DEFAULT_CONFIG_GUN = {
    2: {
        "USE-Premul": True,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": None,
        "Normal Base": 1,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": None
    },
    3: {
        "USE-Premul": True,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": 2,
        "Normal Base": 1,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": None
    },
    4: {
        "USE-Premul": False,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": 2,
        "Normal Base": 1,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": 3
    },
    5: {
        "USE-Premul": True,
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 0,
        "Alpha Input": 0,
        "PBR Multi": 3,
        "Normal Base": 2,
        "Mix Factor": None,
        "Diffuse 2": None,
        "Alpha Input 2": None,
        "PBR Multi 2": None,
        "Normal Base 2": 4
    },
    6: {
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 3,
        "Alpha Input": None,
        "PBR Multi": 5,
        "Normal Base": 4,
        "Mix Factor": 0,
        "Diffuse 2": 0,
        "Alpha Input 2": None,
        "PBR Multi 2": 2,
        "Normal Base 2": 1
    },
    7: {
        "Override Color": None,
        "Override Strength": None,
        "Diffuse": 3,
        "Alpha Input": None,
        "PBR Multi": 5,
        "Normal Base": 4,
        "Mix Factor": 0,
        "Diffuse 2": 0,
        "Alpha Input 2": None,
        "PBR Multi 2": 2,
        "Normal Base 2": 1
    }
}

# Mapping for default config types.
DEFAULT_CONFIG_MAPPING = {
    "MAP": DEFAULT_CONFIG_MAP,
    "CHAR": DEFAULT_CONFIG_OPERATOR,
    "GUN": DEFAULT_CONFIG_GUN
}


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


def load_shader_groups():
    """
    Load the 'Siege Object BSDF' shader group.
    """
    shader_groups = ["Siege Object BSDF"]  # Add additional group names if needed
    for group_name in shader_groups:
        shader_group = append_shader_group(group_name)
        if shader_group:
            print(f"Shader group '{group_name}' loaded successfully.")
        else:
            print(f"Shader group '{group_name}' could not be loaded.")


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
            first_word = input_name.split(" ")[0]
            if first_word == "PBR" or first_word == "Normal":
                img_node.image.colorspace_settings.name = "Non-Color"
            if input_name == "Mix Factor":
                img_node.image.alpha_mode = 'STRAIGHT'

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
                        if node.type == "GROUP" and node.node_tree and node.node_tree.name == 'Siege Object BSDF':
                            already_existing = True

                    if not already_existing:
                        instantiate_group(tree.nodes, 'Siege Object BSDF')

                    node_group = None
                    for node in tree.nodes:
                        if "Image Texture" in node.name and node.image is not None:
                            node.image.alpha_mode = 'NONE'
                        if node.type == "GROUP" and node.node_tree and node.node_tree.name == 'Siege Object BSDF':
                            node_group = node
                            break

                    if node_group is not None:
                        img_nodes = [node for node in tree.nodes if node.type == "TEX_IMAGE"]
                        output_node = node_group.outputs["BSDF"]
                        input_node = tree.nodes["Material Output"].inputs["Surface"]

                        count = len(img_nodes)
                        config = config_switch.get(count, {})

                        for input_key, img_index in config.items():
                            if img_index is not None and img_index < count:
                                img_node = img_nodes[img_index]
                                use_premul = config.get("USE-Premul", False)
                                first_word = input_key.split(" ")[0]
                                if input_key in {"Override Strength", "PBR", "Mix Factor"} or first_word == "Alpha":
                                    dyn_genlink(input_key, "Alpha", img_node, use_premul=use_premul)
                                elif input_key != "USE-Premul":
                                    dyn_genlink(input_key, "Color", img_node)

                        tree.links.new(input_node, output_node)

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