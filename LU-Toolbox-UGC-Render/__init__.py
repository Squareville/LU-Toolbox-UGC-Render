bl_info = {
    "name": "LU Toolbox UGC Render",
    "author": "Christopher Fantauzzo",
    "version": (1, 0, 0),
    "blender": (3, 1, 0),
    "location": "View3D > N-panel > LU UGC Render",
    "description": "Append UGC preset scene, link best LOD, fit camera (position only), and render.",
    "category": "Render",
}

import bpy
import os
from bpy.types import Operator, Panel
from bpy.props import EnumProperty, IntProperty, StringProperty, BoolProperty, FloatProperty

# --- local import (camera fitting in a separate module) ---
from .cam_fit import (
    fit_camera_to_objects,
    collect_render_objects_from_collection,
)

# ---------------------------------------------------------------------------
# Utilities (kept here: appending scene, LOD selection, linking)
# ---------------------------------------------------------------------------

def addon_dir() -> str:
    return os.path.dirname(os.path.realpath(__file__))

def ugc_blend_path() -> str:
    return os.path.join(addon_dir(), "UGC_Renders.blend")

def append_scene(scene_name: str) -> bpy.types.Scene:
    if scene_name in bpy.data.scenes:
        return bpy.data.scenes[scene_name]
    path = ugc_blend_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"UGC_Renders.blend not found at: {path}")

    directory = os.path.join(path, "Scene")
    if not directory.endswith(os.sep):
        directory = directory + os.sep

    bpy.ops.wm.append(
        filepath=os.path.join(directory, scene_name),
        directory=directory,
        filename=scene_name,
        link=False,
        autoselect=False
    )
    if scene_name not in bpy.data.scenes:
        raise RuntimeError(f"Failed to append Scene '{scene_name}' from {path}")
    return bpy.data.scenes[scene_name]

def find_best_lod_collection(scene: bpy.types.Scene):
    candidates = [f"_LOD_{i}" for i in range(0, 5)]
    root = scene.collection

    def all_cols(r):
        stack = [r]
        seen = set()
        while stack:
            c = stack.pop()
            if c.name in seen:
                continue
            seen.add(c.name)
            yield c
            for cc in c.children:
                stack.append(cc)

    cols = list(all_cols(root))
    for suffix in candidates:
        for col in cols:
            if col.name.endswith(suffix):
                return col
    return None

def link_collection_into_scene(collection: bpy.types.Collection, target_scene: bpy.types.Scene):
    root = target_scene.collection

    def is_linked(root_col, col_to_find):
        stack = [root_col]
        while stack:
            c = stack.pop()
            if c == col_to_find:
                return True
            stack.extend(c.children)
        return False

    if not is_linked(root, collection):
        try:
            root.children.link(collection)
        except RuntimeError:
            pass
    return collection

# ---------------------------------------------------------------------------
# Operator & UI
# ---------------------------------------------------------------------------

UGC_TYPES = [
    ("BRICKBUILD", "BrickBuild", "Use the BrickBuild preset scene"),
    ("ROCKET",     "Rocket",     "Use the Rocket preset scene"),
    ("CAR",        "Car",        "Use the Car preset scene"),
]

class LUUGC_OT_RenderIcon(Operator):
    bl_idname = "luugc.render_icon"
    bl_label = "Render Icon"
    bl_options = {'REGISTER', 'UNDO'}

    ugc_type: EnumProperty(name="UGC Type", items=UGC_TYPES, default="BRICKBUILD")
    resolution: IntProperty(name="Resolution", default=512, min=32, soft_max=4096)
    auto_save: BoolProperty(name="Save Render", default=False)
    save_path: StringProperty(name="Output Path", default="", subtype='FILE_PATH')

    # Camera-fit runtime overrides (so you can call from CLI with custom args)
    margin: FloatProperty(name="Framing Margin", default=1.02, min=1.0, max=1.2, description="How much headroom to leave after tight fit")
    center_xy: BoolProperty(name="Auto-Center XY", default=True, description="Allow camera to slide in local X/Y to center the object in view")

    def invoke(self, context, event):
        sc = context.scene
        self.ugc_type = getattr(sc, "luugc_type", self.ugc_type)
        self.resolution = getattr(sc, "luugc_resolution", self.resolution)
        self.auto_save = getattr(sc, "luugc_auto_save", self.auto_save)
        self.save_path = getattr(sc, "luugc_save_path", self.save_path)
        self.margin = getattr(sc, "luugc_margin", self.margin)
        self.center_xy = getattr(sc, "luugc_center_xy", self.center_xy)
        return self.execute(context)

    def execute(self, context):
        src_scene = context.scene
        scene_name = {"BRICKBUILD":"BrickBuild","ROCKET":"Rocket","CAR":"Car"}[self.ugc_type]

        try:
            target_scene = append_scene(scene_name)
        except Exception as e:
            self.report({'ERROR'}, f"Append failed: {e}")
            return {'CANCELLED'}

        # Switch visible scene to the target (keeps UI behavior consistent)
        context.window.scene = target_scene

        # Find and link LOD content
        lod_col = find_best_lod_collection(src_scene)
        if lod_col is None:
            # Fallback: link all top-level renderable objects
            lod_col = bpy.data.collections.new("UGC_Linked_All")
            try:
                target_scene.collection.children.link(lod_col)
            except RuntimeError:
                pass
            for obj in src_scene.objects:
                if obj.parent is None and obj.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME','EMPTY'}:
                    try:
                        lod_col.objects.link(obj)
                    except RuntimeError:
                        pass
        else:
            link_collection_into_scene(lod_col, target_scene)

        objs_to_frame = collect_render_objects_from_collection(lod_col)

        # Set square resolution
        target_scene.render.resolution_x = int(self.resolution)
        target_scene.render.resolution_y = int(self.resolution)

        # Fit the camera tightly (position only)
        cam = target_scene.camera or next((o for o in target_scene.objects if o.type == 'CAMERA'), None)
        if not cam:
            self.report({'ERROR'}, f"No camera found in target scene '{scene_name}'.")
            return {'CANCELLED'}

        fit_camera_to_objects(
            cam=cam,
            scene=target_scene,
            objects=objs_to_frame,
            margin=self.margin,
            allow_xy_center=self.center_xy
        )

        # Render (optional save)
        if self.auto_save and self.save_path:
            ext = os.path.splitext(self.save_path)[1].lower()
            fmt_map = {
                ".png": 'PNG', ".jpg": 'JPEG', ".jpeg": 'JPEG', ".tga": 'TARGA',
                ".tif": 'TIFF', ".tiff": 'TIFF', ".exr": 'OPEN_EXR', ".hdr": 'HDR', ".bmp": 'BMP',
            }
            fmt = fmt_map.get(ext)
            prev_fmt = target_scene.render.image_settings.file_format
            prev_path = target_scene.render.filepath
            try:
                if fmt: target_scene.render.image_settings.file_format = fmt
                outpath = bpy.path.abspath(self.save_path)
                os.makedirs(os.path.dirname(outpath), exist_ok=True)
                target_scene.render.filepath = outpath
                bpy.ops.render.render(write_still=True)
            finally:
                target_scene.render.image_settings.file_format = prev_fmt
                target_scene.render.filepath = prev_path
            self.report({'INFO'}, f"Saved render: {self.save_path}")
        else:
            bpy.ops.render.render('INVOKE_DEFAULT', write_still=False)

        return {'FINISHED'}

class LUUGC_PT_Panel(Panel):
    bl_label = "LU UGC Render"
    bl_idname = "LUUGC_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "LU UGC Render"

    def draw(self, context):
        layout = self.layout
        sc = context.scene

        layout.prop(sc, "luugc_type", text="UGC Type")
        layout.prop(sc, "luugc_resolution", text="Resolution")

        box = layout.box()
        box.label(text="Camera Fit")
        row = box.row(align=True)
        row.prop(sc, "luugc_margin", text="Margin")
        row.prop(sc, "luugc_center_xy", text="Auto-Center XY")

        col = layout.column(align=True)
        col.prop(sc, "luugc_auto_save", text="Save Render")
        sub = col.column(align=True)
        sub.enabled = sc.luugc_auto_save
        sub.prop(sc, "luugc_save_path", text="Output Path")

        op = layout.operator("luugc.render_icon", text="Render Icon", icon='RENDER_STILL')
        op.ugc_type = sc.luugc_type
        op.resolution = sc.luugc_resolution
        op.auto_save = sc.luugc_auto_save
        op.save_path = sc.luugc_save_path
        op.margin = sc.luugc_margin
        op.center_xy = sc.luugc_center_xy

def _register_scene_props():
    bpy.types.Scene.luugc_type = EnumProperty(name="UGC Type", items=UGC_TYPES, default="BRICKBUILD")
    bpy.types.Scene.luugc_resolution = IntProperty(name="Resolution", default=512, min=32, soft_max=4096)
    bpy.types.Scene.luugc_auto_save = BoolProperty(name="Save Render", default=False)
    bpy.types.Scene.luugc_save_path = StringProperty(name="Output Path", default="", subtype='FILE_PATH')
    bpy.types.Scene.luugc_margin = FloatProperty(
        name="Margin", default=1.02, min=1.0, max=1.2,
        description="How much headroom to leave after tight fit"
    )
    bpy.types.Scene.luugc_center_xy = BoolProperty(
        name="Auto-Center XY", default=True,
        description="Allow camera to slide in local X/Y to center the object"
    )

def _unregister_scene_props():
    for p in ("luugc_type","luugc_resolution","luugc_auto_save","luugc_save_path","luugc_margin","luugc_center_xy"):
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)

classes = (LUUGC_OT_RenderIcon, LUUGC_PT_Panel)

def register():
    for c in classes: bpy.utils.register_class(c)
    _register_scene_props()

def unregister():
    _unregister_scene_props()
    for c in reversed(classes): bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
