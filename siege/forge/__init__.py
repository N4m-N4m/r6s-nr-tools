"""
Forge binary-format parsers.

Self-contained: no Blender imports below this layer. The functions here can
be unit-tested with plain CPython by handing them byte buffers.

Submodules:
    file_io      - read FAT entries from .forge archives
    decompress   - Scimitar zstd / streaming payload decompression
    entity       - entity record discovery + payload scanning
    mesh_records - Mesh and DestructionMeshGroup records inside LoadUnits
    cmo          - CompiledMeshObject vertex/face decoding
    material     - Material / TextureMapSpec / TextureMap / CompiledTexture
"""
