"""Siege Space (room) volumes, and the floor table derived from them.

A Siege map is partitioned into **Spaces** — rooms — joined by SpacePortals.
That is the room graph the engine uses for culling, sound propagation, callouts
and the spectator/replay floor view, and it is the authoritative answer to
"which floor is this on". The designers author it; we only have to read it.

The link between a Space and its floor is not a stored field — it is the room's
**floor slab**, i.e. `min(z)` over the room volume. Those values are snapped by
the level designers and cluster into exact bands. On Y5S4 Oregon (where Heated
Metal gives us the real space names as ground truth) the clusters land on
B ~ -3.5, 1F ~ 0.35, 2F ~ 3.6, 3F ~ 9.8, and stairwells correctly land on the
floor they *start* from (`B_LaundryStairs` zmin -3.45 -> B, `1F_MainStairs`
zmin 0.32 -> 1F). That last property is exactly what per-object Z-banding gets
wrong, and it is why this module exists.

Record layout lives in `common/constants.py` next to SPACE_EDGE_MAGIC. Nothing
here imports bpy, so it stays testable headlessly against a forge (see
`research/space_floor_system.md` for the harness).
"""

import collections
import math
import struct

try:
    from ..common.constants import SPACE_EDGE_MAGIC, SPACE_PLANE_MAGIC
except ImportError:  # headless / direct import
    from common.constants import SPACE_EDGE_MAGIC, SPACE_PLANE_MAGIC

from .entity import parse_entity_components


# Both space records are exactly this long; the *last* component in an entity's
# list runs to the end of the record and so carries trailing unrelated bytes.
# Reading a fixed 33 bytes rather than the component's full extent is what keeps
# that trailing data out of the point cloud.
_RECORD_SIZE = 33

# Coordinate sanity bound. Map extents are ~1000 units at the very most; this
# only has to reject garbage from a mis-walked component.
_COORD_LIMIT = 1e4

# --- floor derivation tuning (all validated in research/space_floor_system.md) ---
# Spaces this tall or this wide in plan are exterior/roof volumes (open sky
# boxes), not storeys, and must not vote on floor heights.
EXTERIOR_Z_SPAN = 8.0
EXTERIOR_FOOTPRINT = 1200.0

# zmin histogram bucket.
FLOOR_BUCKET = 0.25
# Two distinct storeys are never closer together than this. Candidate slabs are
# picked by non-maximum suppression at this radius: strongest (most rooms) first,
# and a weaker candidate within the radius is absorbed rather than becoming its
# own floor. This is what rejects the landing/mezzanine bands that sit between
# real storeys (Clubhouse z=1.9, Bank z=1.8) while still keeping a genuine
# single-room top storey (Y5S4 Oregon's 3F BigTower, one room, 6.3 m clear).
# Ground truth: 48/48 correct floor labels on Y5S4 Oregon at 2.0, 2.5 and 3.0,
# so the exact value is not load-bearing.
FLOOR_NMS_RADIUS = 2.5
# A room's own slab may sit slightly below its storey's nominal slab (stepped
# floors, thresholds, sunken rooms), so a space belongs to the highest slab at
# or below `zmin + this`. Straight nearest-slab misclassifies stairwells; this
# is what puts B_BackStairs (slab -0.55, between B -3.5 and 1F 0.25) on B, as
# the designers named it.
FLOOR_BELOW_TOLERANCE = 0.7
# Share of rooms a band needs before it can anchor the ground floor. Stops one
# stray room renaming every storey on maps with a world Z offset.
GROUND_MIN_SHARE = 0.10
# Maps built around the origin put 1F within this of z=0. Maps built on elevated
# terrain (Stadium, Outback, Emerald Plains all sit at z=22..43) have no such
# band, and fall back to "lowest substantial storey is the ground floor".
GROUND_NEAR_ZERO = 2.5

# --- containment tuning (measured in research/space_floor_system.md) ---
# An object whose test point lands in no room falls back to the global slab
# band, which is the weakest path in this module (it is per-object Z-banding,
# the very thing Spaces exist to replace). Most of those objects are indoor
# props sitting just outside a boundary: over the v32 magus build, 28-50% of
# misses are within 2 m of a room — clubhouse 1642 objects (12.0% of ALL
# entities), oregon 2253 (15.3%), chalet 1116 (9.0%) — while the genuinely
# outdoor population sits far out (p90 = 29-51 m). So a distance-capped
# nearest-room search recovers the first group without touching the second.
#
# It also repairs what `trim_ceilings` necessarily breaks: a prop shaved out of
# its own room by the trim is, by construction, within a metre of it.
#
# 0 disables the fallback and restores the pure containment behaviour.
NEAR_ROOM_RADIUS = 2.0

# XY cell size for the containment grid. Rooms run ~5-10 m across, so this
# keeps a handful of cells per bucket. Pure index: results are identical
# without it, only slower.
_GRID_CELL = 8.0
# A cell list this wide (a map-spanning exterior sky-box) is not worth
# bucketing — it would be inserted into every cell in the grid. Those go in a
# single always-checked list instead.
_MAX_GRID_CELLS = 64

# Vertex quantisation for welding edge endpoints into connected pieces.
_WELD = 1e-3


def _read_points(data, off, stop):
    """Both endpoints of one edge record, or [] if this isn't a clean record."""
    if stop - off - 12 < _RECORD_SIZE:
        return []
    body = data[off + 12:off + 12 + _RECORD_SIZE]
    out = []
    for base in (0, 16):
        p = struct.unpack_from('<3f', body, base)
        if all(abs(v) < _COORD_LIMIT for v in p):
            out.append(p)
    return out


def _read_plane(data, off, stop):
    """`(nx, ny, nz, px, py, pz)` for one face-plane record, or None."""
    if stop - off - 12 < _RECORD_SIZE:
        return None
    body = data[off + 12:off + 12 + _RECORD_SIZE]
    n = struct.unpack_from('<3f', body, 0)
    p = struct.unpack_from('<3f', body, 16)
    if not all(abs(v) < _COORD_LIMIT for v in n + p):
        return None
    return n + p


def _aabb(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def trim_ceilings(spaces, fraction):
    """Shave the top `fraction` (0..1) off every interior room's cells.

    A Space runs floor-slab to roof-slab, so the ceiling **above** a room is
    inside that room's volume. Everything that follows from containment inherits
    the problem: the ceiling gets tagged with the room under it, so isolating a
    storey draws its own roof and you cannot look down into it, and the debug
    volume looks like a sealed box.

    Cutting a slice off the top drops the roof slab out of the room below, which
    is where a viewer expects it — it belongs to the storey it is the floor of.
    Cells stay convex (a horizontal cut of a convex solid is convex) and stay
    watertight, so nothing downstream has to care.

    `zmin` is untouched, so floor derivation is unaffected — only containment
    and the drawn volume change. Exterior/sky volumes are skipped: they have no
    ceiling to remove, and trimming them would push roof props out of every room.
    """
    if not fraction or fraction <= 0.0:
        return spaces
    fraction = min(float(fraction), 0.9)
    for sp in spaces.values():
        if sp.get('exterior'):
            continue
        solids, pieces = [], []
        for pts in sp.get('solids') or ():
            zs = [p[2] for p in pts]
            lo, hi = min(zs), max(zs)
            zcut = hi - (hi - lo) * fraction
            # Clamping the tops is the exact intersection for the vertical-sided
            # boxes and prisms the decomposition uses, and stays conservative
            # (never larger than the true cut) for a slanted one.
            cut = [(x, y, z if z < zcut else zcut) for x, y, z in pts]
            solids.append(cut)
            pieces.append(_aabb(cut))
        if solids:
            sp['solids'] = solids
            sp['pieces'] = pieces
            sp['ceiling_trim'] = fraction
    return spaces


def _cells(edges):
    """Split the stored edge stream into the room's individual convex cells.

    Returns `(cells, leftover)` — `cells` is a list of point lists, one per
    convex cell, `leftover` the trailing edges that never closed.

    A Space is a convex decomposition, and the engine stores it **one cell at a
    time**: each cell's edges are contiguous in the stream and describe a closed
    triangulated convex solid (a box's six quads are triangulated with all six
    face diagonals meeting at two opposite corners — 8 verts, 18 edges). For any
    closed triangulated polyhedron `V - E + F = 2` and `E = 3F/2`, so

        E == 3 * (V - 2)

    holds exactly at a cell boundary and nowhere inside one. Accumulating edges
    until that fires segments the stream with no dependence on the record's
    corner-index byte, and it is self-validating: a wrong split simply never
    closes and shows up as leftover.

    This is what welding-into-connected-components could not do — cells that
    share a face weld into one blob, and a single AABB over an L-shaped blob
    swallows the corner the room does not occupy.
    """
    cells = []
    verts, ekeys, pts, used = set(), set(), [], 0
    for i, (a, b) in enumerate(edges):
        ka, kb = _key(a), _key(b)
        if ka == kb:
            continue
        for k, p in ((ka, a), (kb, b)):
            if k not in verts:
                verts.add(k)
                pts.append(p)
        ekeys.add((ka, kb) if ka < kb else (kb, ka))
        v, e = len(verts), len(ekeys)
        if v >= 4 and e == 3 * (v - 2):
            cells.append(pts)
            verts, ekeys, pts = set(), set(), []
            used = i + 1
    return cells, list(edges[used:])


def _key(p):
    return (round(p[0] / _WELD), round(p[1] / _WELD), round(p[2] / _WELD))


def _corners(box):
    """The 8 corners of an AABB, for feeding a leftover piece to a hull."""
    x0, y0, z0, x1, y1, z1 = box
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def _pieces(edges):
    """Split a space's edges into connected components -> one AABB per piece.

    Fallback for the rooms `_cells` cannot fully segment. Welding edge endpoints
    and taking connected components recovers *groups* of cells rather than
    individual ones, so the AABBs are looser — but it always produces something.
    """
    parent = {}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    key = _key

    for a, b in edges:
        ka, kb = key(a), key(b)
        parent.setdefault(ka, ka)
        parent.setdefault(kb, kb)
        union(ka, kb)

    groups = collections.defaultdict(list)
    for a, b in edges:
        groups[find(key(a))].extend((a, b))
    return [_aabb(pts) for pts in groups.values() if pts]


def parse_spaces(data, entities):
    """Extract every Space volume from one decompressed LoadUnit.

    Returns `{entity_uid: space}` where a space is::

        {'uid', 'origin', 'points', 'edges', 'planes', 'aabb', 'pieces',
         'solids', 'exact', 'zmin', 'zmax', 'footprint', 'exterior'}

    `solids` is the room as the engine authored it: one point list per convex
    cell, ready to hull. Those cells tile the room without overlapping, unlike
    their AABBs (`pieces`), which bulge wherever a cell is not axis-aligned.
    Draw `solids`; test points against `pieces`.

    Coordinates are world-space as stored — no transform is applied, and none is
    needed: the records are already world-space (68/68 Space origins on Y5S4
    Oregon fall inside their own volume).
    """
    spaces = {}
    for i, ent in enumerate(entities):
        end = (entities[i + 1]['payload_start']
               if i + 1 < len(entities) else len(data))
        comps = parse_entity_components(data, ent, end)
        if not comps:
            continue

        edges = []
        planes = []
        for off, magic, stop in comps:
            if magic == SPACE_EDGE_MAGIC:
                pts = _read_points(data, off, stop)
                if len(pts) == 2:
                    edges.append((pts[0], pts[1]))
            elif magic == SPACE_PLANE_MAGIC:
                pl = _read_plane(data, off, stop)
                if pl is not None:
                    planes.append(pl)

        if not edges:
            continue

        points = [p for e in edges for p in e]
        box = _aabb(points)
        footprint = (box[3] - box[0]) * (box[4] - box[1])
        zspan = box[5] - box[2]
        mr = ent['matrix_raw']

        # Exact convex cells where the stream segments; welded-component AABBs
        # for whatever is left over, so a room always yields something.
        cells, leftover = _cells(edges)
        solids = list(cells)
        pieces = [_aabb(c) for c in cells]
        if leftover or not solids:
            for lb in _pieces(leftover or edges):
                pieces.append(lb)
                solids.append(_corners(lb))

        spaces[ent['uid']] = {
            'uid': ent['uid'],
            'origin': (mr[12], mr[13], mr[14]),
            'points': points,
            'edges': edges,
            'planes': planes,
            'aabb': box,
            'pieces': pieces,
            'solids': solids,
            # True when every edge landed in a closed cell — i.e. the room is
            # the designers' exact decomposition, not a welded approximation.
            'exact': not leftover,
            'zmin': box[2],
            'zmax': box[5],
            'footprint': footprint,
            # Open-sky volumes (the map's exterior and roof boxes) are tall and
            # huge in plan. They are real Spaces but they are not storeys, so
            # they must not vote in the floor histogram.
            'exterior': zspan >= EXTERIOR_Z_SPAN or footprint >= EXTERIOR_FOOTPRINT,
        }
    return spaces


def _floor_labels(heights, weights):
    """Siege floor names for ascending slab heights.

    The ground floor is the storey nearest z=0 — but only among storeys holding
    a real share of the map's rooms, so one stray room can't rename the whole
    building. Maps built on elevated terrain have no band near the origin at all
    (Stadium, Outback and Emerald Plains sit at z=22..43); there the lowest
    substantial storey is the ground floor and anything below it is a basement.
    That rule is what makes `stadium2021` and `stadium2021_v2` agree on 1F@29.0
    despite the latter carrying an extra one-room sub-level.
    """
    if not heights:
        return []
    total = sum(weights) or 1
    substantial = [i for i, w in enumerate(weights)
                   if w >= max(1, total * GROUND_MIN_SHARE)]
    if not substantial:
        substantial = list(range(len(heights)))
    near_zero = [i for i in substantial if abs(heights[i]) <= GROUND_NEAR_ZERO]
    ground = (min(near_zero, key=lambda i: abs(heights[i]))
              if near_zero else substantial[0])
    labels = []
    for i in range(len(heights)):
        if i == ground:
            labels.append('1F')
        elif i < ground:
            depth = ground - i
            labels.append('B' if depth == 1 else f'B{depth}')
        else:
            labels.append(f'{i - ground + 1}F')
    return labels


def derive_floors(spaces):
    """Cluster Space slab heights into a floor table.

    Returns `(floors, space_floor)`:

    * `floors` — ascending `[{'index', 'label', 'z', 'z_min', 'z_max'}]`, where
      `z` is the storey's slab height and `[z_min, z_max)` is the band used to
      place loose geometry.
    * `space_floor` — `{space_uid: floor_index}`, including exterior spaces
      (assigned to the band their own slab falls in, so a roof volume doesn't
      vanish).

    Empty list when a map has no usable Spaces — callers must fall back.
    """
    interior = [s for s in spaces.values() if not s['exterior']]
    if not interior:
        return [], {}

    hist = collections.Counter(
        round(s['zmin'] / FLOOR_BUCKET) * FLOOR_BUCKET for s in interior)

    # Non-maximum suppression: the best-supported candidate slab wins its
    # neighbourhood. This is what rejects landing/mezzanine bands wedged between
    # real storeys while still keeping a genuine sparse top storey.
    kept = []
    for z, n in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])):
        if all(abs(z - k) >= FLOOR_NMS_RADIUS for k, _n in kept):
            kept.append((z, n))
    kept.sort()

    # Re-centre each surviving slab on the rooms it actually owns, so a storey
    # whose rooms straddle two buckets lands between them instead of on whichever
    # single bucket happened to be tallest.
    owners = collections.defaultdict(list)
    for z, n in hist.items():
        i = min(range(len(kept)), key=lambda k: abs(z - kept[k][0]))
        owners[i].append((z, n))
    heights, weights = [], []
    for i, (z, _n) in enumerate(kept):
        grp = owners.get(i) or [(z, 1)]
        heights.append(sum(a * b for a, b in grp) / sum(b for _a, b in grp))
        weights.append(sum(b for _a, b in grp))

    labels = _floor_labels(heights, weights)
    floors = []
    for i, h in enumerate(heights):
        lo = float('-inf') if i == 0 else h - FLOOR_BELOW_TOLERANCE
        hi = (float('inf') if i == len(heights) - 1
              else heights[i + 1] - FLOOR_BELOW_TOLERANCE)
        floors.append({'index': i, 'label': labels[i], 'z': h,
                       'z_min': lo, 'z_max': hi, 'rooms': weights[i]})

    space_floor = {uid: _band_index(floors, s['zmin'])
                   for uid, s in spaces.items()}
    return floors, space_floor


def _band_index(floors, z):
    """Highest storey whose slab is at or below `z` (within tolerance).

    Deliberately NOT nearest-slab. Geometry rests *on* a floor, so sitting a
    little above a slab means that storey, while sitting well above it means the
    next one up. Nearest-slab misclassifies stairwells; this rule is what puts
    Oregon's B_BackStairs (slab -0.55, between B at -3.5 and 1F at 0.25) on B,
    exactly as the designers named it.
    """
    if not floors:
        return 0
    idx = floors[0]['index']
    for f in floors:
        if z >= f['z'] - FLOOR_BELOW_TOLERANCE:
            idx = f['index']
    return idx


class _Lookup:
    """Containment index over every room's convex cells.

    Iterating yields the flat
    `(exterior, volume, x0, y0, z0, x1, y1, z1, uid)` entries in the same
    (exterior, volume) order the plain list used to have, so anything that
    walked it still works. `containing` and `nearest` are the paths
    `floor_for_point` actually uses.

    The XY grid exists because `floor_for_point` no longer stops at the first
    hit — it has to see EVERY room containing the point in order to tie-break
    between them — and because the near-room fallback needs a distance query.
    Both would be a full linear scan per object otherwise.
    """

    __slots__ = ('entries', '_grid', '_big', '_cell')

    def __init__(self, entries, cell=_GRID_CELL):
        self.entries = entries
        self._cell = cell
        self._grid = collections.defaultdict(list)
        self._big = []
        for i, e in enumerate(entries):
            # Count before materialising: an exterior sky-box spans the whole
            # map, and at _GRID_CELL = 8 m a 10 km coordinate bound would build
            # a million-entry key list just to throw it away.
            if self._cell_count(e[2], e[3], e[5], e[6]) > _MAX_GRID_CELLS:
                self._big.append(i)
                continue
            for k in self._cells_of(e[2], e[3], e[5], e[6]):
                self._grid[k].append(i)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def _cell_range(self, x0, y0, x1, y1):
        c = self._cell
        return (int(math.floor(x0 / c)), int(math.floor(y0 / c)),
                int(math.floor(x1 / c)), int(math.floor(y1 / c)))

    def _cell_count(self, x0, y0, x1, y1):
        ix0, iy0, ix1, iy1 = self._cell_range(x0, y0, x1, y1)
        return (ix1 - ix0 + 1) * (iy1 - iy0 + 1)

    def _cells_of(self, x0, y0, x1, y1):
        ix0, iy0, ix1, iy1 = self._cell_range(x0, y0, x1, y1)
        return [(ix, iy)
                for ix in range(ix0, ix1 + 1)
                for iy in range(iy0, iy1 + 1)]

    def _bucket(self, px, py):
        c = self._cell
        return self._grid.get((int(math.floor(px / c)),
                               int(math.floor(py / c))), ())

    def containing(self, px, py, pz):
        """Every cell whose box contains the point, in (exterior, volume) order."""
        out = []
        for i in list(self._bucket(px, py)) + self._big:
            e = self.entries[i]
            if (e[2] <= px <= e[5] and e[3] <= py <= e[6]
                    and e[4] <= pz <= e[7]):
                out.append(e)
        return out

    def nearest(self, px, py, pz, radius):
        """Closest INTERIOR cell within `radius`, as `(entry, distance)`.

        Exterior sky-boxes are excluded: snapping a prop to the map's own
        bounding volume is not information, and they are what the point missed
        by being outdoors in the first place.
        """
        best, bd = None, radius
        seen = set()
        for k in self._cells_of(px - radius, py - radius,
                                px + radius, py + radius):
            seen.update(self._grid.get(k, ()))
        for i in list(seen) + self._big:
            e = self.entries[i]
            if e[0]:
                continue
            dx = max(e[2] - px, 0.0, px - e[5])
            dy = max(e[3] - py, 0.0, py - e[6])
            dz = max(e[4] - pz, 0.0, pz - e[7])
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            if d <= bd:
                bd, best = d, e
        return best, bd


def build_lookup(spaces):
    """Flatten spaces into a containment index for `floor_for_point`.

    Interior rooms sort before exterior sky-boxes, and within each group
    smallest piece first. Build once per map — it is queried for every object.
    """
    entries = []
    for uid, s in spaces.items():
        for (x0, y0, z0, x1, y1, z1) in s['pieces']:
            vol = max(x1 - x0, 1e-6) * max(y1 - y0, 1e-6) * max(z1 - z0, 1e-6)
            entries.append((bool(s['exterior']), vol, x0, y0, z0, x1, y1, z1, uid))
    entries.sort(key=lambda e: (e[0], e[1]))
    return _Lookup(entries)


def roof_band(floors, label='Roof'):
    """`floors` plus a synthetic storey above the top one, for roof geometry.

    The top storey's ceiling has no floor above it to belong to, so without this
    a roof is tagged with the storey underneath and hiding that storey still
    leaves you looking at a lid. The extra slab sits one median storey-height
    above the top one.

    Returned as a NEW list; the input is untouched, because Space labels are
    derived from the unextended table and a room must not become 'Roof'.
    """
    if not floors:
        return list(floors)
    gaps = sorted(floors[i + 1]['z'] - floors[i]['z']
                  for i in range(len(floors) - 1))
    storey = gaps[len(gaps) // 2] if gaps else 3.0
    top = floors[-1]
    z = top['z'] + max(storey, 2.0)
    out = list(floors)
    out[-1] = dict(top, z_max=z - FLOOR_BELOW_TOLERANCE)
    out.append({'index': len(floors), 'label': label, 'z': z,
                'z_min': z - FLOOR_BELOW_TOLERANCE, 'z_max': float('inf'),
                'rooms': 0, 'synthetic': True})
    return out


def _pick_room(floors, space_floor, hits, z):
    """Which of several rooms containing the same point owns it.

    Rooms genuinely overlap, because a Space runs from its own floor slab all
    the way to the ROOF slab: clubhouse's basement room 0x51FF6DF8 spans
    z -3.3..4.0, straight through the 1F slab at -0.45, so a 1F prop is inside
    both it and the 1F room. Roughly half of all objects sit in two rooms at
    once (clubhouse 50.5%, oregon 63.3%), and on 4-7% of objects the candidates
    disagree about the storey.

    The old rule was "first hit wins", i.e. smallest cell — an artefact of
    build_lookup's volume sort, and arbitrary with respect to storeys. On the
    objects where candidates disagreed it chose a different floor from the rule
    this module trusts everywhere else for 30-59% of them.

    So prefer the room whose own slab is at or below the height the object
    rests at, nearest to it — `_band_index`'s "geometry rests ON a floor", the
    rule the Oregon ground truth validates 48/48. Cell volume stays the final
    tie-break, which reproduces the old choice whenever every candidate is on
    the same storey (the common case).
    """
    target = _band_index(floors, z)

    def rank(e):
        f = space_floor.get(e[8], 0)
        # rooms at or below the resting storey first, then nearest to it,
        # then the tightest cell
        return (f > target, abs(f - target), e[1])

    return min(hits, key=rank)


def floor_for_point(floors, lookup, space_floor, point, base_z=None,
                    near_radius=NEAR_ROOM_RADIUS, stats=None):
    """Best floor for a world point. Returns `(floor_index, space_uid|None)`.

    Containment in a Space piece wins — that is the engine's own partition, and
    it is what keeps a light fixture hanging under a mezzanine on the right
    storey. When several rooms contain the point, `_pick_room` decides.

    **Except when the containing piece spans more than one storey.** Atria,
    stairwells and double-height halls are common (134 of Kanal's 193 rooms
    cross a slab), and handing such a room's own label to everything inside it
    tags the storey-above's floor slab and walls with the storey below — so
    hiding that storey leaves them floating and you cannot see in. There the
    object's own resting height decides instead, never below the room's own
    floor.

    When the point is in no room at all, fall back to the nearest room within
    `near_radius` before dropping to the global slab band — see
    NEAR_ROOM_RADIUS for why, and pass 0 to disable. A room reached this way is
    still returned as the space uid: the object belongs to it, it merely sits
    just outside the authored volume.

    `base_z` should be the object's *bottom* (a prop rests on its floor); the
    point's own z is used when it isn't given.

    `lookup` comes from `build_lookup`. Pass a dict as `stats` to have the
    three outcomes counted under 'contained' / 'near' / 'band'.
    """
    px, py, pz = point
    z = pz if base_z is None else base_z

    def tally(k):
        if stats is not None:
            stats[k] = stats.get(k, 0) + 1

    def resolve(e):
        """Room's own storey, overridden by the object's where the volume
        cannot speak for a storey."""
        if e[0]:
            # An EXTERIOR volume is the map's sky/roof shell, not a room, so its
            # slab says nothing about which storey an object inside it is on —
            # the object's own height is the only evidence there. This is also
            # the only way anything reaches the synthetic 'Roof' band:
            # `space_floor` is derived from the UNEXTENDED floor table (rooms
            # must not be labelled 'Roof' — see `roof_band`), so no space can
            # ever map to it.
            #
            # House v3 is the case that exposed this. It has 8 exterior roof
            # volumes spanning z 10.90..18.90; both ends sit inside the Roof
            # band, so the span test below — which needs to CROSS a boundary —
            # never fired, and every object inside them inherited the shell's
            # own 3F. Roof props were landing on 3F with their base at 18.5 m.
            return _band_index(floors, z), e[8]
        idx = space_floor.get(e[8], 0)
        if _band_index(floors, e[7]) > _band_index(floors, e[4]):
            idx = max(idx, _band_index(floors, z))
        return idx, e[8]

    hits = lookup.containing(px, py, pz)
    if hits:
        # Interior rooms answer the question; an exterior sky-box is only an
        # answer when nothing interior contains the point.
        inside = [e for e in hits if not e[0]]
        tally('contained')
        return resolve(_pick_room(floors, space_floor, inside or hits, z))

    if near_radius > 0:
        near, _d = lookup.nearest(px, py, pz, near_radius)
        if near is not None:
            idx, uid = resolve(near)
            own = _band_index(floors, z)
            # The object is OUTSIDE this room — that is the whole premise of
            # snapping — so unlike the containment path its own height still
            # gets to win upward. **Roof geometry is why this matters**: a room
            # can only ever carry a real storey, never the synthetic 'Roof'
            # band from `roof_band`, so without this a roof prop lying within
            # `near_radius` of the top storey's rooms is dragged down onto that
            # storey. On clubhouse that silently took 145 of 380 roof objects
            # (38% of the roof group) and labelled them 2F.
            if own > idx:
                # Once height overrides, the room underneath is not where this
                # object lives, so report the storey without claiming the room —
                # otherwise siege_floor says 'Roof' while siege_room names a 2F
                # room. (The multi-storey span case above deliberately DOES keep
                # its room: an atrium prop really is inside that room.)
                tally('near_above')
                return own, None
            tally('near')
            return idx, uid

    tally('band')
    return _band_index(floors, z), None


def summarize(floors, spaces):
    """One-line-per-floor summary for import logging."""
    counts = collections.Counter()
    for s in spaces.values():
        counts[_band_index(floors, s['zmin'])] += 1
    return [f"{f['label']} (z={f['z']:.2f}, {counts[f['index']]} rooms)"
            for f in floors]
