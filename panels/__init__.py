import bpy # type: ignore

from .ui_panel import NODE_PT_AutoSetupPanel

def register():
    bpy.register_class(NODE_PT_AutoSetupPanel)

def unregister():
    bpy.unregister_class(NODE_PT_AutoSetupPanel)
