"""
Package an exported .duf (+ its textures + fixup .dsa) into a DAZ Install
Manager (DIM) installable .zip - Tier 1 per the daz-dim-packaging skill
(Manifest.dsx + Supplement.dsx + Content/, no icon/category registration
needed for DIM to recognize and install it - live-confirmed end-to-end
against real DIM behavior 2026-08-12, see
AIHelpers/plugins/daz/skills/daz-dim-packaging/SKILL.md for the full schema
this follows).

Only concerned with repackaging what duf_export.py already produced - does
not touch DUF/DSON generation itself. The real surgery this module does:

- Rewrites the in-memory duf dict's "image_file" fields from the absolute
  local-disk paths duf_export writes (correct for loading straight from the
  export folder, but meaningless once DIM installs the zip's contents to
  some other Content Library path) to content-library-root-relative paths
  ("/Runtime/Textures/<Vendor>/<Product>/<file>") matching where this module
  physically stages the same texture files under Content/Runtime/Textures/.
  This form of path (leading "/", resolved against registered Content
  Directories) is only meaningful to Daz's own DSON scene-loading code - see
  the next point for why the fixup .dsa script can't use the same trick.

- Rewrites the companion fixup .dsa script's embedded absolute local-disk
  paths - both the bundled Iray Uber Base preset path (which duf_export.py
  hardcodes as an absolute path to this addon's own installed assets/
  folder, only valid on the machine the addon itself is installed on) and
  every texture setMap() call - into calls to a small JS helper injected at
  the top of the script, which resolves a Content/-relative path to a real
  absolute one AT RUNTIME by walking up from the script's own installed
  location (getScriptFileName()). This is NOT the same "/Runtime/..."
  leading-slash trick used for image_file above - live-confirmed that
  DzMaterial.setMap()/DzContentMgr.openFile() do NOT resolve that style of
  path against registered Content Directories the way DSON's own
  image_file reference does (a real installed file at a real Content
  Directory path still fails to resolve), so the fixup script needs its own
  distinct, actually-working mechanism. See _content_relative_resolver_js
  and _rewrite_fixup_script_paths for the mechanism, and TASKS.md for how
  this was found (a live DIM-install reproduction: textures/shader failed
  to load post-install even though the raw .duf's image_file paths and the
  physically staged files were both correct).
"""
import copy
import json
import os
import pathlib
import random
import re
import shutil
import tempfile
import uuid
import zipfile

from . import duf_export


def _safe_folder(name):
    """Sanitize a vendor/product name for use as an on-disk folder segment
    and Manifest/Supplement display value - doesn't need to already be
    filesystem-safe going in (e.g. a raw Blender object name)."""
    keep = "".join(c if c.isalnum() or c in " _-" else "" for c in (name or "")).strip()
    return keep or "Untitled"


def _collect_and_rewrite_images(duf, dest_root_prefix):
    """Deep-copy `duf`, rewriting every "image_file" value from its current
    absolute source path to `<dest_root_prefix>/<basename>`.

    Returns (new_duf, {source_abs_path: dest_root_relative_path}) - the
    mapping drives the actual file copy in build_dim_package, kept separate
    here so this stays a pure data transform.
    """
    new_duf = copy.deepcopy(duf)
    copies = {}

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "image_file" and isinstance(value, str) and value:
                    basename = os.path.basename(value)
                    dest_rel = f"{dest_root_prefix}/{basename}"
                    copies[value] = dest_rel
                    obj[key] = dest_rel
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(new_duf)
    return new_duf, copies


def _content_relative_resolver_js(depth):
    """JS helper injected once into the fixup script: resolves a
    Content/-relative path (e.g. "Runtime/Support/.../Foo.duf") to a real
    absolute filesystem path AT RUNTIME, by walking up from the fixup
    script's own installed location (getScriptFileName()) rather than
    baking any path in ahead of time.

    Necessary because DzMaterial.setMap()/DzContentMgr.openFile() do NOT
    resolve a leading-slash "/Runtime/..." content-root-relative path
    against registered Content Directories the way a DSON "image_file"
    reference does internally - live-confirmed: DzFile("/Runtime/...").
    exists() is false even when the file is genuinely present under a real
    registered content directory (openFile() on that same string then fails
    outright, "Failed to apply Iray Uber Base preset..."). And the real
    install location is unknown until DIM actually installs the zip
    somewhere, so no absolute path can be baked in ahead of time either -
    this is what actually broke texture/preset loading after a real DIM
    install (see TASKS.md).

    `depth` is how many directory levels separate the fixup script's own
    folder (Content/<content_folder>/<vendor>/<product>/) from Content/
    itself - computed by the caller from the actual content_folder/vendor/
    product path segments, not assumed fixed, since content_folder can
    itself contain slashes (e.g. real Daz content folders like "Shader
    Presets/Iray").
    """
    return (
        "    function __dazDimContentPath(relPath) {\n"
        "        var dir = new DzDir(new DzFile(getScriptFileName()).path());\n"
        f"        for (var __dazDimUp = 0; __dazDimUp < {depth}; __dazDimUp++) {{ dir.cdUp(); }}\n"
        "        return dir.filePath(relPath);\n"
        "    }\n"
    )


def _rewrite_fixup_script_paths(script_text, image_copies, preset_content_rel_path, depth):
    """Rewrites every absolute local-disk path the fixup script currently
    embeds - the bundled Iray Uber Base preset path, and every texture
    setMap() call - into a call to the runtime path-resolving helper above,
    and injects that helper's definition once. Both categories of path are
    embedded via json.dumps() (see duf_export._js_str), so matching on that
    exact literal (rather than a looser path substring) is the reliable way
    to find and replace them regardless of OS path separators.

    image_copies is the same {source_abs_path: content_root_relative_path}
    mapping _collect_and_rewrite_images already built for rewriting the raw
    .duf's own "image_file" fields - the fixup script's setMap() calls
    reference the exact same source paths (both are derived from the same
    _iray_overrides() output), so it doubles as the substitution table here
    with no separate bookkeeping needed.
    """
    old_preset_literal = json.dumps(duf_export._BUNDLED_UBER_BASE_PRESET)
    if old_preset_literal not in script_text:
        raise ValueError(
            "Fixup script does not contain the expected bundled preset path literal - "
            "was it generated with a non-default preset_path?"
        )
    new_preset_expr = f"__dazDimContentPath({json.dumps(preset_content_rel_path)})"
    script_text = script_text.replace(f"var presetPath = {old_preset_literal};",
                                       f"var presetPath = {new_preset_expr};")

    for src, dest_rel in image_copies.items():
        old_map_literal = json.dumps(src)
        new_map_expr = f"__dazDimContentPath({json.dumps(dest_rel.lstrip('/'))})"
        script_text = script_text.replace(f".setMap({old_map_literal})",
                                           f".setMap({new_map_expr})")

    if re.search(r'\.setMap\(\s*"[A-Za-z]:/', script_text):
        raise ValueError(
            "Fixup script still has a setMap() call pointing at a raw local-disk path after "
            "rewriting - image_copies is missing an entry for it"
        )

    return script_text.replace("(function(){\n", "(function(){\n" + _content_relative_resolver_js(depth), 1)


def _manifest_dsx(content_root, global_id):
    """Per daz-dim-packaging skill Step 6 - one <File> entry per file that
    actually exists under Content/, generated by walking the staged
    directory rather than hand-listing."""
    lines = ['<DAZInstallManifest VERSION="0.1">', f' <GlobalID VALUE="{global_id}"/>']
    for f in sorted(content_root.rglob("*")):
        if f.is_file():
            rel = f.relative_to(content_root.parent).as_posix()
            lines.append(f' <File TARGET="Content" ACTION="Install" VALUE="{rel}"/>')
    lines.append("</DAZInstallManifest>")
    return "\n".join(lines)


def _supplement_dsx(product_name, product_tags="DAZStudio4_5"):
    """Per daz-dim-packaging skill Step 5 - live-confirmed this is where
    DIM's "Ready to Install" list gets its displayed Product Name/Tag from."""
    return (
        '<ProductSupplement VERSION="0.1">\n'
        f' <ProductName VALUE="{product_name}"/>\n'
        ' <InstallTypes VALUE="Content"/>\n'
        f' <ProductTags VALUE="{product_tags}"/>\n'
        "</ProductSupplement>"
    )


_REGISTRATION_DSA = (
    "// DAZ Studio version 0.0.0.0 filetype DAZ Script\n"
    "if( App.version >= 67109158 ) //4.0.0.294\n"
    "{\n"
    "\tvar oFile = new DzFile( getScriptFileName() );\n"
    "\tvar oAssetMgr = App.getAssetMgr();\n"
    "\tif( oAssetMgr )\n"
    "\t{\n"
    "\t\toAssetMgr.queueDBMetaFile( oFile.baseName() );\n"
    "\t}\n"
    "}\n"
)


def _content_db_install_dsx(product_name, global_id, asset_rel_path, content_type, category,
                             compatibility, compatibility_base, author_name,
                             dsx_rel_path, support_asset_rel_paths):
    """Per daz-dim-packaging skill Step 3 (Tier 2 only) - registers the
    package into Daz Studio's own Content Database so Smart Content can
    filter/badge it correctly. `global_id` must be byte-identical to
    Manifest.dsx's GlobalID (skill's validation item 6) - both come from
    the same _resolve_global_id() call in build_dim_package, never
    generated independently. `Audience` is hardcoded to "Teens" - the only
    value the skill has actually observed on a real shipped product; no
    live-confirmed alternative exists to expose as a choice.

    dsx_rel_path is this .dsx's own Content/-relative path (becomes the
    <SupportAssets VALUE> attribute, per the skill); support_asset_rel_paths
    lists every file the <Asset> depends on but that isn't itself browsable
    - the three icons here (grid/tooltip/product)."""
    compat_base_line = (
        f'     <CompatibilityBase VALUE="{compatibility_base}"/>\n' if compatibility_base else ""
    )
    support_asset_lines = "\n".join(
        f'    <SupportAsset VALUE="{p}"/>' for p in support_asset_rel_paths
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ContentDBInstall VERSION="1.0">\n'
        " <Products>\n"
        f'  <Product VALUE="{product_name}">\n'
        '   <StoreID VALUE=""/>\n'
        f'   <GlobalID VALUE="{global_id}"/>\n'
        '   <ProductToken VALUE=""/>\n'
        f'   <Artists><Artist VALUE="{author_name}"/></Artists>\n'
        "   <Assets>\n"
        f'    <Asset VALUE="{asset_rel_path}">\n'
        f'     <ContentType VALUE="{content_type}"/>\n'
        '     <Audience VALUE="Teens"/>\n'
        f'     <Categories><Category VALUE="{category}"/></Categories>\n'
        '     <Tags><Tag VALUE=""/></Tags>\n'
        f'     <Compatibilities><Compatibility VALUE="{compatibility}"/></Compatibilities>\n'
        f"{compat_base_line}"
        '     <Userwords><Userword VALUE=""/></Userwords>\n'
        "    </Asset>\n"
        "   </Assets>\n"
        f'   <SupportAssets VALUE="/{dsx_rel_path}">\n'
        f"{support_asset_lines}\n"
        "   </SupportAssets>\n"
        "  </Product>\n"
        " </Products>\n"
        "</ContentDBInstall>"
    )


def _make_icon(width, height, dest_path, source_image_path=None):
    """Produce one Tier 2 icon PNG at dest_path, exactly width x height.

    If source_image_path is given, center-crop it to the target aspect
    ratio then resize (no distortion). Otherwise generate a flat
    placeholder - icon generation/rendering has no confirmed scriptable
    path in this project for arbitrary Blender content (per the
    daz-dim-packaging skill), so a placeholder is what lets Tier 2
    registration prove out end-to-end; real per-product artwork can
    replace these three files by hand later without touching anything
    else in the package.

    Uses bpy.data.images rather than an external imaging library since
    this module only ever runs inside Blender's own process (imported by
    the export operator) - same approach duf_export.py already uses for
    _resolve_image_file/_extract_alpha_channel.
    """
    import bpy
    import numpy as np

    if source_image_path and os.path.isfile(source_image_path):
        src_img = bpy.data.images.load(source_image_path)
        try:
            src_w, src_h = src_img.size
            if src_w == 0 or src_h == 0:
                raise ValueError(f"Icon source image has zero size: {source_image_path}")
            channels = src_img.channels
            pixels = np.empty(src_w * src_h * channels, dtype=np.float32)
            src_img.pixels.foreach_get(pixels)
            pixels = pixels.reshape((src_h, src_w, channels))

            target_aspect = width / height
            src_aspect = src_w / src_h
            if src_aspect > target_aspect:
                crop_w = max(1, round(src_h * target_aspect))
                crop_h = src_h
            else:
                crop_w = src_w
                crop_h = max(1, round(src_w / target_aspect))
            x0 = (src_w - crop_w) // 2
            y0 = (src_h - crop_h) // 2
            cropped = pixels[y0:y0 + crop_h, x0:x0 + crop_w, :]

            work_img = bpy.data.images.new("__daz_dim_icon__", width=crop_w, height=crop_h,
                                            alpha=(channels == 4))
            work_img.pixels.foreach_set(cropped.reshape(-1))
            work_img.scale(width, height)
        finally:
            bpy.data.images.remove(src_img)
    else:
        work_img = bpy.data.images.new("__daz_dim_icon__", width=width, height=height, alpha=False)
        flat = np.tile(np.array([0.55, 0.55, 0.55, 1.0], dtype=np.float32), width * height)
        work_img.pixels.foreach_set(flat)

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        work_img.filepath_raw = dest_path
        work_img.file_format = "PNG" if dest_path.lower().endswith(".png") else "JPEG"
        work_img.save()
    finally:
        bpy.data.images.remove(work_img)


def _resolve_global_id(global_id):
    """Blank -> fresh UUID (today's behavior, safe default for a genuinely
    new package). Non-blank -> validated and reused as-is, so the caller can
    pin the same GlobalID across re-exports of the same product - per the
    daz-dim-packaging skill, DIM identifies a package by this value (plus
    the Manifest's own bookkeeping), and it should never change once a
    version has been distributed."""
    if not global_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(global_id))
    except ValueError:
        raise ValueError(f'Global ID "{global_id}" is not a valid UUID')


def _resolve_product_num_id(product_num_id):
    """Blank -> fresh random 8-digit id (today's behavior). Non-blank ->
    validated numeric and reused as-is (zero-padded to 8 digits, matching
    the "IM<8-digit product ID>" naming convention), so the caller can
    target/overwrite an existing installed package's zip-filename slot
    instead of always producing a new one."""
    if not product_num_id:
        return f"{random.randint(10000000, 99999999)}"
    text = str(product_num_id).strip()
    if not text.isdigit():
        raise ValueError(f'Product Number ID "{product_num_id}" must be numeric')
    return f"{int(text):08d}"


def build_dim_package(duf, fixup_script_path, asset_basename, out_dir, product_name,
                       vendor_name="Blender Export", content_folder="Props",
                       global_id=None, product_num_id=None, product_tags="DAZStudio4_5",
                       tier2=False, content_type=None, category=None, compatibility=None,
                       compatibility_base=None, author_name="", icon_image_path=None):
    """
    Package `duf` (the dict export_duf()/export_duf_prop() returned, still
    carrying its "_fixup_script" bookkeeping key - popped here, never
    written to disk) plus the companion fixup .dsa at fixup_script_path into
    a DIM-installable .zip written into out_dir. Tier 1 (Manifest.dsx +
    Supplement.dsx + Content/, DIM-installable but shows a broken-icon
    placeholder in Smart Content) by default; pass tier2=True to also add
    Runtime/Support ContentDBInstall .dsx/.dsa + icons, registering the
    package into Daz Studio's own Content Database for a real thumbnail and
    type/category badge - see the daz-dim-packaging skill for what each
    tier actually gets you.

    asset_basename is the .duf's filename without extension (pass the
    export operator's own chosen filename, not duf["asset_info"]["id"] -
    users may pick a different export filename than the mesh's Blender
    name).

    vendor_name/product_name become on-disk folder segments
    (Content/<content_folder>/<vendor>/<product>/...) and the Supplement.dsx
    display name - sanitized internally, don't need to already be
    filesystem-safe. content_folder is purely cosmetic for Tier 1 (any
    string works - see daz-dim-packaging skill); defaults to "Props" as a
    safe generic choice since this exporter has no reliable way to know the
    real Daz content-type taxonomy leaf for arbitrary Blender content. For
    Tier 2, content_folder still only affects the on-disk path - it's
    content_type/category below that actually drive Smart Content's badge
    and category-tree placement, and the exporter can't guess those either,
    so the caller (the DIM config dialog) must supply real taxonomy values.

    global_id/product_num_id: leave None/blank to auto-generate a fresh
    identity (safe default for a genuinely new package, matches the old
    always-random behavior). Pass a previously-used value to make this
    export update/overwrite that same package's slot in DIM instead of
    installing alongside it as a separate entry - see _resolve_global_id/
    _resolve_product_num_id for the validation each undergoes.

    tier2=True requires content_type and category (both non-empty - Smart
    Content needs a real ContentType and at least one Category per the
    skill's validation checklist, and there's no sane generic default to
    fall back to). compatibility defaults to "/AnySurface" if left blank -
    the skill's own suggested default for genuinely generic content, which
    is the only kind this exporter can assume for arbitrary Blender meshes.
    compatibility_base is optional (only items with their own identity, per
    the skill, get one) and author_name is cosmetic (Artists block).
    icon_image_path is optional - if given, it's center-cropped/resized to
    the three required icon sizes; if omitted, a flat placeholder is
    generated instead (see _make_icon) so Tier 2 registration still proves
    out end-to-end without requiring real artwork up front.

    Returns (zip_path, global_id, product_num_id) - the caller should
    persist the resolved global_id/product_num_id (whether freshly
    generated here or passed straight through) so a later re-export of the
    same asset can reuse them instead of drifting to a new random identity
    every time.
    """
    if tier2 and (not content_type or not category):
        raise ValueError("Tier 2 requires both a Content Type and a Category")
    vendor = _safe_folder(vendor_name)
    product = _safe_folder(product_name)

    duf = dict(duf)
    duf.pop("_fixup_script", None)

    textures_prefix = f"Runtime/Textures/{vendor}/{product}"
    new_duf, image_copies = _collect_and_rewrite_images(duf, "/" + textures_prefix)

    with open(fixup_script_path, "r", encoding="utf-8") as f:
        fixup_text = f.read()
    support_prefix = "Runtime/Support/BlenderDUFExporter"
    preset_content_rel_path = f"{support_prefix}/IrayUberBase.duf"
    # How many directory levels separate the fixup script's own staged
    # folder (Content/<content_folder>/<vendor>/<product>/) from Content/
    # itself - content_folder can itself contain slashes (real Daz content
    # folders sometimes do, e.g. "Shader Presets/Iray"), so this is computed
    # from the actual path segments rather than assumed to always be 3.
    depth = len([seg for seg in f"{content_folder}/{vendor}/{product}".split("/") if seg])
    fixup_text = _rewrite_fixup_script_paths(fixup_text, image_copies, preset_content_rel_path, depth)

    global_id = _resolve_global_id(global_id)
    product_num_id = _resolve_product_num_id(product_num_id)
    zip_name = f"IM{product_num_id}-01_{product.replace(' ', '')}.zip"
    os.makedirs(out_dir, exist_ok=True)
    out_zip_path = os.path.join(out_dir, zip_name)

    stage_dir = tempfile.mkdtemp(prefix="daz_dim_stage_")
    try:
        content_root = pathlib.Path(stage_dir) / "Content"
        asset_dir = content_root / content_folder / vendor / product
        textures_dir = content_root / textures_prefix
        support_dir = content_root / support_prefix
        for d in (asset_dir, textures_dir, support_dir):
            d.mkdir(parents=True, exist_ok=True)

        with open(asset_dir / f"{asset_basename}.duf", "w", encoding="utf-8") as f:
            json.dump(new_duf, f, indent="\t")
        with open(asset_dir / os.path.basename(fixup_script_path), "w", encoding="utf-8") as f:
            f.write(fixup_text)
        for src, dest_rel in image_copies.items():
            if os.path.isfile(src):
                shutil.copy2(src, content_root / dest_rel.lstrip("/"))
        shutil.copy2(duf_export._BUNDLED_UBER_BASE_PRESET, support_dir / "IrayUberBase.duf")

        if tier2:
            asset_rel_path = f"{content_folder}/{vendor}/{product}/{asset_basename}.duf"
            grid_icon_rel = f"{content_folder}/{vendor}/{product}/{asset_basename}.duf.png"
            tip_icon_rel = f"{content_folder}/{vendor}/{product}/{asset_basename}.tip.png"
            reg_name = f"{vendor}_{product}".replace(" ", "")
            reg_dsx_rel = f"{support_prefix}/{reg_name}.dsx"
            reg_dsa_rel = f"{support_prefix}/{reg_name}.dsa"
            product_icon_rel = f"{support_prefix}/{reg_name}.jpg"

            _make_icon(91, 91, str(content_root / grid_icon_rel), icon_image_path)
            _make_icon(250, 250, str(content_root / tip_icon_rel), icon_image_path)
            _make_icon(114, 148, str(content_root / product_icon_rel), icon_image_path)

            support_asset_rel_paths = [f"/{p}" for p in (product_icon_rel, grid_icon_rel, tip_icon_rel)]
            dsx_text = _content_db_install_dsx(
                product_name, global_id, asset_rel_path,
                content_type, category, compatibility or "/AnySurface", compatibility_base,
                author_name, reg_dsx_rel, support_asset_rel_paths,
            )
            (content_root / reg_dsx_rel).write_text(dsx_text, encoding="utf-8")
            (content_root / reg_dsa_rel).write_text(_REGISTRATION_DSA, encoding="utf-8")

        pathlib.Path(stage_dir, "Manifest.dsx").write_text(
            _manifest_dsx(content_root, global_id), encoding="utf-8")
        pathlib.Path(stage_dir, "Supplement.dsx").write_text(
            _supplement_dsx(product_name, product_tags), encoding="utf-8")

        with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            stage_path = pathlib.Path(stage_dir)
            for f in sorted(stage_path.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(stage_path).as_posix())
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    return out_zip_path, global_id, product_num_id
