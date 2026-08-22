# Blender → Daz Exporter — Task Backlog

Working task list for this addon. Check items off as they land; add new ones as they come up.
Each item has enough context for a fresh session to pick it up cold — read the linked
file/function before starting, don't assume the note below is still 100% current.

## In progress / next up

- [x] **Tier 2 DIM packaging (Smart Content registration)** — `build_dim_package(..., tier2=True, ...)`
  adds the `Runtime/Support` `ContentDBInstall` `.dsx`/`.dsa` + three icons per the
  daz-dim-packaging skill's Tier 2, registering the package into Daz Studio's own Content Database
  for a real thumbnail + type/category badge in Smart Content instead of Tier 1's broken-icon
  placeholder. New `dim_package.py` pieces: `_content_db_install_dsx` (the `.dsx` XML - one
  `<Asset>` for the shipped `.duf`, `content_type`/`category`/`compatibility`/`compatibility_base`/
  `author_name` all caller-supplied since the exporter has no way to guess Daz's taxonomy for
  arbitrary Blender content; `Audience` hardcoded to `"Teens"`, the only value the skill has
  actually observed), `_REGISTRATION_DSA` (the fixed `queueDBMetaFile()` boilerplate, copied
  verbatim per the skill), and `_make_icon` (91×91 grid / 250×250 tooltip / 114×148 product icons
  via `bpy.data.images` — center-crop-then-scale from an optional user-supplied source image, or a
  flat placeholder if none given; no external imaging library needed since this module only ever
  runs inside Blender's own process). `global_id` is shared byte-for-byte between `Manifest.dsx`
  and the `.dsx`'s `<Product><GlobalID>` (both come from the same `_resolve_global_id()` call) —
  the skill's validation item 6 requires this, and it's structurally guaranteed here rather than
  passed twice. Tier 2 requires non-empty `content_type`/`category` (raises otherwise, no sane
  generic fallback exists); `compatibility` defaults to `/AnySurface` if left blank.
  UI: a "Register in Smart Content (Tier 2)" toggle + Content Type/Category/Compatibility/
  Compatibility Base/Icon Image fields, added only to `EXPORT_OT_daz_dim_config`'s popup dialog and
  `DazExportSettings` (not the standalone File > Export sidebar) — deliberately scoped the same way
  Global ID/Product Number ID already were: too many interdependent fields for the plain redo
  panel, and standalone export has no way to supply them anyway.
  **Verified**: structurally in headless Blender (real synthetic export → `build_dim_package(...,
  tier2=True)` → zip contents inspected: `GlobalID` matches byte-for-byte between `Manifest.dsx`
  and the `.dsx`, `ContentType`/`Category`/`Compatibility`/`Artist` all round-trip correctly, `.dsa`
  contains the expected `queueDBMetaFile` call, all three icons come back at the exact required
  pixel dimensions) and live against real Daz Studio: installed the built `Content/` into a real
  registered content directory and ran the registration `.dsa` via `DzScript.loadFromFile()+
  execute()` (same faithful-execution technique the texture-loading fix above required) — it
  completed with no error, meaning `queueDBMetaFile()` accepted the generated `.dsx` without
  complaint. **Not verified**: the actual Smart Content thumbnail/badge rendering itself, which per
  the skill only happens on a real Content Library directory *scan* and requires visually
  inspecting Daz Studio's Smart Content pane — no tool in this project's toolset can drive or read
  that UI (unlike Blender/Chrome, which do have screenshot/automation tools). Test content cleaned
  up from the real content library afterward.

- [x] **Fix: textures/shader failed to load after a real DIM install** — root-caused and fixed via
  a real live-Daz-Studio reproduction (not just code inspection). Symptom: after installing a
  built DIM zip, the material stayed `DzDefaultMaterial` and threw
  `"Failed to apply Iray Uber Base preset..."` when the companion fixup `.dsa` was run.
  Root cause: `dim_package.py` rewrote the fixup script's bundled-preset path to a
  content-root-relative string (`/Runtime/Support/BlenderDUFExporter/IrayUberBase.duf`), the same
  style used for DSON `image_file` references — but that style only resolves via Daz's *own*
  DSON scene-loading code. `DzContentMgr.openFile()`/`DzMaterial.setMap()` do **not** resolve it
  the same way — confirmed live: `DzFile("/Runtime/...").exists()` is `false` even when the file
  is genuinely present under a real registered Content Directory. Worse, every texture `setMap()`
  call in the fixup script was never rewritten at all — it still pointed at the exporting
  machine's original absolute local-disk path, meaningless once DIM installs elsewhere.
  Fix: `dim_package.py` now injects a small JS helper (`_content_relative_resolver_js`) at the top
  of the fixup script that resolves a `Content/`-relative path to a real absolute one **at
  runtime**, by walking up from the script's own installed location via
  `DzFile(getScriptFileName()).path()` + `DzDir.cdUp()` (the up-count is computed from the actual
  `content_folder`/`vendor`/`product` path-segment count, not assumed fixed, since a real Daz
  `content_folder` can itself contain slashes). `_rewrite_fixup_script_paths` replaces both the
  preset-path literal and every `setMap()` literal with calls to this helper, and raises if any
  raw absolute-path `setMap()` call survives the rewrite (`image_copies` — reused from the
  `image_file` rewrite pass — is the substitution table for both).
  **Live-verified end-to-end**, not just unit-level: built a real synthetic export (armature +
  textured mesh) with an external, unpacked texture (the case that previously broke because
  `_resolve_image_file` reuses an existing on-disk file's path as-is rather than copying it into
  the addon's own textures folder), packaged it, extracted the zip's `Content/` into a real
  registered Daz Studio content directory (`C:/Users/.../My Library`) exactly as DIM would, and ran
  the installed fixup script via `DzScript.loadFromFile()+execute()` (the faithful way to invoke a
  file-based script — this project's own `daz_execute_file` MCP tool turned out to evaluate script
  *text* inline rather than truly running it as a file, which left `getScriptFileName()` empty and
  gave a red herring failure during the first verification pass; worth remembering for future
  DazScript debugging through this tool). Confirmed via `daz_list_materials`: the material shader
  correctly promoted from `DzDefaultMaterial` to `DzUberIrayMaterial`, and via a direct property
  query (`isMapped()`/`getMapValue()`) that the Diffuse Color map genuinely loaded the installed
  texture file (not null/unmapped). Test content cleaned up from the real content library
  afterward.

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

- [x] Add Tier 2 DIM packaging (Smart Content .dsx/.dsa + icon registration)
- [x] Fix DIM-installed textures/shader failing to load (fixup script path resolution)
- [x] Add DIM package identity round-trip + Configure DIM Package... popup dialog
- [x] Combine material fixup + rig transfer into one companion script
- [x] Auto-fit standalone clothing exports via declarative `conform_target`
- [x] Expand Iray Uber material coverage: IOR, Clearcoat/Coat, Bump-node
- [x] Replace `conform_target` with Daz's Transfer Utility for attached-mesh rigging
