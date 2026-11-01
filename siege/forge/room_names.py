"""In-game COMPASS room names, read from the map forge itself.

Siege ships no *entity* names — that was established separately (see
`entity_names.py`). But it does ship the **callout names the compass displays**,
and those are the ones worth having: they are the words players actually use.

They live in a string table inside a non-LoadUnit entry of the map forge:

    [u32 handle][u32 0][u32 0xDB9C79F2][u32 len][utf8 bytes]

and shortly past the string sits the u64 id of the **SpaceComponent** the name
belongs to. That id is not the Space entity's own uid — it is the component's —
but it also appears inside the owning entity's payload, which is what ties a
name to a Space.

**The id's offset is not fixed**: v30 puts it at +0x15, v32 at +0x0E, because a
preceding field changed size. Hardcoding +0x15 silently resolved only ~half of a
v32 map. So the whole window after the string is scanned, and a candidate counts
only if it appears inside a Space entity's payload — the check that made the
offset unnecessary in the first place. Matching is done against a value index
built from Space payloads alone (a few hundred KB), so widening the search from
1 offset to 64 costs nothing.

Coverage is deliberately partial: only rooms the compass announces get a name.
It also works where the third-party entity-name table does not — v32 Australia
and Viperstrike have zero coverage there.

The names are the compass strings, NOT the level-designer entity names, and the
two genuinely differ: Oregon's `B_Storage` callout is the designer's
`Space_B_ElectricRoom`. The callout is the one to show.
"""

import bisect
import struct


# Field type magic introducing a length-prefixed UTF-8 string.
STRING_FIELD_MAGIC = 0xDB9C79F2

# How far past the string to look for the SpaceComponent id. Wide enough to
# cover every layout seen (v30 +0x15, v32 +0x0E) without needing to know which.
_ID_WINDOW = 64

# A name is a room callout, not some other string in the same table, when it
# starts with a floor token. Keeps map/debug strings out.
_FLOOR_PREFIXES = ('b_', 'b1_', 'b2_', 'ext', 'roof', 'r_')

_MAX_STRING = 512


def parse_name_records(data):
    """`[(name, [candidate_id, ...])]` for every string record in one entry.

    Candidates are every u64 in the window after the string, in order, because
    the id's offset moved between forge versions. The caller decides which one
    is real by testing it against Space payloads.
    """
    pat = struct.pack('<I', STRING_FIELD_MAGIC)
    out, pos = [], 0
    while True:
        i = data.find(pat, pos)
        if i < 0:
            return out
        pos = i + 4
        try:
            n = struct.unpack_from('<I', data, i + 4)[0]
        except struct.error:
            continue
        if not (0 < n < _MAX_STRING) or i + 8 + n > len(data):
            continue
        try:
            text = data[i + 8:i + 8 + n].decode('utf-8')
        except UnicodeDecodeError:
            continue
        end = i + 8 + n
        span = min(_ID_WINDOW, len(data) - end - 8)
        cands = [struct.unpack_from('<Q', data, end + o)[0]
                 for o in range(max(span, 0))]
        out.append((text, cands))


def looks_like_room(name):
    """Is this string a room name rather than some other string in the table?

    Two shapes occur. Most maps store a bare callout — `1F_Kitchen`, `B_Boiler`,
    `1F - Tellers`, `Ext Roof`. Kanal stores the full entity name instead
    (`PVP09_Kanal_Space_1F_Kitchen`), which is why a floor-prefix test alone
    found nothing there; anything carrying a `Space` token counts too.
    """
    if not name or len(name) > 64:
        return False
    low = name.lower()
    if low[:1].isdigit() and 'f' in low[:3]:
        return True
    if low.startswith(_FLOOR_PREFIXES) or low.startswith('room'):
        return True
    return 'space' in low


def link_room_names(tables, load_units, space_uids):
    """`{space_uid: callout_name}`.

    `tables` are decompressed entries that may hold a name table; `load_units`
    are `(data, entities)` pairs as the importer already has them; `space_uids`
    is the set of entity uids known to be Spaces.
    """
    if not space_uids:
        return {}

    # Every u64 that occurs inside a Space entity's payload -> that Space.
    # Only Space payloads are indexed (a few hundred KB per map), which is what
    # makes scanning a whole window of candidate offsets affordable.
    index = {}
    for data, entities in load_units:
        for i, ent in enumerate(entities):
            if ent['uid'] not in space_uids:
                continue
            ps = ent['payload_start']
            pe = (entities[i + 1]['payload_start']
                  if i + 1 < len(entities) else len(data))
            body = data[ps:pe]
            uid = ent['uid']
            for o in range(max(len(body) - 7, 0)):
                index.setdefault(struct.unpack_from('<Q', body, o)[0], uid)

    records = [(n, c) for t in tables for n, c in parse_name_records(t)
               if looks_like_room(n)]
    if not records:
        return {}

    # The id sits at ONE fixed offset per file — it just isn't the same offset
    # across forge versions. Rather than guess, try them all and keep the offset
    # that names the most DISTINCT rooms. Taking the first candidate that hits
    # anything instead is what silently mis-assigns names: unrelated bytes
    # elsewhere in the window collide onto a handful of spaces, and a run that
    # should resolve 32 rooms collapses to 4.
    best_offset, best = None, {}
    for off in range(_ID_WINDOW):
        mapping, used = {}, set()
        for name, cands in records:
            if off >= len(cands):
                continue
            cid = cands[off]
            if cid <= 0xFFFF:      # padding / small counts, never an id
                continue
            uid = index.get(cid)
            if uid is not None and uid not in used:
                mapping[uid] = name
                used.add(uid)
        if len(mapping) > len(best):
            best_offset, best = off, mapping
    return best


def prettify(name):
    """`1F_MeetingHall` -> `1F Meeting Hall`; `Ext Balcony` unchanged."""
    if not name:
        return ''
    parts = name.replace('_', ' ').split()
    words = []
    for p in parts:
        # split CamelCase but keep tokens like 1F, EXT, VIP, B2 intact
        if p.isupper() or (len(p) <= 3 and any(c.isdigit() for c in p)):
            words.append(p)
            continue
        buf = ''
        for ch in p:
            if ch.isupper() and buf and not buf[-1].isupper():
                words.append(buf)
                buf = ch
            else:
                buf += ch
        if buf:
            words.append(buf)
    return ' '.join(words)
