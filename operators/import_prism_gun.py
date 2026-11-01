"""Import a Prism gun rig package (gun.json) and set up the rig.

Prism's Gun Editor ("Export...") writes one folder per gun:

    gun.json                 manifest: name, meshes, skeleton, bone names, textures, skins
    _skeleton.txt            bones keyed by CRC32 of the bone name (same format as characters)
    mesh_<uid>.obj/.weights  each mesh, and its per-vertex bone weights keyed by the same CRCs
    textures/base/           the gun's own texture set (its Default skin)
    skins/<name>_<uid>/      each exported skin: its texture set and preview.png (the menu card);
                             a "3D" skin also carries part_*.obj meshes drawn on top of the gun
    attachments/<slot>/<name>/ every sight, barrel, grip, underbarrel and magazine the gun can fit,
                             already moved onto its socket, with its own material
    model_variants/          skins that ship their own model, when any were ticked

The import builds the armature from the game's own skeleton (Root, Bone_Trigger, Bone_Bolt,
Bone_MagRelease, the SOCKET_ATT_* attachment points and SOCKET_IK_* hand targets), names every
bone the manifest can name, binds the meshes by vertex group, sorts bones into collections with
custom shapes, and builds one material per skin with a switcher on the rig. Attachments are
mounted on their socket bones (SOCKET_ATT_ScopeMount, _Muzzle, _Grip, _Laser, _Mag), one
switcher per slot.

Reuses the character importer's OBJ, weights and skeleton helpers (import_prism_group.py), so a
change to those formats lands in both.
"""

import json
import os

import bpy  # type: ignore
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty  # type: ignore
from bpy_extras.io_utils import ImportHelper  # type: ignore

from . import import_prism_group as pg

# Sidebar tab shared with the character importer.
CAT_CHAR = "R6S Character"

# Bone collections, in display order: (name, predicate on the bone's real name, colour theme).
COLL_ROOT = "Root"
COLL_MECH = "Mechanism"
COLL_ATT = "Attachment Sockets"
COLL_IK = "Hand IK Sockets"
COLL_FX = "FX Sockets"
COLL_OTHER = "Other"

_COLLECTION_THEME = {
    COLL_ROOT: "THEME09",
    COLL_MECH: "THEME01",
    COLL_ATT: "THEME04",
    COLL_IK: "THEME03",
    COLL_FX: "THEME06",
    COLL_OTHER: "THEME10",
}


def _bone_collection_for(name):
    if name == "Root":
        return COLL_ROOT
    if name.startswith("SOCKET_IK_"):
        return COLL_IK
    if name.startswith("SOCKET_FX_"):
        return COLL_FX
    if name.startswith("SOCKET_"):
        return COLL_ATT
    if name.startswith("Bone_") or name.startswith("BJ_"):
        return COLL_MECH
    return COLL_OTHER


# ---------------------------------------------------------------------------
# Widgets (custom bone shapes)
# ---------------------------------------------------------------------------

def _widget(name, kind, size, collection):
    """A small wire mesh used as a bone's custom shape. Shared across imports."""
    obj = bpy.data.objects.get(name)
    if obj is not None:
        return obj
    import bmesh  # type: ignore
    import math

    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    if kind == "circle":
        verts = [bm.verts.new((math.cos(a) * size, 0.0, math.sin(a) * size))
                 for a in (i * 2 * math.pi / 24 for i in range(24))]
        for i in range(24):
            bm.edges.new((verts[i], verts[(i + 1) % 24]))
    elif kind == "sphere":
        for axis in range(3):
            ring = []
            for i in range(16):
                a = i * 2 * math.pi / 16
                c, s = math.cos(a) * size, math.sin(a) * size
                co = [(0.0, c, s), (c, 0.0, s), (c, s, 0.0)][axis]
                ring.append(bm.verts.new(co))
            for i in range(16):
                bm.edges.new((ring[i], ring[(i + 1) % 16]))
    else:  # "cube"
        h = size
        corners = [bm.verts.new((x, y, z)) for x in (-h, h) for y in (-h, h) for z in (-h, h)]
        for a in range(8):
            for b in range(a + 1, 8):
                if bin(a ^ b).count("1") == 1:
                    bm.edges.new((corners[a], corners[b]))
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _widget_collection():
    coll = bpy.data.collections.get("Prism Gun Widgets")
    if coll is None:
        coll = bpy.data.collections.new("Prism Gun Widgets")
        bpy.context.scene.collection.children.link(coll)
    coll.hide_viewport = True
    coll.hide_render = True
    return coll


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def _load_image(path, colorspace):
    """Load a texture with its alpha kept apart from its colour.

    A gun's diffuse alpha is a mask: where a universal skin's tiled pattern may cover the gun
    and where the original details stay. With Blender's default (Straight) alpha the colour of
    every texel with alpha 0 is lost, which turned half the PCX-33 near-black and glossy. Channel
    Packed keeps colour and alpha independent; the alpha is only used by pattern skins.
    """
    if not path or not os.path.exists(path):
        return None
    img = bpy.data.images.load(path, check_existing=True)
    try:
        img.colorspace_settings.name = colorspace
    except TypeError:
        pass
    try:
        img.alpha_mode = "CHANNEL_PACKED"
    except TypeError:
        pass
    return img


def _socket(sockets, identifier, fallback_index):
    for s in sockets:
        if s.identifier == identifier:
            return s
    return sockets[fallback_index]


def _pick(textures, tex_type):
    """The largest texture of a type (a set can hold several: body atlas, sight, …)."""
    cands = [t for t in textures if t.get("type") == tex_type]
    if not cands:
        return None
    return max(cands, key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0))


def _maps(textures, material=None):
    """The diffuse/normal/specular files one material is built from.

    Prism names them in the package (`material`, `defaultMaterial`): a gun's own set mixes the
    body atlas with sight, casing and muzzle-flash materials, so picking the largest map of each
    type can pair the body diffuse with another material's normal. Older packages fall back to
    that pick.
    """
    if material:
        return {k: material.get(k) for k in ("diffuse", "normal", "specular", "tint", "glass", "reticle",
                                             "emissive")}
    return {k: (_pick(textures, t) or {}).get("file")
            for k, t in (("diffuse", "Diffuse"), ("normal", "Normal"), ("specular", "Specular"))}


def _set_blended(mat):
    """Let Eevee draw a material with transparency (Cycles needs nothing)."""
    for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
        try:
            setattr(mat, attr, value)
        except (AttributeError, TypeError):
            pass


def _build_glass(name, tint):
    """A sight's lens: clear glass with a trace of its coating colour."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (300, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    t = list(tint or (0.8, 0.8, 0.8))[:3]
    peak = max(max(t), 1e-3)
    colour = [1.0 - 0.35 + 0.35 * (c / peak) for c in t]
    bsdf.inputs["Base Color"].default_value = (*colour, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.03
    bsdf.inputs["IOR"].default_value = 1.5
    for key in ("Transmission Weight", "Transmission"):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = 1.0
            break
    _set_blended(mat)
    return mat


def _build_reticle(name, tint, mask_file, root):
    """A reticle: its colour emitted through the reticle mask's alpha, clear elsewhere."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (500, 0)
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.location = (0, -120)
    emit.inputs["Color"].default_value = (*list(tint or (1, 0, 0))[:3], 1.0)
    emit.inputs["Strength"].default_value = 8.0
    clear = nt.nodes.new("ShaderNodeBsdfTransparent")
    clear.location = (0, 60)
    mix = nt.nodes.new("ShaderNodeMixShader")
    mix.location = (250, 0)
    nt.links.new(clear.outputs[0], mix.inputs[1])
    nt.links.new(emit.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    img = _load_image(os.path.join(root, mask_file), "Non-Color") if mask_file else None
    if img:
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.location = (-300, 0)
        nt.links.new(node.outputs["Alpha"], mix.inputs["Fac"])
    else:
        mix.inputs["Fac"].default_value = 1.0
    _set_blended(mat)
    return mat


def _build_flat(name, tint):
    """An untextured part (a sight's dark inner body): its tint, or neutral grey."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (300, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    t = list(tint or (0.5, 0.5, 0.5))[:3]
    bsdf.inputs["Base Color"].default_value = (*[max(c, 0.02) for c in t], 1.0)
    bsdf.inputs["Roughness"].default_value = 0.6
    return mat


def _build_any(name, maps, root, pattern=None):
    """The right builder for a package material entry."""
    # A reticle with no mask of its own (the shared red one) is emitted over the whole quad.
    if maps.get("reticle") or maps.get("emissive"):
        return _build_reticle(name, maps.get("tint"), maps.get("reticle"), root)
    if maps.get("glass"):
        return _build_glass(name, maps.get("tint"))
    if not (maps.get("diffuse") or maps.get("normal") or maps.get("specular")):
        return _build_flat(name, maps.get("tint"))
    return _build_material(name, maps, root, pattern)


def _build_material(name, maps, root, pattern=None):
    """Principled material from a Siege texture set.

    Siege's "Specular" is a packed data map, not a colour: R = metalness, G = gloss,
    B = cavity/AO (see TEXTURE_TYPE_MAPPING in import_prism_group.py). So R drives Metallic,
    1-G drives Roughness, and B darkens the base colour.

    A material's tint multiplies the diffuse: a magazine's diffuse is a near-white mask and its
    tint (0, 0, 0) makes it black polymer. A little is kept so the surface still reads.

    `pattern` is a universal skin's tiling diffuse. The gun's diffuse alpha says where it may go
    (white) and where the original details stay (black), so the two are mixed by that alpha. The
    pattern's tiling is a guess the user can change: the Mapping node's scale.
    """
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (300, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    diffuse = maps.get("diffuse")
    normal = maps.get("normal")
    spec = maps.get("specular")

    base_color = None
    if diffuse:
        img = _load_image(os.path.join(root, diffuse), "sRGB")
        if img:
            node = nt.nodes.new("ShaderNodeTexImage")
            node.image = img
            node.location = (-600, 300)
            base_color = node.outputs["Color"]
            if pattern:
                pimg = _load_image(os.path.join(root, pattern), "sRGB")
                if pimg:
                    coord = nt.nodes.new("ShaderNodeTexCoord")
                    coord.location = (-1300, 500)
                    mapping = nt.nodes.new("ShaderNodeMapping")
                    mapping.location = (-1100, 500)
                    mapping.label = "Pattern scale"
                    mapping.inputs["Scale"].default_value = (4.0, 4.0, 1.0)
                    nt.links.new(coord.outputs["UV"], mapping.inputs["Vector"])
                    pnode = nt.nodes.new("ShaderNodeTexImage")
                    pnode.image = pimg
                    pnode.location = (-850, 500)
                    nt.links.new(mapping.outputs["Vector"], pnode.inputs["Vector"])
                    over = nt.nodes.new("ShaderNodeMix")
                    over.data_type = "RGBA"
                    over.location = (-350, 400)
                    nt.links.new(node.outputs["Alpha"], _socket(over.inputs, "Factor_Float", 0))
                    nt.links.new(base_color, _socket(over.inputs, "A_Color", 6))
                    nt.links.new(pnode.outputs["Color"], _socket(over.inputs, "B_Color", 7))
                    base_color = _socket(over.outputs, "Result_Color", 2)
            tint = maps.get("tint")
            if tint:
                mul = nt.nodes.new("ShaderNodeMix")
                mul.data_type = "RGBA"
                mul.blend_type = "MULTIPLY"
                mul.location = (-300, 300)
                mul.inputs[0].default_value = 1.0
                nt.links.new(base_color, _socket(mul.inputs, "A_Color", 6))
                _socket(mul.inputs, "B_Color", 7).default_value = (
                    max(tint[0], 0.02), max(tint[1], 0.02), max(tint[2], 0.02), 1.0)
                base_color = _socket(mul.outputs, "Result_Color", 2)

    if spec:
        img = _load_image(os.path.join(root, spec), "Non-Color")
        if img:
            node = nt.nodes.new("ShaderNodeTexImage")
            node.image = img
            node.location = (-600, 0)
            sep = nt.nodes.new("ShaderNodeSeparateColor")
            sep.location = (-300, 0)
            nt.links.new(node.outputs["Color"], sep.inputs["Color"])
            nt.links.new(sep.outputs[0], bsdf.inputs["Metallic"])
            inv = nt.nodes.new("ShaderNodeMath")
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            inv.location = (-100, -100)
            nt.links.new(sep.outputs[1], inv.inputs[1])
            nt.links.new(inv.outputs[0], bsdf.inputs["Roughness"])
            if base_color is not None:
                mix = nt.nodes.new("ShaderNodeMix")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                mix.location = (0, 250)
                mix.inputs[0].default_value = 1.0
                nt.links.new(base_color, _socket(mix.inputs, "A_Color", 6))
                nt.links.new(sep.outputs[2], _socket(mix.inputs, "B_Color", 7))
                base_color = _socket(mix.outputs, "Result_Color", 2)

    if base_color is not None:
        nt.links.new(base_color, bsdf.inputs["Base Color"])

    if normal:
        img = _load_image(os.path.join(root, normal), "Non-Color")
        if img:
            node = nt.nodes.new("ShaderNodeTexImage")
            node.image = img
            node.location = (-600, -300)
            nmap = nt.nodes.new("ShaderNodeNormalMap")
            nmap.location = (-100, -300)
            nt.links.new(node.outputs["Color"], nmap.inputs["Color"])
            nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


# ---------------------------------------------------------------------------
# Skin switcher
# ---------------------------------------------------------------------------

_SKIN_ITEMS = []  # Blender needs the enum item strings kept alive between calls.


def _skin_items(self, context):
    global _SKIN_ITEMS
    names = list(self.get("prism_gun_materials", []))
    _SKIN_ITEMS = [(n, n.split(" | ", 1)[-1], "") for n in names] or [("NONE", "No skins", "")]
    return _SKIN_ITEMS


def _apply_skin(self, context):
    """Put the chosen skin on every island that wears the gun's own material (and nothing else:
    lenses, rounds and attachments keep theirs)."""
    mat = bpy.data.materials.get(self.prism_gun_skin)
    if mat is None:
        return
    # A replacement skin is a whole gun: the plain body hides while it is on.
    replaced = any(c.get("prism_skin_part_of") == mat.name and c.get("prism_replaces_body") for c in self.children)
    for child in self.children:
        if child.type != "MESH":
            continue
        # Attachments keep the visibility their own slot gives them.
        if child.get("prism_skin_part_of") is None and child.get("prism_att_slot") is None and "prism_gun_skin_slots" in child:
            child.hide_viewport = child.hide_render = replaced
        # A "3D" skin's add-on parts show only while their skin is on.
        part_of = child.get("prism_skin_part_of")
        if part_of is not None:
            child.hide_viewport = child.hide_render = part_of != mat.name
            continue
        for slot in child.get("prism_gun_skin_slots", []):
            if slot < len(child.data.materials):
                child.data.materials[slot] = mat


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

SLOTS = ("Sight", "Barrel", "Grip", "Underbarrel", "Magazine")
NONE = "None"
_SLOT_ITEMS = {}  # Blender needs the enum item strings kept alive between calls.


def _slot_prop(slot):
    return f"prism_gun_{slot.lower()}"


def _slot_items(slot):
    def items(self, context):
        names = list((self.get("prism_gun_attachments") or {}).get(slot, []))
        _SLOT_ITEMS[slot] = [(NONE, "None", "Nothing fitted")] + [(n, n, "") for n in names]
        return _SLOT_ITEMS[slot]
    return items


def _slot_update(slot):
    def update(self, context):
        _show_attachment(self, slot, getattr(self, _slot_prop(slot)))
    return update


def _show_attachment(arm, slot, choice):
    for obj in bpy.data.objects:
        if obj.parent == arm and obj.get("prism_att_slot") == slot:
            hide = obj.get("prism_att_name") != choice
            obj.hide_viewport = hide
            obj.hide_render = hide


def _dress(parts, islands, gun_skin, materials):
    """Give each imported island object its material, before the islands are joined.

    `islands[i]` is the package key of island i's material; `gun_skin` the islands that wear the
    gun's own material. Returns the materials that take a skin, so the joined object can record
    which of its slots those are.
    """
    skin_mats = set()
    for part in parts:
        i = pg._object_index(part.name)
        i = i if i is not None and i < len(islands) else 0
        mat = materials.get(islands[i]) if islands else None
        part.data.materials.clear()
        if mat is not None:
            part.data.materials.append(mat)
            if i in (gun_skin or []):
                skin_mats.add(mat.name)
    return skin_mats


def _record_skin_slots(obj, skin_mats):
    obj["prism_gun_skin_slots"] = [i for i, m in enumerate(obj.data.materials) if m is not None and m.name in skin_mats]


def _mount(obj, arm, bone_name):
    """Parent to the socket bone without moving: the exporter already placed the mesh on it."""
    world = obj.matrix_world.copy()
    obj.parent = arm
    if bone_name and bone_name in arm.data.bones:
        obj.parent_type = "BONE"
        obj.parent_bone = bone_name
    bpy.context.view_layer.update()
    obj.matrix_world = world


def _add_skin_bones(arm, path, bone_names):
    """The bones only a skin has, the ones that move its sculpted parts (the G36C dragon's 58), added to the gun's armature."""
    import mathutils  # type: ignore

    bones = pg._parse_skeleton_file(path)
    if not bones:
        return
    # The gun's bones may already carry their real names; the skeleton file spells every bone by CRC.
    def resolved(name):
        return bone_names.get(name, name)

    missing = {n: b for n, b in bones.items() if resolved(n) not in arm.data.bones}
    if not missing:
        return
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    edit = arm.data.edit_bones
    for name, b in missing.items():
        eb = edit.new(name)
        qx, qy, qz, qw = b["quat"]
        quat = mathutils.Quaternion((qw, qx, qy, qz))
        if quat.magnitude < 1e-6:
            quat = mathutils.Quaternion((1, 0, 0, 0))
        quat.normalize()
        head = mathutils.Vector(b["pos"])
        mat = quat.to_matrix().to_4x4()
        mat.translation = head
        eb.head = head
        eb.tail = head + mathutils.Vector((0, 0.02, 0))
        eb.matrix = mat
        eb.length = 0.02
    for name, b in missing.items():
        parent = resolved(b["parent"]) if b["parent"] else None
        if parent and parent in edit and parent != name:
            edit[name].parent = edit[parent]
            edit[name].use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")


def _import_skin_parts(man, root, gun, coll, arm, bone_names, skin_materials):
    """A "3D" skin adds meshes on top of the gun (a bayonet, a sculpted shroud). They are imported hidden,
    and the skin switcher shows the chosen skin's parts. A part keeps its own bone list, with any bones only
    it has; one without weights rides the root bone, as nearly all of the gun body does."""
    entries = [(s, p) for s in man.get("skins", []) for p in (s.get("parts") or [])]
    if not entries:
        return []
    root_bone = None
    if arm is not None:
        root_bone = "Root" if "Root" in arm.data.bones else next((b.name for b in arm.data.bones if b.parent is None), None)
    top = bpy.data.collections.new(f"{gun} Skin Parts")
    coll.children.link(top)
    objs = []
    for skin, entry in entries:
        owner = skin_materials.get(skin.get("name"))
        path = os.path.join(root, entry["obj"])
        if owner is None or not os.path.exists(path):
            continue
        parts = pg._import_obj(path)
        if not parts:
            continue
        weights = pg._parse_weights_file(os.path.join(root, entry["weights"])) if entry.get("weights") else None
        if weights:
            pos_index = pg._build_obj_position_index(path)
            for o in parts:
                pg._apply_weights_to_object(o, weights, pos_index)
        if arm is not None and entry.get("skeleton"):
            _add_skin_bones(arm, os.path.join(root, entry["skeleton"]), bone_names)
        maps = _maps([], entry.get("material")) if entry.get("material") else None
        # A part whose material was not told apart wears its skin's pattern.
        mat = (_build_material(f"{gun} | {skin['name']} | part", maps, root)
               if maps and (maps.get("diffuse") or maps.get("normal")) else bpy.data.materials.get(owner))
        # Each island wears its own material: a replacement's receiver and sculpted parts have an atlas each.
        island_materials = entry.get("island_materials") or []
        built = {}
        for o in parts:
            i = pg._object_index(o.name)
            own = island_materials[i] if i is not None and i < len(island_materials) else None
            part_mat = mat
            if own:
                key = str(own)
                if key not in built:
                    own_maps = _maps([], own)
                    built[key] = (_build_material(f"{gun} | {skin['name']} | part {len(built) + 1}", own_maps, root)
                                  if own_maps.get("diffuse") or own_maps.get("normal") else mat)
                part_mat = built[key]
            o.data.materials.clear()
            if part_mat is not None:
                o.data.materials.append(part_mat)
        obj = _join(parts)
        obj.name = f"{gun} {skin['name']} part {entry['uid'][-6:]}"
        obj["prism_skin_part_of"] = owner
        obj["prism_replaces_body"] = bool(skin.get("replaces_body"))
        obj["prism_uid"] = entry["uid"]
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        top.objects.link(obj)
        for g in obj.vertex_groups:
            if g.name in bone_names:
                g.name = bone_names[g.name]
        if arm is not None:
            if weights:
                pg._parent_to_armature(obj, arm)
            else:
                _mount(obj, arm, root_bone)
        obj.hide_viewport = obj.hide_render = True
        objs.append(obj)
    return objs


def _import_attachments(man, root, gun, coll, arm, materials):
    atts = man.get("attachments") or []
    if not atts:
        return []
    top = bpy.data.collections.new(f"{gun} Attachments")
    coll.children.link(top)
    slot_colls = {}
    objs = []
    for a in atts:
        slot = a.get("slot")
        parts = []
        skin_mats = set()
        for m in a.get("meshes", []):
            entry = m if isinstance(m, dict) else {"obj": m, "islands": [], "gunSkin": []}
            path = os.path.join(root, entry["obj"])
            if not os.path.exists(path):
                continue
            mesh_parts = pg._import_obj(path) or []
            skin_mats |= _dress(mesh_parts, entry.get("islands") or [], entry.get("gunSkin") or [], materials)
            parts += mesh_parts
        if not parts:
            continue
        obj = _join(parts)
        obj.name = f"{gun} {slot} {a.get('name')}"
        obj["prism_att_slot"] = slot
        obj["prism_att_name"] = a.get("name")
        _record_skin_slots(obj, skin_mats)
        sc = slot_colls.get(slot)
        if sc is None:
            sc = slot_colls[slot] = bpy.data.collections.new(f"{gun} {slot}")
            top.children.link(sc)
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        sc.objects.link(obj)
        if arm is not None:
            _mount(obj, arm, a.get("socket"))
        objs.append(obj)
    return objs


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_gun(manifest_path, import_parts=True, bone_size=0.02, report=None, import_extras=True):
    root = os.path.dirname(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        man = json.load(f)
    if man.get("format") != "prism-gun":
        raise ValueError("not a Prism gun manifest (gun.json)")

    gun = man.get("name") or "Gun"
    names = {f"bone_{crc.upper()}": nm for crc, nm in (man.get("boneNames") or {}).items()}

    coll = bpy.data.collections.new(gun)
    bpy.context.scene.collection.children.link(coll)

    # ---- armature -----------------------------------------------------------------------
    arm = None
    skel_file = man.get("skeleton")
    bones = pg._parse_skeleton_file(os.path.join(root, skel_file)) if skel_file else None
    if bones:
        arm = pg._build_armature(bones, name=f"{gun}_Rig")
        for c in list(arm.users_collection):
            c.objects.unlink(arm)
        coll.objects.link(arm)
        arm.show_in_front = True
        arm.data.display_type = "OCTAHEDRAL"
        _setup_bones(arm, names, bone_size)

    # ---- materials ----------------------------------------------------------------------
    # Every material the package names, by key. The gun's own ("Default") is what skins replace.
    package = {}
    for key, entry in (man.get("materials") or {}).items():
        label = "Default" if key == man.get("defaultMaterial") else entry.get("name") or key
        package[key] = _build_any(f"{gun} | {label}", _maps([], entry), root)
    default_mat = package.get(man.get("defaultMaterial"))
    default_entry = (man.get("materials") or {}).get(man.get("defaultMaterial")) or {}
    default_maps = _maps([], default_entry) if default_entry else _maps(man.get("baseTextures") or [], None)
    if default_mat is None and (default_maps.get("diffuse") or default_maps.get("normal")):
        default_mat = _build_material(f"{gun} | Default", default_maps, root)

    materials = [default_mat] if default_mat is not None else []
    skin_materials = {}  # skin name -> its material's (possibly uniquified) name, for 3D skin parts
    for skin in man.get("skins", []):
        if not skin.get("textures"):
            if skin.get("parts") and (default_maps.get("diffuse") or default_maps.get("normal")):
                # A 3D skin whose pattern is not on disk: its parts still go on the gun as issued.
                materials.append(_build_material(f"{gun} | {skin['name']}", dict(default_maps), root))
                skin_materials[skin.get("name")] = materials[-1].name
            continue
        maps = _maps(skin["textures"], skin.get("material"))
        if skin.get("pattern") or not (maps.get("normal") or maps.get("specular")):
            # A universal pattern: the gun's own maps, with the pattern laid over where the gun's
            # diffuse alpha allows it.
            materials.append(_build_material(f"{gun} | {skin['name']}", dict(default_maps), root,
                                             pattern=maps.get("diffuse")))
        else:
            materials.append(_build_material(f"{gun} | {skin['name']}", maps, root))
        skin_materials[skin.get("name")] = materials[-1].name

    # ---- meshes -------------------------------------------------------------------------
    # "extra" meshes are what the in-hand builder draws besides the gun: shared helpers placed
    # metres away, loose rounds, FX strips. They are kept, hidden, in their own sub-collection.
    extras = None
    mesh_objects = []
    for i, entry in enumerate(man.get("meshes", [])):
        role = entry.get("role", "body")
        if role == "part" and not import_parts:
            continue
        if role == "extra" and not import_extras:
            continue
        obj_path = os.path.join(root, entry["obj"])
        if not os.path.exists(obj_path):
            continue
        parts = pg._import_obj(obj_path)
        if not parts:
            continue
        weights = None
        if entry.get("weights"):
            weights = pg._parse_weights_file(os.path.join(root, entry["weights"]))
        if weights:
            pos_index = pg._build_obj_position_index(obj_path)
            for o in parts:
                pg._apply_weights_to_object(o, weights, pos_index)
        islands = entry.get("islands") or []
        if islands:
            skin_mats = _dress(parts, islands, entry.get("gunSkin") or [], package)
        else:
            # An older package: one material per mesh, the gun's own on the body.
            skin_mats = {default_mat.name} if default_mat is not None and role == "body" else set()
            for part in parts:
                part.data.materials.clear()
                if default_mat is not None:
                    part.data.materials.append(default_mat)
        obj = _join(parts)
        obj.name = f"{gun}_{role}_{entry['uid'][-6:]}"
        obj["prism_gun_role"] = role
        obj["prism_uid"] = entry["uid"]
        _record_skin_slots(obj, skin_mats)
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        if role == "extra":
            if extras is None:
                extras = bpy.data.collections.new(f"{gun} Extras")
                coll.children.link(extras)
                _set_collection_hidden(extras)
            extras.objects.link(obj)
        else:
            coll.objects.link(obj)
        for g in obj.vertex_groups:
            if g.name in names:
                g.name = names[g.name]
        if arm is not None:
            if weights:
                pg._parent_to_armature(obj, arm)
            else:
                # Nothing to deform by: follow the rig as a whole.
                obj.parent = arm
                obj.matrix_parent_inverse = arm.matrix_world.inverted()
        mesh_objects.append(obj)

    # ---- attachments --------------------------------------------------------------------
    attachments = _import_attachments(man, root, gun, coll, arm, package)
    skin_parts = _import_skin_parts(man, root, gun, coll, arm, names, skin_materials)
    first = materials[1] if len(materials) > 1 else (materials[0] if materials else None)

    if arm is not None:
        arm["prism_gun_materials"] = [m.name for m in materials]
        arm["prism_gun_manifest"] = manifest_path
        if first is not None:
            arm.prism_gun_skin = first.name
        if attachments:
            by_slot = {}
            for obj in attachments:
                by_slot.setdefault(obj["prism_att_slot"], []).append(obj["prism_att_name"])
            arm["prism_gun_attachments"] = by_slot
            # The gun as issued: its magazine and its first sight fitted, nothing on the barrel,
            # grip or underbarrel.
            for slot in SLOTS:
                names = by_slot.get(slot, [])
                choice = names[0] if names and slot in ("Sight", "Magazine") else NONE
                setattr(arm, _slot_prop(slot), choice)
                _show_attachment(arm, slot, choice)
        bpy.context.view_layer.objects.active = arm
        arm.select_set(True)

    if report:
        named = sum(1 for b in (arm.data.bones if arm else []) if not b.name.startswith("bone_"))
        n_extra = len(extras.objects) if extras is not None else 0
        report({"INFO"}, f"{gun}: {len(mesh_objects) - n_extra} meshes (+{n_extra} hidden extras), "
                         f"{len(attachments)} attachments, {len(skin_parts)} 3D skin parts, "
                         f"{len(arm.data.bones) if arm else 0} bones ({named} named), {len(materials)} materials")
    return arm, mesh_objects


def _set_collection_hidden(coll):
    """Hidden in the viewport and the render, and unticked in the outliner, but still there."""
    coll.hide_render = True
    layer = _find_layer_collection(bpy.context.view_layer.layer_collection, coll.name)
    if layer is not None:
        layer.hide_viewport = True
    else:
        coll.hide_viewport = True


def _find_layer_collection(layer, name):
    if layer.name == name:
        return layer
    for child in layer.children:
        found = _find_layer_collection(child, name)
        if found is not None:
            return found
    return None


def _join(parts):
    """One object per mesh: the OBJ splits each mesh into its islands."""
    if len(parts) == 1:
        return parts[0]
    ctx = bpy.context
    for o in ctx.selected_objects:
        o.select_set(False)
    for o in parts:
        o.select_set(True)
    ctx.view_layer.objects.active = parts[0]
    with ctx.temp_override(active_object=parts[0], selected_editable_objects=parts, selected_objects=parts):
        bpy.ops.object.join()
    return parts[0]


def _setup_bones(arm, names, bone_size):
    """Real names, short bones, collections, colours and widgets."""
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    for eb in arm.data.edit_bones:
        eb.length = bone_size
    bpy.ops.object.mode_set(mode="OBJECT")

    for b in arm.data.bones:
        real = names.get(b.name)
        if real and real not in arm.data.bones:
            b["prism_crc"] = b.name[5:]
            b.name = real

    wcoll = _widget_collection()
    shapes = {
        COLL_ROOT: _widget("WGT_PrismGun_Root", "circle", bone_size * 6, wcoll),
        COLL_MECH: _widget("WGT_PrismGun_Mechanism", "sphere", bone_size * 0.6, wcoll),
        COLL_ATT: _widget("WGT_PrismGun_Socket", "cube", bone_size * 0.35, wcoll),
        COLL_IK: _widget("WGT_PrismGun_IK", "cube", bone_size * 0.5, wcoll),
        COLL_FX: _widget("WGT_PrismGun_FX", "cube", bone_size * 0.25, wcoll),
    }

    colls = {}
    for cname in (COLL_ROOT, COLL_MECH, COLL_ATT, COLL_IK, COLL_FX, COLL_OTHER):
        colls[cname] = arm.data.collections.get(cname) or arm.data.collections.new(cname)

    for b in arm.data.bones:
        cname = _bone_collection_for(b.name)
        colls[cname].assign(b)
        try:
            b.color.palette = _COLLECTION_THEME[cname]
        except (AttributeError, TypeError):
            pass
        pb = arm.pose.bones.get(b.name)
        if pb is not None:
            pb.rotation_mode = "XYZ"
            shape = shapes.get(cname)
            if shape is not None:
                pb.custom_shape = shape
                pb.use_custom_shape_bone_size = False

    # Sockets are reference points, not things to animate: visible, but out of the way.
    colls[COLL_FX].is_visible = False
    colls[COLL_OTHER].is_visible = any(len(c.bones) for c in [colls[COLL_OTHER]])


class IMPORT_SCENE_OT_prism_gun(bpy.types.Operator, ImportHelper):
    """Import a Prism gun rig package (gun.json): meshes, rig, weights and skins"""
    bl_idname = "import_scene.prism_gun"
    bl_label = "Import Prism Gun"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="gun.json;*.json", options={"HIDDEN"})

    import_parts: BoolProperty(
        name="Import shared parts",
        description="Also import meshes the gun shares with other guns (rails, sights)",
        default=True,
    )
    import_extras: BoolProperty(
        name="Import extras (hidden)",
        description="Also import what the in-hand model draws besides the gun (shared helpers, loose "
                    "rounds), into a hidden sub-collection",
        default=True,
    )
    bone_size: FloatProperty(
        name="Bone size",
        description="Display length of the bones; guns are small, so the character default is too big",
        default=0.02, min=0.001, max=0.5,
    )

    def execute(self, context):
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        try:
            import_gun(self.filepath, self.import_parts, self.bone_size, self.report, self.import_extras)
        except Exception as ex:  # noqa: BLE001 - surface every failure in the UI
            self.report({"ERROR"}, f"Gun import failed: {ex}")
            return {"CANCELLED"}
        return {"FINISHED"}


class VIEW3D_PT_prism_gun(bpy.types.Panel):
    """Prism gun import and the skin switcher for the active gun rig."""
    bl_label = "Prism Gun"
    bl_idname = "VIEW3D_PT_prism_gun"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CAT_CHAR

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator("import_scene.prism_gun", text="Import Gun (gun.json)", icon="IMPORT")

        arm = context.active_object
        if arm is None and context.object is not None:
            arm = context.object
        if arm is not None and arm.type != "ARMATURE" and arm.parent is not None:
            arm = arm.parent
        if arm is None or arm.type != "ARMATURE" or "prism_gun_materials" not in arm:
            return

        box = layout.box()
        box.label(text=arm.name, icon="ARMATURE_DATA")
        box.prop(arm, "prism_gun_skin", text="Skin")
        if "prism_gun_attachments" in arm:
            att = box.column(align=True)
            for slot in SLOTS:
                if slot in arm["prism_gun_attachments"]:
                    att.prop(arm, _slot_prop(slot), text=slot)
        row = box.row(align=True)
        for c in arm.data.collections:
            row.prop(c, "is_visible", text=c.name[:4], toggle=True)


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_prism_gun.bl_idname, text="Prism Gun (gun.json)")


def register():
    bpy.types.Object.prism_gun_skin = EnumProperty(
        name="Skin", description="Which exported skin the gun body wears",
        items=_skin_items, update=_apply_skin)
    for slot in SLOTS:
        setattr(bpy.types.Object, _slot_prop(slot), EnumProperty(
            name=slot, description=f"Which {slot.lower()} the gun has fitted",
            items=_slot_items(slot), update=_slot_update(slot)))
    bpy.utils.register_class(IMPORT_SCENE_OT_prism_gun)
    bpy.utils.register_class(VIEW3D_PT_prism_gun)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(VIEW3D_PT_prism_gun)
    bpy.utils.unregister_class(IMPORT_SCENE_OT_prism_gun)
    del bpy.types.Object.prism_gun_skin
    for slot in SLOTS:
        delattr(bpy.types.Object, _slot_prop(slot))
