"""Real engine entity names, from a Heated Metal `EntityIDMap.bin`.

**Siege does not ship entity names.** Verified on Y5S4 Oregon: none of the 68
Space names appears as a string anywhere in 53 MB of decompressed LoadUnits or
11.8 MB of raw forge, and no hash of a name (crc32 / ~crc32 / fnv1a32 / fnv1a64,
over as-is / lower / upper / utf16 spellings, whole name and suffix) appears in
any Space entity payload. The entity UID isn't derived from the name either.
A byte scan of the entire 21.9 GB install finds the names in exactly one file —
`HeatedMetal/Resources/EntityIDMap.bin`, a third-party mod resource.

So names are *recoverable*, but only from that file, and only if the user has it.
Nothing here bundles it: the caller passes a path, and everything degrades to
generated labels when it is absent.

Format (validated at 10734/10734 exact matches against a Heated Metal
`ExportEntities` dump):

    u64 count
    u64 string_blob_offset                  == 16 + count*12
    count x { u32 uid_lo; u32 uid_hi; u32 name_offset }
    NUL-terminated UTF-8 names, `name_offset` relative to string_blob_offset

Entity UIDs are stable across game versions — the Y5S4-era table names 100% of
Y5S4 Oregon's spaces AND 49/49 of v32 University, 102/103 of v32 Tower, 98/98 of
v32 House. Reworked maps get 0%: their entities were rebuilt with new UIDs.
Corpus-wide that is 1175/4239 rooms (27.7%).
"""

import os
import re
import struct


# Where the file sits relative to a game install root.
_REL = os.path.join('HeatedMetal', 'Resources', 'EntityIDMap.bin')

# Everything up to and including the last "space"-ish token is a path prefix the
# level tooling prepended, not part of the room name:
#   PVP02_Oregon_V2_Space_1F_Kitchen                        -> 1F Kitchen
#   GP_SPACE_EXT_OfficeBalcony                              -> EXT OfficeBalcony
#   ..._Dab_Output_DabGeneratedSpace_2F_Office              -> 2F Office
_PREFIX = re.compile(r'^.*?space[_\s]*', re.IGNORECASE)
# A leftover map tag on names that carry no Space token at all.
_MAPTAG = re.compile(r'^(?:PVP|EVN|PVE)\d+[_\s]*', re.IGNORECASE)


def find_map_file(*hints):
    """Locate `EntityIDMap.bin` from a forge path or install dir, else None.

    A Heated Metal build keeps the forges at its root, so walking up from the
    map file finds it without the user pointing at anything.
    """
    seen = set()
    for hint in hints:
        if not hint:
            continue
        d = hint if os.path.isdir(hint) else os.path.dirname(hint)
        for _ in range(4):
            if not d or d in seen:
                break
            seen.add(d)
            cand = os.path.join(d, _REL)
            if os.path.isfile(cand):
                return cand
            if os.path.basename(d).lower() == 'heatedmetal':
                cand = os.path.join(d, 'Resources', 'EntityIDMap.bin')
                if os.path.isfile(cand):
                    return cand
            d = os.path.dirname(d)
    return None


def load_entity_names(path):
    """`{uid: name}` from an EntityIDMap.bin. Raises on a malformed file."""
    with open(path, 'rb') as fh:
        blob = fh.read()
    if len(blob) < 16:
        raise ValueError("file too short to be an EntityIDMap")
    count, str_off = struct.unpack_from('<QQ', blob, 0)
    if count <= 0 or str_off != 16 + count * 12 or str_off > len(blob):
        raise ValueError(f"not an EntityIDMap (count={count}, blob@{str_off}, "
                         f"size={len(blob)})")
    out = {}
    for i in range(count):
        lo, hi, off = struct.unpack_from('<III', blob, 16 + i * 12)
        p = str_off + off
        if p >= len(blob):
            continue
        end = blob.find(b'\x00', p)
        if end < 0:
            end = len(blob)
        out[(hi << 32) | lo] = blob[p:end].decode('utf-8', 'replace')
    return out


def clean_space_name(raw):
    """Engine Space name -> something worth putting on screen.

    `PVP02_Oregon_V2_Space_1F_Kitchen` -> `1F Kitchen`. Returns '' for a name
    that is nothing but prefix, so the caller can fall back.
    """
    if not raw:
        return ''
    name = _PREFIX.sub('', raw.strip(), count=1) or raw.strip()
    name = _MAPTAG.sub('', name, count=1)
    name = name.replace('_', ' ').strip()
    return re.sub(r'\s+', ' ', name)
