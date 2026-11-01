"""
Web GLB export for cleaned Siege maps.

In-Blender reimplementation of the `optimize.mjs` gltf-transform stage. The
original two-stage pipeline was: Blender headless cleanup (now `web/clean.py`)
-> Node gltf-transform (strip attrs -> dedup -> GPU instancing -> weld ->
error-bounded simplify -> prune -> meshopt compress). This operator collapses
the whole thing into one Blender export:

  * strip attributes      : clean.py already drops materials/UVs/colors; this
                            operator additionally exports POSITION(+NORMAL) only.
  * dedup + GPU instancing: Blender objects that share a mesh datablock are
                            exported as EXT_mesh_gpu_instancing (export_gpu_instances),
                            which is exactly the dedup+instance step.
  * weld + simplify       : an optional per-datablock COLLAPSE decimate (ratio)
                            stands in for the error-bounded simplify. weld is
                            done in clean.py's remove_doubles.
  * compress              : Draco mesh compression (the glTF exporter's built-in;
                            decoded by three.js DRACOLoader). meshopt/EXT_meshopt
                            still needs the bundled optimize.mjs Node post-step.

Differences from optimize.mjs are intentional: pure Blender has Draco, not
meshopt, and a ratio-based decimate, not a true error budget. The shipped
optimize.mjs remains the route to a meshopt EXT_meshopt_compression file.
"""

import math
import os

import bpy


# Transform components above this magnitude are treated as garbage. Real Siege
# maps fit in a few hundred metres; values near FLT_MAX (3.4e38) come from
# mis-parsed entity matrices and overflow to inf when the glTF exporter forms a
# primitive's position min/max, which it then can't serialise as JSON.
_MAX_FINITE_COORD = 1.0e30


def _is_finite(x):
    return not (math.isinf(x) or math.isnan(x))


def _is_sane(x):
    return _is_finite(x) and abs(x) < _MAX_FINITE_COORD


def sanitize_scene(use_selection=False, log=print):
    """Drop objects the glTF exporter would choke on (non-finite / absurdly
    large transforms or vertices). A single inf/NaN — or a value so large its
    products overflow to inf — makes the exporter try to write an infinite
    bounding box as a JSON int and raise OverflowError, so we strip the
    offenders up front. Returns the number of objects removed.

    Causes seen on Siege maps: an entity whose matrix bytes weren't a real
    transform (scale near FLT_MAX, location ~1e36), or a CMO with a stray
    non-finite vertex.
    """
    objs = (bpy.context.selected_objects if use_selection
            else list(bpy.context.scene.objects))
    doomed = []
    for obj in objs:
        mw = obj.matrix_world
        if not all(_is_sane(c) for row in mw for c in row):
            doomed.append(obj)
            continue
        if obj.type != 'MESH':
            continue
        me = obj.data
        bad_vert = False
        for v in me.vertices:
            if not (_is_sane(v.co.x) and _is_sane(v.co.y) and _is_sane(v.co.z)):
                bad_vert = True
                break
        if bad_vert:
            doomed.append(obj)

    for obj in doomed:
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    if doomed:
        log(f"removed {len(doomed)} objects with non-finite/oversized transform/vertices")
    return len(doomed)


def _supported_gltf_kwargs(kwargs):
    """Filter kwargs against the installed glTF exporter's accepted props.

    The exporter's property set changes across Blender versions; passing an
    unknown kwarg raises. filepath is always allowed.
    """
    try:
        props = bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
    except Exception:
        return kwargs
    return {k: v for k, v in kwargs.items() if k in props or k == "filepath"}


def simplify_datablocks(ratio, log=print):
    """Apply a COLLAPSE decimate at `ratio` to each unique mesh datablock.

    Runs once per datablock (instancing preserved) by temporarily binding a
    throwaway object to it, adding+applying a Decimate modifier via a temporary
    override context. ratio >= 1.0 is a no-op.
    """
    if ratio >= 1.0:
        return 0
    count = 0
    meshes = [m for m in bpy.data.meshes if m.users > 0 and len(m.polygons) > 0]
    for me in meshes:
        # Bind a scratch object to this datablock so the modifier evaluates on it.
        obj = bpy.data.objects.new(f"__decim_{me.name}", me)
        bpy.context.scene.collection.objects.link(obj)
        try:
            mod = obj.modifiers.new("Decimate", 'DECIMATE')
            mod.decimate_type = 'COLLAPSE'
            mod.ratio = ratio
            mod.use_collapse_triangulate = True
            with bpy.context.temp_override(object=obj,
                                           active_object=obj,
                                           selected_objects=[obj]):
                bpy.ops.object.modifier_apply(modifier=mod.name)
            count += 1
        except Exception as e:
            log(f"  WARN decimate failed on '{me.name}': {e}")
        finally:
            bpy.data.objects.remove(obj, do_unlink=True)
    return count


def export_web_glb(filepath, keep_normals=False, keep_uvs=False,
                   simplify_ratio=1.0, use_draco=True, draco_level=6,
                   use_selection=False, log=print):
    """Optimize the current scene and write a web-ready GLB.

    Returns the output file size in bytes (0 if the file wasn't written).
    """
    sanitize_scene(use_selection=use_selection, log=log)

    if simplify_ratio < 1.0:
        n = simplify_datablocks(simplify_ratio, log=log)
        log(f"decimated {n} datablocks at ratio {simplify_ratio}")

    kwargs = dict(
        filepath=filepath,
        export_format="GLB",
        export_materials="NONE",
        export_normals=keep_normals,
        export_texcoords=keep_uvs,
        export_colors=False,
        export_tangents=False,
        export_skins=False,
        export_animations=False,
        export_morph=False,
        export_apply=True,
        export_yup=True,
        use_selection=use_selection,
        # dedup + GPU instancing: objects sharing a mesh datablock become
        # EXT_mesh_gpu_instancing nodes (the optimize.mjs dedup()+instance() step).
        export_gpu_instances=True,
        # Draco compression (three.js DRACOLoader decodes it). The optimize.mjs
        # meshopt path is still available via the shipped Node script.
        export_draco_mesh_compression_enable=bool(use_draco),
        export_draco_mesh_compression_level=int(draco_level),
    )
    kwargs = _supported_gltf_kwargs(kwargs)
    log(f"exporting {filepath} (draco={use_draco}, instances=on)")
    bpy.ops.export_scene.gltf(**kwargs)

    try:
        return os.path.getsize(filepath)
    except OSError:
        return 0


class SIEGE_OT_web_export(bpy.types.Operator):
    """Export the cleaned map as a web-ready GLB.

    POSITION(+NORMAL/UV) only, no materials. Objects sharing a mesh datablock
    are written as GPU instances (EXT_mesh_gpu_instancing). Optional per-datablock
    COLLAPSE decimate and Draco compression. Run AFTER 'Clean Map for Web'.
    """
    bl_idname = "siege.web_export"
    bl_label = "Siege: Export Web GLB"
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filename_ext = ".glb"
    filter_glob: bpy.props.StringProperty(default="*.glb", options={'HIDDEN'})

    keep_normals: bpy.props.BoolProperty(
        name="Keep Normals", default=False,
        description="Export vertex normals (off: depth-shaded shells need none)")
    keep_uvs: bpy.props.BoolProperty(
        name="Keep UVs", default=False,
        description="Export UV coordinates")
    simplify_ratio: bpy.props.FloatProperty(
        name="Simplify Ratio", default=1.0, min=0.0, max=1.0,
        description="Per-datablock COLLAPSE decimate ratio (1.0 = no simplify)")
    use_draco: bpy.props.BoolProperty(
        name="Draco Compression", default=True,
        description="Compress meshes with Draco (decoded by three.js DRACOLoader)")
    draco_level: bpy.props.IntProperty(
        name="Draco Level", default=6, min=0, max=10,
        description="Draco compression level (higher = smaller, slower)")
    use_selection: bpy.props.BoolProperty(
        name="Selected Only", default=False,
        description="Export only selected objects")

    def invoke(self, context, event):
        if not self.filepath:
            blend = bpy.data.filepath
            base = (os.path.splitext(blend)[0] if blend
                    else os.path.join(os.path.expanduser("~"), "map"))
            self.filepath = base + "_web.glb"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, "No output path")
            return {'CANCELLED'}
        try:
            size = export_web_glb(
                bpy.path.abspath(self.filepath),
                keep_normals=self.keep_normals, keep_uvs=self.keep_uvs,
                simplify_ratio=self.simplify_ratio, use_draco=self.use_draco,
                draco_level=self.draco_level, use_selection=self.use_selection,
                log=lambda m: print(f"[SiegeWeb] {m}"))
        except Exception as e:
            self.report({'ERROR'}, f"Web export failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
        mb = size / 1024 / 1024
        self.report({'INFO'}, f"Exported {os.path.basename(self.filepath)} ({mb:.2f} MB)")
        return {'FINISHED'}


_classes = (SIEGE_OT_web_export,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
