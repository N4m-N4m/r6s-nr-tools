"""
Y11S2 (forge v34) map parsing.

v34 re-hashed the map record magics and changed a few offsets, so the v33
scanners in entity.py / mesh_records.py don't apply directly. This module
isolates the v34 map path: identify LoadUnit asset blocks, parse the Entity
placement records and Mesh->CMO records, and link entities to meshes. It
returns plain dicts in the same shape the map importer's Blender-build phase
already consumes, so the downstream object creation is unchanged.

Everything here was verified against live v34 bytes — see
research/y11s2_map_layout.md.
"""

import struct

from ..common.constants import (
    DMG_MAGIC_V34,
    ENTITY_MAGIC_V34,
    LOADUNIT_MAGIC_V34,
    MESH_MAGIC_V34,
    VISUAL_COMPONENT_MAGIC_V34,
)

# CMO uid at this offset from the Mesh record's second magic (p2). Verified on
# university: external (non-internal) uid at +21 for every sampled mesh record.
MESH_CMO_BODY_OFFSET = 21
# Entity placement matrix begins directly after the second magic (v34 dropped
# the v33 unk1 byte): p2 + 4. Matrix is 16 floats, row-major.
ENTITY_MATRIX_OFFSET = 4

# --- v34 resource-record (render-instance) layout ---------------------------
# The bulk of a map's real geometry lives in a dense "resource" region near the
# tail of the decompressed LoadUnit blob. Each record binds a mesh to its
# concrete bank CompiledMeshObject in a SINGLE-magic form (unlike the earlier
# triplet Mesh records the v33 scanner finds):
#
#     [meshUid u64][MESH_MAGIC_V34 u32][flags…][cmoUid u64 @ magic+21]
#
# i.e. the mesh uid sits 8 bytes BEFORE the mesh magic and the concrete bank
# CMO uid sits 21 bytes AFTER it. Filtering the +21 value against the shared
# mesh-bank FAT uid set reproduces the depgraph oracle's real-CMO set for a
# LoadUnit EXACTLY (university big LU: 876/876, 0 false-positive, 0 miss; all
# 53 LUs: 967/967). See research/y11s2_map_layout.md.
RESOURCE_MESH_UID_OFFSET = -8   # mesh uid, relative to the mesh magic
RESOURCE_CMO_OFFSET = 21        # bank CMO uid, relative to the mesh magic
# 0x863E1FD3 marks a per-record local bounding box ([minXYZ][maxXYZ] floats),
# NOT a world transform — the CMO geometry is local (origin-centred) and its
# WORLD placement is accumulated through the LoadUnit's internal-handle
# (0xFBF8xxxx) scene graph, not stored in the record. See the layout doc.
RESOURCE_BBOX_MAGIC = 0x863E1FD3


def read_top_level_file_type(asset, meta):
    """Recover a v34 entry's FileType from its decompressed blocks.

    The FileType is not in the v34 FAT; it sits at the head of the asset block
    as a [fileType u32][staticUid u64][fileType u32] triplet, where staticUid
    is the meta block's u64 at +4. Returns the FileType magic, or None.
    """
    if not asset or not meta or len(meta) < 12:
        return None
    static_uid = struct.unpack_from('<Q', meta, 4)[0]
    limit = min(len(asset) - 12, 256)
    for k in range(0, limit):
        if struct.unpack_from('<Q', asset, k + 4)[0] == static_uid:
            m = struct.unpack_from('<I', asset, k)[0]
            if m not in (0, 1) and m == struct.unpack_from('<I', asset, k + 12)[0]:
                return m
    return None


def _find_triplets(data, magic):
    """Yield offsets p1 where [magic][uid8][magic] appears (magic repeats at +12)."""
    mag = struct.pack('<I', magic)
    out = []
    pos = 0
    n = len(data)
    while True:
        idx = data.find(mag, pos)
        if idx == -1:
            break
        if idx + 16 <= n and data[idx + 12:idx + 16] == mag:
            out.append(idx)
        pos = idx + 1
    return out


def parse_v34_entities(data, entity_magic=ENTITY_MAGIC_V34):
    """Parse v34 Entity placement records from a LoadUnit blob.

    Returns list of {uid, matrix_raw (16 floats), payload_start}. payload_start
    points at the second magic (p2), consistent with the v33 parser's contract.
    """
    entities = []
    n = len(data)
    for p1 in _find_triplets(data, entity_magic):
        uid = struct.unpack_from('<Q', data, p1 + 4)[0]
        p2 = p1 + 12
        ms = p2 + ENTITY_MATRIX_OFFSET
        if ms + 64 > n:
            continue
        m = struct.unpack_from('<16f', data, ms)
        # affine sanity: bottom-right ~1.0
        if not (0.5 < m[15] < 1.5):
            continue
        entities.append({'uid': uid, 'matrix_raw': m, 'payload_start': p2})
    return entities


def parse_v34_mesh_records(data, cmo_uid_set=None, mesh_magic=MESH_MAGIC_V34):
    """Parse v34 Mesh records: mesh_uid -> cmo_uid.

    The external CMO uid sits at body +21 (from the second magic). When
    cmo_uid_set is provided, the +21 value is validated against it and a short
    fallback scan of the body recovers records whose layout drifts; otherwise
    the +21 value is taken as-is.

    Returns (mesh_to_cmo, mesh_uids) where mesh_uids is the set of all Mesh
    record uids (used to link entities even when the CMO isn't resolvable yet).
    """
    mesh_to_cmo = {}
    mesh_uids = set()
    n = len(data)
    for p1 in _find_triplets(data, mesh_magic):
        mesh_uid = struct.unpack_from('<Q', data, p1 + 4)[0]
        mesh_uids.add(mesh_uid)
        p2 = p1 + 12
        off = p2 + MESH_CMO_BODY_OFFSET
        if off + 8 > n:
            continue
        cmo_uid = struct.unpack_from('<Q', data, off)[0]
        if cmo_uid_set is not None and cmo_uid not in cmo_uid_set:
            # fallback: first u64 in the body that is a known CMO
            found = None
            body_end = min(p2 + 600, n - 7)
            for o in range(p2 + 4, body_end):
                v = struct.unpack_from('<Q', data, o)[0]
                if v in cmo_uid_set:
                    found = v
                    break
            cmo_uid = found
        if cmo_uid:
            mesh_to_cmo[mesh_uid] = cmo_uid
    return mesh_to_cmo, mesh_uids


def _find_resource_bbox(data, magic_off, cmo_off):
    """Best-effort per-record local bounding box.

    Returns (min_xyz, max_xyz) floats from the first RESOURCE_BBOX_MAGIC block
    that follows the CMO uid within the record, or None. This is a *local*
    bbox (the CMO's own extent); the record does NOT carry a world matrix.
    """
    mag = struct.pack('<I', RESOURCE_BBOX_MAGIC)
    lo = magic_off + cmo_off + 8
    hi = min(len(data) - 28, magic_off + 512)
    idx = data.find(mag, lo, hi)
    if idx == -1:
        return None
    try:
        f = struct.unpack_from('<6f', data, idx + 4)
    except struct.error:
        return None
    return (f[0], f[1], f[2]), (f[3], f[4], f[5])


def parse_v34_resource_records(data, cmo_uid_set, want_bbox=False,
                               mesh_magic=MESH_MAGIC_V34):
    """Parse the v34 LoadUnit resource (render-instance) records.

    Scans every MESH_MAGIC_V34 occurrence in single-magic form and, when the
    u64 at magic+21 is a known bank CMO, records the binding
    meshUid(magic-8) -> cmoUid(magic+21). This recovers the concrete geometry
    the LoadUnit uses; per-LoadUnit the resulting CMO set matches the depgraph
    oracle exactly (see research/y11s2_map_layout.md).

    Returns (mesh_to_cmo, records) where:
        mesh_to_cmo : {mesh_uid: cmo_uid}
        records     : list of {mesh_uid, cmo_uid, offset[, bbox]} in blob order.
                      bbox (local) is included only when want_bbox=True.

    cmo_uid_set is required — it is the discriminator that separates real bank
    CMOs (kept) from the internal self-handles that share the +21 slot.
    """
    mesh_to_cmo = {}
    records = []
    if not cmo_uid_set:
        return mesh_to_cmo, records
    mag = struct.pack('<I', mesh_magic)
    n = len(data)
    pos = 0
    while True:
        m = data.find(mag, pos)
        if m == -1:
            break
        pos = m + 1
        if m + RESOURCE_MESH_UID_OFFSET < 0 or m + RESOURCE_CMO_OFFSET + 8 > n:
            continue
        cmo_uid = struct.unpack_from('<Q', data, m + RESOURCE_CMO_OFFSET)[0]
        if cmo_uid not in cmo_uid_set:
            continue
        mesh_uid = struct.unpack_from('<Q', data, m + RESOURCE_MESH_UID_OFFSET)[0]
        rec = {'mesh_uid': mesh_uid, 'cmo_uid': cmo_uid, 'offset': m}
        if want_bbox:
            rec['bbox'] = _find_resource_bbox(data, m, RESOURCE_CMO_OFFSET)
        records.append(rec)
        # First binding wins (a mesh uid is bound to one CMO).
        mesh_to_cmo.setdefault(mesh_uid, cmo_uid)
    return mesh_to_cmo, records


def parse_v34_dmg_records(data, dmg_magic=DMG_MAGIC_V34):
    """Parse v34 DestructionMeshGroup records: dmg_uid -> mesh_uid.

    A destructible entity (soft wall, breakable prop, vending/slot machine)
    references a DMG uid in its payload rather than a Mesh uid directly; the
    DMG record wraps the concrete Mesh uid. Record is a
    [magic][uid][magic] triplet with the Mesh uid at body +12 (p2+12) — the
    same layout as the v33 DestructionMeshGroup. Verified on university: 400
    records, every uid referenced by an entity.

    Returns {dmg_uid: mesh_uid}.
    """
    dmg_to_mesh = {}
    n = len(data)
    for p1 in _find_triplets(data, dmg_magic):
        dmg_uid = struct.unpack_from('<Q', data, p1 + 4)[0]
        mesh_off = p1 + 12 + 12  # p2 (+12) then body +12
        if mesh_off + 8 <= n:
            dmg_to_mesh[dmg_uid] = struct.unpack_from('<Q', data, mesh_off)[0]
    return dmg_to_mesh


def _first_inline_child_offset(data, ps, end, entity_magic=ENTITY_MAGIC_V34):
    """Offset of the first inline child record in an entity payload, or `end`.

    A group/parent entity embeds child instances as
    `[child_uid u64][ENTITY_MAGIC_V34][matrix 64]...`. The parent's OWN mesh
    (if any) sits before the first child, so callers cap the parent's mesh
    scan here to stop it stealing a child's mesh (the "floating object" bug).
    """
    emag = struct.pack('<I', entity_magic)
    i = data.find(emag, ps + 4 + 64, end)
    if i == -1:
        return end
    # The child uid precedes the magic; back up to it so the parent scan stops
    # before the child record entirely.
    return max(ps, i - 8)


def link_v34_entities_to_meshes(data, entities, link_target_set, dmg_to_mesh=None,
                                entity_magic=ENTITY_MAGIC_V34):
    """Link each entity to a Mesh uid by scanning its payload for the first u64
    in link_target_set. Bounds each scan to the next entity AND to the entity's
    first inline child, so a group parent never adopts a child's mesh.

    link_target_set should be the RESOLVABLE targets — Mesh uids that resolve
    to a CMO plus DMG uids — so an entity whose payload also holds unresolvable
    handles still links to real geometry. A matched DMG uid is resolved through
    dmg_to_mesh to the concrete Mesh uid, so callers always get a Mesh uid back.

    Returns {entity_uid: mesh_uid}.
    """
    dmg_to_mesh = dmg_to_mesh or {}
    entity_to_mesh = {}
    starts = sorted(e['payload_start'] for e in entities)
    next_of = {s: (starts[i + 1] if i + 1 < len(starts) else None)
               for i, s in enumerate(starts)}
    n = len(data)
    for e in entities:
        ps = e['payload_start']
        scan_start = ps + 4 + 64  # after magic + matrix
        hard = ps + 8192
        nxt = next_of.get(ps)
        end = min(hard, nxt if nxt is not None else hard, n - 7)
        end = _first_inline_child_offset(data, ps, end, entity_magic)
        for off in range(scan_start, end):
            v = struct.unpack_from('<Q', data, off)[0]
            if v in link_target_set:
                entity_to_mesh[e['uid']] = dmg_to_mesh.get(v, v)
                break
    return entity_to_mesh


def find_v34_child_transforms(data, entities, link_target_set, dmg_to_mesh=None,
                              entity_magic=ENTITY_MAGIC_V34):
    """Recover inline child-transform records from v34 entity payloads.

    A group/parent entity embeds instance children directly in its payload as
    `[child_uid u64][ENTITY_MAGIC_V34][matrix 64][tail]`, each with its OWN
    world matrix and (usually) its own Mesh/DMG ref in the tail. Without this,
    the parent grabs one child's mesh and stamps it at the group origin — the
    displaced "floating object" artifact. Placing children at their own
    matrices fixes that and recovers the rest of the group's geometry.

    Returns a list of dicts:
        {uid, matrix_raw, payload_start, parent_uid, is_child_transform, mesh_uid}
    mesh_uid is the concrete Mesh uid (DMG refs resolved) or None.
    """
    dmg_to_mesh = dmg_to_mesh or {}
    emag = struct.pack('<I', entity_magic)
    n = len(data)
    starts = sorted(e['payload_start'] for e in entities)
    next_of = {s: (starts[i + 1] if i + 1 < len(starts) else None)
               for i, s in enumerate(starts)}
    top_uids = {e['uid'] for e in entities}
    children = []
    seen = set()
    for e in entities:
        ps = e['payload_start']
        nxt = next_of.get(ps)
        end = min(nxt if nxt is not None else ps + 16384, ps + 16384, n)
        scan = ps + 4 + 64
        while scan < end:
            i = data.find(emag, scan, end)
            if i == -1:
                break
            uid_off = i - 8
            ms = i + 4  # matrix starts right after the magic (v34 drops unk1)
            if uid_off < ps or ms + 64 > end:
                scan = i + 1
                continue
            child_uid = struct.unpack_from('<Q', data, uid_off)[0]
            # Real 40-bit-ish uid, not an internal 0xFBF8 handle or a top-level.
            if (child_uid == 0 or (child_uid >> 44) != 0
                    or child_uid in top_uids or child_uid in seen):
                scan = i + 1
                continue
            matrix_raw = struct.unpack_from('<16f', data, ms)
            if not (0.5 < matrix_raw[15] < 1.5):
                scan = i + 1
                continue
            seen.add(child_uid)
            # Child's own Mesh/DMG ref in its tail.
            mesh_uid = None
            t = ms + 64
            tend = min(t + 512, end - 7)
            while t < tend:
                v = struct.unpack_from('<Q', data, t)[0]
                if v in link_target_set:
                    mesh_uid = dmg_to_mesh.get(v, v)
                    break
                t += 1
            children.append({
                'uid': child_uid,
                'matrix_raw': matrix_raw,
                'payload_start': i,
                'parent_uid': e['uid'],
                'is_child_transform': True,
                'mesh_uid': mesh_uid,
            })
            scan = ms + 64
    return children


def gather_v34_loadunits(filepath, entries, progress=None):
    """Decompress and return the LoadUnit asset blobs of a v34 map forge.

    The LoadUnit type magic is re-hashed by roughly every game patch, so we do
    NOT filter on a hard-coded constant. Instead we recover each entry's
    top-level FileType (the [fileType][staticUid][fileType] triplet) and treat
    the FileType that holds the most total asset bytes as LoadUnit — the
    LoadUnits carry the map geometry, so they dominate by size. This makes
    LoadUnit identification survive future re-hashes with no code change.

    Returns list of (entry, asset_bytes).
    """
    # Adapted to the r6s-nr-tools v34 reader: each entry carries pre-parsed
    # ForgeAsset block descriptors ('_v34_asset' / '_v34_meta') from
    # forge.v34.read_v34_entries, so we decode them directly with one handle.
    typed = []
    ft_size = {}
    with open(filepath, 'rb') as f:
        for e in entries:
            ab = e.get('_v34_asset')
            if ab is None:
                continue
            try:
                asset = ab.read_data(f)
            except Exception as ex:
                if progress:
                    progress(f"entry 0x{e.get('uid', 0):016X} decode failed: {ex}")
                continue
            meta = None
            mb = e.get('_v34_meta')
            if mb is not None:
                try:
                    meta = mb.read_data(f)
                except Exception:
                    meta = None
            ft = read_top_level_file_type(asset, meta)
            if ft:
                typed.append((e, asset, ft))
                ft_size[ft] = ft_size.get(ft, 0) + len(asset)
    if not ft_size:
        return []
    loadunit_magic = max(ft_size, key=ft_size.get)
    if progress and loadunit_magic != LOADUNIT_MAGIC_V34:
        progress(f"LoadUnit magic re-hashed by a game update: "
                 f"0x{loadunit_magic:08X} (was 0x{LOADUNIT_MAGIC_V34:08X})")
    return [(e, a) for e, a, ft in typed if ft == loadunit_magic]


def _has_triplet(data, magic, minimum=1):
    """True if `magic` appears as a [magic][uid8][magic] triplet at least
    `minimum` times — a cheap check for whether a hard-coded magic is stale."""
    mag = struct.pack('<I', magic)
    n = len(data)
    pos = 0
    hits = 0
    while True:
        i = data.find(mag, pos)
        if i == -1:
            return False
        if i + 16 <= n and data[i + 12:i + 16] == mag:
            hits += 1
            if hits >= minimum:
                return True
        pos = i + 1


def harvest_v34_magics(lu_blobs, cmo_uid_set=None):
    """Structurally re-derive the in-LoadUnit record magics from live bytes.

    Called when the hard-coded magics are stale (a game patch re-hashed them).
    The record LAYOUT never changes, so each record type is identifiable by its
    structure regardless of the magic value:
      - Entity : the triplet magic with the most distinct uids.
      - Mesh   : the triplet magic whose single-magic +21 field is a bank CMO
                 (the resource-record rule) most often.
      - DMG    : the triplet magic whose body +12 is a known Mesh uid most often.

    Returns {'entity':.., 'mesh':.., 'dmg':..} (values may be None if a type
    couldn't be identified, e.g. a map with no destructibles).
    """
    out = {'entity': None, 'mesh': None, 'dmg': None}
    if not lu_blobs:
        return out
    big = max(lu_blobs, key=len)
    n = len(big)

    # Histogram [magic][uid8][magic] triplets by distinct uid count.
    mag_uids = {}
    for k in range(n - 16):
        if (big[k] == big[k + 12] and big[k + 1] == big[k + 13]
                and big[k + 2] == big[k + 14] and big[k + 3] == big[k + 15]):
            mg = struct.unpack_from('<I', big, k)[0]
            if mg == 0 or (mg & 0xFF) == 0 or (mg >> 24) == 0:
                continue  # skip byte-shift artifacts / trivial values
            s = mag_uids.get(mg)
            if s is None:
                s = mag_uids[mg] = set()
            s.add(struct.unpack_from('<Q', big, k + 4)[0])
    if not mag_uids:
        return out
    candidates = set(mag_uids)
    out['entity'] = max(mag_uids, key=lambda m: len(mag_uids[m]))

    # Mesh: candidate whose single-form +21 lands on a bank CMO most often.
    if cmo_uid_set:
        mesh_votes = {}
        for o in range(n - 29):
            mg = struct.unpack_from('<I', big, o)[0]
            if mg in candidates and struct.unpack_from('<Q', big, o + 21)[0] in cmo_uid_set:
                mesh_votes[mg] = mesh_votes.get(mg, 0) + 1
        if mesh_votes:
            out['mesh'] = max(mesh_votes, key=mesh_votes.get)

    # DMG: triplet whose body +12 is a resource mesh uid most often.
    if out['mesh'] is not None and cmo_uid_set:
        mm = struct.pack('<I', out['mesh'])
        mesh_uids = set()
        pos = 0
        while True:
            i = big.find(mm, pos)
            if i == -1:
                break
            pos = i + 1
            if i - 8 >= 0 and i + 29 <= n and struct.unpack_from('<Q', big, i + 21)[0] in cmo_uid_set:
                mesh_uids.add(struct.unpack_from('<Q', big, i - 8)[0])
        if mesh_uids:
            dmg_votes = {}
            for k in range(n - 16):
                if (big[k] == big[k + 12] and big[k + 1] == big[k + 13]
                        and big[k + 2] == big[k + 14] and big[k + 3] == big[k + 15]):
                    mg = struct.unpack_from('<I', big, k)[0]
                    if mg in candidates and mg not in (out['entity'], out['mesh']):
                        p2 = k + 12
                        if p2 + 20 <= n and struct.unpack_from('<Q', big, p2 + 12)[0] in mesh_uids:
                            dmg_votes[mg] = dmg_votes.get(mg, 0) + 1
            if dmg_votes:
                out['dmg'] = max(dmg_votes, key=dmg_votes.get)
    return out


# --- v34 entity component model -------------------------------------------
# A v34 map entity is an AnvilNext serialized object; its "components" are
# inline sub-objects addressed by ENTITY-LOCAL internal handles (0xFBF8000X,
# always numbered 0..N per entity — NOT a global scene-graph namespace). Each
# component is typed by a re-hashed magic. The only component that carries a
# resolvable geometry reference is the VISUAL component
# (VISUAL_COMPONENT_MAGIC_V34), whose record is [magic][01 00][mesh_uid u64].
# "Logic" entities (vending/slot machines, triggers, sound emitters) carry only
# non-visual components and therefore have no geometry of their own — see
# research/y11s2_map_layout.md ("Entity component model / logic entities").
VISUAL_MESH_BODY_OFFSET = 6  # mesh uid, relative to the visual component magic


def _magic_like(mg):
    """Cheap filter: a real type/component magic has all bytes 'busy' and is not
    an internal 0xFBF8xxxx handle."""
    return mg != 0 and (mg >> 24) != 0 and (mg & 0xFF) != 0 and (mg >> 16) != 0xFBF8


def _visual_magic_hits(data, magic, resolvable_mesh_uids, cap=None):
    """Count how often `magic` appears with a resolvable mesh uid at +6."""
    mag = struct.pack('<I', magic)
    n = len(data)
    hits = 0
    pos = 0
    while True:
        i = data.find(mag, pos)
        if i == -1:
            break
        pos = i + 1
        off = i + VISUAL_MESH_BODY_OFFSET
        if off + 8 <= n and struct.unpack_from('<Q', data, off)[0] in resolvable_mesh_uids:
            hits += 1
            if cap is not None and hits >= cap:
                return hits
    return hits


def harvest_v34_visual_magic(data, resolvable_mesh_uids):
    """Structurally re-derive the VISUAL component magic from live bytes.

    The visual component record is [magic][01 00][mesh_uid u64], so the magic is
    the u32 that most often sits 6 bytes before a resolvable Mesh uid. Called
    when the hard-coded VISUAL_COMPONENT_MAGIC_V34 no longer matches (a patch
    re-hashed it), the same self-healing approach used for the record magics.
    Returns the magic, or None.
    """
    if not resolvable_mesh_uids:
        return None
    votes = {}
    n = len(data)
    for o in range(n - 14):
        mu = struct.unpack_from('<Q', data, o + VISUAL_MESH_BODY_OFFSET)[0]
        if mu in resolvable_mesh_uids:
            mg = struct.unpack_from('<I', data, o)[0]
            if _magic_like(mg):
                votes[mg] = votes.get(mg, 0) + 1
    return max(votes, key=votes.get) if votes else None


def scan_v34_entity_components(data, entities, visual_magic):
    """Classify each entity by the components present in its inline payload.

    Currently detects the one component that has been reversed + verified: the
    VISUAL (render-mesh) component. Entities carrying it are geometry-bearing
    props; entities without it are "logic" nodes (vending/slot machines,
    triggers, sound emitters). Returns {entity_uid: set(component_names)} — the
    same shape as the v33 scan_entity_components, so the importer consumes it
    unchanged. Each scan is bounded to the next entity so hits don't bleed.
    """
    if not visual_magic:
        return {}
    vb = struct.pack('<I', visual_magic)
    n = len(data)
    starts = sorted(e['payload_start'] for e in entities)
    next_of = {s: (starts[i + 1] if i + 1 < len(starts) else None)
               for i, s in enumerate(starts)}
    out = {}
    for e in entities:
        ps = e['payload_start']
        nxt = next_of.get(ps)
        end = min(nxt if nxt is not None else n, n)
        if data.find(vb, ps + 4 + 64, end) != -1:
            out[e['uid']] = {'visual'}
    return out


def parse_v34_map(filepath, entries, cmo_uid_set=None, progress=None):
    """Parse a whole v34 map forge into importer-ready structures.

    Returns dict:
        unique_entities   : {uid: {uid, matrix_raw, payload_start}}
        mesh_to_cmo       : {mesh_uid: cmo_uid}
        entity_to_mesh    : {entity_uid: mesh_uid}
        mesh_uids         : set(all Mesh record uids)
        entity_components : {entity_uid: set(component_names)}
        num_loadunits     : int
    """
    unique_entities = {}
    mesh_to_cmo = {}
    entity_to_mesh = {}
    all_mesh_uids = set()
    resource_records = []

    lus = gather_v34_loadunits(filepath, entries, progress)
    if progress:
        progress(f"v34 map: {len(lus)} LoadUnits")

    # Resolve the in-LoadUnit record magics.
    #
    # Fast path, in order of trust:
    #   1. the MagicRegistry detected from this install's own bytes (shared with
    #      Prism, so it is normally already cached and free);
    #   2. the last-known constants below.
    # If both are stale — the Entity magic no longer appears as a triplet, which
    # is what a patch re-hash looks like — the set is re-derived STRUCTURALLY
    # from the live bytes so the import self-heals with no code change.
    entity_magic = ENTITY_MAGIC_V34
    mesh_magic = MESH_MAGIC_V34
    dmg_magic = DMG_MAGIC_V34

    # Mesh is anchored to a StaticUid singleton, so the registry knows it exactly.
    # Entity and DMG have no singleton and still rely on the structural harvest.
    try:
        from . import magic_registry
        _reg = magic_registry.for_game_dir(magic_registry.game_dir_of(filepath))
        if _reg is not None:
            _mesh = _reg.current_named('Mesh')
            if _mesh:
                mesh_magic = _mesh
    except Exception:
        pass  # detection is an optimisation; the harvest below still covers us

    lu_blobs = [a for _e, a in lus]
    if lu_blobs and not any(_has_triplet(a, entity_magic, 4) for a in lu_blobs[:4]):
        h = harvest_v34_magics(lu_blobs, cmo_uid_set)
        if h.get('entity'):
            entity_magic = h['entity']
            mesh_magic = h.get('mesh') or mesh_magic
            dmg_magic = h.get('dmg') or dmg_magic
            if progress:
                progress("v34 record magics re-hashed by a game update — "
                         f"auto-harvested: Entity=0x{entity_magic:08X} "
                         f"Mesh=0x{mesh_magic:08X} DMG=0x{dmg_magic:08X}")

    lu_parsed = []  # (data, entities, dmg_to_mesh) per LoadUnit
    for entry, data in lus:
        # (a) Triplet Mesh records — carry the mesh uids entities reference and
        #     a handful of direct CMO hits.
        m2c, muids = parse_v34_mesh_records(data, cmo_uid_set, mesh_magic)
        mesh_to_cmo.update(m2c)
        all_mesh_uids |= muids
        # (b) Resource (render-instance) records near the blob tail — the real
        #     geometry set. Its mesh->cmo bindings reproduce the depgraph oracle
        #     exactly, so they take precedence over the sparse triplet hits.
        res_m2c, recs = parse_v34_resource_records(data, cmo_uid_set,
                                                   mesh_magic=mesh_magic)
        mesh_to_cmo.update(res_m2c)
        all_mesh_uids |= set(res_m2c.keys())
        resource_records.extend(recs)
        # (c) DestructionMeshGroup records — the destructible chain
        #     entity -> dmg_uid -> mesh_uid -> cmo (soft walls, breakable props).
        dmg_to_mesh = parse_v34_dmg_records(data, dmg_magic)
        ents = parse_v34_entities(data, entity_magic)
        lu_parsed.append((data, ents, dmg_to_mesh))
        for ent in ents:
            unique_entities.setdefault(ent['uid'], ent)

    # Link entities to geometry. Prefer RESOLVABLE targets — Mesh uids that map
    # to a CMO plus DMG uids — so an entity that also carries unresolvable
    # internal handles still links to real geometry. DMG uids resolve through
    # dmg_to_mesh to the concrete Mesh uid. The parent scan is bounded to its
    # first inline child so group parents don't adopt a child's mesh.
    resolvable_meshes = set(mesh_to_cmo.keys())

    # Resolve the VISUAL component magic. Fast path: the last-known constant,
    # validated against live bytes; if a patch re-hashed it, harvest it
    # structurally on the biggest LoadUnit (self-heals with no code change).
    visual_magic = VISUAL_COMPONENT_MAGIC_V34
    if lu_blobs and resolvable_meshes:
        big = max(lu_blobs, key=len)
        if _visual_magic_hits(big, visual_magic, resolvable_meshes, cap=8) < 8:
            hv = harvest_v34_visual_magic(big, resolvable_meshes)
            if hv:
                visual_magic = hv
                if progress:
                    progress("v34 visual component magic re-hashed by a game "
                             f"update — auto-harvested: 0x{visual_magic:08X}")
    entity_components = {}

    for data, ents, dmg_to_mesh in lu_parsed:
        link_targets = resolvable_meshes | set(dmg_to_mesh.keys())
        links = link_v34_entities_to_meshes(data, ents, link_targets, dmg_to_mesh,
                                            entity_magic)
        for k, v in links.items():
            entity_to_mesh.setdefault(k, v)

        # Classify entities by their inline components (VISUAL present => a
        # geometry prop; absent => a "logic" node: vending/slot machine,
        # trigger, sound emitter). Lets the importer's type filter / collection
        # routing work on v34 instead of dumping every entity into OTHER.
        for uid, comps in scan_v34_entity_components(data, ents, visual_magic).items():
            entity_components.setdefault(uid, comps)

        # Recover inline child-transform instances (group members). Each child
        # places its own mesh at its own world matrix — fixes the "floating
        # object" artifact where a parent stamped a child's mesh at the group
        # origin, and adds the rest of the group's geometry.
        children = find_v34_child_transforms(data, ents, link_targets, dmg_to_mesh,
                                             entity_magic)
        for child in children:
            cuid = child['uid']
            if cuid in unique_entities:
                continue
            unique_entities[cuid] = {
                'uid': cuid,
                'matrix_raw': child['matrix_raw'],
                'payload_start': child['payload_start'],
                'parent_uid': child['parent_uid'],
                'is_child_transform': True,
            }
            if child['mesh_uid'] is not None:
                entity_to_mesh.setdefault(cuid, child['mesh_uid'])

    # Inline child-transform entities inherit their parent's component set —
    # their short inline record carries no component magics of its own (same
    # rule the v33 path uses).
    for ent in unique_entities.values():
        if ent.get('is_child_transform'):
            puid = ent.get('parent_uid')
            if puid in entity_components:
                entity_components.setdefault(ent['uid'], set(entity_components[puid]))

    return {
        'unique_entities': unique_entities,
        'mesh_to_cmo': mesh_to_cmo,
        'entity_to_mesh': entity_to_mesh,
        'mesh_uids': all_mesh_uids,
        'entity_components': entity_components,
        'resource_records': resource_records,
        'num_loadunits': len(lus),
    }
