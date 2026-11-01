import bpy  # type: ignore
import os
import re
import glob

from .auto_setup import append_shader_group, load_shader_groups


# Texture type tokens from Prism's _export.log filename convention.
# Map each to (Character BSDF input name, colorspace, alpha_mode, is_normal).
TEXTURE_TYPE_MAPPING = {
    # BC3: RGB albedo + a real alpha channel.
    "Diffuse":      ("Diffuse",     "sRGB",      "STRAIGHT", False),
    # BC5: R = X, G = Y, Z is reconstructed. Blue/alpha carry nothing.
    "Normal":       ("Normal Base", "Non-Color", "NONE",     True),
    # Siege's "Specular" is a BC1 packed data map, NOT a specular colour. Channel
    # meanings measured against the Smoke/Kaid/Maverick sets (see
    # research/y11s2_character_materials.md §"Specular channel packing"):
    #     R = METALNESS   near-binary; exactly 0 across whole dielectric materials
    #     G = GLOSS       continuous; low for fabric (~0.2), high for metal (~0.58)
    #     B = NOT cavity  on a face it is bright on nose/lips/brows and dark on the
    #                     cheeks (the inverse of AO), and unauthored cloth sits at a flat
    #                     0.48. Multiplying it into the albedo blackened every head, so
    #                     _build_material turns the group's Cavity Strength off.
    # Gloss, not roughness -- invert it if the group wants roughness. The group takes
    # the raw colour and splits it itself, so no colour management here.
    "Specular":     ("PBR Multi",   "Raw",       "NONE",     False),
    # BC1 region mask: R/G/B are mutually exclusive (corr(R,G) = -0.998), one region
    # per detail-normal slot. Data, not colour -- but kept sRGB because the existing
    # group's thresholds were authored against the sRGB-decoded values.
    "ColorMask":    ("ID map",      "sRGB",      "NONE",     False),
    "Misc":         (None,          "Non-Color", "NONE",     False),
    "Displacement": (None,          "Non-Color", "NONE",     False),
    "Translucence": (None,          "sRGB",      "NONE",     False),
}

# Colorspace names differ between Blender versions/OCIO configs. Try in order and keep
# the first that the build accepts, so the import doesn't silently fall back to sRGB.
COLORSPACE_FALLBACKS = {
    "Raw": ("Raw", "Non-Color", "Linear Rec.709", "Linear"),
    "Non-Color": ("Non-Color", "Raw"),
    "sRGB": ("sRGB",),
}

# V5 is the setup matched to the reversed material data: Specular R/G read as
# metalness / gloss (its B "cavity" multiply is switched off per material, see
# _build_material), and the three detail normals selected by the ColorMask
# channel that gates them (see research/y11s2_character_materials.md §11-12).
CHARACTER_SHADER_NAME = "Siege Character BSDF V5"

# Fallback group for exports whose log has no material parameters. Routing is per
# MATERIAL now (see _is_eye_material) -- the old per-mesh UID match is gone, because
# the eye mesh also carries the teeth and they were getting the eye material.
EYE_SHADER_NAME = "R6S_Default_Eye"

# Override/tint variant materials are paired with each base material in the BT
# (base 0x...169C__ ↔ variant 0x00000009D301F0__). They carry no real texture set
# of their own; the importer only assigns the BASE materials per object.
OVERRIDE_VARIANT_PREFIXES = ("00000009D301F0",)
# The synthetic orphan bucket the exporter uses for textures it couldn't attribute
# to a real material. Never assign it to an object.
ORPHAN_MATERIAL_UID = "0000000000000000"

# Matches "<NAME>_<TYPE>_<UID>" (with or without extension).
TEX_FILENAME_RE = re.compile(
    r"^(?P<name>.+)_(?P<type>[A-Za-z0-9]+)_(?P<uid>[0-9A-F]{16})$"
)

# Largest dimension (px) at or below which an unconnected image is treated as a
# "ripped" constant/color value and skipped (matches auto_setup's heuristic).
RIPPED_TEXTURE_MAX_SIZE = 16


def _parse_texture_filename(stem):
    """Return (name, type, uid) from a texture file stem, or None."""
    m = TEX_FILENAME_RE.match(stem)
    if not m:
        return None
    return m.group("name"), m.group("type"), m.group("uid")


def _texture_stem(tex):
    """A log texture name without its extension. Exports that wrote both formats logged
    "x.png|.dds"; splitext alone left "x.png|", which matched no file, so those
    characters imported with no textures at all."""
    tex = tex.split("|", 1)[0].strip()
    return os.path.splitext(tex)[0]


def _parse_export_log(log_path):
    """
    Parse a Prism _export.log.

    Returns (meshes, materials_index):
        meshes = {
          mesh_uid: [
            {"bt": bt_uid,
             "islands": {island_index: mat_uid, ...},   # authoritative binding
             "materials": {
                mat_uid: {"base": [tex_stem, ...],
                          "detail": [(tex_stem, scale_x, scale_y, chan, kind), ...]}
            }},
            ...
          ],
          ...
        }
        materials_index = {mat_uid: {"base": [...], "detail": [...]}}  # log-wide

    The "island -> material" block is what makes the object<->material pairing
    exact: Prism resolves it from the BuildTable's mesh material array (indexed
    by each island's MatId). Older logs have no such block, so the caller keeps
    a best-guess fallback.
    """
    meshes = {}
    materials_index = {}
    current_bt_entry = None
    current_mat_uid = None

    def _parse_detail(m, tex):
        """One detail line -> (tex_stem, scale_x, scale_y, channel, kind).

        channel is 0/1/2 for the ColorMask R/G/B slot that gates this detail map, or
        None on legacy logs that predate channel resolution.
        """
        groups = m.groupdict()
        tex_stem = _texture_stem(tex)
        if "t" in groups:  # current form: one tiling factor, applied to both axes
            try:
                tiling = float(groups["t"])
            except (TypeError, ValueError):
                tiling = 1.0
            chan = "RGB".find((groups.get("chan") or "?").upper())
            return (tex_stem, tiling, tiling, chan if chan >= 0 else None,
                    (groups.get("kind") or "normal").lower())
        try:
            sx, sy = float(groups["sx"]), float(groups["sy"])
        except (TypeError, ValueError):
            sx, sy = 1.0, 1.0
        return (tex_stem, sx, sy, None, "normal")

    mesh_re = re.compile(
        r"^mesh\s+0x(?P<mesh>[0-9A-F]{16})\s+via\s+BT\s+0x(?P<bt>[0-9A-F]{16})",
        re.IGNORECASE,
    )
    # "material 0x....  type=Eye  shader=0x7005948D"
    mat_re = re.compile(
        r"^material\s+0x(?P<mat>[0-9A-F]{16})"
        r"(?:\s+type=(?P<type>\S+))?"
        r"(?:\s+shader=0x(?P<shader>[0-9A-F]{8}))?",
        re.IGNORECASE,
    )
    # "param[4] color (0.341,0.411,0.31) #53851558"  /  "param[9] scalar 2.15 #1106D434"
    param_re = re.compile(
        r"^param\[(?P<idx>\d+)\]\s+(?:"
        r"color\s*\((?P<r>[-0-9.eE]+),(?P<g>[-0-9.eE]+),(?P<b>[-0-9.eE]+)\)"
        r"|scalar\s+(?P<v>[-0-9.eE]+))",
        re.IGNORECASE,
    )
    # "object3 (MatId 4) -> material 0x...."  (older logs: "object3 (mat3) -> ...")
    island_re = re.compile(
        r"^object(?P<isl>\d+)\s*\([^)]*\)\s*->\s*material\s+0x(?P<mat>[0-9A-F]{16})\s*$",
        re.IGNORECASE,
    )
    # Current form: "detail[G] normal (tiling 20) -> <tex>".  The letter is the ColorMask
    # channel that gates the slot, so it -- not arrival order -- decides which
    # Normal Detail input the texture belongs on.
    detail_re = re.compile(
        r"^detail\[(?P<chan>[RGB?])\]\s*(?P<kind>normal|albedo)\s*"
        r"\(tiling\s*(?P<t>[-0-9.]+)\)\s*->\s*(?P<tex>.+)$",
        re.IGNORECASE,
    )
    # Legacy form written before the detail slots were channel-resolved; no channel, and
    # the pair was a (scaleX, scaleY) rather than a single tiling factor.
    detail_legacy_re = re.compile(
        r"^detail\s*\(scale\s*(?P<sx>[-0-9.]+)\s*,\s*(?P<sy>[-0-9.]+)\)\s*->\s*(?P<tex>.+)$",
        re.IGNORECASE,
    )
    tex_re = re.compile(r"^->\s*(?P<tex>.+)$")
    # "name: CHR_Maverick_UNF_Body_M99_LOD0_CM" / "package: HOTSHOT WELDER" under a mesh line.
    label_re = re.compile(r"^(?P<key>name|package):\s*(?P<value>.+)$", re.IGNORECASE)

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue

            if stripped.startswith("==="):
                current_bt_entry = None
                current_mat_uid = None
                continue

            m = mesh_re.match(stripped)
            if m:
                mesh_uid = m.group("mesh").upper()
                bt_uid = m.group("bt").upper()
                current_bt_entry = {"bt": bt_uid, "islands": {}, "materials": {}}
                current_mat_uid = None
                meshes.setdefault(mesh_uid, []).append(current_bt_entry)
                continue

            m = label_re.match(stripped)
            if m and current_bt_entry is not None and current_mat_uid is None:
                current_bt_entry[m.group("key").lower()] = m.group("value").strip()
                continue

            # Island binding lines come before the material blocks and must be
            # matched first — they also start with a word+"0x..." shape.
            m = island_re.match(stripped)
            if m and current_bt_entry is not None:
                current_bt_entry["islands"][int(m.group("isl"))] = m.group("mat").upper()
                continue

            m = mat_re.match(stripped)
            if m and current_bt_entry is not None:
                current_mat_uid = m.group("mat").upper()
                blank = {"base": [], "detail": [], "params": {},
                         "type": (m.group("type") or "").strip(),
                         "shader": (m.group("shader") or "").upper()}
                current_bt_entry["materials"].setdefault(current_mat_uid, dict(blank))
                entry = materials_index.setdefault(current_mat_uid, dict(blank))
                # A material block repeats per mesh; keep the type/shader from whichever
                # copy carries them.
                entry["type"] = entry.get("type") or blank["type"]
                entry["shader"] = entry.get("shader") or blank["shader"]
                continue

            # Numeric material parameters, keyed by their position in the material's
            # parameter table. Position is the identifier -- the name hashes are a
            # bespoke per-build transform, so they are logged only as a comment.
            m = param_re.match(stripped)
            if m and current_bt_entry is not None and current_mat_uid is not None:
                idx = int(m.group("idx"))
                if m.group("r") is not None:
                    val = (float(m.group("r")), float(m.group("g")), float(m.group("b")))
                else:
                    val = float(m.group("v"))
                for store in (current_bt_entry["materials"][current_mat_uid],
                              materials_index[current_mat_uid]):
                    store.setdefault("params", {})[idx] = val
                continue

            # Detail lines must be checked BEFORE the generic "-> tex" line.
            m = detail_re.match(stripped) or detail_legacy_re.match(stripped)
            if m and current_bt_entry is not None and current_mat_uid is not None:
                tex = m.group("tex").strip()
                if not tex.lower().endswith("(not exported)"):
                    entry = _parse_detail(m, tex)
                    if entry is not None:
                        current_bt_entry["materials"][current_mat_uid]["detail"].append(entry)
                        _append_unique(
                            materials_index[current_mat_uid]["detail"], entry
                        )
                continue

            m = tex_re.match(stripped)
            if m and current_bt_entry is not None and current_mat_uid is not None:
                tex = m.group("tex").strip()
                # Skip "0x...  (not exported)" lines — only real texture files matter.
                if tex.lower().endswith("(not exported)"):
                    continue
                tex_stem = _texture_stem(tex)
                current_bt_entry["materials"][current_mat_uid]["base"].append(tex_stem)
                _append_unique(materials_index[current_mat_uid]["base"], tex_stem)
                continue

    return meshes, materials_index


def _append_unique(seq, value):
    if value not in seq:
        seq.append(value)


def _island_materials_for_mesh(bt_entries):
    """
    Merge the per-BuildTable island bindings for one mesh into
    {island_index: mat_uid}. A mesh is exported once per (mesh, BT) row, and every
    row resolves the same array, so the first non-empty binding per island wins.
    Returns {} when the log predates the island block.
    """
    islands = {}
    for bt_entry in bt_entries:
        for isl, mat_uid in bt_entry.get("islands", {}).items():
            islands.setdefault(isl, mat_uid)
    return islands


def _base_materials_for_mesh(bt_entries):
    """
    Collapse a mesh's BT entries into the ordered list of BASE materials
    (skipping the 0x0 orphan bucket and the D301F0__ override variants).

    Returns a list of (mat_uid, [tex_stem,...], bt_uid), UID-sorted so the
    object<->material pairing is stable across runs. Only materials that actually
    have at least one resolved texture are kept.
    """
    seen = {}
    order = []
    for bt_entry in bt_entries:
        bt_uid = bt_entry["bt"]
        for mat_uid, tex in bt_entry["materials"].items():
            up = mat_uid.upper()
            if up == ORPHAN_MATERIAL_UID:
                continue
            if any(up.startswith(p) for p in OVERRIDE_VARIANT_PREFIXES):
                continue
            base = tex.get("base", [])
            detail = tex.get("detail", [])
            if not base and not detail:
                continue
            if up in seen:
                continue
            seen[up] = True
            order.append((up, base, detail, bt_uid))
    order.sort(key=lambda t: t[0])
    return order


def _find_texture_on_disk(textures_dir, tex_stem):
    """Resolve a texture stem from the log to a file on disk; prefer PNG."""
    for ext in (".png", ".dds"):
        candidate = os.path.join(textures_dir, tex_stem + ext)
        if os.path.exists(candidate):
            return candidate
    return None


def _set_colorspace(img, wanted):
    """
    Set an image's colorspace, tolerating builds where the exact name is absent.
    Assigning an unknown name raises and would otherwise leave the image on the sRGB
    default — which visibly wrecks a packed PBR map.
    """
    for name in COLORSPACE_FALLBACKS.get(wanted, (wanted,)):
        try:
            img.colorspace_settings.name = name
            return name
        except Exception:
            continue
    print(f"[PrismImport] Could not set colorspace '{wanted}' on {img.name}")
    return None


def _load_image_cached(filepath, cache):
    """Load image once, share via cache keyed by absolute path."""
    abspath = os.path.abspath(filepath)
    if abspath in cache:
        return cache[abspath]

    for img in bpy.data.images:
        try:
            if img.filepath and os.path.abspath(bpy.path.abspath(img.filepath)) == abspath:
                cache[abspath] = img
                return img
        except Exception:
            pass

    img = bpy.data.images.load(filepath, check_existing=True)
    cache[abspath] = img
    return img


def _is_ripped(img):
    """A tiny image (<=16px) is a ripped constant/color value, not a real map."""
    try:
        return max(img.size[0], img.size[1]) <= RIPPED_TEXTURE_MAX_SIZE
    except Exception:
        return False


def _new_group_material(mat_name, shader_name):
    """Create a material whose surface is a fresh instance of the named shader group."""
    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    tree = mat.node_tree
    nodes = tree.nodes
    links = tree.links

    for n in list(nodes):
        if n.type == "BSDF_PRINCIPLED":
            nodes.remove(n)

    output_node = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None)
    if output_node is None:
        output_node = nodes.new(type="ShaderNodeOutputMaterial")
    output_node.location = (300, 100)

    shader_group = bpy.data.node_groups.get(shader_name)
    group_node = nodes.new(type="ShaderNodeGroup")
    if shader_group is not None:
        group_node.node_tree = shader_group
    group_node.location = (0, 0)

    if shader_group is not None and "BSDF" in group_node.outputs:
        links.new(output_node.inputs["Surface"], group_node.outputs["BSDF"])

    return mat, tree, group_node


def _build_eye_material(image_cache):
    """The legacy shared eye material: a self-contained group with the eye baked in.

    Kept only as a fallback for exports whose log carries no material parameters --
    it cannot express a per-character iris colour. See _build_eye_material_from_params.
    """
    mat_name = "mat_eye_R6S_Default_Eye"
    if mat_name in bpy.data.materials:
        return bpy.data.materials[mat_name]
    mat, _tree, _group = _new_group_material(mat_name, EYE_SHADER_NAME)
    return mat


# The eye material's parameter table (shader template 0x7005948D), by position.
# Verified across Ash / Smoke / Maverick: 2 and 3 are identical on all three, 4 and 5
# track the character's actual eye colour. Positions are the identifier because the
# parameter name hashes are a bespoke per-build transform.
EYE_PARAM_SCLERA = 2
EYE_PARAM_VEIN = 3
EYE_PARAM_IRIS = 4
EYE_PARAM_IRIS_INNER = 5
EYE_SHADER_GROUP = "Siege Eye BSDF"


def _is_eye_material(tex):
    """True when this material is the real eye, not the eyelash that shares its atlas.

    Both are typed 'Eye' (they sample the same atlas, so a texture-signature classifier
    cannot separate them), but only the eye carries the iris colour parameters.
    """
    if (tex.get("type") or "").lower() != "eye":
        return False
    params = tex.get("params") or {}
    return all(isinstance(params.get(i), tuple)
               for i in (EYE_PARAM_SCLERA, EYE_PARAM_IRIS, EYE_PARAM_IRIS_INNER))


def _build_eye_material_from_params(mat_uid, tex, textures_dir, image_cache):
    """Eye material wired to the shared atlas plus this character's iris colours."""
    mat_name = f"mat_eye_0x{mat_uid}"
    if mat_name in bpy.data.materials:
        return bpy.data.materials[mat_name]

    mat, tree, group = _new_group_material(mat_name, EYE_SHADER_GROUP)
    if group.node_tree is None:
        # Group missing from the library -- fall back rather than emit a blank material.
        bpy.data.materials.remove(mat)
        return _build_eye_material(image_cache)

    nodes, links = tree.nodes, tree.links
    y = 300
    for tex_stem in tex.get("base", []):
        parsed = _parse_texture_filename(tex_stem)
        if not parsed:
            continue
        _n, tex_type, _u = parsed
        path = _find_texture_on_disk(textures_dir, tex_stem)
        if path is None:
            continue
        img = _load_image_cached(path, image_cache)
        node = nodes.new(type="ShaderNodeTexImage")
        node.image = img
        node.label = tex_type
        node.location = (-620, y)
        y -= 300
        if tex_type == "Diffuse":
            _set_colorspace(img, "sRGB")
            links.new(group.inputs["Eye Atlas"], node.outputs["Color"])
            # The atlas alpha is the iris/pupil mask, not opacity.
            links.new(group.inputs["Atlas Alpha"], node.outputs["Alpha"])
        elif tex_type == "Normal":
            _set_colorspace(img, "Non-Color")
            links.new(group.inputs["Normal"], node.outputs["Color"])

    params = tex.get("params") or {}
    for sock, idx in (("Sclera Color", EYE_PARAM_SCLERA),
                      ("Vein Color", EYE_PARAM_VEIN),
                      ("Iris Color", EYE_PARAM_IRIS),
                      ("Iris Inner Color", EYE_PARAM_IRIS_INNER)):
        v = params.get(idx)
        if isinstance(v, tuple) and sock in group.inputs:
            group.inputs[sock].default_value = (v[0], v[1], v[2], 1.0)
    return mat


# The shared mouth material family. Its diffuse (0x0D8CC62B9F) is unambiguously teeth,
# gums and tongue -- MeshTypeClassifier labels the family "Eyelash", which is simply
# wrong, so match the family rather than trusting the label.
MOUTH_DIFFUSE_PREFIX = "0D8CC62B"


def _is_mouth_material(tex):
    for stem in tex.get("base", []):
        parsed = _parse_texture_filename(stem)
        if parsed and parsed[1] == "Diffuse" and parsed[2].upper().lstrip("0").startswith(MOUTH_DIFFUSE_PREFIX):
            return True
    return (tex.get("type") or "").lower() == "eyelash"


# The shader group's detail-normal inputs, filled in order as detail maps arrive.
DETAIL_NORMAL_INPUTS = ("Normal Detail R", "Normal Detail G", "Normal Detail B")


_CONSTANT_CACHE = {}
_ALPHA_CACHE = {}


def _image_stats(img):
    """(alpha_std, fraction of texels with alpha < 0.5, all-channels-constant)."""
    if img is None:
        return None
    key = img.name
    if key in _ALPHA_CACHE:
        return _ALPHA_CACHE[key]
    w, h = img.size
    stats = None
    if w and h and w * h <= 4096 * 4096 and img.channels >= 4:
        try:
            import numpy as np
            buf = np.empty(w * h * img.channels, dtype=np.float32)
            img.pixels.foreach_get(buf)
            buf = buf.reshape(-1, img.channels)
            a = buf[:, 3]
            stats = (float(a.std()), float((a < 0.5).mean()),
                     bool(np.all(buf.max(axis=0) - buf.min(axis=0) < 1e-4)))
        except Exception:
            stats = None
    _ALPHA_CACHE[key] = stats
    return stats


def _alpha_is_opacity_mask(img):
    """True when a diffuse alpha is a real cutout, not a flat 1.0.

    Measured on Ash: skin/head and mouth diffuses are alpha = 1.0 everywhere, while
    hair (70 % of texels at 0) and the eyelash half of the eye atlas are genuine
    cutouts. Only the latter should drive transparency.
    """
    st = _image_stats(img)
    if st is None:
        return False
    alpha_std, frac_cut, _const = st
    return alpha_std > 0.02 and frac_cut > 0.02


def _enable_alpha_blend(mat):
    """Turn on transparency in a way that works on both EEVEE generations."""
    try:
        mat.surface_render_method = 'DITHERED'   # Blender 4.2+ / EEVEE Next
        return
    except (AttributeError, TypeError):
        pass
    try:
        mat.blend_method = 'HASHED'
        mat.shadow_method = 'HASHED'
    except (AttributeError, TypeError):
        pass


def _is_constant_image(img):
    """True when every texel is identical -- an unset slot written as a flat placeholder.

    Hair is the case that matters: its Specular is 128x256 of solid (255, 255, 127),
    i.e. metalness 1.0 and gloss 1.0, which renders as a chrome mirror. Feeding that in
    is what makes hair look glass-smooth, and it is why inverting gloss appeared to fix
    hair while breaking the body -- the body's map is real data, the hair's is not.
    """
    if img is None:
        return False
    key = img.name
    if key in _CONSTANT_CACHE:
        return _CONSTANT_CACHE[key]
    st = _image_stats(img)
    result = bool(st[2]) if st else False
    _CONSTANT_CACHE[key] = result
    return result


def _build_material(mat_uid, base_stems, detail_maps, textures_dir, image_cache, tex=None):
    """
    Create (or reuse) the Blender material for one game material.

    Dispatches on what the material is: the eye gets the eye group driven by its own
    iris-colour parameters, the shared mouth material gets the teeth preset, and
    everything else gets the Character BSDF with its base textures
    (Diffuse/Normal/Specular/ColorMask) plus detail normals routed to Normal Detail
    R/G/B by ColorMask channel through a Mapping node carrying that slot's tiling.

    `tex` is the material's full log entry (type / shader / params); base_stems and
    detail_maps are passed separately because the caller may have filtered them.
    """
    tex = dict(tex or {})
    tex.setdefault("base", base_stems)

    # Route by what the material IS, not by which mesh it sits on. The old rule keyed on
    # the mesh UID, so every island of the eye mesh got the eye group -- including the
    # teeth, which are a separate island with their own full texture set.
    if _is_eye_material(tex):
        return _build_eye_material_from_params(mat_uid, tex, textures_dir, image_cache)

    is_mouth = _is_mouth_material(tex)
    mat_name = f"mat_teeth_0x{mat_uid}" if is_mouth else f"mat_0x{mat_uid}"
    if mat_name in bpy.data.materials:
        return bpy.data.materials[mat_name]

    mat, tree, group_node = _new_group_material(mat_name, CHARACTER_SHADER_NAME)
    nodes = tree.nodes
    links = tree.links
    shader_group = group_node.node_tree

    # Teeth/gums/tongue preset: same shader as skin, but the mouth is translucent, and
    # this material is a shared default so the preset applies to every character.
    if is_mouth and shader_group is not None and "Subsurface" in group_node.inputs:
        group_node.inputs["Subsurface"].default_value = 0.15

    # The group multiplies Specular B into the base colour as "cavity". B is not cavity
    # (see TEXTURE_TYPE_MAPPING): on a head it is ~0.15 across the face, which rendered
    # the skin at a fifth of its albedo.
    if "Cavity Strength" in group_node.inputs:
        group_node.inputs["Cavity Strength"].default_value = 0.0

    y_offset = 400
    for tex_stem in base_stems:
        parsed = _parse_texture_filename(tex_stem)
        if not parsed:
            continue
        _name, tex_type, _uid = parsed

        filepath = _find_texture_on_disk(textures_dir, tex_stem)
        if filepath is None:
            print(f"[PrismImport] Missing texture file for {tex_stem}")
            continue

        img = _load_image_cached(filepath, image_cache)

        input_name, colorspace, alpha_mode, is_normal = TEXTURE_TYPE_MAPPING.get(
            tex_type, (None, "Non-Color", "NONE", False)
        )

        # Skip tiny ripped constant textures entirely — they carry no real data.
        if _is_ripped(img):
            continue
        # A flat placeholder carries no data either, whatever its resolution. Leaving the
        # group input unconnected lets its default apply (non-metal, mid roughness),
        # which is far closer than the placeholder's metal=1 / roughness=0.
        if tex_type == "Specular" and _is_constant_image(img):
            print(f"[PrismImport] {tex_stem}: flat placeholder Specular, leaving default")
            continue

        _set_colorspace(img, colorspace)
        try:
            img.alpha_mode = alpha_mode
        except Exception:
            pass

        tex_node = nodes.new(type="ShaderNodeTexImage")
        tex_node.image = img
        tex_node.label = tex_type
        tex_node.location = (-950, y_offset)
        y_offset -= 280

        if (
            input_name is None
            or shader_group is None
            or input_name not in group_node.inputs
        ):
            continue

        # Every input, normals included, takes the image Color directly: the Character
        # BSDF group already handles normal decoding/mapping internally, so inserting a
        # Normal Map node here would decode twice and flatten the result.
        links.new(group_node.inputs[input_name], tex_node.outputs["Color"])

        # Hair cards and eyelashes are cut out in the diffuse alpha. Wire it only when
        # the alpha really is a mask -- most character diffuses are opaque with a few
        # stray zero texels, and connecting those would punch pinholes in solid geometry.
        if (tex_type == "Diffuse" and "Alpha Input" in group_node.inputs
                and _alpha_is_opacity_mask(img)):
            links.new(group_node.inputs["Alpha Input"], tex_node.outputs["Alpha"])
            _enable_alpha_blend(mat)

    _wire_detail_maps(
        mat, tree, group_node, detail_maps, textures_dir, image_cache, y_offset
    )

    return mat


def _wire_detail_maps(mat, tree, group_node, detail_maps, textures_dir, image_cache, y_offset):
    """
    Wire each detail map into the Normal Detail R/G/B input its ColorMask channel
    names. Detail normals tile, so each gets a Texture Coordinate -> Mapping node
    scaled by that slot's tiling factor.

    The channel is authoritative, not arrival order: a material can bind slots G and
    B while leaving R unset, and packing them in order would put both on the wrong
    mask channel. Legacy logs carry no channel (chan is None) and fall back to
    filling the inputs in order.
    """
    if not detail_maps or group_node.node_tree is None:
        return
    nodes = tree.nodes
    links = tree.links
    shader_group = group_node.node_tree

    used = set()
    for entry in detail_maps:
        tex_stem, sx, sy, chan, kind = entry
        # The shader group exposes detail NORMAL inputs only; an albedo-detail slot
        # (never bound on any character measured) has nowhere to go.
        if kind != "normal":
            continue
        if chan is None:
            chan = next((i for i in range(len(DETAIL_NORMAL_INPUTS)) if i not in used), None)
        if chan is None or chan >= len(DETAIL_NORMAL_INPUTS) or chan in used:
            continue
        input_name = DETAIL_NORMAL_INPUTS[chan]
        if input_name not in group_node.inputs:
            continue
        used.add(chan)

        filepath = _find_texture_on_disk(textures_dir, tex_stem)
        if filepath is None:
            print(f"[PrismImport] Missing detail texture {tex_stem}")
            continue
        img = _load_image_cached(filepath, image_cache)
        if _is_ripped(img):
            continue
        try:
            _set_colorspace(img, "Non-Color")
        except Exception:
            pass

        tex_node = nodes.new(type="ShaderNodeTexImage")
        tex_node.image = img
        tex_node.label = f"Detail {input_name[-1]}"
        tex_node.location = (-950, y_offset)
        y_offset -= 280

        # Shared TexCoord -> Mapping chain scaled by this detail map's UV tiling.
        tex_coord = nodes.new(type="ShaderNodeTexCoord")
        tex_coord.location = (-1550, y_offset + 140)
        mapping = nodes.new(type="ShaderNodeMapping")
        mapping.location = (-1350, y_offset + 140)
        try:
            mapping.inputs["Scale"].default_value = (sx, sy, 1.0)
        except Exception:
            pass
        links.new(mapping.inputs["Vector"], tex_coord.outputs["UV"])
        links.new(tex_node.inputs["Vector"], mapping.outputs["Vector"])

        links.new(group_node.inputs[input_name], tex_node.outputs["Color"])


def _import_obj(filepath):
    """Import a Wavefront OBJ and return the list of newly created mesh objects."""
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=filepath, forward_axis="Y", up_axis="Z")
    after = set(bpy.data.objects)
    return [o for o in (after - before) if o.type == "MESH"]


def _object_index(obj_name):
    """Extract the trailing objectN index from an imported OBJ sub-object name."""
    m = re.search(r"object(\d+)", obj_name)
    return int(m.group(1)) if m else None


# Minimum weight to bother creating a vertex-group entry for (skip near-zero noise).
MIN_BONE_WEIGHT = 0.0001


def _bone_group_name(token):
    """
    Normalise a .weights bone token to a vertex-group / bone name. Modern exports
    use 0x-hex nameCRCs (stable across meshes + the skeleton); older exports used
    raw decimal ordinals. Either way the name is bone_<token>.
    """
    t = token.strip()
    if t.lower().startswith("0x"):
        return f"bone_{t.upper()[2:]}"
    return f"bone_{t}"


def _parse_weights_file(weights_path):
    """
    Parse a Prism .weights sidecar. Returns a list indexed by (vertexIndex-1):
        weights[i] = [(bone_group_name, weight), ...]   (weight>0)
    Returns None if the file is missing/empty. Bone tokens are nameCRCs (0x-hex)
    in modern exports — the same key the skeleton uses — so vertex groups bind to
    the armature bones by name.

    The influence count is read from the row width, not assumed: most meshes carry 4 per
    vertex, but a head with a dense facial rig carries 8 (see the exporter's
    CompiledMeshObject.ReadTrailingTables). A row is
    `vertexIndex bone0..bone{k-1} weight0..weight{k-1}`, so k = (columns - 1) / 2.
    """
    if not os.path.exists(weights_path):
        return None
    rows = []
    with open(weights_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            k = (len(parts) - 1) // 2
            if k < 1 or len(parts) < 1 + 2 * k:
                continue
            try:
                names = [_bone_group_name(t) for t in parts[1:1 + k]]
                wts = [float(t) for t in parts[1 + k:1 + 2 * k]]
            except (ValueError, IndexError):
                continue
            influences = [
                (names[j], wts[j]) for j in range(k) if wts[j] > MIN_BONE_WEIGHT
            ]
            rows.append(influences)
    return rows if rows else None


def _build_obj_position_index(obj_path):
    """
    Read the OBJ's 'v' lines in order. Returns (pos_to_global, kdtree, coords):
      - pos_to_global: dict rounded-pos-key -> 0-based global vertex index (fast exact path)
      - kdtree: a mathutils.kdtree.KDTree over all vertex positions for nearest-neighbour
        fallback (Blender merges/reindexes verts and float rounding can miss the exact key)
      - coords: list of (x,y,z) per global index, so a KD hit maps back to its weight row.
    """
    import mathutils  # type: ignore

    coords = []
    with open(obj_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                parts = line.split()
                try:
                    coords.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except (ValueError, IndexError):
                    coords.append((0.0, 0.0, 0.0))

    pos_to_global = {}
    kd = mathutils.kdtree.KDTree(len(coords))
    for gi, c in enumerate(coords):
        key = (round(c[0], 4), round(c[1], 4), round(c[2], 4))
        if key not in pos_to_global:
            pos_to_global[key] = gi
        kd.insert(mathutils.Vector(c), gi)
    kd.balance()
    return pos_to_global, kd, coords


def _apply_weights_to_object(obj, weights, pos_index):
    """
    Create vertex groups (named by bone key) and assign per-vertex weights. Blender
    reindexes/merges vertices per imported object, so each mesh vertex is mapped back
    to its global OBJ index by position: an exact rounded-key lookup first, then a
    KD-tree nearest-neighbour fallback (handles float-rounding misses).
    """
    if not weights:
        return 0
    pos_to_global, kd, _coords = pos_index
    mesh = obj.data
    groups = {}  # bone group name -> VertexGroup

    def group_for(bone_name):
        g = groups.get(bone_name)
        if g is None:
            g = obj.vertex_groups.get(bone_name) or obj.vertex_groups.new(name=bone_name)
            groups[bone_name] = g
        return g

    assigned = 0
    for v in mesh.vertices:
        co = v.co
        key = (round(co.x, 4), round(co.y, 4), round(co.z, 4))
        gi = pos_to_global.get(key)
        if gi is None:
            # Nearest-neighbour fallback (handles rounding / merge mismatches).
            _co, gi, dist = kd.find(co)
            if gi is None or dist > 1e-3:
                continue
        if gi >= len(weights):
            continue
        for (bone_name, w) in weights[gi]:
            group_for(bone_name).add([v.index], w, "REPLACE")
            assigned += 1
    return assigned


# ---------------------------------------------------------------------------
# Skeleton / armature (Tier 2): _skeleton.txt holds one row per bone:
#   boneIndex parentIndex nameCrc posX posY posZ rot0..rot6
# The per-vertex .weights reference bones by boneIndex, and the vertex groups
# are named bone_<boneIndex> — so an armature whose bones are also named
# bone_<boneIndex> binds automatically via the Armature modifier.
# ---------------------------------------------------------------------------

def _parse_skeleton_file(skel_path):
    """
    Return {bone_name: {"parent": parent_bone_name|None, "pos": (x,y,z),
                        "quat": (x,y,z,w), "pose": (x,y,z), "posequat": (x,y,z,w)}}
    or None.

    Modern format (crc-keyed):  boneCrc parentCrc posXYZ quatXYZW [poseXYZ poseQuatXYZW]
    Legacy format (index-keyed): boneIndex parentIndex nameCrc posXYZ quatXYZW
    Bone names are bone_<token> so they match the .weights vertex groups.

    pos/quat are the REST pose -- the mesh's canonical bind, which is what the .obj
    vertices are authored against. The optional trailing 7 columns are THIS character's
    pose for the same bone. They differ only where the character re-positions a mesh
    shared game-wide: the eyes and the mouth ride on one such mesh, so without applying
    the pose every character wears the same canonical eyes and teeth. Exports written
    before the pose columns existed simply have no "pose" key.
    """
    if not os.path.exists(skel_path):
        return None
    # Detect format from the header comment (crc-keyed has "boneCrc" in the columns line).
    crc_keyed = True
    with open(skel_path, "r", encoding="utf-8", errors="ignore") as f:
        head = f.read(400)
        if "boneIndex" in head and "boneCrc" not in head:
            crc_keyed = False

    bones = {}
    with open(skel_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            p = s.split()
            pose = posequat = None
            try:
                if crc_keyed:
                    # boneCrc parentCrc posX posY posZ quatX quatY quatZ quatW
                    #   [poseX poseY poseZ poseQX poseQY poseQZ poseQW]
                    if len(p) < 9:
                        continue
                    name = _bone_group_name(p[0])
                    parent = _bone_group_name(p[1]) if int(p[1], 16) != 0 else None
                    pos = (float(p[2]), float(p[3]), float(p[4]))
                    quat = (float(p[5]), float(p[6]), float(p[7]), float(p[8]))
                    if len(p) >= 16:
                        pose = (float(p[9]), float(p[10]), float(p[11]))
                        posequat = (float(p[12]), float(p[13]), float(p[14]), float(p[15]))
                else:
                    # boneIndex parentIndex nameCrc posX posY posZ quatX..quatW
                    if len(p) < 6:
                        continue
                    name = _bone_group_name(p[0])
                    parent = _bone_group_name(p[1]) if int(p[1]) >= 0 else None
                    pos = (float(p[3]), float(p[4]), float(p[5]))
                    quat = (float(p[6]), float(p[7]), float(p[8]), float(p[9])) if len(p) >= 10 else (0.0, 0.0, 0.0, 1.0)
            except (ValueError, IndexError):
                continue
            bones[name] = {"parent": parent, "pos": pos, "quat": quat,
                           "pose": pose, "posequat": posequat}
    return bones if bones else None


DEFAULT_BONE_LENGTH = 0.05


def _build_armature(bones, name="Prism_Skeleton", connect=False):
    """
    Build an armature from the parsed skeleton. Each bone's full bind-pose
    transform (world position + rotation quaternion) is applied via edit_bone
    .matrix, so the rig matches the game's bind pose (orientation matters for
    correct skinning). Bones are named bone_<index> to match the vertex groups.

    connect=True stretches every single-child bone's tail to its child and marks
    the child connected, giving a visually connected chain. This REORIENTS those
    bones (their bind axis changes), so it is a cosmetic/rigging convenience and is
    OFF by default — deformation itself binds by vertex group, not bone length.
    """
    import mathutils  # type: ignore

    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    bpy.context.scene.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = arm_data.edit_bones

    created = {}
    for name in bones.keys():
        b = bones[name]
        eb = edit_bones.new(name)

        head = mathutils.Vector(b["pos"])
        qx, qy, qz, qw = b["quat"]
        # Blender Quaternion is (w, x, y, z).
        quat = mathutils.Quaternion((qw, qx, qy, qz))
        if quat.magnitude < 1e-6:
            quat = mathutils.Quaternion((1, 0, 0, 0))
        quat.normalize()

        # Small uniform bone length: the bind pose for skinning only needs each bone's
        # head position + orientation correct. Stretching to children makes a messy,
        # wrong-looking rig and doesn't change deformation (the Armature modifier binds
        # by vertex group, not bone length/connection).
        length = DEFAULT_BONE_LENGTH

        mat = quat.to_matrix().to_4x4()
        mat.translation = head
        eb.head = head
        eb.tail = head + mathutils.Vector((0, length, 0))  # placeholder before .matrix
        eb.matrix = mat
        eb.length = length
        created[name] = eb

    # Parent (second pass; bones stay DISCONNECTED so heads keep their bind position).
    for name, b in bones.items():
        p = b["parent"]
        if p and p in created and p != name:
            created[name].parent = created[p]
            created[name].use_connect = False

    # Optional: connect single-child chains (parent tail -> child head). Skips branch
    # points (>1 child) and leaves so only clean chains (spine/fingers/limbs) connect.
    if connect:
        child_names = {}
        for nm, b in bones.items():
            p = b["parent"]
            if p and p in created and p != nm:
                child_names.setdefault(p, []).append(nm)
        for nm, kids in child_names.items():
            if len(kids) != 1:
                continue
            peb = created.get(nm)
            ceb = created.get(kids[0])
            if peb is None or ceb is None:
                continue
            if (ceb.head - peb.head).length < 1e-5:
                continue
            peb.tail = ceb.head
            ceb.use_connect = True

    bpy.ops.object.mode_set(mode="OBJECT")
    return arm_obj


# ---------------------------------------------------------------------------
# Bone collections
#
# The forge stores no bone NAMES, only the CRC32 of each name, which is why every
# bone here is called bone_<CRC>. The engine's own bone list is mirrored in
# RainbowForge/Model/BoneId.cs -- each enum member's VALUE is the CRC of its own
# member name -- so that file doubles as the CRC -> name table. Regenerate the block
# below if that enum ever grows; nothing else here depends on the names.
# ---------------------------------------------------------------------------

_BONE_NAME_TABLE = """
B6C65665 Root|DED10611 Hips|530EC1CB Spine|8F39FA4E Spine1|1630ABF4 Spine2
8023796D Neck|07C159A2 Head|F60647E5 RightShoulder|6BB3F727 RightArm
7257A1AA RightForeArm|75F94D30 RightHand|9829BECF RightHandThumb1
0120EF75 RightHandThumb2|7627DFE3 RightHandThumb3|DBF635C5 RightHandRing1
42FF647F RightHandRing2|35F854E9 RightHandRing3|45445772 RightHandMiddle1
DC4D06C8 RightHandMiddle2|AB4A365E RightHandMiddle3|FEBAC1B3 RightHandIndex1
67B39009 RightHandIndex2|10B4A09F RightHandIndex3|2A0D8811 RightHandCup
147BB2DF RightHandPinky1|8D72E365 RightHandPinky2|FA75D3F3 RightHandPinky3
637D11C4 R_RightForeArmRoll_U|09CD3136 R_RightForeArmRoll_D|EE3F1CCB C_RightElbow
FEE9F290 R_RightArmRoll_U|9459D262 R_RightArmRoll_D|DFFF7B8D Fx_Temple_R
25F046EE Fx_Temple_L|40498122 Fx_Nose_R|CD418CD7 Fx_Nose_M|BA46BC41 Fx_Nose_L
5E0F376E Fx_Mouth_UR|A4000A0D Fx_Mouth_UL|DEA4100C Fx_Mouth_U|40C085AF Fx_Mouth_R
BACFB8CC Fx_Mouth_L|A363C4EB Fx_Jaw_R1|5DA6E280 Fx_Jaw_M|942592D4 Fx_Tongue
0DD6147E Fx_Mouth_DR|F7D9291D Fx_Mouth_DL|B41430FE Fx_Mouth_D|3A6A9551 Fx_Jaw_R2
EE2BAA8E Fx_Jaw_L2|7D0616F6 Fx_Chin_R|F00E1B03 Fx_Chin_M|87092B95 Fx_Chin_L
7722FB34 Fx_Jaw_L1|FDDD5CD4 Fx_Eyelid_RU|976D7C26 Fx_Eyelid_RD|299C630B Fx_Eyelid_LU
432C43F9 Fx_Eyelid_LD|F7774FCD Fx_Eyebrow_R2|6E7E1E77 Fx_Eyebrow_R1
977AED75 Fx_Eyebrow_M|23367012 Fx_Eyebrow_L2|BA3F21A8 Fx_Eyebrow_L1
D8F170CA Fx_Eye_R|22FE4DA9 Fx_Eye_L|5435BA4F Fx_Ear_R|AE3A872C Fx_Ear_L
29515A3F Fx_Cheek_R4|B735CF9C Fx_Cheek_R3|C032FF0A Fx_Cheek_R2|593BAEB0 Fx_Cheek_R1
FD1065E0 Fx_Cheek_L4|6374F043 Fx_Cheek_L3|1473C0D5 Fx_Cheek_L2|8D7A916F Fx_Cheek_L1
76C700FC Fx_Throat|99E55949 Fx_Chin_Up|2D4660A8 LeftShoulder|EB830ADA LeftArm
D190180D R_LeftArmRoll_U|BB2038FF R_LeftArmRoll_D|89B93A80 LeftForeArm
DD0E822D R_LeftForeArmRoll_U|B7BEA2DF R_LeftForeArmRoll_D|B675F36C LeftHand
78F4E1AC LeftHandThumb1|E1FDB016 LeftHandThumb2|96FA8080 LeftHandThumb3
D34048CF LeftHandRing1|4A491975 LeftHandRing2|3D4E29E3 LeftHandRing3
911FBACF LeftHandMiddle1|0816EB75 LeftHandMiddle2|7F11DBE3 LeftHandMiddle3
1E679ED0 LeftHandIndex1|876ECF6A LeftHandIndex2|F069FFFC LeftHandIndex3
D1E3133B LeftHandCup|F4A6EDBC LeftHandPinky1|6DAFBC06 LeftHandPinky2
1AA88C90 LeftHandPinky3|32FE6838 C_LeftElbow|757F1291 RightUpLeg|863D09FC RightLeg
9B14362C RightFoot|42BE0FCB RightToeBase|72D84BD7 C_RightKnee|D6697E1E C_RightAss
07CDC2C3 L_RightThigh|176183F0 LeftUpLeg|060DF401 LeftLeg|58988870 LeftFoot
B95094E1 LeftToeBase|65A177F9 C_LeftKnee|18EB1380 C_LeftAss|3A60DD87 L_LeftThigh
A9CEFD4A CameraNode|AC67F5A7 GroundNode|22E53C03 BONE_ROOT_OFFSET
9E64F428 BONE_FX_TEETH_DOWN|6AEE6DF1 BONE_R65_BA_ROOT|2897BDCB BONE_R65_BA_L_HAND
114BDE20 BONE_R65_BA_R_HAND|C393AE8E BONE_R65_BA_R_UPPERARM|C12857FB BONE_R65_BA_CAMERA
02325860 BONE_SOCKET_ATT_GUN3RD|26322F6A BONE_SOCKET_ATT_SLEDGEHAMMER
37A967D3 BONE_SOCKET_ATT_CANISTER
FD854A47 Fx_Jaw|3C6520AD Fx_Nostril_L|C66A1DCE Fx_Nostril_R|D27C95DF L_Neck
78819F37 L_LeftArmRoll_U|30818F43 L_LeftForeArmRoll_D|78CB9BB6 L_LeftWrist
384C3AA5 L_RightArmRoll_U|F0F3D174 L_RightForeArmRoll_D|456684F2 L_RightWrist
49B0BCE7 Fx_Masseter_L|B3BF8184 Fx_Masseter_R|B25E6C19 Fx_Nasalis_L
4851517A Fx_Nasalis_R|034CD5BC O_LeftArmRoll_D|7B34AEA5 O_RightArmRoll_D
F9672918 O_LeftForeArmRoll_U|35EABC4C O_RightForeArmRoll_U|38E4FAFF O_LeftElbow
EB35514A O_RightElbow|7F9D9777 O_LeftKnee|78C2D910 O_RightKnee|FEDFE094 O_LeftAss
CC559E90 O_RightAss|5A8083E3 T_LeftShoulder_01|C389D259 T_LeftShoulder_02
77F85826 T_RightShoulder_01|EEF1099C T_RightShoulder_02|285E3E4E X_LeftClavicle
8BF083FF X_RightClavicle
"""

# NOTE on provenance: a bone's key is CRC32 of its name, so a name is only "known" once some
# candidate string hashes to the CRC. 118 of the 128 BoneId.cs entries verify that way; the
# ten BONE_* entries do NOT (CRC32 of those member names does not equal their value) — they
# are descriptive labels somebody pinned on important CRCs, not recovered names. The block
# appended above WAS recovered by hashing candidates, which is how the L_* corrective family
# turned up. See _sync_bone_names for what this is used for.

BONE_NAMES = {}
for _entry in _BONE_NAME_TABLE.replace("\n", "|").split("|"):
    _entry = _entry.strip()
    if _entry:
        _crc, _nm = _entry.split()
        BONE_NAMES[f"bone_{_crc}"] = _nm

# Collection names. "Non Used" is the trashcan and starts hidden.
COLL_BASE = "Base"
COLL_FACE = "Facial Controls"
COLL_HAND = "Hand Controls"
COLL_CORRECTIVE = "Corrective"
COLL_EXTRA = "Extras"
COLL_UNUSED = "Non Used"

_ROOT_BONE = "bone_B6C65665"                              # Root


_HEAD_BONE = "bone_07C159A2"                              # Head
_HAND_BONES = ("bone_B675F36C", "bone_75F94D30")          # LeftHand, RightHand


def _bone_category(bone_name, ancestry):
    """Which collection a bone belongs in, decided by ANCESTRY rather than by name.

    Y11S2 does not use the classic Fx_* facial rig that BoneId.cs describes. Measured on a
    real character: of the 53 bones under Head only TWO are named (Fx_Eye_L/R) — the rest
    are unnamed CRCs — so a name-based rule put 2 bones in the face collection and swept
    the other 51 into Extras. Every bone that deforms the head mesh IS under Head, which
    makes the subtree the reliable discriminator. Same for the hands (88 bones, mostly
    unnamed helpers under the two named wrist joints).

    `ancestry` is [bone, parent, ..., root], so a bone matches its own anchor too.
    """
    if _HEAD_BONE in ancestry:
        return COLL_FACE
    if any(h in ancestry for h in _HAND_BONES):
        return COLL_HAND
    n = BONE_NAMES.get(bone_name)
    if n is not None:
        # The engine marks every joint helper with a SINGLE-LETTER prefix: R_ (roll),
        # C_ (corrective), L_, and — recovered by hashing — O_ (O_LeftElbow, O_RightKnee,
        # O_LeftAss), T_ (T_LeftShoulder_01 twists) and X_ (X_LeftClavicle). Matching the
        # shape rather than a fixed letter set means a family we have not cracked yet still
        # sorts correctly. The underscore is what separates these from the real limbs,
        # which spell out Left/Right; "Fx_" has its underscore at [2], not [1].
        if len(n) > 2 and n[1] == "_":
            return COLL_CORRECTIVE
        if not n.startswith("BONE_SOCKET_") and not n.startswith("BONE_R65_BA_"):
            return COLL_BASE      # the named humanoid chain: root, spine, limbs, feet
    # Anything left is rigging that ships with an outfit rather than with the body:
    # hair, clothing, pouches, weapon sockets and attachment rigs.
    return COLL_EXTRA


def _assign_bone_collections(arm_obj, deforming):
    """Sort the armature's bones into Base / Facial / Hand / Extras / Non Used.

    `deforming` is the set of bone names some bound mesh actually has a vertex group for.
    A bone goes to "Non Used" when it deforms nothing AND nothing below it in the
    hierarchy deforms either -- dropping a parent whose child still drives a mesh would
    hide a bone the rig genuinely needs. Everything else is categorised by name.

    Returns {collection_name: count}.
    """
    arm = arm_obj.data
    bones = arm.bones
    if not bones:
        return {}

    # "Used" propagates UP: mark each deforming bone and every ancestor of it.
    used = set()
    for b in bones:
        if b.name not in deforming:
            continue
        cur, guard = b, 0
        while cur is not None and guard < 512:
            if cur.name in used:
                break
            used.add(cur.name)
            cur = cur.parent
            guard += 1

    def ancestry(bone):
        out, cur, guard = [], bone, 0
        while cur is not None and guard < 512:
            out.append(cur.name)
            cur = cur.parent
            guard += 1
        return out

    # CORRECTIVE bones: the unnamed joint helpers (twist / roll / volume-preservation) that
    # hang directly off a named limb or spine joint. Measured on one body mesh, all 25 of the
    # bones that would otherwise fall into Extras were of this kind, and every one of them was
    # parented to ForeArm / Arm / Shoulder / UpLeg / Spine2 / Hips, or to another such helper.
    #
    # Root is deliberately excluded as an attachment point: merging re-parents each outfit
    # sub-rig's ROOT onto the body root, so allowing Root here would sweep all the hair and
    # clothing rigging in with the correctives. Outfit bones reach the skeleton through their
    # own unnamed sub-rig root and therefore stay in Extras.
    corrective = set()
    for b in sorted(bones, key=lambda x: len(ancestry(x))):     # parents before children
        anc = ancestry(b)
        cat_b = _bone_category(b.name, anc)
        if BONE_NAMES.get(b.name):
            # A NAMED corrective (R_/L_/C_) still has to join the set, or its unnamed
            # children stop chaining and fall through to Extras.
            if cat_b == COLL_CORRECTIVE:
                corrective.add(b.name)
            continue
        if cat_b != COLL_EXTRA:
            continue
        parent = b.parent
        if parent is None:
            continue
        if parent.name in corrective:
            corrective.add(b.name)
        elif (parent.name != _ROOT_BONE and BONE_NAMES.get(parent.name)
              and _bone_category(parent.name, ancestry(parent)) == COLL_BASE):
            corrective.add(b.name)

    buckets = {}
    for b in bones:
        if b.name not in used:
            cat = COLL_UNUSED
        elif b.name in corrective:
            cat = COLL_CORRECTIVE
        else:
            cat = _bone_category(b.name, ancestry(b))
        buckets.setdefault(cat, []).append(b)

    # Stable, readable order in the UI; only create collections that have members.
    counts = {}
    for name in (COLL_BASE, COLL_FACE, COLL_HAND, COLL_CORRECTIVE, COLL_EXTRA, COLL_UNUSED):
        members = buckets.get(name)
        if not members:
            continue
        coll = arm.collections.get(name) if hasattr(arm.collections, "get") else None
        if coll is None:
            coll = arm.collections.new(name)
        for b in members:
            coll.assign(b)
        if name == COLL_UNUSED:
            coll.is_visible = False
        counts[name] = len(members)
    return counts


def _sync_bone_names(arm_obj, objects):
    """Rename `bone_<CRC>` to the engine's real bone name wherever we know it.

    The forge never stores bone names, only CRC32 of the name, so a name is "known" only
    because some candidate string was found to hash to that CRC. That makes the mapping
    version-INDEPENDENT: CRC32("Head") is 0x07C159A2 in every build, so this cannot drift
    the way the per-patch record magics do.

    Vertex groups are renamed in lockstep. The Armature modifier binds a group to a bone by
    NAME, so renaming a bone without its groups would silently unbind the mesh — the exact
    failure mode we just spent a fix on. Each bone also keeps its CRC in a `prism_crc`
    custom property so the original key stays recoverable.

    Returns (renamed_bones, renamed_groups).
    """
    mapping = {}
    for b in arm_obj.data.bones:
        real = BONE_NAMES.get(b.name)
        if real and real != b.name and real not in arm_obj.data.bones:
            mapping[b.name] = real

    nb = ng = 0
    for old, new in mapping.items():
        b = arm_obj.data.bones.get(old)
        if b is None:
            continue
        b["prism_crc"] = old[5:]
        b.name = new
        nb += 1
    for o in objects:
        for g in o.vertex_groups:
            new = mapping.get(g.name)
            if new:
                g.name = new
                ng += 1
    return nb, ng


def _apply_character_pose(arm_obj, bones):
    """Pose the armature to the character's own bind, leaving REST at the canonical bind.

    The .obj vertices are authored against the CANONICAL bind, and Blender's armature
    modifier deforms each vertex by pose.matrix @ bone.matrix_local^-1 -- which is exactly
    the boneWorld_character * invBind_canonical the game applies. So simply posing the rig
    puts every shared mesh where this character wears it, with no per-mesh special casing:
    the eyes and the mouth ride on one game-wide mesh and are placed purely by these bones.

    Bones the character does not move have pose == rest and contribute nothing, so this is
    a no-op for the ~99 % of the rig that already agreed.

    Returns the number of bones actually displaced.
    """
    import mathutils

    if not any(b.get("pose") and b.get("posequat") for b in bones.values()):
        return 0

    def world_matrix(pos, quat):
        qx, qy, qz, qw = quat
        q = mathutils.Quaternion((qw, qx, qy, qz))
        if q.magnitude < 1e-6:
            q = mathutils.Quaternion((1, 0, 0, 0))
        q.normalize()
        m = q.to_matrix().to_4x4()
        m.translation = mathutils.Vector(pos)
        return m

    # Parents before children: matrix_basis is expressed relative to the parent's POSED
    # frame, so a child solved first would be rebased the moment its parent moves.
    order, seen = [], set()

    def visit(name, guard=0):
        if name in seen or guard > 512:
            return
        seen.add(name)
        parent = bones.get(name, {}).get("parent")
        if parent in bones:
            visit(parent, guard + 1)
        order.append(name)

    for name in bones:
        visit(name)

    effective = {}   # bone -> its final armature-space matrix, posed or inherited
    moved = 0
    for name in order:
        pb = arm_obj.pose.bones.get(name)
        if pb is None:
            continue
        rest = pb.bone.matrix_local
        parent = pb.parent
        if parent is not None and parent.name in effective:
            inherited = (effective[parent.name]
                         @ parent.bone.matrix_local.inverted()
                         @ rest)
        else:
            inherited = rest

        b = bones.get(name) or {}
        if b.get("pose") and b.get("posequat"):
            target = world_matrix(b["pose"], b["posequat"])
        else:
            # No pose recorded -> inherit the parent's, i.e. leave basis at identity.
            effective[name] = inherited
            continue

        basis = inherited.inverted() @ target
        pb.matrix_basis = basis
        effective[name] = target
        if basis.translation.length > 1e-6 or abs(basis.to_quaternion().angle) > 1e-5:
            moved += 1
    return moved


def _parent_to_armature(obj, arm_obj):
    """Add an Armature modifier binding obj to arm_obj (vertex groups by name)."""
    mod = obj.modifiers.get("Armature") or obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    mod.use_vertex_groups = True
    # Parent without re-transform (meshes + armature share world space).
    obj.parent = arm_obj


def _build_armatures(export_dir, merge_rigs, connect_bones, apply_pose=True):
    """
    Build the armature(s) for the import. Returns a list of (arm_obj, bone_name_set).
      - merge_rigs=False (default): one armature per _skeleton_<bt>.txt (separate rigs,
        each source BuildTable's own skeleton).
      - merge_rigs=True: one armature from the merged _skeleton.txt.
    Falls back to the merged file when no per-BT rig files are present (older exports).

    apply_pose drives _apply_character_pose. Note the per-BT rigs are the authoritative
    source for it: a wardrobe export carries several head packages and each poses the
    shared eye/teeth mesh to fit its own head, so the merged file can only keep one of
    those answers.
    """
    armatures = []
    if not merge_rigs:
        for skel_path in sorted(glob.glob(os.path.join(export_dir, "_skeleton_*.txt"))):
            bt = os.path.splitext(os.path.basename(skel_path))[0].replace("_skeleton_", "")
            skeleton = _parse_skeleton_file(skel_path)
            if not skeleton:
                continue
            arm = _build_armature(skeleton, name=f"Rig_{bt}", connect=connect_bones)
            if apply_pose:
                _apply_character_pose(arm, skeleton)
            armatures.append((arm, set(skeleton.keys())))
        if armatures:
            return armatures
    # Merged mode (or fallback when no per-BT files exist).
    skeleton = _parse_skeleton_file(os.path.join(export_dir, "_skeleton.txt"))
    if skeleton:
        arm = _build_armature(skeleton, name="Prism_Skeleton", connect=connect_bones)
        if apply_pose:
            _apply_character_pose(arm, skeleton)
        armatures.append((arm, set(skeleton.keys())))
    return armatures


def _best_armature_for_object(obj, armatures):
    """The armature whose bones cover the most of obj's vertex groups — parents a
    head/gear mesh to the rig that actually owns its bones. Single armature -> that one."""
    if not armatures:
        return None
    if len(armatures) == 1:
        return armatures[0][0]
    vg = {g.name for g in obj.vertex_groups}
    best, best_score = None, -1
    for arm, bone_names in armatures:
        score = len(vg & bone_names)
        if score > best_score:
            best_score, best = score, arm
    return best


class NODE_OT_ImportPrismGroup(bpy.types.Operator):
    """Import a Prism Character Group export (folder containing _export.log)."""

    bl_idname = "import_scene.prism_group"
    bl_label = "Prism Character Group (Folder)"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(  # type: ignore
        name="Directory",
        description="Prism export directory containing _export.log",
        subtype="DIR_PATH",
    )
    filter_folder: bpy.props.BoolProperty(default=True, options={"HIDDEN"})  # type: ignore

    merge_rigs: bpy.props.BoolProperty(  # type: ignore
        name="Merge into single armature",
        description="Combine all source rigs into ONE armature (fixed merge: sub-rig roots attach "
                    "to the body root, not to arbitrary nearby bones). Turn OFF to build a SEPARATE "
                    "armature per source BuildTable and parent each mesh to the rig that owns its "
                    "bones — which is also the more accurate source for the character pose, since a "
                    "wardrobe export carries several head packages that each pose the shared "
                    "eye/teeth mesh differently",
        default=True,
    )
    connect_bones: bpy.props.BoolProperty(  # type: ignore
        name="Connect bones",
        description="Stretch each single-child bone's tail to its child for a connected chain. "
                    "Cosmetic — reorients those bones' bind axis; deformation binds by vertex group",
        default=False,
    )
    apply_character_pose: bpy.props.BoolProperty(  # type: ignore
        name="Pose to character",
        description="Pose the rig to THIS character's bind instead of leaving it at the mesh's "
                    "canonical bind. The eyes and the mouth are one mesh shared game-wide, placed "
                    "purely by the face bones, so without this every character wears the same "
                    "canonical eyes and teeth. Rest pose is untouched, so skinning stays correct",
        default=True,
    )
    rename_bones: bpy.props.BoolProperty(  # type: ignore
        name="Use real bone names",
        description="Rename bone_<CRC> to the engine's actual bone name where it is known "
                    "(Head, Spine2, LeftHandIndex1, Fx_Eye_L, ...), renaming the matching vertex "
                    "groups with them. The CRC is kept on each bone as 'prism_crc'. Bones whose "
                    "name has not been recovered keep their bone_<CRC> name",
        default=True,
    )
    bone_collections: bpy.props.BoolProperty(  # type: ignore
        name="Sort bones into collections",
        description="Split each rig into Base / Facial Controls / Hand Controls / Extras, plus a "
                    "'Non Used' collection holding every bone that deforms nothing and has no "
                    "deforming descendant. 'Non Used' is created hidden",
        default=True,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        export_dir = bpy.path.abspath(self.directory)
        if not export_dir or not os.path.isdir(export_dir):
            self.report({"ERROR"}, f"Not a directory: {export_dir}")
            return {"CANCELLED"}

        log_path = os.path.join(export_dir, "_export.log")
        if not os.path.exists(log_path):
            self.report({"ERROR"}, f"_export.log not found in {export_dir}")
            return {"CANCELLED"}

        textures_dir = os.path.join(export_dir, "textures")
        if not os.path.isdir(textures_dir):
            self.report({"ERROR"}, f"textures/ folder not found in {export_dir}")
            return {"CANCELLED"}

        load_shader_groups()
        append_shader_group(CHARACTER_SHADER_NAME)
        append_shader_group(EYE_SHADER_GROUP)
        append_shader_group(EYE_SHADER_NAME)

        meshes, materials_index = _parse_export_log(log_path)
        if not meshes:
            self.report({"WARNING"}, "No mesh entries found in _export.log")
            return {"CANCELLED"}

        image_cache = {}
        material_cache = {}

        imported_objects = 0
        imported_materials = 0
        missing_obj = 0
        count_mismatches = 0
        island_bound_meshes = 0   # meshes bound from the exporter's island->material block
        unbound_islands = 0       # islands whose material had no textures in the log
        weighted_meshes = 0
        all_weighted_objects = []  # objects that got weights, to parent to the armature

        # Build armature(s): default = one per source rig (_skeleton_<bt>.txt); the
        # "Merge into single armature" option uses the merged _skeleton.txt instead.
        # armatures = list of (arm_obj, bone_name_set) for per-mesh parenting below.
        armatures = _build_armatures(export_dir, self.merge_rigs, self.connect_bones,
                                     self.apply_character_pose)

        to_rename = []  # (object, asset name, island index, islands in the mesh)

        def get_base_material(mat_uid, base_stems, detail_maps, tex=None):
            if mat_uid in material_cache:
                return material_cache[mat_uid]
            mat = _build_material(mat_uid, base_stems, detail_maps, textures_dir,
                                  image_cache, tex)
            material_cache[mat_uid] = mat
            nonlocal imported_materials
            imported_materials += 1
            return mat

        for mesh_uid, bt_entries in meshes.items():
            obj_path = os.path.join(export_dir, f"mesh_{mesh_uid}.obj")
            if not os.path.exists(obj_path):
                missing_obj += 1
                print(f"[PrismImport] Missing OBJ: {obj_path}")
                continue

            new_objects = _import_obj(obj_path)
            if not new_objects:
                continue
            imported_objects += len(new_objects)
            # Named after the asset once everything keyed on "mesh_0x<uid>_objectN" is done.
            mesh_name = next((e["name"] for e in bt_entries if e.get("name")), None)
            package = next((e["package"] for e in bt_entries if e.get("package")), None)
            for o in new_objects:
                o["prism_mesh_uid"] = "0x" + mesh_uid
                if _object_index(o.name) is not None:
                    o["prism_island"] = _object_index(o.name)
                if package:
                    o["prism_package"] = package
                if mesh_name:
                    to_rename.append((o, mesh_name, _object_index(o.name), len(new_objects)))

            # Sort sub-objects by their objectN index so index i lines up with the
            # i-th material slot (the OBJ islands are emitted in MatId 0..N-1 order).
            new_objects.sort(key=lambda o: (_object_index(o.name) is None, _object_index(o.name) or 0))

            # Skin weights: load the .weights sidecar (per-vertex bone keys = nameCRCs +
            # weights) and a position index (exact + KD-tree) from the OBJ, then create
            # vertex groups (named bone_<crc>) on every sub-object. With a _skeleton.txt
            # present these bind to the armature's bone_<crc> bones by name.
            # Applied to BOTH the eye and regular meshes.
            weights = _parse_weights_file(os.path.splitext(obj_path)[0] + ".weights")
            pos_index = _build_obj_position_index(obj_path) if weights else None

            def apply_weights(objs):
                if not weights:
                    return
                nonlocal weighted_meshes
                any_assigned = False
                for o in objs:
                    if _apply_weights_to_object(o, weights, pos_index) > 0:
                        any_assigned = True
                        all_weighted_objects.append(o)
                if any_assigned:
                    weighted_meshes += 1

            # NOTE: there is deliberately no per-MESH eye override here any more. The eye
            # mesh also carries the teeth as a separate island, and forcing the eye group
            # onto the whole mesh gave the teeth the eye material. Routing happens per
            # material instead, in _build_material.

            # --- ONE material slot per sub-object. ---
            # Preferred path: the exporter resolved each island's material from the
            # BuildTable, so object i's material is known exactly — no guessing, no
            # manual re-assignment afterwards.
            island_mats = _island_materials_for_mesh(bt_entries)
            if island_mats:
                bound = 0
                for i, obj in enumerate(new_objects):
                    obj.data.materials.clear()
                    mat_uid = island_mats.get(_object_index(obj.name), island_mats.get(i))
                    tex = materials_index.get(mat_uid) if mat_uid else None
                    if not tex or (not tex["base"] and not tex["detail"]):
                        unbound_islands += 1
                        continue
                    obj.data.materials.append(
                        get_base_material(mat_uid, tex["base"], tex["detail"], tex)
                    )
                    bound += 1
                if bound:
                    island_bound_meshes += 1
                apply_weights(new_objects)
                continue

            base_mats = _base_materials_for_mesh(bt_entries)

            if len(base_mats) != len(new_objects):
                # The log gives materials per-BuildTable, not per-object, and the
                # exact object->material permutation is not yet recoverable. When the
                # counts match we pair object[i] -> base_mats[i] (best-guess, stable
                # UID-sorted order). When they differ we still give each object ONE
                # slot (cycling through the available base materials) so the user can
                # fix individual slots, rather than dumping all materials on all.
                count_mismatches += 1
                print(
                    f"[PrismImport] mesh {mesh_uid}: {len(new_objects)} objects but "
                    f"{len(base_mats)} base materials — pairing is best-guess."
                )

            for i, obj in enumerate(new_objects):
                obj.data.materials.clear()
                if base_mats:
                    mat_uid, base_stems, detail_maps, _bt = base_mats[i % len(base_mats)]
                    obj.data.materials.append(
                        get_base_material(mat_uid, base_stems, detail_maps,
                                          materials_index.get(mat_uid))
                    )

            apply_weights(new_objects)

        for o, name, island, count in to_rename:
            o.name = name if count == 1 or island is None else f"{name}_{island}"
            o.data.name = o.name
        named = len({id(o) for o, *_ in to_rename})

        # Parent each weighted mesh to the armature that owns its bones (binds by name).
        # In separate-rig mode this routes a head/gear mesh to its OWN rig, not the body's.
        bound_to = {}
        for o in all_weighted_objects:
            arm = _best_armature_for_object(o, armatures)
            if arm is not None:
                _parent_to_armature(o, arm)
                bound_to.setdefault(arm.name, []).append(o)

        # Sort each rig's bones into collections. This runs only now, because "does this
        # bone deform anything" can only be answered once the meshes are bound.
        coll_counts = {}
        if self.bone_collections:
            for arm, _bone_names in armatures:
                deforming = set()
                for o in bound_to.get(arm.name, ()):
                    deforming.update(g.name for g in o.vertex_groups)
                for name, n in _assign_bone_collections(arm, deforming).items():
                    coll_counts[name] = coll_counts.get(name, 0) + n

        # Renaming happens LAST: collection sorting keys off bone_<CRC>, and the vertex
        # groups have to travel with the bones or the meshes come unbound.
        renamed = 0
        if self.rename_bones:
            for arm, _bone_names in armatures:
                nb, _ng = _sync_bone_names(arm, bound_to.get(arm.name, ()))
                renamed += nb

        msg = (
            f"Prism import: {imported_objects} object(s), "
            f"{imported_materials} material(s), "
            f"{weighted_meshes} weighted mesh(es)"
        )
        if armatures:
            total_bones = sum(len(a.data.bones) for a, _ in armatures)
            mode = "merged" if self.merge_rigs else "separate"
            msg += f", {len(armatures)} {mode} armature(s), {total_bones} bones"
        if coll_counts:
            msg += " [" + ", ".join(f"{k} {v}" for k, v in coll_counts.items()) + "]"
        if renamed:
            msg += f", {renamed} bone(s) given real names"
        if named:
            msg += f", {named} object(s) named after their asset"
        if island_bound_meshes:
            msg += f", {island_bound_meshes} mesh(es) bound per-island from the BuildTable"
        if unbound_islands:
            msg += f", {unbound_islands} island(s) with no texture set in the log"
        if count_mismatches:
            msg += f", {count_mismatches} mesh(es) with best-guess pairing"
        if missing_obj:
            msg += f", {missing_obj} OBJ file(s) missing"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


def menu_func_import(self, context):
    self.layout.operator(
        NODE_OT_ImportPrismGroup.bl_idname,
        text="Prism Character Group (Folder)",
    )


def register():
    bpy.utils.register_class(NODE_OT_ImportPrismGroup)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(NODE_OT_ImportPrismGroup)
