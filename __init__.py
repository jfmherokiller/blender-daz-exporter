"""
Blender addon wrapper: File > Export > Daz Studio Scene (.duf)

Core export logic lives in duf_export.py (kept import-able standalone for
headless `blender --background --python` testing, or from execute_blender_code
against a live session, without needing the addon registered). See
../DUF_EXPORT_NOTES.md for format background.
"""

bl_info = {
    "name": "Daz Studio Native Export (.duf)",
    "author": "Blender DUF Exporter",
    "version": (0, 3, 0),
    "blender": (3, 0, 0),
    "location": "File > Export > Daz Studio Scene (.duf)",
    "description": "Export a rigged mesh (or multiple meshes sharing one armature, optionally as conforming clothing) as a native Daz Studio .duf scene file",
    "category": "Import-Export",
}

import os

import bpy
from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty, PointerProperty

from . import duf_export
from . import dim_package


def _rigged_children_of_armature(armature_obj):
    """Every MESH anywhere in armature_obj's hierarchy (children_recursive)
    that has an Armature modifier targeting this specific armature - the
    same "rigged" test _selected_rigged_meshes applies per mesh, just
    discovered from hierarchy instead of the selection."""
    meshes = []
    for obj in armature_obj.children_recursive:
        if obj.type != "MESH":
            continue
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and mod.object is armature_obj:
                meshes.append(obj)
                break
    return meshes


def _selected_rigged_meshes(context):
    """Resolve (mesh_objs, armature_obj, error_message_or_None) from the
    current selection.

    Two modes:
    - Select the Armature itself: exports every rigged mesh in its
      hierarchy automatically (children_recursive) - no need to
      multi-select every body part by hand.
    - Select one or more mesh objects directly (existing behavior):
      exports exactly those meshes, as long as they all share one
      armature. Still the right mode for exporting a single item (e.g.
      just the "hoodie" mesh as a standalone conforming-clothing export).
    """
    selected_armatures = [o for o in context.selected_objects if o.type == "ARMATURE"]
    if selected_armatures:
        if len(selected_armatures) > 1:
            return None, None, "Multiple armatures selected - select exactly one"
        armature_obj = selected_armatures[0]
        meshes = _rigged_children_of_armature(armature_obj)
        if not meshes:
            return None, None, (
                f'"{armature_obj.name}" has no child mesh with an Armature modifier '
                "targeting it - nothing to export"
            )
        return meshes, armature_obj, None

    meshes = [o for o in context.selected_objects if o.type == "MESH"]
    if not meshes:
        return None, None, "No mesh objects or armature selected"

    armature_obj = None
    for m in meshes:
        this_arm = None
        for mod in m.modifiers:
            if mod.type == "ARMATURE" and mod.object:
                this_arm = mod.object
                break
        if this_arm is None:
            return None, None, f'"{m.name}" has no Armature modifier with an assigned object'
        if armature_obj is None:
            armature_obj = this_arm
        elif this_arm is not armature_obj:
            return None, None, (
                f'Selected meshes use different armatures ("{armature_obj.name}" vs '
                f'"{this_arm.name}") - export figures separately, one armature per export'
            )
    return meshes, armature_obj, None


class EXPORT_OT_daz_duf(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.daz_duf"
    bl_label = "Export Daz Studio Scene (.duf)"
    bl_description = (
        "Export the selected mesh(es) + their shared armature as a native Daz Studio .duf "
        "scene file. Select the Armature itself to export every rigged mesh in its hierarchy "
        "automatically, or select multiple meshes directly to combine just those into one "
        "multi-part figure (e.g. body + hair + eyes)."
    )
    bl_options = {"REGISTER"}

    filename_ext = ".duf"
    filter_glob: StringProperty(default="*.duf", options={"HIDDEN"})

    export_as_clothing: BoolProperty(
        name="Export as Conforming Clothing",
        description=(
            "Mark this as a wardrobe item (skeleton pruned to only the bones it actually "
            "uses) instead of a standalone figure. Use Daz Studio's own \"Fit To\" feature "
            "after importing both files to make it follow the body figure's pose"
        ),
        default=False,
    )
    presentation_type: StringProperty(
        name="Wardrobe Category",
        description='Daz asset category, e.g. "Follower/Wardrobe/Top", ".../Pant", ".../Suit"',
        default="Follower/Wardrobe/Top",
    )
    preferred_base: StringProperty(
        name="Fits Figure",
        description='Name of the figure this is meant to fit, e.g. "/MyCharacter" '
                     "(cosmetic - shown in Daz's Smart Content matching, does not force-apply Fit To)",
        default="",
    )
    morph_smooth_iterations: IntProperty(
        name="Morph Smoothing Passes",
        description=(
            "Smooth shape-key deltas before export to avoid the jagged/faceted look sparse "
            "per-vertex morph deltas otherwise produce (a known problem with PMX-style morph "
            "import). 0 disables smoothing"
        ),
        default=2, min=0, max=10,
    )
    morph_smooth_factor: FloatProperty(
        name="Morph Smoothing Strength",
        description="How strongly each smoothing pass blends toward the neighbor average (0 = no effect, 1 = full blend)",
        default=0.5, min=0.0, max=1.0,
    )
    root_mesh_name: StringProperty(
        name="Root Mesh",
        description=(
            "Which selected mesh becomes the main posable figure. Every other mesh becomes a "
            "separate attached figure conform_target-ed to it. Leave blank to use the first "
            "mesh in the selection. Ignored when only one mesh is being exported"
        ),
        default="",
    )
    bake_textures: BoolProperty(
        name="Bake Procedural Textures",
        description=(
            "Bake procedurally-fed material channels (Mix/AO/Noise node graphs) to flat "
            "textures via Cycles. Usually more accurate, but can produce a flat, detail-less "
            "result for materials mixing an image with a procedural Noise Texture on small/"
            "thin mesh regions (e.g. a tail). Turn off to skip baking and reuse whatever real "
            "image texture is upstream instead - less exact, but predictable"
        ),
        default=True,
    )
    create_dim_zip: BoolProperty(
        name="Also Build DIM-Installable .zip",
        description=(
            "Additionally package the exported .duf, its textures, and its fixup script into a "
            "DAZ Install Manager (DIM) installable .zip next to the .duf - drop it in DIM's "
            "Downloads folder and click Install. Tier 1 only (no Smart Content icon/category) - "
            "see the daz-dim-packaging skill for what that means"
        ),
        default=False,
    )
    dim_product_name: StringProperty(
        name="Product Name",
        description="Shown in DIM's Ready to Install list. Leave blank to use the export filename",
        default="",
    )
    dim_vendor_name: StringProperty(
        name="Vendor Folder",
        description="On-disk vendor/author folder segment under Content/ (cosmetic - doesn't need to match any storefront name)",
        default="Blender Export",
    )
    dim_content_folder: StringProperty(
        name="Content Folder",
        description='On-disk folder under Content/ this asset installs under, e.g. "Props", "People", "Wardrobe" (cosmetic for a Tier 1 package - any value works)',
        default="Props",
    )

    @classmethod
    def poll(cls, context):
        meshes, armature_obj, _err = _selected_rigged_meshes(context)
        return meshes is not None

    def execute(self, context):
        mesh_objs, armature_obj, err = _selected_rigged_meshes(context)
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}

        kwargs = {
            "morph_smooth_iterations": self.morph_smooth_iterations,
            "morph_smooth_factor": self.morph_smooth_factor,
            "bake_textures": self.bake_textures,
        }
        if self.export_as_clothing:
            kwargs["presentation_type"] = self.presentation_type or None
            kwargs["preferred_base"] = self.preferred_base or None
        if len(mesh_objs) > 1 and self.root_mesh_name:
            kwargs["root_mesh_name"] = self.root_mesh_name

        try:
            result = duf_export.export_duf(mesh_objs, armature_obj, self.filepath, **kwargs)
        except Exception as e:
            self.report({"ERROR"}, f"Export failed: {e}")
            return {"CANCELLED"}

        names = ", ".join(m.name for m in mesh_objs)
        fixup_script = result.get("_fixup_script")
        msg = f"Exported [{names}] -> {self.filepath}"
        if fixup_script:
            msg += f" (run this once after loading: {fixup_script})"

        if self.create_dim_zip:
            asset_basename = os.path.splitext(os.path.basename(self.filepath))[0]
            try:
                zip_path = dim_package.build_dim_package(
                    result, fixup_script, asset_basename,
                    out_dir=os.path.dirname(self.filepath),
                    product_name=self.dim_product_name or asset_basename,
                    vendor_name=self.dim_vendor_name,
                    content_folder=self.dim_content_folder,
                )
                msg += f" | DIM zip -> {zip_path}"
            except Exception as e:
                self.report({"ERROR"}, f"Export succeeded but DIM zip packaging failed: {e}")
                return {"FINISHED"}

        self.report({"INFO"}, msg)
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_as_clothing")
        if self.export_as_clothing:
            layout.prop(self, "presentation_type")
            layout.prop(self, "preferred_base")
        layout.separator()
        layout.prop(self, "root_mesh_name")
        layout.separator()
        layout.prop(self, "bake_textures")
        layout.separator()
        layout.prop(self, "morph_smooth_iterations")
        if self.morph_smooth_iterations > 0:
            layout.prop(self, "morph_smooth_factor")
        layout.separator()
        layout.prop(self, "create_dim_zip")
        if self.create_dim_zip:
            layout.prop(self, "dim_product_name")
            layout.prop(self, "dim_vendor_name")
            layout.prop(self, "dim_content_folder")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_daz_duf.bl_idname, text="Daz Studio Scene (.duf)")


class MATERIAL_PT_daz_export(bpy.types.Panel):
    """"Hide from Daz Export" toggle, shown in the Material Properties tab.

    A checkbox on the material rather than a face/vertex selection because
    material is the natural unit for "geometry Daz's renderer handles badly"
    - it's how the tail fix that motivated this feature was actually found
    (a whole material zone, "HairCards", not an arbitrary face selection).
    export_duf()/_build_mesh_prop()/export_duf_prop() all read this via
    duf_export._ensure_hide_material_shapekeys() at export time.
    """
    bl_label = "Daz Export"
    bl_idname = "MATERIAL_PT_daz_export"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    @classmethod
    def poll(cls, context):
        return context.material is not None

    def draw(self, context):
        layout = self.layout
        mat = context.material
        layout.prop(mat, "daz_hide_on_export")
        if mat.daz_hide_on_export:
            layout.label(text="Faces using this material collapse to ~0 scale on export",
                         icon="INFO")
            layout.label(text="(via an auto-generated shape key - geometry isn't deleted,")
            layout.label(text="so other morphs stay intact)")


class DazExportSettings(bpy.types.PropertyGroup):
    """Scene-level mirror of EXPORT_OT_daz_duf's own properties, so
    VIEW3D_PT_daz_export's N-panel widgets have somewhere to persist their
    values between exports - operator properties alone only remember the
    last-used values via the post-export redo panel, not before the
    operator has ever run. VIEW3D_PT_daz_export copies these onto the
    operator button at draw time (op.<name> = settings.<name>) rather than
    the operator reading them itself, so File > Export still works
    standalone with the operator's own defaults."""
    export_as_clothing: BoolProperty(
        name="Export as Conforming Clothing",
        description=(
            "Mark this as a wardrobe item (skeleton pruned to only the bones it actually "
            "uses) instead of a standalone figure. Use Daz Studio's own \"Fit To\" feature "
            "after importing both files to make it follow the body figure's pose"
        ),
        default=False,
    )
    presentation_type: StringProperty(
        name="Wardrobe Category",
        description='Daz asset category, e.g. "Follower/Wardrobe/Top", ".../Pant", ".../Suit"',
        default="Follower/Wardrobe/Top",
    )
    preferred_base: StringProperty(
        name="Fits Figure",
        description='Name of the figure this is meant to fit, e.g. "/MyCharacter" '
                     "(cosmetic - shown in Daz's Smart Content matching, does not force-apply Fit To)",
        default="",
    )
    root_mesh_name: StringProperty(
        name="Root Mesh",
        description=(
            "Which selected mesh becomes the main posable figure. Every other mesh becomes a "
            "separate attached figure conform_target-ed to it. Leave blank to use the first "
            "mesh in the selection. Ignored when only one mesh is being exported"
        ),
        default="",
    )
    bake_textures: BoolProperty(
        name="Bake Procedural Textures",
        description=(
            "Bake procedurally-fed material channels (Mix/AO/Noise node graphs) to flat "
            "textures via Cycles. Usually more accurate, but can produce a flat, detail-less "
            "result for materials mixing an image with a procedural Noise Texture on small/"
            "thin mesh regions (e.g. a tail). Turn off to skip baking and reuse whatever real "
            "image texture is upstream instead - less exact, but predictable"
        ),
        default=True,
    )
    morph_smooth_iterations: IntProperty(
        name="Morph Smoothing Passes",
        description=(
            "Smooth shape-key deltas before export to avoid the jagged/faceted look sparse "
            "per-vertex morph deltas otherwise produce (a known problem with PMX-style morph "
            "import). 0 disables smoothing"
        ),
        default=2, min=0, max=10,
    )
    morph_smooth_factor: FloatProperty(
        name="Morph Smoothing Strength",
        description="How strongly each smoothing pass blends toward the neighbor average (0 = no effect, 1 = full blend)",
        default=0.5, min=0.0, max=1.0,
    )
    create_dim_zip: BoolProperty(
        name="Also Build DIM-Installable .zip",
        description=(
            "Additionally package the exported .duf, its textures, and its fixup script into a "
            "DAZ Install Manager (DIM) installable .zip next to the .duf - drop it in DIM's "
            "Downloads folder and click Install. Tier 1 only (no Smart Content icon/category) - "
            "see the daz-dim-packaging skill for what that means"
        ),
        default=False,
    )
    dim_product_name: StringProperty(
        name="Product Name",
        description="Shown in DIM's Ready to Install list. Leave blank to use the export filename",
        default="",
    )
    dim_vendor_name: StringProperty(
        name="Vendor Folder",
        description="On-disk vendor/author folder segment under Content/ (cosmetic - doesn't need to match any storefront name)",
        default="Blender Export",
    )
    dim_content_folder: StringProperty(
        name="Content Folder",
        description='On-disk folder under Content/ this asset installs under, e.g. "Props", "People", "Wardrobe" (cosmetic for a Tier 1 package - any value works)',
        default="Props",
    )


class VIEW3D_PT_daz_export(bpy.types.Panel):
    """N-panel (3D viewport Sidebar, press N) tab holding every export
    option ahead of time, so repeated exports don't need the file-browser's
    operator-redo sidebar reopened and re-filled each time. The actual
    export still goes through EXPORT_OT_daz_duf (and still shows its file
    browser) - this panel just pre-fills that operator's properties from
    DazExportSettings when its button is clicked."""
    bl_label = "Export to Daz Studio"
    bl_idname = "VIEW3D_PT_daz_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Daz Export"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.daz_export_settings

        _, _, err = _selected_rigged_meshes(context)
        if err:
            layout.label(text=err, icon="ERROR")
        else:
            layout.label(text="Ready to export", icon="CHECKMARK")

        layout.prop(settings, "export_as_clothing")
        if settings.export_as_clothing:
            layout.prop(settings, "presentation_type")
            layout.prop(settings, "preferred_base")
        layout.separator()
        layout.prop(settings, "root_mesh_name")
        layout.separator()
        layout.prop(settings, "bake_textures")
        layout.separator()
        layout.prop(settings, "morph_smooth_iterations")
        if settings.morph_smooth_iterations > 0:
            layout.prop(settings, "morph_smooth_factor")
        layout.separator()
        layout.prop(settings, "create_dim_zip")
        if settings.create_dim_zip:
            layout.prop(settings, "dim_product_name")
            layout.prop(settings, "dim_vendor_name")
            layout.prop(settings, "dim_content_folder")
        layout.separator()

        op = layout.operator(EXPORT_OT_daz_duf.bl_idname,
                              text="Export Daz Studio Scene (.duf)...", icon="EXPORT")
        op.export_as_clothing = settings.export_as_clothing
        op.presentation_type = settings.presentation_type
        op.preferred_base = settings.preferred_base
        op.root_mesh_name = settings.root_mesh_name
        op.bake_textures = settings.bake_textures
        op.morph_smooth_iterations = settings.morph_smooth_iterations
        op.morph_smooth_factor = settings.morph_smooth_factor
        op.create_dim_zip = settings.create_dim_zip
        op.dim_product_name = settings.dim_product_name
        op.dim_vendor_name = settings.dim_vendor_name
        op.dim_content_folder = settings.dim_content_folder


def register():
    bpy.utils.register_class(EXPORT_OT_daz_duf)
    bpy.utils.register_class(MATERIAL_PT_daz_export)
    bpy.utils.register_class(DazExportSettings)
    bpy.utils.register_class(VIEW3D_PT_daz_export)
    bpy.types.Scene.daz_export_settings = PointerProperty(type=DazExportSettings)
    bpy.types.Material.daz_hide_on_export = bpy.props.BoolProperty(
        name="Hide From Daz Export",
        description=(
            "Collapse every face using this material to near-zero size via an "
            "auto-generated shape key on export, instead of deleting them. Use for "
            "geometry Daz Studio's Iray renderer handles badly (e.g. a disconnected "
            "card-based hair/fur shell casting unwanted shadows) without breaking "
            "other shape keys/morphs, which reference vertices by index and silently "
            "corrupt if geometry is actually deleted"
        ),
        default=False,
    )
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    del bpy.types.Material.daz_hide_on_export
    del bpy.types.Scene.daz_export_settings
    bpy.utils.unregister_class(VIEW3D_PT_daz_export)
    bpy.utils.unregister_class(DazExportSettings)
    bpy.utils.unregister_class(MATERIAL_PT_daz_export)
    bpy.utils.unregister_class(EXPORT_OT_daz_duf)


if __name__ == "__main__":
    register()
