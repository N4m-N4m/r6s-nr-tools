bl_info = {
    "name": "R6S NR Tools",
    "description": "Tool for importing and managing NR assets in R6, plus Siege .forge map import (incl. Y11S2/v34) and web GLB export.",
    "author": "dyn4micfx and nam-nam",
    "version": (3, 3, 0),
    "blender": (4, 1, 0),
    "location": "View3D > Sidebar > R6S-NR-Tools / R6S Map / R6S Character",
    "warning": "",
    "wiki_url": "http://my.wiki.url",
    "tracker_url": "http://my.bugtracker.url",
    "support": "COMMUNITY",
    "category": "Import-Export",
}

import bpy # type: ignore
import os
import importlib

from .operators import auto_setup, create_lights, delete_objects, mesh_cleanup, find_missing_textures, mesh_alignment, set_uv, uv_cleanup, multy_rip_cleanup, color_override, similar_instancing, load_materials, import_prism_group, import_prism_anim, import_prism_gun, rig_character, floor_separator, attribute_filter
from .panels import ui_panel
from . import siege

# ui_panel FIRST: it owns the three sidebar root panels, and Blender resolves a
# panel's `bl_parent_id` when the child class is registered — so the roots have
# to exist before attribute_filter and siege register their sub-panels under
# them. Everything else is order-independent.
modules = [ui_panel,
           auto_setup, create_lights, delete_objects, mesh_cleanup, find_missing_textures, mesh_alignment, set_uv, uv_cleanup, multy_rip_cleanup, color_override, similar_instancing, load_materials, import_prism_group, import_prism_anim, import_prism_gun, rig_character, floor_separator, attribute_filter
           , siege]

for module in modules:
    importlib.reload(module)

def register():
    for module in modules:
        module.register()

def unregister():
    # Reverse order, so a sub-panel is always torn down before the root panel
    # it parents to.
    for module in reversed(modules):
        module.unregister()

if __name__ == "__main__":
    register()
