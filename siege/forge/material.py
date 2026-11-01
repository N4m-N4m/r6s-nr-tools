"""
Material / TextureMapSpec / TextureMap / CompiledTexture parsing.

The resolution chain inside a LoadUnit is:
    Material        -- N TextureSelector records, each pointing at...
    TextureMapSpec  -- carries the TextureMap UID + textureType
    TextureMap      -- carries 1..N compiled-texture file UIDs (2 mip sets)
    <compiled texture entry in *_textures*.forge files>

`build_texture_forge_index` walks the *_textures*.forge files and returns
a mapping from compiled-texture UID to the best-available resolution
variant.
"""

import io
import os
import struct

from ..common.constants import (
    COMPILED_TEXTURE_FILE_TYPE_SET,
    COMPILED_TEXTURE_MAP_DATA_MAGIC,
    COMPILED_TEXTURE_MAP_MAGIC,
    DDS_FOURCC_DX10,
    MATERIAL_MAGIC,
    TEXFORMAT_DDS_INFO,
    TEXTURE_MAP_MAGIC,
    TEXTURE_MAP_SPEC_MAGIC,
    TEXTURE_SELECTOR_MAGIC,
    TEXTURE_TYPE_NAMES,
)
from .file_io import read_forge_entries


def _pair_scan(data, magic_u32):
    """
    Generic [magic][uid][magic_echo] pair scanner shared by every record
    type that uses Siege's standard envelope (Material, TextureMapSpec,
    TextureMap, etc.). Returns list of (body_offset, uid) where body_offset
    is the position of the second magic (start of payload).
    """
    pat = struct.pack('<I', magic_u32)
    positions = []
    pos = 0
    while True:
        idx = data.find(pat, pos)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 1
    out = []
    i = 0
    while i < len(positions) - 1:
        p1 = positions[i]
        p2 = positions[i + 1]
        if p2 == p1 + 12:
            uid = struct.unpack('<Q', data[p1+4:p1+12])[0]
            out.append((p2, uid))
            i += 2
        else:
            i += 1
    return out


def parse_texture_map_records(data):
    """
    Parse every TextureMap record in a decompressed LoadUnit and return:
        tm_uid -> list of compiled-texture file UIDs (deduped, in order found)

    The TextureMap layout we observe in current Siege builds (different from
    the literal C# struct, which has stale field sizes) is:
        +0x00  magic_echo(4) + var1(4)
        +0x08  var2(4) + var3(4)
        +0x10  var4(4) + var5(4)
        +0x18  var6(4)
        +0x1C  texUidMipSet1[0]   (8)
        +0x24  texUidMipSet1[1]   (8)
        +0x2C  pad(8) -- often zero
        +0x34  texUidMipSet1[2]   (8) etc.
        ...
    Rather than hard-coding offsets we walk the body in 8-byte ulongs and
    accept any UID whose high byte != 0xF8 (the internal-uid flag) AND that
    is non-zero. We bound the body by the next TextureMap record's start
    so adjacent records don't bleed in.
    """
    pairs = _pair_scan(data, TEXTURE_MAP_MAGIC)
    if not pairs:
        return {}

    out = {}
    for i, (p2, uid) in enumerate(pairs):
        next_p2 = pairs[i + 1][0] if i + 1 < len(pairs) else len(data)
        # The TM body has a small fixed header (28 bytes including the
        # echoed magic at +0x00) followed by two mip-sets of 5 ulongs each.
        # In current Siege builds the first ulong sits at +0x1C, but the
        # mip sets are not contiguous — they're interleaved with 4-byte
        # filler. Walking 4-aligned ulongs catches all of them without
        # producing duplicates because each real UID lives at a unique
        # 4-byte offset and zero ulongs are filtered out.
        scan_start = p2 + 0x1C
        scan_end = min(next_p2, p2 + 200, len(data))
        if scan_end - scan_start < 8:
            continue

        # Real compiled-texture file UIDs in current Siege builds fit in
        # ~40 bits (max observed ~0x52C29A0844), so the upper 24 bits are
        # always zero. The 4-byte stride catches every real UID but also
        # pulls in misaligned junk reads where the upper bits are non-zero.
        # We use that to discard the trash.
        seen = set()
        ordered = []
        off = scan_start
        while off + 8 <= scan_end:
            v = struct.unpack('<Q', data[off:off+8])[0]
            if v != 0 and (v >> 40) == 0:
                if v not in seen:
                    seen.add(v)
                    ordered.append(v)
            off += 4
        if ordered:
            out[uid] = ordered
    return out


def parse_texture_map_spec_records(data):
    """
    Parse every TextureMapSpec record and return:
        tms_uid -> (texture_map_uid, texture_type)

    Layout (from RainbowForge/RenderPipeline/TextureMapSpec.cs):
        +0x00  magic_echo(4)
        +0x04  TextureMapUid(8)
        +0x0C  textureType(4)
        +0x10  extraData(12)
    """
    pairs = _pair_scan(data, TEXTURE_MAP_SPEC_MAGIC)
    out = {}
    for p2, uid in pairs:
        if p2 + 16 > len(data):
            continue
        tm_uid = struct.unpack('<Q', data[p2+4:p2+12])[0]
        tex_type = struct.unpack('<I', data[p2+12:p2+16])[0]
        out[uid] = (tm_uid, tex_type)
    return out


def parse_material_records(data):
    """
    Parse every Material record and follow the full chain
        Material -> TextureSelector -> TextureMapSpec -> TextureMap -> file UIDs
    Returns two dicts:
        material_uid -> {tex_type_name: [list of compiled-texture file UIDs]}
        material_uid -> int (number of non-null texture selectors found)

    tex_type_name is one of TEXTURE_TYPE_NAMES values ('diffuse', 'normal',
    'specular', 'misc', etc). The list is the ordered set of compiled-texture
    UIDs found in the resolved TextureMap's mip set.

    The downstream texture index is responsible for picking the
    highest-resolution variant — we just hand it every UID we found.

    The selector count mirrors the "image count" heuristic used by r6s-nr-tools:
    materials with 2-5 selectors typically use diffuse alpha for transparency,
    while materials with 6+ selectors use alpha for layer blending.
    """
    tms_table = parse_texture_map_spec_records(data)
    tm_table = parse_texture_map_records(data)
    if not tms_table or not tm_table:
        return {}

    sel_pat = struct.pack('<I', TEXTURE_SELECTOR_MAGIC)
    mat_pairs = _pair_scan(data, MATERIAL_MAGIC)
    out = {}
    sel_counts = {}  # material_uid -> int

    # Bound each Material body to the next Material's start so we don't pull
    # selectors from a sibling record.
    for i, (mat_p2, mat_uid) in enumerate(mat_pairs):
        body_end = (mat_pairs[i + 1][0] - 12) if i + 1 < len(mat_pairs) else len(data)
        body_end = min(body_end, mat_p2 + 4096, len(data))

        per_type = {}  # tex_type_name -> list of file uids
        num_selectors = 0  # count non-null selectors
        pos = mat_p2
        while True:
            idx = data.find(sel_pat, pos, body_end)
            if idx == -1:
                break
            sel_start = idx - 8
            if sel_start < mat_p2 or sel_start + 28 > body_end:
                pos = idx + 4
                continue
            # Selector layout: EntryUid(8) | magic(4) | TMSpecUid(8) | var1(4) | mip(4)
            tmspec_uid = struct.unpack('<Q', data[sel_start+12:sel_start+20])[0]
            pos = idx + 4

            if tmspec_uid == 0:
                continue
            num_selectors += 1
            spec_entry = tms_table.get(tmspec_uid)
            if spec_entry is None:
                continue
            tm_uid, tex_type = spec_entry
            file_uids = tm_table.get(tm_uid)
            if not file_uids:
                continue
            type_name = TEXTURE_TYPE_NAMES.get(tex_type)
            if type_name is None:
                continue
            # Multiple selectors of the same type can appear (e.g. detail
            # diffuse + main diffuse); keep them in document order with dedup.
            slot = per_type.setdefault(type_name, [])
            for u in file_uids:
                if u not in slot:
                    slot.append(u)

        if per_type:
            out[mat_uid] = per_type
        sel_counts[mat_uid] = num_selectors
    return out, sel_counts


def parse_compiled_texture(data):
    """
    Parse a compiled texture entry payload (already decompressed).
    Returns dict with the fields needed to build a DDS file, or None on failure.

    Compiled texture layout (from RainbowForge/Image/Texture.cs):
        FileMetaData header:
            filename_len(4) + filename(N) + container_type(4) + file_type(4) + uid(8)
        secondMagic(4) + var2(4) + var3(4)
        CompiledTextureMap_magic(4)  -- must be COMPILED_TEXTURE_MAP_MAGIC
        texFormat(4) + var(4) + var(4) + texType(4) + var*6(24)
        containerId(4)               -- must be COMPILED_TEXTURE_MAP_DATA_MAGIC
        data1(1) + numBlocks(2) + data2(1)
        x30(4)
        <DDS surface bytes>
        Footer (last 0x29 bytes):
            w(4) + h(4) + e8(4) + eC(4) + chan(4) + e14(4) + mips(4) + e1C(4) + e20(4) + e24(4) + e28(1)

    Returns:
        {
            'tex_format': int,
            'tex_type':   int,
            'width':      int,    # decoded (after chan downscale)
            'height':     int,
            'mips':       int,
            'chan':       int,
            'dds_offset': int,    # start of surface bytes inside `data`
            'dds_size':   int,    # length of surface bytes
        }
    """
    try:
        r = io.BytesIO(data)
        filename_len = struct.unpack('<I', r.read(4))[0]
        if filename_len > 1024:
            return None
        r.read(filename_len)
        r.read(4)  # container_type
        r.read(4)  # file_type
        r.read(8)  # uid

        r.read(4 + 4 + 4)  # secondMagic + var2 + var3

        inner_magic = struct.unpack('<I', r.read(4))[0]
        if inner_magic != COMPILED_TEXTURE_MAP_MAGIC:
            return None

        tex_format = struct.unpack('<I', r.read(4))[0]
        r.read(4)  # x04
        r.read(4)  # x08
        tex_type = struct.unpack('<I', r.read(4))[0]
        r.read(4 * 6)  # x10..x24

        container_id = struct.unpack('<I', r.read(4))[0]
        if container_id != COMPILED_TEXTURE_MAP_DATA_MAGIC:
            return None

        r.read(1)  # data1
        r.read(2)  # numBlocks
        r.read(1)  # data2
        r.read(4)  # x30

        dds_offset = r.tell()
        # Footer is the last 0x29 bytes of the entry payload.
        footer_off = len(data) - 0x29
        if footer_off <= dds_offset:
            return None
        dds_size = footer_off - dds_offset

        r.seek(footer_off)
        w = struct.unpack('<I', r.read(4))[0]
        h = struct.unpack('<I', r.read(4))[0]
        r.read(4)  # e8
        r.read(4)  # eC
        chan = struct.unpack('<I', r.read(4))[0]
        r.read(4)  # e14
        mips = struct.unpack('<I', r.read(4))[0]

        # The C# parser divides w/h by 2^chan to get the actual decoded size.
        # `chan` is effectively a "log2 downscale" stored on the texture.
        if chan > 16:
            return None
        scale = 1 << chan
        width = max(1, w // scale)
        height = max(1, h // scale)

        if width <= 0 or height <= 0 or dds_size <= 0:
            return None

        return {
            'tex_format': tex_format,
            'tex_type': tex_type,
            'width': width,
            'height': height,
            'mips': max(1, mips),
            'chan': chan,
            'dds_offset': dds_offset,
            'dds_size': dds_size,
        }
    except Exception:
        return None


def build_dds_file(tex_info, surface_bytes):
    """
    Build a complete .dds byte string for the given parsed compiled texture.

    Layout:
        DDS magic (4)
        DDS_HEADER (124)
        [DX10 header (20)] -- only when fourcc == 'DX10'
        surface bytes

    The DDS_HEADER is the standard DirectXTex 124-byte struct. We use the
    legacy FOURCC path for BC1/BC3/BC5 (DXT1/DXT5/ATI2), which Blender 4.5+
    decodes natively. BC4/BC6H/BC7 use a DX10 extension header carrying the
    DXGI format directly.
    """
    fmt_info = TEXFORMAT_DDS_INFO.get(tex_info['tex_format'])
    if fmt_info is None:
        return None
    fourcc, dxgi_format, block_size, is_compressed = fmt_info

    width = tex_info['width']
    height = tex_info['height']
    mips = tex_info['mips']

    # DDS header field constants
    DDSD_CAPS = 0x1
    DDSD_HEIGHT = 0x2
    DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000
    DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    DDPF_FOURCC = 0x4
    DDSCAPS_TEXTURE = 0x1000
    DDSCAPS_MIPMAP = 0x400000
    DDSCAPS_COMPLEX = 0x8

    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    caps = DDSCAPS_TEXTURE
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_MIPMAP | DDSCAPS_COMPLEX

    # pitch_or_linear_size for compressed: max(1, ((width+3)/4)) * blocksize
    if is_compressed:
        pitch_or_linear_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_size
    else:
        pitch_or_linear_size = ((width * 32 + 7) // 8)

    # DDS_PIXELFORMAT (32 bytes): size + flags + fourcc(4) + bitcount + 4xmask
    pf = struct.pack(
        '<II4sIIIII',
        32,                # size
        DDPF_FOURCC,       # flags
        fourcc,            # fourCC
        0,                 # RGB bit count
        0, 0, 0, 0,        # masks
    )

    # DDS_HEADER (124 bytes total: size(4) + 28 fields(112) + pixelformat(32) + caps[4](16) + reserved2(4))
    # Standard DirectXTex layout: see DXGI/DDS docs.
    header = struct.pack(
        '<7I44s32s5I',
        124,                  # size
        flags,
        height,
        width,
        pitch_or_linear_size,
        0,                    # depth
        mips,                 # mipMapCount
        b'\x00' * 44,         # reserved1[11]
        pf,                   # pixel format (already 32 bytes)
        caps,
        0, 0, 0,              # caps2..caps4
        0,                    # reserved2
    )

    out = bytearray()
    out.extend(b'DDS ')
    out.extend(header)

    # DX10 extension header for BC4/BC6/BC7
    if fourcc == DDS_FOURCC_DX10:
        out.extend(struct.pack('<5I', dxgi_format, 3, 0, 1, 0))

    out.extend(surface_bytes)
    return bytes(out)


def build_texture_forge_index(forge_dir, progress_callback=None, max_rank=3):
    """
    Walk every *_textures*.forge file in forge_dir and return:
        texture_uid -> (forge_path, entry, resolution_rank)

    For each texture UID we keep the highest-resolution variant available
    up to max_rank (Ultra > High > Med > Low > Future). The resolution rank
    comes from the forge filename suffix _textures{0..4}, mapping
    0=Low ... 3=Ultra ... 4=Future.

    max_rank controls texture quality: 0=Low, 1=Med, 2=High, 3=Ultra.
    Forge files above max_rank are skipped entirely.
    """
    forge_files = []
    for f in os.listdir(forge_dir):
        if not f.lower().endswith('.forge'):
            continue
        if '_textures' not in f.lower():
            continue
        forge_files.append(os.path.join(forge_dir, f))

    if not forge_files:
        return {}

    # Map forge filename suffix to a numeric rank (higher = more detail).
    def _rank_for_path(path):
        name = os.path.basename(path).lower()
        # Find _texturesN substring and grab the digit after it.
        idx = name.find('_textures')
        if idx == -1:
            return 0
        d = name[idx + len('_textures'):idx + len('_textures') + 1]
        try:
            return int(d)
        except ValueError:
            return 0

    index = {}  # uid -> (forge_path, entry, rank)
    for i, fp in enumerate(forge_files):
        rank = _rank_for_path(fp)
        if rank > max_rank:
            continue
        if progress_callback:
            progress_callback(
                f"Indexing {os.path.basename(fp)} ({i+1}/{len(forge_files)})")
        try:
            entries, _ = read_forge_entries(fp)
        except Exception as e:
            print(f"[Siege][TEX] Failed to read {os.path.basename(fp)}: {e}")
            continue
        for e in entries:
            if e['file_type'] not in COMPILED_TEXTURE_FILE_TYPE_SET:
                continue
            uid = e['uid']
            cur = index.get(uid)
            if cur is None or rank > cur[2]:
                index[uid] = (fp, e, rank)
    return index
