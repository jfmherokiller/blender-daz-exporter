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


def _bone_node_entry(bone_id, label, parent_id, center_point, end_point, name=None):
    """One node_library entry for a bone (or the figure root when parent_id is None).

    `name` (DSON's internal identifier field, distinct from `id`) defaults to
    bone_id when not given. An attached mesh's independent skeleton copy
    needs a prefixed, collision-free `id` (see _build_mesh_figure) but its
    bones' `name` must stay the PLAIN, unprefixed bone name - confirmed by
    inspecting a real shipped conforming asset ("High Tops Wearable" for
    Genesis 8 Male): its bones' `name` fields are plain ("hip", "pelvis",
    "lThighBend", matching the target figure's own bone names exactly), not
    prefixed with the product name anywhere. Daz's conforming/"Fit To"
    engine matches bones between the follower's independent skeleton copy
    and the target's by this `name` field - prefixing it (as an earlier
    version of this exporter did, to dodge id collisions) silently breaks
    conforming: `conform_target` resolves fine and getFollowTarget() reads
    back correctly, but posing the target does nothing at all, because the
    engine never finds a same-named bone to mirror.
    """
    entry = {
        "id": bone_id,
        "name": name or bone_id,
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


def _prop_node_entry(node_id, label, end_point=(0.0, 0.0, 1.0)):
    """One node_library entry for a standalone static prop (type "node",
    not "bone"/"figure" - no skeleton, no skin_binding). Sits at the scene
    origin with identity transform since its geometry is already baked into
    world/Daz space (see _build_geometry_and_uv's world_space=True)."""
    return {
        "id": node_id,
        "name": node_id,
        "type": "node",
        "label": label,
        "rotation_order": "XYZ",
        "inherits_scale": True,
        "center_point": _xyz_channels("Origin", "Origin", (0.0, 0.0, 0.0), step=0.01),
        "end_point": _xyz_channels("End", "End", end_point, step=0.01),
        "orientation": _xyz_channels("Orientation", "Orientation", [0.0, 0.0, 0.0], step=0.1),
        "rotation": _xyz_channels("Rotate", "Rotate", [0.0, 0.0, 0.0], -180.0, 180.0, 0.5, True),
        "translation": _xyz_channels("Translate", "Translate", [0.0, 0.0, 0.0], step=1.0),
        "scale": _xyz_channels("Scale", "Scale", [1.0, 1.0, 1.0], -100.0, 100.0, 0.005),
        "general_scale": _channel("scale", "float", "Scale", "Scale", 1.0, 0.0, 100.0, 0.005),
    }


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


def _walk_bones(armature_obj, figure_end_point, allowed_names=None, root_label=None):
    """
    Yield (bone_id, label, parent_id_or_None, center_point_daz, end_point_daz)
    for the figure root followed by every bone, depth-first.

    If allowed_names is given (already expanded to include ancestors - see
    _expand_with_ancestors), bones outside that set - and their entire
    subtree - are skipped entirely. Safe because an included bone's ancestors
    are always included too by construction, so no parent-chain gaps result.

    root_label overrides the figure root's display label (default:
    armature_obj.name) - used by _build_mesh_figure() so each mesh's own
    independent skeleton copy is labeled after the MESH it belongs to.

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
    yield (figure_id, root_label or armature_obj.name, None, [0.0, 0.0, 0.0], figure_end_point)

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


def _build_geometry_and_uv(mesh_obj, geo_id, uv_id, world_space=False):
    """world_space=True bakes mesh_obj.matrix_world into the exported vertex
    positions - needed for a standalone prop node (see export_duf_prop),
    which has no bone/figure transform of its own to place it. Rigged
    figure meshes (the default, world_space=False) instead rely on the
    project convention that the mesh object's own transform is identity and
    its vertices already sit in the same space as the figure root node
    (translation [0,0,0])."""
    mesh = mesh_obj.data

    if world_space:
        mw = mesh_obj.matrix_world
        vertices = [to_daz_vec(mw @ v.co) for v in mesh.vertices]
    else:
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


def _build_skin_binding(mesh_obj, geo_id, figure_id, mod_id, bone_id_map):
    """bone_id_map: {plain_safe_id(vertex_group_name): actual_node_library_id}.
    Kept as a mapping rather than a flat set of valid ids because an
    attached mesh's independent skeleton copy uses PREFIXED node_library
    ids (e.g. "hoodie_chest") while its Blender vertex groups are still
    named plainly ("chest") - looking those plain names up directly
    against the prefixed id set silently finds no matches at all
    (confirmed live: a DzSkinBinding modifier structurally attached but
    getNumBoneBindings() read back 0). For the root mesh, plain name and
    actual id are identical, so this degenerates to the old behavior."""
    mesh = mesh_obj.data
    group_index_to_bone = {}
    for vg in mesh_obj.vertex_groups:
        plain = _safe_id(vg.name)
        bid = bone_id_map.get(plain)
        if bid is not None:
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


def _extract_alpha_channel(image, out_dir, basename):
    """
    Export just an Image's alpha channel as its own standalone grayscale
    PNG (R=G=B=alpha, alpha=1) - for channels like Cutout Opacity that need
    a genuine per-pixel mask image, not a reused copy of the full-color
    texture the shader's Alpha input happens to trace back to.

    This mirrors a technique already confirmed to work in a previous,
    separate PMX-import project: save the alpha channel out to its own
    image and wire it in as its own map, rather than pointing the cutout
    channel at the same file used for color. Baking (via the EMIT-reroute
    technique _bake_socket_to_image uses) was tried here first as the
    general-purpose fallback and produces a mask that LOOKS correct when
    inspected as a standalone image, but the corresponding real-render
    result still came out wrong for this mesh (a large multi-material
    fur-card mesh with heavily reused/overlapping UV space) - direct pixel
    extraction sidesteps whatever is going wrong with baking on this kind
    of geometry entirely, by not baking at all.
    """
    if image.channels < 4:
        return None  # no alpha channel to extract
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, basename[:60] + "_alpha.png")

    w, h = image.size
    if w == 0 or h == 0:
        return None

    import numpy as np
    src = np.empty(w * h * image.channels, dtype=np.float32)
    image.pixels.foreach_get(src)
    src = src.reshape((-1, image.channels))
    alpha = src[:, 3]

    out = np.ones((w * h, 4), dtype=np.float32)
    out[:, 0] = alpha
    out[:, 1] = alpha
    out[:, 2] = alpha

    new_img = bpy.data.images.new(basename[:60] + "_alpha", width=w, height=h, alpha=True)
    try:
        new_img.pixels.foreach_set(out.reshape(-1))
        new_img.filepath_raw = target
        new_img.file_format = "PNG"
        new_img.save()
    finally:
        bpy.data.images.remove(new_img)
    return target if os.path.isfile(target) else None


def _direct_image(inp, out_dir, basename):
    """If `inp` is fed by nothing but a single Image Texture node (directly,
    through one or more Reroute nodes, or through one Normal Map node),
    return an appropriate image filepath - cheap reuse, no baking needed
    since there's no processing to resolve.

    Dispatches on which OUTPUT of the Image Texture node actually feeds
    `inp`, traced through any Reroute passthroughs in between (confirmed
    live: a real fur-card material's Alpha input is fed via exactly one
    Reroute node straight from an Image Texture's Alpha output):
    - "Color" output: reuse the file directly (_resolve_image_file) -
      cheap and correct, this is the same pixel data either way.
    - "Alpha" output: extract just the alpha channel as its own standalone
      grayscale image (_extract_alpha_channel). Reusing the full-color file
      here is wrong - Daz reads an image_file map on a scalar channel like
      Cutout Opacity via the file's own luminance, not any alpha channel
      embedded in it, and a colorful fur-card texture's luminance has
      nothing to do with its actual transparency shape.
    """
    if not inp.is_linked:
        return None
    link = inp.links[0]
    from_node = link.from_node
    from_socket = link.from_socket
    depth = 0
    while from_node.type == "REROUTE" and depth < 8:
        reroute_input = from_node.inputs[0]
        if not reroute_input.is_linked:
            return None
        link = reroute_input.links[0]
        from_node = link.from_node
        from_socket = link.from_socket
        depth += 1

    if from_node.type == "TEX_IMAGE" and from_node.image:
        if from_socket.name == "Alpha":
            return _extract_alpha_channel(from_node.image, out_dir, basename)
        if from_socket.name != "Color":
            return None
        return _resolve_image_file(from_node.image, out_dir, basename)
    if from_node.type == "NORMAL_MAP":
        color_in = from_node.inputs.get("Color")
        if color_in and color_in.is_linked:
            tex_node = color_in.links[0].from_node
            if tex_node.type == "TEX_IMAGE" and tex_node.image:
                return _resolve_image_file(tex_node.image, out_dir, basename)
    return None


_FACTOR_SOCKET_IDS = ("Fac", "Factor", "Factor_Float", "Factor_Vector")


def _heuristic_upstream_image_search(socket, out_dir, basename, visited, skip_factor):
    """Recursive worker for _heuristic_upstream_image. When skip_factor is
    True, refuses to descend into a blend-weight input (the "Fac" input of a
    legacy MixRGB node, or the "Factor_Float"/"Factor_Vector" duplicate
    sockets of Blender 4.x's unified Mix node - both display as "Factor" in
    the UI but only the identifier reliably tells them apart, same pitfall
    as the hidden A/B duplicate sockets), usually fed by a mask/AO ColorRamp
    rather than real color data, so a first pass can prefer genuine
    color-carrying textures over masks reached via a shorter path. Also
    skips disabled duplicate sockets (e.g. Factor_Vector when the node's
    data_type is COLOR) since Blender 4.x's unified nodes keep every
    data-type variant of a socket present but inert."""
    if not socket.is_linked:
        return None
    node = socket.links[0].from_node
    if node.name in visited:
        return None
    visited.add(node.name)
    if node.type == "TEX_IMAGE" and node.image:
        return _resolve_image_file(node.image, out_dir, basename)
    for inp in node.inputs:
        if not inp.enabled:
            continue
        if skip_factor and inp.identifier in _FACTOR_SOCKET_IDS:
            continue
        img = _heuristic_upstream_image_search(inp, out_dir, basename, visited, skip_factor)
        if img:
            return img
    return None


def _heuristic_upstream_image(socket, out_dir, basename):
    """
    Fallback for when baking isn't available (see _bake_socket_to_image):
    walk upstream through the node graph and return an Image Texture found
    anywhere feeding this socket, ignoring how it's actually combined
    (Mix/ColorRamp/etc). Not correct for genuinely blended multi-texture
    materials - only used as a last resort.

    Two passes: first refusing to descend into Mix/MixRGB "Fac"/"Factor"
    inputs (typically fed by a mask, e.g. an AO ColorRamp, not real color
    data - confirmed live to otherwise grab a grayscale AO mask instead of
    the actual colored texture one hop further away), then falling back to
    an unrestricted search if that finds nothing.
    """
    img = _heuristic_upstream_image_search(socket, out_dir, basename, set(), skip_factor=True)
    if img:
        return img
    return _heuristic_upstream_image_search(socket, out_dir, basename, set(), skip_factor=False)


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
# Same story for Clearcoat -> Coat (renamed in Blender 4.0, which also added a
# tint/IOR pair the older Principled BSDF never had - aliases resolve to None
# there via _find_socket, which resolve() already treats as "no override".
_COAT_WEIGHT_ALIASES = ("Coat Weight", "Clearcoat")
_COAT_ROUGHNESS_ALIASES = ("Coat Roughness", "Clearcoat Roughness")
_COAT_TINT_ALIASES = ("Coat Tint",)


def _find_socket(bsdf, aliases):
    for name in aliases:
        inp = bsdf.inputs.get(name)
        if inp is not None:
            return inp
    return None


def _iray_overrides(mat, mesh_obj, textures_dir, id_prefix, bake_textures=True):
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

        bake_textures=False skips the bake step entirely, going straight to
        the heuristic upstream-image fallback - an escape hatch for
        materials where baking produces a WRONG result rather than merely
        an approximate one. Confirmed live: a material blending an AO map
        with a procedural Noise Texture (Generated/3D-position-based, not
        UV-based) baked to a flat, detail-less patch specifically on a
        small/thin mesh region (a tail) while baking correctly everywhere
        else on the same body - reproducible across repeated bakes, not a
        caching fluke. The likely mechanism: the material's Overlay blend
        mode is a no-op when the noise value it's blending against happens
        to be near 0.5, which can happen when a small mesh region's full
        3D extent falls within less than one wavelength of the noise
        function. Root-causing that fully (e.g. isolating a bake to a
        sub-mesh with its own tighter bounding box, changing what
        "Generated" coordinates mean for it) is unsolved - this flag is the
        pragmatic workaround: skip baking, fall back to whatever real
        Image Texture is upstream instead, trading exact multi-layer
        color/AO/noise fidelity for a predictable, non-broken result.

        If baking raises (some environments can't run bpy.ops.object.bake -
        e.g. confirmed non-functional when invoked through this project's
        Blender MCP scripting bridge specifically, "No valid selected
        objects" despite every Python-level selection/visibility check
        passing - looks like a context restriction in that execution path,
        not a flaw in the baking logic itself), also falls back to the
        heuristic upstream-image search rather than aborting the whole
        export."""
        if inp is None:
            return None, None
        if not inp.is_linked:
            return inp.default_value, None
        basename = f"{id_prefix}_{bake_name}"
        direct = _direct_image(inp, textures_dir, basename)
        if direct:
            return None, direct
        if bake_textures:
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
            if channel_id == "Emission Color":
                # Daz's Emission Color channel is a plain multiplier on the
                # emissive shader, not clamped to [0,1] - real Iray Uber
                # materials commonly push it above 1 for a strong glow
                # (documented Iray convention, not a guess). Folding
                # Blender's separate Emission Strength multiplier in here
                # is the direct equivalent of what Blender's own viewport/
                # Cycles shading already does (final emission = color *
                # strength) - unlike Bump Strength below, this isn't a
                # cross-application unit-scale guess, it's the same
                # multiply-two-linear-factors-together operation on both
                # sides.
                value_to_write = [c * strength for c in val[:3]]
            else:
                value_to_write = val
            overrides[channel_id] = (value_to_write, None)
        elif img:
            # Iray Uber multiplies value * map - leaving value at None means
            # the fixup script never calls setValue(), so the channel keeps
            # whatever the stock Iray Uber Base preset default is. Confirmed
            # live that default is 0 for "Glossy Roughness" (and likely
            # other channels too), which multiplies a perfectly good baked
            # map down to nothing - the surface renders mirror-smooth
            # everywhere regardless of the map's actual content (confirmed:
            # a real baked roughness map was present, isMapped() true, but
            # the material still looked far too shiny). 1.0 is the neutral
            # "let the map fully drive this channel" multiplier - same fix
            # already applied to Base Color (-> white) and Normal Map
            # (-> 1.0) above/below; this closes the gap for the rest.
            emission_default = [strength, strength, strength] if channel_id == "Emission Color" else 1.0
            overrides[channel_id] = (emission_default, img)

    spec_inp = _find_socket(bsdf, _SPECULAR_ALIASES)
    val, img = resolve(spec_inp, "Specular")
    if val is not None:
        overrides["Glossy Reflectivity"] = (val, None)
    elif img:
        overrides["Glossy Reflectivity"] = (1.0, img)

    # Anisotropic/Anisotropic Rotation -> Glossy Anisotropy/Glossy Anisotropy
    # Rotations: both sides use the same convention (Anisotropy: 0 = round
    # highlight, 1 = fully stretched; Rotation: 0-1 fraction of a full turn),
    # a direct 1:1 mapping with no unit-scale guess involved - matches IOR's
    # reasoning above, unlike Bump Strength's cross-application scale gap.
    aniso_inp = bsdf.inputs.get("Anisotropic")
    val, img = resolve(aniso_inp, "Anisotropic")
    if val is not None and val > 0:
        overrides["Glossy Anisotropy"] = (val, None)
    elif img:
        overrides["Glossy Anisotropy"] = (1.0, img)

    if "Glossy Anisotropy" in overrides:
        aniso_rot_inp = bsdf.inputs.get("Anisotropic Rotation")
        val, img = resolve(aniso_rot_inp, "AnisotropicRotation")
        if val is not None:
            overrides["Glossy Anisotropy Rotations"] = (val, None)
        elif img:
            overrides["Glossy Anisotropy Rotations"] = (1.0, img)

    trans_inp = _find_socket(bsdf, _TRANSMISSION_ALIASES)
    val, img = resolve(trans_inp, "Transmission")
    if val is not None and val > 0:
        overrides["Refraction Weight"] = (val, None)
    elif img:
        overrides["Refraction Weight"] = (1.0, img)

    # IOR: direct 1:1 value - both Blender's "IOR" socket and Daz's
    # "Refraction Index" channel are the same physical quantity (Blender
    # default 1.45, Daz's Iray Uber template default 1.5 - close enough that
    # writing the actual value is strictly more correct than leaving the
    # template default).
    ior_inp = bsdf.inputs.get("IOR")
    val, img = resolve(ior_inp, "IOR")
    if val is not None:
        overrides["Refraction Index"] = (val, None)
    elif img:
        overrides["Refraction Index"] = (1.5, img)

    # Clearcoat/Coat: same value/map priority as every other channel above.
    # Roughness/tint are only worth writing once a non-zero coat weight is
    # actually present (Top Coat Weight's template default is 0 - inert - so
    # a coat-less material has nothing for roughness/tint to visibly affect).
    coat_weight_inp = _find_socket(bsdf, _COAT_WEIGHT_ALIASES)
    val, img = resolve(coat_weight_inp, "CoatWeight")
    if val is not None and val > 0:
        overrides["Top Coat Weight"] = (val, None)
    elif img:
        overrides["Top Coat Weight"] = (1.0, img)

    if "Top Coat Weight" in overrides:
        coat_rough_inp = _find_socket(bsdf, _COAT_ROUGHNESS_ALIASES)
        val, img = resolve(coat_rough_inp, "CoatRoughness")
        if val is not None:
            overrides["Top Coat Roughness"] = (val, None)
        elif img:
            overrides["Top Coat Roughness"] = (1.0, img)

        coat_tint_inp = _find_socket(bsdf, _COAT_TINT_ALIASES)
        val, img = resolve(coat_tint_inp, "CoatTint")
        if val is not None:
            overrides["Top Coat Color"] = (list(val[:3]), None)
        elif img:
            overrides["Top Coat Color"] = ([1.0, 1.0, 1.0], img)

        # Coat IOR: same direct 1:1 physical-quantity mapping as the base
        # IOR channel above - only worth writing alongside an actual coat
        # layer (template default 1.5 is already inert with Top Coat Weight
        # at 0, same reasoning as roughness/tint just above).
        coat_ior_inp = bsdf.inputs.get("Coat IOR")
        val, img = resolve(coat_ior_inp, "CoatIOR")
        if val is not None:
            overrides["Top Coat IOR"] = (val, None)
        elif img:
            overrides["Top Coat IOR"] = (1.5, img)

    # Subsurface Scattering -> Daz's /Volume/Scattering group (SSS Amount/
    # Color/Direction/Scattering Measurement Distance) + Thin Walled.
    #
    # Live-verified 2026-08-24 against a real Genesis 9 base figure: Thin
    # Walled defaults to true on the stock Iray Uber Base preset, which
    # makes the entire /Volume/Scattering group inert regardless of SSS
    # Amount/Color - confirmed live (setValue(false) and setValue(0) both
    # work on this DzBoolProperty). So Thin Walled must be explicitly
    # flipped false whenever SSS is actually wanted here, or every other
    # channel below silently does nothing. This environment has no
    # commercially-authored skin content installed (only the bare, unskinned
    # base figure), so only the raw preset's own template defaults could be
    # confirmed this way - not how a real shipped skin product actually
    # authors these channels day to day.
    #
    # Blender's Principled BSDF has no separate Subsurface Color input as of
    # 4.0+ (tint comes entirely from Base Color - confirmed live by
    # enumerating every input on this Blender version) and no direct analog
    # to Daz's Translucency Weight/Color, Base Color Effect, or SSS
    # Reflectance Tint (a thin-surface backlight effect distinct from true
    # volumetric SSS) - those are deliberately left untouched here (stay at
    # template default) rather than guessed, same as Sheen was deliberately
    # left unmapped above for having no Daz equivalent.
    sss_weight_inp = bsdf.inputs.get("Subsurface Weight") or bsdf.inputs.get("Subsurface")
    val, img = resolve(sss_weight_inp, "SubsurfaceWeight")
    if (val is not None and val > 0) or img:
        overrides["SSS Amount"] = (val if val is not None else 1.0, img)
        overrides["Thin Walled"] = (False, None)

        # SSS Color: Daz's channel is the tint of light after scattering -
        # the closest available analog is the same resolved Base Color
        # value already feeding Diffuse Color above (Blender's own SSS
        # model reuses Base Color as its scattering albedo, having no
        # separate Subsurface Color input to read instead).
        overrides["SSS Color"] = (list(diffuse_rgb[:3]), diffuse_image)

        # Scattering Measurement Distance: Daz's single scalar "at this
        # thickness, this color was measured" distance. Blender's per-
        # channel Subsurface Radius (a mean-free-path vector, meters) times
        # Subsurface Scale (also meters) is the closest physical analog;
        # collapsed to one scalar via the max channel (the longest-
        # travelling channel is usually the perceptually dominant one, e.g.
        # skin's red channel) since Daz has no separate per-channel
        # scattering-distance channel to receive the other two components.
        # Converted meters -> centimeters with the same *100 factor this
        # exporter already uses for every other Blender-to-Daz unit
        # conversion (see to_daz_vec). Only handled when both Radius and
        # Scale are plain values, not texture-driven - an extremely uncommon
        # authoring pattern for these two sockets, and baking a "color" from
        # a distance vector has no clear meaning, so this is left at the
        # template default in that case rather than guessed.
        radius_inp = bsdf.inputs.get("Subsurface Radius")
        scale_inp = bsdf.inputs.get("Subsurface Scale")
        if (radius_inp is not None and not radius_inp.is_linked
                and scale_inp is not None and not scale_inp.is_linked):
            radius_val = radius_inp.default_value[:3]
            scale_val = scale_inp.default_value
            distance_cm = max(radius_val) * scale_val * 100.0
            if distance_cm > 0:
                overrides["Scattering Measurement Distance"] = (distance_cm, None)

        # SSS Direction: Daz's -1..1 Henyey-Greenstein phase-function
        # anisotropy ("g") vs. Blender's 0..1 forward-scattering-only
        # Anisotropy factor - same convention (0 = isotropic, higher =
        # stronger forward scattering) over the positive half of Daz's
        # range, a direct value copy like Anisotropic -> Glossy Anisotropy
        # above, not a unit-scale guess.
        aniso_ss_inp = bsdf.inputs.get("Subsurface Anisotropy")
        val, img = resolve(aniso_ss_inp, "SubsurfaceAnisotropy")
        if val is not None and val > 0:
            overrides["SSS Direction"] = (val, None)

    normal_inp = bsdf.inputs.get("Normal")
    if normal_inp is not None and normal_inp.is_linked:
        normal_from_node = normal_inp.links[0].from_node
        if normal_from_node.type == "BUMP":
            # Height-based bump (Blender's Bump node) rather than a
            # tangent-space Normal Map - maps to Daz's separate "Bump
            # Strength" channel instead of "Normal Map". Only the map is
            # written, not a translated strength value: Blender's Bump
            # "Strength" is a 0-1 multiplier while Daz's "Bump Strength"
            # channel is a 0-50 range whose own template default (5, see
            # iray_uber_channels_template.json) is already a sane
            # "map present" working value - not confirmed live what scale
            # factor would correctly translate one range into the other, so
            # this leaves that value at the template default rather than
            # guess and risk a far-too-strong or near-invisible result.
            height_inp = normal_from_node.inputs.get("Height")
            _, bump_img = resolve(height_inp, "Bump")
            if bump_img:
                overrides["Bump Strength"] = (None, bump_img)
        else:
            # normal maps: reuse only, never bake (EMIT-baking would re-encode
            # the already-tangent-space-correct map incorrectly)
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


def _build_materials(mesh_obj, mesh_id, geo_id, uv_id, material_names, textures_dir, bake_textures=True):
    """mesh_id prefixes every material id so multiple meshes' materials
    (e.g. two different meshes both having a slot literally named the same)
    can't collide in the shared file-wide id namespace.

    bake_textures=False skips Cycles baking for procedurally-fed channels
    entirely (see _iray_overrides' resolve() docstring) - an escape hatch
    for materials where baking produces a wrong result rather than merely
    an approximate one.

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
        diffuse_rgb, diffuse_image, overrides = _iray_overrides(mat, mesh_obj, textures_dir, mat_id, bake_textures)
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


def _ensure_hide_material_shapekeys(mesh_obj):
    """
    For every material on mesh_obj flagged `daz_hide_on_export=True` (a
    custom property the addon registers on bpy.types.Material - see
    __init__.py's MATERIAL_PT_daz_export panel), ensure a shape key exists
    that collapses that material's faces to near-zero scale, and return
    their names so the caller can pass them to _build_morphs' `force_active`
    param (channel default 1.0 - "hidden" is the rest state, no manual
    dialing needed once loaded in Daz).

    Found live: Daz's Iray renderer can make certain geometry (confirmed:
    a disconnected card-based hair shell, not topologically joined to the
    rest of the mesh) look badly wrong - hard-edged black banding that
    survived every material-property fix tried (Cutout Opacity, Diffuse
    Color, environment lighting) - most likely from shadow-casting/AO
    contribution that Cutout Opacity alone doesn't suppress. Actually
    shrinking the geometry away (not just hiding its material) fixed it.

    Deliberately does NOT delete geometry: other modifiers/shape keys
    reference vertices by index, and deleting changes indices out from under
    them - confirmed live this breaks other morphs. A near-zero-scale shape
    key is topology-preserving (same vertex/polygon count and indices) and
    safe to add alongside existing morphs.

    Collapses each vertex toward the average center of its own owning
    hidden-material face(s) - a cheap stand-in for scaling with an
    "Individual Origins" pivot in Blender, so the geometry visually
    collapses roughly in place instead of every vertex converging on one
    shared point. A vertex that's ALSO used by a non-hidden material's face
    is left untouched even if it touches a hidden face too - collapsing a
    shared boundary vertex would distort the geometry that's being kept.

    Idempotent: if a shape key of the expected name already exists, it's
    left as-is and just reported back - safe to call on every export
    without piling up duplicates as the user iterates, and lets a user
    hand-tune the collapse afterward (e.g. a different scale factor) without
    losing their edit on the next export.
    """
    mesh = mesh_obj.data
    hide_mat_indices = [
        i for i, m in enumerate(mesh.materials)
        if m is not None and getattr(m, "daz_hide_on_export", False)
    ]
    if not hide_mat_indices:
        return []

    if not mesh.shape_keys:
        mesh_obj.shape_key_add(name="Basis")
    basis = mesh.shape_keys.key_blocks[0]

    created = []
    collapse_scale = 0.001
    for mat_idx in hide_mat_indices:
        mat = mesh.materials[mat_idx]
        key_name = f"DazHide_{_safe_id(mat.name)}"
        if key_name in mesh.shape_keys.key_blocks:
            created.append(key_name)
            continue

        hide_vert_faces = {}
        keep_verts = set()
        for poly in mesh.polygons:
            if poly.material_index == mat_idx:
                center = poly.center.copy()
                for vi in poly.vertices:
                    hide_vert_faces.setdefault(vi, []).append(center)
            else:
                keep_verts.update(poly.vertices)

        sk = mesh_obj.shape_key_add(name=key_name, from_mix=False)
        for vi, centers in hide_vert_faces.items():
            if vi in keep_verts:
                continue
            pivot = sum(centers, Vector()) / len(centers)
            orig = basis.data[vi].co
            sk.data[vi].co = pivot + (orig - pivot) * collapse_scale
        created.append(key_name)

    return created


def _build_morphs(mesh_obj, geo_id, geo_instance_id, mesh_id, smooth_iterations=2, smooth_factor=0.5,
                   force_active=frozenset()):
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
        is_forced = kb.name in force_active
        raw = {}
        for i in range(vertex_count):
            d = kb.data[i].co - basis.data[i].co
            if d.length > 1e-6:
                raw[i] = d

        # A force-active "hide geometry" shape key is a deliberate hard
        # structural collapse, not a sculpted correction - boundary
        # softening would partially un-collapse its edge vertices (pulling
        # them toward their zero-delta kept-geometry neighbors) and risks
        # bleeding a slight shrink into the geometry being kept. Skip it.
        smoothed = raw if is_forced else (
            _smooth_sparse_deltas(raw, adjacency, smooth_iterations, smooth_factor)
            if smooth_iterations > 0 else raw)

        deltas = []
        for i, d in smoothed.items():
            if d.length > 1e-6:
                dx, dy, dz = to_daz_vec(d)
                deltas.append([i, dx, dy, dz])
        if not deltas:
            continue

        morph_id = f"{mesh_id}_{_safe_id(kb.name)}"
        rest_value = 1.0 if is_forced else 0.0
        modifiers_lib.append({
            "id": morph_id,
            "name": morph_id,
            "parent": f"#{geo_id}",
            "presentation": {
                "type": "Modifier/Shape",
                "label": "", "description": "", "icon_large": "",
                "colors": [[0.3529412, 0.3529412, 0.3529412], [1, 1, 1]],
            },
            "channel": _channel("value", "float", "Value", kb.name, rest_value, 0.0, 1.0, 0.01, True) | {
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
        # json.dumps rather than an f-string interpolation: a Python bool
        # (needed for Thin Walled, the first bool-typed channel this
        # exporter writes) renders as "False"/"True" via str() - invalid JS
        # - but correctly as "false"/"true" via json.dumps. Every existing
        # numeric value already round-trips identically through either path
        # (json.dumps(0.5) == str(0.5)), so this is strictly safer with no
        # behavior change for the channels already in use.
        lines.append(f"{prop_expr}.setValue({json.dumps(value)});")
    if image:
        lines.append(f"{prop_expr}.setMap({_js_str(image.replace(chr(92), '/'))});")
    return "\n".join(lines)


def _material_fixup_body_lines(materials_manifest_by_node, preset_path=_BUNDLED_UBER_BASE_PRESET):
    """
    Builds the material-fixup logic as a self-contained IIFE
    (`["(function(){", ..., "})();"]`) that upgrades every material zone
    across a set of scene nodes from the legacy DzDefaultMaterial our raw
    .duf merge produces to genuine DzUberIrayMaterial, then re-applies the
    diffuse/PBR channel values and baked texture maps this exporter already
    computed. Factored out from _write_material_fixup_script so
    _write_combined_fixup_script can also embed this logic (nested inside
    its own outer script) without duplicating it - see that function's
    docstring for why a combined single-script output exists at all.

    materials_manifest_by_node: a list of (node_label, {group_name: {...}})
    pairs, one per exported mesh/figure - every exported mesh is its own
    separate scene node/figure with its own single shape (see
    _build_mesh_figure), so unlike an earlier single-shared-node design,
    there's no shape-index-switching or cross-mesh material-name collision
    risk to handle here.

    WHY THIS EXISTS: describing a "studio_material_channels" block by hand in
    a merged .duf (what this exporter used to rely on exclusively) is not
    enough to make Daz Studio instantiate the real Iray Uber shader class -
    confirmed by direct isolation testing (see DUF_EXPORT_NOTES.md), TWICE:
    once with a byte-for-byte copy of a real working Iray Uber material's
    channel data dropped into a self-contained single-file export, and again
    (2026-08-11) with that same data moved to a genuinely separate companion
    .dsf file (asset_info.type "preset_shader", matching how every real
    shipped product's materials actually live on disk) and referenced via an
    external file `url` instead of a same-document `#id` fragment - still
    `DzDefaultMaterial` either way. Daz Studio's plain scene-merge parser
    just never promotes a material past `DzDefaultMaterial` from raw JSON,
    regardless of where that JSON lives. What *does* work is going through
    Daz's own preset-application codepath: select the node, then
    App.getContentMgr().openFile() a real "preset_shader"-type .duf (this is
    exactly what happens when a user drags a shader preset from Smart
    Content onto a figure). That path builds a native shader object instead
    of parsing one from raw JSON, but it also resets every channel to shader
    defaults - so step 2 re-applies our actual values/maps through the live
    DzMaterial property API (setFloatColorValue / setValue / setMap), which
    is confirmed to work uniformly across both color and plain numeric
    channels.

    This can't be triggered automatically on scene load/merge - DSON has no
    supported hook for that, and Daz's one real "auto-run a script" mechanism
    (a `.dsa` under `Runtime/Support`, SKILL_PACKAGING.md) only fires once
    per file during a Content Library directory *scan* (registering CMS
    metadata), not each time an associated asset is actually loaded/merged
    into a scene - confirmed by re-reading daz-mcp-server's packaging docs
    while investigating this, not by a live test (that mechanism requires a
    full DIM-style content-library install this project doesn't do). Meant
    to run once, after the .duf it's paired with has been loaded/merged into
    the scene (e.g. via this project's `daz` MCP tools: daz_load_file then
    daz_execute_file, or manually through Daz Studio's Window > Script IDE).
    """
    lines = [
        "(function(){",
        f"    var presetPath = {_js_str(preset_path)};",
        "    var fixedZones = [];",
        "    var missingZones = [];",
    ]

    for node_label, manifest_dict in materials_manifest_by_node:
        lines.append("")
        lines.append(f"    // --- node {node_label!r} ---".replace("'", '"'))
        lines.append(f"    (function(){{")
        lines.append(f"        var node = Scene.findNodeByLabel({_js_str(node_label)});")
        lines.append(f"        if (!node) node = Scene.findNode({_js_str(node_label)});")
        lines.append(f"        if (!node) {{ missingZones.push({_js_str(node_label)} + \":<node not found>\"); return; }}")
        lines.append("        var shape = node.getObject().getCurrentShape();")
        lines.append("")
        lines.append("        Scene.selectAllNodes(false);")
        lines.append("        node.select(true);")
        lines.append("        for (var i = 0; i < shape.getNumMaterials(); i++) shape.getMaterial(i).select(true);")
        lines.append("        var presetOk = App.getContentMgr().openFile(presetPath, new DzFileIOSettings(), false);")
        lines.append("        if (!presetOk) throw new Error(\"Failed to apply Iray Uber Base preset on node \" + node.getLabel() + \": \" + presetPath);")

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
        "    return {",
        "        fixedZones: fixedZones,",
        "        missingZones: missingZones",
        "    };",
        "})();",
    ])
    return lines


def _write_material_fixup_script(materials_manifest_by_node, script_path,
                                  preset_path=_BUNDLED_UBER_BASE_PRESET):
    """Standalone-file wrapper around _material_fixup_body_lines - used when
    there's no attached-mesh rig transfer to combine it with (see
    _write_combined_fixup_script for the multi-mesh case)."""
    lines = [
        "// Auto-generated by blender_daz_exporter - upgrades DzDefaultMaterial",
        "// zones to Iray Uber and restores their channel values/maps.",
        "// See DUF_EXPORT_NOTES.md 'Materials: shader-class fixup' for why this exists.",
    ] + _material_fixup_body_lines(materials_manifest_by_node, preset_path)

    script_text = "\n".join(lines)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    return script_path


def _build_mesh_figure(mesh_obj, armature_obj, textures_dir, is_root,
                        morph_smooth_iterations, morph_smooth_factor, bake_textures=True):
    """
    Build a fully self-contained figure - its own pruned skeleton copy,
    geometry, skin_binding, materials, and morphs - for ONE mesh. The
    building block for export_duf()'s multi-figure output: every exported
    mesh becomes a separate scene node this way, one designated the root
    (is_root=True, keeps plain bone ids) and the rest attached as their own
    independent conforming figures parented AND conform_target-ed to it.

    Mirrors real multi-piece conforming Daz assets, confirmed by inspecting
    two real shipped products directly (a "High Tops Wearable" for Genesis
    8 Male, and an "Otter Tail" prop - both gzip-compressed .duf despite the
    extension): each one's root figure carries its OWN full copy of the
    target's skeleton (own "hip"/"pelvis"/etc bones - separate DzBone
    objects from the target's), with a skin_binding modifier referencing
    only its OWN bones. What actually makes it follow the target figure's
    pose is the scene node's "conform_target" field (plus "parent") - a
    DECLARATIVE DSON field. Live-validated using the real, unmodified High
    Tops file: merged it onto a loaded Genesis 8 Male and posed the thigh -
    the shoe moved correctly, zero script involved. (An earlier attempt at
    achieving the same effect purely via a post-load script calling
    node.setFollowTarget() registered the relationship but did NOT sync
    bone rotations - conform_target is a different, and actually-effective,
    mechanism.)

    Bone `id`s for a non-root mesh are prefixed with its own mesh_id so its
    independent skeleton copy can't collide with the root's (or another
    attached mesh's) ids in the shared node_library - e.g. root gets plain
    "chest", an attached "hoodie" mesh gets "hoodie_chest". Their `name`
    fields, however, stay PLAIN/unprefixed (see _bone_node_entry's
    docstring - conforming matches bones by `name`, and the real High Tops
    file's bone names are never prefixed with the product name).

    Returns a dict with everything export_duf() needs to splice this
    figure's data into the combined file: node_entries (this figure's own
    (bone_id, label, parent_id, center, end) tuples, ids already prefixed),
    node_library, figure_id (this figure's own root bone id), geometry,
    uv_set, geometry_instance, modifiers_lib/modifier_instances,
    materials_lib/materials_scene/materials_manifest, and label.
    """
    mesh_id = _safe_id(mesh_obj.name)
    allowed_names = _expand_with_ancestors(used_bone_names([mesh_obj], armature_obj), armature_obj)
    bbox_max_z = max((v[2] for v in mesh_obj.bound_box), default=1.0)
    figure_end = to_daz_vec((0.0, 0.0, bbox_max_z))

    raw_entries = list(_walk_bones(armature_obj, figure_end, allowed_names, root_label=mesh_obj.name))
    if is_root:
        node_entries = raw_entries
    else:
        def prefix(bid):
            return f"{mesh_id}_{bid}"
        node_entries = [
            (prefix(bid), label, (prefix(parent_id) if parent_id else None), center, end)
            for (bid, label, parent_id, center, end) in raw_entries
        ]

    figure_id = node_entries[0][0]
    # plain vertex-group-matchable bone name -> actual (possibly prefixed)
    # node_library id. See _build_skin_binding's docstring for why this
    # can't just be a flat set of the (possibly prefixed) ids.
    bone_id_map = {raw[0]: entry[0] for raw, entry in zip(raw_entries[1:], node_entries[1:])}
    node_library = [
        _bone_node_entry(bid, label, parent_id, center, end, name=raw[0])
        for (bid, label, parent_id, center, end), raw in zip(node_entries, raw_entries)
    ]

    geo_id = mesh_id + "Base"
    uv_id = mesh_id + "UV"
    mod_id = mesh_id + "SkinBinding"

    def instance_id(lib_id):
        return f"{lib_id}-1"

    hide_shapekey_names = _ensure_hide_material_shapekeys(mesh_obj)
    geometry, uv_set, material_names = _build_geometry_and_uv(mesh_obj, geo_id, uv_id)
    skin_modifier = _build_skin_binding(mesh_obj, geo_id, figure_id, mod_id, bone_id_map)
    mats_lib, mats_scene, materials_manifest = _build_materials(
        mesh_obj, mesh_id, geo_id, uv_id, material_names, textures_dir, bake_textures)
    morphs_lib, morphs_scene = _build_morphs(mesh_obj, geo_id, instance_id(geo_id), mesh_id,
                                              morph_smooth_iterations, morph_smooth_factor,
                                              force_active=hide_shapekey_names)

    geometry_instance = {
        "id": instance_id(geo_id), "url": f"#{geo_id}", "name": geometry["name"],
        "label": mesh_obj.name, "type": "polygon_mesh",
    }
    modifier_instances = [{
        "id": instance_id(mod_id),
        "url": f"#{mod_id}",
        # Parent to the GEOMETRY's own scene instance, not the figure/bone
        # root's - confirmed against the real High Tops file: its
        # SkinBinding modifier's scene-instance parent is "#HighTops-4"
        # (the geometry's own nested instance id), not "#HighTops-3" (the
        # figure/node's own instance id), even though both live "on" the
        # same node. Matches the pattern morphs already use correctly.
        "parent": f"#{instance_id(geo_id)}",
    }] + morphs_scene

    return {
        "label": mesh_obj.name,
        "node_entries": node_entries,
        "node_library": node_library,
        "figure_id": figure_id,
        "geometry": geometry,
        "uv_set": uv_set,
        "geometry_instance": geometry_instance,
        "modifiers_lib": [skin_modifier] + morphs_lib,
        "modifier_instances": modifier_instances,
        "materials_lib": mats_lib,
        "materials_scene": mats_scene,
        "materials_manifest": materials_manifest,
    }


def _build_mesh_prop(mesh_obj, textures_dir, morph_smooth_iterations, morph_smooth_factor,
                      bake_textures=True):
    """
    Build a standalone PROP (geometry + materials + morphs, no
    skeleton/skin_binding at all) for one attached mesh - replaces the old
    independent-skeleton-copy + conform_target approach for every mesh
    except the root. That approach loaded and resolved cleanly (conform_target
    correctly registered a follow relationship, getFollowTarget() read back
    right) but never actually deformed with the target figure's pose no
    matter what was fixed about its DSON structure - see git history / prior
    revisions of this file for that investigation.

    Real deformation now comes from Daz's own weight-transfer engine instead
    of a hand-authored one: export_duf() writes each attached mesh as a
    prop via this function, then bundles a companion "_rig_transfer.dsa"
    script (see _write_rig_transfer_script) that, once run after load,
    calls DzFigure.convertPropToFigure() to promote this prop to a figure
    and then DzTransferUtility.doTransfer() (setTransferBinding(true)) to
    compute genuine proximity-projected bone weights against the root
    figure's actual skeleton - confirmed live to produce real pose-following
    deformation (bounding box changes shape under a pose change, not just a
    rigid parent-follow) where conform_target produced zero movement.

    mesh_obj must be at an identity object transform, in the same space as
    the root mesh and armature_obj - same convention _build_mesh_figure
    already relies on (see _build_geometry_and_uv's world_space=False
    default). Not to be confused with export_duf_prop(), the standalone
    single-prop entry point (which bakes world_space=True since it has no
    figure-root convention to lean on) - kept separate rather than sharing
    one function because their id/instance-suffix conventions differ (this
    one must slot into export_duf()'s shared node/geometry/material
    library lists without id collisions across multiple attached meshes).
    """
    mesh_id = _safe_id(mesh_obj.name)
    geo_id = mesh_id + "Base"
    uv_id = mesh_id + "UV"

    bbox_max_z = max((v[2] for v in mesh_obj.bound_box), default=1.0)
    end_point = to_daz_vec((0.0, 0.0, bbox_max_z))

    hide_shapekey_names = _ensure_hide_material_shapekeys(mesh_obj)
    geometry, uv_set, material_names = _build_geometry_and_uv(mesh_obj, geo_id, uv_id)
    mats_lib, mats_scene, materials_manifest = _build_materials(
        mesh_obj, mesh_id, geo_id, uv_id, material_names, textures_dir, bake_textures)
    morphs_lib, morphs_scene = _build_morphs(mesh_obj, geo_id, f"{geo_id}-1", mesh_id,
                                              morph_smooth_iterations, morph_smooth_factor,
                                              force_active=hide_shapekey_names)

    node_id = mesh_id
    node_entry = _prop_node_entry(node_id, mesh_obj.name, end_point)
    geometry_instance = {
        "id": f"{geo_id}-1", "url": f"#{geo_id}", "name": geometry["name"],
        "label": mesh_obj.name, "type": "polygon_mesh",
    }

    return {
        "label": mesh_obj.name,
        "node_id": node_id,
        "node_entry": node_entry,
        "geometry": geometry,
        "uv_set": uv_set,
        "geometry_instance": geometry_instance,
        "modifiers_lib": morphs_lib,
        "modifier_instances": morphs_scene,
        "materials_lib": mats_lib,
        "materials_scene": mats_scene,
        "materials_manifest": materials_manifest,
    }


def _rig_transfer_body_lines(attached_mesh_labels, root_label):
    """
    Builds the rig-transfer logic as a self-contained IIFE
    (`["(function(){", ..., "})();"]`) that promotes every attached-mesh
    PROP (see _build_mesh_prop) to a real figure and transfers genuine bone
    weights onto it from the root figure, via Daz's own Transfer Utility -
    see _build_mesh_prop's docstring for why this replaces the old
    conform_target approach. Factored out from _write_rig_transfer_script so
    _write_combined_fixup_script can also embed this logic - see that
    function's docstring for why a combined single-script output exists.

    Each attached mesh gets its own throwaway root bone name
    ("<mesh_id>_root") to convert around - doesn't need to mean anything,
    DzTransferUtility's projected weights are what actually end up driving
    deformation, not this bone's placement.
    """
    lines = [
        "(function(){",
        f"    var rootLabel = {_js_str(root_label)};",
        "    var root = Scene.findNodeByLabel(rootLabel);",
        "    var transferred = [];",
        "    var missing = [];",
        "    if (!root) { missing.push(rootLabel + \":<root not found>\"); return {transferred: transferred, missing: missing}; }",
        "",
        "    var attachedLabels = [" + ", ".join(_js_str(lbl) for lbl in attached_mesh_labels) + "];",
        "    for (var i = 0; i < attachedLabels.length; i++) {",
        "        var label = attachedLabels[i];",
        "        var propNode = Scene.findNodeByLabel(label);",
        "        if (!propNode) { missing.push(label + \":<node not found>\"); continue; }",
        "",
        "        var rootBoneName = label.replace(/[^A-Za-z0-9_]/g, '_') + '_root';",
        "        var newFig = new DzFigure();",
        "        var converted = newFig.convertPropToFigure(propNode, rootBoneName, false, false);",
        "        if (!converted) { missing.push(label + \":<convertPropToFigure failed>\"); continue; }",
        "",
        "        var xfer = new DzTransferUtility();",
        "        xfer.setSource(root);",
        "        xfer.setTarget(converted);",
        "        xfer.setTransferBinding(true);",
        "        xfer.setTransferUVs(false);",
        "        xfer.setTransferMorphs(false);",
        "        xfer.setFitToFigure(true);",
        "        xfer.setParentToFigure(true);",
        "        xfer.setMergeHierarchies(false);",
        "        var ok = xfer.doTransfer();",
        "        if (ok) { transferred.push(label); } else { missing.push(label + \":<doTransfer failed>\"); }",
        "    }",
        "",
        "    return {transferred: transferred, missing: missing};",
        "})();",
    ]
    return lines


def _write_rig_transfer_script(attached_mesh_labels, root_label, script_path):
    """Standalone-file wrapper around _rig_transfer_body_lines - kept for
    callers that want just this half (none currently do; export_duf() uses
    the combined output below, see _write_combined_fixup_script)."""
    lines = [
        "// Auto-generated by blender_daz_exporter - promotes each attached",
        "// mesh prop to a figure and transfers real bone weights onto it",
        "// from the root figure via Daz's Transfer Utility.",
        "// See DUF_EXPORT_NOTES.md 'Attached-mesh rigging: Transfer Utility'",
        "// for why this replaced the old conform_target approach.",
    ] + _rig_transfer_body_lines(attached_mesh_labels, root_label)

    script_text = "\n".join(lines)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    return script_path


def _write_combined_fixup_script(materials_manifest_by_node, attached_mesh_labels, root_label,
                                  script_path, preset_path=_BUNDLED_UBER_BASE_PRESET):
    """
    One companion script combining BOTH post-load fixups a multi-mesh
    export_duf() can need - the material shader-class fixup
    (_material_fixup_body_lines) and the attached-mesh rig transfer
    (_rig_transfer_body_lines) - so the user only has to find and run ONE
    .dsa instead of two. This is the practical ceiling on "no script needed"
    for this exporter: DzUberIrayMaterial genuinely cannot be built from raw
    merged DSON (see _material_fixup_body_lines' docstring for the isolation
    tests behind that conclusion, including one against a genuinely separate
    material .dsf file), so *some* post-load step is unavoidable - cutting
    two required scripts down to one is the real, achievable win.

    Each block's own self-contained IIFE (`(function(){...})();`, built by
    the `_..._body_lines` helpers) is reused completely unmodified - the
    only change from either standalone-file version is prefixing its first
    line with `var <name> = `, which is valid JS purely because each block's
    own trailing `})();` already terminates that as one complete statement.

    Order between the two blocks doesn't matter (they touch disjoint scene
    state) - material fixup runs first here only so its result appears
    first in the combined return value.
    """
    material_lines = _material_fixup_body_lines(materials_manifest_by_node, preset_path)
    material_lines[0] = "    var materialResult = " + material_lines[0]
    rig_lines = _rig_transfer_body_lines(attached_mesh_labels, root_label)
    rig_lines[0] = "    var rigTransferResult = " + rig_lines[0]

    lines = [
        "// Auto-generated by blender_daz_exporter - combined post-load fixup.",
        "// (1) upgrades DzDefaultMaterial zones to Iray Uber Base and restores",
        "//     their channel values/maps - see 'Materials: shader-class fixup'",
        "//     in DUF_EXPORT_NOTES.md for why this can't be done from the .duf",
        "//     alone (Daz's scene-merge parser never promotes a material past",
        "//     DzDefaultMaterial from raw JSON, confirmed multiple ways).",
        "// (2) promotes each attached mesh prop to a figure and transfers real",
        "//     bone weights from the root figure via Daz's Transfer Utility -",
        "//     see 'Attached-mesh rigging: Transfer Utility' in the same doc.",
        "(function(){",
    ] + material_lines + [""] + rig_lines + [
        "",
        "    return {materials: materialResult, rigTransfer: rigTransferResult};",
        "})();",
    ]
    script_text = "\n".join(lines)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_text)
    return script_path


def export_duf(mesh_objs, armature_obj, filepath, asset_name=None,
                presentation_type=None, preferred_base=None,
                morph_smooth_iterations=2, morph_smooth_factor=0.5,
                root_mesh_name=None, bake_textures=True):
    """
    Export one or more meshes (all rigged to `armature_obj` via vertex groups
    matching bone names) as a self-contained Daz Studio native .duf scene
    file.

    mesh_objs: a single mesh Object, or a list of mesh Objects.

    bake_textures=False skips Cycles baking for procedurally-fed material
    channels entirely, going straight to reusing whatever real Image
    Texture is upstream instead (see _iray_overrides' resolve() docstring
    for the full story). Baking is usually the more ACCURATE choice
    (correctly flattens Mix/AO/ColorRamp chains into a texture), but it can
    produce a flatly WRONG result for materials combining an image with a
    procedural Noise Texture on small/thin mesh regions (confirmed live: a
    tail, baked repeatedly, reproducibly came out as a flat solid-color
    patch instead of showing the material's real fine-grain detail).
    Turning this off trades exact multi-layer fidelity for a predictable,
    non-broken result when baking is doing more harm than good.

    ARCHITECTURE: one mesh is the ROOT figure - its own geometry + a full
    skeleton copy, i.e. what you'd actually pose (root_mesh_name picks
    which one, must match one of mesh_objs' .name; defaults to mesh_objs[0]
    if not given). Every OTHER mesh is exported as a standalone PROP (see
    _build_mesh_prop) - geometry + materials + morphs, no skeleton/
    skin_binding. A companion "_fixup.dsa" script (see
    _write_combined_fixup_script, path returned as duf["_fixup_script"])
    promotes each prop to a figure and transfers real bone weights onto it
    from the root figure via Daz's own Transfer Utility once run after load
    - see _build_mesh_prop's docstring for why this replaced an earlier
    conform_target-based approach that loaded fine but never actually
    deformed with the root's pose - and also does the material shader-class
    fixup every export needs (see _material_fixup_body_lines' docstring),
    combined into that same one script so there's only one companion file
    to find and run instead of two. A single-mesh export is just the
    degenerate case with zero attachments (and no rig-transfer script, since
    there's nothing to transfer onto).

    Pass presentation_type (e.g. "Follower/Wardrobe/Top") and preferred_base
    (e.g. "/MyFox/Body") to mark the ROOT figure as conforming clothing
    Daz's own "Fit to" feature can target from an externally-loaded body,
    instead of a standalone figure. This is orthogonal to the attached-mesh
    conform_target above: it's for when the whole export (root + its
    attachments) is itself meant to be worn by something else.

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

    if root_mesh_name:
        matches = [m for m in mesh_objs if m.name == root_mesh_name]
        if not matches:
            raise ValueError(f"root_mesh_name {root_mesh_name!r} not found among the meshes being exported")
        root_mesh = matches[0]
    else:
        root_mesh = mesh_objs[0]
    attached_meshes = [m for m in mesh_objs if m is not root_mesh]

    asset_name = asset_name or root_mesh.name

    # Baked/complex-material textures land next to the export file, matching
    # how the reference Blender-to-Daz plugin bakes "simplified texture maps".
    textures_dir = os.path.join(os.path.dirname(os.path.abspath(filepath)),
                                 _safe_id(asset_name) + "_textures")

    def instance_id(lib_id):
        # See long comment on scene_nodes below for why this suffix exists.
        return f"{lib_id}-1"

    root_figure = _build_mesh_figure(root_mesh, armature_obj, textures_dir, True,
                                      morph_smooth_iterations, morph_smooth_factor, bake_textures)
    attached_props = [
        _build_mesh_prop(m, textures_dir, morph_smooth_iterations, morph_smooth_factor, bake_textures)
        for m in attached_meshes
    ]

    node_library = list(root_figure["node_library"])
    geometries = [root_figure["geometry"]]
    uv_sets = [root_figure["uv_set"]]
    all_modifiers = list(root_figure["modifiers_lib"])
    materials_lib = list(root_figure["materials_lib"])
    materials_scene = list(root_figure["materials_scene"])
    modifier_instances = list(root_figure["modifier_instances"])
    materials_manifest_by_node = [(root_figure["label"], root_figure["materials_manifest"])]
    for prop in attached_props:
        node_library.append(prop["node_entry"])
        geometries.append(prop["geometry"])
        uv_sets.append(prop["uv_set"])
        all_modifiers.extend(prop["modifiers_lib"])
        materials_lib.extend(prop["materials_lib"])
        materials_scene.extend(prop["materials_scene"])
        modifier_instances.extend(prop["modifier_instances"])
        materials_manifest_by_node.append((prop["label"], prop["materials_manifest"]))

    # Scene-section objects are INSTANCES of library-section definitions and
    # need their own ids distinct from the library id they instance ("url"
    # points at the library id via "#id"). Real multi-file Daz assets get
    # away with reusing the same string for both because the library lives in
    # a separate .dsf file from the scene .duf - reusing it here, in one
    # self-contained file, produces Daz Studio's "duplicate ids found" import
    # error (confirmed live). Suffix every scene-instance id with "-1",
    # matching the convention Daz's own exporter uses for material instances
    # (e.g. "bearTongue-1").
    root_figure_root_instance_id = instance_id(root_figure["figure_id"])
    scene_nodes = []
    for i, (bid, label, parent_id, _center, _end) in enumerate(root_figure["node_entries"]):
        n = {"id": instance_id(bid), "url": f"#{bid}", "name": bid, "label": label}
        if parent_id:
            # Node_library "parent" only defines the reusable prototype's
            # default; the SCENE graph is actually wired by this
            # instance-level "parent" field (confirmed against a real
            # Daz-authored .duf), and it must point at the PARENT's
            # scene-instance id, not its library id.
            n["parent"] = f"#{instance_id(parent_id)}"
        if i == 0:
            n["geometries"] = [root_figure["geometry_instance"]]
        scene_nodes.append(n)
    for prop in attached_props:
        # Parented to the root purely as a sane default so it's positioned
        # sensibly if the companion rig-transfer script hasn't been run yet -
        # DzTransferUtility's setParentToFigure(true) re-parents it for real
        # once that script runs (see _write_rig_transfer_script).
        n = {
            "id": instance_id(prop["node_id"]), "url": f"#{prop['node_id']}",
            "name": prop["node_id"], "label": prop["label"],
            "parent": f"#{root_figure_root_instance_id}",
            "geometries": [prop["geometry_instance"]],
        }
        scene_nodes.append(n)

    root_node_entry = root_figure["node_library"][0]
    if presentation_type:
        # Matches the real-asset convention for marking something as
        # conforming clothing (e.g. "Follower/Wardrobe/Top") vs. a standalone
        # figure/prop, confirmed against a shipped Daz clothing product.
        root_node_entry["presentation"] = {
            "type": presentation_type,
            "label": "", "description": "", "icon_large": "",
            "colors": [[0.35, 0.35, 0.35], [1, 1, 1]],
        }
        if preferred_base:
            root_node_entry["presentation"]["preferred_base"] = preferred_base

        # "name://@selection:" is the literal, static DSON value a real
        # shipped wardrobe item's scene.nodes[] entry uses to auto-fit onto
        # whatever figure is selected at load time - Daz's own "Fit to"
        # convenience, expressed declaratively (SKILL_DSON_FORMAT.md,
        # daz-mcp-server, 2026-08-11 ground truth: confirmed on a real
        # dForce-ready wardrobe item's node, which ALSO ships a full
        # weight-mapped bone hierarchy - conform_target only actually
        # deforms when it rides on top of real weight-map data that exists
        # first, per that same doc; a hand-authored conform_target with no
        # underlying skin_binding is a confirmed dead end, see the "Replace
        # conform_target..." investigation below and in git history). This
        # export path already builds that real skin_binding from the
        # Blender rig's own vertex-group weights (_build_skin_binding), so
        # the precondition holds - unlike the attached-mesh case below,
        # which instead defers to Daz's Transfer Utility for that. No
        # DazScript needed: this is a pure declarative field on the root
        # figure's SCENE node instance (not its node_library definition -
        # matches every other instance-level-vs-definition split already in
        # this file, e.g. skin_binding/morph "parent").
        scene_nodes[0]["conform_target"] = "name://@selection:"

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

    # Stashed after json.dump() so it can't leak into the written .duf file.
    # One combined script when there's an attached-mesh rig transfer to do
    # too, so the user only has to find and run ONE .dsa instead of two (see
    # _write_combined_fixup_script) - otherwise just the material fixup,
    # unchanged from before.
    if attached_props:
        fixup_script_path = os.path.splitext(filepath)[0] + "_fixup.dsa"
        _write_combined_fixup_script(
            materials_manifest_by_node, [prop["label"] for prop in attached_props],
            root_mesh.name, fixup_script_path)
    else:
        fixup_script_path = os.path.splitext(filepath)[0] + "_fix_materials.dsa"
        _write_material_fixup_script(materials_manifest_by_node, fixup_script_path)
    duf["_fixup_script"] = fixup_script_path

    return duf


def export_duf_prop(mesh_obj, filepath, asset_name=None, bake_textures=True,
                     morph_smooth_iterations=2, morph_smooth_factor=0.5):
    """
    Export a single mesh as a standalone static DSON PROP - geometry and
    materials only, no skeleton/skin_binding at all (unlike export_duf's
    figures). Meant as the source for Daz's own Transfer Utility workflow
    instead of this project's hand-authored conform_target/skin_binding
    approach: load this prop, use DzFigure.convertPropToFigure() to promote
    it to a figure, then run DzTransferUtility (setSource = the posable
    body figure, setTarget = this new figure, setTransferBinding(true)) to
    let Daz itself compute real proximity-projected bone weights. The
    conform_target approach never got attached meshes to actually deform
    with the target's pose (see _build_mesh_figure's docstring for that
    investigation); this sidesteps it entirely by leaning on Daz's own
    weight-transfer engine instead of describing one by hand in DSON.

    Also sidesteps Daz's interactive "OBJ Import Options" dialog (which
    demands a manual scale/axis setup per import and has no scriptable
    silent equivalent) - a merged .duf loads with no dialog at all, same as
    every other file this exporter produces, and vertices are written
    already in Daz cm/Y-up space so the scale is always correct.

    Geometry is baked in WORLD space (mesh_obj.matrix_world), unlike
    export_duf's figure meshes which assume an identity object transform -
    a standalone prop has no bone/figure transform to place it otherwise.
    """
    if mesh_obj.type != "MESH":
        raise ValueError(f"{mesh_obj.name} is not a MESH object")

    asset_name = asset_name or mesh_obj.name
    mesh_id = _safe_id(mesh_obj.name)

    textures_dir = os.path.join(os.path.dirname(os.path.abspath(filepath)),
                                 _safe_id(asset_name) + "_textures")

    geo_id = mesh_id + "Base"
    uv_id = mesh_id + "UV"

    bbox_max_z = max((v[2] for v in mesh_obj.bound_box), default=1.0)
    end_point = to_daz_vec((0.0, 0.0, bbox_max_z))

    hide_shapekey_names = _ensure_hide_material_shapekeys(mesh_obj)
    geometry, uv_set, material_names = _build_geometry_and_uv(mesh_obj, geo_id, uv_id, world_space=True)
    mats_lib, mats_scene, materials_manifest = _build_materials(
        mesh_obj, mesh_id, geo_id, uv_id, material_names, textures_dir, bake_textures)

    node_id = mesh_id
    node_entry = _prop_node_entry(node_id, mesh_obj.name, end_point)

    def instance_id(lib_id):
        return f"{lib_id}-1"

    geometry_instance = {
        "id": instance_id(geo_id), "url": f"#{geo_id}", "name": geometry["name"],
        "label": mesh_obj.name, "type": "polygon_mesh",
    }
    scene_node = {
        "id": instance_id(node_id), "url": f"#{node_id}", "name": node_id,
        "label": mesh_obj.name,
        "geometries": [geometry_instance],
    }

    morphs_lib, morphs_scene = _build_morphs(mesh_obj, geo_id, instance_id(geo_id), mesh_id,
                                              morph_smooth_iterations, morph_smooth_factor,
                                              force_active=hide_shapekey_names)

    duf = {
        "file_version": "0.6.0.0",
        "asset_info": {
            "id": f"/{asset_name}.duf",
            "type": "prop",
            "contributor": {"author": "Blender DUF Exporter", "email": "", "website": ""},
            "revision": "1.0",
        },
        "node_library": [node_entry],
        "geometry_library": [geometry],
        "uv_set_library": [uv_set],
        "material_library": mats_lib,
        "modifier_library": morphs_lib,
        "scene": {
            "nodes": [scene_node],
            "modifiers": morphs_scene,
            "materials": mats_scene,
        },
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(duf, f, indent="\t")

    fixup_script_path = os.path.splitext(filepath)[0] + "_fix_materials.dsa"
    _write_material_fixup_script([(mesh_obj.name, materials_manifest)], fixup_script_path)
    duf["_fixup_script"] = fixup_script_path

    return duf
