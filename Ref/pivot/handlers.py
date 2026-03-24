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

# Blender Event Handlers
# -----------------------
# Handles Blender lifecycle events including:
# - File load/save events (load_pre, load_post)
# - Depsgraph updates (on_depsgraph_update)

import bpy
from bpy.app.handlers import persistent
import os
import sys

from pivot_lib import engine_state
from pivot_lib import group_manager
from pivot_lib import engine
from pivot_lib import surface_manager
import time

# Cache of each object's last-known scale to detect transform-only edits quickly.
_previous_scales: dict[str, tuple[float, float, float]] = {}
# Cache of each object's last-known rotation to detect rotation changes.
_previous_rotations: dict[str, tuple[float, float, float, float]] = {}


@persistent
def on_depsgraph_update(scene, depsgraph):
    """Orchestrate all depsgraph update handlers in guaranteed order."""
    start_time = time.time()
    if engine_state.is_performing_classification():
        engine_state.set_performing_classification(False)
    else:
        detect_collection_hierarchy_changes(scene, depsgraph)
        unsync_mesh_changes(scene, depsgraph)
    enforce_colors(scene, depsgraph)
    end_time = time.time()
    # print(f"on_depsgraph_update took {1000 * (end_time - start_time):.4f} milliseconds")


def detect_collection_hierarchy_changes(scene, depsgraph):
    """Detect changes in collection hierarchy and mark affected groups as out-of-sync with the engine."""
    group_mgr = group_manager.get_group_manager()
    
    current_snapshot = group_mgr.get_group_membership_snapshot()
    expected_snapshot = engine_state.get_group_membership_snapshot()
    for group_name, expected_members in expected_snapshot.items():
        current_members = current_snapshot.get(group_name, set())
        if expected_members != current_members:
            group_mgr.set_group_unsynced(group_name)


def enforce_colors(scene, depsgraph):
    """Enforce correct color tags for group collections based on sync state.
    
    Also immediately handles orphaned groups by dropping them from engine,
    sync state, and clearing their colors.
    """
    group_mgr = group_manager.get_group_manager()
    orphaned_groups = group_mgr.update_orphaned_groups()
    
    # Immediately handle orphaned groups
    if orphaned_groups:
        try:
            # Drop from engine
            engine_comm = engine.get_engine_communicator()
            dropped_count = engine_comm.drop_groups(orphaned_groups)
            if dropped_count >= 0:
                # Clear their colors and remove from sync state
                for coll_name in orphaned_groups:
                    if coll_name in bpy.data.collections:
                        bpy.data.collections[coll_name].color_tag = 'NONE'
                group_mgr.drop_groups(orphaned_groups)
                print(f"[Pivot] Dropped {dropped_count} orphaned groups from engine")
        except Exception as e:
            print(f"[Pivot] Error handling orphaned groups: {e}")
    
    # Update colors for remaining managed groups
    group_mgr.update_colors()


def unsync_mesh_changes(scene, depsgraph):
    """Detect mesh and transform changes on selected objects and mark groups as unsynced."""
    global _previous_scales, _previous_rotations
    
    group_mgr = group_manager.get_group_manager()

    # Get all selected mesh objects first (quick operation)
    all_selected_mesh = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
    all_selected_mesh_set = set(all_selected_mesh)  # For O(1) lookup
    
    if not all_selected_mesh:
        return  # No selected objects, nothing to do
    
    # Get managed collections info (fast, no full snapshot yet)
    managed_group_names = group_mgr.get_sync_state_keys()  # Returns a set
    
    selected_objects = []
    obj_to_groups = {}
    
    # Iterate managed collections to find selected objects (avoids checking all 220 objects)
    for group_name in managed_group_names:
        # Get the collection
        coll = bpy.data.collections.get(group_name)
        if not coll:
            continue
        
        # Check only objects in this collection against selected set (O(1) lookups)
        for obj in coll.objects:
            if obj in all_selected_mesh_set:
                if obj not in selected_objects:
                    selected_objects.append(obj)
                if obj not in obj_to_groups:
                    obj_to_groups[obj] = []
                obj_to_groups[obj].append(group_name)
    
    if not selected_objects:
        return  # No selected objects in managed collections
    
    # Only build snapshots if we have selected objects to process
    expected_snapshot = engine_state.get_group_membership_snapshot()
    current_snapshot = group_mgr.get_group_membership_snapshot()
    
    # Build reverse lookup for O(1) matching: update.id.original -> obj
    id_to_obj = {}
    for obj in selected_objects:
        id_to_obj[id(obj)] = obj
        id_to_obj[id(obj.data)] = obj

    # Main processing loop
    for update in depsgraph.updates:
        if not (update.is_updated_geometry or update.is_updated_transform):
            continue

        # O(1) lookup instead of O(m) loop
        obj = id_to_obj.get(id(update.id.original))
        if obj is None:
            continue

        group_names = obj_to_groups.get(obj, [])
        for group_name in group_names:
            expected_members = expected_snapshot.get(group_name)
            current_members = current_snapshot.get(group_name, set())
            member_count = len(expected_members) if expected_members is not None else len(current_members)

            current_scale = tuple(obj.scale)
            prev_scale = _previous_scales.get(obj.name)
            scale_changed = prev_scale is not None and current_scale != prev_scale

            current_rotation = tuple(obj.rotation_quaternion)
            prev_rotation = _previous_rotations.get(obj.name)
            rotation_changed = prev_rotation is not None and current_rotation != prev_rotation

            should_mark_unsynced = (
                expected_members is None
                or update.is_updated_geometry
                or scale_changed
                or rotation_changed
                or (update.is_updated_transform and member_count > 1)
            )

            if should_mark_unsynced:
                group_mgr.set_group_unsynced(group_name)

        # Update scale and rotation caches for next handler invocation
        _previous_scales[obj.name] = current_scale
        _previous_rotations[obj.name] = current_rotation

def clear_previous_scales():
    """Clear the scale and rotation caches used for detecting transform changes."""
    global _previous_scales, _previous_rotations
    _previous_scales.clear()
    _previous_rotations.clear()


def on_group_name_changed(collection, group_mgr):
    """Callback for when a managed group's name changes.
    
    Args:
        collection: The collection whose name changed
        group_mgr: The GroupManager instance
    """
    name_tracker = group_mgr.get_name_tracker()
    
    old_name = name_tracker.get(collection)
    new_name = collection.name

    if old_name and old_name != new_name:
        # Mark the collection as orphaned - enforce_colors will handle cleanup
        collection.color_tag = 'NONE'


# File Load Handlers
# ------------------
# Manage engine lifecycle and state synchronization around file load/save events.

@persistent
def on_load_pre(scene):
    """Executed before a new file is loaded.
    
    Shuts down the engine and syncs any pending classification state before file load.
    """
    try:
        # Sync any pending group classifications to the engine before shutting down
        surface_mgr = surface_manager.get_surface_manager()
        classifications = surface_mgr.collect_group_classifications()
        if classifications:
            surface_mgr.sync_group_classifications(classifications)
    except Exception as e:
        print(f"[Pivot] Failed to sync classifications before load: {e}")
    
    # Stop the pivot engine
    engine.stop_engine()
    

@persistent
def on_load_post(scene):
    """Executed after a new file has finished loading.
    
    Starts the engine up again for the new scene and initializes local tracked state.
    """
    # Reset GroupManager state for the new scene
    group_manager.get_group_manager().reset_state()
    
    # Initialize engine state for the new scene
    engine_state.update_group_membership_snapshot({}, replace=True)
    clear_previous_scales()
    
    
    