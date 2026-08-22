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
  - Reference: `/mnt/steamdrive/modelStuff/AIHelpers/plugins/daz/skills/daz-dim-packaging/SKILL.md`
    for the full on-disk schema, Tier 1 vs Tier 2 tradeoffs, and the live-confirmed validation
    checklist.

- [x] **DIM package identity round-trips across re-exports + a dedicated config popup** — fixes
  the "fresh UUID every time" gap noted above. `dim_package.build_dim_package()` now takes
  `global_id`/`product_num_id`/`product_tags` kwargs (all optional) and returns
  `(zip_path, global_id, product_num_id)` instead of just the path — blank in, fresh
  UUID/8-digit-id out (old behavior, unchanged default); non-blank in, validated
  (`_resolve_global_id`/`_resolve_product_num_id`) and reused as-is, so a caller can pin a
  specific package identity to update/overwrite rather than install-alongside. `EXPORT_OT_daz_duf.
  execute()` always writes the resolved (possibly freshly-generated) pair back onto
  `context.scene.daz_export_settings` after a DIM build, regardless of entry point — so the *next*
  export via the N-panel automatically reuses the same identity instead of drifting to a new
  random one, with zero action needed from the user beyond leaving the fields blank the first time.
  Added `DazExportSettings.dim_global_id`/`dim_product_num_id`/`dim_product_tags`/
  `dim_author_name` and a new popup-dialog operator `EXPORT_OT_daz_dim_config`
  (`bl_idname="export_scene.daz_dim_config"`, `invoke_props_dialog`) launched via a "Configure DIM
  Package..." button in `VIEW3D_PT_daz_export`'s DIM section — reads/writes the scene settings
  directly (not a copy-onto-operator-then-run step like the main export button), so edits apply
  immediately on OK. `dim_author_name` is collected but **not yet wired into any output file** —
  Tier 1's `Supplement.dsx`/`Manifest.dsx` have no author field per the daz-dim-packaging skill;
  it's stashed on `DazExportSettings` for when Tier 2 (`Runtime/Support` `ContentDBInstall` .dsx,
  which does have an `<Artists>` block) gets built. Standalone File > Export's own sidebar
  (`EXPORT_OT_daz_duf.draw()`) deliberately does NOT expose Global ID/Product Number ID (only
  `dim_product_tags`, which is harmless standalone) — those two only make sense with the
  scene-settings persistence loop, which standalone-only usage never engages; every standalone
  export still gets a fresh identity every time, same as before this change, so there's no
  regression for that path, just no new persistence benefit either.
  Verified against real headless Blender 5.2: registered the addon, confirmed
  `EXPORT_OT_daz_dim_config` registers (`bpy.ops.export_scene.daz_dim_config`) with every expected
  field, `DazExportSettings`/`EXPORT_OT_daz_duf` both carry the new fields via
  `get_rna_type().properties`, `_resolve_global_id`/`_resolve_product_num_id` validate and
  round-trip correctly (blank → fresh UUID/8-digit id; non-blank → reused verbatim; garbage input →
  clean `ValueError`, caught by the existing `execute()` try/except and reported as an operator
  error same as any other DIM build failure). Then a full synthetic export: built a real 1-bone
  rig + cube mesh, called `export_duf()` + `build_dim_package()` twice back-to-back — first with
  blank IDs (captured the generated `GlobalID`/product id), second passing those same values back
  in — and confirmed the second run produced the **exact same zip filename** and a `Manifest.dsx`
  whose `GlobalID` matched byte-for-byte, proving the reuse path actually works end-to-end, not
  just that the properties exist. Not yet confirmed: the popup dialog's on-screen appearance/click
  interaction (headless, no GUI) — registration-level and data-flow correctness only.

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

- [x] Add DIM package identity round-trip + Configure DIM Package... popup dialog
- [x] Combine material fixup + rig transfer into one companion script
- [x] Auto-fit standalone clothing exports via declarative `conform_target`
- [x] Expand Iray Uber material coverage: IOR, Clearcoat/Coat, Bump-node
- [x] Replace `conform_target` with Daz's Transfer Utility for attached-mesh rigging
