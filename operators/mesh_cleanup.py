import bpy # type: ignore
import bmesh # type: ignore
import numpy as np
from collections import defaultdict
from mathutils.kdtree import KDTree # type: ignore


class MESH_OT_mark_boundary_and_merge(bpy.types.Operator):
    bl_idname = "mesh.mark_boundary_and_merge"
    bl_label = "Merge & Keep Normals"
    bl_description = "Merge vertices by distance while preserving original shading normals"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "No mesh objects selected")
            return {'CANCELLED'}

        merge_distance = context.scene.boundary_merge_distance

        # Ensure object mode
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        wm = context.window_manager
        total_objects = len(selected_objects)
        wm.progress_begin(0, 100)

        total_merged = 0

        for obj_idx, obj in enumerate(selected_objects):
            percent = int((obj_idx / total_objects) * 100)
            wm.progress_update(percent)
            try:
                context.workspace.status_text_set(
                    f"Merge & Keep Normals: {percent}% ({obj_idx + 1}/{total_objects})"
                )
            except Exception:
                pass

            mesh = obj.data
            num_loops = len(mesh.loops)
            if num_loops == 0:
                continue

            # --- Store original loop normals (Blender 4.0+ API) ---
            old_normals = np.empty(num_loops * 3, dtype=np.float32)
            mesh.corner_normals.foreach_get("vector", old_normals)
            old_normals = old_normals.reshape(-1, 3)

            loop_vert_idx = np.empty(num_loops, dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", loop_vert_idx)

            num_verts = len(mesh.vertices)
            vert_co = np.empty(num_verts * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", vert_co)
            vert_co = vert_co.reshape(-1, 3)

            old_loop_pos = vert_co[loop_vert_idx]

            num_polys = len(mesh.polygons)
            old_face_normals = np.empty(num_polys * 3, dtype=np.float32)
            mesh.polygons.foreach_get("normal", old_face_normals)
            old_face_normals = old_face_normals.reshape(-1, 3)

            # Vectorized loop-to-face mapping
            face_loop_totals = np.empty(num_polys, dtype=np.int32)
            mesh.polygons.foreach_get("loop_total", face_loop_totals)
            old_loop_face = np.repeat(np.arange(num_polys, dtype=np.int32), face_loop_totals)

            # --- Merge by distance (bypasses edit mode entirely) ---
            bm = bmesh.new()
            bm.from_mesh(mesh)
            old_vert_count = len(bm.verts)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_distance)
            new_vert_count = len(bm.verts)
            merged_count = old_vert_count - new_vert_count
            total_merged += merged_count
            bm.to_mesh(mesh)
            bm.free()

            if merged_count == 0:
                continue

            mesh.update()

            # --- Restore normals on the merged mesh ---
            new_num_loops = len(mesh.loops)
            if new_num_loops == 0:
                continue

            new_loop_vert_idx = np.empty(new_num_loops, dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", new_loop_vert_idx)

            new_num_verts = len(mesh.vertices)
            new_vert_co = np.empty(new_num_verts * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", new_vert_co)
            new_vert_co = new_vert_co.reshape(-1, 3)

            new_loop_pos = new_vert_co[new_loop_vert_idx]

            new_num_polys = len(mesh.polygons)
            new_face_normals = np.empty(new_num_polys * 3, dtype=np.float32)
            mesh.polygons.foreach_get("normal", new_face_normals)
            new_face_normals = new_face_normals.reshape(-1, 3)

            new_face_loop_totals = np.empty(new_num_polys, dtype=np.int32)
            mesh.polygons.foreach_get("loop_total", new_face_loop_totals)
            new_loop_face = np.repeat(
                np.arange(new_num_polys, dtype=np.int32), new_face_loop_totals
            )

            # Spatial hash from old loop positions (bytes keys for speed)
            quantized_old = np.ascontiguousarray(
                np.round(old_loop_pos, decimals=7).astype(np.float32)
            )
            raw_old = quantized_old.tobytes()
            pos_to_old = defaultdict(list)
            for li in range(num_loops):
                pos_to_old[raw_old[li * 12 : (li + 1) * 12]].append(li)

            quantized_new = np.ascontiguousarray(
                np.round(new_loop_pos, decimals=7).astype(np.float32)
            )
            raw_new = quantized_new.tobytes()

            restored = np.empty((new_num_loops, 3), dtype=np.float64)
            unmatched = []

            for li in range(new_num_loops):
                key = raw_new[li * 12 : (li + 1) * 12]
                candidates = pos_to_old.get(key)
                if candidates is None:
                    unmatched.append(li)
                elif len(candidates) == 1:
                    restored[li] = old_normals[candidates[0]]
                else:
                    # Disambiguate by face normal similarity
                    new_fn = new_face_normals[new_loop_face[li]]
                    best_score = -2.0
                    best_idx = candidates[0]
                    for old_li in candidates:
                        old_fn = old_face_normals[old_loop_face[old_li]]
                        score = float(
                            new_fn[0] * old_fn[0]
                            + new_fn[1] * old_fn[1]
                            + new_fn[2] * old_fn[2]
                        )
                        if score > best_score:
                            best_score = score
                            best_idx = old_li
                    restored[li] = old_normals[best_idx]

            # KDTree fallback for loops whose vertices moved during merge
            if unmatched:
                kd = KDTree(num_loops)
                for li in range(num_loops):
                    kd.insert(old_loop_pos[li], li)
                kd.balance()

                search_radius = merge_distance * 2 + 0.001
                for li in unmatched:
                    pos = new_loop_pos[li]
                    new_fn = new_face_normals[new_loop_face[li]]
                    results = kd.find_range(pos, search_radius)
                    if not results:
                        _co, idx, _dist = kd.find(pos)
                        restored[li] = old_normals[idx]
                    elif len(results) == 1:
                        restored[li] = old_normals[results[0][1]]
                    else:
                        best_score = -2.0
                        best_idx = results[0][1]
                        for _co, idx, _dist in results:
                            old_fn = old_face_normals[old_loop_face[idx]]
                            score = float(
                                new_fn[0] * old_fn[0]
                                + new_fn[1] * old_fn[1]
                                + new_fn[2] * old_fn[2]
                            )
                            if score > best_score:
                                best_score = score
                                best_idx = idx
                        restored[li] = old_normals[best_idx]

            # Apply custom split normals to preserve original shading
            mesh.normals_split_custom_set(restored.tolist())
            mesh.update()

        wm.progress_update(100)
        wm.progress_end()
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

        self.report(
            {'INFO'},
            f"Merged {total_merged} vertices across {total_objects} objects with normals preserved",
        )
        return {'FINISHED'}


def register():
    bpy.utils.register_class(MESH_OT_mark_boundary_and_merge)


def unregister():
    bpy.utils.unregister_class(MESH_OT_mark_boundary_and_merge)
