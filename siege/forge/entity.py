"""
Entity record discovery and per-entity payload scanning.

A LoadUnit blob contains inline Entity records identified by ENTITY_MAGIC.
Once the top-level entity list is recovered, the same payload is re-scanned
to extract per-entity attributes (mesh link, component flags, material
overrides, tint colors, inline child transforms).

Every scanner here bounds its read window to the next entity's
payload_start so values from neighboring entities don't leak across.
"""

import struct

from ..common.constants import (
    COMPONENT_MAGICS,
    COMPONENT_NAME_TO_TAG,
    COMPONENT_TYPE_MAGICS,
    DESTRUCTION_MESH_GROUP_MAGIC,
    ENTITY_MAGIC,
    LIGHT_DEFINITION_MAGICS,
    MATERIAL_TINT_MAGIC,
    MESH_MAGIC,
    WALK_AUTHORITATIVE_TAGS,
)


def _magic_positions(data, magic):
    """All offsets of a 4-byte magic in the blob."""
    pat = struct.pack('<I', magic)
    out = []
    pos = 0
    while True:
        i = data.find(pat, pos)
        if i == -1:
            return out
        out.append(i)
        pos = i + 1


def entity_record_starts(data):
    """Sorted offsets of every Entity record start physically present in `data`.

    Scan bounds MUST come from the blob, never from the entity list a caller
    happens to pass in. map_importer deduplicates entities across LoadUnits
    before handing them to the scanners, so a list-derived "next entity" bound
    silently widens whenever a physical neighbour was dropped as a duplicate —
    on Oregon that stretched one entity's window from 365 bytes to 3964 and let
    its mesh scan reach 2 KB past its own record into unrelated geometry.
    """
    return sorted(_magic_positions(data, ENTITY_MAGIC))


def record_starts(data):
    """Sorted offsets of every top-level record start (Entity / Mesh / DMG).

    An entity's payload cannot run past the next record of ANY kind, so this is
    the structural limit for the raw uid sweep — as opposed to a tuned byte cap.
    Without it, an entity that is genuinely the last one in a sparse region kept
    scanning until it hit the LoadUnit's shared Mesh-record block and adopted
    whatever Mesh uid it found there.
    """
    out = []
    for magic in (ENTITY_MAGIC, MESH_MAGIC, DESTRUCTION_MESH_GROUP_MAGIC):
        out.extend(_magic_positions(data, magic))
    return sorted(set(out))


def _next_after(sorted_positions, pos, default):
    """First position strictly greater than `pos`, else `default`."""
    import bisect
    i = bisect.bisect_right(sorted_positions, pos)
    return sorted_positions[i] if i < len(sorted_positions) else default


# Safety valve for the component walk — no observed entity comes close.
_MAX_COMPONENTS = 512


def parse_entity_components(data, ent, end):
    """Enumerate an entity's components. Returns [(offset, type_magic, next_offset)].

    THE ENTITY BODY IS A COMPONENT LIST. Immediately after the transform matrix,
    the record continues as consecutive component entries:

        [internal_uid : u64]   sequential, base + 0, base + 1, base + 2, ...
        [type_magic   : u32]
        [payload      : type-dependent length]

    Because the internal uids are strictly sequential AND laid out in increasing
    byte order, the list can be walked exactly without knowing a single payload
    length: component N+1 begins wherever `base + N + 1` appears. Verified on
    Y5S3 Oregon at 860/860 entities and on the v32 "Siege X" build at 1261/1261.

    Two things are read from the data rather than assumed, which is what makes
    this portable:

    * `body_start` comes from the matrix offset the record parser actually used
      (matrix + 64), so the v<=29 envelope — whose matrix sits one byte earlier —
      works unchanged. v29 lands on +68, v30..v32 on +69.
    * The uid base is simply the u64 at body_start, i.e. component 0's own uid.
      It is NOT a constant: v30 uses 0xF8000000, v32/v34 an 0xFBF8xxxx base, v29 a
      small integer. Nothing here needs updating when it moves again.

    The type magics themselves ARE stable: 0x4AEC3476 (component 0 on every
    entity), 0xEACD765A, 0x7855A3D0, 0x164984BA and friends appear unchanged from
    Y5S3 through the v32 builds. NOTE these are a different namespace from the
    legacy `COMPONENT_MAGICS` table, which occurs only a few dozen times per
    LoadUnit and is not the per-entity component type set.
    """
    body_start = ent.get('body_start')
    if body_start is None or body_start + 12 > end:
        return []
    base = struct.unpack_from('<Q', data, body_start)[0]
    # Component uids are 32-bit values; a 64-bit-looking value here means this
    # record isn't a component list (or we mis-located the matrix).
    if base == 0 or (base >> 32) != 0:
        return []

    starts = [body_start]
    nn = 1
    while nn <= _MAX_COMPONENTS:
        nxt = data.find(struct.pack('<Q', base + nn), starts[-1] + 12, end)
        if nxt == -1:
            break
        starts.append(nxt)
        nn += 1

    out = []
    for i, off in enumerate(starts):
        stop = starts[i + 1] if i + 1 < len(starts) else end
        if off + 12 > len(data):
            break
        out.append((off, struct.unpack_from('<I', data, off + 8)[0], stop))
    return out


# Outer-envelope layouts for inline records (Entity, Mesh, DMG) inside a
# LoadUnit blob. Two variants observed across game versions:
#   v>=30 (Y5S3+): magic(4) + uid(8) + magic2(4) + body            => gap=12
#   v<=29 (Y3S1):  magic(4) + var1(8) + uid(8) + magic2(4) + body  => gap=20
# After the second magic, the entity-record body begins with:
#   v>=30: unk1(1) + matrix(64) + ...
#   v<=29: matrix(64) + ...                            (no unk1 byte)
# The gap is what we use to discriminate — each occurrence is checked,
# so a single LoadUnit can contain mixed layouts (in practice it doesn't,
# but mixing is harmless).
ENVELOPE_BODY_OFFSETS = {
    12: {'uid_off': 4,  'matrix_off': 5},   # v>=30 layout
    20: {'uid_off': 12, 'matrix_off': 4},   # v<=29 (Y3S1) layout
}

# Y11S2 (v34) keeps the gap-12 envelope but the entity-record body drops the
# unk1 byte, so the matrix sits one byte earlier (+4 instead of +5). Rather than
# thread the forge version down here, _read_entity_matrix tries the table offset
# and the one-byte-earlier offset and keeps whichever decodes to a valid affine
# transform (bottom row ~[0,0,0,1], finite, sane magnitude). This fixes v34
# without changing the v30-33 result.
_MATRIX_OFFSET_CANDIDATES = (0, -1)


def _is_affine_matrix(m):
    """True when a 16-float column-major matrix looks like a real affine TRS.

    Bottom row must be ~[0,0,0,1] and every element finite and within a sane
    world-space magnitude (Siege maps fit in a few hundred metres; mis-parsed
    matrices come back near FLT_MAX).
    """
    import math
    for x in m:
        if math.isinf(x) or math.isnan(x) or abs(x) > 1.0e6:
            return False
    return (abs(m[3]) < 1e-3 and abs(m[7]) < 1e-3
            and abs(m[11]) < 1e-3 and abs(m[15] - 1.0) < 1e-3)


def _read_entity_matrix(data, matrix_start):
    """Decode the 16-float matrix at matrix_start, retrying one byte earlier for
    the v34 layout. Returns (matrix_raw, used_offset) or (None, None)."""
    for delta in _MATRIX_OFFSET_CANDIDATES:
        ms = matrix_start + delta
        if ms < 0 or ms + 64 > len(data):
            continue
        m = struct.unpack_from('<16f', data, ms)
        if _is_affine_matrix(m):
            return m, ms
    return None, None


def parse_entities_from_data(data):
    """
    Parse all entities from decompressed LoadUnit data.
    Returns list of dicts: {uid, matrix_raw (16 floats column-major)}.

    Two passes, unioned:

    1. The pair walk. An entity record is `[uid(8)][ENTITY_MAGIC][unk1][matrix]`,
       and the 4 bytes *preceding* the uid are very often ENTITY_MAGIC too, which
       makes records look like magic-uid-magic triplets with a 12-byte gap.
    2. Magic-anchored recovery, for records the pair walk can't see.

    Pass 1 alone drops every record whose preceding field happens to hold
    something else (a count, or the tail of a float array). On Oregon that is
    exactly what hid the mobile home (0x3B14A59073) and all four cranes: their
    lead-in fields read 0x00000005 / 0x00000004 / 1.0f instead of the magic, so
    the pair walk skipped them and they only ever reappeared — indirectly, and
    only sometimes — through inline child-transform recovery.

    Pass 2 therefore anchors on the magic itself and reads the uid at magic-8,
    keeping the hit only when the body decodes to a genuine affine transform.
    That validation is what stops it swallowing the 12-byte-stride reference
    arrays that make up most of the other magic occurrences.
    """
    entity_bytes = struct.pack('<I', ENTITY_MAGIC)
    positions = []
    pos = 0
    while True:
        idx = data.find(entity_bytes, pos)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 1

    entities = []
    i = 0
    while i < len(positions) - 1:
        p1 = positions[i]
        p2 = positions[i + 1]
        env = ENVELOPE_BODY_OFFSETS.get(p2 - p1)
        if env is not None:
            uid = struct.unpack('<Q', data[p1+env['uid_off']:p1+env['uid_off']+8])[0]
            matrix_start = p2 + env['matrix_off']
            if matrix_start + 64 <= len(data):
                # Validate + auto-detect the offset (handles v34's -1 shift). A
                # mis-parsed matrix near FLT_MAX would otherwise place the entity
                # at infinity and break downstream transform/export. Fall back to
                # identity when neither candidate offset decodes to a sane affine.
                matrix_raw, _ms = _read_entity_matrix(data, matrix_start)
                if matrix_raw is None:
                    matrix_raw = (1.0, 0.0, 0.0, 0.0,
                                  0.0, 1.0, 0.0, 0.0,
                                  0.0, 0.0, 1.0, 0.0,
                                  0.0, 0.0, 0.0, 1.0)
                    _ms = matrix_start
                entities.append({
                    'uid': uid,
                    'matrix_raw': matrix_raw,
                    'payload_start': p2,
                    # The component list starts right after the matrix. Carrying
                    # the offset the matrix was actually decoded at keeps
                    # parse_entity_components correct across envelope variants
                    # (v<=29 sits one byte earlier than v30+).
                    'body_start': _ms + 64,
                })
            i += 2
        else:
            i += 1

    # Pass 2: magic-anchored recovery (see docstring). Only magics the pair walk
    # didn't already claim are considered, and a candidate has to decode to a
    # valid affine matrix — reference-array entries and random magic-valued bytes
    # don't, so they're rejected rather than injected as phantom entities.
    claimed = {e['payload_start'] for e in entities}
    seen_uids = {e['uid'] for e in entities}
    recovered = []
    for pm in positions:
        if pm in claimed or pm < 8:
            continue
        matrix_raw, _ms = _read_entity_matrix(data, pm + 5)
        if matrix_raw is None:
            continue
        uid = struct.unpack_from('<Q', data, pm - 8)[0]
        if uid == 0 or uid in seen_uids:
            continue
        seen_uids.add(uid)
        recovered.append({
            'uid': uid,
            'matrix_raw': matrix_raw,
            'payload_start': pm,
            'body_start': _ms + 64,
        })

    if recovered:
        entities.extend(recovered)
        # Downstream scanners bound each entity's read window by the next
        # entity's payload_start, so the combined list has to stay ordered.
        entities.sort(key=lambda e: e['payload_start'])

    return entities


def find_entity_mesh_links(data, entities, mesh_uid_set, dmg_uid_set=None):
    """
    Scan entity payloads to find which Mesh UID each entity references.

    Pass 1: direct scan at +69..+300 for either
            (a) a Mesh UID in the global set (simple static entities), or
            (b) a DMG UID in the global set (destructibles — walls, floors,
                destructible props with DestructionComponent). The DMG UID
                is then resolved to a real Mesh UID via the dmg_uid_set's
                caller-provided mapping applied after this function returns.
    Pass 2: scan for [sub_uid][29 8D 65 EC]...[mesh_uid] sub-mesh markers
            (compound entities with multiple sub-meshes).

    Both passes bound their scan to the next entity's payload_start to
    prevent mesh references from bleeding across entity boundaries.

    Returns (entity_to_mesh, entity_to_submeshes, entity_mesh_offsets) where
    entity_to_mesh maps entity_uid -> (Mesh or DMG) UID,
    entity_to_submeshes maps entity_uid -> list of mesh_uids for compound
    entities, and entity_mesh_offsets maps entity_uid -> absolute offset
    in data where the mesh UID was found (used by material override scanner).
    """
    MESH_REF_MARKER = b'\x29\x8D\x65\xEC'
    dmg_uid_set = dmg_uid_set or set()
    # Union of Mesh + DMG UIDs that Pass 1 treats as valid link targets.
    link_target_set = mesh_uid_set | dmg_uid_set

    entity_to_mesh = {}
    entity_to_submeshes = {}
    entity_mesh_offsets = {}  # entity_uid -> absolute data offset of mesh UID

    # Per-entity payload bounds. Both position lists come from the BLOB, not
    # from `entities` — see entity_record_starts() for why that distinction is
    # load-bearing (callers pass a deduplicated list).
    sorted_starts = entity_record_starts(data)
    all_record_starts = record_starts(data)

    def _payload_bound(ent, hard_cap):
        """Bound at the next Entity record (or hard_cap), for the record-shaped
        passes that legitimately read inline Mesh records inside a payload."""
        ps = ent['payload_start']
        next_ps = _next_after(sorted_starts, ps, None)
        max_end = ps + hard_cap
        if next_ps is not None:
            max_end = min(max_end, next_ps)
        return min(max_end, len(data))

    def _structural_bound(ent):
        """Bound at the next record of ANY kind — the hard structural limit for
        the raw uid sweep in Pass 1, which has no anchor of its own and would
        otherwise adopt the first in-set u64 it stumbles across."""
        ps = ent['payload_start']
        return min(_next_after(all_record_starts, ps, len(data)), len(data))

    # Pass A (preferred): read the link out of the entity's own COMPONENT LIST.
    # This is the only pass with a structural guarantee — parse_entity_components
    # returns exact per-component byte ranges derived from the sequential internal
    # uids, so a uid found here provably belongs to this entity. The scanning
    # passes below only run for records whose component list can't be walked.
    #
    # We search components in list order and take the first mesh/DMG uid. On Y5S3
    # Oregon the link concentrates in a handful of component types — 0xEACD765A
    # (63.5%), 0x25AA1CCF (25.6%), 0x49FCD7BF (6.4%), 0xCF153BBA (3.0%) — but we
    # deliberately don't filter on type: the component bound is what makes this
    # correct, and an unknown type carrying a valid mesh ref is still this
    # entity's mesh.
    component_linked = set()
    for ent in entities:
        comps = parse_entity_components(data, ent, _structural_bound(ent))
        if not comps:
            continue
        for c_off, _magic, c_end in comps:
            stop = min(c_end, len(data) - 7)
            hit = None
            for off in range(c_off + 12, stop):
                val = struct.unpack_from('<Q', data, off)[0]
                if val in link_target_set:
                    hit = (val, off)
                    break
            if hit is not None:
                entity_to_mesh[ent['uid']] = hit[0]
                entity_mesh_offsets[ent['uid']] = hit[1]
                component_linked.add(ent['uid'])
                break

    # Pass 0: scan for inline Mesh records inside the entity payload.
    # Some Y3S1 entities (cars, multi-piece props) embed multiple Mesh
    # records (with the same magic+uid+magic envelope used by top-level
    # mesh records, just inlined into the entity blob). Without this,
    # Pass 1 picks the first match and the rest are silently dropped —
    # the user-visible "partial load" symptom.
    #
    # Layout observed on Y3S1 cars: each LOD cluster is a sequence of
    # paired Mesh-magic records, where the FIRST pair in the cluster is
    # the primary mesh (no MeshObjectShapeData prefix) and SIBLING pairs
    # are prefixed with the `MeshObjectShapeData` magic (0x95CA575A,
    # bytes 5A 57 CA 95) within ~50 bytes. The same entity may store
    # multiple LOD clusters back-to-back; we want only the LOD0 cluster
    # (highest detail) since each CMO already contains its own internal
    # LODs. Stop when we see another non-MOSD-prefixed pair after we
    # already collected siblings — that's the start of LOD1.
    #
    # Hard cap at 8 KiB so we don't bleed into the shared mesh storage
    # block that follows the entity records in big LUs.
    INLINE_MESH_MAGIC_BYTES = struct.pack('<I', MESH_MAGIC)
    MOSD_BYTES = b'\x5a\x57\xca\x95'  # MeshObjectShapeData magic
    MOSD_LOOKBACK = 64  # bytes before a pair to scan for MOSD prefix
    PASS0_HARD_CAP = 8192  # per-entity scan cap
    for ent in entities:
        ps = ent['payload_start']
        scan_end = min(_payload_bound(ent, PASS0_HARD_CAP), len(data) - 4)
        scan_start = ps + 69  # past magic + matrix
        if scan_end <= scan_start:
            continue
        # Find all Mesh-magic positions in this entity payload
        positions = []
        pos = scan_start
        while pos < scan_end:
            idx = data.find(INLINE_MESH_MAGIC_BYTES, pos, scan_end)
            if idx == -1:
                break
            positions.append(idx)
            pos = idx + 1
        if len(positions) < 2:
            continue
        # Walk paired positions and collect LOD0 cluster only.
        inline_meshes = []
        seen = set()
        collected_sibling = False
        i = 0
        while i < len(positions) - 1:
            p1 = positions[i]
            p2 = positions[i + 1]
            env = ENVELOPE_BODY_OFFSETS.get(p2 - p1)
            if env is None:
                i += 1
                continue
            # Check whether MOSD precedes this pair (sibling sub-mesh).
            lookback_start = max(scan_start, p1 - MOSD_LOOKBACK)
            has_mosd = MOSD_BYTES in data[lookback_start:p1]
            # Stop condition: encountering a primary (non-MOSD) pair after
            # we already collected at least one sibling — that means the
            # current LOD cluster ended and the next one started.
            if collected_sibling and not has_mosd:
                break
            uid_pos = p1 + env['uid_off']
            if uid_pos + 8 <= len(data):
                uid = struct.unpack('<Q', data[uid_pos:uid_pos+8])[0]
                if uid in mesh_uid_set and uid not in seen:
                    inline_meshes.append(uid)
                    seen.add(uid)
                    if has_mosd:
                        collected_sibling = True
            i += 2
        if len(inline_meshes) >= 2 and collected_sibling:
            # Compound entity — capture LOD0 sub-meshes only. This multi-mesh
            # case is the entire reason Pass 0 exists (Pass 1 stops at the first
            # match and would drop the siblings).
            #
            # `collected_sibling` is REQUIRED: a sub-mesh is marked by its MOSD
            # prefix, so a run of mesh records with no MOSD among them is not a
            # compound entity at all — it is the LoadUnit's shared mesh table,
            # which begins right after the entity records. Without this guard an
            # entity whose payload happens to abut that table adopts a dozen
            # unrelated meshes and joins them at its own transform, producing
            # geometry from all over the map fused into one object (Kanal:
            # 12 meshes spread +/-1500 units on a ~200-unit map, visible as
            # shards floating over the docks).
            entity_to_submeshes[ent['uid']] = inline_meshes
        # A SINGLE inline mesh is deliberately NOT linked here. It's redundant
        # with Pass 1, which reaches the same uid whenever the record really is
        # inside the entity's payload — and unlike Pass 0, Pass 1 is bounded by
        # the next record start. Pass 0's window is the next *Entity* record,
        # which for a sparse region can be tens of KB, so this branch used to
        # adopt an isolated Mesh record from the LoadUnit's shared mesh block:
        # Oregon entity 0x3FF171DA7F picked one up 3690 bytes downstream and
        # imported as bomb-site geometry.

    # Pass 1: direct scan starting at +69 (after magic + matrix).
    # Originally capped at +300 because Y5S4+ entities place the mesh UID
    # near the matrix, but Y3S1 entities pack the mesh UID much later in
    # the payload (commonly +0x130..+0x600). Empirical sweep on Y3S1
    # Resort: widening to full payload bound recovers ~295 more links
    # without false positives, since `link_target_set` membership is a
    # strong filter.
    for ent in entities:
        # Skip entities Pass 0 already resolved (single or compound).
        if ent['uid'] in entity_to_mesh or ent['uid'] in entity_to_submeshes:
            continue
        scan_start = ent['payload_start'] + 69
        # Structural bound, NOT the 8 KiB cap: this sweep accepts the first u64
        # that is a member of a ~3000-entry global set, so anything it reads past
        # the end of the entity's own record is a coin flip. Two Oregon entities
        # picked up the same unrelated mesh 2 KB and 3.7 KB downstream and
        # imported as bomb-site geometry.
        scan_end = min(_structural_bound(ent), len(data) - 7)
        for off in range(scan_start, scan_end):
            val = struct.unpack('<Q', data[off:off+8])[0]
            if val in link_target_set:
                entity_to_mesh[ent['uid']] = val
                entity_mesh_offsets[ent['uid']] = off
                break

    # Pass 2: for unmatched entities, collect sub-mesh UIDs via the
    # [marker 29 8D 65 EC]...[mesh_uid] signature. The marker-to-UID
    # distance differs by version:
    #   v>=30 (Y5S4+): marker + 10 bytes of metadata + uid → uid_off = idx+14
    #   v<=29 (Y3S1):  marker + 2 bytes + uid             → uid_off = idx+6
    # We try both offsets and accept whichever hits the mesh_uid_set first.
    # The marker can also prefix non-mesh references (child entity IDs,
    # etc.), so the set membership is the actual filter.
    MARKER_UID_OFFSETS = (14, 6)
    for ent in entities:
        if ent['uid'] in entity_to_mesh or ent['uid'] in entity_to_submeshes:
            continue
        scan_start = ent['payload_start'] + 69
        scan_end = min(_payload_bound(ent, 16384), len(data) - 24)
        if scan_end <= scan_start:
            continue
        submeshes = []
        seen = set()
        pos = scan_start
        while pos < scan_end:
            idx = data.find(MESH_REF_MARKER, pos, scan_end)
            if idx == -1:
                break
            for off_delta in MARKER_UID_OFFSETS:
                uid_off = idx + off_delta
                if uid_off + 8 > len(data):
                    continue
                val = struct.unpack('<Q', data[uid_off:uid_off+8])[0]
                if val in mesh_uid_set and val not in seen:
                    submeshes.append(val)
                    seen.add(val)
                    break
            pos = idx + 1
        if submeshes:
            entity_to_submeshes[ent['uid']] = submeshes

    # Pass 3: deferred marker-based fallback for entities that still have
    # no link. Same dual-offset rule as Pass 2 — Y5S4+ stores the UID at
    # marker+14; Y3S1 at marker+6. Pass 1 (now full-payload) usually catches
    # these, but the marker anchor stays as a safety net for cases where
    # the first u64 hit is a stray reference and the right one is later.
    VISUAL_MARKER = b'\x29\x8D\x65\xEC'
    for ent in entities:
        if ent['uid'] in entity_to_mesh or ent['uid'] in entity_to_submeshes:
            continue
        ps = ent['payload_start']
        scan_start = ps + 69
        scan_end = min(_payload_bound(ent, 8192), len(data) - 7)
        if scan_end <= scan_start:
            continue
        marker_idx = data.find(VISUAL_MARKER, scan_start, scan_end)
        if marker_idx == -1:
            continue
        for off_delta in MARKER_UID_OFFSETS:
            uid_off = marker_idx + off_delta
            if uid_off + 8 > len(data):
                continue
            val = struct.unpack('<Q', data[uid_off:uid_off+8])[0]
            if val in link_target_set:
                entity_to_mesh[ent['uid']] = val
                entity_mesh_offsets[ent['uid']] = uid_off
                break

    return entity_to_mesh, entity_to_submeshes, entity_mesh_offsets


def find_entity_child_transforms(data, entities, link_target_set=None):
    """
    Scan entity payloads for inline child-transform records.

    In Y5S4+ forges, parent entities that represent a group of identical
    props (trailers, door footers, bathroom tiles, ceiling lights, excavator
    pieces, etc.) don't emit a separate top-level Entity record per instance.
    Instead, each instance is embedded inside the parent's payload as:

        [child_uid: 8 bytes]
        [ENTITY_MAGIC: 4 bytes]
        [unk1: 1 byte = 0x03]
        [matrix: 64 bytes, 16 floats column-major]
        [tail: ~200 bytes including the child's own Mesh/DMG UID]

    The child UID's upper 3 bytes are zero, which is why Todo.txt only
    captured the low 40 bits of these entities. There is NO preceding
    ENTITY_MAGIC, which is what makes the top-level parser skip them —
    it requires the magic/uid/magic triplet.

    When ``link_target_set`` (the global Mesh+DMG UID set) is provided,
    each child's post-matrix tail is scanned for its own mesh UID. The
    mesh UID usually sits at +125/+146 or +164/+185 or +172/+193 past the
    tail start (observed across trailers, door footers, bath tiles,
    ceiling lights, excavator pieces) — the first UID in the tail that
    matches a Mesh/DMG record is accepted. Without this, children inherit
    the parent's mesh which for soft-wall parents is the wrong geometry.

    Returns list of dicts:
        {uid, matrix_raw, payload_start, parent_uid, mesh_uid?}
    The payload_start points at the inline ENTITY_MAGIC so downstream
    scanners can still search the child's local tail (components, etc.).
    """
    if not entities:
        return []

    ent_magic_bytes = struct.pack('<I', ENTITY_MAGIC)

    # Blob-derived, not list-derived — see entity_record_starts().
    sorted_starts = entity_record_starts(data)
    next_start_map = {s: _next_after(sorted_starts, s, None) for s in sorted_starts}

    # UIDs of the top-level entities — we must not re-emit any of these
    # as children even if their byte pattern happens to match.
    top_level_uids = {e['uid'] for e in entities}

    children = []
    seen_uids = set()
    data_len = len(data)

    for ent in entities:
        ps = ent['payload_start']
        next_ps = next_start_map.get(ps)
        # Bound to next entity to avoid bleeding across boundaries.
        end = next_ps if next_ps is not None else ps + 16384
        end = min(end, ps + 16384, data_len)
        # Start after the parent's own header+matrix (magic4 + unk1 1 + matrix 64 = 69).
        scan = ps + 69
        while scan < end:
            idx = data.find(ent_magic_bytes, scan, end)
            if idx == -1:
                break
            # Must be followed by unk1 = 0x03 and have 64 bytes of matrix.
            if idx + 5 + 64 > end:
                break
            if data[idx + 4] != 0x03:
                scan = idx + 1
                continue
            # The 8 bytes immediately before the magic are the child UID.
            uid_off = idx - 8
            if uid_off < ps + 5:
                # Not enough room for a valid preceding UID — skip.
                scan = idx + 1
                continue
            child_uid = struct.unpack('<Q', data[uid_off:uid_off+8])[0]
            # Heuristic: a valid inline child UID has its top 24 bits zero
            # (engine-observed: Todo.txt short UIDs are 5-byte / 40-bit).
            # Reject anything where the upper 3 bytes are set OR the UID is
            # zero/float-like (e.g. 0x3F800000 = 1.0f).
            if child_uid == 0 or (child_uid >> 40) != 0:
                scan = idx + 1
                continue
            if child_uid in top_level_uids or child_uid in seen_uids:
                scan = idx + 1
                continue

            # Decode + validate the matrix, auto-detecting the v34 -1 shift.
            # _is_affine_matrix is stricter than the old [15]~1.0 check (it also
            # rejects FLT_MAX garbage), filtering false positives landing
            # mid-payload as well as mis-parsed transforms.
            matrix_raw, matrix_start = _read_entity_matrix(data, idx + 5)
            if matrix_raw is None:
                scan = idx + 1
                continue

            seen_uids.add(child_uid)

            # Scan the child's own post-matrix tail for its Mesh/DMG UID.
            # Bound the tail at the next inline child record (if any) or
            # at the parent's end. Observed hit range is +120..+210 bytes
            # past the matrix end, but we widen to 512 to be safe.
            child_mesh = None
            if link_target_set:
                tail_start = matrix_start + 64
                tail_end = min(tail_start + 512, end - 7)
                t = tail_start
                while t < tail_end:
                    v = struct.unpack_from('<Q', data, t)[0]
                    if v in link_target_set:
                        child_mesh = v
                        break
                    t += 1

            children.append({
                'uid': child_uid,
                'matrix_raw': matrix_raw,
                'payload_start': idx,  # absolute offset of the inline magic
                'parent_uid': ent['uid'],
                'mesh_uid': child_mesh,
            })
            # Skip past the decoded matrix so we don't re-scan its bytes.
            scan = matrix_start + 64

    # Y3S1 (v<=29) inline child entities use the gap-20 envelope (the same
    # outer header used by top-level entity records, just inlined inside
    # another entity's payload). Layout from the start of the first magic:
    #   magic(4) + var1(4) + var2(4) + child_uid(8) + magic2(4) + matrix(64)
    # Total 24 + 64 = 88 bytes minimum for the envelope. Used by animated /
    # cutscene entities that wrap a sub-entity (e.g. mounted weapon, swing
    # door) with its own mesh ref.
    for ent in entities:
        ps = ent['payload_start']
        next_ps = next_start_map.get(ps)
        end = next_ps if next_ps is not None else ps + 16384
        end = min(end, ps + 16384, data_len)
        scan = ps + 69
        # Find consecutive ENTITY_MAGIC positions and detect gap=20 pairs
        positions = []
        pos = scan
        while pos < end:
            i = data.find(ent_magic_bytes, pos, end)
            if i == -1: break
            positions.append(i); pos = i + 1
        if len(positions) < 2: continue
        i = 0
        while i < len(positions) - 1:
            p1 = positions[i]
            p2 = positions[i + 1]
            if p2 - p1 != 20:
                i += 1
                continue
            # Y3S1 envelope: child_uid is at p1 + 12 (after var1+var2)
            child_uid = struct.unpack('<Q', data[p1+12:p1+20])[0]
            if child_uid == 0 or child_uid in top_level_uids or child_uid in seen_uids:
                i += 2
                continue
            # Matrix starts at p2 + 4 (no unk1 byte in v29). Validate via the
            # shared affine check (also rejects FLT_MAX garbage).
            matrix_start = p2 + 4
            if matrix_start + 64 > end:
                i += 2
                continue
            matrix_raw, matrix_start = _read_entity_matrix(data, matrix_start)
            if matrix_raw is None:
                i += 2
                continue
            seen_uids.add(child_uid)

            # Look for child's mesh UID in tail (next 1024 bytes)
            child_mesh = None
            if link_target_set:
                tail_start = matrix_start + 64
                tail_end = min(tail_start + 1024, end - 7)
                t = tail_start
                while t < tail_end:
                    v = struct.unpack_from('<Q', data, t)[0]
                    if v in link_target_set:
                        child_mesh = v
                        break
                    t += 1

            children.append({
                'uid': child_uid,
                'matrix_raw': matrix_raw,
                'payload_start': p2,  # absolute offset of second magic
                'parent_uid': ent['uid'],
                'mesh_uid': child_mesh,
            })
            i += 2

    return children


def scan_entity_material_overrides(data, entities, entity_to_mesh,
                                   entity_mesh_offsets, mesh_to_materials,
                                   material_uid_set, dmg_to_mesh=None):
    """
    Scan entity payloads for per-entity material override pairs.

    In the Anvil engine, entities can override their Mesh record's default
    material list. The overrides appear in the entity payload as adjacent
    pairs of 8-byte UIDs:  [original_mat_uid][override_mat_uid]
    located between the entity's post-matrix data and the visual component
    marker (0xEC658D29) that precedes the mesh UID reference.

    We scan the region from payload_start+69 up to the mesh UID offset for
    pairs where the first UID is in the entity's Mesh material list and the
    second UID is a known Material UID (from the global pair-scan set).

    entity_to_mesh may contain DMG UIDs (before DMG resolution). When
    dmg_to_mesh is provided, the scanner resolves them to real Mesh UIDs
    so that mesh_to_materials lookups succeed for destructible entities.

    Returns dict: entity_uid -> {original_mat_uid: override_mat_uid}
    """
    VISUAL_MARKER = b'\x29\x8D\x65\xEC'
    dmg_to_mesh = dmg_to_mesh or {}
    overrides = {}

    for ent in entities:
        uid = ent['uid']
        link_uid = entity_to_mesh.get(uid)
        if link_uid is None:
            continue
        mesh_off = entity_mesh_offsets.get(uid)
        if mesh_off is None:
            continue
        # Resolve DMG UID -> real Mesh UID for destructible entities
        mesh_uid = dmg_to_mesh.get(link_uid, link_uid)
        mesh_mats = mesh_to_materials.get(mesh_uid)
        if not mesh_mats:
            continue
        mesh_mat_set = set(mesh_mats)

        ps = ent['payload_start']
        # The override pairs sit between +69 (after matrix) and the visual
        # component marker that precedes the mesh UID.  Scan this window.
        scan_start = ps + 69
        scan_end = mesh_off  # mesh UID absolute offset

        # Find the visual component marker to tighten the end bound — the
        # override pairs appear before it.
        marker_idx = data.find(VISUAL_MARKER, scan_start, scan_end)
        if marker_idx != -1:
            scan_end = marker_idx

        if scan_end - scan_start < 16:
            continue

        ent_overrides = {}
        off = scan_start
        while off + 16 <= scan_end:
            orig = struct.unpack('<Q', data[off:off+8])[0]
            if orig in mesh_mat_set:
                repl = struct.unpack('<Q', data[off+8:off+16])[0]
                if repl != 0 and repl != orig and repl in material_uid_set:
                    ent_overrides[orig] = repl
                    off += 16
                    continue
            off += 1

        if ent_overrides:
            overrides[uid] = ent_overrides

    return overrides


def scan_entity_material_tints(data, entities):
    """
    Scan entity payloads for per-entity material tint color blocks.

    The Anvil engine stores material tint data in entity payloads as
    repeating blocks with the structure:
        MATERIAL_TINT_MAGIC (4 bytes)  -- 0xD7BF79A4
        material_uid        (8 bytes)  -- which material this tint applies to
        RGBA float group 0  (16 bytes) -- the diffuse/base tint color
        RGBA float group 1  (16 bytes) -- (engine-internal, ignored)
        RGBA float group 2  (16 bytes) -- (engine-internal, ignored)
        RGBA float group 3  (16 bytes) -- (engine-internal, ignored)

    Each block is 76 bytes total. Multiple blocks can appear per entity,
    separated by a 9-byte internal-UID gap. Only the first RGBA group
    (group 0) is used as the visible tint color.

    Returns dict: entity_uid -> [(R,G,B,A), ...] where each tuple is
    a 0-255 RGBA color, ordered to match the entity's material list.
    Materials without a tint block get a default (255,255,255,255).
    """
    TINT_PAT = struct.pack('<I', MATERIAL_TINT_MAGIC)
    TINT_BLOCK_SIZE = 4 + 8 + 64  # magic + uid + 4*16 floats

    # Precompute per-entity payload bounds (same pattern as other scanners).
    # Blob-derived, not list-derived — see entity_record_starts().
    sorted_starts = entity_record_starts(data)
    next_start_map = {s: _next_after(sorted_starts, s, None) for s in sorted_starts}

    tints = {}

    for ent in entities:
        uid = ent['uid']
        ps = ent['payload_start']
        next_ps = next_start_map.get(ps)
        scan_end = min(
            next_ps if next_ps is not None else ps + 4096,
            ps + 4096,
            len(data),
        )

        # Scan the payload region for tint magic occurrences.
        # Tint blocks appear after the visual component area, typically
        # starting around payload +230..+260.
        scan_start = ps + 69  # after the matrix
        mat_tints = {}  # material_uid -> (R,G,B,A)

        pos = scan_start
        while pos + TINT_BLOCK_SIZE <= scan_end:
            idx = data.find(TINT_PAT, pos, scan_end)
            if idx == -1:
                break
            if idx + TINT_BLOCK_SIZE > scan_end:
                break

            # Read material UID
            mat_uid = struct.unpack('<Q', data[idx+4:idx+12])[0]

            # Read first RGBA float group (the visible tint color)
            float_off = idx + 12
            r, g, b, a = struct.unpack('<4f', data[float_off:float_off+16])

            # Convert 0.0-1.0 floats to 0-255 bytes
            rb = max(0, min(255, int(round(r * 255))))
            gb = max(0, min(255, int(round(g * 255))))
            bb = max(0, min(255, int(round(b * 255))))
            ab = max(0, min(255, int(round(a * 255))))

            mat_tints[mat_uid] = (rb, gb, bb, ab)

            # Skip past this block
            pos = idx + TINT_BLOCK_SIZE

        if not mat_tints:
            continue

        # Build the tint list ordered to match the entity's material list.
        # Look up which mesh this entity uses to get the material order.
        # We check all mesh_to_materials entries since we don't have the
        # entity-to-mesh mapping at this point — the caller can filter.
        # For now, store the raw {mat_uid: tint} dict; the caller will
        # resolve it against the actual material list.
        tints[uid] = mat_tints

    return tints


def scan_entity_components(data, entities):
    """
    For each entity, scan its payload region for the 4-byte COMPONENT_MAGICS.
    Returns dict: entity_uid -> set of component name strings.

    We bound the scan to the next entity's payload_start (same pattern as
    find_entity_mesh_links Pass 2) so component hits don't bleed across
    entity boundaries. Payloads in a LoadUnit are densely packed, so an
    unbounded scan would mis-attribute components from the following
    entity to the current one.

    False-positive rate: each magic is a fixed 32-bit value, so over a
    ~600-byte payload the expected random-match rate is ~1.4e-7 per magic
    per entity — small enough to ignore. That estimate only holds while the
    window really is payload-sized: it assumes random bytes, and these magics
    also occur as genuine values in neighbouring records, so a window that
    overruns the entity's own record mis-attributes real components rather than
    matching noise. Hence the blob-derived bound below.
    """
    if not entities:
        return {}

    # Bounds come from the blob, not from `entities` — callers pass a list that
    # has been deduplicated across LoadUnits, which silently widens the window
    # for any entity whose physical neighbour was dropped. See
    # entity_record_starts().
    sorted_starts = entity_record_starts(data)
    all_record_starts = record_starts(data)

    # Pre-pack each magic to its little-endian 4-byte pattern once.
    magic_patterns = [
        (struct.pack('<I', m), name) for m, name in COMPONENT_MAGICS.items()
    ]

    entity_components = {}
    data_len = len(data)
    for ent in entities:
        ps = ent['payload_start']
        # Start after the matrix (magic 4 + unk1 1 + matrix 64 = 69).
        scan_start = ps + 69
        # Stop at the next record of any kind. The old 16 KiB fallback applied
        # whenever there was no following entity in the passed list, which made
        # a trailing entity absorb the components of everything after it.
        scan_end = min(_next_after(all_record_starts, ps, data_len), data_len)
        if scan_end <= scan_start:
            continue

        window = data[scan_start:scan_end]
        found = set()
        for pattern, name in magic_patterns:
            if pattern in window:
                found.add(name)

        # Exact path: read the entity's real component types out of its component
        # list and translate them through the Heated Metal-derived name table.
        # COMPONENT_MAGICS (scanned above) is a DIFFERENT namespace — it occurs
        # only a few dozen times per LoadUnit, so the window scan attributes
        # whatever happens to be nearby. For the tags the walk owns we therefore
        # discard the scan's opinion instead of unioning it in; measured against
        # the Heated Metal dump, unioning dropped 'space' precision to 0%.
        # Tags with no confident mapping yet ('visual', 'inert') keep coming from
        # the scan, which is empirically precise for them.
        walk_tags = set()
        comps = parse_entity_components(data, ent, scan_end)
        for _c_off, magic, _c_end in comps:
            name = COMPONENT_TYPE_MAGICS.get(magic)
            if name is None:
                continue
            tag = COMPONENT_NAME_TO_TAG.get(name)
            if tag:
                walk_tags.add(tag)
        if comps:
            found -= WALK_AUTHORITATIVE_TAGS
        found |= walk_tags

        if found:
            entity_components[ent['uid']] = found

    return entity_components


def find_light_definitions(data):
    """Find standalone light-definition records inside a LoadUnit blob.

    SpotLight/OmniLight/SkyLight records use the gap-12 `magic + uid(8) + magic`
    envelope (the same shape as Entity/Mesh records) and live in a shared
    lighting block. The record carries the light's colour/range — not a world
    transform — so on its own it can't be placed. We only need the uid->kind map
    here; placement is recovered from entities that reference these uids.

    Returns dict: light_uid -> component name ('spot_light'/'omni_light'/'sky_light').
    """
    out = {}
    for magic, name in LIGHT_DEFINITION_MAGICS.items():
        mb = struct.pack('<I', magic)
        pos = 0
        while True:
            i = data.find(mb, pos)
            if i == -1:
                break
            pos = i + 1
            # gap-12 envelope: the same magic repeats 12 bytes on, uid in between.
            if i + 16 <= len(data) and struct.unpack_from('<I', data, i + 12)[0] == magic:
                out[struct.unpack_from('<Q', data, i + 4)[0]] = name
    return out


def scan_entity_light_refs(data, entities, light_uids):
    """Find which entities place a light by referencing a light-definition uid.

    A light placement entity carries the light's transform (its entity matrix)
    and references the light record's uid in its payload (observed at ~+110..+210,
    occasionally further). The light record itself has no world position, so the
    referencing entity is what positions the light. We map each such entity to the
    referenced light's kind so the caller can add it as a synthetic component and
    route the entity into the LIGHT category.

    light_uids: dict light_uid -> component name (from find_light_definitions).
    Returns dict: entity_uid -> component name.

    Membership against the small, specific light-uid set makes false positives
    negligible; the per-entity window bound also rejects the light records' own
    self-reference (they sit far past any entity's payload window).
    """
    if not light_uids or not entities:
        return {}

    import bisect
    starts = sorted(e['payload_start'] for e in entities)
    uid_by_start = {e['payload_start']: e['uid'] for e in entities}
    next_start = {}
    for i, s in enumerate(starts):
        next_start[s] = starts[i + 1] if i + 1 < len(starts) else None

    out = {}
    for luid, name in light_uids.items():
        needle = struct.pack('<Q', luid)
        pos = 0
        while True:
            idx = data.find(needle, pos)
            if idx == -1:
                break
            pos = idx + 1
            j = bisect.bisect_right(starts, idx) - 1
            if j < 0:
                continue
            ps = starts[j]
            nxt = next_start.get(ps)
            end = min(nxt if nxt is not None else ps + 2048, ps + 2048)
            # Reference must sit in the entity's own payload window (past the
            # header+matrix), not in the trailing lighting block.
            if ps + 69 <= idx < end:
                out.setdefault(uid_by_start[ps], name)
    return out
