# Blender → Daz Exporter — Task Backlog

Working task list for this addon. Check items off as they land; add new ones as they come up.
Each item has enough context for a fresh session to pick it up cold — read the linked
file/function before starting, don't assume the note below is still 100% current.

## In progress / next up

- [x] **Subsurface Scattering material mapping** — `_iray_overrides` now maps Blender's Principled
  BSDF SSS sockets onto Daz's `/Volume/Scattering` group + `Thin Walled`, **and** (added in a second
  pass, see "real-asset evidence" below) `/Base/Diffuse/Translucency` + `/Volume/Transmission`.
  Researched via a parallel workflow (Blender-side socket introspection + Daz-side schema/docs
  research, zero Daz Studio interaction), then manual one-call-at-a-time live Daz Studio
  verification, then a second real-asset-grounded refinement pass (see below) — a spawned workflow
  agent was deliberately NOT used for either live/data-touching part: Daz Studio here is a single
  shared, stateful live process, and a subagent can't pause mid-task to ask the user something if a
  load looks risky, the way the main conversation can.
  - **Mapping**: `Subsurface Weight` → `SSS Amount` AND `Translucency Weight` (same source value/map
    driving both — see real-asset evidence below for why both, not just one). `Base Color Effect` is
    force-set to `1` ("Scatter & Transmit") whenever SSS is used. `SSS Color`/`Transmitted Color` =
    the average RGB of the resolved Base Color (a flat value directly if Base Color is unlinked, or
    the pixel-averaged color of the resolved texture via a new `_average_image_color` helper if
    textured — **never the texture itself**, see below for why). `Scattering Measurement Distance`/
    `Transmitted Measurement Distance` (same value for both) = `max(Subsurface Radius channels) *
    Subsurface Scale * 100` (Blender's per-channel Radius vector × Scale, in meters, collapsed to
    Daz's single scalar "measured distance" via the max component, converted to centimeters with the
    same `*100` factor `to_daz_vec` already uses for geometry) — only computed when Radius and Scale
    are both plain (non-texture-driven) values. `Subsurface Anisotropy` (Blender 0-1 forward-
    scattering factor) → `SSS Direction` (Daz's -1..1 Henyey-Greenstein "g" parameter) as a direct
    value copy, same convention as the existing Anisotropic → Glossy Anisotropy mapping. **`Thin
    Walled` is force-set to `false` whenever SSS is used at all** — the load-bearing fix, not a style
    choice: the stock Iray Uber Base preset ships with `Thin Walled = true`, which makes the entire
    `/Volume/Scattering` group inert regardless of what SSS Amount/Color/Distance are set to.
    Deliberately still left untouched: `SSS Mode` (stays at the template's own `Mono` default —
    real-asset evidence below confirmed this is actually the empirically correct choice, not just an
    untested assumption) and `SSS Reflectance Tint`/`Translucency Color` (stayed at their own
    template defaults on every real asset checked).
  - **Real-asset evidence (the actual point of this pass)**: this Daz Studio *session* has no
    purchased skin content loadable in-app, but real purchased products exist as plain `.zip` files
    under `E:\DazStuff\Applications\Data\DAZ 3D\InstallManager\Downloads\` (DIM's own Downloads
    folder — user-pointed) — and since a `.duf` inside one of those zips is just gzip-compressed (or
    plain) JSON, it can be read directly with Python's `zipfile`/`gzip`/`json`, **with zero Daz
    Studio involvement and zero risk of hanging the live server**. Read three real, independent
    assets this way: two commercial Genesis 8 characters (`Reyna`/`Reynard`, different PAs, different
    genders) and Daz's own official `Genesis 8 Basic Female.duf` base figure. All three confirmed,
    consistently:
    1. **Real skin always tunes Translucency Weight + Base Color Effect + Transmitted Color TOGETHER
       with SSS Amount/Color/Distance, never SSS alone** — the original "SSS-group-only" scope this
       mapping first shipped with was incomplete. `Base Color Effect` was `1` on all 3 samples.
       `Translucency Weight` tracked `SSS Amount` at a similar-but-not-identical magnitude (0.5-0.6
       vs 0.69-1.0); `Transmitted Color` tracked `SSS Color` similarly (both warm skin tones, not
       identical). Blender's Principled BSDF has no second, independent control for this — one
       `Subsurface Weight`/Base-Color-derived-tint drives all of it here.
    2. **`SSS Color`/`Transmitted Color`/`SSS Reflectance Tint`/`Translucency Color` never had an
       `image_file` key on any of the 3 samples** — always flat colors, even on materials whose
       `Diffuse Color` *value* was plain white `[1,1,1]` with the real skin tone coming entirely from
       an attached texture map. **This caught a real bug in the first version of this mapping**,
       which reused `diffuse_image` directly for `SSS Color` — meaning any textured-Base-Color
       material (virtually all real skin) would have gotten a meaningless white-with-attached-texture
       `SSS Color`, not a real skin tone. Fixed with the new `_average_image_color` helper (same
       `bpy.data.images` + numpy pixel-averaging idiom `_extract_alpha_channel` already uses) —
       verified in a synthetic test with a known 4-pixel R/G/B/White texture, confirming the averaged
       output and the absence of any `image_file` on the resulting channel.
    3. **`SSS Mode` stayed `Mono` (0) on all 3 samples** — confirms the original mapping's choice to
       leave it untouched (at the template's own Mono default) was empirically correct, not merely
       conservative-by-luck.
    4. **`Scattering Measurement Distance` real-world values were 0.12-2 (i.e. ~1mm-2cm)** — confirms
       the cm-unit hypothesis behind the `*100` conversion, though the exact Blender
       Radius/Scale-to-this-number relationship has no matching Blender-side source asset to check
       the conversion factor itself against.
    5. `Reyna`/`Reynard` (different PAs/genders) had **byte-identical** SSS/Translucency/Transmission
       values — strong evidence these are inherited from a shared vendor/Daz-recommended base skin
       recipe most PAs just don't touch beyond diffuse texture, not independently hand-tuned per
       character; Daz's own base figure had similar-pattern but not identical numbers (its own
       `Thin Walled` was already `false` in its material *library* baseline, and its `SSS Color`
       stayed black/untouched) — so the pattern (which channels get tuned together, Mono mode, no
       image maps) generalizes, but exact magnitudes are genuinely per-asset tunable, not hardcoded.
  - **Also confirmed live** (mechanism, not choice-of-mapping): `setValue()` on an enum property
    (`SSS Mode`, `Base Color Effect`) only accepts the integer index — a string label like
    `"Chromatic"` is silently ignored, value stays unchanged, no error. `Thin Walled` is a
    `DzBoolProperty` — `setValue(false)`/`setValue(0)` both work identically.
  - **Real bug caught and fixed along the way (independent of the above)**: `_js_channel_value_snippet`
    (the fixup-script generator) built its `setValue()` call via an f-string
    (`f"{prop_expr}.setValue({value});"`), fine for numbers but rendering a Python `bool` as
    `False`/`True` — invalid JavaScript. `Thin Walled` is the first bool-typed channel this exporter
    has ever written, so this would have been a silent, invisible failure (only throwing when the
    fixup script actually runs in Daz, not when Blender exports it). Fixed via `json.dumps(value)`,
    a no-op for every numeric value already in use.
  - **Verification, both passes**: full synthetic test (real armature + cube + Principled BSDF,
    Subsurface Weight=0.6, Radius=(0.012, 0.006, 0.003)m, Scale=0.05m, Anisotropy=0.3, both a flat
    and a textured Base Color) — exported, loaded directly into Daz Studio, ran the fixup script via
    `DzScript.loadFromFile()+execute()` (the faithful way to invoke a file-based script — this
    project's own `daz_execute_file` MCP tool turned out to evaluate script *text* inline rather than
    truly running it as a file, which left `getScriptFileName()` empty and gave a red herring failure
    on the first attempt; worth remembering for future DazScript debugging through this tool), and
    read every value back live: `DzUberIrayMaterial`, `SSS Amount=0.6`, `SSS Direction=0.3`,
    `Scattering Measurement Distance=0.06`, `Thin Walled=0` (false), `Translucency Weight=0.6`,
    `Base Color Effect=1`, `Transmitted Measurement Distance=0.06`, `SSS Color=Transmitted
    Color=[0.8,0.5,0.4,1]` matching Base Color — all exactly as designed. Test content/temp files
    cleaned up afterward.
  - **Process note for future sessions**: the initial research (Blender socket introspection + Daz
    schema/docs reading) ran as a 2-agent parallel workflow since it was genuinely parallelizable and
    touched zero Daz Studio state. Both the live Daz Studio verification AND the real-purchased-asset
    reading were done directly in conversation rather than delegated to a workflow agent — the former
    because Daz Studio is a shared live process a subagent can't pause to ask about, the latter
    because it was cheap/fast enough (plain zip/gzip/json reads) not to need parallelizing. **If
    real shipped Daz content is ever needed again for verification, check DIM's own Downloads folder
    first** (`E:\DazStuff\Applications\Data\DAZ 3D\InstallManager\Downloads\` on this machine) — every
    purchased product sits there as a `.zip` alongside its DIM sidecar `.dsx`, and a `.duf`/`.dsf`
    inside can be read straight out of the zip (gzip-decompress if it starts with `\x1f\x8b`) with
    zero Daz Studio involvement and zero risk to the live session.

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

- [x] Add Subsurface Scattering material mapping (+ fix a Python-bool-to-JS fixup-script bug)
- [x] Add Tier 2 DIM packaging (Smart Content .dsx/.dsa + icon registration)
- [x] Fix DIM-installed textures/shader failing to load (fixup script path resolution)
- [x] Add DIM package identity round-trip + Configure DIM Package... popup dialog
- [x] Combine material fixup + rig transfer into one companion script
- [x] Auto-fit standalone clothing exports via declarative `conform_target`
- [x] Expand Iray Uber material coverage: IOR, Clearcoat/Coat, Bump-node
- [x] Replace `conform_target` with Daz's Transfer Utility for attached-mesh rigging
