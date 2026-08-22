# Blender → Daz Exporter — Task Backlog

Working task list for this addon. Check items off as they land; add new ones as they come up.
Each item has enough context for a fresh session to pick it up cold — read the linked
file/function before starting, don't assume the note below is still 100% current.

## In progress / next up

- [x] **DIM zip export option** — implemented in `dim_package.py` (`build_dim_package()`), wired
  into `EXPORT_OT_daz_duf` in `__init__.py` via a `create_dim_zip` checkbox (+
  `dim_product_name`/`dim_vendor_name`/`dim_content_folder`). Tier 1 only (`Manifest.dsx` +
  `Supplement.dsx` + `Content/...`, no icon/category). Key design point future sessions should
  know: `duf_export.py` writes `image_file` fields as **absolute local-disk paths** (fine for
  loading straight from the export folder, meaningless once DIM installs elsewhere) — so
  `dim_package.py` deep-copies the in-memory `duf` dict returned by `export_duf`/`export_duf_prop`
  and rewrites every `image_file` to a content-library-root-relative path
  (`/Runtime/Textures/<Vendor>/<Product>/<file>`), physically copying the same files into that
  spot under the staged `Content/`. The fixup `.dsa`'s bundled `IrayUberBase.duf` preset path gets
  the same treatment (copied to `Content/Runtime/Support/BlenderDUFExporter/`, script's
  `presetPath` literal string-replaced via `json.dumps()` matching — see
  `_rewrite_fixup_preset_path`). Verified end-to-end against **real Blender 5.2**
  (`blender --background --python`, not just a stub): registered the addon, built a real
  rigged+textured mesh, exported with `create_dim_zip=True` and `bake_textures=True` (real Cycles
  bake), and confirmed the produced zip's shape, the rewritten `image_file` paths, and the
  physically staged textures/preset all match. Not yet tested against a real DIM install (do that
  before considering this fully proven, same caveat the daz-dim-packaging skill flags for Tier 2).
  - Not done: no stable/reused `GlobalID` across re-exports of the same asset (fresh UUID every
    time) — revisit only if that turns out to matter for real usage.
  - Reference: `/mnt/steamdrive/modelStuff/AIHelpers/plugins/daz/skills/daz-dim-packaging/SKILL.md`
    for the full on-disk schema, Tier 1 vs Tier 2 tradeoffs, and the live-confirmed validation
    checklist.

- [x] **Expose export options in the N-panel (3D viewport Sidebar)** — implemented: added
  `DazExportSettings` (`bpy.types.PropertyGroup`, one field per export option, registered as
  `Scene.daz_export_settings`) and `VIEW3D_PT_daz_export` (`bl_space_type="VIEW_3D"`,
  `bl_region_type="UI"`, `bl_category="Daz Export"` — its own tab alongside Item/Tool/View/
  Animation, per the user's screenshot) in `__init__.py`. The panel shows a live
  ready/error label (reusing `_selected_rigged_meshes`) and every export option, then copies
  them onto `EXPORT_OT_daz_duf`'s properties (`op.<name> = settings.<name>`) when its own
  "Export Daz Studio Scene (.duf)..." button is clicked — the button still opens the normal file
  browser (`ExportHelper`'s default `invoke()`), just pre-filled instead of reset to defaults
  every time. File > Export still works unchanged/standalone. Verified against real Blender 5.2
  (`bpy.types.VIEW3D_PT_daz_export` present with correct `bl_space_type`/`bl_region_type`/
  `bl_category`, `Scene.daz_export_settings` present with every field, clean register/unregister
  cycle) — actual on-screen panel rendering/click-through not visually confirmed (headless
  background mode has no GUI to screenshot), but registration-level correctness is proven.

- [x] **Additional Iray Uber material channel coverage** — audited `_iray_overrides` against every
  Blender 5.x Principled BSDF socket and the full `iray_uber_channels_template.json` channel list.
  Added, each verified against real Blender: **Anisotropic/Anisotropic Rotation** →
  `Glossy Anisotropy`/`Glossy Anisotropy Rotations` (direct 1:1, both sides use the same "0-1
  fraction" convention), **Coat IOR** → `Top Coat IOR` (same physical-quantity reasoning as the
  existing base IOR mapping), and **Emission Strength** now multiplies into `Emission Color`'s
  value instead of being silently dropped (Daz's Emission Color channel is an unclamped multiplier,
  so `color * strength` is the direct equivalent of what Blender's own shading already computes —
  see the inline comment in `_iray_overrides` for why this isn't the same kind of guess as Bump
  Strength's cross-application scale gap).
  - Deliberately NOT mapped: **Sheen Weight/Tint/Roughness** — audited the full
    `iray_uber_channels_template.json` channel list, there is no Sheen-equivalent channel in Daz's
    Iray Uber shader at all, so there's nothing to map to (not an oversight).
  - Deliberately deferred, not attempted: **Subsurface Scattering** (Blender's SSS socket set
    changed shape across 3.x→4.x — no separate Subsurface Color input in 4.x, tinting instead comes
    from Base Color — and Daz's SSS/Translucency channels have their own non-obvious
    mode/units semantics; needs live Daz visual verification to get right, not available in this
    environment) and **Displacement** (fed via the Material Output node's Displacement socket, a
    different node-graph entry point than everything `_iray_overrides` currently walks from the
    Principled BSDF — a bigger, separate feature, not a one-line channel addition).

## Backlog (not yet scoped in detail)

- [ ] **Subsurface Scattering material mapping** — split out from the material-channel-coverage
  item above; needs live Daz Studio access to verify SSS Mode/Amount/Color/Direction semantics
  before attempting, see the note there for why it wasn't bundled in.
- [ ] **Displacement material mapping** — split out from the material-channel-coverage item above;
  needs new code that walks from the Material Output node's Displacement input rather than the
  Principled BSDF, unlike every channel `_iray_overrides` currently handles.
- [ ] **Morph/shape-key export improvements** — revisit `_build_morphs`/`_smooth_sparse_deltas` for
  edge cases (e.g. shape keys with non-default `vertex_group` masking, relative-to-relative shape
  key chains, drivers on shape key values).
- [ ] **Texture handling improvements** — UDIM support, texture resolution/format options exposed
  as export settings, avoiding redundant re-bakes when the same material is shared across meshes.
- [ ] **New content type support** — hair (strand-based Blender hair → Daz-native hair asset, if
  feasible) and generic prop export beyond `export_duf_prop`'s current single-mesh-only scope.

## Done (recent, for context — see git log for full history)

- [x] Combine material fixup + rig transfer into one companion script
- [x] Auto-fit standalone clothing exports via declarative `conform_target`
- [x] Expand Iray Uber material coverage: IOR, Clearcoat/Coat, Bump-node
- [x] Replace `conform_target` with Daz's Transfer Utility for attached-mesh rigging
