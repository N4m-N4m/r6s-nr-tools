"""
Siege Map (.forge) Blender addon.

Installable via Edit > Preferences > Add-ons > Install...; point it at a zip
of this directory (or the directory itself when running from source).

Layout:
    common/        constants, runtime helpers (zstd loader)
    forge/         pure binary-format parsers (no bpy dependency below this layer)
    importers/     Blender operators that drive the import pipeline
    web/           post-process tools that prepare an imported scene for web export

Both registered sub-packages must expose `register()` / `unregister()`. The
root register() composes them; on failure during a partial register we
unwind to avoid leaking class registrations into Blender.
"""

bl_info = {
    "name": "Siege Map (.forge)",
    "author": "Prism",
    "version": (0, 5, 0),
    "blender": (3, 6, 0),
    "location": "File > Import > Siege Map (.forge); View3D > N-panel > Siege",
    "description": "Import Rainbow Six: Siege maps and optimize them for web export",
    "category": "Import-Export",
}


# Submodules are imported lazily inside register() so merely importing
# `siege_addon` doesn't require bpy. This keeps the parser layer
# (siege_addon.forge.*) usable from plain CPython for testing.
_registered_modules = []


def _iter_submodules():
    from .importers import map_importer
    from .web import clean as web_clean
    from .web import decals as web_decals
    from .web import ao_bake as web_ao_bake
    from .web import export as web_export
    from .web import floors as web_floors
    from .web import export_map as web_export_map
    from .web import optimize as web_optimize
    # web_optimize hosts the panel, whose buttons reference the operator modules
    # by bl_idname (and draws the floors UIList + scene.siege_floors). Register
    # those operator modules — including web_floors — first so the idnames,
    # UIList class, and scene props exist when the panel registers/draws.
    return (map_importer, web_clean, web_decals, web_ao_bake,
            web_export, web_floors, web_export_map, web_optimize)


def register():
    global _registered_modules
    _registered_modules = []
    try:
        for mod in _iter_submodules():
            mod.register()
            _registered_modules.append(mod)
    except Exception:
        for mod in reversed(_registered_modules):
            try:
                mod.unregister()
            except Exception:
                pass
        _registered_modules = []
        raise


def unregister():
    global _registered_modules
    for mod in reversed(_registered_modules):
        try:
            mod.unregister()
        except Exception:
            pass
    _registered_modules = []


if __name__ == "__main__":
    register()
