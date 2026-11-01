"""Fast geometric ambient-occlusion bake for the web export.

Writes a per-vertex AO signal into a POINT-domain color attribute (`web_ao`) that
the web viewer shapes live (strength / contrast / far-boost) at zero runtime
cost. The GLB carries it as COLOR_0, Draco-compressed like positions. No
UVs/textures.

NOT ray-traced, and NOT using Blender operators. The maps are decimated to low
vert counts and the viewer only needs a *plausible* occlusion signal to read
edges and ground geometry. Two earlier approaches were both far too slow:
  * Cycles AO bake — ray traced the whole scene per object, ~minutes.
  * Blender's Dirty Vertex Colors operator — `bpy.ops.paint.vertex_color_dirt`
    is ~330 ms PER CALL (context/depsgraph/undo overhead), so 6700 objects took
    ~37 minutes. Operators in a per-object loop are the classic Blender perf trap.

This computes everything with numpy + foreach_get/set (bulk array reads, zero
operator calls) — the full 3.5M-vert Casino map bakes in ~1.5 s:

  1. Cavity / concavity — per vertex, the direction to the mean of its edge
     neighbours vs. its normal. Concave verts (crevices, inner corners) darken.
     Per-datablock (local geometry), cached so instanced meshes compute once.

  2. Grounding — downward-facing and low (small world-Z) vertices darken, so
     floors and wall bases read grounded. Uses the datablock's representative
     world transform (instances share one vertex-colour buffer, so grounding is
     resolved once per datablock — fine for the small instanced props, and the
     look is faked + live-tuned in the viewer anyway).

`compute_ao` (numpy) and `ground_ao` (scalar) are bpy-free for unit testing.
"""

try:
    import numpy as np
except ImportError:
    np = None

try:
    import bpy
    from mathutils import Vector
except ImportError:
    bpy = None


AO_LAYER_NAME = "web_ao"   # color attribute exported as COLOR_0

# default shaping weights for the bake (the viewer re-shapes live on top)
GROUND_STRENGTH = 0.5      # darken down-facing surfaces
LOW_STRENGTH = 0.4         # extra darkening near the floor
CAVITY_STRENGTH = 0.7      # darken concave verts (crevices / inner corners)


def ground_ao(world_z, normal_z, z_min, z_max,
              ground_strength=GROUND_STRENGTH, low_strength=LOW_STRENGTH):
    """Scalar grounding factor in [0,1] (1 = unoccluded). Pure; for tests."""
    span = max(z_max - z_min, 1e-6)
    h = min(max((world_z - z_min) / span, 0.0), 1.0)
    down = max(-normal_z, 0.0)
    low = 1.0 - h
    occ = ground_strength * down + low_strength * low * down
    return max(0.0, 1.0 - occ)


def compute_ao(co, no, edges, mw3, mw_t, z_min, z_max,
               ground_strength=GROUND_STRENGTH, low_strength=LOW_STRENGTH,
               cavity_strength=CAVITY_STRENGTH):
    """Vectorised AO for one mesh. Returns float array[n] in [0,1].

    co     : (n,3) local vertex coords          no  : (n,3) local vertex normals
    edges  : (m,2) int vertex-index pairs       mw3 : (3,3) world rotation/scale
    mw_t   : (3,) world translation             z_min/z_max : scene Z extent
    Pure numpy (no bpy) so it can be tested headless.
    """
    n = co.shape[0]
    # --- concavity from edge neighbours ---
    cav = np.zeros(n)
    if edges is not None and len(edges):
        a, b = edges[:, 0], edges[:, 1]
        s = np.zeros((n, 3)); cnt = np.zeros(n)
        np.add.at(s, a, co[b]); np.add.at(cnt, a, 1.0)
        np.add.at(s, b, co[a]); np.add.at(cnt, b, 1.0)
        to_nbr = s / np.maximum(cnt, 1.0)[:, None] - co
        ln = np.linalg.norm(to_nbr, axis=1, keepdims=True); ln[ln == 0] = 1.0
        to_nbr /= ln
        cav = np.clip(np.sum(to_nbr * no, axis=1), 0.0, 1.0)  # >0 = concave
    # --- grounding in world space ---
    world = co @ mw3.T + mw_t
    wz = world[:, 2]
    wn = no @ mw3.T
    wnl = np.linalg.norm(wn, axis=1); wnl[wnl == 0] = 1.0
    wnz = wn[:, 2] / wnl
    span = max(z_max - z_min, 1e-6)
    h = np.clip((wz - z_min) / span, 0.0, 1.0)
    down = np.maximum(-wnz, 0.0)
    low = 1.0 - h
    occ = ground_strength * down + low_strength * low * down + cavity_strength * cav
    return np.clip(1.0 - occ, 0.0, 1.0)


if bpy is not None:

    def _ensure_color_layer(obj, name=AO_LAYER_NAME):
        """Ensure obj.data has a POINT-domain color attribute `name`, active for
        viewport + render (so glTF writes it as COLOR_0). Returns the layer."""
        me = obj.data
        ca = me.color_attributes
        layer = ca.get(name)
        if layer is None:
            layer = ca.new(name=name, type='BYTE_COLOR', domain='POINT')
        idx = ca.find(name)
        if idx >= 0:
            ca.active_color_index = idx
            ca.render_color_index = idx
        return layer

    def _mesh_arrays(me):
        """Pull (co, no, edges) as numpy arrays via foreach_get (fast)."""
        n = len(me.vertices)
        co = np.empty(n * 3); me.vertices.foreach_get("co", co)
        no = np.empty(n * 3); me.vertices.foreach_get("normal", no)
        ne = len(me.edges)
        edges = None
        if ne:
            edges = np.empty(ne * 2, dtype=np.int64)
            me.edges.foreach_get("vertices", edges)
            edges = edges.reshape(-1, 2)
        return co.reshape(-1, 3), no.reshape(-1, 3), edges

    def _write_ao(me, ao):
        """Write float AO array[n] into the web_ao POINT colors (grayscale RGB)."""
        layer = me.color_attributes.get(AO_LAYER_NAME)
        if layer is None or len(layer.data) != ao.shape[0]:
            return False
        n = ao.shape[0]
        rgba = np.empty((n, 4), dtype=np.float32)
        rgba[:, 0] = ao; rgba[:, 1] = ao; rgba[:, 2] = ao; rgba[:, 3] = 1.0
        layer.data.foreach_set("color", rgba.ravel())
        me.update()
        return True

    def _scene_z_extent(meshes):
        zmin, zmax = float('inf'), float('-inf')
        for o in meshes:
            m = o.matrix_world
            for c in o.bound_box:
                wz = (m @ Vector((c[0], c[1], c[2]))).z
                if wz < zmin: zmin = wz
                if wz > zmax: zmax = wz
        if zmin == float('inf'):
            return 0.0, 1.0
        return zmin, zmax

    def bake_object_ao(obj, z_min, z_max, ground_strength=GROUND_STRENGTH,
                       low_strength=LOW_STRENGTH, cavity_strength=CAVITY_STRENGTH):
        """Compute + write AO for one object's datablock. Fast (numpy)."""
        if obj.type != 'MESH' or obj.data is None or np is None:
            return False
        me = obj.data
        _ensure_color_layer(obj)
        co, no, edges = _mesh_arrays(me)
        mw = np.array(obj.matrix_world)
        ao = compute_ao(co, no, edges, mw[:3, :3], mw[:3, 3], z_min, z_max,
                        ground_strength, low_strength, cavity_strength)
        return _write_ao(me, ao)

    def bake_scene_ao(context, objects, ground_strength=GROUND_STRENGTH,
                      low_strength=LOW_STRENGTH, cavity_strength=CAVITY_STRENGTH,
                      **_ignored):
        """One-shot AO over all meshes (used by the exporter). Bakes each unique
        datablock once (instances share the vertex-colour buffer)."""
        if np is None:
            print("[SiegeWeb] numpy unavailable — cannot bake AO")
            return 0
        meshes = [o for o in objects if o.type == 'MESH' and o.data is not None]
        if not meshes:
            return 0
        z_min, z_max = _scene_z_extent(meshes)
        seen = set()
        done = 0
        for obj in meshes:
            if obj.data.name in seen:
                continue
            seen.add(obj.data.name)
            if bake_object_ao(obj, z_min, z_max,
                              ground_strength, low_strength, cavity_strength):
                done += 1
        return done


    class SIEGE_OT_web_bake_ao(bpy.types.Operator):
        """Fast geometric AO bake into vertex colours (numpy, no operators/Cycles).

        Modal so a % shows on the cursor; each step is cheap array math. Bakes
        each unique mesh datablock once (instances share the buffer).
        """
        bl_idname = "siege.web_bake_ao"
        bl_label = "Siege: Bake AO to Vertex Colors"
        bl_options = {'REGISTER'}     # no UNDO: we write color buffers in bulk

        scope: bpy.props.EnumProperty(
            name="Scope",
            items=(('SCENE', "Whole Scene", "All mesh objects"),
                   ('SELECTED', "Selected Only", "Only selected mesh objects")),
            default='SCENE',
        )
        ground_strength: bpy.props.FloatProperty(
            name="Ground strength", default=GROUND_STRENGTH, min=0.0, max=1.0,
            description="Darken downward-facing surfaces")
        low_strength: bpy.props.FloatProperty(
            name="Low-height strength", default=LOW_STRENGTH, min=0.0, max=1.0,
            description="Extra darkening near the floor")
        cavity_strength: bpy.props.FloatProperty(
            name="Cavity strength", default=CAVITY_STRENGTH, min=0.0, max=2.0,
            description="Darken concave verts (crevices, inner corners)")

        _datablocks = None   # list of representative objects, one per datablock
        _i = 0
        _total = 0
        _zmin = 0.0
        _zmax = 1.0
        _timer = None

        def _collect(self, context):
            src = (context.selected_objects if self.scope == 'SELECTED'
                   else context.scene.objects)
            return [o for o in src if o.type == 'MESH' and o.data is not None]

        def execute(self, context):
            n = bake_scene_ao(context, self._collect(context),
                              self.ground_strength, self.low_strength,
                              self.cavity_strength)
            self.report({'INFO'} if n else {'WARNING'},
                        f"Baked AO on {n} datablocks" if n else "Nothing baked")
            return {'FINISHED'} if n else {'CANCELLED'}

        def invoke(self, context, event):
            if np is None:
                self.report({'ERROR'}, "numpy unavailable — cannot bake AO")
                return {'CANCELLED'}
            objs = self._collect(context)
            if not objs:
                self.report({'WARNING'}, "No mesh objects to bake")
                return {'CANCELLED'}
            self._zmin, self._zmax = _scene_z_extent(objs)
            # one representative object per unique datablock
            seen = {}
            for o in objs:
                seen.setdefault(o.data.name, o)
            self._datablocks = list(seen.values())
            self._i = 0
            self._total = len(self._datablocks)
            context.window_manager.progress_begin(0, 100)
            context.window.cursor_set('NONE')
            self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
            context.window_manager.modal_handler_add(self)
            print(f"[SiegeWeb] Geometric AO: {len(objs)} objs / {self._total} datablocks")
            return {'RUNNING_MODAL'}

        def modal(self, context, event):
            if event.type == 'ESC':
                return self._finish(context, True)
            if event.type != 'TIMER':
                return {'RUNNING_MODAL'}
            # Each datablock is ~1 ms; do a big chunk per tick for throughput.
            end = min(self._i + 200, self._total)
            for j in range(self._i, end):
                bake_object_ao(self._datablocks[j], self._zmin, self._zmax,
                               self.ground_strength, self.low_strength,
                               self.cavity_strength)
            self._i = end
            if self._i >= self._total:
                return self._finish(context, False)
            pct = int(self._i / max(self._total, 1) * 100)
            context.window_manager.progress_update(pct)
            context.workspace.status_text_set(
                f"Baking AO… {pct}%  ({self._i}/{self._total}) — Esc to cancel")
            return {'RUNNING_MODAL'}

        def _finish(self, context, cancelled):
            wm = context.window_manager
            if self._timer:
                wm.event_timer_remove(self._timer); self._timer = None
            wm.progress_end()
            context.window.cursor_set('DEFAULT')
            context.workspace.status_text_set(None)
            if cancelled:
                self.report({'WARNING'}, f"AO bake cancelled ({self._i}/{self._total})")
                return {'CANCELLED'}
            self.report({'INFO'}, f"Baked AO on {self._i} datablocks -> '{AO_LAYER_NAME}'")
            print(f"[SiegeWeb] Geometric AO done: {self._i} datablocks")
            return {'FINISHED'}


    _classes = (SIEGE_OT_web_bake_ao,)

    def register():
        for cls in _classes:
            bpy.utils.register_class(cls)

    def unregister():
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)
