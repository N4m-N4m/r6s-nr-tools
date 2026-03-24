import bpy  # type: ignore
from mathutils import Matrix, Vector  # type: ignore


def _mesh_signature(obj):
    if obj.type != 'MESH' or obj.data is None:
        return None
    vertex_count = len(obj.data.vertices)
    material_names = tuple(slot.material.name if slot.material else "" for slot in obj.material_slots)
    return (vertex_count, material_names)


def _mat_vec_mul(m, v):
    return Vector((
        m[0][0] * v.x + m[0][1] * v.y + m[0][2] * v.z,
        m[1][0] * v.x + m[1][1] * v.y + m[1][2] * v.z,
        m[2][0] * v.x + m[2][1] * v.y + m[2][2] * v.z,
    ))


def _outer(v):
    return (
        (v.x * v.x, v.x * v.y, v.x * v.z),
        (v.y * v.x, v.y * v.y, v.y * v.z),
        (v.z * v.x, v.z * v.y, v.z * v.z),
    )


def _power_iteration(cov, iterations=24):
    v = Vector((1.0, 0.0, 0.0))
    for _ in range(iterations):
        nv = _mat_vec_mul(cov, v)
        if nv.length_squared < 1e-18:
            break
        v = nv.normalized()
    lam = v.dot(_mat_vec_mul(cov, v))
    return lam, v


def _covariance_from_mesh(mesh):
    verts = mesh.vertices
    if not verts:
        return None

    center = Vector((0.0, 0.0, 0.0))
    for vert in verts:
        center += vert.co
    center /= len(verts)

    cxx = cyy = czz = cxy = cxz = cyz = 0.0
    for vert in verts:
        d = vert.co - center
        cxx += d.x * d.x
        cyy += d.y * d.y
        czz += d.z * d.z
        cxy += d.x * d.y
        cxz += d.x * d.z
        cyz += d.y * d.z

    return (
        (cxx, cxy, cxz),
        (cxy, cyy, cyz),
        (cxz, cyz, czz),
    )


def _local_obb_basis(mesh):
    cov = _covariance_from_mesh(mesh)
    if cov is None:
        return Matrix.Identity(3)

    lam1, v1 = _power_iteration(cov)
    out1 = _outer(v1)
    cov2 = (
        (cov[0][0] - lam1 * out1[0][0], cov[0][1] - lam1 * out1[0][1], cov[0][2] - lam1 * out1[0][2]),
        (cov[1][0] - lam1 * out1[1][0], cov[1][1] - lam1 * out1[1][1], cov[1][2] - lam1 * out1[1][2]),
        (cov[2][0] - lam1 * out1[2][0], cov[2][1] - lam1 * out1[2][1], cov[2][2] - lam1 * out1[2][2]),
    )

    _, v2_raw = _power_iteration(cov2)
    v2 = (v2_raw - v1 * v2_raw.dot(v1))
    if v2.length_squared < 1e-18:
        return Matrix.Identity(3)
    v2.normalize()

    v3 = v1.cross(v2)
    if v3.length_squared < 1e-18:
        return Matrix.Identity(3)
    v3.normalize()
    v2 = v3.cross(v1).normalized()

    basis = Matrix((v1, v2, v3)).transposed()
    if basis.determinant() < 0:
        basis[0][2] *= -1.0
        basis[1][2] *= -1.0
        basis[2][2] *= -1.0

    return basis


def _canonical_local_transform(mesh):
    """
    Return local transform that maps canonical OBB-space mesh -> current local mesh.
    T_local = Translation(obb_center_local) @ obb_basis
    """
    basis = _local_obb_basis(mesh)
    basis_inv = basis.inverted_safe()

    min_v = Vector((float("inf"), float("inf"), float("inf")))
    max_v = Vector((float("-inf"), float("-inf"), float("-inf")))

    verts = mesh.vertices
    if not verts:
        return Matrix.Identity(4)

    for vert in verts:
        p = basis_inv @ vert.co
        min_v.x = min(min_v.x, p.x)
        min_v.y = min(min_v.y, p.y)
        min_v.z = min(min_v.z, p.z)
        max_v.x = max(max_v.x, p.x)
        max_v.y = max(max_v.y, p.y)
        max_v.z = max(max_v.z, p.z)

    obb_center_basis = (min_v + max_v) * 0.5
    obb_center_local = basis @ obb_center_basis

    return Matrix.Translation(obb_center_local) @ basis.to_4x4()


def _obb_basis_and_extents(mesh):
    basis = _local_obb_basis(mesh)
    basis_inv = basis.inverted_safe()

    min_v = Vector((float("inf"), float("inf"), float("inf")))
    max_v = Vector((float("-inf"), float("-inf"), float("-inf")))
    verts = mesh.vertices
    if not verts:
        return basis, Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 0.0))

    for vert in verts:
        p = basis_inv @ vert.co
        min_v.x = min(min_v.x, p.x)
        min_v.y = min(min_v.y, p.y)
        min_v.z = min(min_v.z, p.z)
        max_v.x = max(max_v.x, p.x)
        max_v.y = max(max_v.y, p.y)
        max_v.z = max(max_v.z, p.z)

    center_basis = (min_v + max_v) * 0.5
    half_extents = (max_v - min_v) * 0.5
    return basis, center_basis, half_extents


class OBJECT_OT_SelectSimilarByVertexAndMaterial(bpy.types.Operator):
    bl_idname = "object.select_similar_vertex_material"
    bl_label = "Select Similar (Verts + Materials)"
    bl_description = "Select mesh objects with the same vertex count and material list as active object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active = context.view_layer.objects.active
        if active is None or active.type != 'MESH':
            self.report({'WARNING'}, "Active object must be a mesh")
            return {'CANCELLED'}

        signature = _mesh_signature(active)
        if signature is None:
            self.report({'WARNING'}, "Could not build active object signature")
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')

        count = 0
        for obj in context.scene.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            if _mesh_signature(obj) == signature:
                obj.select_set(True)
                count += 1

        context.view_layer.objects.active = active
        self.report({'INFO'}, f"Selected {count} similar objects.")
        return {'FINISHED'}


class OBJECT_OT_InstanceSimilarByOBB(bpy.types.Operator):
    bl_idname = "object.instance_similar_by_obb"
    bl_label = "Instance Similar (OBB Rotation)"
    bl_description = "Instance selected similar meshes and preserve visual orientation using OBB basis compensation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if len(selected) < 2:
            self.report({'WARNING'}, "Select at least two mesh objects")
            return {'CANCELLED'}

        groups = {}
        for obj in selected:
            sig = _mesh_signature(obj)
            if sig is not None:
                groups.setdefault(sig, []).append(obj)

        instance_count = 0
        for objects in groups.values():
            if len(objects) < 2:
                continue

            reference = objects[0]
            ref_mesh = reference.data
            if ref_mesh.users > 1:
                ref_mesh = ref_mesh.copy()
                reference.data = ref_mesh

            ref_local_t = _canonical_local_transform(ref_mesh)
            ref_local_t_inv = ref_local_t.inverted_safe()

            # Normalize reference mesh around OBB in local canonical space.
            ref_mesh.transform(ref_local_t_inv)
            ref_mesh.update()

            # Back-transform object so world-space appearance stays unchanged.
            reference.matrix_world = reference.matrix_world @ ref_local_t

            for obj in objects[1:]:
                src_local_t = _canonical_local_transform(obj.data)
                obj.data = ref_mesh
                obj.matrix_world = obj.matrix_world @ src_local_t

                instance_count += 1

        if instance_count == 0:
            self.report({'WARNING'}, "No similar groups with at least two selected objects were found")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Instanced {instance_count} objects.")
        return {'FINISHED'}


class OBJECT_OT_CreateSmallestBoundingBox(bpy.types.Operator):
    bl_idname = "object.create_smallest_bounding_box"
    bl_label = "Create Smallest Bounding Box"
    bl_description = "Create an oriented bounding box mesh for each selected mesh object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected:
            self.report({'WARNING'}, "Select at least one mesh object")
            return {'CANCELLED'}

        created = 0
        for obj in selected:
            mesh = obj.data
            basis, center_basis, half_extents = _obb_basis_and_extents(mesh)
            if half_extents.length_squared < 1e-18:
                continue

            cx, cy, cz = center_basis.x, center_basis.y, center_basis.z
            ex, ey, ez = half_extents.x, half_extents.y, half_extents.z

            verts = [
                Vector((cx - ex, cy - ey, cz - ez)),
                Vector((cx + ex, cy - ey, cz - ez)),
                Vector((cx + ex, cy + ey, cz - ez)),
                Vector((cx - ex, cy + ey, cz - ez)),
                Vector((cx - ex, cy - ey, cz + ez)),
                Vector((cx + ex, cy - ey, cz + ez)),
                Vector((cx + ex, cy + ey, cz + ez)),
                Vector((cx - ex, cy + ey, cz + ez)),
            ]

            # Convert OBB basis-space verts to local mesh-space verts.
            verts_local = [basis @ v for v in verts]
            faces = [
                (0, 1, 2, 3),
                (4, 5, 6, 7),
                (0, 1, 5, 4),
                (1, 2, 6, 5),
                (2, 3, 7, 6),
                (3, 0, 4, 7),
            ]

            bbox_mesh = bpy.data.meshes.new(f"{obj.name}_OBB")
            bbox_mesh.from_pydata([v[:] for v in verts_local], [], faces)
            bbox_mesh.update()

            bbox_obj = bpy.data.objects.new(f"{obj.name}_OBB", bbox_mesh)
            if obj.users_collection:
                for collection in obj.users_collection:
                    collection.objects.link(bbox_obj)
            else:
                context.scene.collection.objects.link(bbox_obj)

            bbox_obj.matrix_world = obj.matrix_world.copy()
            bbox_obj.display_type = 'WIRE'
            bbox_obj.show_in_front = True
            created += 1

        if created == 0:
            self.report({'WARNING'}, "No valid bounding boxes could be created")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Created {created} oriented bounding box objects.")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(OBJECT_OT_SelectSimilarByVertexAndMaterial)
    bpy.utils.register_class(OBJECT_OT_InstanceSimilarByOBB)
    bpy.utils.register_class(OBJECT_OT_CreateSmallestBoundingBox)


def unregister():
    bpy.utils.unregister_class(OBJECT_OT_CreateSmallestBoundingBox)
    bpy.utils.unregister_class(OBJECT_OT_SelectSimilarByVertexAndMaterial)
    bpy.utils.unregister_class(OBJECT_OT_InstanceSimilarByOBB)
