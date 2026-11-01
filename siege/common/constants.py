"""
Shared constants for Siege forge parsing.

Magic numbers, format tables, and entity-classification rules used by every
parsing module. Splitting these out of the original monolithic importer
gives every submodule a single dependency-free place to import from.
"""

# ----- File / Record magics -----

SCIMITAR_FILE_MAGICS = {0x1014FA9957FBAA34, 0x1015FA9957FBAA36, 0x1004FA9957FBAA33}
ENTITY_MAGIC = 0x0984415E
MESH_MAGIC = 0x415D9568
LOADUNIT_MAGIC = 0x943945C4
COMPILED_MESH_MAGIC = 0xFC9E1595
CMO_FILE_TYPE = 0xABEB2DFB
# Top-level asset magics used as per-forge FileType hints (Magic.cs). v31+
# forges zero their metadata table, so a bank's FileType has to be inferred from
# the forge FILENAME instead of read from disk — see file_io.infer_file_type_from_name.
COMPILED_MESH_SHAPE_FILE_TYPE = 0x9231EE0F
COMPILED_SOUND_MEDIA_FILE_TYPE = 0x427411A3
COMPILED_SOUND_BANK_FILE_TYPE = 0x82688E42
# DestructionMeshGroup: in-LoadUnit record that maps a destructible's
# group UID to a concrete Mesh UID. Destructible entities reference
# the DMG UID (not the Mesh UID) inside their payload.
DESTRUCTION_MESH_GROUP_MAGIC = 0xAF53B819
# MaterialTint block magic: each tint block in an entity payload starts with
# this 4-byte value followed by the material UID (8 bytes) and 4 RGBA float
# groups (64 bytes). Only the first RGBA group is the actual tint color;
# the remaining three appear to be engine-internal (e.g. specular tint).
MATERIAL_TINT_MAGIC = 0xD7BF79A4

# ----- Y11S2 (forge v34) re-hashed map magics -----
# !!! THESE MAGICS ARE RE-HASHED BY (ROUGHLY) EVERY GAME PATCH !!!
# The record layout stays identical; only the 4-byte type-magic VALUES change to
# a new, non-derivable set each update (the transform is NOT a bitshift/xor — a
# bespoke table/hash). Values below are the LAST KNOWN set and are only a FAST
# PATH; nothing depends on them being current. Two things keep the addon working
# across a patch with no code change:
#   * forge.magic_registry detects the anchorable types (Mesh, Material,
#     TextureMap, …) from the install's own bytes and shares one cache with Prism;
#   * forge.v34_map.harvest_v34_magics() re-derives the rest (Entity, DMG,
#     LoadUnit, VISUAL) structurally from live map bytes at import time.
# Values below are from the 2026-08-11 patch.
LOADUNIT_MAGIC_V34 = 0x3742BB7F   # LoadUnit — holds entities/meshes (harvested structurally)
WORLD_MAGIC_V34 = 0x27A2FDD9      # World (not re-harvested since Y11S2)
ENTITY_MAGIC_V34 = 0x9E014926     # Entity (harvested structurally when stale)
MESH_MAGIC_V34 = 0xF5C0AFD3       # Mesh — 2026-08-11 (was 0xF96E3642); cmo_uid at magic+21
MATERIAL_MAGIC_V34 = 0x9BFBCAA8   # Material — 2026-08-11 (was 0x3C27BA9C)
TEXTURE_MAP_MAGIC_V34 = 0x3C7E34FD  # TextureMap — 2026-08-11 (was 0x0AA42F34)
BUILD_TABLE_MAGIC_V34 = 0xB4361608  # BuildTable — 2026-08-11 (was 0xF0748227)
ANIMATION_MAGIC_V34 = 0x4E962A98    # Animation — 2026-08-11 (was 0xD9ECB19B)
# DestructionMeshGroup: destructible entities reference the DMG uid; the DMG
# record wraps the Mesh uid at body +12 (v33 layout). Y11S2 value 0xAA4B347C.
DMG_MAGIC_V34 = 0x64AC9BBA
# Visual (render-mesh) component. v34 map entities are AnvilNext serialized
# objects whose inline components are typed by re-hashed magics; the VISUAL
# component carries the render Mesh uid at magic+6 ([magic][01 00][mesh_uid]).
# Present in 99.94% of the entities the linker resolves to a Mesh; absent from
# "logic" nodes (vending/slot machines, triggers, sound emitters) which have no
# visual geometry. Re-hashed per patch; harvested structurally when stale by
# forge.v34_map.harvest_v34_visual_magic.
VISUAL_COMPONENT_MAGIC_V34 = 0x0C595D61

FACE_CHUNK_SIZE = 0x180
TRIS_IN_CHUNK = 64
BIAS = 0x7FFF

VALID_VERT_LENS = {0x18, 0x1C, 0x24, 0x28, 0x2C, 0x5C}

# ----- Material/Texture chain magics -----
# All come from the C# parsers in RainbowForge/RenderPipeline and
# RainbowForge/Image. The TextureSelector magic is the inner field that
# delimits a 28-byte selector record inside a Material payload, which
# we use to scrape texture UIDs without parsing the full Material schema.
#
# Resolution chain in a LoadUnit:
#   Material        (0x85C817C3) -- holds N TextureSelector records
#   TextureSelector (28 bytes)   -- carries TextureMapSpec UID
#   TextureMapSpec  (0x989DC6B2) -- carries TextureMap UID + textureType
#   TextureMap      (0xA2B7E917) -- carries 1..N compiled-texture file UIDs
#                                   in 2 mip sets (typically 5+5 ulongs).
#   <compiled texture entry in *_textures*.forge files>
MATERIAL_MAGIC = 0x85C817C3
TEXTURE_SELECTOR_MAGIC = 0x7D08460D
TEXTURE_MAP_SPEC_MAGIC = 0x989DC6B2
TEXTURE_MAP_MAGIC = 0xA2B7E917
COMPILED_TEXTURE_MAP_MAGIC = 0x13237FE9      # inner magic inside a compiled texture
COMPILED_TEXTURE_MAP_DATA_MAGIC = 0xC30C4B3D  # container id immediately before the DDS payload

# Internal-uid flag: any forge UID with the high byte == 0xF8 is "internal"
# (refers to a record that lives inside the same LoadUnit blob, not a file).
# These never appear in the texture forge index, so we filter them out
# when picking compiled-texture references from a TextureMap mip set.
INTERNAL_UID_HIGH_BYTE = 0xF8

# Per-resolution compiled texture file_type magics, in low->ultra order.
# A single texture UID family appears across multiple *_textures*.forge
# files, one per resolution. The forge filename suffix matches:
#     _textures0 -> Low, _textures1 -> Med, _textures2 -> High,
#     _textures3 -> Ultra, _textures4 -> Future (when present).
COMPILED_TEXTURE_FILE_TYPES = (
    0xD7B5C478,  # Low
    0xF9C80707,  # Med
    0x59CE4D13,  # High
    0x9F492D22,  # Ultra
    0x3876CCDF,  # Future
)
COMPILED_TEXTURE_FILE_TYPE_SET = set(COMPILED_TEXTURE_FILE_TYPES)

# ----- DDS format tables -----
# Texture format -> (FOURCC code, is_dx10) for DDS header generation.
# The FOURCC path produces a legacy DDS that every loader (including
# Blender's native DDS importer) handles. BC4/BC6/BC7 don't have a stable
# legacy FOURCC, so we emit a DX10 header for them and let the DXGI
# format field carry the meaning.
#  texFormat values come from the PixelFormat_* enum at the top of
#  RainbowForge/Image/DdsHelper.cs.
DDS_FOURCC_DXT1 = b'DXT1'
DDS_FOURCC_DXT5 = b'DXT5'
DDS_FOURCC_ATI2 = b'ATI2'  # BC5UNORM (RG)
DDS_FOURCC_DX10 = b'DX10'

DXGI_FORMAT_BC4_UNORM = 80
DXGI_FORMAT_BC6H_UF16 = 95
DXGI_FORMAT_BC7_UNORM = 98

# texFormat -> (fourcc, dxgi_format_or_0, block_size_bytes, is_compressed)
# block_size_bytes is the size of one 4x4 block (8 for BC1/BC4, 16 for BC3/BC5/BC6/BC7).
TEXFORMAT_DDS_INFO = {
    0x02: (DDS_FOURCC_DXT1, 0,                       8,  True),   # BC1
    0x03: (DDS_FOURCC_DXT1, 0,                       8,  True),   # BC1A
    0x04: (DDS_FOURCC_DXT5, 0,                       16, True),   # BC2 -> reuse DXT5 path
    0x05: (DDS_FOURCC_DXT5, 0,                       16, True),   # BC3
    0x06: (DDS_FOURCC_ATI2, 0,                       16, True),   # BC5
    0x0E: (DDS_FOURCC_DX10, DXGI_FORMAT_BC4_UNORM,   8,  True),   # BC4
    0x0F: (DDS_FOURCC_DX10, DXGI_FORMAT_BC6H_UF16,   16, True),   # BC6H
    0x10: (DDS_FOURCC_DX10, DXGI_FORMAT_BC7_UNORM,   16, True),   # BC7
}

# Pixel formats whose diffuse textures carry a meaningful alpha channel.
# When the diffuse uses one of these, we wire Alpha -> BSDF Alpha and enable
# blended transparency. BC1 (0x02) has only 1-bit alpha which the engine
# rarely uses for transparency; BC1A (0x03) explicitly marks 1-bit punch-
# through alpha. BC3 (0x05), BC2 (0x04), BC7 (0x10), and RGBA (0x00) all
# carry full or multi-bit alpha.
TEXFORMAT_HAS_ALPHA = {0x00, 0x03, 0x04, 0x05, 0x10}

TEXTURE_TYPE_NAMES = {
    0: 'diffuse',
    1: 'normal',
    2: 'specular',
    3: 'misc',
    5: 'displacement',
    6: 'translucence',
    7: 'colormask',
}

# ----- Entity COMPONENT TYPE magics (the real namespace) -----
# Derived 2026-08-02 by cross-referencing a Heated Metal `ExportEntities` dump of
# Y5S4 Neon Dawn Oregon (10,393 entities with NAMED components) against the
# component list parsed out of that build's forge — see
# research/entity_component_layout.md and forge.entity.parse_entity_components.
#
# Method: presence correlation. A magic means component X iff the set of entities
# carrying that magic matches the set Heated Metal says have X. The number below
# is the Jaccard index over ~10.3k entities; 1.000 is exact set equality. This is
# deliberately NOT based on reading ids out of component payloads — a component's
# payload can reference OTHER components' ids, which makes containment tests lie.
#
# Note several magics map to one component name (ClutterComponent has five). Our
# walk is finer-grained than the engine's component list: one logical component is
# built from several typed sub-records.
#
# These are STABLE across versions (verified identical Y5S3 v30 -> v32 "Siege X"),
# unlike the v34 top-level record magics which re-hash every patch.
COMPONENT_TYPE_MAGICS = {
    # --- exact set equality (J = 1.000) ---
    0x05AE8FBD: 'SpacePortalComponent',
    0xE527E531: 'SpacePortalComponent',
    # SpaceComponent, J=0.986 (68/69 named spaces on Y5S4 Oregon). Same run
    # reproduced the SpacePortal pair above at J=1.000, which validates the
    # correlation. These two carry the room VOLUME -- see forge/space.py.
    0x0881ADF8: 'SpaceComponent',
    0xD67914BD: 'SpaceComponent',
    0x33FA64C5: 'ReinforcementComponent',
    0x943D6D3F: 'ReinforcementComponent',
    0x3146498C: 'DecoDestructionComponent',
    0x1B478101: 'SoundBankEntityComponent',
    0x6290D74A: 'SoundBankEntityComponent',
    0x2350A2E0: 'DamageComponent',
    0x3001E293: 'DamageComponent',
    0x82589DD7: 'DamageComponent',
    0xA117C2BE: 'DamageComponent',
    0xEF1C7C37: 'AnimatedEntityComponent',
    0xF85FBB25: 'AnimatedEntityComponent',
    0x08ED0292: 'ClutterComponent',
    0x1FB7CB75: 'ClutterComponent',
    0x21795599: 'ClutterComponent',
    0x90796963: 'ClutterComponent',
    0xF49B6117: 'ClutterComponent',
    0x7A3FFD02: 'CullingVolume',
    0x8A2588E8: 'TriggerComponent',
    0xCF153BBA: 'TriggerComponent',
    0x1BDE971A: 'CapturePointObjectiveComponent',
    0x2C49B73D: 'TriggerEventSenderComponent',
    0x3939DC9A: 'TriggerEventSenderComponent',
    0x5DC64112: 'TriggerEventSenderComponent',
    0xC800CC7B: 'TriggerEventSenderComponent',
    0x600BFFB5: 'DestructibleAmbient3dSoundComponent',
    0xA9EF8AD6: 'EnvironmentSettings',
    # --- strong (J >= 0.90) ---
    0x49FCD7BF: 'MaterialOverrider',              # J=0.996
    0x4DB4F3BA: 'DestructionRigidBodyComponent',  # J=0.948
    0xEECEC280: 'DestructionRigidBodyComponent',  # J=0.948
    0xA2622CF6: 'MultiStateSoundComponent',       # J=0.923
    0x25AA1CCF: 'SoundSettingsComponent',         # J=0.904
    # DELIBERATELY EXCLUDED, measured against the Heated Metal ground truth:
    #   0x95CA575A ReinforcementComponent J=0.962 -> drops reinforcement precision
    #              100% -> 96.2% for no recall gain (the J=1.000 pair covers it).
    #   0xF917833A DamageComponent        J=0.889 -> drops damage precision to 88.9%.
    #   0x228F402A InertComponent  covers 4075/4075 inert but sits on 6218 entities,
    #              so it collapses inert precision 99.0% -> 65.5%. `inert` is left
    #              to the legacy window scan, which happens to be precise for it.
    # Add a magic here only if it improves precision or recall against that dump.
}

# Component types present on ~100% of entities. No Heated Metal component name
# reaches them (the most common, Visual, is on 86%), so they are engine-internal
# per-entity records, not gameplay components. Excluded from classification.
COMPONENT_TYPE_UNIVERSAL = {0x4AEC3476, 0xEACD765A}

# ----- Space (room) volume records -----
# The two SpaceComponent magics above, called out by role. A Siege map is
# partitioned into Spaces (rooms) joined by SpacePortals — the room graph behind
# culling, sound propagation, callouts and the spectator floor view. Both records
# are 33 bytes and describe the room volume in WORLD space:
#
#   SPACE_EDGE_MAGIC   [x f32][y f32][z f32][cornerIdx u8][00 00 3f]  x2  + pad
#                      -> two endpoints of ONE edge of the volume
#   SPACE_PLANE_MAGIC  [normal f32 x3][pad][point f32 x3][1.0f]
#                      -> one face plane of the volume
#
# Validated on Y5S4 Oregon: 68/68 Space origins lie inside their own volume.
# A Space is a convex DECOMPOSITION, not a single convex hull — simple 6-plane
# rooms satisfy every half-space, complex ones don't (56% overall), so containment
# uses per-piece AABBs (forge/space.py) rather than a whole-space half-space test.
SPACE_EDGE_MAGIC = 0x0881ADF8
SPACE_PLANE_MAGIC = 0xD67914BD

# Heated Metal component name -> the category tag ENTITY_TYPE_CATEGORIES and
# map_importer._classify_category already speak.
COMPONENT_NAME_TO_TAG = {
    'InertComponent': 'inert',
    'DestructionComponent': 'destruction',
    'DestructionRigidBodyComponent': 'rigidbody',
    'DecoDestructionComponent': 'deco_destruction',
    'ReinforcementComponent': 'reinforcement',
    'SoundSettingsComponent': 'sound',
    'SoundBankEntityComponent': 'sound_bank',
    'MultiStateSoundComponent': 'sound',
    'DestructibleAmbient3dSoundComponent': 'sound',
    'TriggerComponent': 'trigger',
    'TriggerEventSenderComponent': 'trigger_event',
    'SpacePortalComponent': 'space',
    'CapturePointObjectiveComponent': 'bomb_objective',
    'DamageComponent': 'damage',
    'MarkableComponent': 'markable',
    'DynamicClippingDataComponent': 'dynamic_clipping',
    'InteractiveWindowComponent': 'interactive_window',
    # MaterialOverrider / CullingVolume / EnvironmentSettings / AnimatedEntity /
    # Clutter intentionally have no category tag — they don't determine what an
    # entity IS. In particular do NOT map Animated/Clutter to 'visual': Heated
    # Metal's Visual is on 8851 entities and those two on 7, so they are not it.
}

# Tags the component walk owns outright. Where the walk runs, the legacy window
# scan's opinion on these is DISCARDED rather than unioned in — the scan reads the
# wrong namespace and its false positives otherwise drag precision down (it tagged
# 'space' on entities with no SpacePortalComponent at all: 0% precision, versus
# 100% for the walk).
#
# DERIVED, never hand-written. A tag may only be suppressed if some mapped magic
# can actually re-produce it; hardcoding this set once silently zeroed
# 'destruction', 'markable', 'interactive_window' and 'dynamic_clipping', whose
# component types have no magic in COMPONENT_TYPE_MAGICS yet. Everything else —
# 'visual' and 'inert' most importantly — keeps coming from the window scan,
# which is empirically precise for them (99-100% against the Heated Metal dump).
WALK_AUTHORITATIVE_TAGS = {
    tag for tag in (
        COMPONENT_NAME_TO_TAG.get(name) for name in COMPONENT_TYPE_MAGICS.values()
    ) if tag
}

# ----- Component / Entity-category tables -----

# Component type magics observed in entity payloads. These are the same
# 4-byte values the in-game QVM script prints alongside each component.
# Scanning the entity payload for these lets us classify entities into
# visual-only / destructible / floor-group / etc. without running the game.
COMPONENT_MAGICS = {
    0xEC658D29: 'visual',
    0x0E5A450A: 'inert',
    0x0EEA1B2D: 'sound',
    0xDAB4219F: 'sound_bank',
    0xD730F3E0: 'rigidbody',
    0xC177D702: 'rigidbody_plain',
    0x66871E91: 'destruction',
    0x4DDC8E7A: 'deco_destruction',
    0x4982AF0B: 'pilot_destructible',
    0xD7525878: 'dynamic_clipping',
    0x558029BD: 'reinforcement',
    0x75DEEB68: 'security_camera',
    0xD9B3D0F8: 'camera_ui',
    0x17806374: 'camera_tool',
    0x81538378: 'bomb_objective',
    0x77CC2D14: 'bomb_marker',
    0xDE85D581: 'counter_defuser',
    0x502CC335: 'space',
    0x4AB0DD24: 'trigger',
    0xDB883EE3: 'trigger_event',
    0x51E7F5DF: 'interactive_gadget',
    0x628523F7: 'interactive_window',
    0x86EB1698: 'interactive_rope',
    0x33E86CF3: 'ai',
    0x9E8FE224: 'ai_detection',
    0x1F899234: 'damage',
    0xDFB6F50F: 'auto_pickup',
    0x37EBA837: 'destruction_navmesh',
    0xCFDAAB6D: 'dseg',
    0x2B0BF969: 'markable',
    0xE877D19E: 'adhesion',
    0x32B6D84B: 'visual_detection',
    # Observed on `PVP13_Border_Dab_Output_FloorWallGroups_NNN` entities.
    # These are the procedural floor/wall partition volumes the engine
    # uses for things like the spectator-mode level split — four per map
    # on Border. Unknown hash symbolically, so tagged by function.
    0xBFB02B95: 'floor_group',
    # NOTE: the SpotLight/OmniLight/SkyLight magics are deliberately NOT here.
    # They are asset-type magics that live in a shared lighting block, not
    # per-entity component hashes — scanning an entity's window for them
    # falsely tags whatever emissive prop happens to precede that block. Light
    # entities are detected precisely via LIGHT_DEFINITION_MAGICS +
    # scan_entity_light_refs instead (only bare light volumes get the
    # spot_light/omni_light/sky_light component).
}

# Light-source definition magics (SpotLight/OmniLight/SkyLight from Magic.cs).
# Inside a LoadUnit these appear as standalone light-definition records using the
# gap-12 `magic + uid(8) + magic` envelope (same shape as Entity/Mesh records),
# packed into a shared lighting block. The light record holds only color/range —
# NOT a world transform. A *placement entity* carries the transform and
# references the light record's uid in its payload, so we detect lights by that
# reference and map the light's kind onto the matching component name above. (On
# Casino: 56 light defs — 31 spot / 23 omni / 2 sky — referenced by 199 entities,
# none of which are themselves the light record.)
LIGHT_DEFINITION_MAGICS = {
    0x80320FB8: 'spot_light',
    0x344780D6: 'omni_light',
    0x77EEEBB4: 'sky_light',
}

# Entity type classification for the import filter. Each category maps to
# a set of component names — an entity belongs to a category if it has
# at least one component from that category's set. Categories are checked
# in priority order; an entity is included if ANY of its matching
# categories is enabled.
ENTITY_TYPE_CATEGORIES = {
    'VISUAL': {
        'description': 'Entities with a Visual component and no destruction/reinforcement/objective/sound',
        'include': {'visual'},
        'exclude': {'destruction', 'deco_destruction', 'pilot_destructible', 'rigidbody',
                    'reinforcement', 'sound', 'sound_bank', 'bomb_objective', 'bomb_marker',
                    'counter_defuser', 'security_camera', 'camera_ui', 'camera_tool',
                    'trigger', 'trigger_event', 'space', 'interactive_gadget'},
    },
    'DECAL': {
        'description': 'Decal/overlay entities (visual-only, no inert/destruction/rigidbody)',
        'include': {'visual'},
        'exclude': {'destruction', 'deco_destruction', 'pilot_destructible', 'rigidbody',
                    'inert', 'reinforcement', 'sound', 'sound_bank', 'bomb_objective',
                    'bomb_marker', 'counter_defuser', 'security_camera', 'camera_ui',
                    'camera_tool', 'trigger', 'trigger_event', 'space',
                    'interactive_gadget'},
    },
    'NON_DESTRUCTIBLE': {
        'description': 'Static geometry with InertComponent (non-breakable walls, floors)',
        'include': {'inert'},
        'exclude': {'destruction', 'deco_destruction', 'pilot_destructible'},
    },
    'DESTRUCTIBLE': {
        'description': 'Breakable entities (DestructionComponent, DecoDestruction, etc.)',
        'include': {'destruction', 'deco_destruction', 'pilot_destructible', 'rigidbody'},
        'exclude': set(),
    },
    'REINFORCEMENT': {
        'description': 'Reinforceable surfaces',
        'include': {'reinforcement'},
        'exclude': set(),
    },
    'SOUND': {
        'description': 'Sound emitter entities',
        'include': {'sound', 'sound_bank'},
        # Cameras carry sound/sound_bank too — exclude them so the dedicated
        # Camera filter stays authoritative (camera comps never appear on a
        # genuine sound emitter, so this only affects CCTV/observation cams).
        'exclude': {'security_camera', 'camera_ui', 'camera_tool'},
    },
    'CAMERA': {
        'description': 'Security cameras (CCTV) and default observation cameras',
        'include': {'security_camera', 'camera_ui', 'camera_tool'},
        'exclude': set(),
    },
    'LIGHT': {
        'description': 'Light volumes (spot/omni/sky lights baked into lighting)',
        'include': {'spot_light', 'omni_light', 'sky_light'},
        'exclude': set(),
    },
    'SPAWN': {
        'description': 'Spawn/trigger/interaction points',
        'include': {'trigger', 'trigger_event', 'space', 'interactive_gadget',
                    'interactive_window', 'interactive_rope'},
        # Some cameras carry a trigger volume — keep them under Camera only.
        'exclude': {'security_camera', 'camera_ui', 'camera_tool'},
    },
    'OBJECTIVE': {
        'description': 'Bomb sites, defuser interactions, objective markers',
        'include': {'bomb_objective', 'bomb_marker', 'counter_defuser'},
        'exclude': set(),
    },
    # Player trigger volumes that live on the edge of each objective room
    # ("runout blockers"). Detected as trigger + inert + visual with no
    # interactive/gadget/objective components — i.e. a plain trigger box.
    'RUNOUT_BLOCKER': {
        'description': 'Runout blocker trigger volumes (out-of-bounds kill zones)',
        'include': {'trigger'},
        'exclude': {'interactive_gadget', 'interactive_window', 'interactive_rope',
                    'bomb_objective', 'bomb_marker', 'counter_defuser',
                    'destruction', 'deco_destruction', 'pilot_destructible',
                    'reinforcement', 'trigger_event', 'space',
                    'security_camera', 'camera_ui', 'camera_tool'},
    },
    # Interactive gameplay props (ammo refills, biohazard containers, hostage
    # spawns, etc.) — distinguished from pure spawn points by carrying an
    # InteractiveGadgetComponent with accompanying sound/rigidbody state.
    'INTERACTIVE': {
        'description': 'Interactive gameplay props (ammo, biohazard, hostage, gadgets)',
        'include': {'interactive_gadget', 'interactive_window', 'interactive_rope'},
        'exclude': {'bomb_objective', 'bomb_marker', 'counter_defuser'},
    },
}
