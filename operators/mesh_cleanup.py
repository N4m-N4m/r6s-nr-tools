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


class MESH_OT_create_proxy(bpy.types.Operator):
    bl_idname = "mesh.create_proxy"
    bl_label = "Create Proxy Mesh"
    bl_description = "Fast vertex-clustering decimation with boundary preservation"
    bl_options = {'REGISTER', 'UNDO'}

    # ---- static helpers (pure numpy, no bpy.ops, no mode switches) ----

    @staticmethod
    def _detect_boundary(mesh, num_verts):
        """Find boundary vertices via edge-face count (fully vectorised)."""
        num_edges = len(mesh.edges)
        num_loops = len(mesh.loops)
        if num_edges == 0:
            return np.zeros(num_verts, dtype=bool)

        loop_edge = np.empty(num_loops, dtype=np.int32)
        mesh.loops.foreach_get("edge_index", loop_edge)
        edge_face_count = np.bincount(loop_edge, minlength=num_edges)

        boundary_mask = edge_face_count == 1
        if not np.any(boundary_mask):
            return np.zeros(num_verts, dtype=bool)

        edge_verts = np.empty(num_edges * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", edge_verts)
        edge_verts = edge_verts.reshape(-1, 2)

        bv = np.unique(edge_verts[boundary_mask].ravel())
        is_boundary = np.zeros(num_verts, dtype=bool)
        is_boundary[bv] = True
        return is_boundary

    @staticmethod
    def _cluster(norm, grid_res, is_boundary):
        """Vertex clustering — boundary verts each get a unique cell."""
        cells = np.clip((norm * grid_res).astype(np.int32), 0, grid_res - 1)
        cell_id = (
            cells[:, 0].astype(np.int64) * grid_res * grid_res
            + cells[:, 1].astype(np.int64) * grid_res
            + cells[:, 2].astype(np.int64)
        )
        # Boundary vertices get unique IDs above the grid range
        boundary_idx = np.where(is_boundary)[0]
        if len(boundary_idx) > 0:
            base = np.int64(grid_res) ** 3
            cell_id[boundary_idx] = np.arange(len(boundary_idx), dtype=np.int64) + base

        return np.unique(cell_id, return_inverse=True)

    @staticmethod
    def _count_faces(inv, loop_vi, face_totals, face_starts, num_polys, mesh_type):
        new_lv = inv[loop_vi]
        if mesh_type == 3:
            t = new_lv.reshape(-1, 3)
            v = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
            return len(np.unique(np.sort(t[v], axis=1), axis=0))
        if mesh_type == 4:
            q = new_lv.reshape(-1, 4)
            v = (
                (q[:, 0] != q[:, 1]) & (q[:, 1] != q[:, 2])
                & (q[:, 2] != q[:, 3]) & (q[:, 3] != q[:, 0])
                & (q[:, 0] != q[:, 2]) & (q[:, 1] != q[:, 3])
            )
            return len(np.unique(np.sort(q[v], axis=1), axis=0))
        seen = set()
        c = 0
        for fi in range(num_polys):
            fv = new_lv[face_starts[fi] : face_starts[fi] + face_totals[fi]]
            if len(np.unique(fv)) >= 3:
                k = tuple(sorted(fv))
                if k not in seen:
                    seen.add(k)
                    c += 1
        return c

    @staticmethod
    def _build_faces(inv, loop_vi, face_totals, face_starts, num_polys, mesh_type):
        new_lv = inv[loop_vi]
        if mesh_type == 3:
            t = new_lv.reshape(-1, 3)
            v = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
            t = t[v]
            _, ui = np.unique(np.sort(t, axis=1), axis=0, return_index=True)
            return t[np.sort(ui)].tolist()
        if mesh_type == 4:
            q = new_lv.reshape(-1, 4)
            v = (
                (q[:, 0] != q[:, 1]) & (q[:, 1] != q[:, 2])
                & (q[:, 2] != q[:, 3]) & (q[:, 3] != q[:, 0])
                & (q[:, 0] != q[:, 2]) & (q[:, 1] != q[:, 3])
            )
            q = q[v]
            _, ui = np.unique(np.sort(q, axis=1), axis=0, return_index=True)
            return q[np.sort(ui)].tolist()
        faces, seen = [], set()
        for fi in range(num_polys):
            fv = new_lv[face_starts[fi] : face_starts[fi] + face_totals[fi]]
            if len(np.unique(fv)) >= 3:
                k = tuple(sorted(fv))
                if k not in seen:
                    seen.add(k)
                    faces.append(fv.tolist())
        return faces

    # ---- main entry ----

    def execute(self, context):
        selected = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected:
            self.report({'ERROR'}, "No mesh objects selected")
            return {'CANCELLED'}

        target_pct = context.scene.proxy_target_percentage / 100.0

        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        wm = context.window_manager
        total = len(selected)
        wm.progress_begin(0, 100)

        total_before = 0
        total_after = 0

        for i, obj in enumerate(selected):
            percent = int((i / total) * 100)
            wm.progress_update(percent)
            try:
                context.workspace.status_text_set(
                    f"Creating proxy: {percent}% ({i + 1}/{total})"
                )
            except Exception:
                pass

            mesh = obj.data
            num_verts = len(mesh.vertices)
            num_polys = len(mesh.polygons)
            num_loops = len(mesh.loops)

            if num_verts < 4 or num_polys == 0:
                continue

            target_faces = max(int(num_polys * target_pct), 4)
            if num_polys <= target_faces:
                total_before += num_polys
                total_after += num_polys
                continue

            if mesh.users > 1:
                obj.data = mesh.copy()
                mesh = obj.data

            total_before += num_polys

            # Boundary detection (vectorised numpy — no bmesh, no Python loops)
            is_boundary = self._detect_boundary(mesh, num_verts)

            # Read geometry once
            co = np.empty(num_verts * 3, dtype=np.float64)
            mesh.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)

            loop_vi = np.empty(num_loops, dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", loop_vi)

            face_totals = np.empty(num_polys, dtype=np.int32)
            mesh.polygons.foreach_get("loop_total", face_totals)

            face_starts = np.empty(num_polys, dtype=np.int32)
            mesh.polygons.foreach_get("loop_start", face_starts)

            if np.all(face_totals == 3):
                mt = 3
            elif np.all(face_totals == 4):
                mt = 4
            else:
                mt = 0

            # Normalise positions to [0,1] once (reused by every search step)
            bbox_min = co.min(axis=0)
            inv_size = 1.0 / np.maximum(co.max(axis=0) - bbox_min, 1e-10)
            norm = (co - bbox_min) * inv_size

            # --- binary search for grid_res closest to target face count ---
            lo = 2
            hi = max(int(np.cbrt(num_verts) * 3), 10)

            _, inv_hi = self._cluster(norm, hi, is_boundary)
            fc_hi = self._count_faces(inv_hi, loop_vi, face_totals, face_starts, num_polys, mt)
            while fc_hi < target_faces and hi < 4000:
                hi *= 2
                _, inv_hi = self._cluster(norm, hi, is_boundary)
                fc_hi = self._count_faces(inv_hi, loop_vi, face_totals, face_starts, num_polys, mt)

            best_res = hi
            best_diff = abs(fc_hi - target_faces)

            while lo <= hi:
                mid = (lo + hi) // 2
                _, inv_mid = self._cluster(norm, mid, is_boundary)
                fc = self._count_faces(inv_mid, loop_vi, face_totals, face_starts, num_polys, mt)
                diff = abs(fc - target_faces)
                if diff < best_diff:
                    best_diff = diff
                    best_res = mid
                if fc < target_faces:
                    lo = mid + 1
                elif fc > target_faces:
                    hi = mid - 1
                else:
                    break

            # --- build final mesh at best resolution ---
            unique, inv = self._cluster(norm, best_res, is_boundary)
            new_nverts = len(unique)

            # Vertex positions: bincount is faster than np.add.at
            new_co = np.zeros((new_nverts, 3), dtype=np.float64)
            for d in range(3):
                new_co[:, d] = np.bincount(inv, weights=co[:, d], minlength=new_nverts)
            counts = np.bincount(inv, minlength=new_nverts).astype(np.float64)
            new_co /= counts[:, np.newaxis]

            faces = self._build_faces(inv, loop_vi, face_totals, face_starts, num_polys, mt)
            total_after += len(faces)

            mesh.clear_geometry()
            mesh.from_pydata(new_co.tolist(), [], faces)
            mesh.update()

        wm.progress_update(100)
        wm.progress_end()
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

        self.report(
            {'INFO'},
            f"Proxy: {total_before:,} \u2192 {total_after:,} faces across {total} objects",
        )
        return {'FINISHED'}


def register():
    bpy.utils.register_class(MESH_OT_mark_boundary_and_merge)
    bpy.utils.register_class(MESH_OT_create_proxy)


def unregister():
    bpy.utils.unregister_class(MESH_OT_create_proxy)
    bpy.utils.unregister_class(MESH_OT_mark_boundary_and_merge)
