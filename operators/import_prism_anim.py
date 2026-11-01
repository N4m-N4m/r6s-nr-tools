"""Import a Prism `.prismanim` clip onto an imported Siege armature.

A clip is a flat list of per-bone tracks keyed by CRC32(bone name) — the same key
`_skeleton.txt` and the `.weights` sidecars use. Binding is by CRC, but the bone that CRC
ends up on depends on how the character was imported, so `_resolve_pose_bones` tries three
conventions in order (see its docstring). Do NOT assume a single naming scheme here: that
assumption is what made this importer match zero bones on every build.

Clip values are LOCAL (parent-relative) transforms, matching the rest locals the armature was
built from. Blender's `pose_bone.location` / `.rotation_quaternion` are relative to the bone's
REST pose, not to its parent, so each frame is applied as

    matrix_basis = rest_local^-1 @ clip_local

which is exact, and — because a parent-relative transform is invariant under a global rotation
of the whole armature — stays correct regardless of the up-axis conversion the group importer
applied.
"""

import bpy  # type: ignore
from bpy.props import BoolProperty, FloatProperty, StringProperty  # type: ignore
from bpy_extras.io_utils import ImportHelper  # type: ignore
from mathutils import Quaternion, Vector  # type: ignore


def _normalise_crc(token):
    """
    Normalise a clip's bone key to the bare uppercase-hex CRC the group importer uses
    (`bone_<CRC>` names and the `prism_crc` custom property both drop the 0x prefix).
    A non-hex token (a very old ordinal-keyed export) is passed through unchanged.
    """
    t = token.strip()
    return t[2:].upper() if t.lower().startswith("0x") else t


def _parse_prismanim(path):
    """Return (header, {crc: {"rot": [(frame, Quaternion)], "loc": [(frame, Vector)]}})."""
    header, tracks, cur = {}, {}, None
    has_rot = has_loc = False

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("PRISMANIM"):
                header["version"] = line.split()[-1]
                continue

            if line.startswith("bone "):
                parts = line.split()
                crc = parts[1]
                flags = parts[2] if len(parts) > 2 else "RT"
                has_rot, has_loc = "R" in flags, "T" in flags
                # Key by the bare CRC. Mapping CRC -> bone happens at bind time, because the
                # armature's naming depends on the group importer's rename_bones option.
                cur = {"rot": [], "loc": []}
                tracks[_normalise_crc(crc)] = cur
                continue

            if cur is None:
                bits = line.split(None, 1)
                if len(bits) == 2:
                    header[bits[0]] = bits[1]
                continue

            vals = line.split()
            if len(vals) < 2:
                continue
            try:
                frame = int(vals[0])
                nums = [float(v) for v in vals[1:]]
            except ValueError:
                continue

            i = 0
            if has_rot and len(nums) >= 4:
                # File order is (x, y, z, w); mathutils wants (w, x, y, z).
                cur["rot"].append((frame, Quaternion((nums[3], nums[0], nums[1], nums[2]))))
                i = 4
            if has_loc and len(nums) >= i + 3:
                cur["loc"].append((frame, Vector((nums[i], nums[i + 1], nums[i + 2]))))

    return header, tracks


def _resolve_pose_bones(arm, crcs):
    """
    Map clip CRCs onto this armature's pose bones. Returns (resolved, misses, how).

    Three conventions have to be supported, because the group importer's `rename_bones`
    option (ON by default) rewrites bones to their real names after building them:

      1. `bone_<CRC>`      — the group importer's raw naming (`_bone_group_name`).
      2. `prism_crc`       — the custom property `_sync_bone_names` stashes on every bone
                             it renames, so `Head` still resolves from CRC 07C159A2.
      3. `bone_0x<CRC>`    — legacy exports that kept the 0x prefix in the bone name.

    Anything not matched is a real miss and is only reported: a clip legitimately drives
    bones a given character does not have (gear, a different weapon rig).
    """
    by_crc = {}
    for b in arm.data.bones:
        stored = b.get("prism_crc")
        if stored:
            by_crc.setdefault(str(stored).upper(), b.name)

    resolved, misses, how = {}, [], {"bone_": 0, "prism_crc": 0, "bone_0x": 0}
    for crc in crcs:
        for candidate, tag in (("bone_%s" % crc, "bone_"),
                               (by_crc.get(crc), "prism_crc"),
                               ("bone_0x%s" % crc, "bone_0x")):
            if not candidate:
                continue
            pb = arm.pose.bones.get(candidate)
            if pb is not None:
                resolved[crc] = pb
                how[tag] += 1
                break
        else:
            misses.append(crc)
    return resolved, misses, how


def _find_armature(context):
    obj = context.active_object
    if obj is not None:
        if obj.type == "ARMATURE":
            return obj
        if obj.parent is not None and obj.parent.type == "ARMATURE":
            return obj.parent
    for o in context.selected_objects:
        if o.type == "ARMATURE":
            return o
    for o in context.scene.objects:
        if o.type == "ARMATURE":
            return o
    return None


class NODE_OT_ImportPrismAnim(bpy.types.Operator, ImportHelper):
    """Import a Prism animation clip (.prismanim) onto the selected armature"""

    bl_idname = "import_scene.prism_anim"
    bl_label = "Import Prism Animation (.prismanim)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".prismanim"
    filter_glob: StringProperty(default="*.prismanim", options={"HIDDEN"})

    scale: FloatProperty(
        name="Translation scale",
        description="Scale applied to bone translations. Leave at 1.0 unless the armature was "
                    "imported at a different scale",
        default=1.0, min=0.0001, max=1000.0,
    )
    set_scene_range: BoolProperty(
        name="Set scene frame range",
        description="Set the scene's start/end frames to the clip's length",
        default=True,
    )
    clear_existing: BoolProperty(
        name="Replace existing action",
        description="Unlink whatever action is on the armature before importing",
        default=True,
    )

    def execute(self, context):
        arm = _find_armature(context)
        if arm is None:
            self.report({"ERROR"}, "No armature found. Select the imported character first.")
            return {"CANCELLED"}

        try:
            header, tracks = _parse_prismanim(self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, "Could not read the clip: %s" % exc)
            return {"CANCELLED"}

        if not tracks:
            self.report({"ERROR"}, "The clip contained no bone tracks.")
            return {"CANCELLED"}

        # Bind by CRC. A miss is reported, not fatal: a clip legitimately drives bones a given
        # character does not have (gear, a different weapon rig).
        pose_bones, misses, how = _resolve_pose_bones(arm, tracks.keys())
        missing = len(misses)

        if not pose_bones:
            self.report({"ERROR"},
                        "None of the clip's %d bones exist on '%s'. Is this the right armature? "
                        "(Bones are matched by CRC via 'bone_<CRC>', the 'prism_crc' custom "
                        "property, or legacy 'bone_0x<CRC>' — this armature has none of them.)"
                        % (len(tracks), arm.name))
            return {"CANCELLED"}

        clip_name = header.get("name") or header.get("uid") or "PrismAnim"
        try:
            frames = int(float(header.get("frames", 1) or 1))
        except ValueError:
            frames = 1

        if self.clear_existing and arm.animation_data is not None:
            arm.animation_data.action = None
        if arm.animation_data is None:
            arm.animation_data_create()
        arm.animation_data.action = bpy.data.actions.new(name=clip_name)

        # Rest local (parent-relative) per bone, so matrix_basis can be derived per frame.
        rest_local = {}
        for crc, pb in pose_bones.items():
            bone = pb.bone
            rest_local[crc] = (bone.parent.matrix_local.inverted_safe() @ bone.matrix_local
                               if bone.parent is not None else bone.matrix_local.copy())
            pb.rotation_mode = "QUATERNION"

        for crc, pb in pose_bones.items():
            t = tracks[crc]
            rest = rest_local[crc]
            # Action groups are named after the BONE, not the CRC, so the channel list stays
            # readable after rename_bones has run.
            name = pb.name
            rest_inv = rest.inverted_safe()
            rot, loc = dict(t["rot"]), dict(t["loc"])

            for frame in sorted(set(rot) | set(loc)):
                q, v = rot.get(frame), loc.get(frame)
                clip_local = (q.to_matrix().to_4x4() if q is not None
                              else rest.to_3x3().to_4x4())
                clip_local.translation = (v * self.scale) if v is not None else rest.translation
                pb.matrix_basis = rest_inv @ clip_local

                if rot:
                    pb.keyframe_insert("rotation_quaternion", frame=frame + 1, group=name)
                if loc:
                    pb.keyframe_insert("location", frame=frame + 1, group=name)

        if self.set_scene_range and frames > 0:
            context.scene.frame_start = 1
            context.scene.frame_end = frames
        context.scene.frame_set(1)

        note = "Imported '%s': %d bones, %d frames" % (clip_name, len(pose_bones), frames)
        matched = ", ".join("%s=%d" % (k, v) for k, v in how.items() if v)
        if matched:
            note += " [matched via %s]" % matched
        if missing:
            note += " (%d clip bones not on this armature)" % missing
        self.report({"INFO"}, note)
        return {"FINISHED"}


def _menu_func_import(self, context):
    self.layout.operator(NODE_OT_ImportPrismAnim.bl_idname, text="Prism Animation (.prismanim)")


def register():
    bpy.utils.register_class(NODE_OT_ImportPrismAnim)
    bpy.types.TOPBAR_MT_file_import.append(_menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_func_import)
    bpy.utils.unregister_class(NODE_OT_ImportPrismAnim)
