"""
Forge FAT (file allocation table) parsing.

Reads the entry table from a .forge archive and returns one record per
asset, including the per-entry file_type magic used downstream to filter
LoadUnits / textures / meshes / CMOs.
"""

import os
import struct

from . import v34
from ..common.constants import (
    CMO_FILE_TYPE,
    COMPILED_MESH_SHAPE_FILE_TYPE,
    COMPILED_SOUND_BANK_FILE_TYPE,
    COMPILED_SOUND_MEDIA_FILE_TYPE,
    COMPILED_TEXTURE_FILE_TYPES,
)

# Prism writes a per-forge entry-summary sidecar (`<forge>.prismcache`). It
# carries each entry's StaticUid + FileType — which our legacy v31-v33 FAT
# reader can't recover: those banks store File3 asset containers but keep a FAT,
# and cross-forge references (mesh -> CMO) use the StaticUid, not the FAT uid.
_FORGE_ENTRY_CACHE_MAGIC = 0x43454650  # 'PFEC'


def read_forge_entry_cache(forge_path):
    """Read a Prism `<forge>.prismcache` entry-summary, if present and current.

    Returns (entries, forge_version) or None. Each entry dict uses the StaticUid
    (falling back to the FAT uid) as 'uid', plus offset/end/size/file_type and
    '_file3': True so decompress_entry decodes via the File3 path. The cache is
    rejected when its recorded forge size doesn't match the file on disk.
    """
    cache_path = forge_path + ".prismcache"
    if not os.path.exists(cache_path):
        return None
    try:
        forge_size = os.path.getsize(forge_path)
        with open(cache_path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    # header: magic(4) cacheFormatVersion(4) forgeSize(8) forgeMtime(8)
    #         forgeVersion(4) entryCount(4); then entryCount * 32-byte records.
    if len(data) < 32 or struct.unpack_from('<I', data, 0)[0] != _FORGE_ENTRY_CACHE_MAGIC:
        return None
    cached_size, _mtime, forge_ver, count = struct.unpack_from('<qqIi', data, 8)
    if cached_size != forge_size:
        return None  # stale cache; let the caller fall back to a live read
    entries = []
    off = 32  # header: magic(4)+ver(4)+size(8)+mtime(8)+forgeVer(4)+count(4)
    for _ in range(count):
        if off + 32 > len(data):
            break
        uid, static_uid, eoff, size, ftype = struct.unpack_from('<QQqII', data, off)
        off += 32
        entries.append({
            'offset': eoff,
            'uid': static_uid if static_uid != 0 else uid,
            'size': size,
            'end': eoff + size,
            'file_type': ftype,
            '_file3': True,
        })
    return entries, forge_ver


def read_forge_version(filepath):
    """Read just the SCIMITAR version field. Cheap pre-flight check."""
    with open(filepath, 'rb') as f:
        if f.read(9) != b'scimitar\x00':
            raise ValueError(f"Not a SCIMITAR archive: {filepath}")
        return struct.unpack('<I', f.read(4))[0]


def infer_file_type_from_name(filepath):
    """Guess a forge's asset FileType from its filename. 0 when unknown.

    v31+ forges zero the on-disk metadata table (see EntryMetaData.Read in the
    C# core: `case >= 31` skips 320 zero bytes), so the per-entry FileType the
    v<=30 reader used simply isn't stored any more. Mirrors Prism's
    ForgeOpener.InferFileTypeFromForgeName, with one addition: `_texturesN`
    banks resolve to the matching per-resolution compiled-texture magic rather
    than the generic CompiledTextureMap, because that's what the texture index
    filters on.

    Content banks (`*_mesh.forge`, `*_textures2.forge`, ...) hold exactly one
    asset kind, so the filename is authoritative and free. Map forges have no
    hint and fall back to the asset-block magic scan.
    """
    name = os.path.basename(filepath).lower()

    idx = name.find('_textures')
    if idx != -1:
        digit = name[idx + len('_textures'):idx + len('_textures') + 1]
        if digit.isdigit() and int(digit) < len(COMPILED_TEXTURE_FILE_TYPES):
            return COMPILED_TEXTURE_FILE_TYPES[int(digit)]
        return 0
    # Check meshshape before mesh — a `_meshshape.` bank holds collision shapes,
    # not CompiledMeshObjects, and must not land in the CMO index.
    if '_meshshape.' in name:
        return COMPILED_MESH_SHAPE_FILE_TYPE
    if '_mesh.' in name:
        return CMO_FILE_TYPE
    if '_soundmedia' in name:
        return COMPILED_SOUND_MEDIA_FILE_TYPE
    if '_soundbank' in name:
        return COMPILED_SOUND_BANK_FILE_TYPE
    return 0


def _resolve_file3_entries(filepath, entries):
    """Recover uid + file_type for v31..v33 entries. Mutates `entries` in place.

    These versions keep a real FAT but store each asset in the same File3
    ForgeAsset container v34 uses, and they zero the metadata table. That leaves
    the raw FAT records with:
      * file_type == 0 for every entry, so the importer's `file_type ==
        LOADUNIT_MAGIC` / `== CMO_FILE_TYPE` filters match nothing (this is why
        a v32 map imported as an empty scene), and
      * a FAT uid that is NOT the uid cross-forge references use. Mesh records
        point at the MetaBlock's StaticUid; on Oregon v32 the FAT uid resolved
        0/2444 CMO references while the StaticUid resolved 2444/2444.

    So: take the uid from the MetaBlock and the file_type from the filename hint,
    falling back to scanning the asset block's first 256 bytes when the bank has
    no hint (map forges). Mirrors Prism's ForgeOpener.ApplyResolve.
    """
    hint = infer_file_type_from_name(filepath)
    used_uids = {e['uid'] for e in entries}
    with open(filepath, 'rb') as f:
        for e in entries:
            e['_file3'] = True
            try:
                f.seek(e['offset'])
                container_magic = struct.unpack('<I', f.read(4))[0]
            except Exception:
                continue
            # Descriptor/hash entries aren't assets — leave them alone.
            if container_magic not in v34.FILE3_CONTAINER_MAGICS:
                continue
            try:
                meta_block, asset_block = v34.parse_forge_asset(
                    f, e['offset'], e['end'])
            except Exception:
                continue

            try:
                static_uid = v34._read_static_uid(meta_block, f)
            except Exception:
                static_uid = 0
            # Keep the FAT uid on collision so uid->entry maps stay 1:1.
            if static_uid and static_uid not in used_uids:
                used_uids.discard(e['uid'])
                used_uids.add(static_uid)
                e['uid'] = static_uid

            if hint:
                e['file_type'] = hint
            else:
                try:
                    e['file_type'] = v34._scan_file_type(asset_block, f)
                except Exception:
                    pass


def read_forge_entries(filepath):
    """Parse SCIMITAR forge file, return (entries, version)."""
    with open(filepath, 'rb') as f:
        magic = f.read(9)
        if magic != b'scimitar\x00':
            raise ValueError(f"Not a SCIMITAR archive: {filepath}")

        version = struct.unpack('<I', f.read(4))[0]

        # Y11S2 (v34) abandons the on-disk FAT entirely: entries are recovered by
        # scanning for 4KB-aligned File3 markers, and each entry's UID/FileType is
        # derived by decompressing its ForgeAsset blocks. Dispatch to the dedicated
        # v34 reader, which needs only the file length (not the rest of the header).
        if version >= 34:
            file_len = os.path.getsize(filepath)
            return v34.read_v34_entries(f, file_len), version

        f.read(4 + 8 + 4 + 1)
        num_entries = struct.unpack('<I', f.read(4))[0]
        f.read(4 + 4 + 4)
        if version >= 27:
            f.read(4)
        f.read(4 + 4 + 4)
        num_tables = struct.unpack('<I', f.read(4))[0]
        first_table_pos = struct.unpack('<Q', f.read(8))[0]

        all_entries = []
        f.seek(first_table_pos)

        for _ in range(num_tables):
            max_file = struct.unpack('<i', f.read(4))[0]
            f.read(4)
            pos_fat = struct.unpack('<q', f.read(8))[0]
            next_pos_fat = struct.unpack('<q', f.read(8))[0]
            f.read(4 + 4)
            meta_offset = struct.unpack('<q', f.read(8))[0]
            f.read(8)

            f.seek(pos_fat)
            entries = []
            for _ in range(max_file):
                offset = struct.unpack('<q', f.read(8))[0]
                uid = struct.unpack('<Q', f.read(8))[0]
                size = struct.unpack('<I', f.read(4))[0]
                entries.append({
                    'offset': offset, 'uid': uid, 'size': size,
                    'end': offset + size, 'file_type': 0,
                })

            f.seek(meta_offset)
            for j in range(max_file):
                if version >= 30:
                    f.read(4 + 4 + 8 + 4 + 0xFF + 1 + 4 + 4 + 4 + 4 + 8)
                    file_type = struct.unpack('<I', f.read(4))[0]
                    f.read(4 + 8 + 4)
                else:
                    f.read(4 + 8 + 4)
                    file_type = struct.unpack('<I', f.read(4))[0]
                    f.read(8 + 4 + 4 + 4 + 4 + 0xFF + 1 + 4 + 4 + 4 + 4 + 4)
                entries[j]['file_type'] = file_type

            all_entries.extend(entries)
            if next_pos_fat not in (-1, 0xFFFFFFFFFFFFFFFF):
                f.seek(next_pos_fat)

    # v31..v33 ("Siege X" v32 included): the FAT is real but its metadata table
    # is zeroed, so uid/file_type have to be recovered from each File3 container.
    if 31 <= version <= 33:
        _resolve_file3_entries(filepath, all_entries)

    return all_entries, version
