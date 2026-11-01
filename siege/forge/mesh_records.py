"""
Mesh and DestructionMeshGroup record parsing.

Both record types live inside a decompressed LoadUnit payload using the
same outer envelope as Entity records (see entity.ENVELOPE_BODY_OFFSETS).
"""

import io
import struct

from ..common.constants import (
    DESTRUCTION_MESH_GROUP_MAGIC,
    MESH_MAGIC,
)
from .entity import ENVELOPE_BODY_OFFSETS


def parse_mesh_entries_from_data(data, quiet=False, cmo_uid_set=None, version=0):
    """
    Parse all Mesh entries from decompressed LoadUnit data.
    Returns (mesh_to_cmo, mesh_to_materials) where:
        mesh_to_cmo:       mesh_uid -> compiled_mesh_object_uid
        mesh_to_materials: mesh_uid -> list[material_uid] (in mat_id order)

    The Mesh record layout (from RainbowForge/RenderPipeline/Mesh.cs, v>=30):
        magic(4) + var1(4) + internal_uid(8) + data(32) +
        num_bones(4) + [bones * 80] +
        magic2(4) + var2(4) + cmo_uid(8) +
        num_materials(4) + [material_uid * 8] +
        internal_uid2(8) + data2(25)

    Y11S2 (forge v34) drops the v30 bones-walk entirely: the cmo_uid sits at a
    FIXED body offset, with the material list right after:
        magic(4) + ... + cmo_uid(8) @ p2+21 + num_materials(4) @ p2+29 +
        [material_uid * 8] @ p2+33
    The v30 reader misparses this (num_bones comes back >1000, so the code
    drops the mesh or lets the cmo fallback scan latch onto a sentinel /
    neighbour CMO — the wrong-geometry + missing-entity symptoms). When
    `version >= 34`, read cmo_uid at p2+21 directly. We record it even when it
    isn't in `cmo_uid_set`, because +21 is the authoritative field: keeping the
    mesh in the table preserves the entity->mesh link and lets the importer
    report an honest `cmo_not_in_index` instead of rendering a wrong CMO.

    v<=29 (Y3S1): the inner body diverges from v>=30 — the post-bones
    `magic2 + var2 + cmo_uid` layout doesn't apply. When the deterministic
    offset yields a value not present in `cmo_uid_set`, fall back to scanning
    the body window for the first u64 that matches a known CMO. Empirically
    this offset is `p2 + 0x14` for ~99.8% of v29 mesh records.

    cmo_uid_set: optional set of valid CMO UIDs (from build_cmo_index). When
    provided, used to validate the parsed cmo_uid and trigger fallback scan
    for old-version forges.

    version: forge version (from read_forge_entries). Selects the v34 fast
    path described above; defaults to 0 (legacy v30/v29 behaviour).

    The island headers' mat_id indexes into that material UID list, so
    capturing it here lets the importer assign per-island Blender material
    slots later.

    When quiet=True, diagnostic prints are suppressed — used when
    scanning sub-regions of entity payloads where false-positive
    MESH_MAGIC hits are expected and noisy.
    """
    mesh_bytes = struct.pack('<I', MESH_MAGIC)
    positions = []
    pos = 0
    while True:
        idx = data.find(mesh_bytes, pos)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 1

    mesh_to_cmo = {}
    mesh_to_materials = {}
    skipped_bones = 0
    skipped_exception = 0
    fallback_scans = 0
    v34_in_index = 0
    v34_not_in_index = 0
    i = 0
    while i < len(positions) - 1:
        p1 = positions[i]
        p2 = positions[i + 1]
        env = ENVELOPE_BODY_OFFSETS.get(p2 - p1)
        if env is not None:
            mesh_uid = struct.unpack('<Q', data[p1+env['uid_off']:p1+env['uid_off']+8])[0]

            # Y11S2 (forge v34) fast path: cmo_uid @ p2+21, num_materials @
            # p2+29, material uids @ p2+33. See the module/function docstring.
            # Read +21 as the authoritative CMO and record it unconditionally
            # (even when it isn't in cmo_uid_set) so the entity->mesh link is
            # preserved and a not-yet-installed CMO surfaces honestly downstream
            # instead of being masked by a wrong sentinel from the v30 fallback.
            if version >= 34 and p2 + 29 <= len(data):
                cmo_uid = struct.unpack_from('<Q', data, p2 + 21)[0]
                if cmo_uid != 0:
                    mesh_to_cmo[mesh_uid] = cmo_uid
                    if cmo_uid_set is not None and cmo_uid in cmo_uid_set:
                        v34_in_index += 1
                    else:
                        v34_not_in_index += 1
                    num_materials = struct.unpack_from('<I', data, p2 + 29)[0]
                    if num_materials <= 256 and p2 + 33 + 8 * num_materials <= len(data):
                        mesh_to_materials[mesh_uid] = [
                            struct.unpack_from('<Q', data, p2 + 33 + 8 * k)[0]
                            for k in range(num_materials)
                        ]
                    i += 2
                    continue
            try:
                r = io.BytesIO(data)
                r.seek(p2 + 4)  # skip magic
                r.read(4)  # var1
                r.read(8)  # internal_uid
                r.read(32)  # data blob
                num_bones = struct.unpack('<I', r.read(4))[0]
                bones_corrupt = num_bones > 1000
                if bones_corrupt:
                    # Body layout doesn't match — happens on Y3S1 v29 inline
                    # mesh records that pack bones differently. Fall back
                    # entirely to the cmo_uid_set scan below; do NOT skip
                    # the mesh (animated/skinned entities show up here).
                    skipped_bones += 1
                    cmo_uid = 0
                else:
                    for _ in range(num_bones):
                        r.read(80)  # bone: padding(8)+magic(4)+matrix(64)+boneId(4)
                    r.read(4 + 4)  # magic2 + var2
                    cmo_uid = struct.unpack('<Q', r.read(8))[0]

                # Validate against the global CMO set when available. If the
                # deterministic offset misses (e.g. v<=29 layout, or bones
                # were corrupt), scan the body window for any u64 that is a
                # known CMO UID.
                if cmo_uid_set is not None and cmo_uid not in cmo_uid_set:
                    body_end = min(p2 + 512, len(data) - 7)
                    for off in range(p2 + 4, body_end):
                        v = struct.unpack_from('<Q', data, off)[0]
                        if v in cmo_uid_set:
                            cmo_uid = v
                            fallback_scans += 1
                            break

                # Skip recording if we still don't have a usable cmo_uid
                # (i.e. neither deterministic read nor fallback found one).
                if cmo_uid == 0 and bones_corrupt:
                    i += 2
                    continue

                mesh_to_cmo[mesh_uid] = cmo_uid
                # Read material UID list when the deterministic body parse
                # was sane. When bones were corrupt, the reader cursor is
                # in an unknown spot, so skip materials.
                if not bones_corrupt:
                    # Clamp to a sane upper bound to avoid eating gigabytes
                    # on a corrupt count. The reader is still parked at the
                    # deterministic offset, so for v<=29 this is a misread
                    # and the downstream count check filters most garbage
                    # out (max 256 materials).
                    num_materials = struct.unpack('<I', r.read(4))[0]
                    if num_materials <= 256:
                        mats = []
                        for _ in range(num_materials):
                            mats.append(struct.unpack('<Q', r.read(8))[0])
                        mesh_to_materials[mesh_uid] = mats
            except Exception as e:
                skipped_exception += 1
                if not quiet:
                    print(f"[Siege][DIAG] Mesh 0x{mesh_uid:016X} parse failed: {e}")
            i += 2
        else:
            i += 1

    if not quiet:
        if v34_in_index or v34_not_in_index:
            print(f"[Siege][DIAG] v34 mesh records: cmo@+21 in-index={v34_in_index}, "
                  f"not-in-index={v34_not_in_index}")
        if skipped_bones:
            print(f"[Siege][DIAG] Mesh entries skipped (num_bones > 1000): {skipped_bones}")
        if skipped_exception:
            print(f"[Siege][DIAG] Mesh entries skipped (parse exception): {skipped_exception}")
        if fallback_scans:
            print(f"[Siege][DIAG] Mesh entries using cmo_uid fallback scan: {fallback_scans}")

    return mesh_to_cmo, mesh_to_materials


def parse_dmg_entries_from_data(data):
    """
    Parse all DestructionMeshGroup (DMG) records from decompressed LoadUnit data.
    Destructible entities (walls, floors, props with DestructionComponent) reference
    a DMG UID inside their payload rather than a Mesh UID directly. Each DMG record
    wraps a Mesh UID, so the full resolution chain is:
        entity_payload -> DMG_UID -> Mesh_UID -> CMO_UID -> geometry

    The DMG record uses the same "magic-then-uid-then-magic" header layout as Entity
    and Mesh records. After the second magic, the body begins with:
        +0: magic echo (4 bytes)
        +4: unknown (4 bytes, seen as 00 00 00 00)
        +8: count/flag (4 bytes, seen as 01 00 00 00)
        +12: Mesh UID (8 bytes)  <-- what we want

    Returns dict: dmg_uid -> mesh_uid.
    """
    dmg_bytes = struct.pack('<I', DESTRUCTION_MESH_GROUP_MAGIC)
    positions = []
    pos = 0
    while True:
        idx = data.find(dmg_bytes, pos)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 1

    dmg_to_mesh = {}
    i = 0
    while i < len(positions) - 1:
        p1 = positions[i]
        p2 = positions[i + 1]
        env = ENVELOPE_BODY_OFFSETS.get(p2 - p1)
        if env is not None:
            dmg_uid = struct.unpack('<Q', data[p1+env['uid_off']:p1+env['uid_off']+8])[0]
            # Body begins at p2; Mesh UID at body+12 (post-magic2)
            mesh_off = p2 + 12
            if mesh_off + 8 <= len(data):
                mesh_uid = struct.unpack('<Q', data[mesh_off:mesh_off+8])[0]
                dmg_to_mesh[dmg_uid] = mesh_uid
            i += 2
        else:
            i += 1

    return dmg_to_mesh
