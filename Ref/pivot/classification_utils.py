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

from pivot_lib.surface_manager import CLASSIFICATION_ROOT_MARKER_PROP


def get_all_mesh_objects_in_collection(coll):
    meshes = []
    for obj in coll.objects:
        if obj.type == 'MESH':
            meshes.append(obj)
    for child in coll.children:
        meshes.extend(get_all_mesh_objects_in_collection(child))
    return meshes


def build_collection_caches(scene_root):
    coll_to_top = {}

    def traverse(current_coll, current_top):
        for child in current_coll.children:
            coll_to_top[child] = current_top
            traverse(child, current_top)

    for top in scene_root.children:
        if top.get(CLASSIFICATION_ROOT_MARKER_PROP, False):
            continue
        coll_to_top[top] = top
        traverse(top, top)

    top_has_mesh_cache = {}

    def coll_has_mesh(coll):
        for o in coll.objects:
            if o.type == 'MESH':
                return True
        for child in coll.children:
            if coll_has_mesh(child):
                return True
        return False

    return coll_to_top, top_has_mesh_cache, coll_has_mesh


def object_qualifies(obj, scene_root, coll_to_top, top_has_mesh_cache, coll_has_mesh):
    if obj.type == 'MESH' and scene_root in obj.users_collection:
        return True

    def has_mesh_descendants(obj):
        for child in obj.children:
            if child.type == 'MESH' or has_mesh_descendants(child):
                return True
        return False

    if scene_root in obj.users_collection and has_mesh_descendants(obj):
        return True

    for coll in getattr(obj, 'users_collection', []) or []:
        if coll is scene_root:
            continue
        top = coll_to_top.get(coll)
        if not top or top.get(CLASSIFICATION_ROOT_MARKER_PROP, False):
            continue
        if top not in top_has_mesh_cache:
            top_has_mesh_cache[top] = coll_has_mesh(top)
        if top_has_mesh_cache[top]:
            return True

    return False


def selected_has_qualifying_objects(selected_objects, objects_collection):
    scene_root = objects_collection
    if not scene_root or not selected_objects:
        return False

    coll_to_top, top_has_mesh_cache, coll_has_mesh = build_collection_caches(scene_root)

    for obj in selected_objects:
        if obj and object_qualifies(obj, scene_root, coll_to_top, top_has_mesh_cache, coll_has_mesh):
            return True

    return False


def get_qualifying_objects_for_selected(selected_objects, objects_collection):
    qualifying = []
    scene_root = objects_collection

    coll_to_top, top_has_mesh_cache, coll_has_mesh = build_collection_caches(scene_root)

    for obj in selected_objects:
        if object_qualifies(obj, scene_root, coll_to_top, top_has_mesh_cache, coll_has_mesh):
            qualifying.append(obj)

    return list(set(qualifying))  # remove duplicates