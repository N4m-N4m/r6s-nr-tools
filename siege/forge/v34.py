"""
Y11S2 (forge v34) container reader.

v34 dropped the on-disk FAT and the legacy Scimitar block wrapper. Instead:

  * The whole entry table is reconstructed by scanning the file for 4096-byte
    aligned File3 magic markers (0x57FBAA37). Each marker begins one asset; the
    asset's size is the gap to the next marker (or EOF). (Mirrors
    RainbowForge `Forge.ReadV34`.)
  * Each asset is a `ForgeAsset` container: an optional MetaBlock (holds the
    StaticUid) followed by an AssetBlock (the real payload). Each block is
    either a ChunkedDataBlock (zstd OR Oodle compressed chunks) or a flat block.
    (Mirrors `ForgeAsset.Read` / `ChunkedDataBlock` / `FlatDataBlock`.)
  * There is no on-disk UID or FileType. The StaticUid comes from the MetaBlock
    payload header (u32 count, u64 uid); the FileType (a Magic enum value such
    as CompiledMeshObject / LoadUnit) is found by scanning the first 256 bytes
    of the decompressed AssetBlock for a known Magic.

This module is import-safe from plain CPython; the Oodle DLL is only loaded when
an Oodle-compressed chunk is actually decoded.
"""

import struct

from ..common import zstd_loader
from ..common import oodle_loader

# 4KB-aligned container magic that starts every v34 asset (and its inner data
# block). "File3" in the C# naming.
FILE3_MAGIC = 0x57FBAA37
V34_ALIGNMENT = 4096

# Legacy container magics that can also introduce an asset block on v34.
_ALT_CONTAINER_MAGICS = {0x57FBAA34, 0x57FBAA36}

# Every container magic that introduces a File3-style ForgeAsset. v31+ banks
# (including v32 "Siege X") store assets in this container while still keeping a
# real on-disk FAT, so file_io/decompress use this set to decide whether an
# entry decodes through parse_forge_asset or the legacy Scimitar reader.
FILE3_CONTAINER_MAGICS = {FILE3_MAGIC} | _ALT_CONTAINER_MAGICS

# Known top-level Magic enum values we care about for map import, used by the
# FileType scan. We don't need the full enum — only the magics the importer
# filters on plus a couple needed to keep the scan from latching onto noise.
# Importing them here keeps the lookup table in one place.
from ..common.constants import (
    LOADUNIT_MAGIC,
    MESH_MAGIC,
    COMPILED_MESH_MAGIC,
    CMO_FILE_TYPE,
    MATERIAL_MAGIC,
    ENTITY_MAGIC,
    COMPILED_TEXTURE_FILE_TYPES,
)

# The set of magics ScanFileTypeFromAssetBlock treats as a valid FileType. The
# C# version tests Enum.IsDefined(Magic); we approximate with the magics the
# importer actually keys on, which is enough to label LoadUnit / CMO / texture
# entries correctly. Unknown assets keep file_type 0 (harmless — they're skipped
# by the importer's file_type filters).
_KNOWN_FILE_TYPES = {
    LOADUNIT_MAGIC,
    MESH_MAGIC,
    COMPILED_MESH_MAGIC,
    CMO_FILE_TYPE,
    MATERIAL_MAGIC,
    ENTITY_MAGIC,
}
_KNOWN_FILE_TYPES.update(COMPILED_TEXTURE_FILE_TYPES)


# ---------------------------------------------------------------------------
# Chunked / flat data-block descriptors. We parse the descriptor at FAT-build
# time and decode lazily, exactly like the C# IAssetBlock split.
# ---------------------------------------------------------------------------


class _Chunk:
    __slots__ = ("data_len", "ser_len", "offset")

    def __init__(self, data_len, ser_len, offset):
        self.data_len = data_len      # decompressed length
        self.ser_len = ser_len        # on-disk length
        self.offset = offset          # absolute file offset of chunk bytes

    @property
    def is_compressed(self):
        return self.data_len > self.ser_len


class ChunkedBlock:
    """A ChunkedDataBlock descriptor: a list of chunks + whether Oodle is used."""

    __slots__ = ("chunks", "use_oodle")

    def __init__(self, chunks, use_oodle):
        self.chunks = chunks
        self.use_oodle = use_oodle

    def read_data(self, f, max_bytes=None):
        """Decode the block. With max_bytes, stop once that many bytes are out.

        Chunks are independently compressed, so a prefix read only has to touch
        the first chunk(s) — the difference between one 64 KB Oodle call and
        decompressing an entire 18 MB LoadUnit just to peek at its first 256
        bytes (which is what the FileType scan does for every entry).
        """
        out = bytearray()
        for ch in self.chunks:
            f.seek(ch.offset)
            if not ch.is_compressed:
                out += f.read(ch.ser_len)
            else:
                raw = f.read(ch.ser_len)
                if self.use_oodle:
                    out += oodle_loader.oodle_decompress(raw, ch.data_len)
                else:
                    dctx = zstd_loader.zstd.ZstdDecompressor()
                    out += dctx.decompress(raw, max_output_size=ch.data_len)
            if max_bytes is not None and len(out) >= max_bytes:
                break
        return bytes(out)


class FlatBlock:
    """A FlatDataBlock descriptor: raw bytes on disk, no compression."""

    __slots__ = ("offset", "length")

    def __init__(self, offset, length):
        self.offset = offset
        self.length = length

    def read_data(self, f, max_bytes=None):
        f.seek(self.offset)
        n = self.length if max_bytes is None else min(self.length, max_bytes)
        return f.read(n)


def _read_chunked_block(f, use_oodle):
    """Parse a ChunkedDataBlock at the current position. Mirrors ChunkedDataBlock.Read."""
    num_chunks, _u2 = struct.unpack('<HH', f.read(4))
    chunks = []
    for _ in range(num_chunks):
        unpacked, packed = struct.unpack('<II', f.read(8))
        chunks.append(_Chunk(unpacked, packed, 0))
    # Second pass: each chunk has a u32 hash, then its serialized bytes follow.
    for ch in chunks:
        f.read(4)  # hash
        ch.offset = f.tell()
        f.seek(ch.ser_len, 1)
    return ChunkedBlock(chunks, use_oodle)


def _read_flat_block(f, entry_end):
    """Parse a FlatDataBlock. Mirrors FlatDataBlock.Read (consumes rest of entry)."""
    num_chunks, _unk1 = struct.unpack('<HH', f.read(4))
    for _ in range(num_chunks):
        f.read(8)  # payloadSize, serializedSize
    for _ in range(num_chunks):
        f.read(4)  # checksum
    data_start = f.tell()
    return FlatBlock(data_start, entry_end - data_start)


def _read_asset_block(f, entry_end):
    """Parse one IAssetBlock (chunked/flat) at the current position.

    Mirrors ForgeAsset.GetAssetBlock: u16 container-type, u16 deserializer-type,
    u8, u16, then the type-specific block.
    """
    _x = struct.unpack('<H', f.read(2))[0]
    deser = struct.unpack('<H', f.read(2))[0]
    f.read(1)              # y
    f.read(2)              # z
    if deser == 3:
        return _read_chunked_block(f, use_oodle=False)
    elif deser in (13, 15):
        # Y9S2+/Y11S2: chunked layout but Oodle-compressed.
        return _read_chunked_block(f, use_oodle=True)
    elif deser == 7:
        return _read_flat_block(f, entry_end)
    else:
        raise NotImplementedError(
            f"Unknown v34 assetDeserializerType {deser}")


def parse_forge_asset(f, offset, end):
    """Parse a ForgeAsset container at `offset`. Returns (meta_block, asset_block).

    Mirrors ForgeAsset.Read: u32 magic, blockA, [u64 assetBlockMagic, blockB].
    When there's only one block it's the asset block (no meta).
    """
    f.seek(offset)
    # Two u32s precede block A: the File3 container magic (consumed by the v34
    # entry walk in C# ReadV34) and the inner ForgeAsset magic (consumed by
    # ForgeAsset.Read). On disk this is the 8-byte Scimitar block magic, e.g.
    # 0x1015FA9957FBAA37.
    f.read(4)  # File3 container magic
    f.read(4)  # ForgeAsset magic
    block_a = _read_asset_block(f, end)
    if f.tell() >= end:
        return None, block_a            # single block -> it's the asset
    f.read(8)  # assetBlockMagic
    block_b = _read_asset_block(f, end)
    return block_a, block_b             # meta, asset


# ---------------------------------------------------------------------------
# Entry enumeration
# ---------------------------------------------------------------------------


def _find_first_aligned_magic(f, file_len, magic):
    """Return the first 4KB-aligned offset whose first u32 == magic, or -1."""
    buf_magic = struct.pack('<I', magic)
    cursor = 0
    # The pre-entry region (header + dead FAT) is usually a few MB; step by the
    # alignment so we only test 4KB boundaries (matches the C# scan).
    while cursor + 4 <= file_len:
        f.seek(cursor)
        if f.read(4) == buf_magic:
            return cursor
        cursor += V34_ALIGNMENT
    return -1


def _scan_file_type(asset_block, f):
    """Decompress the asset block and scan its first 256 bytes for a known Magic."""
    if asset_block is None:
        return 0
    try:
        data = asset_block.read_data(f, max_bytes=256)
    except Exception:
        return 0
    n = min(256, len(data))
    for k in range(0, n - 3):
        m = struct.unpack_from('<I', data, k)[0]
        if m in _KNOWN_FILE_TYPES:
            return m
    return 0


def _read_static_uid(meta_block, f):
    """Read StaticUid from the meta block payload: u32 count, u64 uid."""
    if meta_block is None:
        return 0
    try:
        data = meta_block.read_data(f, max_bytes=64)
    except Exception:
        return 0
    if len(data) < 12:
        return 0
    return struct.unpack_from('<Q', data, 4)[0]


def read_v34_entries(f, file_len):
    """Enumerate v34 forge entries. Returns a list of entry dicts.

    Each entry carries the parsed ForgeAsset blocks under '_v34_asset' so
    `decompress_entry` can decode the payload without re-parsing.
    """
    first = _find_first_aligned_magic(f, file_len, FILE3_MAGIC)
    if first < 0:
        # Soundmedia forges (raw RIFFs, no File3) — not needed for map import.
        return []

    offsets = []
    buf_magic = struct.pack('<I', FILE3_MAGIC)
    cursor = first
    while cursor + 4 <= file_len:
        f.seek(cursor)
        if f.read(4) == buf_magic:
            offsets.append(cursor)
        cursor += V34_ALIGNMENT

    entries = []
    used_uids = set()
    for i, off in enumerate(offsets):
        end = offsets[i + 1] if i + 1 < len(offsets) else file_len
        size = end - off
        uid = off
        file_type = 0
        meta_block = asset_block = None
        try:
            f.seek(off)
            container_magic = struct.unpack('<I', f.read(4))[0]
            if container_magic == FILE3_MAGIC or container_magic in _ALT_CONTAINER_MAGICS:
                meta_block, asset_block = parse_forge_asset(f, off, end)
                su = _read_static_uid(meta_block, f)
                if su not in (0, None):
                    uid = su
                file_type = _scan_file_type(asset_block, f)
        except Exception:
            pass  # keep offset-as-uid + file_type 0

        # Guarantee uid uniqueness so the importer's uid->entry maps stay 1:1.
        if uid in used_uids:
            uid = off
        used_uids.add(uid)

        entries.append({
            'offset': off,
            'uid': uid,
            'size': size,
            'end': end,
            'file_type': file_type,
            '_v34_asset': asset_block,
            '_v34_meta': meta_block,
        })

    return entries


def decompress_v34_entry(filepath, entry):
    """Decode a v34 entry's asset-block payload. Returns bytes or None."""
    asset_block = entry.get('_v34_asset')
    if asset_block is None:
        return None
    with open(filepath, 'rb') as f:
        try:
            return asset_block.read_data(f)
        except Exception as e:
            print(f"[Siege][v34] decode failed for 0x{entry['uid']:X}: {e}")
            return None


def decompress_file3_at(filepath, offset, end):
    """Decode a File3 asset container located at a known FAT offset.

    Y9S2+ (forge v31-v33) keeps an on-disk FAT but already stores each asset as
    a File3 ForgeAsset container — the same layout v34 uses, just enumerated via
    the FAT instead of a magic scan. The legacy Scimitar reader in decompress.py
    can't decode these, so when an entry is flagged '_file3' (its offset came
    from a Prism entry-summary cache) we parse the container here and return its
    asset-block payload. Returns bytes, or None on failure.
    """
    with open(filepath, 'rb') as f:
        try:
            _meta, asset = parse_forge_asset(f, offset, end)
            if asset is None:
                return None
            return asset.read_data(f)
        except Exception as e:
            print(f"[Siege][v34] File3 decode failed at 0x{offset:X}: {e}")
            return None
