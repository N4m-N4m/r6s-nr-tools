bl_info = {
    "name": "R6S NR Tools",
    "description": "Tool for importing and managing NR assets in R6.",
    "author": "dyn4micfx and nam-nam",
    "version": (3, 0, 1),
    "blender": (4, 1, 0),
    "location": "View3D > Sidebar > R6S-NR-Tools",
    "warning": "",
    "wiki_url": "http://my.wiki.url",
    "tracker_url": "http://my.bugtracker.url",
    "support": "COMMUNITY",
    "category": "Import-Export",
}

import bpy # type: ignore
import os
import importlib

from .operators import auto_setup, create_lights, delete_objects, find_missing_textures, mesh_alignment, set_uv, uv_cleanup, multy_rip_cleanup, color_override
from .panels import ui_panel

modules = [auto_setup, create_lights, delete_objects, find_missing_textures, mesh_alignment, set_uv, uv_cleanup, multy_rip_cleanup, color_override
           , ui_panel]

for module in modules:
    importlib.reload(module)

def register():
    for module in modules:
        module.register()

def unregister():
    for module in modules:
        module.unregister()

if __name__ == "__main__":
    register()
