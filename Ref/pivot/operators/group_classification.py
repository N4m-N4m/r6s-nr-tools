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
import time

from ..constants import PRE, FINISHED
from pivot_lib import standardize
from pivot_lib import group_manager
from pivot_lib import engine_state
from ..classification_utils import get_qualifying_objects_for_selected, selected_has_qualifying_objects


class Pivot_OT_Standardize_Selected_Groups(bpy.types.Operator):
    """
    Pro Edition: Standardize Selected Groups
    
    Takes user selection, groups objects by collection boundaries and root parents,
    performs classification on entire groups with group guessing in the engine.
    """
    bl_idname = "object." + PRE.lower() + "standardize_selected_groups"
    bl_icon = 'OUTLINER_COLLECTION'
    license_type = engine_state.get_engine_license_status()
    bl_label = "Standardize & Classify Selected Assets"
    bl_description = "Analyzes the selection to identify asset hierarchies (parenting/collection-based) in the Source Collection. Runs the full standardization and classification process on each group, then creates a new, perfectly organized Outliner structure. This is the main 'processing' step for your scene"
    bl_options = {"REGISTER", "UNDO"}
    

    @classmethod
    def poll(cls, context):
        sel = getattr(context, "selected_objects", None) or []
        objects_collection = group_manager.get_group_manager().get_objects_collection()
        return selected_has_qualifying_objects(sel, objects_collection)

    def execute(self, context):
        # Exit edit mode if active to ensure mesh data is accessible
        if bpy.context.mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
        
        startTime = time.perf_counter()
        
        objects_collection = group_manager.get_group_manager().get_objects_collection()
        objects = get_qualifying_objects_for_selected(context.selected_objects, objects_collection)
        origin_method = context.scene.pivot.origin_method
        surface_type = context.scene.pivot.surface_type
        
        standardize.standardize_groups(
            objects, 
            origin_method=origin_method, 
            surface_context=surface_type
        )
        
        endTime = time.perf_counter()
        elapsed = endTime - startTime
        print(f"Standardize Selected Groups completed in {(elapsed) * 1000:.2f}ms")
        engine_state.set_performing_classification(True)
        return {FINISHED}