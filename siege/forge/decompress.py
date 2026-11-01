"""
Scimitar payload decompression.

Each forge entry's data block is either zstd-compressed (pack_method == 3)
or streaming-style chunked (pack_method == 7). Some entries carry two
candidate blocks back-to-back; we pick the larger one because in practice
that's the actual asset payload, with the smaller being header/descriptor.
"""

import struct

from ..common.constants import SCIMITAR_FILE_MAGICS
from ..common.zstd_loader import zstd
from . import v34


def decompress_entry(filepath, entry):
    """Decompress a Scimitar file entry, returning the payload data block."""
    # Y11S2 (v34) entries carry a pre-parsed ForgeAsset descriptor; their on-disk
    # layout is the File3 container, not the legacy Scimitar block wrapper, so
    # they decode through the v34 path (which handles Oodle as well as zstd).
    if '_v34_asset' in entry:
        return v34.decompress_v34_entry(filepath, entry)

    # v31-v33 banks also store File3 ForgeAsset containers (while keeping a FAT).
    # Decide by the on-disk container magic rather than by the '_file3' flag: the
    # flag only gets set when the entry came from a Prism entry-summary cache or
    # from file_io's v31-v33 resolve, and a File3 container is never decodable by
    # the legacy Scimitar reader below (its 8-byte magic 0x1015FA9957FBAA37 isn't
    # in SCIMITAR_FILE_MAGICS, so decompress_entry silently returned None — the
    # other half of "a v32 map imports as an empty scene").
    try:
        with open(filepath, 'rb') as f:
            f.seek(entry['offset'])
            head = struct.unpack('<I', f.read(4))[0]
    except Exception:
        head = 0
    if head == v34.FILE3_MAGIC or (entry.get('_file3')
                                   and head in v34.FILE3_CONTAINER_MAGICS):
        return v34.decompress_file3_at(filepath, entry['offset'], entry['end'])

    with open(filepath, 'rb') as f:
        f.seek(entry['offset'])
        magic = struct.unpack('<Q', f.read(8))[0]
        if magic not in SCIMITAR_FILE_MAGICS:
            return None

        f.read(2)
        pack_method = struct.unpack('<h', f.read(2))[0]
        f.read(1 + 2)
        block1 = _read_data_block(f, pack_method)

        if f.tell() < entry['end'] - 16:
            try:
                nm = struct.unpack('<Q', f.read(8))[0]
                if nm in SCIMITAR_FILE_MAGICS:
                    f.read(2)
                    pm2 = struct.unpack('<h', f.read(2))[0]
                    f.read(1 + 2)
                    block2 = _read_data_block(f, pm2)
                    if block2 and len(block2) > len(block1 or b''):
                        return block2
                else:
                    nm2 = struct.unpack('<Q', f.read(8))[0]
                    if nm2 in SCIMITAR_FILE_MAGICS:
                        f.read(2)
                        pm2 = struct.unpack('<h', f.read(2))[0]
                        f.read(1 + 2)
                        block2 = _read_data_block(f, pm2)
                        if block2 and len(block2) > len(block1 or b''):
                            return block2
            except Exception:
                pass

        return block1


def _read_data_block(f, pack_method):
    if pack_method == 3:
        return _decompress_zstd(f)
    elif pack_method == 7:
        return _read_streaming(f)
    return None


def _decompress_zstd(f):
    nc = struct.unpack('<H', f.read(2))[0]
    f.read(2)
    si = []
    for _ in range(nc):
        ps = struct.unpack('<i', f.read(4))[0]
        ss = struct.unpack('<i', f.read(4))[0]
        si.append((ps, ss))
    co = []
    for i in range(nc):
        f.read(4)
        co.append(f.tell())
        f.seek(si[i][1], 1)
    dctx = zstd.ZstdDecompressor()
    result = bytearray()
    for i in range(nc):
        ps, ss = si[i]
        f.seek(co[i])
        raw = f.read(ss)
        if ps > ss:
            result.extend(dctx.decompress(raw, max_output_size=ps))
        else:
            result.extend(raw[:ps])
    return bytes(result)


def _read_streaming(f):
    nc = struct.unpack('<H', f.read(2))[0]
    f.read(2)
    si = []
    for _ in range(nc):
        ps = struct.unpack('<i', f.read(4))[0]
        ss = struct.unpack('<i', f.read(4))[0]
        si.append((ps, ss))
    for _ in range(nc):
        f.read(4)
    result = bytearray()
    for ps, _ in si:
        result.extend(f.read(ps))
    return bytes(result)
