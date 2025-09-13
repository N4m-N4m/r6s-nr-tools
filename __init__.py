bl_info = {
    "name": "Dyna R6 NR Importer",
    "description": "Tool for importing and managing NR assets in R6.",
    "author": "dyn4micfx",
    "version": (2, 0),
    "blender": (4, 1, 0),
    "location": "View3D > Sidebar > Dyna R6",
    "warning": "",
    "wiki_url": "http://my.wiki.url",
    "tracker_url": "http://my.bugtracker.url",
    "support": "COMMUNITY",
    "category": "Import-Export",
}

import bpy # type: ignore
import os
import importlib

from .operators import auto_setup, create_lights, delete_objects, find_missing_textures, mesh_alignment, set_uv, multy_rip_cleanup
from .panels import ui_panel

modules = [auto_setup, create_lights, delete_objects, find_missing_textures, mesh_alignment, set_uv, multy_rip_cleanup
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
