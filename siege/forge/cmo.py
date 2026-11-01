"""
CompiledMeshObject decoder.

A CMO contains:
  - one shared vertex buffer (multiple LODs index into it)
  - one set of island headers per LOD, each carrying a face-chunk range and
    a mat_id that selects which slot from the parent Mesh record's material
    list applies
  - optional vertex remapping tables (vertmaps) per LOD that translate
    logical face indices to physical vertex indices

Returns a dict consumable by the Blender mesh builder.
"""

import io
import struct

from ..common.constants import (
    BIAS,
    COMPILED_MESH_MAGIC,
    FACE_CHUNK_SIZE,
    TRIS_IN_CHUNK,
    VALID_VERT_LENS,
)


def read_uint64_as_pos(r):
    """Decode packed int16x4 position."""
    x = struct.unpack('<h', r.read(2))[0]
    y = struct.unpack('<h', r.read(2))[0]
    z = struct.unpack('<h', r.read(2))[0]
    s = struct.unpack('<h', r.read(2))[0]
    if s == 0:
        s = 1
    return (x * s / BIAS, y * s / BIAS, z * s / BIAS)


def read_vector3(r):
    """Read 3 floats as position."""
    return struct.unpack('<3f', r.read(12))


def parse_compiled_mesh_object(data):
    """
    Parse a CompiledMeshObject from decompressed forge entry data.
    Returns dict with vertices and per-LOD face lists.

    The returned dict contains:
      - 'num_lods': total LOD count stored in the CMO
      - 'lods_faces': {lod_idx: [(a,b,c), ...]} faces for every LOD
      - 'lods_headers': {lod_idx: [island_header_dicts]}
      - 'faces', 'island_headers': LOD0 convenience aliases (legacy)

    Vertices are shared across all LODs; only the face index sets differ.
    Returns None on failure.
    """
    r = io.BytesIO(data)

    # The FileMetaData header preceding the CompiledMesh magic varies by
    # forge version:
    #   v>=31 (Y9S2): variable-length prefix
    #   v>=30 (Y5S3+): filenameLen(4)+filename(N)+containerType(4)+fileType(4)+uid(8)+secondMagic(4)+var2(4)+var3(4)  (32+N bytes)
    #   v<=29 (Y3S1): fileType(4)+var1(4)+var2(4)+uid(8)+secondMagic(4)+var2(4)+var3(4)  (32 bytes, no filename)
    # Magic-scan is robust for all three: locate CompiledMesh magic and seek
    # past it. The 4 bytes of size_until_footer follow the magic regardless
    # of version.
    cm_bytes = struct.pack('<I', COMPILED_MESH_MAGIC)
    cm_idx = data.find(cm_bytes, 0, min(len(data), 1024))
    if cm_idx < 0:
        print(f"[Siege][DIAG] CMO rejected: CompiledMesh magic not found in first 1KB")
        return None
    try:
        r.seek(cm_idx + 4)  # past CompiledMesh magic

        size_until_footer = struct.unpack('<I', r.read(4))[0]
        data_start = r.tell()

        # Model header (0x4C bytes)
        x00 = struct.unpack('<I', r.read(4))[0]
        revision = struct.unpack('<I', r.read(4))[0]
        vert_len = struct.unpack('<I', r.read(4))[0]

        if vert_len not in VALID_VERT_LENS:
            print(f"[Siege][DIAG] CMO rejected: vert_len=0x{vert_len:X} not in VALID_VERT_LENS")
            return None

        verts_data_len = struct.unpack('<I', r.read(4))[0]
        face_data_len = struct.unpack('<I', r.read(4))[0]
        vertmaps_data_len = struct.unpack('<I', r.read(4))[0]
        unknown_block1_len = struct.unpack('<I', r.read(4))[0]
        face_stat_data_len = struct.unpack('<I', r.read(4))[0]
        face_unknown_data_len = struct.unpack('<I', r.read(4))[0]
        r.read(4 + 4 + 4)  # x24, x28, x2C
        num_lods = struct.unpack('<I', r.read(4))[0]
        mesh_type = struct.unpack('<i', r.read(4))[0]
        num_islands = struct.unpack('<I', r.read(4))[0]
        r.read(4 + 4 + 4 + 4)  # x3C, x40, x44, rng3_len

        if num_islands == 0 or num_lods == 0 or verts_data_len == 0:
            print(f"[Siege][DIAG] CMO rejected: num_islands={num_islands} num_lods={num_lods} verts_data_len={verts_data_len}")
            return None
        if vert_len == 0:
            print(f"[Siege][DIAG] CMO rejected: vert_len=0")
            return None

        num_verts = verts_data_len // vert_len
        vert_block_offset = r.tell()
        tris_block_offset = vert_block_offset + verts_data_len
        extra_data_offset = tris_block_offset + face_data_len
        tail_offset = (extra_data_offset + vertmaps_data_len +
                       unknown_block1_len + face_stat_data_len +
                       face_unknown_data_len)

        # Read island headers (at tail)
        r.seek(tail_offset)
        island_headers = []
        for _ in range(num_islands * num_lods):
            ih_x00 = struct.unpack('<I', r.read(4))[0]
            offset_verts = struct.unpack('<I', r.read(4))[0]
            num_verts_ih = struct.unpack('<I', r.read(4))[0]
            offset_face_chunks = struct.unpack('<I', r.read(4))[0]
            num_face_chunks = struct.unpack('<I', r.read(4))[0]
            mat_id = struct.unpack('<I', r.read(4))[0]
            r.read(12)  # x18, x1C, x20
            island_headers.append({
                'offset_verts': offset_verts,
                'num_verts': num_verts_ih,
                'offset_face_chunks': offset_face_chunks,
                'num_face_chunks': num_face_chunks,
                'mat_id': mat_id,
            })

        # Read bounding boxes (6 floats each, one per base island)
        # followed by skin mappings (0x10C bytes each, one per base island)
        skin_mat_ids = []
        try:
            # Skip bounding boxes: num_islands * 24 bytes
            r.read(num_islands * 24)
            # Read skin mappings
            SKIN_MAPPING_SIZE = 0x10C  # 268 bytes
            for _ in range(num_islands):
                sm_start = r.tell()
                sm_x00 = struct.unpack('<H', r.read(2))[0]
                sm_bones_used = struct.unpack('<B', r.read(1))[0]
                sm_mat_id = struct.unpack('<B', r.read(1))[0]
                skin_mat_ids.append(sm_mat_id)
                sm_x04 = struct.unpack('<H', r.read(2))[0]
                sm_vert_buf_len = struct.unpack('<H', r.read(2))[0]
                sm_len_indices = struct.unpack('<B', r.read(1))[0]
                # Skip remaining bytes to reach next mapping
                remaining = SKIN_MAPPING_SIZE - 9  # 2+1+1+2+2+1 = 9
                if sm_len_indices > 0:
                    remaining -= 4  # x108 at the end
                r.read(remaining)
                if sm_len_indices > 0:
                    r.read(4)  # x108
        except Exception as e:
            print(f"[Siege][MATDIAG] Failed to read skin mappings: {e}")
            skin_mat_ids = []

        # SkinMapping vs ObjectHeader mat_id mismatches were diagnostic-only
        # during decoding; silence in production. Re-enable if a future entity
        # shows wrong material assignment.

        # Read vertices
        r.seek(vert_block_offset)
        vertices = []
        uvs = []

        # SoA layout for the compact vertex formats (0x18 / 0x1C):
        #   pos(8) + norm(4) + tangent(4) + binormal(4) [+ color(4) for 0x1C] + UV(4)
        # UV offset = nv * (vert_len - 4), encoding = half-float, V = 1 - V.
        #
        # The float-position formats (0x24 / 0x28 / 0x2C) have:
        #   pos(12) + norm(4) + tangent(4) + binormal(4) + UV(4) [+ colors]
        # UV offset = nv * 24, encoding = half-float, V stored directly.
        #
        # Verified by brute-force matching 726 barrel UVs against the Ninja
        # Ripper ground truth (606/606 matched with <0.001 error at
        # offset=nv*20, half+vflip for vert_len=0x18).
        if revision in (1, 2):
            if vert_len in (0x24, 0x28, 0x2C):
                for _ in range(num_verts):
                    vertices.append(read_vector3(r))
                r.seek(vert_block_offset + num_verts * 24)
                for _ in range(num_verts):
                    u, v = struct.unpack('<ee', r.read(4))
                    uvs.append((u, v))
            elif vert_len in (0x18, 0x1C):
                for _ in range(num_verts):
                    vertices.append(read_uint64_as_pos(r))
                r.seek(vert_block_offset + num_verts * (vert_len - 4))
                for _ in range(num_verts):
                    u, v = struct.unpack('<ee', r.read(4))
                    uvs.append((u, v))
            elif vert_len == 0x5C:
                for _ in range(num_verts):
                    vertices.append(read_vector3(r))
                    r.read(80)
        elif revision == 0:
            if vert_len in (0x18, 0x1C):
                skip = vert_len - 12
                for _ in range(num_verts):
                    vertices.append(read_uint64_as_pos(r))
                    r.read(skip)
                    u, v = struct.unpack('<ee', r.read(4))
                    uvs.append((u, v))

        if not vertices:
            print(f"[Siege][DIAG] CMO rejected: no vertices parsed (revision={revision}, vert_len=0x{vert_len:X})")
            return None

        # Read vertex remapping tables (vertmaps).
        # The vertmaps block sits at extra_data_offset and contains one
        # uint16[num_verts] array per LOD. Face indices stored in the face
        # chunks are *logical* indices; the vertmap translates them to
        # *physical* indices into the shared vertex/UV buffer.
        vertmaps = []  # vertmaps[lod_idx][logical_idx] -> physical_idx
        if vertmaps_data_len > 0:
            expected_vm_size = num_lods * num_verts * 2
            if vertmaps_data_len == expected_vm_size:
                r.seek(extra_data_offset)
                for _ in range(num_lods):
                    vm = struct.unpack(f'<{num_verts}H', r.read(num_verts * 2))
                    vertmaps.append(vm)
            else:
                print(f"[Siege][DIAG] vertmaps size mismatch: "
                      f"vertmaps_data_len={vertmaps_data_len} != "
                      f"expected={expected_vm_size} "
                      f"(num_lods={num_lods}, num_verts={num_verts})")

        # Diagnostic: dump vertmap ranges per island to understand mapping
        if vertmaps and num_verts > 5000 and island_headers:
            vm = vertmaps[0]
            for idx, ih in enumerate(island_headers):
                ov = ih['offset_verts']
                nv = ih['num_verts']
                if nv == 0:
                    continue
                hi_bound = min(ov + nv, len(vm))
                island_vm_vals = [vm[i] for i in range(ov, hi_bound)]
                if island_vm_vals:
                    print(f"[Siege][DIAG] vertmap island[{idx}] "
                          f"input_range=[{ov},{ov+nv-1}] "
                          f"output_range=[{min(island_vm_vals)},{max(island_vm_vals)}] "
                          f"output_unique={len(set(island_vm_vals))}")

        # Read faces for every LOD. The island_headers array is laid out as
        # [LOD0 island0, LOD0 island1, ..., LOD1 island0, ...]. All LODs share
        # the same vertex buffer; only the face index sets differ.
        #
        # Each island carries a mat_id that indexes into the parent Mesh
        # record's material UID list. We record it per-face so the importer
        # can assign Blender material slots per polygon.
        lods_faces = {}
        lods_headers = {}
        lods_face_mat_ids = {}  # parallel list of mat_id per face
        for lod_idx in range(num_lods):
            lod_slice = island_headers[lod_idx * num_islands:(lod_idx + 1) * num_islands]
            vm = vertmaps[lod_idx] if lod_idx < len(vertmaps) else None
            lod_faces = []
            lod_face_mats = []
            for island_idx, ih in enumerate(lod_slice):
                start = tris_block_offset + ih['offset_face_chunks'] * FACE_CHUNK_SIZE
                r.seek(start)
                island_mat_id = island_idx
                ov = ih['offset_verts']
                # Clamp num_face_chunks against:
                # 1. the face_data block size (bbox/collision placeholders
                #    advertise more chunks than actually fit — same Y9S2 issue
                #    documented in CompiledMeshObject.cs)
                # 2. the actual stream length (decompressed CMOs can be
                #    truncated for placeholders)
                requested = ih['num_face_chunks']
                avail_from_face_data = max(
                    0, (face_data_len - ih['offset_face_chunks'] * FACE_CHUNK_SIZE)
                       // FACE_CHUNK_SIZE)
                avail_from_stream = max(0, (len(data) - start) // FACE_CHUNK_SIZE)
                effective_chunks = min(requested, avail_from_face_data, avail_from_stream)
                for _ in range(effective_chunks):
                    for _ in range(TRIS_IN_CHUNK):
                        a = struct.unpack('<H', r.read(2))[0]
                        b = struct.unpack('<H', r.read(2))[0]
                        c = struct.unpack('<H', r.read(2))[0]
                        # Face indices are local to the island (0-based).
                        # Add offset_verts to form the global logical index,
                        # then remap through the vertmap which returns a
                        # *local* physical index within the island.  Add
                        # offset_verts once more to obtain the global
                        # physical index into the vertex buffer.
                        a += ov; b += ov; c += ov
                        if vm is not None:
                            a, b, c = vm[a] + ov, vm[b] + ov, vm[c] + ov
                        if a != b and b != c and a != c:
                            lod_faces.append((a, b, c))
                            lod_face_mats.append(island_mat_id)
            lods_faces[lod_idx] = lod_faces
            lods_headers[lod_idx] = lod_slice
            lods_face_mat_ids[lod_idx] = lod_face_mats

        # Per-CMO mat-id and large-mesh diagnostic dumps were noisy during
        # decoding — silenced in production. Re-enable locally when a
        # specific mesh renders with wrong materials.

        return {
            'revision': revision,
            'vert_len': vert_len,
            'mesh_type': mesh_type,
            'num_verts': num_verts,
            'num_lods': num_lods,
            'num_islands': num_islands,
            'vertices': vertices,
            'uvs': uvs,
            'lods_faces': lods_faces,
            'lods_headers': lods_headers,
            'lods_face_mat_ids': lods_face_mat_ids,
            # Legacy LOD0 aliases
            'faces': lods_faces.get(0, []),
            'island_headers': lods_headers.get(0, []),
        }

    except Exception as e:
        print(f"[Siege][DIAG] CMO rejected: exception during parse: {e}")
        return None
