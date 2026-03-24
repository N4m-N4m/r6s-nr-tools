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

from re import S
import bpy
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

from .constants import PRE, CATEGORY, LICENSE_PRO
from .classes import LABEL_OBJECTS_COLLECTION, LABEL_ORIGIN_METHOD, LABEL_SURFACE_TYPE
from pivot_lib.engine_state import get_engine_license_status, set_engine_license_status
from pivot_lib import engine


class Pivot_PT_Status_Panel(bpy.types.Panel):
    bl_label = "Pivot Status"
    bl_idname = PRE + "_PT_status_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY  # Tab name in the N-Panel
    bl_order = 0  # Make it appear at the top

    def draw(self, context):
        layout = self.layout
        
        # Get license_type from cached engine status, sync if needed
        license_type = get_engine_license_status()
        if license_type == "UNKNOWN":
            try:
                license_type = engine.sync_license_mode()
                set_engine_license_status(license_type)
            except Exception as e:
                print(f"[Pivot] Failed to sync license: {e}")
                license_type = "UNKNOWN"
        
        # Show license selector
        self._draw_license_selector(layout, license_type)
    
    def _draw_license_selector(self, layout, license_type):
        """Draw the license type display (read-only)."""
        row = layout.row()
        row.label(text=f"License: {license_type}")

class Pivot_PT_Configuration_Panel(bpy.types.Panel):
    bl_label = "Pivot Configuration"
    bl_idname = PRE + "_PT_configuration_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY  # Tab name in the N-Panel

    def draw(self, context):
        layout = self.layout

        # Objects Collection selector
        row = layout.row()
        row.label(text=LABEL_SURFACE_TYPE)
        row = layout.row()
        row.prop(bpy.context.scene.pivot, "surface_type", expand=True)
        row = layout.row()
        row.label(text=LABEL_ORIGIN_METHOD)
        row = layout.row()
        row.prop(bpy.context.scene.pivot, "origin_method", expand=True)


class Pivot_PT_Pro_Panel(bpy.types.Panel):
    bl_label = "Scene Standardization"
    bl_idname = PRE + "_PT_pro_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY  # Tab name in the N-Panel

    def draw_header(self, context):
        row = self.layout.row()
        if get_engine_license_status() != LICENSE_PRO:
            row.label(text="", icon='LOCKED')

    def draw(self, context):
        layout = self.layout
        
        # Get license_type from cached engine status, sync if needed
        license_type = get_engine_license_status()
        if license_type == "UNKNOWN":
            try:
                license_type = engine.sync_license_mode()
                set_engine_license_status(license_type)
            except Exception as e:
                print(f"[Pivot] Failed to sync license: {e}")
                license_type = "UNKNOWN"
        
        enabled = (license_type == LICENSE_PRO)
        
        if enabled:
            row = layout.row()
            row.label(text=LABEL_OBJECTS_COLLECTION)
            row = layout.row()
            row.prop(bpy.context.scene.pivot, "objects_collection", text="")
            layout.separator()
            row = layout.row()
            row.operator(Pivot_OT_Standardize_Selected_Groups.bl_idname)
            
            # Organization button
            row = layout.row()
            row.operator(Pivot_OT_Organize_Classified_Objects.bl_idname)
            
            # Reset classifications button
            layout.separator()
            row = layout.row()
            row.operator(Pivot_OT_Reset_Classifications.bl_idname, icon='X')
        else:
            # Standard mode: show upgrade info
            layout.label(text="Unlock Your Full Pipeline:")
            layout.label(text="- Intuitive multi-object grouping")
            layout.label(text="- Auto-classify your entire scene")
            layout.label(text="- Multithreaded C++ performance")
            layout.separator()
            row = layout.row()
            row.operator(Pivot_OT_Upgrade_To_Pro.bl_idname, icon='WORLD')


class Pivot_PT_Standard_Panel(bpy.types.Panel):
    bl_label = "Object Standardization"
    bl_idname = PRE + "_PT_standard_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY  # Tab name in the N-Panel

    def draw(self, context):
        obj = context.active_object
        layout = self.layout
        
        # Get license_type from cached engine status
        layout.label(text="On Selected Objects:")
        # Always show selected objects operators
        
        row = layout.row()
        row.operator(Pivot_OT_Set_Origin_Selected_Objects.bl_idname, icon=Pivot_OT_Set_Origin_Selected_Objects.bl_icon)
        row = layout.row()
        row.operator(Pivot_OT_Align_Facing_Selected_Objects.bl_idname, icon=Pivot_OT_Align_Facing_Selected_Objects.bl_icon)

