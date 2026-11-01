"""
Per-build type-magic table, detected from the game's own bytes.

WHY THIS EXISTS
---------------
Siege re-hashes its 4-byte type magics roughly every game patch. The record
LAYOUTS never change - only the magic VALUES - and the transform is a bespoke
table lifted from the game binary, so it is NOT computable (see
`research/y11s2_magic_rehash.md`). Every hardcoded constant therefore goes stale
on the next patch, and because the parsers just "find nothing" rather than
raising, the failure is silent: an empty map import, untextured materials, no
armature.

This is the Python counterpart of `RainbowForge/Core/MagicRegistry.cs`. The two
share one on-disk cache, so whichever runs first pays for detection and the
other just reads the answer.

HOW DETECTION WORKS
-------------------
Two sources, both relying only on relationships that survive a re-hash:

1. **StaticUid anchors.** Engine singletons keep the same uid forever (0x800 =
   GameBootstrap, 0xBDB = the default Material, 0xC1C = the default TextureMap,
   0xE10 = the unit-sphere Mesh). An asset block spells its own identity as
   ``[magic][uid][magic]``, so reading a known uid's block hands back that
   type's CURRENT magic with no table involved.
2. **The same trick inside GameBootstrap**, where most manager singletons and
   the default Material actually live rather than at forge top level.

Types with no singleton (Entity, LoadUnit, the Bone family) are still recovered
by the existing structural harvesters in `v34_map.py`; this module supplies the
ones that CAN be anchored, which is what the material/texture chain needs.
"""

import json
import os
import struct

# Canonical (pre-v34) magics. These are what the rest of the addon compares
# against; the registry's job is to translate this build's spelling back to one
# of these. Values are zlib.crc32 of the type name, which is what the magic used
# to be before the re-hash.
CANONICAL = {
    'GameBootstrap': 0xE5A83560,
    'Material': 0x85C817C3,
    'TextureMap': 0xA2B7E917,
    'Mesh': 0x415D9568,
    'CompiledMesh': 0xFC9E1595,
    'CompiledTextureMap': 0x13237FE9,
    'SoundSystem': 0xD5B8BCCF,
    'TagManager': 0x6AB6B15A,
    'PlatformManager': 0xAE88DE65,
    'R6Configs': 0xE9B7B975,
    'GameDataManager': 0x2F9A4C63,
    'VisualRenderingSettings': 0x1EA9980D,
}

# StaticUid -> type name. An engine singleton's uid, and the fact that the
# DEFAULT instance of a type IS that type, are both stable across patches.
STATIC_ANCHORS = {
    0x800: 'GameBootstrap',
    0x89E: 'SoundSystem',
    0x8A4: 'TagManager',
    0x969: 'PlatformManager',
    0x962: 'R6Configs',
    0x964: 'GameDataManager',
    0xBDB: 'Material',
    0xBFF: 'VisualRenderingSettings',
    0xC1C: 'TextureMap',
    0xC1D: 'CompiledTextureMap',
    0xE10: 'Mesh',
    0xE11: 'CompiledMesh',
}

_CACHE_VERSION = 1


def _path_key(game_dir):
    """FNV-1a over the normalised directory - must match MagicRegistry.PathKey
    in C# exactly, or the two sides write to different files and never share."""
    norm = game_dir.replace('/', '\\').rstrip('\\').lower()
    h = 2166136261
    for ch in norm:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return f"{h:08X}"


def cache_path_for(game_dir):
    """The shared cache file, same location the C# side uses."""
    local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(local, 'Prism', 'magics', f"magics-{_path_key(game_dir)}.json")


def build_fingerprint(game_dir):
    """Identifies the game build so a cached table is dropped the moment the
    game updates. Mirrors the C# `BuildFingerprint`: size + mtime of the main
    forge and the executable, either of which a patch rewrites."""
    parts = []
    for name in ('datapc64.forge', 'RainbowSix.exe'):
        p = os.path.join(game_dir, name)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        # .NET DateTime ticks: 100ns units since 0001-01-01, with the Unix epoch
        # at 621355968000000000. Computed from st_mtime_ns, NOT st_mtime: the
        # float seconds lose precision at this magnitude and land a tick or two
        # off, which would make every fingerprint comparison with the C# side
        # fail and re-detect on every import.
        ticks = st.st_mtime_ns // 100 + 621355968000000000
        parts.append(f"{st.st_size}-{ticks}_")
    return ''.join(parts) if parts else None


class MagicRegistry:
    """Maps this build's type magics back to their canonical values."""

    def __init__(self, to_canonical=None, detected=None, fingerprint='baked'):
        self.to_canonical = dict(to_canonical or {})
        self.detected = dict(detected or {})
        self.fingerprint = fingerprint
        self._to_current = {}
        for cur, canon in self.to_canonical.items():
            self._to_current[canon] = cur

    def canonical(self, magic):
        """This build's magic -> canonical, or unchanged when never re-hashed."""
        return self.to_canonical.get(magic, magic)

    def current(self, canonical):
        """Canonical magic -> what it is called on this build."""
        return self._to_current.get(canonical, canonical)

    def current_named(self, name):
        """Current magic for a type NAME from :data:`CANONICAL`."""
        canon = CANONICAL.get(name)
        return self.current(canon) if canon is not None else None

    def all_spellings(self, canonical):
        """Every known spelling of a canonical magic, current build first.

        Scanners should try all of them: a payload may come from an older dump,
        and trying a stale value costs one failed scan rather than a wrong
        answer."""
        out = []
        cur = self._to_current.get(canonical)
        if cur is not None:
            out.append(cur)
        for c, canon in self.to_canonical.items():
            if canon == canonical and c not in out:
                out.append(c)
        if canonical not in out:
            out.append(canonical)
        return out

    def __repr__(self):
        return f"<MagicRegistry {len(self.detected)} detected, build {self.fingerprint}>"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _self_type(data, uid, scan_limit=4096):
    """An asset block states its own type as ``[magic][uid u64][magic]``. The
    two copies must agree, which is what makes the match trustworthy. Returns
    the magic, or None."""
    if not data:
        return None
    needle = struct.pack('<Q', uid)
    end = min(len(data), scan_limit)
    start = 4
    while True:
        i = data.find(needle, start, end)
        if i < 0 or i < 4 or i + 12 > len(data):
            return None
        before = struct.unpack_from('<I', data, i - 4)[0]
        after = struct.unpack_from('<I', data, i + 8)[0]
        if before == after and before > 0xFFFF and before != 0xFFFFFFFF:
            return before
        start = i + 1


def detect(game_dir, log=None):
    """Detect this build's magics from ``datapc64.forge``. Returns a
    :class:`MagicRegistry`, or None when nothing could be read."""
    from . import v34

    main = os.path.join(game_dir, 'datapc64.forge')
    if not os.path.isfile(main):
        return None

    detected = {}
    try:
        with open(main, 'rb') as f:
            file_len = os.path.getsize(main)
            entries = v34.read_v34_entries(f, file_len)
            wanted = dict(STATIC_ANCHORS)
            bootstrap = None

            for e in entries:
                uid = e['uid']
                if uid not in wanted and uid != 0x800:
                    continue
                block = e.get('_v34_asset')
                if block is None:
                    continue
                try:
                    data = block.read_data(f)
                except Exception:
                    continue
                if uid == 0x800:
                    bootstrap = data
                name = wanted.get(uid)
                if name is None:
                    continue
                m = _self_type(data, uid)
                if m:
                    detected[name] = m
                    wanted.pop(uid, None)

            # Most singletons - including the default Material that anchors the
            # whole albedo chain - are serialized INSIDE GameBootstrap rather
            # than being top-level entries. Their uids are just as fixed there.
            if bootstrap and wanted:
                for uid, name in list(wanted.items()):
                    m = _self_type(bootstrap, uid, scan_limit=len(bootstrap))
                    if m:
                        detected[name] = m
    except Exception as ex:
        if log:
            log(f"[Siege][magic] detection failed: {ex}")
        return None

    if not detected:
        return None

    to_canonical = {}
    for name, magic in detected.items():
        canon = CANONICAL.get(name)
        if canon is not None:
            to_canonical[magic] = canon

    reg = MagicRegistry(to_canonical, detected, build_fingerprint(game_dir) or 'unknown')
    if log:
        log(f"[Siege][magic] detected {len(detected)} types for this build")
        for name in sorted(detected):
            log(f"[Siege][magic]     {name:<26} 0x{detected[name]:08X}")
    return reg


def _load_cache(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            dto = json.load(fh)
    except Exception:
        return None
    if not isinstance(dto, dict) or dto.get('Version') != _CACHE_VERSION:
        return None

    def _hex(s):
        try:
            return int(str(s), 16)
        except (TypeError, ValueError):
            return None

    to_canonical = {}
    for k, v in (dto.get('ToCanonical') or {}).items():
        kk, vv = _hex(k), _hex(v)
        if kk is not None and vv is not None:
            to_canonical[kk] = vv
    detected = {}
    for k, v in (dto.get('Detected') or {}).items():
        vv = _hex(v)
        if vv is not None:
            detected[k] = vv
    if not to_canonical:
        return None
    return MagicRegistry(to_canonical, detected, dto.get('Fingerprint'))


def _save_cache(path, reg):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        dto = {
            'Version': _CACHE_VERSION,
            'Fingerprint': reg.fingerprint,
            'Detected': {k: f"0x{v:08X}" for k, v in reg.detected.items()},
            'ToCanonical': {f"0x{k:08X}": f"0x{v:08X}" for k, v in reg.to_canonical.items()},
        }
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(dto, fh, indent=2)
    except Exception:
        pass  # the cache is an optimisation; never fail an import over it


_CACHE = {}


def for_game_dir(game_dir, log=None):
    """The registry for a game directory: the cached table when it matches the
    build on disk, otherwise a fresh detection which is then cached.

    Shares the cache file with Prism, so if the C# side already detected this
    build the answer is just read back.
    """
    if not game_dir:
        return None
    key = os.path.normcase(os.path.abspath(game_dir))
    if key in _CACHE:
        return _CACHE[key]

    fp = build_fingerprint(game_dir)
    path = cache_path_for(game_dir)
    cached = _load_cache(path)
    if cached is not None and fp is not None and cached.fingerprint == fp:
        if log:
            log(f"[Siege][magic] using cached table ({len(cached.detected)} types)")
        _CACHE[key] = cached
        return cached

    if log:
        log("[Siege][magic] game updated or no cache - detecting type magics...")
    reg = detect(game_dir, log)
    if reg is None:
        # Keep a stale cache over nothing: its older values may still be right
        # for the types this patch did not touch.
        _CACHE[key] = cached
        return cached
    _save_cache(path, reg)
    _CACHE[key] = reg
    return reg


def game_dir_of(forge_path):
    """The install directory a forge belongs to."""
    try:
        return os.path.dirname(os.path.abspath(forge_path))
    except Exception:
        return None
