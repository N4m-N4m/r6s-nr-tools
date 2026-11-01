"""
Oodle runtime loader (ctypes binding for oo2core_8_win64.dll).

Y11S2 (forge v34) wraps compressed chunks in Oodle (OodleLZ), not zstd. There
is no pure-Python Oodle decoder, so we bind the proprietary native DLL the same
way the C# core does (RainbowForge/Compression/Oodle2Core8.cs) via
`OodleLZ_Decompress`.

DLL discovery order:
    1. OODLE2_8_PATH environment variable (the same var the C# tool uses).
    2. A short list of common install locations.
The DLL ships with the game / tool; we never download it. When it can't be
found, oodle_decompress raises and the importer surfaces a clear message telling
the user to set OODLE2_8_PATH (Y11S2 maps can't be read without it).
"""

import ctypes
import os

# Loaded lazily so importing this module never fails (the parser layer must stay
# importable from plain CPython for tests, and zstd-only maps don't need Oodle).
_oodle_dll = None
_oodle_decompress = None
_load_error = None

# Candidate DLL locations checked after OODLE2_8_PATH. The first existing file
# wins. Add more here if the DLL lives somewhere unusual on a given machine.
_FALLBACK_PATHS = (
    r"C:\oodle\oo2core_8_win64.dll",
    r"C:\Program Files\oodle\oo2core_8_win64.dll",
)

# OodleLZ_Decompress signature (subset we use — decode only). Matches the
# [DllImport] declaration in Oodle2Core8.cs. fuzzSafe/checkCrc/verbosity are the
# only flags that matter for a plain decode; the rest are null/zero.
#   int OodleLZ_Decompress(
#       const void* compBuf, intptr_t compBufSize,
#       void* rawBuf, intptr_t rawLen,
#       int fuzzSafe, int checkCRC, int verbosity,
#       void* dictBuf, intptr_t dictSize,
#       void* fpCallback, void* callbackUserData,
#       void* decoderMem, intptr_t decoderMemSize,
#       int threadPhase)


def _resolve_dll_path():
    env = os.environ.get("OODLE2_8_PATH")
    if env and os.path.isfile(env):
        return env
    for p in _FALLBACK_PATHS:
        if os.path.isfile(p):
            return p
    return None


def ensure_oodle():
    """Load the Oodle DLL and bind OodleLZ_Decompress. Idempotent.

    Returns the bound decompress callable, or raises RuntimeError with a
    user-actionable message when the DLL can't be located/loaded.
    """
    global _oodle_dll, _oodle_decompress, _load_error
    if _oodle_decompress is not None:
        return _oodle_decompress
    if _load_error is not None:
        # Don't keep retrying a known-bad load every chunk.
        raise RuntimeError(_load_error)

    dll_path = _resolve_dll_path()
    if dll_path is None:
        _load_error = (
            "Oodle library not found. Y11S2 (forge v34) maps use Oodle "
            "compression. Set the OODLE2_8_PATH environment variable to your "
            "oo2core_8_win64.dll, or place it at C:\\oodle\\oo2core_8_win64.dll."
        )
        raise RuntimeError(_load_error)

    try:
        dll = ctypes.WinDLL(dll_path)
    except OSError as e:
        _load_error = f"Failed to load Oodle DLL at {dll_path}: {e}"
        raise RuntimeError(_load_error)

    fn = dll.OodleLZ_Decompress
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.c_char_p,   # compBuf
        ctypes.c_ssize_t,  # compBufSize
        ctypes.c_char_p,   # rawBuf (writeable; we pass a ctypes buffer)
        ctypes.c_ssize_t,  # rawLen
        ctypes.c_int,      # fuzzSafe
        ctypes.c_int,      # checkCRC
        ctypes.c_int,      # verbosity
        ctypes.c_void_p,   # dictBuf
        ctypes.c_ssize_t,  # dictSize
        ctypes.c_void_p,   # fpCallback
        ctypes.c_void_p,   # callbackUserData
        ctypes.c_void_p,   # decoderMem
        ctypes.c_ssize_t,  # decoderMemSize
        ctypes.c_int,      # threadPhase
    ]

    _oodle_dll = dll
    _oodle_decompress = fn
    print(f"[Siege] Oodle loaded from {dll_path}")
    return _oodle_decompress


def oodle_decompress(compressed, decompressed_length):
    """Decompress an OodleLZ chunk to exactly `decompressed_length` bytes.

    Mirrors Oodle2Core8.Decompress: unthreaded, no fuzz-safety, no CRC check.
    Returns the decoded bytes, or raises on a short/failed decode.
    """
    fn = ensure_oodle()
    out = ctypes.create_string_buffer(decompressed_length)
    n = fn(
        compressed, len(compressed),
        out, decompressed_length,
        0,  # FuzzSafe.No
        0,  # CheckCRC.No
        0,  # Verbosity.None
        None, 0,
        None, None,
        None, 0,
        3,  # ThreadPhase.Unthreaded (ThreadPhaseAll)
    )
    if n != decompressed_length:
        raise IOError(
            f"Oodle decode produced {n} bytes, expected {decompressed_length}")
    return out.raw[:decompressed_length]


# Probe DLL availability without loading at import time. The importer can read
# this to warn early ("Y11S2 map but Oodle DLL not configured") before doing
# the full forge walk.
def is_available():
    return _resolve_dll_path() is not None
