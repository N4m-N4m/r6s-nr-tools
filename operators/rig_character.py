"""Build an animator-friendly IK control rig (Rigify) for an imported Siege character.

The group importer produces `Prism_Skeleton`: the game's own joint hierarchy, bone for bone,
each bone a short stub along its game-local Y axis, with the Siege vertex weights bound to it
by name. `.prismanim` clips and any export back out expect exactly that skeleton, so this
module never edits it. It builds a separate Rigify rig and makes the Siege skeleton FOLLOW it:

    Rigify controls (IK/FK arms + legs, torso, neck/head, palms, fingers)
      -> ORG-/DEF- bones of the generated rig
        -> PRISM-<bone> offset bones (parented to the chosen rig bone, rest = Siege bind frame)
          -> COPY_TRANSFORMS on every Prism_Skeleton bone -> Armature modifier -> mesh

Why the offset bones: a Rigify bone runs joint to joint, a Siege bone is a stub in the game's
local frame, so their rest orientations never agree. An offset bone sitting at the Siege bind
matrix, parented under the Rigify bone, carries that difference, so the constraint is exact in
rest pose (`verify_binding` measures it) and each Siege bone moves rigidly with its driver.

What makes this generic across Siege characters rather than tuned to one:

1. Joints are found by the humanoid names every Siege character skeleton carries
   (Hips / Spine* / Neck / Head, Left|Right UpLeg / Leg / Foot / ToeBase,
   Shoulder / Arm / ForeArm / Hand, Hand<Finger>1-3). The metacarpals under the hands map to
   Rigify's palm bones.
2. The metarig is fitted in a canonical frame (faces -Y, character left = +X, up = +Z) derived
   from the skeleton; the metarig object carries the inverse rotation. Siege exports face +Y,
   but an export in any orientation rigs the same way.
3. Every other deforming bone (gear, cables, cloth, face, correctives, and mechanical joints
   like the Talos robot head, whose yaw and pitch are separate bones hanging off Spine2) gets
   an FK control at its own Siege joint, parented along the Siege hierarchy. Nothing is
   re-parented by guesswork, see `_plan_binding`.

Re-run safe: building again replaces the previous rig. Editing the metarig and pressing
"Regenerate" (ours or Rigify's own) keeps the edits and re-binds.
"""

import json
import math
import re

import bpy  # type: ignore
from bpy.props import BoolProperty  # type: ignore
from mathutils import Matrix, Vector  # type: ignore

RIG_CONSTRAINT = "PrismRig"
BIND_PREFIX = "PRISM-"
BIND_COLLECTION = "Prism Bind (MCH)"

PROP_RIG = "prism_ik_rig"            # on the Siege armature -> generated rig object
PROP_METARIG = "prism_ik_metarig"    # on the Siege armature -> metarig object
PROP_SOURCE = "prism_source_armature"  # on rig + metarig -> the Siege armature
PROP_BIND = "prism_bind_pose"        # on the Siege armature: {bone: 16 floats}, see _bind_pose
PROP_DRIVER_OVERRIDE = "prism_rig_driver"  # optional, on a Siege bone: rig bone it must follow

SIDES = (("Left", "L"), ("Right", "R"))
# (Siege finger name, Rigify finger prefix, Rigify palm bone)
FINGERS = (("Index", "f_index", "palm.01"), ("Middle", "f_middle", "palm.02"),
           ("Ring", "f_ring", "palm.03"), ("Pinky", "f_pinky", "palm.04"))
LIMB_SEGMENTED = ("upper_arm", "forearm", "thigh", "shin")

UP = Vector((0.0, 0.0, 1.0))
FORWARD = Vector((0.0, -1.0, 0.0))   # canonical: the character faces -Y
BACKWARD = Vector((0.0, 1.0, 0.0))


class SkeletonError(RuntimeError):
    """The armature is not a humanoid Siege skeleton this builder understands."""


# ---------------------------------------------------------------------------
# Context helpers. The operators run from the sidebar (a VIEW_3D context), but the builder is
# also driven from scripts / MCP where context.area is None and mode_set would refuse.
# ---------------------------------------------------------------------------

def _view3d_override(context):
    win = context.window or next(iter(context.window_manager.windows), None)
    if win is None:
        return {}
    for area in win.screen.areas:
        if area.type == 'VIEW_3D':
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            return {"window": win, "screen": win.screen, "area": area, "region": region}
    return {"window": win, "screen": win.screen}


def _set_mode(mode):
    if bpy.context.object is not None and bpy.context.object.mode != mode:
        bpy.ops.object.mode_set(mode=mode)


def _activate(obj):
    ctx = bpy.context
    if ctx.object is not None and ctx.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in ctx.selected_objects:
        o.select_set(False)
    obj.hide_viewport = False
    obj.hide_set(False)
    obj.select_set(True)
    ctx.view_layer.objects.active = obj


def _ensure_rigify():
    if "rigify" not in bpy.context.preferences.addons:
        import addon_utils  # type: ignore
        addon_utils.enable("rigify", default_set=True)


def source_armature(obj):
    """The Siege armature an object belongs to: itself, its rig/metarig owner, or a mesh's."""
    if obj is None:
        return None
    src = obj.get(PROP_SOURCE)
    if src is not None and getattr(src, "type", None) == 'ARMATURE':
        return src
    if obj.type == 'ARMATURE':
        return None if "rig_id" in obj.data else obj
    for mod in getattr(obj, "modifiers", ()):
        if mod.type == 'ARMATURE' and mod.object is not None:
            return source_armature(mod.object)
    if obj.parent is not None:
        return source_armature(obj.parent)
    return None


# ---------------------------------------------------------------------------
# 1. Analyse the Siege skeleton
# ---------------------------------------------------------------------------

def _resolve_joints(arm):
    """Map humanoid roles to bone names. Raises SkeletonError listing what is missing."""
    index = {}
    for b in arm.data.bones:
        index.setdefault(b.name.split(":")[-1].lower(), b.name)

    def get(name):
        return index.get(name.lower())

    def numbered(stem):
        found = [(int(k[len(stem):] or 0), v) for k, v in index.items()
                 if re.fullmatch(stem + r"\d*", k)]
        return [v for _, v in sorted(found)]

    j = {"hips": get("Hips"), "spine": numbered("spine"), "neck": numbered("neck"),
         "head": get("Head")}
    for side, s in SIDES:
        for role, name in (("shoulder", "Shoulder"), ("arm", "Arm"), ("forearm", "ForeArm"),
                           ("hand", "Hand"), ("upleg", "UpLeg"), ("leg", "Leg"),
                           ("foot", "Foot"), ("toe", "ToeBase")):
            j[f"{role}.{s}"] = get(side + name)
        for finger in ("Thumb", "Index", "Middle", "Ring", "Pinky"):
            chain = [get(f"{side}Hand{finger}{i}") for i in (1, 2, 3)]
            j[f"{finger.lower()}.{s}"] = chain if all(chain) else None

    required = ["hips", "head"] + [f"{r}.{s}" for _, s in SIDES
                                   for r in ("arm", "forearm", "hand", "upleg", "leg", "foot")]
    missing = [r for r in required if not j[r]]
    if not j["spine"]:
        missing.append("spine")
    if not j["neck"]:
        missing.append("neck")
    if missing:
        raise SkeletonError(
            "'%s' is missing humanoid bones: %s. The IK rig needs the named Siege character "
            "skeleton: import the group with 'Use real bone names' and 'Merge into single "
            "armature' on." % (arm.name, ", ".join(missing)))
    return j


def _bind_pose(arm, recapture=False):
    """Armature-space matrix of every bone in the character's bind look.

    Taken from the POSE, not the rest bones: the importer poses a few shared bones per
    character (eyes, mouth), and that look is what the rig must reproduce at rest.

    Captured once and stored on the armature. It cannot be re-read later: after a bake or a
    clip import the pose channels hold whatever frame was evaluated last, even with the action
    unlinked, and a rig rebuilt on that would bake the frame in as its rest pose.
    """
    clear_binding(arm)
    stored = arm.get(PROP_BIND)
    if stored is not None and not recapture and set(stored.keys()) == set(arm.pose.bones.keys()):
        return {name: Matrix([list(vals)[i * 4:i * 4 + 4] for i in range(4)])
                for name, vals in stored.items()}

    anim = arm.animation_data
    action = anim.action if anim is not None else None
    if action is not None:
        anim.action = None
    bpy.context.view_layer.update()
    bind = {pb.name: pb.matrix.copy() for pb in arm.pose.bones}
    if action is not None:
        anim.action = action
    arm[PROP_BIND] = {name: [v for row in m for v in row] for name, m in bind.items()}
    return bind


def _canonical_rotation(pos, j):
    """Armature space -> canonical space (left = +X, back = +Y, up = +Z) as a 3x3.

    Snapped to the nearest axis-aligned rotation when the skeleton is within 20 degrees of
    one, so a slightly leaning bind pose does not tilt the whole rig.
    """
    up = (pos[j["head"]] - pos[j["hips"]]).normalized()
    left = pos[j["upleg.L"]] - pos[j["upleg.R"]]
    left = (left - left.project(up)).normalized()
    basis = [left, up.cross(left), up]

    snapped = []
    for v in basis:
        axis = max(range(3), key=lambda i: abs(v[i]))
        if abs(v[axis]) < math.cos(math.radians(20)):
            break
        s = Vector((0.0, 0.0, 0.0))
        s[axis] = math.copysign(1.0, v[axis])
        snapped.append(s)
    if len(snapped) == 3 and len({max(range(3), key=lambda i: abs(v[i])) for v in snapped}) == 3:
        basis = snapped
    return Matrix(basis)


def _deformed_meshes(arm):
    return [o for o in bpy.data.objects
            if o.type == 'MESH' and any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers)]


def _mesh_points(arm, to_canonical):
    """Every vertex of the meshes the armature deforms, in canonical space (N x 3 numpy)."""
    import numpy as np
    chunks = []
    to_can = to_canonical.to_4x4() @ arm.matrix_world.inverted()
    for o in _deformed_meshes(arm):
        n = len(o.data.vertices)
        if not n:
            continue
        co = np.empty(n * 3, dtype=np.float64)
        o.data.vertices.foreach_get("co", co)
        m = np.array(to_can @ o.matrix_world)
        chunks.append(co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3])
    return np.concatenate(chunks) if chunks else np.zeros((0, 3))


# ---------------------------------------------------------------------------
# 2. Fit Rigify's human metarig to the skeleton
# ---------------------------------------------------------------------------

def _resample(points, n):
    """Exactly n points along a joint chain: split the longest gap, or drop evenly."""
    pts = list(points)
    while len(pts) < n:
        i = max(range(len(pts) - 1), key=lambda k: (pts[k + 1] - pts[k]).length)
        pts.insert(i + 1, (pts[i] + pts[i + 1]) / 2)
    if len(pts) > n:
        pts = [pts[round(i * (len(pts) - 1) / (n - 1))] for i in range(n)]
    return pts


def _ensure_bend(a, b, c, direction, min_offset, force):
    """Move the middle joint so the chain bends toward `direction`.

    Rigify derives the IK bend plane and pole side from the rest bend. A dead-straight knee
    has none (the pole flips at random), so a knee is pushed forward to at least `min_offset`
    (force=True). An elbow is only touched when the arm is nearly straight. The Siege joint
    itself is not moved; the offset bones absorb the few millimetres.
    """
    ac = c - a
    if ac.length_squared < 1e-12:
        return b, 0.0
    t = (b - a).dot(ac) / ac.length_squared
    perp = b - (a + ac * t)
    along = perp.dot(direction)
    if force:
        if along >= min_offset:
            return b, 0.0
        delta = min_offset - along
    else:
        if perp.length >= min_offset:
            return b, 0.0
        delta = min_offset - max(along, 0.0)
    return b + direction * delta, delta


def _head_top(pts, head, s):
    import numpy as np
    height = 0.2 * s
    if len(pts):
        dist = np.hypot(pts[:, 0] - head.x, pts[:, 1] - head.y)
        above = pts[(dist < 0.12 * s) & (pts[:, 2] > head.z), 2]
        if len(above) > 20:
            height = min(max(float(np.percentile(above, 99.5)) - head.z, 0.10 * s), 0.35 * s)
    return head + UP * height


def _foot_marks(pts, ankle, ball, s):
    """(toe tip, heel centre on the ground, heel width) measured from the foot's vertices."""
    import numpy as np
    fwd = Vector((ball.x - ankle.x, ball.y - ankle.y, 0.0))
    fwd = fwd.normalized() if fwd.length > 1e-4 else FORWARD.copy()
    lat = UP.cross(fwd)
    foot_len = (ball - ankle).dot(fwd)

    tip = ball + fwd * 0.07 * s
    heel = Vector((ankle.x, ankle.y, 0.0)) - fwd * 0.05 * s
    heel.z = min(ball.z, ankle.z) - 0.03 * s
    width = 0.08 * s

    if len(pts):
        rel = pts - np.array(ankle)
        along = rel @ np.array(fwd)
        across = rel @ np.array(lat)
        sel = ((pts[:, 2] < ankle.z + 0.02 * s)
               & (np.abs(across - (ball - ankle).dot(lat)) < 0.07 * s)
               & (along > -0.2 * s) & (along < foot_len + 0.2 * s))
        if sel.sum() > 50:
            tip_along = float(np.percentile(along[sel], 99.5))
            if tip_along > foot_len + 0.02 * s:
                tip = ball + fwd * (tip_along - foot_len)
                tip.z = ball.z
            ground = float(np.percentile(pts[sel, 2], 0.5))
            heel_along = float(np.percentile(along[sel], 0.5))
            heel = ankle + fwd * min(heel_along, -0.02 * s)
            heel.z = ground
            lo, hi = np.percentile(across[sel], [2, 98])
            width = min(max(float(hi - lo), 0.05 * s), 0.16 * s)
    return tip, heel, width, lat


def _hand_frame(wrist, middle, index, pinky):
    """Orthonormal hand frame (columns: toward fingers, pinky->index, palm normal)."""
    along, across = middle - wrist, index - pinky
    if along.length < 1e-6 or across.length < 1e-6:
        return None
    along.normalize()
    across = across - across.project(along)
    if across.length < 1e-6:
        return None
    across.normalize()
    return Matrix((along, across, along.cross(across))).transposed()


def _fit_metarig(meta, arm, j, P, pts, s):
    """Snap the template metarig onto the Siege joints (edit mode, canonical coordinates).

    Returns notes for the report. Template bones with no Siege counterpart (face, breasts,
    pelvis, missing fingers) are deleted; rolls are carried over from the template so each
    rig type keeps the axis conventions it was authored with.
    """
    from rigify.utils.bones import align_bone_x_axis  # type: ignore

    ebs = meta.data.edit_bones
    tmpl = {b.name: (b.head.copy(), b.tail.copy(), b.x_axis.copy()) for b in ebs}
    placed, notes = set(), []

    def place(name, head, tail):
        if name in ebs:
            ebs[name].head = head
            ebs[name].tail = tail
            placed.add(name)

    # Torso: Hips, Spine.., Neck -> the template's four torso bones.
    torso = _resample([P(n) for n in [j["hips"], *j["spine"], j["neck"][0]]], 5)
    for i, name in enumerate(("spine", "spine.001", "spine.002", "spine.003")):
        place(name, torso[i], torso[i + 1])

    neck, head = P(j["neck"][0]), P(j["head"])
    mid = P(j["neck"][-1]) if len(j["neck"]) > 1 else (neck + head) / 2
    if (mid - neck).length < 0.01 * s or (head - mid).length < 0.01 * s:
        mid = (neck + head) / 2
    place("spine.004", neck, mid)
    place("spine.005", mid, head)
    place("spine.006", head, _head_top(pts, head, s))

    frames = {}
    for side, sx in SIDES:
        # Legs
        hip, ankle = P(j[f"upleg.{sx}"]), P(j[f"foot.{sx}"])
        knee, nudge = _ensure_bend(hip, P(j[f"leg.{sx}"]), ankle, FORWARD, 0.008 * s, True)
        if nudge:
            notes.append("knee.%s nudged %.1f mm forward for a stable IK pole" % (sx, nudge * 1000))
        if j[f"toe.{sx}"]:
            ball = P(j[f"toe.{sx}"])
        else:
            ball = ankle + Vector((FORWARD.x, FORWARD.y, 0.0)) * 0.12 * s
            ball.z = ankle.z * 0.35
        tip, heel, width, lat = _foot_marks(pts, ankle, ball, s)
        place(f"thigh.{sx}", hip, knee)
        place(f"shin.{sx}", knee, ankle)
        place(f"foot.{sx}", ankle, ball)
        place(f"toe.{sx}", ball, tip)
        heel_name = f"heel.02.{sx}"
        if heel_name in tmpl:
            t_head, t_tail, _ = tmpl[heel_name]
            # Keep the template's head/tail order across the foot (it differs per side).
            direction = lat if (t_tail - t_head).x * UP.cross(FORWARD).x >= 0 else -lat
            place(heel_name, heel - direction * width / 2, heel + direction * width / 2)

        # Arms
        arm_j, wrist = P(j[f"arm.{sx}"]), P(j[f"hand.{sx}"])
        elbow, nudge = _ensure_bend(arm_j, P(j[f"forearm.{sx}"]), wrist, BACKWARD, 0.008 * s, False)
        if nudge:
            notes.append("elbow.%s nudged %.1f mm back for a stable IK pole" % (sx, nudge * 1000))
        if j[f"shoulder.{sx}"]:
            shoulder = P(j[f"shoulder.{sx}"])
        else:
            shoulder = arm_j.lerp(Vector((neck.x, neck.y, arm_j.z)), 0.85)
        place(f"shoulder.{sx}", shoulder, arm_j)
        place(f"upper_arm.{sx}", arm_j, elbow)
        place(f"forearm.{sx}", elbow, wrist)

        chains = {f: [P(n) for n in j[f"{f.lower()}.{sx}"]] if j[f"{f.lower()}.{sx}"] else None
                  for f in ("Thumb", "Index", "Middle", "Ring", "Pinky")}
        knuckle = chains["Middle"][0] if chains["Middle"] else wrist + (wrist - elbow).normalized() * 0.08 * s
        place(f"hand.{sx}", wrist, wrist.lerp(knuckle, 0.6))

        hand_bone = j[f"hand.{sx}"]
        for finger, prefix, palm in FINGERS:
            chain = chains[finger]
            if chain is None:
                continue
            # A Siege finger hangs off an (unnamed) metacarpal under the hand; use it as the palm.
            meta_bone = arm.data.bones[j[f"{finger.lower()}.{sx}"][0]].parent
            palm_head = wrist
            if meta_bone is not None and meta_bone.name != hand_bone:
                candidate = P(meta_bone.name)
                if (candidate - chain[0]).length > 0.01 * s:
                    palm_head = candidate
            place(f"{palm}.{sx}", palm_head, chain[0])
            tip = chain[2] + (chain[2] - chain[1]) * 0.85
            for i, (a, b) in enumerate(zip(chain, chain[1:] + [tip])):
                place(f"{prefix}.0{i + 1}.{sx}", a, b)
        if chains["Thumb"]:
            c = chains["Thumb"]
            tip = c[2] + (c[2] - c[1]) * 0.85
            for i, (a, b) in enumerate(zip(c, c[1:] + [tip])):
                place(f"thumb.0{i + 1}.{sx}", a, b)

        if all(chains[f] for f in ("Middle", "Index", "Pinky")) and all(
                n in tmpl for n in (f"hand.{sx}", f"f_middle.01.{sx}", f"f_index.01.{sx}", f"f_pinky.01.{sx}")):
            ft = _hand_frame(tmpl[f"hand.{sx}"][0], tmpl[f"f_middle.01.{sx}"][0],
                             tmpl[f"f_index.01.{sx}"][0], tmpl[f"f_pinky.01.{sx}"][0])
            ff = _hand_frame(wrist, chains["Middle"][0], chains["Index"][0], chains["Pinky"][0])
            if ft is not None and ff is not None:
                frames[sx] = ff @ ft.transposed()

    for eb in [e for e in ebs if e.name not in placed]:
        ebs.remove(eb)

    # Rolls: template X axis, re-expressed in the Siege hand frame for hand-side bones so a
    # palms-in bind pose still spreads/curls about the right axes. Rigify's 'automatic' limbs
    # and fingers re-derive X from the bend, and fall back to this X when a chain is straight.
    for name in placed:
        if name not in ebs:
            continue
        x = tmpl[name][2]
        sx = name[-1] if name[-2:] in (".L", ".R") else None
        if sx in frames and name.startswith(("hand.", "palm.", "f_", "thumb.")):
            x = frames[sx] @ x
        eb = ebs[name]
        if abs(x.normalized().dot(eb.y_axis)) < 0.98:
            align_bone_x_axis(meta, name, x)
    return notes


def _fix_palm_types(meta):
    """super_palm lives on palm.01 and needs >= 2 palms; repair after a missing finger."""
    for _, sx in SIDES:
        palms = sorted(pb.name for pb in meta.pose.bones
                       if pb.name.startswith("palm.") and pb.name.endswith("." + sx))
        for pb_name in palms:
            meta.pose.bones[pb_name].rigify_type = ""
        if len(palms) >= 2:
            meta.pose.bones[palms[0]].rigify_type = "limbs.super_palm"


def _build_metarig(arm, j, bind, R, pts, s):
    from rigify.metarigs import human  # type: ignore

    data = bpy.data.armatures.new("META-" + arm.name)
    meta = bpy.data.objects.new("META-" + arm.name, data)
    for coll in arm.users_collection:
        coll.objects.link(meta)
    meta.matrix_world = arm.matrix_world @ R.inverted().to_4x4()
    meta[PROP_SOURCE] = arm
    _activate(meta)
    human.create(meta)

    R4 = R.to_4x4()
    _set_mode('EDIT')
    notes = _fit_metarig(meta, arm, j, lambda n: (R4 @ bind[n]).translation, pts, s)
    _set_mode('OBJECT')
    _fix_palm_types(meta)

    for bc in list(meta.data.collections_all):
        if bc.name.startswith("Face") and not bc.bones and not bc.children:
            meta.data.collections.remove(bc)
    data.rigify_rig_basename = "RIG-" + arm.name
    return meta, notes


# ---------------------------------------------------------------------------
# 3. Generate + 4. bind the Siege skeleton to the rig
# ---------------------------------------------------------------------------

_OWN_GENERATE = False   # True while build/regenerate run Rigify; the finalize hook then stays out

FINALIZE_TEXT = "prism_rig_finalize.py"
FINALIZE_SOURCE = '''\
# Written by r6s-nr-tools (operators/rig_character.py). Rigify runs this after every generate,
# including its own "Re-Generate Rig" button. A regenerate wipes every bone Rigify did not create
# itself: the Siege extra controls and the PRISM- offset bones. This puts them back and re-binds
# the Siege skeleton.
import sys
for mod in list(sys.modules.values()):
    hook = getattr(mod, "__dict__", {}).get("prism_rig_finalize_hook")
    if hook is not None:
        hook()
        break
else:
    print("prism rig: r6s-nr-tools is not loaded; the Siege skeleton was not re-bound")
'''

# Extra controls, by kind: (bone collection, bone color, visible by default). Colors avoid the
# ones Rigify uses (red IK, green FK, blue tweak, yellow special, orange extra, purple root).
CONTROL_KINDS = {
    "extra": ("Siege Extras", 'THEME07', True),    # teal
    "face": ("Siege Face", 'THEME05', True),       # pink
    "twist": ("Siege Twist", 'THEME13', False),    # grey; Rigify already twists them
}
PROP_CONTROL = "prism_control_for"   # on an extra control bone: the Siege bone it drives


def _install_finalize_hook(meta):
    text = bpy.data.texts.get(FINALIZE_TEXT) or bpy.data.texts.new(FINALIZE_TEXT)
    text.from_string(FINALIZE_SOURCE)
    meta.data.rigify_finalize_script = text


def prism_rig_finalize_hook():
    """Run by the metarig's finalize script after Rigify generates (see FINALIZE_SOURCE)."""
    if _OWN_GENERATE:
        return   # build/regenerate bind right after generating, and verify
    rig = bpy.context.active_object
    arm = rig.get(PROP_SOURCE) if rig is not None else None
    meta = arm.get(PROP_METARIG) if arm is not None else None
    if meta is None or meta.data.rigify_target_rig != rig:
        return
    result = _post_generate(arm, meta, rig)
    _purge_orphan_armatures()
    print("prism rig: re-bound '%s' after a Rigify regenerate (%d constraints, %d controls)"
          % (arm.name, result["constraints"], len(result["controls"])))


def _generate(meta):
    global _OWN_GENERATE
    _activate(meta)
    before = set(bpy.data.armatures)
    _OWN_GENERATE = True
    try:
        result = bpy.ops.pose.rigify_generate()
    finally:
        _OWN_GENERATE = False
    # Drop the temporary metarig copy Rigify leaves behind, so repeated regenerations don't
    # pile up .001/.002 armatures.
    for data in set(bpy.data.armatures) - before:
        if data.users == 0:
            bpy.data.armatures.remove(data)
    rig = meta.data.rigify_target_rig
    if 'FINISHED' not in result or rig is None:
        raise SkeletonError("Rigify generation failed on '%s'; see the Info log for the "
                            "metarig error." % meta.name)
    # Rigify builds bones in metarig-local space but keeps the rig object's own transform.
    rig.matrix_world = meta.matrix_world
    return rig


def _weighted_points(arm, to_canonical):
    """{bone: canonical positions of the vertices it holds with weight >= 0.25} (numpy N x 3).

    A bone appears only if some mesh actually has weight on it, so the keys double as the set
    of deforming bones.
    """
    import numpy as np
    to_can = to_canonical.to_4x4() @ arm.matrix_world.inverted()
    found = {}
    for o in _deformed_meshes(arm):
        n = len(o.data.vertices)
        if not n:
            continue
        co = np.empty(n * 3, dtype=np.float64)
        o.data.vertices.foreach_get("co", co)
        m = np.array(to_can @ o.matrix_world)
        co = co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
        names = [g.name for g in o.vertex_groups]
        per_group = {}
        for v in o.data.vertices:
            for g in v.groups:
                if g.weight > 0.001:
                    per_group.setdefault(g.group, []).append((v.index, g.weight))
        for gi, members in per_group.items():
            if names[gi] not in arm.data.bones:
                continue
            strong = [i for i, w in members if w >= 0.25] or [i for i, _ in members]
            found.setdefault(names[gi], []).append(co[strong])
    return {name: np.concatenate(chunks) for name, chunks in found.items()}


def _plan_binding(arm, rig, j, can, weighted):
    """Decide what drives every Siege bone. Returns (driver, controls).

    driver:   Siege bone -> the rig bone it follows.
    controls: Siege bone -> (control name, parent rig bone, kind), in parent-first order.

    Named humanoid bones follow their Rigify counterpart: ORG- for the segmented limb bones
    (full rotation, like the game's main limb joints), DEF- elsewhere so tweak controls reach
    the mesh.

    Every other bone that DEFORMS gets its own FK control: the robot head's yaw and pitch
    joints, cables, pouches, cloth, face bones, correctives. A control sits at the Siege
    joint in the Siege bind frame (so its local axes are the game's) and is parented the way
    the Siege bone is: under its nearest controlled ancestor, else under whatever its Siege
    parent follows. Left alone it moves exactly as the bone always did; posed, it adds motion.
    Nothing is re-parented by guesswork. The one refinement: a helper on a limb segment
    attaches to the Rigify DEF twist segment covering it, so the roll bones still get Rigify's
    twist distribution under their controls.

    A bone that deforms nothing follows what its parent follows (no control).
    """
    rb = rig.data.bones

    def pick(*names):
        return next((n for n in names if n in rb), None)

    def deform(base):
        return pick("DEF-" + base, "ORG-" + base)

    direct = {}

    def put(bone, target):
        if bone and target:
            direct[bone] = target

    b = arm.data.bones[j["hips"]].parent
    while b is not None:
        direct[b.name] = "root"
        b = b.parent

    torso = ("spine", "spine.001", "spine.002", "spine.003")
    for name in [j["hips"], *j["spine"]]:
        best = min(torso, key=lambda t: (rb["ORG-" + t].head_local - can(name)).length)
        put(name, deform(best))
    for i, name in enumerate(j["neck"]):
        put(name, deform("spine.004" if i == 0 else "spine.005"))
    put(j["head"], deform("spine.006"))

    segments = {}
    for _, sx in SIDES:
        put(j[f"shoulder.{sx}"], deform(f"shoulder.{sx}"))
        put(j[f"arm.{sx}"], pick(f"ORG-upper_arm.{sx}"))
        put(j[f"forearm.{sx}"], pick(f"ORG-forearm.{sx}"))
        put(j[f"hand.{sx}"], deform(f"hand.{sx}"))
        put(j[f"upleg.{sx}"], pick(f"ORG-thigh.{sx}"))
        put(j[f"leg.{sx}"], pick(f"ORG-shin.{sx}"))
        put(j[f"foot.{sx}"], deform(f"foot.{sx}"))
        put(j[f"toe.{sx}"], deform(f"toe.{sx}"))
        for finger, prefix, palm in FINGERS:
            chain = j[f"{finger.lower()}.{sx}"]
            if not chain:
                continue
            for i, name in enumerate(chain):
                put(name, deform(f"{prefix}.0{i + 1}.{sx}"))
            parent = arm.data.bones[chain[0]].parent
            if parent is not None and parent.name != j[f"hand.{sx}"] and parent.name not in direct:
                put(parent.name, deform(f"{palm}.{sx}"))
        if j[f"thumb.{sx}"]:
            for i, name in enumerate(j[f"thumb.{sx}"]):
                put(name, deform(f"thumb.0{i + 1}.{sx}"))
        for base in LIMB_SEGMENTED:
            org = f"ORG-{base}.{sx}"
            if org in rb:
                segs = [f"DEF-{base}.{sx}"] + [f"DEF-{base}.{sx}.{k:03d}" for k in range(1, 10)]
                segments[org] = [n for n in segs if n in rb]

    # Manual override: a `prism_rig_driver` string property on a Siege bone names the rig bone
    # it (or, for a deforming extra, its control) attaches to. The Bone properties tab edits the
    # pose bone's properties in Pose mode and the bone's otherwise, so read both.
    overrides = {}
    for pb in arm.pose.bones:
        value = pb.get(PROP_DRIVER_OVERRIDE) or pb.bone.get(PROP_DRIVER_OVERRIDE)
        if isinstance(value, str) and value in rb:
            overrides[pb.name] = value

    def on_segment(d, p):
        if not segments.get(d):
            return d
        a, ab = rb[d].head_local, rb[d].tail_local - rb[d].head_local
        t = (p - a).dot(ab) / ab.length_squared
        off = (p - (a + ab * min(max(t, 0.0), 1.0))).length
        if off > max(0.03, 0.2 * ab.length):
            return d
        segs = segments[d]
        return segs[min(int(max(t, 0.0) * len(segs)), len(segs) - 1)]

    driver, controls = {}, {}

    def walk(bone, under_head):
        name = bone.name
        parent_d = driver[bone.parent.name] if bone.parent is not None else "root"
        if name in direct:
            d = overrides.get(name, direct[name])
        else:
            attach = overrides.get(name) or on_segment(parent_d, can(name))
            if name in weighted:
                if under_head:
                    kind = "face"
                elif attach != parent_d and name not in overrides:
                    kind = "twist"
                else:
                    kind = "extra"
                ctrl = name if name not in rb else "CTRL-" + name
                controls[name] = (ctrl, attach, kind)
                d = ctrl
            else:
                d = attach
        driver[name] = d
        for child in bone.children:
            walk(child, under_head or name == j["head"])

    for bone in arm.data.bones:
        if bone.parent is None:
            walk(bone, False)
    return driver, controls


def clear_binding(arm):
    for pb in arm.pose.bones:
        for con in [c for c in pb.constraints if c.name.startswith(RIG_CONSTRAINT)]:
            pb.constraints.remove(con)


def _constrained(arm, driver):
    """Bones that need a constraint: roots, and bones whose driver differs from their parent's.

    A bone following the same rig bone as its Siege parent already gets exactly that motion
    through the hierarchy (parent_bind^-1 @ child_bind is constant in both cases), so it is
    left unconstrained: sockets and other non-deforming leaves stay hand-poseable on the
    skeleton.
    """
    return [b.name for b in arm.data.bones
            if b.parent is None or driver[b.name] != driver[b.parent.name]]


def _control_size(points, head, s):
    """Widget radius: 40% of the median distance of the bone's vertices from its joint.

    Scales with the part it moves (a head yaw joint vs a cable link) but stays small enough
    that dozens of them around the neck and shoulders remain clickable.
    """
    import numpy as np
    if points is None or not len(points):
        return 0.02 * s
    dist = np.linalg.norm(points - np.array(head), axis=1)
    return float(min(max(float(np.percentile(dist, 50)) * 0.4, 0.012 * s), 0.09 * s))


def _sphere_widget(rig, meta, bone_name):
    """A Rigify-style sphere widget, kept in the metarig's widget collection."""
    from rigify.utils.widgets import WGT_PREFIX, GeometryData  # type: ignore
    from rigify.utils.widgets_basic import create_sphere_widget  # type: ignore

    name = (WGT_PREFIX + rig.name + "_" + bone_name)[:63]
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != 'MESH':
        geom = GeometryData()
        create_sphere_widget.__wrapped__(geom, radius=0.5)
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(geom.verts, geom.edges, geom.faces)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
    coll = getattr(meta.data, "rigify_widgets_collection", None)
    if coll is not None and obj.name not in coll.objects:
        coll.objects.link(obj)
    return obj


def _control_collection(rig, kind):
    name, _, visible = CONTROL_KINDS[kind]
    coll = rig.data.collections_all.get(name)
    if coll is None:
        coll = rig.data.collections.new(name)
        coll.is_visible = visible
    if hasattr(coll, "rigify_ui_row"):
        # One row under Rigify's own buttons, after a blank separator row.
        rows = [c.rigify_ui_row for c in rig.data.collections_all
                if not c.name.startswith("Siege ") and c.rigify_ui_row > 0]
        coll.rigify_ui_row = (max(rows) if rows else 0) + 2
    return coll


def _bind(arm, rig, meta, R, bind, plan, weights, s):
    driver, controls = plan
    R4 = R.to_4x4()
    bound = _constrained(arm, driver)
    control_names = {c for c, _, _ in controls.values()}

    _activate(rig)
    _set_mode('EDIT')
    ebs = rig.data.edit_bones
    for eb in [e for e in ebs if e.name.startswith(BIND_PREFIX) or e.name in control_names]:
        ebs.remove(eb)

    for siege, (ctrl, parent, kind) in controls.items():   # parent-first order
        eb = ebs.new(ctrl)
        eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)
        eb.matrix = (R4 @ bind[siege]).normalized()
        eb.length = 2 * _control_size(weights.get(siege), eb.head, s)   # sphere radius = length / 2
        eb.parent = ebs[parent]
        eb.use_connect = False
        eb.use_deform = False
        _control_collection(rig, kind).assign(eb)

    coll = rig.data.collections_all.get(BIND_COLLECTION) or rig.data.collections.new(BIND_COLLECTION)
    for name in bound:
        length = arm.data.bones[name].length or 0.05
        eb = ebs.new(BIND_PREFIX + name)
        eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, length, 0.0)
        eb.matrix = (R4 @ bind[name]).normalized()
        eb.parent = ebs[driver[name]]
        eb.use_connect = False
        eb.use_deform = False
        coll.assign(eb)
    coll.is_visible = False
    _set_mode('OBJECT')

    for siege, (ctrl, parent, kind) in controls.items():
        pb = rig.pose.bones[ctrl]
        pb.rotation_mode = 'XYZ'     # mechanical parts turn about one axis; Euler keeps one curve
        pb.custom_shape = _sphere_widget(rig, meta, ctrl)
        pb.color.palette = pb.bone.color.palette = CONTROL_KINDS[kind][1]
        pb.bone[PROP_CONTROL] = siege

    # An edit bone stores head/tail/roll, and roll loses precision for bones pointing along
    # the armature's -Y. Siege head bones do, once rotated into the canonical frame (seen up to
    # 0.06 deg). Put the residual into the offset bone's pose basis and lock it: exact again.
    for name in bound:
        pb = rig.pose.bones[BIND_PREFIX + name]
        pb.rotation_mode = 'QUATERNION'
        pb.matrix_basis = pb.bone.matrix_local.inverted() @ (R4 @ bind[name]).normalized()
        pb.lock_location = pb.lock_rotation = pb.lock_scale = (True, True, True)
        pb.lock_rotation_w = True

    clear_binding(arm)
    for name in bound:
        con = arm.pose.bones[name].constraints.new('COPY_TRANSFORMS')
        con.name = RIG_CONSTRAINT
        con.target = rig
        con.subtarget = BIND_PREFIX + name

    rig[PROP_SOURCE] = arm
    rig["prism_driver_map"] = json.dumps(driver, indent=0, sort_keys=True)
    rig["prism_controls"] = json.dumps({k: list(v) for k, v in controls.items()}, indent=0)
    arm[PROP_RIG] = rig
    return len(bound)


def verify_binding(arm, rig, R, bind, j):
    """Rest drift (should be ~0) plus live checks that the mesh bones actually follow."""
    ctx = bpy.context
    ctx.view_layer.update()
    report = {}

    def rot_err(a, b):
        ang = a.to_quaternion().rotation_difference(b.to_quaternion()).angle
        return math.degrees(min(ang, 2 * math.pi - ang))

    report["rest_max_offset_mm"] = round(1000 * max(
        (pb.matrix.translation - bind[pb.name].translation).length for pb in arm.pose.bones), 4)
    report["rest_max_rotation_deg"] = round(max(
        rot_err(pb.matrix, bind[pb.name]) for pb in arm.pose.bones), 4)

    driver = json.loads(rig["prism_driver_map"])
    controls = json.loads(rig.get("prism_controls", "{}"))
    R4, R4_inv = R.to_4x4(), R.inverted().to_4x4()
    follow = {"mm": 0.0, "deg": 0.0}

    def check_follow():
        # Every Siege bone, constrained or inherited, must sit where its driver puts it.
        for pb in arm.pose.bones:
            d = rig.pose.bones[driver[pb.name]]
            want = R4_inv @ d.matrix @ d.bone.matrix_local.inverted() @ R4 @ bind[pb.name]
            follow["mm"] = max(follow["mm"], 1000 * (pb.matrix.translation - want.translation).length)
            follow["deg"] = max(follow["deg"], rot_err(pb.matrix, want))

    def probe(ctrl, bones, move=None, rotate_x=None):
        pb = rig.pose.bones.get(ctrl)
        if pb is None:
            return None
        before = {n: arm.pose.bones[n].matrix.copy() for n in bones}
        saved = pb.matrix_basis.copy()
        if rotate_x is not None:
            pb.matrix_basis = pb.matrix_basis @ Matrix.Rotation(math.radians(rotate_x), 4, 'X')
        else:
            pb.matrix = Matrix.Translation(move) @ pb.matrix
        ctx.view_layer.update()
        check_follow()
        moved = {n: {"moved": [round(v, 3) for v in R @ (arm.pose.bones[n].matrix.translation
                                                        - before[n].translation)],
                     "turned_deg": round(rot_err(arm.pose.bones[n].matrix, before[n]), 2)}
                 for n in bones}
        pb.matrix_basis = saved
        ctx.view_layer.update()
        return moved

    report["hand_ik.L +15cm up"] = probe("hand_ik.L", [j["hand.L"], j["forearm.L"]],
                                         move=Vector((0, 0, 0.15)))
    report["foot_ik.L +20cm up (knee should go -Y)"] = probe(
        "foot_ik.L", [j["foot.L"], j["leg.L"]], move=Vector((0, 0, 0.20)))
    if controls:
        # The extra control with the most descendants among the controls, e.g. a head yaw joint.
        def depth_below(name):
            return sum(1 for other in controls if other != name
                       and name in _ancestors(arm, other))
        top = max(controls, key=depth_below)
        below = [n for n in controls if n != top and top in _ancestors(arm, n)][:2]
        report["extra control %s local X 30deg" % controls[top][0]] = probe(
            controls[top][0], [top, *below], rotate_x=30)
    report["posed_max_follow_error"] = {k: round(v, 4) for k, v in follow.items()}
    return report


def _ancestors(arm, name):
    out, b = [], arm.data.bones[name].parent
    while b is not None:
        out.append(b.name)
        b = b.parent
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def remove_rig(arm):
    """Delete the rig, metarig, their widgets/UI/finalize scripts and the skeleton constraints."""
    clear_binding(arm)
    for key in (PROP_RIG, PROP_METARIG):
        obj = arm.get(key)
        if obj is None:
            continue
        data = obj.data
        texts = []
        if key == PROP_METARIG:
            wgts = getattr(data, "rigify_widgets_collection", None)
            if wgts is not None:
                for o in list(wgts.all_objects):
                    bpy.data.objects.remove(o, do_unlink=True)
                bpy.data.collections.remove(wgts)
            texts = [getattr(data, "rigify_rig_ui", None), getattr(data, "rigify_finalize_script", None)]
        bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            bpy.data.armatures.remove(data)
        _purge_orphan_armatures()
        # Texts carry a fake user, so their user count never reaches zero; drop them unless a
        # remaining metarig (another rigged character) still points at them.
        for text in texts:
            if text is not None and not any(
                    text in (getattr(a, "rigify_rig_ui", None), getattr(a, "rigify_finalize_script", None))
                    for a in bpy.data.armatures):
                bpy.data.texts.remove(text)
        del arm[key]


def _purge_orphan_armatures():
    """Rigify generates from a temporary metarig copy and leaves its armature data with no users."""
    for data in [a for a in bpy.data.armatures if a.users == 0]:
        bpy.data.armatures.remove(data)


def _scale(j, bind, R4):
    head_z = (R4 @ bind[j["head"]]).translation.z
    foot_z = min((R4 @ bind[j[f"foot.{sx}"]]).translation.z for _, sx in SIDES)
    return max(head_z - foot_z, 1e-3) / 1.5   # 1.0 for a ~1.5 m ankle-to-head Siege operator


def _post_generate(arm, meta, rig):
    """Everything after Rigify: plan, controls, offset bones, constraints."""
    j = _resolve_joints(arm)
    bind = _bind_pose(arm)
    R = _canonical_rotation({n: m.translation for n, m in bind.items()}, j)
    R4 = R.to_4x4()
    rig.matrix_world = meta.matrix_world
    weights = _weighted_points(arm, R)
    plan = _plan_binding(arm, rig, j, lambda n: (R4 @ bind[n]).translation, weights)
    constrained = _bind(arm, rig, meta, R, bind, plan, weights, _scale(j, bind, R4))
    meta.hide_set(True)
    arm.hide_set(True)
    return {"j": j, "bind": bind, "R": R, "driver": plan[0], "controls": plan[1],
            "constraints": constrained}


def _rebind(arm, meta, verify, stash=None):
    j = _resolve_joints(arm)
    bind = _bind_pose(arm)
    R = _canonical_rotation({n: m.translation for n, m in bind.items()}, j)
    meta.matrix_world = arm.matrix_world @ R.inverted().to_4x4()
    _install_finalize_hook(meta)
    if stash is None:
        stash = _stash_action(meta.data.rigify_target_rig)
    rig = _generate(meta)
    result = _post_generate(arm, meta, rig)
    _activate(rig)
    kinds = {}
    for _, _, kind in result["controls"].values():
        kinds[kind] = kinds.get(kind, 0) + 1
    report = {"rig": rig.name, "metarig": meta.name, "bones_bound": len(result["driver"]),
              "constraints": result["constraints"], "controls": kinds,
              "rig_bones_used": len(set(result["driver"].values()))}
    if verify:
        report["verify"] = verify_binding(arm, rig, result["R"], result["bind"], result["j"])
    kept = _restore_action(rig, stash)   # after verification, which needs the rest pose
    if kept:
        report["kept_action"] = kept
    _set_mode('POSE')
    return report


def _stash_action(rig):
    """(action, slot) on a rig. Rigify unlinks the rig's action (and drops its NLA tracks) on
    every generate, and Build replaces the rig object, so both put it back afterwards."""
    anim = rig.animation_data if rig is not None else None
    action = anim.action if anim is not None else None
    return action, (getattr(anim, "action_slot", None) if action is not None else None)


def _restore_action(rig, stash):
    action, slot = stash
    if action is None:
        return None
    rig.animation_data_create()
    rig.animation_data.action = action
    if slot is not None and hasattr(rig.animation_data, "action_slot"):
        rig.animation_data.action_slot = slot
    return action.name


def build_rig(arm, verify=True, recapture_pose=False):
    """Metarig + Rigify rig + binding for a Siege armature. Returns a report dict."""
    j = _resolve_joints(arm)   # fail before touching the scene
    with bpy.context.temp_override(**_view3d_override(bpy.context)):
        _ensure_rigify()
        _activate(arm)
        stash = _stash_action(arm.get(PROP_RIG))
        remove_rig(arm)
        bind = _bind_pose(arm, recapture_pose)
        R = _canonical_rotation({n: m.translation for n, m in bind.items()}, j)
        R4 = R.to_4x4()
        pts = _mesh_points(arm, R)
        meta, notes = _build_metarig(arm, j, bind, R, pts, _scale(j, bind, R4))
        arm[PROP_METARIG] = meta
        report = _rebind(arm, meta, verify, stash)
        report["notes"] = notes
        return report


def regenerate_rig(arm, verify=True):
    """Re-run Rigify on the (possibly hand-edited) metarig and re-bind."""
    meta = arm.get(PROP_METARIG)
    if meta is None:
        raise SkeletonError("'%s' has no metarig yet; use Build IK Rig first." % arm.name)
    with bpy.context.temp_override(**_view3d_override(bpy.context)):
        _ensure_rigify()
        return _rebind(arm, meta, verify)


def set_rig_enabled(arm, enabled):
    for pb in arm.pose.bones:
        for con in pb.constraints:
            if con.name.startswith(RIG_CONSTRAINT):
                con.mute = not enabled


def rig_enabled(arm):
    return any(not c.mute for pb in arm.pose.bones for c in pb.constraints
               if c.name.startswith(RIG_CONSTRAINT))


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _format_report(report):
    controls = report.get("controls", {})
    parts = ["%s: %d Siege bones follow %d rig bones (%d constraints), %d extra controls%s" % (
        report["rig"], report["bones_bound"], report["rig_bones_used"], report["constraints"],
        sum(controls.values()),
        " (%s)" % ", ".join("%d %s" % (n, k) for k, n in sorted(controls.items())) if controls else "")]
    v = report.get("verify")
    if v:
        parts.append("rest drift %.3f mm / %.3f deg" % (v["rest_max_offset_mm"],
                                                        v["rest_max_rotation_deg"]))
    parts.extend(report.get("notes", ()))
    return "; ".join(parts)


class OBJECT_OT_PrismBuildIKRig(bpy.types.Operator):
    """Build a Rigify IK/FK control rig for the selected Siege character and drive its skeleton from it"""

    bl_idname = "object.prism_build_ik_rig"
    bl_label = "Build IK Rig (Rigify)"
    bl_options = {"REGISTER", "UNDO"}

    verify: BoolProperty(
        name="Verify binding",
        description="Measure rest-pose drift and test the IK controls after building",
        default=True,
    )
    recapture_pose: BoolProperty(
        name="Re-capture bind pose",
        description="Take the rig's rest pose from the skeleton as it stands now instead of the "
                    "pose stored by the first build. Only for a deliberately changed character "
                    "pose; never after baking or importing a clip",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return source_armature(context.active_object) is not None

    def execute(self, context):
        arm = source_armature(context.active_object)
        try:
            report = build_rig(arm, verify=self.verify, recapture_pose=self.recapture_pose)
        except SkeletonError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        print("[prism rig]", json.dumps(report, indent=1))
        self.report({"INFO"}, _format_report(report))
        return {"FINISHED"}


class OBJECT_OT_PrismRegenerateIKRig(bpy.types.Operator):
    """Re-generate the Rigify rig from its (edited) metarig and re-bind the Siege skeleton"""

    bl_idname = "object.prism_regenerate_ik_rig"
    bl_label = "Regenerate IK Rig"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        arm = source_armature(context.active_object)
        return arm is not None and arm.get(PROP_METARIG) is not None

    def execute(self, context):
        arm = source_armature(context.active_object)
        try:
            report = regenerate_rig(arm)
        except SkeletonError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, _format_report(report))
        return {"FINISHED"}


class OBJECT_OT_PrismToggleIKRig(bpy.types.Operator):
    """Mute/unmute the rig constraints, e.g. to play an imported .prismanim clip on the raw skeleton"""

    bl_idname = "object.prism_toggle_ik_rig"
    bl_label = "Toggle IK Rig Control"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        arm = source_armature(context.active_object)
        return arm is not None and arm.get(PROP_RIG) is not None

    def execute(self, context):
        arm = source_armature(context.active_object)
        enabled = not rig_enabled(arm)
        set_rig_enabled(arm, enabled)
        self.report({"INFO"}, "Rig control %s on '%s'" % ("enabled" if enabled else "muted", arm.name))
        return {"FINISHED"}


class OBJECT_OT_PrismBakeIKRig(bpy.types.Operator):
    """Bake the rig-driven motion onto the Siege skeleton as a plain action, then mute the rig"""

    bl_idname = "object.prism_bake_ik_rig"
    bl_label = "Bake IK Rig to Skeleton"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        arm = source_armature(context.active_object)
        return arm is not None and arm.get(PROP_RIG) is not None

    def execute(self, context):
        arm = source_armature(context.active_object)
        scene = context.scene
        set_rig_enabled(arm, True)
        with context.temp_override(**_view3d_override(context)):
            _activate(arm)
            _set_mode('POSE')
            for pb in arm.pose.bones:
                pb.select = True
            bpy.ops.nla.bake(frame_start=scene.frame_start, frame_end=scene.frame_end,
                             only_selected=True, visual_keying=True, clear_constraints=False,
                             use_current_action=False, bake_types={'POSE'})
            _set_mode('OBJECT')
        set_rig_enabled(arm, False)
        action = arm.animation_data.action if arm.animation_data else None
        self.report({"INFO"}, "Baked frames %d-%d to '%s'; rig control muted" % (
            scene.frame_start, scene.frame_end, action.name if action else "?"))
        return {"FINISHED"}


class OBJECT_OT_PrismRemoveIKRig(bpy.types.Operator):
    """Delete the generated rig and metarig and free the Siege skeleton"""

    bl_idname = "object.prism_remove_ik_rig"
    bl_label = "Remove IK Rig"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        arm = source_armature(context.active_object)
        return arm is not None and (arm.get(PROP_RIG) is not None or arm.get(PROP_METARIG) is not None)

    def execute(self, context):
        arm = source_armature(context.active_object)
        with context.temp_override(**_view3d_override(context)):
            remove_rig(arm)
            _activate(arm)
        self.report({"INFO"}, "Removed IK rig from '%s'" % arm.name)
        return {"FINISHED"}


classes = (OBJECT_OT_PrismBuildIKRig, OBJECT_OT_PrismRegenerateIKRig, OBJECT_OT_PrismToggleIKRig,
           OBJECT_OT_PrismBakeIKRig, OBJECT_OT_PrismRemoveIKRig)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
