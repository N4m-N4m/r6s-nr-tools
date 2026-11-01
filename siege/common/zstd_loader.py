"""
zstandard runtime loader.

The forge format wraps Scimitar-compressed payloads in zstd frames. The
addon needs `zstandard` available at import time of forge.decompress; if
the user's Blender Python doesn't ship it we install it on demand using
the Blender-bundled pip.
"""

import sys


def ensure_zstandard():
    """Return the zstandard module, pip-installing it if missing."""
    try:
        import zstandard
        return zstandard
    except ImportError:
        import subprocess
        print("[Siege] zstandard not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "zstandard"])
        import zstandard
        print("[Siege] zstandard installed successfully")
        return zstandard


try:
    import zstandard as zstd
except ImportError:
    zstd = None
