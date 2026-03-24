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

# type: ignore
from bpy.types import PropertyGroup, Collection
from bpy.props import BoolProperty, EnumProperty, StringProperty, PointerProperty
import bpy

# Import C enum values from Cython module
from pivot_lib import classification
from .constants import LICENSE_STANDARD, LICENSE_PRO

# UI Labels (property names derived from these)
LABEL_OBJECTS_COLLECTION = "Source Collection:"
LABEL_ROOM_COLLECTION = "Room:"
LABEL_SURFACE_TYPE = "Surface Context:"
LABEL_SURFACE_CONTEXT = "Surface Context:"
LABEL_ORIGIN_METHOD = "Origin Method:"
LABEL_LICENSE_TYPE = "License:"

# Marker property to identify classification collections
CLASSIFICATION_MARKER_PROP = "pivot_is_classification_collection"
CLASSIFICATION_ROOT_MARKER_PROP = "pivot_is_classification_root"

def _is_descendant_of_classification_collection(coll):
    """Check if a collection is a descendant of any classification collection."""
    def check_parents(current_coll):
        for parent_coll in bpy.data.collections:
            if parent_coll.get(CLASSIFICATION_MARKER_PROP, False) or parent_coll.get(CLASSIFICATION_ROOT_MARKER_PROP, False):
                if _is_in_subtree(current_coll, parent_coll):
                    return True
        return False
    
    def _is_in_subtree(target_coll, root_coll):
        """Check if target_coll is in the subtree of root_coll."""
        if target_coll == root_coll:
            return True
        for child in root_coll.children:
            if _is_in_subtree(target_coll, child):
                return True
        return False
    
    return check_parents(coll)

def poll_visible_collections(self, coll):
    """
    Only show collections that are NOT marked as classification collections
    and are NOT descendants of classification collections.
    We use .get() to avoid an error if the property doesn't exist.
    """
    # Check if this collection itself is marked
    if coll.get(CLASSIFICATION_MARKER_PROP, False) or coll.get(CLASSIFICATION_ROOT_MARKER_PROP, False):
        return False
    
    # Check if this collection is a descendant of any classification collection
    if _is_descendant_of_classification_collection(coll):
        return False
    
    return True


class SceneAttributes(PropertyGroup):
    objects_collection: PointerProperty(
        name=LABEL_OBJECTS_COLLECTION.rstrip(":"),
        description="Defines the collection that contains all the assets you want Pivot to operate on. This acts as the source and scope for all Scene Organization tools",
        type=Collection,
        poll=poll_visible_collections,
    )
    surface_type: EnumProperty(
        name=LABEL_SURFACE_TYPE.rstrip(":"),
        description="Sets the global context for all standardization operations. 'Auto' intelligently guesses the surface for each asset, while manual overrides force a specific context (Ground, Wall, or Ceiling) for the entire operation",
        items=[
            ("AUTO", "Auto", "Lets the engine decide"),
            (str(classification.SURFACE_GROUND), "Ground", "Ground surface"),
            (str(classification.SURFACE_WALL), "Wall", "Wall surface"),
            (str(classification.SURFACE_CEILING), "Ceiling", "Ceiling surface"),
        ],
        default="AUTO",
    )
    origin_method: EnumProperty(
        name=LABEL_ORIGIN_METHOD.rstrip(":"),
        description="Sets the method for placing the origin. 'Base' uses the primary contact surface (bottom, back, or top) as determined by the 'Surface Context', while 'Volume (Fast)' uses a proprietary, high-speed approximation of the object's volumetric center designed for stability",
        items=[
            ('BASE', 'Base', 'Center of surface contact'),
            ('VOLUME', 'Volume (Fast)', 'Center of gravity by volume'),
        ],
        default='BASE',
    )
