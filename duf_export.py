"""
Core Blender -> Daz Studio native .duf exporter logic.

Kept separate from the addon/operator wrapper (addon_init.py) so it can be
imported and driven directly from a headless `blender --background --python`
test script, or via the Blender MCP bridge's execute_blender_code against a
live session, without needing the addon registered in a running Blender UI.

Format notes / design rationale: see ../DUF_EXPORT_NOTES.md in this repo.
Coordinate system: Blender is right-handed, Z-up, meters.
Daz/DSON is right-handed, Y-up, centimeters (1 native unit = 1 cm).
The conversion below (x, z, -y) * 100 is a proper rotation (determinant +1,
no reflection), so face winding / vertex order needs no adjustment.
"""

import json
import os

import bpy
from mathutils import Vector


def to_daz_vec(v):
    """Blender (x, y, z) meters, Z-up -> Daz [x, y, z] cm, Y-up."""
    return [v[0] * 100.0, v[2] * 100.0, -v[1] * 100.0]


def _channel(cid, ctype, name, label, value, minv=-10000.0, maxv=10000.0, step=1.0, clamped=False):
    ch = {
        "id": cid,
        "type": ctype,
        "name": name,
        "label": label,
        "value": value,
        "min": minv,
        "max": maxv,
        "step_size": step,
    }
    if clamped:
        ch["clamped"] = True
    return ch


def _xyz_channels(prefix, label_prefix, values, minv=-10000.0, maxv=10000.0, step=1.0, clamped=False):
    labels = {"x": "X " + label_prefix, "y": "Y " + label_prefix, "z": "Z " + label_prefix}
    out = []
    for i, axis in enumerate(("x", "y", "z")):
        out.append(_channel(axis, "float", f"{axis.upper()}{prefix}", labels[axis], values[i],
                             minv, maxv, step, clamped))
    return out


def _bone_node_entry(bone_id, label, parent_id, center_point, end_point):
    """One node_library entry for a bone (or the figure root when parent_id is None)."""
    entry = {
        "id": bone_id,
        "name": bone_id,
        "type": "bone" if parent_id else "figure",
        "label": label,
        "rotation_order": "XYZ",
        "inherits_scale": True,
        "center_point": _xyz_channels("Origin", "Origin", center_point, step=0.01),
        "end_point": _xyz_channels("End", "End", end_point, step=0.01),
        "orientation": _xyz_channels("Orientation", "Orientation", [0.0, 0.0, 0.0], step=0.1),
        "rotation": _xyz_channels("Rotate", "Rotate", [0.0, 0.0, 0.0], -180.0, 180.0, 0.5, True),
        "translation": _xyz_channels("Translate", "Translate", [0.0, 0.0, 0.0], step=1.0),
        "scale": _xyz_channels("Scale", "Scale", [1.0, 1.0, 1.0], -100.0, 100.0, 0.005),
        "general_scale": _channel("scale", "float", "Scale", "Scale", 1.0, 0.0, 100.0, 0.005),
    }
    if parent_id:
        entry["parent"] = f"#{parent_id}"
    return entry


def _safe_id(name):
    """Daz asset ids must be simple tokens; sanitize a Blender name into one."""
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    if out and out[0].isdigit():
        out = "_" + out
    return out or "node"


def used_bone_names(mesh_objs, armature_obj):
    """
    Bone names actually referenced by a vertex group (matching an armature
    bone) on any of mesh_objs. Used to build a pruned "clothing" skeleton the
    way real Daz garment assets do (e.g. a pants asset ships ~17 leg/hip
    bones, not the full 95-bone character rig) - confirmed against a real
    clothing asset in this session.
    """
    bone_names = {b.name for b in armature_obj.data.bones}
    used = set()
    for mesh_obj in mesh_objs:
        for vg in mesh_obj.vertex_groups:
            if vg.name in bone_names:
                used.add(vg.name)
    return used


def _expand_with_ancestors(names, armature_obj):
    bones_by_name = {b.name: b for b in armature_obj.data.bones}
    expanded = set(names)
    for name in list(names):
        b = bones_by_name.get(name)
        while b is not None and b.parent is not None:
            b = b.parent
            expanded.add(b.name)
    return expanded


def _walk_bones(armature_obj, figure_end_point, allowed_names=None):
    """
    Yield (bone_id, label, parent_id_or_None, center_point_daz, end_point_daz)
    for the figure root followed by every bone, depth-first.

    If allowed_names is given (already expanded to include ancestors - see
    _expand_with_ancestors), bones outside that set - and their entire
    subtree - are skipped entirely. Safe because an included bone's ancestors
    are always included too by construction, so no parent-chain gaps result.

    IMPORTANT: center_point and end_point are ABSOLUTE positions (in the
    Daz cm/Y-up space), NOT deltas relative to the parent or to the bone's
    own center_point. Confirmed by inspecting a real Daz-authored figure
    (Brown Bear): consecutive spine bones' center_point values are full
    "world-ish" coordinates (e.g. hip Y=120.57, waist Y=106.44 - not small
    incremental offsets), and a bone's end_point lines up with its child's
    center_point in that same absolute space. Exporting parent-relative
    deltas here loads without error but silently breaks forward-kinematics
    (every bone below the root collapses onto the same position) - confirmed
    live via daz_get_world_position/getWSPos before this was found and fixed.
    """
    figure_id = _safe_id(armature_obj.name)
    yield (figure_id, armature_obj.name, None, [0.0, 0.0, 0.0], figure_end_point)

    bones = armature_obj.data.bones
    roots = [b for b in bones if b.parent is None]

    def head_world(b):
        return armature_obj.matrix_world @ b.head_local

    def tail_world(b):
        return armature_obj.matrix_world @ b.tail_local

    def recurse(bone, parent_id):
        if allowed_names is not None and bone.name not in allowed_names:
            return
        bone_id = _safe_id(bone.name)
        center = to_daz_vec(head_world(bone))
        end = to_daz_vec(tail_world(bone))
        yield (bone_id, bone.name, parent_id, center, end)
        for child in bone.children:
            yield from recurse(child, bone_id)

    for root in roots:
        yield from recurse(root, figure_id)


def _build_geometry_and_uv(mesh_obj, geo_id, uv_id):
    mesh = mesh_obj.data

    vertices = [to_daz_vec(v.co) for v in mesh.vertices]

    material_names = [_safe_id(m.name) if m else "default" for m in mesh.materials] or ["default"]
    if not mesh.materials:
        material_names = ["default"]

    uv_layer = mesh.uv_layers.active

    # DSON polygons support 3 or 4 vertices only ("no more than four vertex
    # indices" - spec). Blender polygons can be n-gons (5+), so fan-triangulate
    # those (simple/convex-fan; adequate for typical n-gon caps, not a general
    # concave-polygon triangulator - acceptable v1 limitation). Every output
    # row below is tracked with its own loop-index list so UVs stay correct
    # even when one Blender polygon becomes multiple output rows.
    polylist = []
    output_rows_loops = []  # list of lists of source loop_indices, parallel to polylist
    for poly in mesh.polygons:
        mat_idx = poly.material_index if mesh.materials else 0
        verts = list(poly.vertices)
        loops = list(poly.loop_indices)
        if len(verts) <= 4:
            polylist.append([mat_idx, 0] + verts)
            output_rows_loops.append(loops)
        else:
            for i in range(1, len(verts) - 1):
                polylist.append([mat_idx, 0, verts[0], verts[i], verts[i + 1]])
                output_rows_loops.append([loops[0], loops[i], loops[i + 1]])

    geometry = {
        "id": geo_id,
        "name": geo_id,
        "type": "polygon_mesh",
        "vertices": {"count": len(vertices), "values": vertices},
        "polygon_material_groups": {"count": len(material_names), "values": material_names},
        "polygon_groups": {"count": 1, "values": ["default"]},
        "polylist": {"count": len(polylist), "values": polylist},
        "default_uv_set": f"#{uv_id}",
    }

    uvs = []
    poly_vertex_indices = []
    if uv_layer is not None:
        for output_index, loop_indices in enumerate(output_rows_loops):
            for loop_index in loop_indices:
                loop = mesh.loops[loop_index]
                uv = uv_layer.data[loop_index].uv
                uv_idx = len(uvs)
                uvs.append([uv[0], uv[1]])
                poly_vertex_indices.append([output_index, loop.vertex_index, uv_idx])

    uv_set = {
        "id": uv_id,
        "name": uv_id,
        "vertex_count": len(vertices),
        "uvs": {"count": len(uvs), "values": uvs},
        "polygon_vertex_indices": poly_vertex_indices,
    }

    return geometry, uv_set, material_names


def _build_skin_binding(mesh_obj, geo_id, figure_id, mod_id, bone_ids):
    mesh = mesh_obj.data
    group_index_to_bone = {}
    for vg in mesh_obj.vertex_groups:
        bid = _safe_id(vg.name)
        if bid in bone_ids:
            group_index_to_bone[vg.index] = bid

    per_bone_weights = {bid: [] for bid in group_index_to_bone.values()}
    for v in mesh.vertices:
        for g in v.groups:
            bid = group_index_to_bone.get(g.group)
            if bid is not None and g.weight > 0.0:
                per_bone_weights[bid].append([v.index, g.weight])

    joints = []
    for bid, weights in per_bone_weights.items():
        if not weights:
            continue
        joints.append({
            "id": bid,
            "node": f"#{bid}",
            "node_weights": {"count": len(weights), "values": weights},
        })

    modifier = {
        "id": mod_id,
        "name": mod_id,
        "parent": f"#{geo_id}",
        "skin": {
            "node": f"#{figure_id}",
            "geometry": f"#{geo_id}",
            "vertex_count": len(mesh.vertices),
            "joints": joints,
        },
        # Without this "skin_settings" extra block, Daz Studio loads the skin
        # binding data structurally but does NOT actually apply it as an
        # active deformer (confirmed live: mesh rendered undeformed after
        # posing a bone until this was added). Values copied from a real
        # Daz-authored figure asset (Brown Bear).
        "extra": [{
            "type": "skin_settings",
            "auto_normalize_general": True,
            "auto_normalize_local": True,
            "auto_normalize_scale": True,
            "binding_mode": "General",
            "general_map_mode": "Linear",
            "scale_mode": "BindingMaps",
        }],
    }
    return modifier


import copy

_IRAY_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "iray_uber_channels_template.json")
with open(_IRAY_TEMPLATE_PATH, "r", encoding="utf-8") as _f:
    # Full 108-channel Iray Uber Base "studio_material_channels" list, extracted
    # verbatim from a real Daz-authored material (Brown Bear's "bearTongue").
    # A material with ONLY the top-level "diffuse" field and the
    # "studio/material/uber_iray" extra marker (what v1 shipped) loads fine but
    # Daz Studio instantiates it as plain DzDefaultMaterial, not Iray Uber -
    # confirmed live. This full channel set is what's actually required for
    # Daz to recognize and build the real Iray Uber shader graph on import.
    IRAY_UBER_CHANNELS_TEMPLATE = json.load(_f)


def _resolve_image_file(image, out_dir, basename):
    """
    Get a real, valid-on-disk filepath for a Blender Image datablock.

    `image.filepath` is frequently stale or unusable directly: it may point
    at wherever the source file lived on a DIFFERENT machine/drive (common
    when the artist packed textures into the .blend for portability - the
    original absolute path, e.g. "D:\\avatars\\...", survives in the
    datablock even though the pixel data itself travels with the file and no
    longer needs that path) - confirmed on this exact fox asset, every
    texture is packed with a stale absolute source path. Prefer the existing
    file only if it genuinely still exists; otherwise export the image's
    actual (already-loaded, packed-or-not) pixel data fresh to out_dir on a
    throwaway copy, leaving the original datablock untouched.
    """
    if image is None:
        return None
    existing = bpy.path.abspath(image.filepath) if image.filepath else None
    if existing and os.path.isfile(existing):
        return existing

    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, basename[:60] + ".png")
    copy = image.copy()
    try:
        copy.filepath_raw = target
        copy.file_format = "PNG"
        copy.save()
    finally:
        bpy.data.images.remove(copy)
    return target if os.path.isfile(target) else None


def _direct_image(inp, out_dir, basename):
    """If `inp` is fed by nothing but a single Image Texture node (directly, or
    through one Normal Map node), return that image's filepath - cheap reuse,
    no baking needed since there's no processing to resolve."""
    if not inp.is_linked:
        return None
    from_node = inp.links[0].from_node
    if from_node.type == "TEX_IMAGE" and from_node.image:
        return _resolve_image_file(from_node.image, out_dir, basename)
    if from_node.type == "NORMAL_MAP":
        color_in = from_node.inputs.get("Color")
        if color_in and color_in.is_linked:
            tex_node = color_in.links[0].from_node
            if tex_node.type == "TEX_IMAGE" and tex_node.image:
                return _resolve_image_file(tex_node.image, out_dir, basename)
    return None


def _heuristic_upstream_image(socket, out_dir, basename, visited=None):
    """
    Fallback for when baking isn't available (see _bake_socket_to_image):
    walk upstream through the node graph and return the first Image Texture
    found anywhere feeding this socket, ignoring how it's actually combined
    (Mix/ColorRamp/etc). Not correct for genuinely blended multi-texture
    materials - may grab a mask or secondary layer instead of the intended
    one - only used as a last resort.
    """
    if visited is None:
        visited = set()
    if not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node.name in visited:
        return None
    visited.add(node.name)
    if node.type == "TEX_IMAGE" and node.image:
        return _resolve_image_file(node.image, out_dir, basename)
    for inp in node.inputs:
        img = _heuristic_upstream_image(inp, out_dir, basename, visited)
        if img:
            return img
    return None


def _bake_socket_to_image(mesh_obj, mat, socket, out_dir, image_basename, size=1024):
    """
    Bake an arbitrary (possibly procedurally-fed) Principled BSDF input socket
    to a flat PNG via Cycles, using mesh_obj's active UV map, matching how the
    reference "Blender to Daz Studio Plugin" handles complex material graphs
    ("Bake materials to create simplified texture maps"). Standard technique:
    temporarily reroute the socket into an Emission shader feeding Material
    Output, bake type=EMIT (captures the raw value, no lighting), then restore
    the material's original node graph exactly. Returns the saved filepath.

    Bakes in a throwaway TEMPORARY SCENE, not the user's actual scene.
    Root-caused live: `bpy.ops.object.bake` raised "No valid selected
    objects" unconditionally in one specific real production .blend file
    (confirmed reproducible even for a brand-new default cube with zero
    material complexity - not about this material, this mesh, hide_render,
    modifiers, or any addon: ARP/mmd_tools/the MCP bridge addon were each
    ruled out by disabling them and retrying). The same operator succeeded
    immediately in a fresh default Blender scene, and - decisively - also
    succeeded on a brand-new `bpy.data.scenes.new()` scene created *within
    that same file's process*. So something in that one Scene datablock's
    saved state (not the file, not the addons, not the objects) breaks the
    bake operator's object-gathering step specifically. A temporary scene
    sidesteps it in every case, at negligible cost (create, link, bake,
    unlink, delete).
    """
    nt = mat.node_tree
    output_node = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None) \
        or next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if output_node is None:
        return None
    surface_input = output_node.inputs["Surface"]
    original_from_socket = surface_input.links[0].from_socket if surface_input.is_linked else None

    emission_node = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(socket.links[0].from_socket, emission_node.inputs["Color"])
    nt.links.new(emission_node.outputs["Emission"], surface_input)

    os.makedirs(out_dir, exist_ok=True)
    img_name = image_basename[:60]
    image = bpy.data.images.new(img_name, width=size, height=size, alpha=False)
    tex_node = nt.nodes.new("ShaderNodeTexImage")
    tex_node.image = image
    for n in nt.nodes:
        n.select = False
    tex_node.select = True
    nt.nodes.active = tex_node

    filepath = os.path.join(out_dir, img_name + ".png")

    # Force mesh_obj down to ONE material slot (just `mat`): Blender's bake
    # operator processes every slot on the baked object and errors out
    # ("No active and selected image texture node found") on any slot
    # besides the one we just rigged with a temp node - confirmed live on
    # the fox's 3-material "Body" mesh.
    orig_material_slots = [slot.material for slot in mesh_obj.material_slots]
    orig_material_indices = [p.material_index for p in mesh_obj.data.polygons]
    mesh_obj.data.materials.clear()
    mesh_obj.data.materials.append(mat)
    for p in mesh_obj.data.polygons:
        p.material_index = 0

    prev_window_scene = bpy.context.window.scene
    prev_hide_render = mesh_obj.hide_render
    prev_hide_viewport = mesh_obj.hide_viewport
    prev_hide_get = mesh_obj.hide_get()
    temp_scene = bpy.data.scenes.new("__daz_export_bake_scene__")
    try:
        temp_scene.render.engine = "CYCLES"
        temp_scene.cycles.device = "CPU"  # GPU compute frequently unavailable in headless/background contexts
        temp_scene.collection.objects.link(mesh_obj)
        bpy.context.window.scene = temp_scene

        mesh_obj.hide_render = False
        mesh_obj.hide_viewport = False
        mesh_obj.hide_set(False)
        for o in bpy.context.selected_objects:
            o.select_set(False)
        mesh_obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_obj

        bpy.ops.object.bake(type="EMIT")

        image.filepath_raw = filepath
        image.file_format = "PNG"
        image.save()
    finally:
        if original_from_socket is not None:
            nt.links.new(original_from_socket, surface_input)
        else:
            for link in list(surface_input.links):
                nt.links.remove(link)
        nt.nodes.remove(emission_node)
        nt.nodes.remove(tex_node)
        bpy.data.images.remove(image)
        mesh_obj.data.materials.clear()
        for m in orig_material_slots:
            mesh_obj.data.materials.append(m)
        for p, idx in zip(mesh_obj.data.polygons, orig_material_indices):
            p.material_index = idx
        mesh_obj.hide_render = prev_hide_render
        mesh_obj.hide_viewport = prev_hide_viewport
        mesh_obj.hide_set(prev_hide_get)
        bpy.context.window.scene = prev_window_scene
        temp_scene.collection.objects.unlink(mesh_obj)
        bpy.data.scenes.remove(temp_scene)

    return filepath if os.path.isfile(filepath) else None


# (blender_socket, daz_channel_id, bake_basename_suffix)
_IRAY_SOCKET_MAP = [
    ("Metallic", "Metallic Weight", "Metallic"),
    ("Roughness", "Glossy Roughness", "Roughness"),
    ("Alpha", "Cutout Opacity", "Alpha"),
    ("Emission Color", "Emission Color", "Emission"),
]
# Blender 4.0+ renamed these two; try the new name, fall back to the old one.
_SPECULAR_ALIASES = ("Specular IOR Level", "Specular")
_TRANSMISSION_ALIASES = ("Transmission Weight", "Transmission")


def _find_socket(bsdf, aliases):
    for name in aliases:
        inp = bsdf.inputs.get(name)
        if inp is not None:
            return inp
    return None


def _iray_overrides(mat, mesh_obj, textures_dir, id_prefix):
    """Map a Blender material's Principled BSDF inputs onto Iray Uber channel
    ids, baking any procedurally-fed (non-trivial) socket to a flat texture.
    Returns (diffuse_rgb[3], diffuse_image_or_None, {channel_id: (value_or_None, image_or_None)})."""
    diffuse_rgb, diffuse_image = [0.8, 0.8, 0.8], None
    overrides = {}
    if not mat or not mat.use_nodes:
        return diffuse_rgb, diffuse_image, overrides
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        return diffuse_rgb, diffuse_image, overrides

    def resolve(inp, bake_name):
        """value_or_None, image_or_None for one socket - direct value, direct
        (unprocessed) texture reuse, or a fresh bake, in that priority order.
        If baking raises (some environments can't run bpy.ops.object.bake -
        e.g. confirmed non-functional when invoked through this project's
        Blender MCP scripting bridge specifically, "No valid selected
        objects" despite every Python-level selection/visibility check
        passing - looks like a context restriction in that execution path,
        not a flaw in the baking logic itself), fall back to the heuristic
        upstream-image search rather than aborting the whole export."""
        if inp is None:
            return None, None
        if not inp.is_linked:
            return inp.default_value, None
        basename = f"{id_prefix}_{bake_name}"
        direct = _direct_image(inp, textures_dir, basename)
        if direct:
            return None, direct
        try:
            baked = _bake_socket_to_image(mesh_obj, mat, inp, textures_dir, basename)
            if baked:
                return None, baked
        except RuntimeError:
            pass
        return None, _heuristic_upstream_image(inp, textures_dir, basename)

    base_val, base_img = resolve(bsdf.inputs.get("Base Color"), "BaseColor")
    if base_img:
        diffuse_rgb, diffuse_image = [1.0, 1.0, 1.0], base_img
    elif base_val is not None:
        diffuse_rgb = list(base_val[:3])

    for socket_name, channel_id, bake_name in _IRAY_SOCKET_MAP:
        inp = bsdf.inputs.get(socket_name)
        val, img = resolve(inp, bake_name)
        if channel_id == "Emission Color":
            # only worth writing if actually emitting
            strength_inp = bsdf.inputs.get("Emission Strength")
            strength = strength_inp.default_value if strength_inp and not strength_inp.is_linked else 1.0
            if not strength:
                continue
        if channel_id == "Cutout Opacity" and val is not None and val >= 0.999:
            continue  # fully opaque, unlinked - no need to write, matches template default
        if val is not None:
            overrides[channel_id] = (list(val[:3]) if channel_id == "Emission Color" else val, None)
        elif img:
            overrides[channel_id] = (None, img)

    spec_inp = _find_socket(bsdf, _SPECULAR_ALIASES)
    val, img = resolve(spec_inp, "Specular")
    if val is not None:
        overrides["Glossy Reflectivity"] = (val, None)
    elif img:
        overrides["Glossy Reflectivity"] = (None, img)

    trans_inp = _find_socket(bsdf, _TRANSMISSION_ALIASES)
    val, img = resolve(trans_inp, "Transmission")
    if val is not None and val > 0:
        overrides["Refraction Weight"] = (val, None)
    elif img:
        overrides["Refraction Weight"] = (None, img)

    normal_inp = bsdf.inputs.get("Normal")
    if normal_inp is not None and normal_inp.is_linked:
        # normal maps: reuse only, never bake (EMIT-baking would re-encode the
        # already-tangent-space-correct map incorrectly)
        normal_img = _direct_image(normal_inp, textures_dir, f"{id_prefix}_Normal")
        if normal_img:
            overrides["Normal Map"] = (1.0, normal_img)

    return diffuse_rgb, diffuse_image, overrides


def _iray_channels_full(overrides):
    """Full channel list (library baseline): template defaults with our
    overrides applied in place."""
    channels = copy.deepcopy(IRAY_UBER_CHANNELS_TEMPLATE)
    for c in channels:
        ch = c["channel"]
        val, img = overrides.get(ch.get("id"), (None, None))
        if val is not None:
            ch["value"] = val
            if "current_value" in ch:
                ch["current_value"] = val
        if img and os.path.isfile(img):
            ch["image_file"] = img.replace("\\", "/")
    return channels


def _iray_channels_sparse(overrides):
    """Minimal per-instance override list (only channels we're actually
    customizing) - matches the real asset's instance-level pattern, which
    only repeats the handful of channels that differ from the library
    definition rather than the full 108."""
    channels = []
    for cid, (val, img) in overrides.items():
        entry = {"id": cid}
        if val is not None:
            entry["current_value"] = val
        if img and os.path.isfile(img):
            entry["image_file"] = img.replace("\\", "/")
        channels.append({"channel": entry})
    return channels


def _build_materials(mesh_obj, mesh_id, geo_id, uv_id, material_names, textures_dir):
    """mesh_id prefixes every material id so multiple meshes' materials
    (e.g. two different meshes both having a slot literally named the same)
    can't collide in the shared file-wide id namespace.

    Also returns materials_manifest: {group_name: {diffuse_rgb, diffuse_image,
    overrides}} - the raw per-material channel data needed by
    _write_material_fixup_script(). Kept separate from the DSON dicts above
    because the fixup script re-derives the same values through Daz's own
    property API rather than the "studio_material_channels" extra block (see
    that function's docstring for why the block alone isn't enough)."""
    mats_lib = []
    mats_scene = []
    materials_manifest = {}
    blender_mats = mesh_obj.data.materials
    for i, group_name in enumerate(material_names):
        mat = blender_mats[i] if (blender_mats and i < len(blender_mats)) else None
        mat_id = f"{mesh_id}_{group_name}_mat"
        diffuse_rgb, diffuse_image, overrides = _iray_overrides(mat, mesh_obj, textures_dir, mat_id)
        materials_manifest[group_name] = {
            "diffuse_rgb": diffuse_rgb,
            "diffuse_image": diffuse_image,
            "overrides": overrides,
        }

        diffuse_channel = _channel("diffuse", "float_color", "Diffuse Color", "Base Color",
                                    diffuse_rgb, 0.0, 1.0, 1.0, True)
        mats_lib.append({
            "id": mat_id,
            "diffuse": {"channel": dict(diffuse_channel)},
            "extra": [
                {"type": "studio/material/uber_iray", "version": "1.1.0.0"},
                {"type": "studio_material_channels", "channels": _iray_channels_full(overrides)},
            ],
        })

        instance_diffuse_channel = dict(diffuse_channel)
        if diffuse_image and os.path.isfile(diffuse_image):
            instance_diffuse_channel["image_file"] = diffuse_image.replace("\\", "/")

        mats_scene.append({
            "id": f"{mat_id}-1",
            "url": f"#{mat_id}",
            "geometry": f"#{geo_id}",
            "groups": [group_name],
            "diffuse": {"channel": instance_diffuse_channel},
            # Confirmed load-bearing, not cosmetic: a real Iray Uber material
            # instance without its own "uv_set" reference here (even though
            # the geometry already declares a "default_uv_set") loaded fine
            # with no error but rendered as plain DzDefaultMaterial with a
            # blank/white surface - the shader-instantiation logic apparently
            # needs this to build the actual Iray Uber shader graph. Fixed by
            # copying the real asset's pattern of repeating the uv_set
            # reference on every material instance.
            "uv_set": f"#{uv_id}",
            "extra": [
                {"type": "studio/material/uber_iray", "version": "1.1.0.0"},
                {"type": "studio_material_channels", "channels": _iray_channels_sparse(overrides)},
            ],
        })
    return mats_lib, mats_scene, materials_manifest


def _build_vertex_adjacency(mesh):
    """Vertex-index -> list of directly-edge-connected vertex indices. Built
    once per mesh and reused across all its shape keys (topology is the same
    for every key on a given mesh)."""
    adjacency = [[] for _ in range(len(mesh.vertices))]
    for e in mesh.edges:
        v0, v1 = e.vertices[0], e.vertices[1]
        adjacency[v0].append(v1)
        adjacency[v1].append(v0)
    return adjacency


def _smooth_sparse_deltas(raw_deltas, adjacency, iterations=2, factor=0.5):
    """
    raw_deltas: {vertex_index: Vector} (object-space, pre-conversion).
    Returns a smoothed {vertex_index: Vector}, expanded outward by one ring
    per iteration and Laplacian-blended toward the neighbor average.

    Why: a morph exported as a bare sparse per-vertex delta list cuts off
    abruptly at whatever vertex happened to have a non-negligible delta -
    every vertex just outside that boundary stays at exactly zero, so the
    surface kinks sharply right at the edge of the affected region. This is
    the classic "jagged"/faceted look the user specifically flagged as a
    known problem with PMX-imported morphs (PMX/MMD morphs are typically
    authored/exported the same naive sparse-delta way). Iteratively
    averaging each affected vertex's delta with its mesh neighbors - and
    including the newly-touched ring of previously-zero neighbors each pass
    - tapers the boundary into a smooth falloff instead of a hard edge,
    the same effect a sculpting/blendshape tool's "smooth" brush produces.

    Restricted to the affected region (+ its growing halo) rather than
    smoothing every vertex in the mesh every iteration, since most vertices
    stay at zero regardless - keeps this fast on large meshes.
    """
    if iterations <= 0:
        return raw_deltas
    current = dict(raw_deltas)
    active = set(current.keys())
    zero = Vector((0.0, 0.0, 0.0))
    for _ in range(iterations):
        halo = set(active)
        for v in active:
            halo.update(adjacency[v])
        new_current = {}
        for v in halo:
            neighbors = adjacency[v]
            if not neighbors:
                new_current[v] = current.get(v, zero)
                continue
            avg = zero.copy()
            for n in neighbors:
                avg += current.get(n, zero)
            avg /= len(neighbors)
            new_current[v] = current.get(v, zero).lerp(avg, factor)
        current = new_current
        active = halo
    return current


def _build_morphs(mesh_obj, geo_id, geo_instance_id, mesh_id, smooth_iterations=2, smooth_factor=0.5):
    """
    Blender shape keys (besides the reference "Basis") -> DSON morph
    modifiers. Format confirmed against a real shipped Daz product's morph
    asset (a corrective JCM on "CB Classic Boots"): a modifier_library entry
    with "channel" (the 0-1 "Value" slider) and "morph" (vertex_count +
    sparse [vertex_index, dx, dy, dz] deltas, same coordinate conversion as
    everything else in this file) side by side.

    Raw per-vertex deltas are smoothed (see _smooth_sparse_deltas) before
    being written out and re-filtered to non-negligible magnitude - softens
    the hard boundary a bare sparse delta list otherwise produces. Pass
    smooth_iterations=0 to disable and get the raw deltas.

    NOTE: the real shipped product this was modeled on ships its morph as a
    genuinely separate .dsf loaded through Daz's content-aware Morph loading
    path, and its scene.modifiers instance has no "parent" field at all -
    just {"id", "url"}. That does NOT work for a self-contained single-file
    export like this one: confirmed live, the modifier silently fails to
    attach (0 change to `object.getNumModifiers()`) without an instance-level
    "parent" pointing at the geometry's SCENE INSTANCE id - exactly the same
    lesson learned for skin_binding. `geo_instance_id` must be the suffixed
    scene-instance id (e.g. "eyesBase-1"), not the bare library id.
    """
    modifiers_lib = []
    modifiers_scene = []
    sk = mesh_obj.data.shape_keys
    if not sk or len(sk.key_blocks) < 2:
        return modifiers_lib, modifiers_scene

    basis = sk.key_blocks[0]
    vertex_count = len(mesh_obj.data.vertices)
    adjacency = _build_vertex_adjacency(mesh_obj.data) if smooth_iterations > 0 else None

    for kb in sk.key_blocks[1:]:
        raw = {}
        for i in range(vertex_count):
            d = kb.data[i].co - basis.data[i].co
            if d.length > 1e-6:
                raw[i] = d

        smoothed = _smooth_sparse_deltas(raw, adjacency, smooth_iterations, smooth_factor) \
            if smooth_iterations > 0 else raw

        deltas = []
        for i, d in smoothed.items():
            if d.length > 1e-6:
                dx, dy, dz = to_daz_vec(d)
                deltas.append([i, dx, dy, dz])
        if not deltas:
            continue

        morph_id = f"{mesh_id}_{_safe_id(kb.name)}"
        modifiers_lib.append({
            "id": morph_id,
            "name": morph_id,
            "parent": f"#{geo_id}",
            "presentation": {
                "type": "Modifier/Shape",
                "label": "", "description": "", "icon_large": "",
                "colors": [[0.3529412, 0.3529412, 0.3529412], [1, 1, 1]],
            },
            "channel": _channel("value", "float", "Value", kb.name, 0.0, 0.0, 1.0, 0.01, True) | {
                "display_as_percent": True,
            },
            "group": f"/Morphs/{mesh_id}",
            "morph": {
                "vertex_count": vertex_count,
                "deltas": {"count": len(deltas), "values": deltas},
            },
        })
        modifiers_scene.append({
            "id": f"{morph_id}-1", "url": f"#{morph_id}", "parent": f"#{geo_instance_id}",
        })

    return modifiers_lib, modifiers_scene


# Bundled stock "!Iray Uber Base" shader preset (copied verbatim from a real
# Daz Studio installation's default resources - author "DAZ 3D", 650 bytes,
# just the "studio/material/uber_iray" marker with zero channel data - see
# the "Materials: shader-class fixup" section of DUF_EXPORT_NOTES.md).
_BUNDLED_UBER_BASE_PRESET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "assets", "IrayUberBase.duf")


def _js_str(s):
    return json.dumps(s)


def _js_channel_value_snippet(prop_expr, value, image):
    """JS statement(s) applying one channel's value/image to a DazScript
    property object already bound to `prop_expr`. `value` mirrors the
    _iray_overrides() convention: a 3-element [r,g,b] list for color
    channels, a bare float for numeric ones, or None. setMap() works
    uniformly on both DzFloatColorProperty and plain DzFloatProperty
    instances (confirmed live), so no per-channel type table is needed."""
    # value and image are independent (mirrors _iray_channels_full/_sparse,
    # which write "current_value" and "image_file" independently too) - a
    # channel like Normal Map legitimately carries both a strength value and
    # a map at once.
    lines = []
    if isinstance(value, (list, tuple)):
        # setColorValue(QColor) round-trips through 0-255 sRGB and re-applies
        # gamma - wrong here since Blender socket values are already linear
        # (confirmed live: feeding 0.15 through a QColor(38,26,20) comes back
        # as ~0.015, not 0.15). setFloatColorValue(new DzFloatColor(...))
        # is the linear-native setter and round-trips exactly.
        r, g, b = value[0], value[1], value[2]
        lines.append(f"{prop_expr}.setFloatColorValue(new DzFloatColor({r}, {g}, {b}, 1.0));")
    elif value is not None:
        lines.append(f"{prop_expr}.setValue({value});")
    if image:
        lines.append(f"{prop_expr}.setMap({_js_str(image.replace(chr(92), '/'))});")
    return "\n".join(lines)


def _write_material_fixup_script(node_label, materials_manifest_by_shape, script_path,
                                  preset_path=_BUNDLED_UBER_BASE_PRESET):
    """
    Write a standalone DazScript (.dsa) that upgrades every material zone on
    `node_label` from the legacy DzDefaultMaterial our raw .duf merge produces
    to a genuine DzUberIrayMaterial, then re-applies the diffuse/PBR channel
    values and baked texture maps this exporter already computed.

    materials_manifest_by_shape: a LIST of per-mesh manifest dicts (one per
    mesh_objs entry, in the same order they were passed to export_duf - which
    is also the order DzObject.getShape(i) exposes them in, confirmed live).
    Deliberately a list, not one flat dict merged across meshes: a multi-mesh
    collection export (e.g. selecting the Armature to export every rigged
    mesh below it) can easily have two different meshes reuse the same
    material-slot name (confirmed live on the fox: shape 0 "Body" and shape 5
    "tounguefake" both have zones literally named "fox" and "HairClumps") - a
    flat dict keyed by that name would silently let one mesh's channel data
    clobber the other's.

    WHY THIS EXISTS: describing a "studio_material_channels" block by hand in
    a merged .duf (what this exporter used to rely on exclusively) is not
    enough to make Daz Studio instantiate the real Iray Uber shader class -
    confirmed by direct isolation testing (see DUF_EXPORT_NOTES.md). What
    *does* work is going through Daz's own preset-application codepath:
    select the node, then App.getContentMgr().openFile() a real
    "preset_shader"-type .duf (this is exactly what happens when a user drags
    a shader preset from Smart Content onto a figure). That path builds a
    native shader object instead of parsing one from raw JSON, but it also
    resets every channel to shader defaults - so step 2 re-applies our actual
    values/maps through the live DzMaterial property API (setColorValue /
    setValue / setMap), which is confirmed to work uniformly across both
    color and plain numeric channels.

    MULTI-SHAPE GOTCHA (confirmed live on a 6-mesh collection export): when
    export_duf() combines several meshes onto one node (each mesh becomes its
    own "geometry" on that single node/DzObject - see the "geometries" list
    on the figure root's scene node), DzObject.getCurrentShape() only ever
    returns shape index 0. Selecting the *node* and even explicitly
    select()-ing every material across every shape still only promotes shape
    0's materials to DzUberIrayMaterial when the preset is applied - the
    other shapes are silently left as DzDefaultMaterial. The actual fix:
    DzObject.getGeometryControl() is the LOD/shape-selector DzEnumProperty;
    setting its value to shape index i BEFORE selecting materials and
    applying the preset makes shape i "current", and only then does the
    preset-apply (and the material selection) actually reach it. So this
    script switches shapes one at a time via that control, fixing each
    shape's materials while it's current, then restores index 0 at the end.
    For a single-shape export this loop just runs once trivially.

    This script is meant to be run once, after the .duf it's paired with has
    been loaded/merged into the scene (e.g. via this project's `daz` MCP
    tools: daz_load_file then daz_execute_file, or manually through Daz
    Studio's Window > Script IDE). DSON itself has no supported hook to
    auto-run a script on scene load, so this can't be wired to fire on its
    own purely from the .duf - shipping it as a companion file (or driving it
    ourselves over the live MCP bridge right after load) is the practical
    substitute.
    """
    lines = [
        "// Auto-generated by blender_daz_exporter - upgrades DzDefaultMaterial",
        "// zones to Iray Uber and restores their channel values/maps.",
        "// See DUF_EXPORT_NOTES.md 'Materials: shader-class fixup' for why this exists.",
        "(function(){",
        f"    var node = Scene.findNodeByLabel({_js_str(node_label)});",
        f"    if (!node) node = Scene.findNode({_js_str(node_label)});",
        f"    if (!node) throw new Error(\"Node not found: \" + {_js_str(node_label)});",
        "",
        "    var obj = node.getObject();",
        "    var numShapes = obj.getNumShapes();",
        "    var ctrl = numShapes > 1 ? obj.getGeometryControl() : null;",
        f"    var presetPath = {_js_str(preset_path)};",
        "    var fixedZones = [];",
        "    var missingZones = [];",
    ]

    for shape_index, manifest_dict in enumerate(materials_manifest_by_shape):
        lines.append("")
        lines.append(f"    // --- shape {shape_index} ---")
        lines.append(f"    (function(){{")
        lines.append(f"        var shapeIndex = {shape_index};")
        lines.append("        if (shapeIndex >= numShapes) return;")
        lines.append("        if (ctrl) ctrl.setValue(shapeIndex);")
        lines.append("        var shape = obj.getShape(shapeIndex);")
        lines.append("")
        lines.append("        Scene.selectAllNodes(false);")
        lines.append("        node.select(true);")
        lines.append("        for (var i = 0; i < shape.getNumMaterials(); i++) shape.getMaterial(i).select(true);")
        lines.append("        var presetOk = App.getContentMgr().openFile(presetPath, new DzFileIOSettings(), false);")
        lines.append("        if (!presetOk) throw new Error(\"Failed to apply Iray Uber Base preset on shape \" + shapeIndex + \": \" + presetPath);")

        for group_name, manifest in manifest_dict.items():
            overrides = manifest["overrides"]
            diffuse_rgb = manifest["diffuse_rgb"]
            diffuse_image = manifest["diffuse_image"]

            lines.append("")
            lines.append(f"        (function(){{")
            lines.append(f"            var matLabel = {_js_str(group_name)};")
            lines.append("            var mat = null;")
            lines.append("            for (var i = 0; i < shape.getNumMaterials(); i++) {")
            lines.append("                var m = shape.getMaterial(i);")
            lines.append("                if (m.getLabel() === matLabel || m.getName() === matLabel) { mat = m; break; }")
            lines.append("            }")
            lines.append("            if (!mat) { missingZones.push(matLabel); return; }")

            lines.append("            var pDiffuse = mat.findProperty(\"Diffuse Color\");")
            lines.append("            if (pDiffuse) {")
            snippet = _js_channel_value_snippet("pDiffuse", diffuse_rgb, diffuse_image)
            for ln in snippet.splitlines():
                lines.append(f"                {ln}")
            lines.append("            }")

            for idx, (channel_id, (value, image)) in enumerate(overrides.items()):
                var_name = f"p{idx}"
                lines.append(f"            var {var_name} = mat.findProperty({_js_str(channel_id)});")
                lines.append(f"            if ({var_name}) {{")
                snippet = _js_channel_value_snippet(var_name, value, image)
                for ln in snippet.splitlines():
                    lines.append(f"                {ln}")
                lines.append("            }")

            lines.append("            fixedZones.push(matLabel);")
            lines.append("        })();")

        lines.append("    })();")

    lines.extend([
        "",
        "    if (ctrl) ctrl.setValue(0);",  # restore, cosmetic
        "",
        "    return {",
        "        node: node.getLabel(),",
        "        numShapes: numShapes,",
        "        fixedZones: fixedZones,",
        "        missingZones: missingZones",
        "    };",
        "})();",
    ])

    script_text = "\n".join(lines)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    return script_path


def export_duf(mesh_objs, armature_obj, filepath, asset_name=None,
                presentation_type=None, preferred_base=None,
                morph_smooth_iterations=2, morph_smooth_factor=0.5):
    """
    Export one or more meshes (all rigged to `armature_obj` via vertex groups
    matching bone names) as a single self-contained Daz Studio native .duf
    scene file - one Figure with one geometry + skin_binding per mesh.

    mesh_objs: a single mesh Object, or a list of mesh Objects.

    The skeleton written is pruned to only the bones actually referenced by
    a vertex group across mesh_objs (plus their ancestors) - mirrors how
    real Daz garment assets ship a reduced skeleton rather than the full
    character rig. Pass presentation_type (e.g. "Follower/Wardrobe/Top") and
    preferred_base (e.g. "/MyFox/Body") to mark the export as a conforming
    clothing item Daz's "Fit to" feature can target, instead of a standalone
    figure - matches the real-asset convention (confirmed against a shipped
    Daz clothing product in this session).

    Shape keys export as DSON morphs with their deltas smoothed
    (morph_smooth_iterations/morph_smooth_factor - see _smooth_sparse_deltas)
    to avoid the jagged/faceted look sparse per-vertex morph deltas otherwise
    produce at the boundary of the affected region. Pass
    morph_smooth_iterations=0 for raw, unsmoothed deltas.

    Returns the dict that was written (useful for tests/inspection).
    """
    if isinstance(mesh_objs, bpy.types.Object):
        mesh_objs = [mesh_objs]
    if not mesh_objs:
        raise ValueError("mesh_objs must contain at least one mesh Object")
    for m in mesh_objs:
        if m.type != "MESH":
            raise ValueError(f"{m.name} is not a MESH object")
    if armature_obj.type != "ARMATURE":
        raise ValueError("armature_obj must be an ARMATURE object")

    asset_name = asset_name or mesh_objs[0].name

    allowed_names = _expand_with_ancestors(used_bone_names(mesh_objs, armature_obj), armature_obj)

    bbox_max_z = max((v[2] for mesh_obj in mesh_objs for v in mesh_obj.bound_box), default=1.0)
    figure_end = to_daz_vec((0.0, 0.0, bbox_max_z))

    node_entries = list(_walk_bones(armature_obj, figure_end, allowed_names))
    figure_id = node_entries[0][0]
    bone_ids = {e[0] for e in node_entries[1:]}

    node_library = [
        _bone_node_entry(bid, label, parent_id, center, end)
        for (bid, label, parent_id, center, end) in node_entries
    ]

    geometries = []
    uv_sets = []
    all_modifiers = []
    materials_lib = []
    materials_scene = []
    geometry_instances = []  # for the figure root's scene "geometries" list
    modifier_instances = []
    # One {group_name: {diffuse_rgb, diffuse_image, overrides}} dict per mesh,
    # in mesh_objs order - matches DzObject.getShape(i) order 1:1 (confirmed
    # live). Kept as a list, NOT merged into one flat dict, because different
    # meshes can reuse the same material-slot name (see
    # _write_material_fixup_script's docstring for why that matters).
    materials_manifest_by_shape = []

    # Baked/complex-material textures land next to the export file, matching
    # how the reference Blender-to-Daz plugin bakes "simplified texture maps".
    textures_dir = os.path.join(os.path.dirname(os.path.abspath(filepath)),
                                 _safe_id(asset_name) + "_textures")

    def instance_id(lib_id):
        # See long comment on scene_nodes below for why this suffix exists.
        return f"{lib_id}-1"

    for mesh_obj in mesh_objs:
        mesh_id = _safe_id(mesh_obj.name)
        geo_id = mesh_id + "Base"
        uv_id = mesh_id + "UV"
        mod_id = mesh_id + "SkinBinding"

        geometry, uv_set, material_names = _build_geometry_and_uv(mesh_obj, geo_id, uv_id)
        skin_modifier = _build_skin_binding(mesh_obj, geo_id, figure_id, mod_id, bone_ids)
        mats_lib, mats_scene, mesh_materials_manifest = _build_materials(
            mesh_obj, mesh_id, geo_id, uv_id, material_names, textures_dir)
        materials_manifest_by_shape.append(mesh_materials_manifest)
        morphs_lib, morphs_scene = _build_morphs(mesh_obj, geo_id, instance_id(geo_id), mesh_id,
                                                  morph_smooth_iterations, morph_smooth_factor)

        geometries.append(geometry)
        uv_sets.append(uv_set)
        all_modifiers.append(skin_modifier)
        all_modifiers.extend(morphs_lib)
        materials_lib.extend(mats_lib)
        materials_scene.extend(mats_scene)

        geometry_instances.append({
            "id": instance_id(geo_id), "url": f"#{geo_id}", "name": geometry["name"],
            "label": mesh_obj.name, "type": "polygon_mesh",
        })
        modifier_instances.append({
            "id": instance_id(mod_id),
            "url": f"#{mod_id}",
            "parent": f"#{instance_id(figure_id)}",
        })
        # Morphs use no scene-level "parent" (see _build_morphs docstring) -
        # just the bare {id, url} instance real product files use.
        modifier_instances.extend(morphs_scene)

    # Scene-section objects are INSTANCES of library-section definitions and
    # need their own ids distinct from the library id they instance ("url"
    # points at the library id via "#id"). Real multi-file Daz assets get
    # away with reusing the same string for both because the library lives in
    # a separate .dsf file from the scene .duf - reusing it here, in one
    # self-contained file, produces Daz Studio's "duplicate ids found" import
    # error (confirmed live). Suffix every scene-instance id with "-1",
    # matching the convention Daz's own exporter uses for material instances
    # (e.g. "bearTongue-1").
    scene_nodes = []
    for i, (bid, label, parent_id, _center, _end) in enumerate(node_entries):
        n = {"id": instance_id(bid), "url": f"#{bid}", "name": bid, "label": label}
        if parent_id:
            # Node_library "parent" only defines the reusable prototype's
            # default; the SCENE graph is actually wired by this instance-level
            # "parent" field (confirmed against a real Daz-authored .duf),
            # and it must point at the PARENT's scene-instance id, not its
            # library id.
            n["parent"] = f"#{instance_id(parent_id)}"
        if i == 0:
            n["geometries"] = geometry_instances
        scene_nodes.append(n)

    figure_node_entry = node_library[0]
    if presentation_type:
        # Matches the real-asset convention for marking something as
        # conforming clothing (e.g. "Follower/Wardrobe/Top") vs. a standalone
        # figure/prop, confirmed against a shipped Daz clothing product.
        figure_node_entry["presentation"] = {
            "type": presentation_type,
            "label": "", "description": "", "icon_large": "",
            "colors": [[0.35, 0.35, 0.35], [1, 1, 1]],
        }
        if preferred_base:
            figure_node_entry["presentation"]["preferred_base"] = preferred_base

    duf = {
        "file_version": "0.6.0.0",
        "asset_info": {
            "id": f"/{asset_name}.duf",
            "type": "figure",
            "contributor": {"author": "Blender DUF Exporter", "email": "", "website": ""},
            "revision": "1.0",
        },
        "node_library": node_library,
        "geometry_library": geometries,
        "uv_set_library": uv_sets,
        "material_library": materials_lib,
        "modifier_library": all_modifiers,
        "scene": {
            "nodes": scene_nodes,
            "modifiers": modifier_instances,
            "materials": materials_scene,
        },
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(duf, f, indent="\t")

    fixup_script_path = os.path.splitext(filepath)[0] + "_fix_materials.dsa"
    _write_material_fixup_script(armature_obj.name, materials_manifest_by_shape, fixup_script_path)
    # Stashed after json.dump() so it can't leak into the written .duf file.
    duf["_fixup_script"] = fixup_script_path

    return duf
