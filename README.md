# Blender → Daz Studio Native Export (.duf)

A Blender addon that exports a rigged mesh (mesh + armature + vertex-group weights + basic
materials) as a native Daz Studio `.duf` scene file, preserving the custom rig/skinning exactly
as built in Blender — no Genesis-fitting, no Daz SDK plugin, no intermediate FBX/OBJ round-trip.

Background/rationale, format research, and known limitations: see `../DUF_EXPORT_NOTES.md`.

## Why this exists

The generic FBX/OBJ import path into Daz Studio doesn't reliably preserve custom (non-Genesis)
rigging. `.duf` is Daz Studio's own native JSON scene format (DSON) — writing one directly sidesteps
that problem entirely, and also sidesteps needing a compiled Daz SDK plugin (which would need to be
rebuilt per major Daz Studio version — see `../MMDImporter_ANALYSIS.md` for what that entails).

## Install

1. In Blender: **Edit > Preferences > Add-ons > Install from Disk...**
2. Point it at this folder (`blender_daz_exporter`) — or zip the folder first if your Blender
   version requires a zip for "Install from Disk".
3. Enable "Daz Studio Native Export (.duf)" in the add-on list.

## Use

1. Select a mesh object that has an **Armature modifier** pointing at its rig.
2. **File > Export > Daz Studio Scene (.duf)**.
3. In Daz Studio: **File > Import...**, pick the `.duf`, or drag it into the viewport.

## Requirements on the Blender side

- One armature; one or more mesh objects rigged to it via a standard Armature modifier
  (`export_duf()` takes a single mesh Object or a list — pass a list for multi-part figures).
- Vertex group names should match armature bone names exactly (standard Blender rigging
  convention — this is how bone weights get matched to bones in the export).
- An active UV map (recommended; export still works without one, just with no UVs).

## Exporting clothing/wardrobe items

Pass `presentation_type="Follower/Wardrobe/Top"` (or `.../Pant`, `.../Suit`, etc.) and
`preferred_base="/YourFigureName"` to `export_duf()` to mark a mesh as conforming clothing
rather than a standalone figure. The written skeleton is automatically pruned to only the bones
the clothing mesh's vertex groups actually reference (plus ancestors) — matching how real Daz
garment assets ship a reduced skeleton. See `../DUF_EXPORT_NOTES.md` for the full writeup.

## Current scope — see `../DUF_EXPORT_NOTES.md` for the full list

Exports: bone hierarchy + rest-pose transforms (single or multi-mesh figures sharing one
skeleton), mesh geometry (tris/quads; n-gons are fan-triangulated since DSON caps polygons at 4
vertices), UVs, per-vertex-group skin weights, conforming-clothing metadata (pruned skeleton +
`presentation` block), full **Iray Uber Base** materials (Base Color/Metallic/Roughness/Alpha/
Specular/Transmission/Emission/Normal, with packed-texture extraction so materials work even
when the source .blend has no valid external texture paths), and **shape-key morphs** (with
delta smoothing to avoid the jagged/faceted look sparse per-vertex deltas otherwise produce).

Not yet implemented: animation/keyframe export. Known gap: for materials with a genuinely
procedural/blended channel (Mix nodes, ColorRamps, etc. rather than a direct texture link),
proper Cycles baking is implemented but currently fails to execute in this project's Blender MCP
scripting environment specifically (falls back to a heuristic that's only approximately
correct) — see `../DUF_EXPORT_NOTES.md`'s materials section for the full story and whether it's
been resolved since.

## Validated

Round-trip tested end-to-end against a live **Daz Studio 6** instance, both via headless
`blender --background --python` (a synthetic 3-bone test rig) and via the live Blender MCP
bridge + a real "File > Export" click against the user's actual production character (a
95-bone, 6-mesh rigged fox with 39 shape keys and complex node-graph materials). Confirmed live
(not just structurally — actually posing/morphing and checking the resulting bounding box
changes in Daz Studio's own scene state):
- Correct bone hierarchy at any depth (validated on the fox's full skeleton — fingers, tail,
  ears, jaw, breast bones, all nested exactly as in Blender) and correct scale/coordinate
  conversion on every axis.
- **Skin binding actually deforms the mesh** on posing (validated on both the simple test rig
  and the fox's tail/chest bones).
- **Materials import as real Iray Uber Base shaders** with real extracted textures (not the
  bare default shader v1 originally produced).
- **Morphs attach and actually deform the mesh** when their Value channel is set (validated:
  bounding box grew correctly on all 3 axes for an eye-enlarging shape key).

Several real bugs were found and fixed this way, most via a repeating pattern worth knowing if
extending this further: **a modifier's (or node's) `scene`-level instance needs its own
`"parent"` field pointing at the target's *scene instance* id** (not the bare library id) or it
silently fails to attach/wire up, even though loading the file itself reports success with no
error. This bit skin_binding, node hierarchy, and morphs independently — check
`../DUF_EXPORT_NOTES.md` for each specific case before assuming a new DSON object type doesn't
need it. Also documented there: `center_point`/`end_point` being absolute (not parent-relative)
positions, a required `"skin_settings"` block on skin_binding, a DazScript testing gotcha around
stale reads immediately after a scripted merge-load, a packed-texture-extraction fix, and a
Python module-caching gotcha specific to testing this addon via both RPC scripting and the real
Blender UI in the same session.
