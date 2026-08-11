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
    "version": (0, 2, 1),
    "blender": (3, 0, 0),
    "location": "File > Export > Daz Studio Scene (.duf)",
    "description": "Export a rigged mesh (or multiple meshes sharing one armature, optionally as conforming clothing) as a native Daz Studio .duf scene file",
    "category": "Import-Export",
}

import bpy
from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty

from . import duf_export


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
        rig_transfer_script = result.get("_rig_transfer_script")
        msg = f"Exported [{names}] -> {self.filepath}"
        if fixup_script:
            msg += f" (material fixup script: {fixup_script})"
        if rig_transfer_script:
            msg += f" (rig transfer script: {rig_transfer_script})"
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


def register():
    bpy.utils.register_class(EXPORT_OT_daz_duf)
    bpy.utils.register_class(MATERIAL_PT_daz_export)
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
    bpy.utils.unregister_class(MATERIAL_PT_daz_export)
    bpy.utils.unregister_class(EXPORT_OT_daz_duf)


if __name__ == "__main__":
    register()
