# Copyright (C) 2025 [Nicholas Wierzbowski/Elbo Studio]

# This file is part of the Pivot Bridge for Blender.

# The Pivot Bridge for Blender is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses>.

import bpy
import os
import stat

from pivot_lib import engine
from pivot_lib import engine_state
from .classes import SceneAttributes
from bpy.props import PointerProperty

from pivot_lib import group_manager
from . import handlers
from .operators.operators import (
    Pivot_OT_Organize_Classified_Objects,
    Pivot_OT_Reset_Classifications,
    Pivot_OT_Upgrade_To_Pro,
)
from .operators.group_classification import Pivot_OT_Standardize_Selected_Groups
from .operators.object_classification import (
    Pivot_OT_Set_Origin_Selected_Objects,
    Pivot_OT_Align_Facing_Selected_Objects,
)
from .ui import Pivot_PT_Standard_Panel, Pivot_PT_Pro_Panel, Pivot_PT_Status_Panel, Pivot_PT_Configuration_Panel

classesToRegister = (
    SceneAttributes,
    Pivot_OT_Standardize_Selected_Groups,
    # Pivot_OT_Set_Origin_Selected_Groups,
    # Pivot_OT_Align_Facing_Selected_Groups,
    Pivot_OT_Set_Origin_Selected_Objects,
    Pivot_OT_Align_Facing_Selected_Objects,
    Pivot_OT_Organize_Classified_Objects,
    Pivot_OT_Reset_Classifications,
    Pivot_OT_Upgrade_To_Pro,
)

def _reset_sync_state() -> None:
    """Clear cached engine sync data so reloads start from scratch."""
    group_mgr = group_manager.get_group_manager()
    group_mgr.reset_state()
    engine_state.update_group_membership_snapshot({}, replace=True)
    handlers.clear_previous_scales()


def register():
    print("App Sandbox:", os.getenv("APP_SANDBOX_CONTAINER_ID"))
    print("Registering Pivot")
    
    # Stop any running engine from previous edition
    try:
        engine.stop_engine()
    except Exception as e:
        print(f"[Pivot] Note: Could not stop engine during register: {e}")
    
    for cls in classesToRegister:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pivot = PointerProperty(type=SceneAttributes)

    # Ensure engine binary is executable after zip install (zip extraction often drops exec bits)
    try:
        engine_path = engine.get_engine_binary_path()
        if engine_path and os.path.exists(engine_path) and os.name != 'nt':
            st = os.stat(engine_path)
            if not (st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
                os.chmod(engine_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                print("Fixed executable permissions on pivot engine binary (register)")
    except Exception as e:
        print(f"Note: Could not adjust permissions for engine binary during register: {e}")

    is_pro = False
    try:
        engine.get_engine_communicator()
        # Print Cython edition for debugging
        try:
            from pivot_lib import edition_utils
            edition_utils.print_edition()
            is_pro = edition_utils.is_pro_edition()
        except Exception as e:
            print(f"[Pivot] Could not print Cython edition: {e}")
            is_pro = False
    except RuntimeError as exc:
        print(f"[Pivot] Failed to start engine after loading file: {exc}")

    # Register name change callback for group management
    try:
        group_mgr = group_manager.get_group_manager()
        group_mgr.set_name_change_callback(handlers.on_group_name_changed)
    except Exception as e:
        print(f"[Pivot] Could not set group name change callback: {e}")

    bpy.utils.register_class(Pivot_PT_Status_Panel)
    bpy.utils.register_class(Pivot_PT_Configuration_Panel)

    # Always register standard panel
    bpy.utils.register_class(Pivot_PT_Standard_Panel)

    bpy.utils.register_class(Pivot_PT_Pro_Panel)

    # Register persistent handlers for engine lifecycle management
    if handlers.on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(handlers.on_load_pre)
    if handlers.on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(handlers.on_load_post)
    
    # Only register depsgraph update handler for Pro edition
    if is_pro:
        if handlers.on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(handlers.on_depsgraph_update)

    


def unregister():
    print("Unregistering Pivot")
    
    bpy.utils.unregister_class(Pivot_PT_Pro_Panel)
    bpy.utils.unregister_class(Pivot_PT_Standard_Panel)
    bpy.utils.unregister_class(Pivot_PT_Status_Panel)
    bpy.utils.unregister_class(Pivot_PT_Configuration_Panel)
    
    for cls in reversed(classesToRegister):  # Unregister in reverse order
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.pivot

    # Unregister all persistent handlers
    if handlers.on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(handlers.on_load_pre)
    if handlers.on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(handlers.on_load_post)
    # Only remove depsgraph update handler if it's registered (was only added for Pro edition)
    if handlers.on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(handlers.on_depsgraph_update)

    # Perform cleanup as if we're unloading a file
    _reset_sync_state()
    handlers.on_load_pre(None)
    engine.stop_engine()


if __name__ == "__main__":
    register()
