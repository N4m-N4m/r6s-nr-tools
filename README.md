# DynaTools-R6 Blender Add-on

## Overview
DynaTools-R6 is a Blender add-on designed to clean up and set up NinjaRipper exports for Rainbow Six Siege. It provides various tools to optimize and organize mesh data, materials, textures, and lighting configurations to streamline the workflow.

## Credits
[Nam-Nam](https://x.com/NamNamR6) who added a lot more functionality to this whole addon.

---

## Installation
1. Download the addon as a `.zip` file.
2. Open Blender and go to **Edit > Preferences > Add-ons**.
3. Click **Install...**, select the addon, and enable the add-on.
4. The add-on panel will be available in the **3D Viewport > Sidebar > DynaTools-R6**.

---

## Features

### 1. Mesh Cleanup

#### Delete Flat Artifacts
- **Function:** Deletes flat objects (artifacts) in the scene that have zero thickness in the Z dimension.
- **Usecase:** Deletes in-game UI elements and overlay artifacts.
- **How it works:**
  - Iterates through all objects.
  - Identifies objects with `dimensions.z == 0`.
  - Deletes them to reduce clutter.

#### Delete Objects Without Texture
- **Function:** Removes objects that do not have any materials or textures.
- **Usecase:** Clean up meshes without materials.
- **How it works:**
  - Checks each object's material slots.
  - Deletes objects without assigned materials.

### 2. Multi Rip Cleanup

#### Merge Duplicate Materials
- **Function:** Merges materials that share identical image textures.
- **Usecase:** Merging duplicate materials when multiple rips have been imported.
- **How it works:**
  - Compares materials by analyzing image texture nodes.
  - Replaces duplicate materials with a single instance.
  - Cleans up unused materials.

#### Delete Duplicate Objects
- **Function:** Removes duplicate objects based on vertex positions and material assignments (should be run after Merge Duplicate Materials and the lineup of multiple rips).
- **Usecase:** Deleting overlaying objects that show up in multiple rips.
- **How it works:**
  - Groups objects by material.
  - Compares vertex positions within a threshold to identify duplicates.
  - Retains the object with more material slots.

### 3. Alignment Tools

#### Align On Target Plane
- **Function:** Aligns selected objects to a specific plane based on an active face's normal.
- **Usecase:** Aligning ground and rotation.
- **How it works:**
  - Checks the selected face's normal.
  - Aligns it to a user-defined target plane (XY, YZ, XZ, etc.).
  - Rotates objects accordingly.
  - ALWAYS APPLY TRANSFORM AFTER ONE ALIGNMENT IF YOU WANT TO ALIGN THE MESH TO MULTIPLE PLANES.

#### Move to Gizmo
- **Function:** Moves the selected object's vertex midpoint to the 3D cursor position.
- **Usecase:** Aligning two meshes or aligning mesh with ground.
- **How it works:**
  - Calculates the midpoint of selected vertices.
  - Moves the object to align with the 3D cursor.

### 4. Material and Lighting Setup

#### Set Active UV
- **Function:** Sets a specified UV layer as the active render layer.
- **Usecase:** Fixing UVs in bulk.
- **How it works:**
  - Assigns a UV layer name provided by the user.

#### Auto Setup Node Group
- **Function:** Automatically sets up a node group for selected objects.
- **Usecase:** Material Auto Setup.
- **How it works:**
  - Ensures shader groups are loaded.
  - Sets up textures in the material nodes accordingly.

#### Create Lights From Material
- **Function:** Creates light sources based on the active object's material properties.
- **Usecase:** Bulk light creation and alignment.
- **How it works:**
  - Searches materials for emissive properties.
  - Creates point lights at object locations.

### 5. Find Missing Textures For Selected

#### Find Missing Textures
- **Function:** Finds and assigns missing textures from a NinjaRipper log file.
- **Usecase:** Auto-locating textures that got ripped but haven't been imported (usually character ID maps and PBR).
- **How it works:**
  - Reads the log file for texture paths.
  - Matches textures to selected objects.
  - Appends missing textures to the object's material node tree.

---

